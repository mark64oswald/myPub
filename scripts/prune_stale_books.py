#!/usr/bin/env python3
"""
prune_stale_books.py — Remove catalog books whose source ePub is gone.

index_books.py never prunes: when a file in the eBook folder is deleted,
moved or renamed, its book/chapter rows (and everything extracted from
them) stay in the catalog and show up as duplicate search hits.

For each stale book this script looks for a **twin**: a live book with
the identical (chapter_num, title) sequence — typically the original of
an "Name (1).epub" re-download. When a twin exists, chapter-sourced
derived data is re-pointed to the twin's chapters before the stale rows
are deleted, so no extraction work is lost:

  * alignment_edge.to_chapter_id
  * concept_relation / skill_source        (rows whose remapped key
                                            already exists are dropped)
  * procedure / generated_source / concept_resolution_queue source_id
  * chapter.extraction_attempted_at / procedure_attempted_at markers

A stale book with no twin (e.g. a superseded early-release version) is
deleted along with its chapter-sourced concept_relation, skill_source
and alignment_edge rows. Procedures keep their now-dangling source_id.

DuckDB checks foreign keys against rows deleted earlier in the same
transaction, so the write path runs in autocommit (as index_books.py
does). Back up the catalog first; on APFS ``cp -c`` clones it instantly.

Usage:
    .venv/bin/python3 scripts/prune_stale_books.py             # dry run
    .venv/bin/python3 scripts/prune_stale_books.py --commit
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"

# Tables whose (source_type='chapter', source_id) points at a chapter.
# Keyed tables need collision handling when remapped onto the twin.
KEYED_SOURCES = {
    "concept_relation": ["from_concept_id", "to_concept_id", "relation_type"],
    "skill_source": ["skill_id"],
}
PLAIN_SOURCES = ["generated_source", "concept_resolution_queue"]
HNSW_INDEX = "chapter_embedding_hnsw"


def _chapter_signature(conn, book_id: int) -> list[tuple]:
    """Return the ordered (chapter_id, chapter_num, title) rows of a book."""
    return conn.execute(
        "SELECT chapter_id, chapter_num, title FROM chapter "
        "WHERE book_id = ? ORDER BY chapter_num, chapter_id",
        [book_id],
    ).fetchall()


# source_path prefixes for books that aren't files (scripts/ingest_pmc.py
# stores PubMed Central articles as "pmc:PMC<id>"). Never stale.
NON_FILE_PREFIXES = ("pmc:",)


def _is_file_book(path: str) -> bool:
    return not (path or "").startswith(NON_FILE_PREFIXES)


def find_plan(conn, book_ids: list[int] | None = None,
              ) -> tuple[list[tuple], dict[int, int], list[tuple]]:
    """Return (stale books, stale→twin book map, stale→twin chapter pairs).

    With ``book_ids``, exactly those books are the ones to remove (no file
    check, no twin remapping) — for dropping rows ingested by mistake.
    """
    books = conn.execute("SELECT book_id, source_path FROM book").fetchall()
    if book_ids:
        wanted = set(book_ids)
        return [(b, p) for b, p in books if b in wanted], {}, []
    stale = [(b, p) for b, p in books if _is_file_book(p) and not os.path.exists(p)]
    live = [b for b, p in books if _is_file_book(p) and os.path.exists(p)]

    # Index live books by chapter-title sequence for twin lookup.
    by_sig: dict[tuple, int] = {}
    for b in live:
        sig = tuple(r[1:] for r in _chapter_signature(conn, b))
        if sig:
            by_sig.setdefault(sig, b)

    twins: dict[int, int] = {}
    pairs: list[tuple] = []
    for b, _ in stale:
        rows = _chapter_signature(conn, b)
        twin = by_sig.get(tuple(r[1:] for r in rows))
        if twin is None:
            continue
        twins[b] = twin
        pairs += [(s[0], t[0]) for s, t in zip(rows, _chapter_signature(conn, twin))]
    return stale, twins, pairs


def remap_to_twins(conn, pairs: list[tuple]) -> None:
    """Re-point chapter-sourced derived data from stale chapters to twins."""
    conn.execute("CREATE OR REPLACE TEMP TABLE cmap (s BIGINT, t BIGINT)")
    conn.executemany("INSERT INTO cmap VALUES (?, ?)", [list(p) for p in pairs])

    conn.execute("""
        UPDATE chapter SET
            extraction_attempted_at = coalesce(chapter.extraction_attempted_at, s.extraction_attempted_at),
            procedure_attempted_at  = coalesce(chapter.procedure_attempted_at,  s.procedure_attempted_at)
        FROM cmap JOIN chapter s ON s.chapter_id = cmap.s
        WHERE chapter.chapter_id = cmap.t
    """)
    conn.execute("UPDATE alignment_edge SET to_chapter_id = cmap.t FROM cmap WHERE to_chapter_id = cmap.s")

    for tbl, key in KEYED_SOURCES.items():
        match = " AND ".join(f"x.{k} = r.{k}" for k in key)
        dropped = conn.execute(f"""
            DELETE FROM {tbl} r
            WHERE r.source_type = 'chapter' AND r.source_id IN (SELECT s FROM cmap)
              AND EXISTS (SELECT 1 FROM {tbl} x JOIN cmap m ON x.source_id = m.t
                          WHERE x.source_type = 'chapter' AND m.s = r.source_id AND {match})
        """).fetchone()[0]
        moved = conn.execute(f"""
            UPDATE {tbl} SET source_id = cmap.t FROM cmap
            WHERE source_type = 'chapter' AND source_id = cmap.s
        """).fetchone()[0]
        print(f"  {tbl}: remapped {moved}, dropped {dropped} already on twin")

    # procedure is FK-referenced by procedure_concept and DuckDB rejects
    # updates to referenced rows: park the links, remap, restore.
    conn.execute("""
        CREATE OR REPLACE TABLE prune_tmp_procedure_concept AS
        SELECT pc.* FROM procedure_concept pc JOIN procedure p USING (procedure_id)
        WHERE p.source_type = 'chapter' AND p.source_id IN (SELECT s FROM cmap)
    """)
    conn.execute("""
        DELETE FROM procedure_concept
        WHERE procedure_id IN (SELECT procedure_id FROM prune_tmp_procedure_concept)
    """)
    moved = conn.execute("""
        UPDATE procedure SET source_id = cmap.t FROM cmap
        WHERE source_type = 'chapter' AND source_id = cmap.s
    """).fetchone()[0]
    conn.execute("INSERT INTO procedure_concept SELECT * FROM prune_tmp_procedure_concept")
    conn.execute("DROP TABLE prune_tmp_procedure_concept")
    print(f"  procedure: remapped {moved}")

    for tbl in PLAIN_SOURCES:
        moved = conn.execute(f"""
            UPDATE {tbl} SET source_id = cmap.t FROM cmap
            WHERE source_type = 'chapter' AND source_id = cmap.s
        """).fetchone()[0]
        print(f"  {tbl}: remapped {moved}")


def delete_books(conn, book_ids: list[int]) -> None:
    """Delete stale books and the rows that FK into their chapters."""
    conn.execute(
        "CREATE OR REPLACE TEMP TABLE sc AS "
        "SELECT chapter_id FROM chapter WHERE book_id IN (SELECT unnest(?))",
        [book_ids],
    )
    conn.execute("DELETE FROM alignment_edge WHERE to_chapter_id IN (SELECT chapter_id FROM sc)")
    for tbl in KEYED_SOURCES:
        conn.execute(f"DELETE FROM {tbl} WHERE source_type = 'chapter' AND source_id IN (SELECT chapter_id FROM sc)")
    # Deleting from an HNSW-indexed table crawls (one stale-book run sat
    # in this DELETE for 40+ minutes), so drop the index, delete, and
    # rebuild it the way build_vss_index.py does.
    try:
        conn.execute("LOAD vss")
        conn.execute("SET hnsw_enable_experimental_persistence = true")
    except duckdb.Error:
        pass  # no vss available (tests, fresh catalogs): no HNSW index either
    has_index = conn.execute(
        "SELECT 1 FROM duckdb_indexes() WHERE index_name = ?", [HNSW_INDEX]).fetchone()
    if has_index:
        conn.execute(f"DROP INDEX IF EXISTS {HNSW_INDEX}")
    conn.execute("DELETE FROM chapter_embedding WHERE chapter_id IN (SELECT chapter_id FROM sc)")
    if has_index:
        conn.execute(f"CREATE INDEX {HNSW_INDEX} ON chapter_embedding USING HNSW (embedding) "
                     "WITH (metric = 'cosine')")
    conn.execute("DELETE FROM chapter WHERE chapter_id IN (SELECT chapter_id FROM sc)")
    conn.execute("DELETE FROM book_author WHERE book_id IN (SELECT unnest(?))", [book_ids])
    conn.execute("DELETE FROM book WHERE book_id IN (SELECT unnest(?))", [book_ids])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--commit", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--book-ids", help="remove exactly these books (comma list or a-b ranges), "
                                           "e.g. 728-767; skips the stale-file check")
    args = parser.parse_args()
    ids: list[int] = []
    for part in (args.book_ids or "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            ids += list(range(int(a), int(b) + 1))
        elif part.strip():
            ids.append(int(part))

    conn = duckdb.connect(str(args.catalog), read_only=not args.commit)
    try:
        stale, twins, pairs = find_plan(conn, ids or None)
        print(f"stale books: {len(stale)}  (with twin: {len(twins)}, chapter pairs: {len(pairs)})")
        for b, p in stale:
            print(f"  {b:>5}  {'twin ' + str(twins[b]) if b in twins else 'no twin':>10}  {Path(p).name}")
        if not stale:
            return 0
        if not args.commit:
            print("dry run: pass --commit to apply")
            return 0
        if pairs:
            remap_to_twins(conn, pairs)
        delete_books(conn, [b for b, _ in stale])
        books, missing = conn.execute("SELECT count(*) FROM book").fetchone()[0], sum(
            _is_file_book(p) and not os.path.exists(p)
            for (p,) in conn.execute("SELECT source_path FROM book").fetchall()
        )
        print(f"books now {books}, missing paths {missing}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
