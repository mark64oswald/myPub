#!/usr/bin/env python3
"""
index_new_books.py — Incrementally index new or updated ePubs into the catalog.

Why this exists: index_books.py re-indexes by deleting a book's rows and
inserting fresh ones, which silently drops everything extracted from the old
chapters (concept relations, procedures, alignment edges, ...). Running it on
the whole folder would wipe the derived data of every book. This script only
touches files whose bytes changed, and carries derived data over to the new
chapters.

For each candidate ePub (default: every *.epub in the library folder):

    unchanged   same path, same file hash          -> skipped
    new         no catalog row for it               -> indexed
    updated     same path, different hash           -> re-indexed, data carried over
    retitled    path gone, but an existing book has the same "-<ID>.epub"
                suffix and its file no longer exists (O'Reilly renamed the book)
                                                    -> treated like "updated"

By default an updated/retitled book is treated as a fresh book: the old
version and everything extracted from it (concept relations, procedures,
alignment edges) are deleted, and the new chapters are left for a fresh
LLM extraction pass, because the new revision is more current (Mark's call,
2026-11-03).

With --carry-over, old -> new chapters are matched by (title, occurrence of
that title), the method used for the 2026-10-26 refresh, and chapter-sourced
data is re-pointed with prune_stale_books.remap_to_twins instead.

After writing, the search side is refreshed: generate_embeddings (new rows
only), build_fts_index, build_vss_index, build_property_graph.

LLM extraction (concepts, procedures) is NOT run here: it needs Claude
sub-agents. Chapters still awaiting it are counted in the summary.

A catalog snapshot (APFS clone, instant and space-free) is taken first:
    data/catalog_pre-autoindex-<YYYYmmdd-HHMMSS>.ddb

Usage:
    .venv/bin/python3 scripts/index_new_books.py --dry-run
    .venv/bin/python3 scripts/index_new_books.py                     # whole folder
    .venv/bin/python3 scripts/index_new_books.py --files a.epub b.epub
    .venv/bin/python3 scripts/index_new_books.py --skip-search-refresh
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import duckdb

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position
from prune_stale_books import HNSW_INDEX, KEYED_SOURCES, delete_books, remap_to_twins  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("index_new_books")
ID_SUFFIX_RE = re.compile(r"-([0-9A-Za-z]{10,13})\.epub$")
SEARCH_REFRESH = ["generate_embeddings.py", "build_fts_index.py", "build_vss_index.py",
                  "build_property_graph.py"]


# ----------------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------------

def _id_suffix(path: str) -> str | None:
    m = ID_SUFFIX_RE.search(path)
    return m.group(1) if m else None


def plan(conn: duckdb.DuckDBPyConnection, files: list[Path]) -> dict[str, list]:
    """Classify candidate files. Returns {new, updated, retitled, unchanged} lists.

    `updated` / `retitled` entries are (path, old_book_id); others are paths.
    """
    rows = conn.execute(
        "SELECT book_id, source_path, content_hash FROM book WHERE source_path LIKE '%.epub'"
    ).fetchall()
    by_path = {p: (b, h) for b, p, h in rows}
    gone_by_suffix: dict[str, list[int]] = defaultdict(list)
    for b, p, _ in rows:
        if not Path(p).exists() and _id_suffix(p):
            gone_by_suffix[_id_suffix(p)].append(b)

    out: dict[str, list] = {"new": [], "updated": [], "retitled": [], "unchanged": []}
    for f in files:
        key = str(f)
        if key in by_path:
            book_id, old_hash = by_path[key]
            if old_hash == ib._sha256_file(f):  # pylint: disable=protected-access
                out["unchanged"].append(f)
            else:
                out["updated"].append((f, book_id))
            continue
        olds = gone_by_suffix.get(_id_suffix(key) or "", [])
        if olds:
            # Most recently indexed predecessor carries over; any others are deleted too.
            out["retitled"].append((f, max(olds)))
            for extra in olds:
                if extra != max(olds):
                    out.setdefault("superseded", []).append(extra)
        else:
            out["new"].append(f)
    return out


def _norm(title: str | None) -> str:
    return re.sub(r"\s+", " ", title or "").strip().lower()


def chapter_keys(conn, book_id: int) -> list[tuple]:
    """[(chapter_id, (normalized title, nth occurrence), extraction_at, procedure_at)] in reading order."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for cid, title, ext, proc in conn.execute(
        "SELECT chapter_id, title, extraction_attempted_at, procedure_attempted_at FROM chapter "
        "WHERE book_id = ? ORDER BY chapter_num, chapter_id", [book_id]
    ).fetchall():
        seen[_norm(title)] += 1
        out.append((cid, (_norm(title), seen[_norm(title)]), ext, proc))
    return out


