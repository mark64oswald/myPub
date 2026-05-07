#!/usr/bin/env python3
"""
migrate_phase7_generator_tables.py — Add generalized generator output
tables to an existing catalog.

Adds:
  * seq_generated_package_id, seq_generated_unit_id, seq_generated_file_id
  * generated_package
  * generated_unit
  * generated_source
  * generated_file

These are the parallel-to-skill_* tables introduced in Phase 7. Skills
Factory continues using the existing skill_* tables; new generators
(Concept Neighborhood Map, Learning Path, etc.) land in generated_*.

Idempotent: re-runs no-op when the tables/sequences already exist.

Usage:
    .venv/bin/python3 scripts/migrate_phase7_generator_tables.py
    .venv/bin/python3 scripts/migrate_phase7_generator_tables.py --catalog PATH
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

from db import open_catalog  # noqa: E402

LOG = logging.getLogger("migrate_phase7")


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE' "
        "  AND table_name=? LIMIT 1",
        [table],
    ).fetchone()
    return row is not None


def _sequence_exists(conn: duckdb.DuckDBPyConnection, seq: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM duckdb_sequences() WHERE sequence_name = ? LIMIT 1",
        [seq],
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path,
                        default=PROJECT_ROOT / "data" / "catalog.ddb")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = open_catalog(args.catalog, read_only=False)
    try:
        # Sequences
        for seq in (
            "seq_generated_package_id",
            "seq_generated_unit_id",
            "seq_generated_file_id",
        ):
            if _sequence_exists(conn, seq):
                LOG.info("sequence %s already exists; skip", seq)
            else:
                conn.execute(f"CREATE SEQUENCE {seq} START 1")
                LOG.info("created sequence %s", seq)

        # generated_package
        if _table_exists(conn, "generated_package"):
            LOG.info("generated_package already exists; skip")
        else:
            conn.execute("""
                CREATE TABLE generated_package (
                    package_id      BIGINT     PRIMARY KEY DEFAULT nextval('seq_generated_package_id'),
                    generator_type  VARCHAR    NOT NULL,
                    name            VARCHAR    NOT NULL,
                    domain          VARCHAR,
                    target_audience VARCHAR,
                    source_query    TEXT,
                    metadata_json   TEXT,
                    created_at      TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (generator_type, name)
                )
            """)
            conn.execute("CREATE INDEX idx_generated_package_type ON generated_package(generator_type)")
            LOG.info("created generated_package")

        # generated_unit
        if _table_exists(conn, "generated_unit"):
            LOG.info("generated_unit already exists; skip")
        else:
            conn.execute("""
                CREATE TABLE generated_unit (
                    unit_id          BIGINT     PRIMARY KEY DEFAULT nextval('seq_generated_unit_id'),
                    package_id       BIGINT     NOT NULL REFERENCES generated_package(package_id),
                    unit_type        VARCHAR    NOT NULL,
                    name             VARCHAR    NOT NULL,
                    ordinal          INTEGER,
                    parent_unit_id   BIGINT,
                    content_markdown TEXT,
                    metadata_json    TEXT,
                    generation_notes TEXT,
                    created_at       TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX idx_generated_unit_package ON generated_unit(package_id)")
            conn.execute("CREATE INDEX idx_generated_unit_type    ON generated_unit(unit_type)")
            LOG.info("created generated_unit")

        # generated_source
        if _table_exists(conn, "generated_source"):
            LOG.info("generated_source already exists; skip")
        else:
            conn.execute("""
                CREATE TABLE generated_source (
                    unit_id      BIGINT   NOT NULL REFERENCES generated_unit(unit_id),
                    source_type  VARCHAR  NOT NULL,
                    source_id    BIGINT   NOT NULL,
                    score        DOUBLE,
                    weight       DOUBLE   DEFAULT 0,
                    drop_reason  VARCHAR,
                    PRIMARY KEY (unit_id, source_type, source_id)
                )
            """)
            LOG.info("created generated_source")

        # generated_file
        if _table_exists(conn, "generated_file"):
            LOG.info("generated_file already exists; skip")
        else:
            conn.execute("""
                CREATE TABLE generated_file (
                    file_id    BIGINT    PRIMARY KEY DEFAULT nextval('seq_generated_file_id'),
                    package_id BIGINT    NOT NULL REFERENCES generated_package(package_id),
                    unit_id    BIGINT    REFERENCES generated_unit(unit_id),
                    filename   VARCHAR   NOT NULL,
                    purpose    VARCHAR,
                    content    TEXT
                )
            """)
            conn.execute("CREATE INDEX idx_generated_file_package ON generated_file(package_id)")
            conn.execute("CREATE INDEX idx_generated_file_unit    ON generated_file(unit_id)")
            LOG.info("created generated_file")

        conn.execute("CHECKPOINT")
        LOG.info("migration complete")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
