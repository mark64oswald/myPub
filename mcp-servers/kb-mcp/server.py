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
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from fastmcp import FastMCP

# Make sibling modules importable when this file is run as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from db import open_catalog  # noqa: E402
from resolution import EntityResolver  # noqa: E402
import ranking  # noqa: E402
import discovery  # noqa: E402

LOG = logging.getLogger("mypub-kb")

EXCERPT_CHARS = 240
EXCERPT_FETCH_CHARS = 1200     # pulled from DB; cleaned + truncated to EXCERPT_CHARS
RRF_K = 60                     # reciprocal-rank-fusion smoothing constant
PER_MODALITY_LIMIT = 20        # candidates pulled from each modality before merge
SCORING_POOL_MULTIPLIER = 5    # ranker sees limit * this many candidates before truncation
WRITER_GRACE_SECONDS = 0.05    # OS lock-release breathing room (Topology A)


# Patterns that mark a leading boilerplate line in book chapter content.
# ePub-to-text extraction typically dumps the chapter heading at the top of
# c.content as one or two short lines, then the actual body. The chapter
# title is already in chapter.title, so showing it again in the excerpt is
# noise that pushes real content out of the 240-char window.
_HEADING_LINE_RE = re.compile(
    r"^(?:Chapter|Section|Part|Appendix|Preface|Foreword|Introduction|Epilogue)"
    r"\s*\d*(?:[.:]\d+)*[.:]?\s*$",
    re.IGNORECASE,
)
_DIGIT_OR_ROMAN_LINE_RE = re.compile(r"^\s*(?:\d+|[IVXLCDM]+)[.:]?\s*$")
_EARLY_RELEASE_PREAMBLE_RE = re.compile(
    r"^a\s+note\s+for\s+early\s+release\s+readers", re.IGNORECASE,
)
# Markdown horizontal-rule / leading section dashes that the doc_section
# sectionizer leaves at the start of section content, e.g. "--\n\n### Heading".
_LEADING_MD_HR_RE = re.compile(r"^-{2,}\s*$")


def _clean_excerpt(content: Optional[str], *, max_chars: int = EXCERPT_CHARS) -> str:
    """Strip leading chapter-heading boilerplate and return a readable excerpt.

    Common patterns we trim:
      "Chapter 2.\\nFundamentals of Events…"   → drop the first two lines
      "Preface\\nDomain-Driven Design in PHP…" → drop the first two lines
      "II\\nFrom Circuits to Networks…"        → drop digit/roman + title
      "Chapter 1.\\nTitle\\nA Note for Early Release Readers…"
                                              → also strip the early-release blurb

    If the cleaning would leave less than half the target excerpt length,
    fall back to the raw prefix so we never return a useless sliver.
    """
    if not content:
        return ""
    lines = content.splitlines()
    # Drop leading blank lines + markdown horizontal-rule markers ("--", "---").
    while lines and (not lines[0].strip() or _LEADING_MD_HR_RE.match(lines[0].strip())):
        lines.pop(0)
    if not lines:
        return ""

    # Strip "Chapter N." / "Preface" / digit / roman heading line.
    if (_HEADING_LINE_RE.match(lines[0].strip())
            or _DIGIT_OR_ROMAN_LINE_RE.match(lines[0])):
        lines.pop(0)
        # The heading is usually followed by the chapter title on its own
        # line — short, no terminal period. Drop one such line.
        if lines and lines[0].strip() and len(lines[0]) < 100 \
                and not lines[0].rstrip().endswith("."):
            lines.pop(0)

    # Strip the "A Note for Early Release Readers" preamble — it's always 3-6
    # lines of disclaimer before the real content starts.
    if lines and _EARLY_RELEASE_PREAMBLE_RE.match(lines[0].strip()):
        lines.pop(0)
        # Drop until we find a blank line or a sentence-ending line, capping at
        # 8 dropped lines so we don't accidentally consume an entire chapter.
        dropped = 0
        while lines and dropped < 8 and lines[0].strip() \
                and not lines[0].rstrip().endswith("."):
            lines.pop(0)
            dropped += 1
        # Drop one more if the preamble closes with a period.
        if lines and lines[0].rstrip().endswith(".") and dropped < 8:
            lines.pop(0)

    text = "\n".join(lines).lstrip()
    # Fallback only if cleaning ate everything substantive — a short but
    # non-trivial cleaned excerpt is still better than the raw prefix with
    # heading boilerplate.
    if len(text) < 40:
        return content.lstrip()[:max_chars]
    return text[:max_chars]


