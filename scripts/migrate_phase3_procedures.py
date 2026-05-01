#!/usr/bin/env python3
"""
migrate_phase3_procedures.py — Schema additions for Phase 3 procedure extraction.

Two additive changes:

    chapter.procedure_attempted_at TIMESTAMP
        Mirrors `extraction_attempted_at` from Phase 2. Set after a sub-agent
        has been run against a chapter for procedure extraction, so resumable
        sessions can skip chapters that already returned zero procedures
        instead of dispatching them again.

    procedure_concept (procedure_id, concept_id)
        Link table for the "procedure operates on these concepts" relationship
        described in arch §5.2. Concept references on the extractor output
        pass through EntityResolver, then land here. The IMPLEMENTS edge
        (Procedure → Pattern) continues to use the existing
        `procedure.implements_pattern` column.

Usage:
    .venv/bin/python3 scripts/migrate_phase3_procedures.py
    .venv/bin/python3 scripts/migrate_phase3_procedures.py --catalog PATH

Idempotent: re-runs no-op when the additions are already present.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("migrate_phase3_procedures")


def _column_exists(
    conn: duckdb.DuckDBPyConnection, table: str, column: str
) -> bool:
    """Return True iff `table.column` is present in the main schema."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name=? AND column_name=? LIMIT 1",
        [table, column],
    ).fetchone()
    return row is not None


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Return True iff base table `table` exists in the main schema."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE' "
        "  AND table_name=? LIMIT 1",
        [table],
    ).fetchone()
    return row is not None


def _alter_add(
    conn: duckdb.DuckDBPyConnection, table: str, column: str, ddl: str
) -> bool:
    """ALTER TABLE ... ADD COLUMN unless already present; return True if added."""
    if _column_exists(conn, table, column):
        LOG.info("  %-10s %-25s already present", table, column)
        return False
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
    LOG.info("  %-10s %-25s added", table, column)
    return True


def _create_procedure_concept(conn: duckdb.DuckDBPyConnection) -> bool:
    """Create the procedure_concept link table; return True if created."""
    if _table_exists(conn, "procedure_concept"):
        LOG.info("  procedure_concept already present")
        return False
    conn.execute(
        """
        CREATE TABLE procedure_concept (
            procedure_id BIGINT NOT NULL REFERENCES procedure(procedure_id),
            concept_id   BIGINT NOT NULL REFERENCES concept(concept_id),
            PRIMARY KEY (procedure_id, concept_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_procedure_concept_concept "
        "ON procedure_concept(concept_id)"
    )
    LOG.info("  procedure_concept created")
    return True


def main() -> int:
    """Apply the Phase 3 additive migration."""
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
        LOG.info("--- ALTER TABLE chapter ---")
        _alter_add(
            conn, "chapter",
            "procedure_attempted_at",
            "procedure_attempted_at TIMESTAMP",
        )

        LOG.info("--- CREATE TABLE procedure_concept ---")
        _create_procedure_concept(conn)

        conn.commit()
        LOG.info("migration complete")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
