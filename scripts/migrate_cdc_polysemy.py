#!/usr/bin/env python3
"""
migrate_cdc_polysemy.py — Resolve "CDC" polysemy in the concept graph.

Phase 4.1 smoke testing surfaced that `compare_concept_across_authors("CDC")`
was returning the medical sense (concept 43776 "CDC", a Tool extracted from
the Healthcare Data Analytics book) instead of the dominant data-engineering
sense ("Change Data Capture"). The cause was twofold:

    1. concept 43776 had exact name "CDC", which wins exact-name match
       lookup ahead of any alias resolution.
    2. The catalog already contained a properly-named concept 82733
       "Centers for Disease Control and Prevention (CDC)" with its own
       embedding — making 43776 a duplicate.

This migration:

    A. Re-points concept_relation rows that referenced 43776 to point at
       the canonical 82733 (same medical-CDC sense, full name).
    B. Drops 43776's redundant embedding row.
    C. Deletes concept 43776.
    D. Adds "CDC" as an alias for concept 5948 ("Change Data Capture",
       the highest-mention data-engineering concept) so that lookups for
       the bare acronym resolve to the data-engineering sense — which is
       the dominant interpretation for this technical KB.

DuckDB 1.5 quirk worked around: the per-row FK checker fires on UPDATE/
DELETE against `concept` rows that have any inbound FK refs, even when
the SQL statement doesn't touch the FK column. The migration splits the
work into two transactions so the second one sees a committed state with
no remaining inbound refs to 43776.

Idempotent: re-runs are safe — each step checks current state first.

Usage:
    .venv/bin/python3 scripts/migrate_cdc_polysemy.py
    .venv/bin/python3 scripts/migrate_cdc_polysemy.py --catalog PATH
    .venv/bin/python3 scripts/migrate_cdc_polysemy.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("migrate_cdc_polysemy")

DUPLICATE_CDC_ID = 43776            # the bare-"CDC" Tool concept (medical sense)
CANONICAL_MEDICAL_CDC_ID = 82733    # "Centers for Disease Control and Prevention (CDC)"
CANONICAL_DE_CDC_ID = 5948          # "Change Data Capture" (Technique)


def _concept_exists(conn: duckdb.DuckDBPyConnection, concept_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM concept WHERE concept_id = ?", [concept_id]
    ).fetchone()
    return row is not None


def _alias_exists(
    conn: duckdb.DuckDBPyConnection, concept_id: int, alias: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM concept_alias "
        "WHERE concept_id = ? AND lower(alias) = lower(?)",
        [concept_id, alias],
    ).fetchone()
    return row is not None


def _verify_preconditions(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if the migration has work to do, False if already applied."""
    if not _concept_exists(conn, CANONICAL_DE_CDC_ID):
        LOG.error(
            "canonical Change Data Capture concept %d is missing — refusing "
            "to attach a 'CDC' alias to nothing", CANONICAL_DE_CDC_ID
        )
        return False

    duplicate_present = _concept_exists(conn, DUPLICATE_CDC_ID)
    alias_present = _alias_exists(conn, CANONICAL_DE_CDC_ID, "CDC")

    if not duplicate_present and alias_present:
        LOG.info("migration already applied — nothing to do")
        return False

    if duplicate_present and not _concept_exists(conn, CANONICAL_MEDICAL_CDC_ID):
        LOG.error(
            "canonical medical-CDC concept %d is missing — would orphan the "
            "FK refs from %d. Aborting; re-extract the Healthcare Data "
            "Analytics book first.", CANONICAL_MEDICAL_CDC_ID, DUPLICATE_CDC_ID
        )
        return False

    return True


def _run(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply the migration. Caller has confirmed preconditions."""
    duplicate_present = _concept_exists(conn, DUPLICATE_CDC_ID)
    alias_present = _alias_exists(conn, CANONICAL_DE_CDC_ID, "CDC")

    # Tx 1: unlink the duplicate's children + add the new alias. These all
    # touch child tables only, so the per-row FK checker on `concept` is
    # not exercised.
    if duplicate_present or not alias_present:
        conn.execute("BEGIN")
        if duplicate_present:
            n = conn.execute(
                "SELECT COUNT(*) FROM concept_relation WHERE to_concept_id = ?",
                [DUPLICATE_CDC_ID],
            ).fetchone()[0]
            conn.execute(
                "UPDATE concept_relation SET to_concept_id = ? "
                " WHERE to_concept_id = ?",
                [CANONICAL_MEDICAL_CDC_ID, DUPLICATE_CDC_ID],
            )
            LOG.info("re-pointed %d concept_relation row(s) %d → %d",
                     n, DUPLICATE_CDC_ID, CANONICAL_MEDICAL_CDC_ID)

            n = conn.execute(
                "SELECT COUNT(*) FROM concept_relation WHERE from_concept_id = ?",
                [DUPLICATE_CDC_ID],
            ).fetchone()[0]
            if n:
                conn.execute(
                    "UPDATE concept_relation SET from_concept_id = ? "
                    " WHERE from_concept_id = ?",
                    [CANONICAL_MEDICAL_CDC_ID, DUPLICATE_CDC_ID],
                )
                LOG.info("re-pointed %d outbound relation(s) from %d → %d",
                         n, DUPLICATE_CDC_ID, CANONICAL_MEDICAL_CDC_ID)

            conn.execute(
                "DELETE FROM concept_embedding WHERE concept_id = ?",
                [DUPLICATE_CDC_ID],
            )
            LOG.info("dropped concept_embedding for %d", DUPLICATE_CDC_ID)

        if not alias_present:
            conn.execute(
                "INSERT INTO concept_alias (concept_id, alias, alias_type) "
                "VALUES (?, 'CDC', 'acronym')",
                [CANONICAL_DE_CDC_ID],
            )
            LOG.info("added alias 'CDC' → concept %d", CANONICAL_DE_CDC_ID)
        conn.execute("COMMIT")

    # Tx 2: now the duplicate has no inbound FK refs; the per-row checker
    # accepts the DELETE.
    if duplicate_present:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM concept WHERE concept_id = ?", [DUPLICATE_CDC_ID])
        conn.execute("COMMIT")
        LOG.info("deleted duplicate concept %d", DUPLICATE_CDC_ID)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without committing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    try:
        if not _verify_preconditions(conn):
            return 0

        if args.dry_run:
            LOG.info("dry-run: would merge %d → %d and add 'CDC' alias to %d",
                     DUPLICATE_CDC_ID, CANONICAL_MEDICAL_CDC_ID, CANONICAL_DE_CDC_ID)
            return 0

        _run(conn)
        LOG.info("migration complete")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