# "(for <name>)" personalization suffix added to book titles by the ePub
# vendor (e.g., "Building Event-Driven Microservices (for Mark Oswald)").
# Strip from titles at query time so responses don't carry the watermark.
_PERSONALIZATION_RE = re.compile(r"\s*\(for [^)]+\)\s*$")


def _clean_book_title(title: Optional[str]) -> Optional[str]:
    """Strip the trailing '(for <name>)' personalization watermark."""
    if title is None:
        return None
    return _PERSONALIZATION_RE.sub("", title).strip() or title


# Phase 1 splitter bug: ePub TOC entries that point at the same xhtml file
# with different anchor fragments all get the FULL file's content, so 94.8%
# of chapters share their content with at least one sibling. Until the
# splitter is reworked to honor fragments (tracked as Phase 1 follow-up),
# we collapse duplicate-content chapters at retrieval time. Keys on
# (book_title, excerpt[:160]) — the cleaned excerpts of duplicate chapters
# match exactly because the underlying content is byte-identical.
def _dedupe_by_content(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse chapter results that share book + content prefix.

    Doc_section rows are passed through unchanged (the sectionizer doesn't
    have this bug). For chapters, the highest-scored representative per
    (book_title, excerpt[:160]) cluster is kept; other rows are dropped.
    """
    seen_chapter_keys: dict[tuple[Any, str], int] = {}
    out: list[dict[str, Any]] = []
    for r in results:
        if r.get("kind") != "chapter":
            out.append(r)
            continue
        excerpt = (r.get("excerpt") or "")[:160]
        key = (r.get("book_title"), excerpt)
        existing_idx = seen_chapter_keys.get(key)
        if existing_idx is None:
            seen_chapter_keys[key] = len(out)
            out.append(r)
            continue
        # Duplicate cluster — keep whichever scored higher.
        existing = out[existing_idx]
        if (r.get("rrf_score") or 0.0) > (existing.get("rrf_score") or 0.0):
            out[existing_idx] = r
    return out

# ---------------------------------------------------------------------------
# Connection + model lifecycle
# ---------------------------------------------------------------------------

_CONN = None
_RESOLVER: EntityResolver | None = None
_MODEL = None  # sentence-transformers model, shared by resolver and search
_CATALOG_PATH: Path | None = None  # cached so writer-context can reopen


def _bootstrap() -> None:
    """Open the catalog, pre-warm the embedding model, build the resolver.

    Idempotent across partial state: if the model is already loaded but the
    connection was torn down (e.g., a writer-context exception path left
    ``_CONN=None``), reopen the connection without paying the cold-start
    cost on the embedding model.
    """
    global _CONN, _RESOLVER, _MODEL, _CATALOG_PATH
    if _CONN is not None:
        return

    catalog_env = os.environ.get("MYPUB_CATALOG")
    _CATALOG_PATH = Path(catalog_env) if catalog_env else None
    LOG.info("opening catalog (%s)", _CATALOG_PATH or "default")
    # Explicit read-only at the call site: server.py only issues SELECTs.
    # Holding an RW lock blocks every other process (other Claude Code
    # sessions, test suite, refresh scripts) — see db.py module docstring.
    _CONN = open_catalog(_CATALOG_PATH, read_only=True)

    if _MODEL is None:
        # pylint: disable=import-outside-toplevel
        from sentence_transformers import SentenceTransformer
        LOG.info("loading sentence-transformers model …")
        _MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    _RESOLVER = EntityResolver(_CONN, model=_MODEL)
    LOG.info("kb-mcp server ready")


@contextmanager
def _temporarily_open_writer() -> "Iterator[Any]":
    """Close the long-lived RO connection, yield a transient RW one, reopen RO.

    Topology A from ~/Developer/notes/duckdb-concurrent-access.md adapted to
    kb-mcp's single-process lifecycle. DuckDB rejects opening a second
    connection with a different ``read_only`` setting in the same process
    while the first is still open, so we must close-before-write.

    Used exclusively by the auto-discovery integration (and any future
    in-process writers). Multiple-Claude-Code-session caveat: while the RW
    connection is held, any *other* kb-mcp process holding RO on the same
    catalog will be locked out for the duration. In single-session use this
    is invisible; for multi-session setups, prefer routing inline ingestion
    through scripts/refresh_docs.py (Topology B + auto-stop) instead.

    Exception safety: the RO connection is GUARANTEED to be restored on exit
    regardless of where a failure occurs (RW open, body, CHECKPOINT, RW
    close). If the RW open itself fails, ``RuntimeError`` propagates after
    the RO connection is reopened, so callers see a real error instead of a
    silently-broken server. If the RO reopen fails, ``_CONN`` stays None and
    the next ``_bootstrap()`` call will rebuild it.
    """
    global _CONN, _RESOLVER
    assert _CONN is not None and _MODEL is not None, "_bootstrap() must run first"

    _CONN.close()
    _CONN = None
    _RESOLVER = None
    time.sleep(WRITER_GRACE_SECONDS)

    rw_conn = None
    try:
        try:
            rw_conn = open_catalog(_CATALOG_PATH, read_only=False)
        except Exception as e:
            raise RuntimeError(
                f"failed to open RW connection for inline writer: {e}"
            ) from e
        yield rw_conn
    finally:
        if rw_conn is not None:
            try:
                rw_conn.execute("CHECKPOINT")
            except Exception as e:  # pragma: no cover - best-effort cleanup
                LOG.warning("CHECKPOINT failed during writer close: %s", e)
            try:
                rw_conn.close()
            except Exception as e:  # pragma: no cover - best-effort cleanup
                LOG.warning("RW close failed: %s", e)
        time.sleep(WRITER_GRACE_SECONDS)
        try:
            _CONN = open_catalog(_CATALOG_PATH, read_only=True)
            _RESOLVER = EntityResolver(_CONN, model=_MODEL)
        except Exception as e:
            # Leave _CONN/_RESOLVER as None; next _bootstrap() will rebuild.
            LOG.error("failed to reopen RO connection after writer: %s", e)
            _CONN = None
            _RESOLVER = None


def _embed(query: str) -> list[float]:
    """Embed a free-text query as a 384-dim float32 list."""
    assert _MODEL is not None
    vec = _MODEL.encode([query], convert_to_numpy=True)[0]
    return vec.astype("float32").tolist()


# Tokens preserved as ALL-CAPS in the original query that are likely
# technical acronyms (e.g., CQRS, FHIR, HL7, EHR, REST, JSON, SQL). When
# present, candidates that do NOT contain the acronym verbatim are highly
# unlikely to be on-topic — even if they BM25-match other tokens. This
# strips the long tail of "MLflow page mentions 'model' + 'projection'
# but has nothing to do with CQRS" results.
# Starts with an uppercase letter, followed by 1-5 uppercase letters or
# digits — catches CQRS, FHIR, REST, JSON, but also HL7, OAuth2, K8s.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,5}\b")
# Common short uppercase tokens that aren't technical acronyms.
_ACRONYM_BLOCKLIST = {"AND", "OR", "OF", "TO", "IN", "AT", "BY", "IS",
                      "IT", "AS", "ON", "AN", "DO", "BE", "GO", "I", "A",
                      "THE", "FOR", "NEW", "THIS", "WITH"}


def _required_acronyms(query: str) -> list[str]:
    """Return uppercase technical-acronym tokens from the query.

    Empty list when the query has no all-caps technical tokens — most
    natural-language queries fall here, so the post-filter is a no-op.
    """
    out = []
    for tok in _ACRONYM_RE.findall(query):
        if tok in _ACRONYM_BLOCKLIST:
            continue
        out.append(tok)
    # Dedup while preserving order.
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _acronym_filter_clauses(
    acronyms: list[str], col: str,
    *, fts_schema: Optional[str] = None, fts_id_col: Optional[str] = None,
) -> tuple[str, list[str]]:
    """Build the SQL fragment that requires each acronym to be present.

    Two modes:
      * ``fts_schema``+``fts_id_col`` set → use the FTS index for the check
        (``AND fts_main_X.match_bm25(id, ?) IS NOT NULL``). 20x faster than
        ILIKE on a large content column because the index already tokenizes.
      * Otherwise → fall back to ``AND col ILIKE '%X%'``. Used when no FTS
        index is available for the table being filtered.

    Returns (sql_fragment, parameters) — empty when ``acronyms`` is empty.
    """
    if not acronyms:
        return "", []
    if fts_schema and fts_id_col:
        fragment = " ".join(
            f" AND {fts_schema}.match_bm25({fts_id_col}, ?) IS NOT NULL"
            for _ in acronyms
        )
        return fragment, list(acronyms)
    fragment = " ".join(f" AND {col} ILIKE ?" for _ in acronyms)
    params = [f"%{a}%" for a in acronyms]
    return fragment, params


# ---------------------------------------------------------------------------
# Modality queries
# ---------------------------------------------------------------------------


def _fts_chapter_search(query: str, limit: int) -> list[dict[str, Any]]:
    """BM25 search over chapter.content via the fts_main_chapter schema."""
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "c.content",
        fts_schema="fts_main_chapter", fts_id_col="c.chapter_id",
    )
    rows = _CONN.execute(
        f"""
        SELECT c.chapter_id,
               fts_main_chapter.match_bm25(c.chapter_id, ?) AS score,
               b.title AS book_title,
               c.title AS chapter_title,
               substring(c.content, 1, ?) AS raw_prefix
          FROM chapter c
          JOIN book b ON c.book_id = b.book_id
         WHERE fts_main_chapter.match_bm25(c.chapter_id, ?) IS NOT NULL
           {acro_clause}
         ORDER BY score DESC
         LIMIT ?
        """,
        [query, EXCERPT_FETCH_CHARS, query, *acro_params, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            "score": float(r[1]),
            "book_title": _clean_book_title(r[2]),
            "chapter_title": r[3],
            "excerpt": _clean_excerpt(r[4]),
        }
        for r in rows
    ]


def _vss_chapter_search(
    qvec: list[float], limit: int, *, query: str = "",
) -> list[dict[str, Any]]:
    """Cosine-distance search over chapter_embedding (HNSW-accelerated).

    ``query`` is the original free-text — used to extract acronym tokens
    that we then require in the candidate's content (post-filter, since
    embedding-only similarity will surface MLflow for 'CQRS' even though
    MLflow has no 'CQRS' string).
    """
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "c.content",
        fts_schema="fts_main_chapter", fts_id_col="c.chapter_id",
    )
    rows = _CONN.execute(
        f"""
        SELECT c.chapter_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS distance,
               b.title AS book_title,
               c.title AS chapter_title,
               substring(c.content, 1, ?) AS raw_prefix
          FROM chapter_embedding e
          JOIN chapter c USING (chapter_id)
          JOIN book    b ON c.book_id = b.book_id
         WHERE 1=1
           {acro_clause}
         ORDER BY distance ASC
         LIMIT ?
        """,
        [qvec, EXCERPT_FETCH_CHARS, *acro_params, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            # Convert distance → similarity for a uniform "higher is better" view.
            "score": 1.0 - float(r[1]),
            "book_title": _clean_book_title(r[2]),
            "chapter_title": r[3],
            "excerpt": _clean_excerpt(r[4]),
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
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "c.content",
        fts_schema="fts_main_chapter", fts_id_col="c.chapter_id",
    )

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
               substring(c.content, 1, ?) AS raw_prefix
          FROM chapter_hits h
          JOIN chapter c ON c.chapter_id = h.chapter_id
          JOIN book    b ON c.book_id    = b.book_id
         WHERE 1=1
           {acro_clause}
         ORDER BY h.concept_hits DESC, h.mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, EXCERPT_FETCH_CHARS, *acro_params, limit],
    ).fetchall()
    return [
        {
            "chapter_id": r[0],
            "concept_hits": int(r[1]),
            "mention_count": int(r[2]),
            "book_title": _clean_book_title(r[3]),
            "chapter_title": r[4],
            "excerpt": _clean_excerpt(r[5]),
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
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "s.content",
        fts_schema="fts_main_doc_section", fts_id_col="s.doc_section_id",
    )
    rows = _CONN.execute(
        f"""
        SELECT s.doc_section_id,
               fts_main_doc_section.match_bm25(s.doc_section_id, ?) AS score,
               src.name AS doc_source_name,
               s.heading_text,
               substring(s.content, 1, ?) AS raw_prefix
          FROM doc_section s
          JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src ON sn.doc_source_id = src.doc_source_id
         WHERE fts_main_doc_section.match_bm25(s.doc_section_id, ?) IS NOT NULL
           {acro_clause}
         ORDER BY score DESC
         LIMIT ?
        """,
        [query, EXCERPT_FETCH_CHARS, query, *acro_params, limit],
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
            "excerpt": _clean_excerpt(r[4]),
        }
        for r in rows
    ]


def _vss_doc_section_search(
    qvec: list[float], limit: int, *, query: str = "",
) -> list[dict[str, Any]]:
    """Cosine-distance search over doc_section_embedding.

    ``query`` is the original free-text — used for acronym post-filter so
    semantic matches that don't contain required tokens (e.g., 'CQRS')
    don't surface."""
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "s.content",
        fts_schema="fts_main_doc_section", fts_id_col="s.doc_section_id",
    )
    rows = _CONN.execute(
        f"""
        SELECT s.doc_section_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS distance,
               src.name AS doc_source_name,
               s.heading_text,
               substring(s.content, 1, ?) AS raw_prefix
          FROM doc_section_embedding e
          JOIN doc_section s     USING (doc_section_id)
          JOIN doc_snapshot sn   ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src  ON sn.doc_source_id = src.doc_source_id
         WHERE 1=1
           {acro_clause}
         ORDER BY distance ASC
         LIMIT ?
        """,
        [qvec, EXCERPT_FETCH_CHARS, *acro_params, limit],
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
            "excerpt": _clean_excerpt(r[4]),
        }
        for r in rows
    ]


