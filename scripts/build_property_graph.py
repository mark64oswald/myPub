#!/usr/bin/env python3
"""
build_property_graph.py — Apply schemas/property_graph.sql to the catalog
and exercise the graph with smoke queries.

Unlike FTS and VSS, DuckPGQ property-graph definitions do not persist
across database reopen — they must be re-declared every connection. The
MCP server runs this (or the DDL inline) at startup.

Phase 1 queries hit the edge tables that actually have data:
    book_author (wrote)   —  541 books × ~1 author each
    chapter    (contains) —  113K chapter rows
Concept / skill / doc queries will return empty until later phases;
they're still executed as syntax-validation smoke tests.

Usage:
    .venv/bin/python3 scripts/build_property_graph.py
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
GRAPH_DDL = PROJECT_ROOT / "schemas" / "property_graph.sql"
LOG = logging.getLogger("build_property_graph")


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- …` line comments.

    DuckPGQ's parser extension can't handle leading `--` comments before
    property-graph statements (DROP/CREATE PROPERTY GRAPH) — it rejects them
    with a plain-SQL parser error. We strip them before handing the DDL off.
    """
    return re.sub(r"--[^\n]*", "", sql)


def _apply_graph(conn: duckdb.DuckDBPyConnection) -> None:
    """Load DuckPGQ and (re-)create the `mypub` property graph."""
    conn.execute("LOAD duckpgq")
    conn.execute(_strip_sql_comments(GRAPH_DDL.read_text()))
    LOG.info("property graph `mypub` declared")


def _check_author_wrote_book(conn: duckdb.DuckDBPyConnection) -> None:
    """Top-5 authors by book count via author-[:wrote]->book traversal."""
    LOG.info("query: top authors by book count")
    rows = conn.execute(
        """
        SELECT author, COUNT(*) AS books
          FROM GRAPH_TABLE (mypub
              MATCH (a:author)-[w:wrote]->(b:book)
              COLUMNS (a.name AS author, b.book_id AS bid)
          )
         GROUP BY author
         ORDER BY books DESC
         LIMIT 5
        """
    ).fetchall()
    for author, books in rows:
        LOG.info("    %d books  %s", books, author)


def _check_book_chapters(conn: duckdb.DuckDBPyConnection) -> None:
    """Count chapters in a well-known book via book-[:contains]->chapter."""
    LOG.info("query: chapter count for 'Kimball' book via CONTAINS")
    rows = conn.execute(
        """
        FROM GRAPH_TABLE (mypub
            MATCH (b:book)-[c:book_contains]->(ch:chapter)
            WHERE b.title ILIKE '%Data Warehouse Toolkit%'
            COLUMNS (b.title AS book, ch.chapter_id AS cid)
        )
        """
    ).fetchall()
    if not rows:
        LOG.warning("    no chapters found for the Kimball book")
        return
    book = rows[0][0]
    LOG.info("    %s → %d chapters", book[:60], len(rows))


def _check_author_to_chapters(conn: duckdb.DuckDBPyConnection) -> None:
    """Two-hop traversal author→book→chapter."""
    LOG.info("query: author → book → chapter 2-hop")
    rows = conn.execute(
        """
        FROM GRAPH_TABLE (mypub
            MATCH (a:author)-[w:wrote]->(b:book)-[c:book_contains]->(ch:chapter)
            WHERE a.name = 'Ralph Kimball'
            COLUMNS (b.title AS book, ch.title AS chapter)
        )
        LIMIT 5
        """
    ).fetchall()
    if not rows:
        LOG.warning("    no results for Ralph Kimball → book → chapter")
        return
    for book, chapter in rows:
        LOG.info("    %s :: %s", (book or "")[:50], (chapter or "")[:50])


def _check_concept_relations(conn: duckdb.DuckDBPyConnection) -> None:
    """Smoke check: single-hop concept→concept traversal.

    Avoids variable-length matches like ``->{1,3}`` — DuckPGQ's CSR builder
    crashes (internal vector OOB) on those once concept_relation has
    meaningful row counts. Single-hop covers the syntax check we actually
    need; full prerequisite walks happen in `find_prerequisites` via a
    recursive CTE rather than DuckPGQ.
    """
    LOG.info("query: concept-[:concept_relates_to]->concept smoke check")
    rows = conn.execute(
        """
        FROM GRAPH_TABLE (mypub
            MATCH (c1:concept)-[r:concept_relates_to]->(c2:concept)
            COLUMNS (c1.name AS from_name, c2.name AS to_name)
        )
        LIMIT 5
        """
    ).fetchall()
    LOG.info("    %d concept→concept edges sampled", len(rows))
    for from_name, to_name in rows:
        LOG.info("    %s → %s", (from_name or "")[:40], (to_name or "")[:40])


def main() -> int:
    """Declare the property graph and exercise it with smoke queries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    try:
        _apply_graph(conn)
        _check_author_wrote_book(conn)
        _check_book_chapters(conn)
        _check_author_to_chapters(conn)
        _check_concept_relations(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
