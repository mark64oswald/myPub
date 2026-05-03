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
    _CONN = open_catalog(catalog_path)

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


def _rrf_merge(
    buckets: dict[str, list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Merge per-modality result lists with reciprocal rank fusion.

    Each modality contributes 1/(k + rank) per chapter; modalities are
    weighted equally. The fusion formula is a placeholder until the
    proper scoring formula from architecture §8.5 lands in Prompt 4.5.
    """
    fused: dict[int, dict[str, Any]] = {}
    for modality, results in buckets.items():
        for rank, row in enumerate(results):
            cid = row["chapter_id"]
            contribution = 1.0 / (RRF_K + rank + 1)
            entry = fused.get(cid)
            if entry is None:
                entry = {
                    "chapter_id": cid,
                    "book_title": row.get("book_title"),
                    "chapter_title": row.get("chapter_title"),
                    "excerpt": row.get("excerpt"),
                    "rrf_score": 0.0,
                    "modalities": [],
                    "modality_scores": {},
                }
                fused[cid] = entry
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


@mcp.tool
def search_chapters(
    query: str, mode: str = "interactive", limit: int = 10
) -> dict[str, Any]:
    """Hybrid chapter search across FTS, VSS, and the concept graph.

    Args:
        query: Free-text search query.
        mode: 'interactive' returns per-modality buckets alongside the
            merged ranking so callers can see provenance. 'generation'
            returns only the merged ranking.
        limit: Maximum number of merged results to return.

    Returns:
        Dict with the merged ranked results and (in interactive mode) the
        raw per-modality top hits. The merged 'rrf_score' is a Phase 4.1
        placeholder; the full scoring formula lands in Prompt 4.5.
    """
    _bootstrap()
    if mode not in ("interactive", "generation"):
        raise ValueError(
            f"mode must be 'interactive' or 'generation', got {mode!r}"
        )
    if limit <= 0:
        raise ValueError("limit must be positive")

    qvec = _embed(query)
    fts_hits = _fts_chapter_search(query, PER_MODALITY_LIMIT)
    vss_hits = _vss_chapter_search(qvec, PER_MODALITY_LIMIT)
    graph_hits = _graph_chapter_search(query, PER_MODALITY_LIMIT)

    merged = _rrf_merge(
        {"fts": fts_hits, "vss": vss_hits, "graph": graph_hits},
        limit=limit,
    )

    payload: dict[str, Any] = {
        "query": query,
        "mode": mode,
        "results": merged,
    }
    if mode == "interactive":
        payload["by_modality"] = {
            "fts": fts_hits[:limit],
            "vss": vss_hits[:limit],
            "graph": graph_hits[:limit],
        }
    return payload


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