def _graph_doc_section_search(query: str, limit: int) -> list[dict[str, Any]]:
    """Concept-graph signal: doc_sections that mention concepts named in the query."""
    cids = _query_concept_ids(query)
    if not cids:
        return []
    acro_clause, acro_params = _acronym_filter_clauses(
        _required_acronyms(query), "s.content",
        fts_schema="fts_main_doc_section", fts_id_col="s.doc_section_id",
    )

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
               substring(s.content, 1, ?) AS raw_prefix
          FROM section_hits h
          JOIN doc_section  s    ON s.doc_section_id = h.doc_section_id
          JOIN doc_snapshot sn   ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src  ON sn.doc_source_id = src.doc_source_id
         WHERE 1=1
           {acro_clause}
         ORDER BY h.concept_hits DESC, h.mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, EXCERPT_FETCH_CHARS, *acro_params, limit],
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
            "excerpt": _clean_excerpt(r[5]),
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


DEFAULT_INTERACTIVE_PROFILE = "balanced_interactive"


def _is_thin_retrieval(
    fts_chapter: list[dict[str, Any]], fts_section: list[dict[str, Any]],
    graph_chapter: list[dict[str, Any]], graph_section: list[dict[str, Any]],
) -> bool:
    """True when keyword + concept-graph signals are all empty.

    VSS deliberately excluded — it always returns *some* result via the HNSW
    nearest-neighbor scan, even for queries with no real corpus match
    (semantic similarity to vaguely related chapters). Letting VSS suppress
    auto-discovery would mean the gap detector never fires on a fresh KB.
    Concept-graph and FTS, by contrast, return [] when there's no real hit.
    """
    return (
        len(fts_chapter) == 0
        and len(fts_section) == 0
        and len(graph_chapter) == 0
        and len(graph_section) == 0
    )


