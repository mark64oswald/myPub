"""
server.py — myPub KB MCP server (Phase 4.1 skeleton).

Exposes three tools over stdio:
    - search_chapters(query, mode='interactive', limit=10)
    - compare_concept_across_authors(concept_name, limit_per_author=2)
    - find_prerequisites(concept_name, max_depth=5)

Implementation notes
--------------------
* Uses the standalone fastmcp package (`from fastmcp import FastMCP`),
  pinned in pyproject as fastmcp>=3.0,<4. Not the legacy
  `mcp.server.fastmcp` form.
* Holds one long-lived DuckDB connection. Stdio MCP is single-client; a
  pool buys nothing for read-mostly traffic.
* Pre-loads the sentence-transformers model at startup. The cold-start
  is paid once during MCP boot rather than on the first tool call.
* search_chapters fan-out is FTS + VSS + a graph signal over
  concept_relation. Three modalities are merged via reciprocal rank
  fusion as a placeholder until the real ranking engine lands in
  Prompt 4.5.
* find_prerequisites uses a recursive CTE rather than a DuckPGQ
  variable-length quantifier. Reason: this DuckPGQ build doesn't bind
  the edge variable inside `->{m,n}` quantifiers, so edge-property
  filters (e.g. `relation_type = 'REQUIRES'`) fail to bind. The
  property graph is still declared on connect (it's part of the
  catalog contract) but read traversals use SQL.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Make sibling modules importable when this file is run as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from db import open_catalog  # noqa: E402
from resolution import EntityResolver  # noqa: E402
import ranking  # noqa: E402

LOG = logging.getLogger("mypub-kb")

EXCERPT_CHARS = 240
RRF_K = 60                     # reciprocal-rank-fusion smoothing constant
PER_MODALITY_LIMIT = 20        # candidates pulled from each modality before merge

# ---------------------------------------------------------------------------
# Connection + model lifecycle
# ---------------------------------------------------------------------------

_CONN = None
_RESOLVER: EntityResolver | None = None
_MODEL = None  # sentence-transformers model, shared by resolver and search


def _bootstrap() -> None:
    """Open the catalog, pre-warm the embedding model, build the resolver."""
    global _CONN, _RESOLVER, _MODEL
    if _CONN is not None:
        return

    catalog_env = os.environ.get("MYPUB_CATALOG")
    catalog_path = Path(catalog_env) if catalog_env else None
    LOG.info("opening catalog (%s)", catalog_path or "default")
    # Explicit read-only at the call site: server.py only issues SELECTs.
    # Holding an RW lock blocks every other process (other Claude Code
    # sessions, test suite, refresh scripts) — see db.py module docstring.
    _CONN = open_catalog(catalog_path, read_only=True)

    # pylint: disable=import-outside-toplevel
    from sentence_transformers import SentenceTransformer
    LOG.info("loading sentence-transformers model …")
    _MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    _RESOLVER = EntityResolver(_CONN, model=_MODEL)
    LOG.info("kb-mcp server ready")


def _embed(query: str) -> list[float]:
    """Embed a free-text query as a 384-dim float32 list."""
    assert _MODEL is not None
    vec = _MODEL.encode([query], convert_to_numpy=True)[0]
    return vec.astype("float32").tolist()


# ---------------------------------------------------------------------------
# Modality queries
# ---------------------------------------------------------------------------


def _fts_chapter_search(query: str, limit: int) -> list[dict[str, Any]]:
    """BM25 search over chapter.content via the fts_main_chapter schema."""
    rows = _CONN.execute(
        """
        SELECT c.chapter_id,
               fts_main_chapter.match_bm25(c.chapter_id, ?) AS score,
               b.title AS book_title,
               c.title AS chapter_title,
               substring(c.content, 1, ?) AS excerpt
          FROM chapter c
          JOIN book b ON c.book_id = b.book_id
         WHERE fts_main_chapter.match_bm25(c.chapter_id, ?) IS NOT NULL
         ORDER BY score DESC
         LIMIT ?
        """,
        [query, EXCERPT_CHARS, query, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            "score": float(r[1]),
            "book_title": r[2],
            "chapter_title": r[3],
            "excerpt": r[4],
        }
        for r in rows
    ]


def _vss_chapter_search(qvec: list[float], limit: int) -> list[dict[str, Any]]:
    """Cosine-distance search over chapter_embedding (HNSW-accelerated)."""
    rows = _CONN.execute(
        """
        SELECT c.chapter_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS distance,
               b.title AS book_title,
               c.title AS chapter_title,
               substring(c.content, 1, ?) AS excerpt
          FROM chapter_embedding e
          JOIN chapter c USING (chapter_id)
          JOIN book    b ON c.book_id = b.book_id
         ORDER BY distance ASC
         LIMIT ?
        """,
        [qvec, EXCERPT_CHARS, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            # Convert distance → similarity for a uniform "higher is better" view.
            "score": 1.0 - float(r[1]),
            "book_title": r[2],
            "chapter_title": r[3],
            "excerpt": r[4],
        }
        for r in rows
    ]


def _query_concept_ids(query: str) -> list[int]:
    """Map a free-text query to concept_ids via lookup-only resolution.

    Tries the whole query first, then each whitespace-delimited token. No
    embedding stage, no concept creation — read-only. Order-preserving
    deduplication so the most specific match (the full phrase) ranks first.
    """
    assert _RESOLVER is not None
    seen: set[int] = set()
    out: list[int] = []
    candidates = [query] + [t for t in query.split() if t.strip()]
    for cand in candidates:
        cid = _RESOLVER.resolve_lookup_only(cand)
        if cid is not None and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _graph_chapter_search(query: str, limit: int) -> list[dict[str, Any]]:
    """Graph signal: chapters that mention concepts named in the query.

    Uses concept_relation as the chapter↔concept link, filtered to
    source_type='chapter'. Ranking is concept-mention count: a chapter
    that touches multiple query concepts beats one that touches only
    one. Returns at most `limit` chapters.
    """
    cids = _query_concept_ids(query)
    if not cids:
        return []

    placeholders = ",".join(["?"] * len(cids))
    rows = _CONN.execute(
        f"""
        WITH chapter_hits AS (
          SELECT cr.source_id AS chapter_id,
                 COUNT(DISTINCT
                   CASE WHEN cr.from_concept_id IN ({placeholders})
                        THEN cr.from_concept_id
                        WHEN cr.to_concept_id   IN ({placeholders})
                        THEN cr.to_concept_id
                   END
                 ) AS concept_hits,
                 COUNT(*) AS mention_count
            FROM concept_relation cr
           WHERE cr.source_type = 'chapter'
             AND (cr.from_concept_id IN ({placeholders})
                  OR cr.to_concept_id IN ({placeholders}))
          GROUP BY cr.source_id
        )
        SELECT h.chapter_id,
               h.concept_hits,
               h.mention_count,
               b.title AS book_title,
               c.title AS chapter_title,
               substring(c.content, 1, ?) AS excerpt
          FROM chapter_hits h
          JOIN chapter c ON c.chapter_id = h.chapter_id
          JOIN book    b ON c.book_id    = b.book_id
         ORDER BY h.concept_hits DESC, h.mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, EXCERPT_CHARS, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            "concept_hits": int(r[1]),
            "mention_count": int(r[2]),
            "book_title": r[3],
            "chapter_title": r[4],
            "excerpt": r[5],
            # A normalized score for the merge step. Real scoring lands in 4.5.
            "score": float(r[1]) + 0.1 * float(r[2]),
        }
        for r in rows
    ]


