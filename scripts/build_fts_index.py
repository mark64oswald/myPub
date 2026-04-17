#!/usr/bin/env python3
"""
build_fts_index.py — Build a BM25 full-text index on chapter.content.

DuckDB's FTS extension stores the index in a separate schema named
`fts_main_<table>`; after creation it's queried via
`fts_main_chapter.match_bm25(chapter_id, 'query terms')`. The index
persists across database reopen (unlike HNSW).

Usage:
    .venv/bin/python3 scripts/build_fts_index.py
    .venv/bin/python3 scripts/build_fts_index.py --no-rebuild  # skip if exists
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
FTS_SCHEMA = "fts_main_chapter"
LOG = logging.getLogger("build_fts_index")


TEST_QUERIES = [
    "change data capture",
    "dimensional modeling",
    "kafka streaming",
]


def _fts_index_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if the chapter FTS index has already been built."""
    rows = conn.execute(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name = ?",
        [FTS_SCHEMA],
    ).fetchone()
    return rows is not None


def _build_index(conn: duckdb.DuckDBPyConnection) -> None:
    """Build (or overwrite) the BM25 index on chapter.content."""
    LOG.info("building FTS index on chapter.content …")
    # Porter stemmer + english stopwords are the defaults. Overwrite=1 makes
    # the call idempotent.
    conn.execute(
        "PRAGMA create_fts_index('chapter', 'chapter_id', 'content', "
        "stemmer='porter', stopwords='english', ignore='(\\\\.|[^a-z])+', "
        "strip_accents=1, lower=1, overwrite=1)"
    )


def _run_test_queries(conn: duckdb.DuckDBPyConnection) -> int:
    """Run a few canned queries and report hit counts."""
    missing = 0
    for q in TEST_QUERIES:
        rows = conn.execute(
            f"""
            SELECT chapter_id,
                   {FTS_SCHEMA}.match_bm25(chapter_id, ?) AS score
              FROM chapter
             WHERE score IS NOT NULL
             ORDER BY score DESC
             LIMIT 5
            """,
            [q],
        ).fetchall()
        if not rows:
            LOG.warning("no hits for %r", q)
            missing += 1
            continue
        LOG.info("query %r → %d top hits (best score=%.3f)", q, len(rows), rows[0][1])
        for chapter_id, score in rows[:3]:
            meta = conn.execute(
                "SELECT b.title, c.title "
                "  FROM chapter c JOIN book b ON c.book_id = b.book_id "
                " WHERE c.chapter_id = ?",
                [chapter_id],
            ).fetchone()
            book_title = (meta[0] or "")[:50]
            chap_title = (meta[1] or "")[:50]
            LOG.info("    %.3f  %s  :: %s", score, book_title, chap_title)
    return missing


def main() -> int:
    """Build and verify the chapter FTS index."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--no-rebuild", action="store_true",
                        help="Skip building if an FTS index already exists.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    try:
        conn.execute("LOAD fts")
        if _fts_index_exists(conn) and args.no_rebuild:
            LOG.info("FTS index already exists; skipping build (--no-rebuild).")
        else:
            _build_index(conn)
        missing = _run_test_queries(conn)
        return 0 if missing == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
