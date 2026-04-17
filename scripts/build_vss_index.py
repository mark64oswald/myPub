#!/usr/bin/env python3
"""
build_vss_index.py — Build HNSW indexes on embedding side tables.

DuckDB's VSS extension requires experimental persistence to be explicitly
enabled for HNSW indexes inside file-backed databases:
    SET hnsw_enable_experimental_persistence = true;
Without this, the index is rejected with a "not yet stable" error. In
practice indexes survive a reopen of the database — but the MCP server
will still call this script on startup as a safety net.

Usage:
    .venv/bin/python3 scripts/build_vss_index.py
    .venv/bin/python3 scripts/build_vss_index.py --test-query "change data capture"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
LOG = logging.getLogger("build_vss_index")


INDEXES = [
    ("chapter_embedding_hnsw", "chapter_embedding", "embedding"),
    ("concept_embedding_hnsw", "concept_embedding", "embedding"),
]


def _ensure_extension(conn: duckdb.DuckDBPyConnection) -> None:
    """LOAD the VSS extension and enable persistent HNSW indexes."""
    conn.execute("LOAD vss")
    conn.execute("SET hnsw_enable_experimental_persistence = true")


def _build_index(
    conn: duckdb.DuckDBPyConnection, index_name: str, table: str, column: str
) -> int:
    """Create the HNSW index if missing; return the indexed row count."""
    existing = conn.execute(
        "SELECT index_name FROM duckdb_indexes() "
        "WHERE index_name = ? AND table_name = ?",
        [index_name, table],
    ).fetchone()
    row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    if existing:
        LOG.info("index %s already present on %s (%d rows)", index_name, table, row_count)
        return row_count

    if row_count == 0:
        LOG.info("skip %s: %s is empty", index_name, table)
        return 0

    LOG.info("building %s on %s(%s) over %d rows …", index_name, table, column, row_count)
    conn.execute(
        f"CREATE INDEX {index_name} ON {table} "
        f"USING HNSW ({column}) WITH (metric = 'cosine')"
    )
    LOG.info("built %s", index_name)
    return row_count


def _test_semantic_query(conn: duckdb.DuckDBPyConnection, query: str) -> None:
    """Embed `query` and show the 5 most-similar chapters + concepts."""
    # pylint: disable=import-outside-toplevel
    from sentence_transformers import SentenceTransformer

    LOG.info("loading %s for test query …", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)
    qvec = model.encode([query], convert_to_numpy=True)[0].astype("float32").tolist()

    LOG.info("top chapters for %r:", query)
    rows = conn.execute(
        """
        SELECT e.chapter_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS d,
               b.title, c.title
          FROM chapter_embedding e
          JOIN chapter c USING (chapter_id)
          JOIN book    b ON c.book_id = b.book_id
         ORDER BY d ASC
         LIMIT 5
        """,
        [qvec],
    ).fetchall()
    for chapter_id, d, book_title, chap_title in rows:
        LOG.info(
            "    d=%.4f  %s :: %s",
            d, (book_title or "")[:50], (chap_title or "")[:50],
        )

    concept_count = conn.execute("SELECT COUNT(*) FROM concept_embedding").fetchone()[0]
    if concept_count:
        LOG.info("top concepts for %r:", query)
        rows = conn.execute(
            """
            SELECT c.name,
                   array_cosine_distance(e.embedding, ?::FLOAT[384]) AS d
              FROM concept_embedding e
              JOIN concept c USING (concept_id)
             ORDER BY d ASC
             LIMIT 5
            """,
            [qvec],
        ).fetchall()
        for name, d in rows:
            LOG.info("    d=%.4f  %s", d, name)
    else:
        LOG.info("concept_embedding is empty — skipping concept similarity query")


def main() -> int:
    """Create HNSW indexes on chapter/concept embeddings and run a test query."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--test-query", default="how to implement change data capture")
    parser.add_argument("--skip-test", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    try:
        _ensure_extension(conn)
        for name, table, col in INDEXES:
            _build_index(conn, name, table, col)
        if not args.skip_test:
            _test_semantic_query(conn, args.test_query)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