def _fts_doc_section_search(query: str, limit: int) -> list[dict[str, Any]]:
    """BM25 search over doc_section.content via fts_main_doc_section.

    Schema mirrors the chapter version but result rows carry kind='doc_section'
    and a doc_section_id (chapter_id is None) so the unified merger can hold
    both kinds. ``fts_main_doc_section`` only exists once Phase 4.4 has
    refreshed at least one source; if it's absent (greenfield catalog), we
    return an empty list rather than erroring.
    """
    if not _doc_section_fts_index_exists():
        return []
    rows = _CONN.execute(
        """
        SELECT s.doc_section_id,
               fts_main_doc_section.match_bm25(s.doc_section_id, ?) AS score,
               src.name AS doc_source_name,
               s.heading_text,
               substring(s.content, 1, ?) AS excerpt
          FROM doc_section s
          JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src ON sn.doc_source_id = src.doc_source_id
         WHERE fts_main_doc_section.match_bm25(s.doc_section_id, ?) IS NOT NULL
         ORDER BY score DESC
         LIMIT ?
        """,
        [query, EXCERPT_CHARS, query, limit],
    ).fetchall()
    return [
        {
            "kind": "doc_section",
            "result_id": int(r[0]),
            "doc_section_id": int(r[0]),
            "chapter_id": None,
            "score": float(r[1]),
            "doc_source_name": r[2],
            "heading_text": r[3],
            "excerpt": r[4],
        }
        for r in rows
    ]