def _run_modality_fanout(query: str, qvec: list[float]):
    """Helper: fan out to all six modality functions and normalize chapter rows.

    Extracted from search_chapters so the auto-discovery re-retrieval path
    can call it again without copy-pasting the per-modality call list.
    """
    fts_chapter = [_normalize_chapter_row(r)
                   for r in _fts_chapter_search(query, PER_MODALITY_LIMIT)]
    vss_chapter = [_normalize_chapter_row(r)
                   for r in _vss_chapter_search(qvec, PER_MODALITY_LIMIT, query=query)]
    graph_chapter = [_normalize_chapter_row(r)
                     for r in _graph_chapter_search(query, PER_MODALITY_LIMIT)]
    fts_section = _fts_doc_section_search(query, PER_MODALITY_LIMIT)
    vss_section = _vss_doc_section_search(qvec, PER_MODALITY_LIMIT, query=query)
    graph_section = _graph_doc_section_search(query, PER_MODALITY_LIMIT)
    return (fts_chapter, vss_chapter, graph_chapter,
            fts_section, vss_section, graph_section)


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
    auto_discover: bool = True,
    selection_strategy: Optional[str] = None,
) -> dict[str, Any]:
    """Hybrid search across book chapters and live doc sections.

    Args:
        query: Free-text search query.
        mode: 'interactive' returns the §8.1 {primary, corroborations,
            conflicts} shape with per-modality buckets and full component
            scores. 'generation' returns the §8.2 selection-strategy shape:
            a curated ``selected`` list plus ``dropped`` provenance for §8.6.
        limit: Maximum number of results to return after scoring.
            Corroborations and conflicts each cap at 5 in interactive mode.
        weight_profile: Name of a profile in ranking.WEIGHT_PROFILES.
            Defaults to ``currency_critical_interactive``. Use
            ``foundational_interactive`` for queries about timeless concepts
            where authority and corroboration matter more than recency.
        selection_strategy: Generation-mode only. One of
            ``recent_doc_anchored`` (drops chapters contradicted by current
            docs), ``consensus_synthesis`` (keeps corroborated material),
            ``authority_pick`` (top-1 by authority component). When omitted
            in generation mode, returns the unfiltered combined-score-sorted
            ranking — useful for downstream pipelines that pick their own
            strategy. Ignored in interactive mode.

    Returns:
        Dict per the §8.1 spec for interactive mode, or a
        ``{query, mode, weight_profile, selection_strategy, results, dropped}``
        shape for generation mode.
    """
    _bootstrap()
    if mode not in ("interactive", "generation"):
        raise ValueError(
            f"mode must be 'interactive' or 'generation', got {mode!r}"
        )
    if selection_strategy is not None:
        if mode != "generation":
            raise ValueError(
                "selection_strategy is only valid when mode='generation'"
            )
        if selection_strategy not in ranking.SELECTION_STRATEGIES:
            raise ValueError(
                f"selection_strategy must be one of "
                f"{list(ranking.SELECTION_STRATEGIES)}; got {selection_strategy!r}"
            )
    if limit <= 0:
        raise ValueError("limit must be positive")
    if weight_profile not in ranking.WEIGHT_PROFILES:
        raise ValueError(
            f"weight_profile must be one of {sorted(ranking.WEIGHT_PROFILES)}; "
            f"got {weight_profile!r}"
        )

    qvec = _embed(query)
    (fts_chapter, vss_chapter, graph_chapter,
     fts_section, vss_section, graph_section) = _run_modality_fanout(query, qvec)

    # Phase 4.5b: if FTS + graph all came back empty, the corpus probably
    # doesn't know the topic — try auto-discovery before giving up.
    discovery_outcomes: list[dict[str, Any]] = []
    if auto_discover and _is_thin_retrieval(
        fts_chapter, fts_section, graph_chapter, graph_section,
    ):
        # Build a preliminary search_response shape for the gap detector to
        # inspect (it cross-references hits to skip terms already surfaced).
        prelim = {"results": fts_chapter + fts_section + vss_chapter + vss_section}
        discovery_outcomes = _run_auto_discovery(query, prelim)
        if any(o.get("decision") == "ingested" for o in discovery_outcomes):
            # Refresh fan-out against the freshly-ingested data. _CONN was
            # already swapped back to RO inside the writer context, so the
            # new doc_section rows are visible.
            (fts_chapter, vss_chapter, graph_chapter,
             fts_section, vss_section, graph_section) = _run_modality_fanout(query, qvec)

    # Merge across all six buckets — RRF keys on (kind, result_id) so
    # chapter and doc_section rows can co-occur in the merged ranking.
    # We collect a wider pool than ``limit`` so the five-factor ranker can
    # promote a candidate that ranks lower on RRF but higher on combined
    # score (e.g., a recent, well-corroborated, high-authority result that
    # didn't quite win the keyword fight). Final truncation to ``limit``
    # happens AFTER scoring.
    scoring_pool = max(limit, limit * SCORING_POOL_MULTIPLIER)
    merged = _rrf_merge(
        {
            "fts_chapter": fts_chapter, "vss_chapter": vss_chapter,
            "graph_chapter": graph_chapter,
            "fts_doc_section": fts_section, "vss_doc_section": vss_section,
            "graph_doc_section": graph_section,
        },
        limit=scoring_pool,
    )
    # Collapse chapter rows that share book + content prefix (Phase 1 splitter
    # bug — see _dedupe_by_content). Doc_section rows pass through unchanged.
    merged = _dedupe_by_content(merged)

    # Phase 4.5: feed the merged set through the ranking engine. RRF score
    # becomes the relevance component; recency / doc_alignment / corroboration
    # / authority are looked up per-result and combined under the chosen
    # weight profile.
    weights = ranking.WEIGHT_PROFILES[weight_profile]

    if mode == "generation":
        gen_ranker = ranking.GenerationRanker(_CONN, weights, top_k=limit)
        if selection_strategy is None:
            # No strategy specified — return the combined-score-sorted view
            # so downstream callers can pick their own filter. Same shape as
            # before to keep API compatibility for the no-strategy case.
            scored = []
            for r in merged:
                comps = ranking.compute_components_for_result(_CONN, r)
                scored.append(ranking.ScoredResult(
                    result=r, components=comps, combined=comps.combine(weights),
                ))
            scored.sort(key=lambda s: s.combined, reverse=True)
            return {
                "query": query,
                "mode": mode,
                "weight_profile": weight_profile,
                "selection_strategy": None,
                "results": [_scored_to_dict(s) for s in scored[:limit]],
                "dropped": [],
            }

        # Strategy specified — let GenerationRanker apply the §8.3 selector
        # and surface dropped-source provenance for §8.6 auditability.
        gen_output = gen_ranker.select(merged, strategy=selection_strategy)
        return {
            "query": query,
            "mode": mode,
            "weight_profile": weight_profile,
            "selection_strategy": gen_output.strategy,
            "results": [_scored_to_dict(s) for s in gen_output.selected],
            "dropped": [
                {**_scored_to_dict(s), "drop_reason": reason}
                for s, reason in gen_output.dropped
            ],
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
        "discovery": discovery_outcomes,
    }


