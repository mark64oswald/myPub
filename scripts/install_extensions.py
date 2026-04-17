#!/usr/bin/env python3
"""
install_extensions.py — Install and smoke-test DuckDB extensions used by v2.

Ensures the catalog has FTS, VSS, and DuckPGQ available and exercises each
with a minimal end-to-end query. Safe to re-run.

Extensions:
    - fts     (core)        inverted index + BM25 on text columns
    - vss     (core)        HNSW over FLOAT[N] arrays for nearest-neighbor
    - duckpgq (community)   SQL/PGQ property-graph queries

Usage:
    .venv/bin/python3 scripts/install_extensions.py
    .venv/bin/python3 scripts/install_extensions.py --catalog PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"


def install_and_load(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    *,
    community: bool = False,
) -> None:
    """Install (if needed) and LOAD the given extension."""
    repo = "community" if community else "core"
    conn.execute(f"INSTALL {name}" + (f" FROM {repo}" if community else ""))
    conn.execute(f"LOAD {name}")


# ----------------------------------------------------------------------------
# Smoke tests
# ----------------------------------------------------------------------------

def smoke_fts(conn: duckdb.DuckDBPyConnection) -> tuple[bool, str]:
    """Build a BM25 index over a small temp table and run a match query."""
    try:
        conn.execute("DROP TABLE IF EXISTS _fts_smoke")
        conn.execute(
            """
            CREATE TEMP TABLE _fts_smoke (id INTEGER, body TEXT);
            INSERT INTO _fts_smoke VALUES
                (1, 'change data capture streams updates from a database'),
                (2, 'star schemas are central to dimensional modeling'),
                (3, 'kafka is a log-structured streaming platform');
            """
        )
        conn.execute(
            "PRAGMA create_fts_index('_fts_smoke', 'id', 'body', overwrite=1)"
        )
        rows = conn.execute(
            """
            SELECT id, fts_main__fts_smoke.match_bm25(id, 'change data capture') AS score
            FROM _fts_smoke
            WHERE score IS NOT NULL
            ORDER BY score DESC
            """
        ).fetchall()
        if not rows or rows[0][0] != 1:
            return False, f"FTS returned unexpected rows: {rows}"
        return True, f"FTS match_bm25 picked row 1 (score={rows[0][1]:.3f})"
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False, f"FTS smoke test failed: {exc}"


def smoke_vss(conn: duckdb.DuckDBPyConnection) -> tuple[bool, str]:
    """Build an HNSW index on a small FLOAT[4] table and run a nearest-neighbor query."""
    try:
        conn.execute("SET hnsw_enable_experimental_persistence = true")
        conn.execute("DROP TABLE IF EXISTS _vss_smoke")
        conn.execute(
            """
            CREATE TABLE _vss_smoke (id INTEGER, vec FLOAT[4]);
            INSERT INTO _vss_smoke VALUES
                (1, [1.0, 0.0, 0.0, 0.0]),
                (2, [0.0, 1.0, 0.0, 0.0]),
                (3, [0.9, 0.1, 0.0, 0.0]),
                (4, [0.0, 0.0, 1.0, 0.0]);
            """
        )
        conn.execute(
            "CREATE INDEX _vss_smoke_hnsw ON _vss_smoke USING HNSW (vec) WITH (metric = 'cosine')"
        )
        rows = conn.execute(
            """
            SELECT id, array_cosine_distance(vec, [1.0, 0.0, 0.0, 0.0]::FLOAT[4]) AS d
            FROM _vss_smoke
            ORDER BY d ASC
            LIMIT 2
            """
        ).fetchall()
        conn.execute("DROP INDEX _vss_smoke_hnsw")
        conn.execute("DROP TABLE _vss_smoke")
        if not rows or rows[0][0] != 1 or rows[1][0] != 3:
            return False, f"VSS returned unexpected ordering: {rows}"
        d0, d1 = rows[0][1], rows[1][1]
        return True, (
            f"VSS HNSW + cosine distance ranked rows 1,3 first "
            f"(d={d0:.4f}, {d1:.4f})"
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False, f"VSS smoke test failed: {exc}"


def smoke_duckpgq(conn: duckdb.DuckDBPyConnection) -> tuple[bool, str]:
    """Define a tiny property graph and run a variable-length path query.

    Conventions learned from DuckPGQ experimentation:
    * Labels must be lowercase (Node/Edge hit reserved-word parser errors).
    * Every edge pattern in a MATCH needs a bound variable name.
    """
    try:
        conn.execute("DROP PROPERTY GRAPH IF EXISTS _pgq_smoke")
        conn.execute("DROP TABLE IF EXISTS _pgq_edge")
        conn.execute("DROP TABLE IF EXISTS _pgq_node")
        conn.execute(
            "CREATE TABLE _pgq_node (id INTEGER PRIMARY KEY, name VARCHAR)"
        )
        conn.execute(
            "INSERT INTO _pgq_node VALUES (1, 'A'), (2, 'B'), (3, 'C'), (4, 'D')"
        )
        conn.execute("CREATE TABLE _pgq_edge (src INTEGER, dst INTEGER)")
        conn.execute(
            "INSERT INTO _pgq_edge VALUES (1, 2), (2, 3), (3, 4)"
        )
        conn.execute(
            """
            CREATE PROPERTY GRAPH _pgq_smoke
            VERTEX TABLES (_pgq_node LABEL v)
            EDGE TABLES (
                _pgq_edge
                SOURCE KEY (src) REFERENCES _pgq_node (id)
                DESTINATION KEY (dst) REFERENCES _pgq_node (id)
                LABEL e
            )
            """
        )
        rows = conn.execute(
            """
            FROM GRAPH_TABLE (_pgq_smoke
                MATCH p = (a:v)-[k:e]->{1,5}(b:v)
                WHERE a.name = 'A' AND b.name = 'D'
                COLUMNS (path_length(p) AS hops)
            )
            """
        ).fetchall()
        conn.execute("DROP PROPERTY GRAPH _pgq_smoke")
        conn.execute("DROP TABLE _pgq_edge")
        conn.execute("DROP TABLE _pgq_node")
        if not rows or rows[0][0] != 3:
            return False, f"DuckPGQ returned unexpected path length: {rows}"
        return True, "DuckPGQ SQL/PGQ MATCH found 3-hop path A→B→C→D"
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False, f"DuckPGQ smoke test failed: {exc}"


# ----------------------------------------------------------------------------
# Top-level
# ----------------------------------------------------------------------------

def main() -> int:
    """Install FTS/VSS/DuckPGQ into the given catalog and run smoke tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()

    print(f"Catalog: {args.catalog}")
    conn = duckdb.connect(str(args.catalog))
    try:
        print("Installing + loading extensions…")
        install_and_load(conn, "fts")
        install_and_load(conn, "vss")
        install_and_load(conn, "duckpgq", community=True)

        results = [
            ("fts",     *smoke_fts(conn)),
            ("vss",     *smoke_vss(conn)),
            ("duckpgq", *smoke_duckpgq(conn)),
        ]

        print()
        all_ok = True
        for name, ok, msg in results:
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {name:<8} {msg}")
            all_ok = all_ok and ok

        print()
        if all_ok:
            print("All extensions installed and verified.")
            return 0
        print("One or more smoke tests failed.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
