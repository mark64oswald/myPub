#!/usr/bin/env python3
"""
Migrate data/catalog.ddb to the v2 schema.

Per user direction (clean-slate migration): backs up the v1 database,
drops v1 tables and views, and creates the v2 schema from
schemas/catalog.sql.

Usage:
    .venv/bin/python3 scripts/migrate_v2_schema.py                  # backup + migrate
    .venv/bin/python3 scripts/migrate_v2_schema.py --no-backup      # skip backup
    .venv/bin/python3 scripts/migrate_v2_schema.py --catalog PATH   # custom path
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
DEFAULT_BACKUP = PROJECT_ROOT / "data" / "catalog_v1_backup.ddb"
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"


def summarize_tables(conn: duckdb.DuckDBPyConnection, label: str) -> None:
    """Print the list of tables/views with row counts under a header label."""
    print(f"\n=== {label} ===")
    tables = conn.execute(
        """
        SELECT table_name, table_type
        FROM information_schema.tables
        WHERE table_schema = 'main'
        ORDER BY table_type, table_name
        """
    ).fetchall()
    if not tables:
        print("  (no tables)")
        return
    for name, kind in tables:
        if kind == "BASE TABLE":
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"  {name:<32} {kind:<12} rows={count}")
        else:
            print(f"  {name:<32} {kind}")


def backup_catalog(catalog: Path, backup: Path) -> None:
    """Copy the catalog to a backup path, skipping if the backup already exists."""
    if not catalog.exists():
        print(f"No existing catalog at {catalog} — skipping backup.")
        return
    if backup.exists():
        print(f"Backup already exists at {backup} — skipping (not overwriting).")
        return
    shutil.copy2(catalog, backup)
    print(f"Backup written: {backup} ({backup.stat().st_size:,} bytes)")


def drop_all_objects(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop every base table and view in main schema.

    DuckDB does not defer FK checks, so we iterate: each pass drops any object
    whose dependencies are gone, until nothing remains or we stop making
    progress. Views go first (they can reference tables but not vice versa).
    """
    views = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='VIEW'"
        ).fetchall()
    ]
    for v in views:
        conn.execute(f'DROP VIEW IF EXISTS "{v}" CASCADE')

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE'"
        ).fetchall()
    ]
    remaining = set(tables)
    while remaining:
        dropped_this_pass = set()
        for t in list(remaining):
            try:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
                dropped_this_pass.add(t)
            except duckdb.CatalogException:
                # Another table still references this one; try again next pass.
                continue
        if not dropped_this_pass:
            raise RuntimeError(
                f"Could not drop tables (FK cycle?): {sorted(remaining)}"
            )
        remaining -= dropped_this_pass

    sequences = [
        r[0]
        for r in conn.execute(
            "SELECT sequence_name FROM duckdb_sequences() "
            "WHERE schema_name='main'"
        ).fetchall()
    ]
    for s in sequences:
        conn.execute(f'DROP SEQUENCE IF EXISTS "{s}" CASCADE')
    print(f"Dropped {len(views)} view(s), {len(tables)} table(s), {len(sequences)} sequence(s).")


def apply_schema(conn: duckdb.DuckDBPyConnection, schema_file: Path) -> None:
    """Execute the entire DDL file against the connection."""
    sql = schema_file.read_text()
    conn.execute(sql)
    print(f"Applied schema from {schema_file}")


def main() -> int:
    """Back up the v1 catalog, drop its objects, and apply the v2 schema."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    catalog: Path = args.catalog
    backup: Path = args.backup

    print(f"Catalog: {catalog}")
    print(f"Schema:  {SCHEMA_FILE}")

    if not args.no_backup:
        backup_catalog(catalog, backup)

    catalog.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(catalog))
    try:
        summarize_tables(conn, "BEFORE migration")
        drop_all_objects(conn)
        apply_schema(conn, SCHEMA_FILE)
        summarize_tables(conn, "AFTER migration")
    finally:
        conn.close()

    print("\nMigration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