def _run_auto_discovery(
    query: str, search_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run AutoDiscoveryOrchestrator inside a Topology A writer context.

    Returns one summary dict per attempted query term so callers can surface
    what was probed, what got ingested, and what needs user disambiguation.
    Best-effort: if the RW writer can't open (another process holds it, disk
    error, etc.), logs the failure and returns an empty list so the search
    still completes against the existing corpus.
    """
    assert _RESOLVER is not None
    summaries: list[dict[str, Any]] = []
    try:
        with _temporarily_open_writer() as rw_conn:
            # The orchestrator needs an RW connection (for the InlineIngester) and
            # a resolver (for the gap detector). Build a fresh resolver against the
            # writer connection — the module-level _RESOLVER is None right now
            # (cleared by _temporarily_open_writer entry).
            rw_resolver = EntityResolver(rw_conn, model=_MODEL)
            orchestrator = discovery.AutoDiscoveryOrchestrator(
                rw_conn, rw_resolver, embedder=_MODEL,
            )
            outcomes = orchestrator.run(query, search_response)
    except RuntimeError as e:
        LOG.warning("auto-discovery skipped (writer unavailable): %s", e)
        # _bootstrap will rebuild _CONN on the next call if the RO reopen
        # also failed inside the context manager.
        _bootstrap()
        return []
    for out in outcomes:
        summaries.append({
            "query_term": out.query_term,
            "decision": out.decision,
            "source": out.source,
            "doc_source_id": out.doc_source_id,
            "chosen_match": (
                {"name": out.chosen_match.name,
                 "identifier": out.chosen_match.identifier,
                 "description": out.chosen_match.description,
                 "score": out.chosen_match.score}
                if out.chosen_match else None
            ),
            "candidates": [
                {"name": m.name, "identifier": m.identifier,
                 "description": m.description, "score": m.score}
                for m in out.candidates
            ],
            "note": out.note,
        })
    return summaries


@mcp.tool
def disambiguate_discovery(
    source: str, identifier: str,
    display_name: Optional[str] = None,
    query_term: Optional[str] = None,
) -> dict[str, Any]:
    """Complete an ``asked_user`` discovery outcome by ingesting the user's pick.

    When ``search_chapters`` runs auto-discovery and the ConfidenceGate
    returns ``ambiguous`` (multiple candidate libraries / repos with similar
    scores), the response carries ``decision='asked_user'`` plus a
    ``candidates`` list. Use this tool to commit the user's choice to the
    catalog.

    Args:
        source: The probe source the candidate came from. One of
            ``context7``, ``deepwiki``, ``github`` — must match the
            ``source`` field of the asked_user outcome.
        identifier: The candidate's source-specific ID (e.g.,
            ``/duckdb/duckdb`` for Context7, ``redis/redis`` for DeepWiki).
            Pulled verbatim from the candidate's ``identifier`` field.
        display_name: Human-readable name to store in ``doc_source.name``.
            Defaults to ``identifier`` if not provided.
        query_term: Optional — the query term that triggered the original
            asked_user outcome. Logged to ``discovery_log`` for audit.

    Returns:
        ``{status, source, identifier, doc_source_id, snapshot_id,
        section_count}``. ``status`` is ``'ingested'`` on a successful
        new ingestion, ``'already_present'`` when the doc_source row
        already existed (idempotent re-call), or ``'error'`` with a
        ``message`` field on failure.
    """
    _bootstrap()
    if source not in discovery.DISCOVERY_AUTHORITY_DEFAULTS:
        raise ValueError(
            f"source must be one of "
            f"{sorted(discovery.DISCOVERY_AUTHORITY_DEFAULTS)}; got {source!r}"
        )
    if not identifier or not identifier.strip():
        raise ValueError("identifier must be non-empty")
    name = (display_name or identifier).strip()
    identifier = identifier.strip()

    try:
        with _temporarily_open_writer() as rw_conn:
            already_present = rw_conn.execute(
                "SELECT doc_source_id FROM doc_source "
                " WHERE source_type = ? AND identifier = ?",
                [source, identifier],
            ).fetchone()

            ingester = discovery.InlineIngester()
            doc_source_id = ingester.ingest(
                rw_conn,
                source=source, identifier=identifier,
                display_name=name, embedder=_MODEL,
            )
            if doc_source_id is None:
                discovery.log_discovery_event(
                    rw_conn,
                    query_term=query_term or identifier,
                    probe_source=source, probe_result="match",
                    match_count=1, top_match_name=name, top_match_score=None,
                    action_taken="discarded", doc_source_id=None,
                )
                return {
                    "status": "error",
                    "message": f"InlineIngester returned no doc_source_id for {source}/{identifier}",
                    "source": source, "identifier": identifier,
                }

            # Read back the latest snapshot for this source so the caller
            # knows what just got ingested.
            snap_row = rw_conn.execute(
                """
                SELECT snapshot_id,
                       (SELECT COUNT(*) FROM doc_section
                          WHERE snapshot_id = ds.snapshot_id) AS sections
                  FROM doc_snapshot ds
                 WHERE doc_source_id = ?
                 ORDER BY retrieved_at DESC, snapshot_id DESC
                 LIMIT 1
                """,
                [doc_source_id],
            ).fetchone()
            snapshot_id = int(snap_row[0]) if snap_row else None
            section_count = int(snap_row[1]) if snap_row else 0

            discovery.log_discovery_event(
                rw_conn,
                query_term=query_term or identifier,
                probe_source=source, probe_result="match",
                match_count=1, top_match_name=name, top_match_score=None,
                action_taken="ingested" if not already_present else "already_present",
                doc_source_id=doc_source_id,
            )
    except RuntimeError as e:
        LOG.warning("disambiguate_discovery: writer unavailable: %s", e)
        _bootstrap()
        return {
            "status": "error",
            "message": f"writer unavailable: {e}",
            "source": source, "identifier": identifier,
        }

    return {
        "status": "already_present" if already_present else "ingested",
        "source": source,
        "identifier": identifier,
        "display_name": name,
        "doc_source_id": doc_source_id,
        "snapshot_id": snapshot_id,
        "section_count": section_count,
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
                 substring(ch.content, 1, ?) AS raw_prefix,
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
               chapter_id, chapter_title, raw_prefix
          FROM ranked
         WHERE rn <= ?
         ORDER BY author_name, book_title, chapter_id
        """,
        [cid, cid, EXCERPT_FETCH_CHARS, limit_per_author],
    ).fetchall()

    by_author: dict[int, dict[str, Any]] = {}
    for author_id, author_name, book_id, book_title, ch_id, ch_title, raw_prefix in rows:
        excerpt = _clean_excerpt(raw_prefix)
        book_title = _clean_book_title(book_title)
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