def _vss_doc_section_search(qvec: list[float], limit: int) -> list[dict[str, Any]]:
    """Cosine-distance search over doc_section_embedding."""
    rows = _CONN.execute(
        """
        SELECT s.doc_section_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS distance,
               src.name AS doc_source_name,
               s.heading_text,
               substring(s.content, 1, ?) AS excerpt
          FROM doc_section_embedding e
          JOIN doc_section s     USING (doc_section_id)
          JOIN doc_snapshot sn   ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src  ON sn.doc_source_id = src.doc_source_id
         ORDER BY distance ASC
         LIMIT ?
        """,
        [qvec, EXCERPT_CHARS, limit],
    ).fetchall()
    return [
        {
            "kind": "doc_section",
            "result_id": int(r[0]),
            "doc_section_id": int(r[0]),
            "chapter_id": None,
            "score": 1.0 - float(r[1]),  # convert distance → similarity
            "doc_source_name": r[2],
            "heading_text": r[3],
            "excerpt": r[4],
        }
        for r in rows
    ]


def _graph_doc_section_search(query: str, limit: int) -> list[dict[str, Any]]:
    """Concept-graph signal: doc_sections that mention concepts named in the query."""
    cids = _query_concept_ids(query)
    if not cids:
        return []

    placeholders = ",".join(["?"] * len(cids))
    rows = _CONN.execute(
        f"""
        WITH section_hits AS (
          SELECT cr.source_id AS doc_section_id,
                 COUNT(DISTINCT
                   CASE WHEN cr.from_concept_id IN ({placeholders})
                        THEN cr.from_concept_id
                        WHEN cr.to_concept_id   IN ({placeholders})
                        THEN cr.to_concept_id
                   END
                 ) AS concept_hits,
                 COUNT(*) AS mention_count
            FROM concept_relation cr
           WHERE cr.source_type = 'doc_section'
             AND (cr.from_concept_id IN ({placeholders})
                  OR cr.to_concept_id IN ({placeholders}))
          GROUP BY cr.source_id
        )
        SELECT h.doc_section_id, h.concept_hits, h.mention_count,
               src.name AS doc_source_name, s.heading_text,
               substring(s.content, 1, ?) AS excerpt
          FROM section_hits h
          JOIN doc_section  s    ON s.doc_section_id = h.doc_section_id
          JOIN doc_snapshot sn   ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src  ON sn.doc_source_id = src.doc_source_id
         ORDER BY h.concept_hits DESC, h.mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, EXCERPT_CHARS, limit],
    ).fetchall()
    return [
        {
            "kind": "doc_section",
            "result_id": int(r[0]),
            "doc_section_id": int(r[0]),
            "chapter_id": None,
            "concept_hits": int(r[1]),
            "mention_count": int(r[2]),
            "doc_source_name": r[3],
            "heading_text": r[4],
            "excerpt": r[5],
            "score": float(r[1]) + 0.1 * float(r[2]),
        }
        for r in rows
    ]


def _doc_section_fts_index_exists() -> bool:
    """True if the doc_section FTS index has been built (any refresh ran)."""
    row = _CONN.execute(
        "SELECT 1 FROM information_schema.schemata "
        " WHERE schema_name = 'fts_main_doc_section' LIMIT 1"
    ).fetchone()
    return row is not None


def _normalize_chapter_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add unified (kind, result_id) keys to a chapter modality result row."""
    out = dict(row)
    out.setdefault("kind", "chapter")
    out.setdefault("result_id", row["chapter_id"])
    out.setdefault("doc_section_id", None)
    return out


def _rrf_merge(
    buckets: dict[str, list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Merge per-modality result lists with reciprocal rank fusion.

    Keys on (kind, result_id) so chapter and doc_section results compete in
    the same ranked list. Modalities contribute 1/(k + rank) equally. The
    fusion formula is a placeholder until the real scoring engine lands in
    Prompt 4.5.
    """
    fused: dict[tuple[str, int], dict[str, Any]] = {}
    for modality, results in buckets.items():
        for rank, row in enumerate(results):
            kind = row.get("kind", "chapter")
            rid = row.get("result_id", row.get("chapter_id") or row.get("doc_section_id"))
            if rid is None:
                continue
            key = (kind, int(rid))
            contribution = 1.0 / (RRF_K + rank + 1)
            entry = fused.get(key)
            if entry is None:
                entry = {
                    "kind": kind,
                    "result_id": int(rid),
                    "chapter_id": row.get("chapter_id"),
                    "doc_section_id": row.get("doc_section_id"),
                    "book_title": row.get("book_title"),
                    "chapter_title": row.get("chapter_title"),
                    "doc_source_name": row.get("doc_source_name"),
                    "heading_text": row.get("heading_text"),
                    "excerpt": row.get("excerpt"),
                    "rrf_score": 0.0,
                    "modalities": [],
                    "modality_scores": {},
                }
                fused[key] = entry
            entry["rrf_score"] += contribution
            entry["modalities"].append(modality)
            entry["modality_scores"][modality] = row.get("score")

    merged = sorted(fused.values(), key=lambda e: e["rrf_score"], reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# FastMCP wiring
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "mypub-kb",
    instructions=(
        "myPub knowledge base: hybrid retrieval over ~345 technical eBooks. "
        "search_chapters fans out BM25, vector, and concept-graph queries; "
        "compare_concept_across_authors and find_prerequisites read the "
        "concept graph directly."
    ),
)


DEFAULT_INTERACTIVE_PROFILE = "currency_critical_interactive"


def _scored_to_dict(s: "ranking.ScoredResult") -> dict[str, Any]:
    """Flatten a ScoredResult into a JSON-friendly dict for the MCP envelope."""
    return {
        "kind": s.result.get("kind"),
        "result_id": s.result.get("result_id"),
        "chapter_id": s.result.get("chapter_id"),
        "doc_section_id": s.result.get("doc_section_id"),
        "book_title": s.result.get("book_title"),
        "chapter_title": s.result.get("chapter_title"),
        "doc_source_name": s.result.get("doc_source_name"),
        "heading_text": s.result.get("heading_text"),
        "excerpt": s.result.get("excerpt"),
        "rrf_score": s.result.get("rrf_score"),
        "modalities": s.result.get("modalities"),
        "modality_scores": s.result.get("modality_scores"),
        "combined_score": s.combined,
        "components": {
            "recency": s.components.recency,
            "doc_alignment": s.components.doc_alignment,
            "relevance": s.components.relevance,
            "corroboration": s.components.corroboration,
            "authority": s.components.authority,
        },
    }


@mcp.tool
def search_chapters(
    query: str, mode: str = "interactive", limit: int = 10,
    weight_profile: str = DEFAULT_INTERACTIVE_PROFILE,
) -> dict[str, Any]:
    """Hybrid search across book chapters and live doc sections.

    Args:
        query: Free-text search query.
        mode: 'interactive' returns the §8.1 {primary, corroborations,
            conflicts} shape with per-modality buckets and full component
            scores. 'generation' returns just the merged ranking, sorted by
            combined score.
        limit: Maximum number of merged results to consider for scoring;
            corroborations and conflicts each cap at 5 (in interactive mode).
        weight_profile: Name of a profile in ranking.WEIGHT_PROFILES.
            Defaults to ``currency_critical_interactive``. Use
            ``foundational_interactive`` for queries about timeless concepts
            where authority and corroboration matter more than recency.

    Returns:
        Dict per the §8.1 spec for interactive mode, or a flat
        ``{query, mode, results: [...]}`` for generation mode.
    """
    _bootstrap()
    if mode not in ("interactive", "generation"):
        raise ValueError(
            f"mode must be 'interactive' or 'generation', got {mode!r}"
        )
    if limit <= 0:
        raise ValueError("limit must be positive")
    if weight_profile not in ranking.WEIGHT_PROFILES:
        raise ValueError(
            f"weight_profile must be one of {sorted(ranking.WEIGHT_PROFILES)}; "
            f"got {weight_profile!r}"
        )

    qvec = _embed(query)
    # Chapter modalities (book corpus)
    fts_chapter = [_normalize_chapter_row(r)
                   for r in _fts_chapter_search(query, PER_MODALITY_LIMIT)]
    vss_chapter = [_normalize_chapter_row(r)
                   for r in _vss_chapter_search(qvec, PER_MODALITY_LIMIT)]
    graph_chapter = [_normalize_chapter_row(r)
                     for r in _graph_chapter_search(query, PER_MODALITY_LIMIT)]
    # Doc-section modalities (live-doc corpus). Phase 4.4b adds these so
    # Context7 / DeepWiki / GitHub content rides in the same ranked list.
    fts_section = _fts_doc_section_search(query, PER_MODALITY_LIMIT)
    vss_section = _vss_doc_section_search(qvec, PER_MODALITY_LIMIT)
    graph_section = _graph_doc_section_search(query, PER_MODALITY_LIMIT)

    # Merge across all six buckets — RRF keys on (kind, result_id) so
    # chapter and doc_section rows can co-occur in the merged ranking.
    merged = _rrf_merge(
        {
            "fts_chapter": fts_chapter, "vss_chapter": vss_chapter,
            "graph_chapter": graph_chapter,
            "fts_doc_section": fts_section, "vss_doc_section": vss_section,
            "graph_doc_section": graph_section,
        },
        limit=limit,
    )

    # Phase 4.5: feed the merged set through the ranking engine. RRF score
    # becomes the relevance component; recency / doc_alignment / corroboration
    # / authority are looked up per-result and combined under the chosen
    # weight profile.
    weights = ranking.WEIGHT_PROFILES[weight_profile]

    if mode == "generation":
        gen_ranker = ranking.GenerationRanker(_CONN, weights, top_k=limit)
        # Generation mode here exposes the ranked-by-combined-score view;
        # selection-strategy gating lives in the Skills Factory (Phase 5).
        max_rrf = max((float(r.get("rrf_score") or 0.0) for r in merged), default=1.0) or 1.0
        scored = []
        for r in merged:
            comps = ranking.compute_components_for_result(_CONN, r, max_rrf_score=max_rrf)
            scored.append(ranking.ScoredResult(
                result=r, components=comps, combined=comps.combine(weights),
            ))
        scored.sort(key=lambda s: s.combined, reverse=True)
        return {
            "query": query,
            "mode": mode,
            "weight_profile": weight_profile,
            "results": [_scored_to_dict(s) for s in scored[:limit]],
        }

    # Interactive mode (§8.1): primary + corroborations + conflicts.
    interactive = ranking.InteractiveRanker(_CONN, weights).rank(merged)
    return {
        "query": query,
        "mode": mode,
        "weight_profile": weight_profile,
        "primary": _scored_to_dict(interactive.primary) if interactive.primary else None,
        "corroborations": [_scored_to_dict(s) for s in interactive.corroborations],
        "conflicts": [_scored_to_dict(s) for s in interactive.conflicts],
        "all_scored": [_scored_to_dict(s) for s in interactive.all_scored[:limit]],
        "by_modality": {
            "fts_chapter": fts_chapter[:limit],
            "vss_chapter": vss_chapter[:limit],
            "graph_chapter": graph_chapter[:limit],
            "fts_doc_section": fts_section[:limit],
            "vss_doc_section": vss_section[:limit],
            "graph_doc_section": graph_section[:limit],
        },
    }


@mcp.tool
def compare_concept_across_authors(
    concept_name: str, limit_per_author: int = 2
) -> dict[str, Any]:
    """Show how different authors discuss a concept.

    Resolves `concept_name` to an existing concept (lookup-only — no new
    concepts are created). Then walks concept_relation (chapter sources
    only) → chapter → book → book_author → author and returns a
    per-author roll-up of chapters that discuss the concept.
    """
    _bootstrap()
    if not concept_name or not concept_name.strip():
        raise ValueError("concept_name must be non-empty")
    if limit_per_author <= 0:
        raise ValueError("limit_per_author must be positive")

    cid = _RESOLVER.resolve_lookup_only(concept_name)
    if cid is None:
        return {
            "concept": None,
            "found": False,
            "message": f"No concept matched {concept_name!r} via name or alias.",
            "by_author": [],
        }

    name = _CONN.execute(
        "SELECT name FROM concept WHERE concept_id = ?", [cid]
    ).fetchone()[0]

    rows = _CONN.execute(
        """
        WITH chapter_mentions AS (
          SELECT DISTINCT cr.source_id AS chapter_id
            FROM concept_relation cr
           WHERE cr.source_type = 'chapter'
             AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
        ),
        ranked AS (
          SELECT a.author_id,
                 a.name AS author_name,
                 b.book_id,
                 b.title AS book_title,
                 ch.chapter_id,
                 ch.title AS chapter_title,
                 substring(ch.content, 1, ?) AS excerpt,
                 ROW_NUMBER() OVER (
                     PARTITION BY a.author_id ORDER BY b.book_id, ch.chapter_id
                 ) AS rn
            FROM chapter_mentions cm
            JOIN chapter   ch ON ch.chapter_id = cm.chapter_id
            JOIN book      b  ON b.book_id    = ch.book_id
            JOIN book_author ba ON ba.book_id = b.book_id
            JOIN author    a  ON a.author_id  = ba.author_id
        )
        SELECT author_id, author_name, book_id, book_title,
               chapter_id, chapter_title, excerpt
          FROM ranked
         WHERE rn <= ?
         ORDER BY author_name, book_title, chapter_id
        """,
        [cid, cid, EXCERPT_CHARS, limit_per_author],
    ).fetchall()

    by_author: dict[int, dict[str, Any]] = {}
    for author_id, author_name, book_id, book_title, ch_id, ch_title, excerpt in rows:
        author_entry = by_author.setdefault(
            author_id,
            {"author_id": author_id, "author": author_name, "books": {}},
        )
        book_entry = author_entry["books"].setdefault(
            book_id,
            {"book_id": book_id, "title": book_title, "chapters": []},
        )
        book_entry["chapters"].append(
            {
                "chapter_id": ch_id,
                "title": ch_title,
                "excerpt": excerpt,
            }
        )

    # Flatten the inner book dicts into ordered lists.
    authors_out = []
    for entry in by_author.values():
        entry["books"] = list(entry["books"].values())
        authors_out.append(entry)
    authors_out.sort(key=lambda e: e["author"] or "")

    return {
        "concept": {"id": cid, "name": name},
        "found": True,
        "by_author": authors_out,
    }


@mcp.tool
def find_prerequisites(
    concept_name: str, max_depth: int = 5
) -> dict[str, Any]:
    """Walk REQUIRES edges from a concept to surface its prerequisites.

    Resolves `concept_name` to an existing concept (lookup-only). Then
    runs a recursive traversal over concept_relation filtered to
    relation_type='REQUIRES', up to `max_depth` hops. Returns
    prerequisite concepts with the shortest depth at which each was
    reached.
    """
    _bootstrap()
    if not concept_name or not concept_name.strip():
        raise ValueError("concept_name must be non-empty")
    if max_depth <= 0:
        raise ValueError("max_depth must be positive")

    cid = _RESOLVER.resolve_lookup_only(concept_name)
    if cid is None:
        return {
            "concept": None,
            "found": False,
            "message": f"No concept matched {concept_name!r} via name or alias.",
            "prerequisites": [],
        }

    name = _CONN.execute(
        "SELECT name FROM concept WHERE concept_id = ?", [cid]
    ).fetchone()[0]

    # Recursive CTE: shortest depth wins per concept_id. UNION (set, not all)
    # collapses duplicate (concept_id, depth) pairs; the GROUP BY at the end
    # keeps the smallest depth for each prerequisite concept.
    rows = _CONN.execute(
        """
        WITH RECURSIVE prereq(concept_id, depth) AS (
          SELECT to_concept_id, 1
            FROM concept_relation
           WHERE from_concept_id = ?
             AND relation_type   = 'REQUIRES'
          UNION
          SELECT cr.to_concept_id, p.depth + 1
            FROM concept_relation cr
            JOIN prereq p ON p.concept_id = cr.from_concept_id
           WHERE cr.relation_type = 'REQUIRES'
             AND p.depth < ?
        )
        SELECT MIN(p.depth) AS depth, c.concept_id, c.name, c.concept_type
          FROM prereq p
          JOIN concept c USING (concept_id)
         WHERE c.concept_id != ?
         GROUP BY c.concept_id, c.name, c.concept_type
         ORDER BY depth, c.name
        """,
        [cid, max_depth, cid],
    ).fetchall()

    return {
        "concept": {"id": cid, "name": name},
        "found": True,
        "max_depth": max_depth,
        "prerequisites": [
            {
                "depth": int(r[0]),
                "concept_id": r[1],
                "name": r[2],
                "concept_type": r[3],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        # Stdio MCP uses stdout for the protocol; logs must go to stderr.
        stream=sys.stderr,
    )
    _bootstrap()
    mcp.run()  # default transport is stdio


if __name__ == "__main__":
    main()