def chapter_pairs(conn, old_book: int, new_book: int) -> list[tuple[int, int]]:
    """Match chapters by (title, nth occurrence of that title) in reading order."""
    new = {key: cid for cid, key, _, _ in chapter_keys(conn, new_book)}
    return [(cid, new[key]) for cid, key, _, _ in chapter_keys(conn, old_book) if key in new]


# ----------------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------------

def connect_rw(catalog: Path, attempts: int = 12, wait: float = 5.0) -> duckdb.DuckDBPyConnection:
    """Open RW, waiting out short-lived locks (kb-mcp opens the catalog per tool call)."""
    for i in range(attempts):
        try:
            return duckdb.connect(str(catalog))
        except duckdb.IOException as e:
            if "lock" not in str(e).lower() or i == attempts - 1:
                raise
            LOG.info("catalog busy (%s); retrying in %.0fs", str(e).split("\n")[0][:120], wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def snapshot(catalog: Path) -> Path:
    dest = catalog.with_name("catalog_pre-autoindex-%s.ddb" % time.strftime("%Y%m%d-%H%M%S"))
    subprocess.run(["cp", "-c", str(catalog), str(dest)], check=True)  # APFS clone
    return dest


def _drop_hnsw(conn) -> bool:
    """Deleting from an HNSW-indexed table crawls; drop it for the batch (see prune_stale_books)."""
    try:
        conn.execute("LOAD vss")
        conn.execute("SET hnsw_enable_experimental_persistence = true")
    except duckdb.Error:
        return False  # no vss (tests, fresh catalogs): no HNSW index either
    if conn.execute("SELECT 1 FROM duckdb_indexes() WHERE index_name = ?", [HNSW_INDEX]).fetchone():
        conn.execute(f"DROP INDEX {HNSW_INDEX}")
        return True
    return False


def delete_old_version(conn, old_id: int) -> int:
    """Delete a superseded book and everything extracted from its chapters. Returns chapter count."""
    n = conn.execute("SELECT count(*) FROM chapter WHERE book_id = ?", [old_id]).fetchone()[0]
    conn.execute("CREATE OR REPLACE TEMP TABLE old_ch AS SELECT chapter_id FROM chapter WHERE book_id = ?",
                 [old_id])
    # Procedures are FK-referenced by procedure_concept: links first (auto-commit; see main()).
    conn.execute("DELETE FROM procedure_concept WHERE procedure_id IN (SELECT procedure_id FROM procedure "
                 "WHERE source_type = 'chapter' AND source_id IN (SELECT chapter_id FROM old_ch))")
    conn.execute("DELETE FROM procedure WHERE source_type = 'chapter' "
                 "AND source_id IN (SELECT chapter_id FROM old_ch)")
    delete_books(conn, [old_id])  # alignment edges, concept_relation, skill_source, embeddings, rows
    return n


def replace_book(conn, f: Path, old_id: int, carry_over: bool = False) -> tuple[int, int]:
    """Re-index `f` in place of book `old_id`. Returns (chapters carried, chapters dropped)."""
    if not carry_over:
        dropped = delete_old_version(conn, old_id)
        if not ib.index_book(conn, f):
            raise RuntimeError("indexing failed for %s" % f)
        LOG.info("%s: replaced old version (%d chapters); new chapters await extraction", f.name, dropped)
        return 0, dropped
    return carry_book_over(conn, f, old_id)


def carry_book_over(conn, f: Path, old_id: int) -> tuple[int, int]:
    """Re-index `f` in place of book `old_id`, carrying chapter-sourced data over.

    DuckDB can't UPDATE a unique column on an FK-referenced row (source_path) and
    alignment_edge FKs into chapter, so: snapshot the old chapters, park alignment
    edges, delete the old book's own rows (derived rows keep their logical
    source_id), index the new file, then remap old -> new chapter ids.
    Returns (chapters carried, chapters dropped).
    """
    old = chapter_keys(conn, old_id)
    old_ids = [c for c, _, _, _ in old]
    conn.execute("CREATE OR REPLACE TEMP TABLE old_ch (chapter_id BIGINT)")
    conn.executemany("INSERT INTO old_ch VALUES (?)", [[c] for c in old_ids])
    conn.execute("CREATE OR REPLACE TEMP TABLE parked_alignment AS "
                 "SELECT * FROM alignment_edge WHERE to_chapter_id IN (SELECT chapter_id FROM old_ch)")
    conn.execute("DELETE FROM alignment_edge WHERE to_chapter_id IN (SELECT chapter_id FROM old_ch)")
    conn.execute("DELETE FROM chapter_embedding WHERE chapter_id IN (SELECT chapter_id FROM old_ch)")
    conn.execute("DELETE FROM chapter WHERE book_id = ?", [old_id])
    conn.execute("DELETE FROM book_author WHERE book_id = ?", [old_id])
    conn.execute("DELETE FROM book WHERE book_id = ?", [old_id])

    if not ib.index_book(conn, f):
        raise RuntimeError("indexing failed for %s" % f)
    new_id = conn.execute("SELECT book_id FROM book WHERE source_path = ?", [str(f)]).fetchone()[0]
    new = {key: cid for cid, key, _, _ in chapter_keys(conn, new_id)}
    pairs = [(cid, new[key]) for cid, key, _, _ in old if key in new]

    if pairs:
        remap_to_twins(conn, pairs)  # concept_relation, skill_source, procedure, generated_source, ...
        conn.executemany(
            "UPDATE chapter SET extraction_attempted_at = ?, procedure_attempted_at = ? WHERE chapter_id = ?",
            [[ext, proc, new[key]] for _, key, ext, proc in old if key in new])
    if pairs:
        conn.execute("INSERT INTO alignment_edge SELECT p.* REPLACE (m.t AS to_chapter_id) "
                     "FROM parked_alignment p JOIN cmap m ON p.to_chapter_id = m.s")

    # Data extracted from chapters that no longer exist goes, as in prune_stale_books.delete_books.
    mapped = {s for s, _ in pairs}
    gone = [c for c in old_ids if c not in mapped]
    if gone:
        conn.execute("CREATE OR REPLACE TEMP TABLE gone_ch (chapter_id BIGINT)")
        conn.executemany("INSERT INTO gone_ch VALUES (?)", [[c] for c in gone])
        for tbl in KEYED_SOURCES:
            conn.execute(f"DELETE FROM {tbl} WHERE source_type = 'chapter' "
                         "AND source_id IN (SELECT chapter_id FROM gone_ch)")
    conn.execute("DROP TABLE IF EXISTS parked_alignment")
    LOG.info("%s: carried %d of %d chapters over to book %d", f.name, len(pairs), len(old_ids), new_id)
    return len(pairs), len(gone)


def apply(conn, work: dict[str, list], carry_over: bool = False) -> dict[str, int]:
    stats = defaultdict(int)
    had_hnsw = _drop_hnsw(conn)
    try:
        for f in work["new"]:
            stats["new" if ib.index_book(conn, f) else "failed"] += 1

        for kind in ("updated", "retitled"):
            for f, old_id in work[kind]:
                carried, dropped = replace_book(conn, f, old_id, carry_over)
                stats["chapters_carried"] += carried
                stats["chapters_dropped"] += dropped
                stats[kind] += 1

        for old_id in work.get("superseded", []):
            delete_old_version(conn, old_id)
    finally:
        if had_hnsw:
            conn.execute(f"CREATE INDEX {HNSW_INDEX} ON chapter_embedding USING HNSW (embedding) "
                         "WITH (metric = 'cosine')")
    return stats


def pending_extraction(conn, files: list[Path]) -> tuple[int, int]:
    if not files:
        return 0, 0
    paths = [str(f) for f in files]
    return conn.execute(
        "SELECT count(*) FILTER (WHERE extraction_attempted_at IS NULL), "
        "       count(*) FILTER (WHERE procedure_attempted_at IS NULL) "
        "FROM chapter c JOIN book b USING (book_id) WHERE b.source_path IN (SELECT unnest(?))",
        [paths],
    ).fetchone()


def refresh_search(catalog: Path) -> bool:
    """Embed new rows, then rebuild FTS, HNSW and the property graph (all accept --catalog)."""
    for script in SEARCH_REFRESH:
        LOG.info("running %s", script)
        start = time.time()
        result = subprocess.run([sys.executable, str(SCRIPTS_DIR / script), "--catalog", str(catalog)],
                                cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
        LOG.info("  %s finished in %.0fs (exit %d)", script, time.time() - start, result.returncode)
        if result.returncode != 0:
            LOG.error("  %s failed:\n%s", script, (result.stderr or result.stdout)[-2000:])
            return False
    return True


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=ib.DEFAULT_SOURCE)
    parser.add_argument("--catalog", type=Path, default=ib.DEFAULT_CATALOG)
    parser.add_argument("--files", nargs="+", type=Path, help="only consider these ePubs")
    parser.add_argument("--dry-run", action="store_true", help="classify only; write nothing")
    parser.add_argument("--carry-over", action="store_true",
                        help="keep extracted data from matching chapters of the old version "
                             "(default: drop it and re-extract)")
    parser.add_argument("--no-snapshot", action="store_true", help="skip the catalog snapshot")
    parser.add_argument("--skip-search-refresh", action="store_true",
                        help="don't rebuild embeddings / FTS / HNSW / graph afterwards")
    parser.add_argument("--notify", action="store_true", help="macOS notification when finished")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    files = [f.expanduser().resolve() for f in args.files] if args.files \
        else sorted(args.source.expanduser().glob("*.epub"))
    missing = [f for f in files if not f.exists()]
    if missing:
        LOG.error("not found: %s", ", ".join(map(str, missing)))
        return 2

    conn = duckdb.connect(str(args.catalog), read_only=True) if args.dry_run else connect_rw(args.catalog)
    try:
        work = plan(conn, files)
    finally:
        conn.close()

    todo = work["new"] + [f for f, _ in work["updated"]] + [f for f, _ in work["retitled"]]
    print("new %d, updated %d, retitled %d, unchanged %d" % (
        len(work["new"]), len(work["updated"]), len(work["retitled"]), len(work["unchanged"])))
    for label in ("new", "updated", "retitled"):
        for item in work[label]:
            f = item if isinstance(item, Path) else item[0]
            print("  %-9s %s" % (label, f.name))
    if args.dry_run or not todo:
        return 0

    # One writer at a time: back-to-back downloads queue up here instead of fighting over the lock.
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    lock = open(PROJECT_ROOT / "logs" / ".index_new_books.lock", "w")  # logs/ is gitignored
    fcntl.flock(lock, fcntl.LOCK_EX)

    snap = None if args.no_snapshot else snapshot(args.catalog)
    if snap:
        print("snapshot: %s" % snap)

    # Auto-commit on purpose: DuckDB 1.5 rejects deleting a parent row in the same
    # transaction that deleted its FK children (tests/test_duckdb_fk_bugs.py, variant 4).
    # The snapshot is the rollback path.
    conn = connect_rw(args.catalog)
    try:
        stats = apply(conn, work, args.carry_over)
        conn.execute("CHECKPOINT")
        pend_concepts, pend_procs = pending_extraction(conn, todo)
    except Exception:
        if snap:
            LOG.error("indexing failed part-way; restore with:  cp -c '%s' '%s'", snap, args.catalog)
        raise
    finally:
        conn.close()

    print("indexed: %s" % ", ".join("%s %d" % kv for kv in sorted(stats.items())))
    ok = True
    if not args.skip_search_refresh:
        ok = refresh_search(args.catalog)
        print("search indexes refreshed" if ok else "search refresh FAILED (see log)")
    print("awaiting LLM extraction: %d chapters (concepts), %d (procedures)" % (pend_concepts, pend_procs))
    status = 0 if ok and not stats.get("failed") else 1
    if args.notify:
        msg = ("%d book(s) indexed; %d chapters await extraction" % (len(todo), pend_concepts)
               if status == 0 else "indexing finished with errors; see logs/")
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "myPub"' % msg], check=False)
    return status


if __name__ == "__main__":
    sys.exit(main())
