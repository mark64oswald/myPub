#!/usr/bin/env python3
"""
migrate_phase4_4b_alignment.py — Schema additions for Phase 4.4b alignment edges.

Adds the seq_alignment_edge_id sequence and the alignment_edge table per
architecture §7.3 (DocSection → Chapter / DocSection → DocSection
CORROBORATES / CONTRADICTS edges with concept context). The existing
concept_relation table is concept↔concept and can't represent alignment
edges, so this is a new dedicated table rather than an extension.

Usage:
    .venv/bin/python3 scripts/migrate_phase4_4b_alignment.py
    .venv/bin/python3 scripts/migrate_phase4_4b_alignment.py --catalog PATH

Idempotent: re-runs no-op when the table is already present.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("migrate_phase4_4b_alignment")


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE' "
        "  AND table_name=? LIMIT 1",
        [table],
    ).fetchone()
    return row is not None


def _sequence_exists(conn: duckdb.DuckDBPyConnection, sequence: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM duckdb_sequences() WHERE sequence_name = ? LIMIT 1",
        [sequence],
    ).fetchone()
    return row is not None


def _create_sequence(conn: duckdb.DuckDBPyConnection) -> bool:
    if _sequence_exists(conn, "seq_alignment_edge_id"):
        LOG.info("  seq_alignment_edge_id already present")
        return False
    conn.execute("CREATE SEQUENCE seq_alignment_edge_id START 1")
    LOG.info("  seq_alignment_edge_id created")
    return True


def _create_alignment_edge(conn: duckdb.DuckDBPyConnection) -> bool:
    if _table_exists(conn, "alignment_edge"):
        LOG.info("  alignment_edge already present")
        return False
    conn.execute(
        """
        CREATE TABLE alignment_edge (
            alignment_edge_id   BIGINT      PRIMARY KEY DEFAULT nextval('seq_alignment_edge_id'),
            from_doc_section_id BIGINT      NOT NULL REFERENCES doc_section(doc_section_id),
            to_chapter_id       BIGINT      REFERENCES chapter(chapter_id),
            to_doc_section_id   BIGINT      REFERENCES doc_section(doc_section_id),
            concept_id          BIGINT      NOT NULL REFERENCES concept(concept_id),
            relation_type       VARCHAR     NOT NULL CHECK (relation_type IN ('CORROBORATES','CONTRADICTS')),
            confidence          DOUBLE,
            explanation         TEXT,
            created_at          TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
            CHECK ((to_chapter_id IS NOT NULL) OR (to_doc_section_id IS NOT NULL))
        )
        """
    )
    for ix_sql in (
        "CREATE INDEX idx_alignment_edge_from_section ON alignment_edge(from_doc_section_id)",
        "CREATE INDEX idx_alignment_edge_to_chapter   ON alignment_edge(to_chapter_id)",
        "CREATE INDEX idx_alignment_edge_to_section   ON alignment_edge(to_doc_section_id)",
        "CREATE INDEX idx_alignment_edge_concept      ON alignment_edge(concept_id)",
    ):
        conn.execute(ix_sql)
    LOG.info("  alignment_edge created (with 4 supporting indexes)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    LOG.info("catalog: %s", args.catalog)
    conn = duckdb.connect(str(args.catalog))
    try:
        LOG.info("--- CREATE SEQUENCE seq_alignment_edge_id ---")
        _create_sequence(conn)
        LOG.info("--- CREATE TABLE alignment_edge ---")
        _create_alignment_edge(conn)
        conn.commit()
        LOG.info("migration complete")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
