"""
db.py — Catalog connection helper for the KB MCP server.

Three DuckDB extensions matter for myPub: VSS (HNSW vector search), FTS
(BM25 full-text), and DuckPGQ (property-graph SQL/PGQ). None of them
auto-load on connect — every connection that needs them must `LOAD`
them explicitly. DuckPGQ also needs the `mypub` property graph
re-declared on each connection because graph definitions don't persist
across reopen (see scripts/build_property_graph.py:48 for the canonical
pattern this mirrors).

`open_catalog()` is the one place that knows the full incantation. The
MCP server calls it at startup; future callers (refresh pipeline,
ranking helpers) reuse it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
DEFAULT_GRAPH_DDL = PROJECT_ROOT / "schemas" / "property_graph.sql"


def _strip_sql_comments(sql: str) -> str:
    """Strip `-- …` line comments so DuckPGQ's parser doesn't choke on them."""
    return re.sub(r"--[^\n]*", "", sql)


def open_catalog(
    catalog_path: Optional[Path] = None,
    *,
    read_only: bool = False,
    graph_ddl_path: Optional[Path] = None,
) -> duckdb.DuckDBPyConnection:
    """Open the catalog with VSS, FTS, and DuckPGQ loaded and graph declared.

    Parameters
    ----------
    catalog_path:
        Path to the DuckDB file. Defaults to `<repo>/data/catalog.ddb`.
    read_only:
        If True, open the catalog in read-only mode. The graph DDL is
        skipped in read-only mode because `CREATE PROPERTY GRAPH` writes
        catalog metadata.
    graph_ddl_path:
        Path to the property-graph DDL. Defaults to
        `<repo>/schemas/property_graph.sql`.
    """
    catalog_path = catalog_path or DEFAULT_CATALOG
    graph_ddl_path = graph_ddl_path or DEFAULT_GRAPH_DDL

    conn = duckdb.connect(str(catalog_path), read_only=read_only)
    conn.execute("LOAD vss")
    conn.execute("LOAD fts")
    conn.execute("LOAD duckpgq")
    conn.execute("SET hnsw_enable_experimental_persistence = true")

    if not read_only:
        conn.execute(_strip_sql_comments(graph_ddl_path.read_text()))

    return conn
