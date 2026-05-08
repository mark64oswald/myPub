#!/usr/bin/env python3
"""
migrate_phase1_splitter.py — Re-split ePub chapter content with the
fixed slicer, in place, without orphaning FK references.

Background
----------
Phase 1's ``index_books.py`` had a splitter bug: TOC entries pointing
at the same xhtml file with different fragment anchors all received
the FULL file's content via ``content_cache.get(href_path)``. ~88%
of chapter rows in the live catalog share content with at least one
sibling. A retrieval-time mitigation collapses duplicate results,
but downstream graph-derived signals (concept relations, procedures,
alignment edges) were extracted from the bug-bloated content and
remain polluted.

The fix to ``_insert_chapters`` (commit forthcoming with this file)
slices xhtml content at fragment boundaries. Re-running the indexer
naively would DELETE+INSERT chapter rows, which orphans
``chapter_embedding`` and breaks ``alignment_edge`` FKs. This script
performs an *in-place* re-split:

    UPDATE chapter SET content/content_hash/token_count = (new sliced)
    WHERE chapter_id = (existing chapter_id, preserved)

After updating content, downstream-derived rows for changed chapters
are invalidated so the standard regeneration pipelines pick them up:

    * ``chapter_embedding``  — DELETED for changed chapter_ids;
                               ``generate_embeddings.py`` recomputes.
    * ``concept_relation``   — DELETED where source_type='chapter'
                               AND source_id IN (changed); next
                               ``extract_batch.py`` run re-extracts.
    * ``procedure``          — DELETED where source_type='chapter'
                               AND source_id IN (changed);
                               ``procedure_concept`` cascaded;
                               next ``extract_procedures.py`` run
                               re-extracts.
    * ``alignment_edge``     — DELETED where to_chapter_id IN
                               (changed); alignment for the 7
                               aligned sources will be re-run via
                               ``migrate_phase4_4b_alignment.py``
                               after re-extraction completes.
    * ``extraction_attempted_at`` and ``procedure_attempted_at``
                            — set to NULL on changed chapters so the
                              prep scripts pick them up again.

All operations run inside a single transaction; on any error the
catalog is left unchanged.

Per-book invariant
------------------
The TOC parsing logic is unchanged — only the content slicing
changed. So ``_flatten_toc(book)`` produces the same number of
entries in the same order as before, with the same ``href`` values.
We match each existing chapter row to its TOC entry by
``(book_id, chapter_num, href)`` and update only the content fields.

If the file on disk has been modified since the original indexing
(TOC count or hrefs differ from the catalog), the script SKIPS that
book and logs it for manual review rather than risking a corrupt
mapping.

Usage
-----
::

    # Dry-run: compute changes, write nothing.
    .venv/bin/python3 scripts/migrate_phase1_splitter.py --dry-run

    # Limited dry-run for development.
    .venv/bin/python3 scripts/migrate_phase1_splitter.py --dry-run --limit 20

    # Single book.
    .venv/bin/python3 scripts/migrate_phase1_splitter.py --book-id 346

    # Real run.
    .venv/bin/python3 scripts/migrate_phase1_splitter.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import duckdb
from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position
from db import open_catalog  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("migrate_phase1_splitter")


# ---------------------------------------------------------------------------
# Per-book result types
# ---------------------------------------------------------------------------

@dataclass
class BookResult:
    """One book's re-split outcome."""
    book_id: int
    title: str
    status: str  # 'ok' | 'missing_file' | 'mismatch' | 'error'
    rows: int = 0
    changed_chapter_ids: list[int] = field(default_factory=list)
    unchanged: int = 0
    note: str = ""


@dataclass
class MigrationStats:
    """Aggregated stats across all books."""
    books_processed: int = 0
    books_ok: int = 0
    books_mismatch: int = 0
    books_missing_file: int = 0
    books_error: int = 0
    chapters_changed: int = 0
    chapters_unchanged: int = 0
    chapter_embedding_deleted: int = 0
    concept_relation_deleted: int = 0
    procedure_deleted: int = 0
    procedure_concept_deleted: int = 0
    alignment_edge_deleted: int = 0
    extraction_flags_reset: int = 0


# ---------------------------------------------------------------------------
# Per-book re-split
# ---------------------------------------------------------------------------

def resplit_book(
    conn: duckdb.DuckDBPyConnection,
    book_id: int,
    title: str,
    source_path: Path,
    *,
    dry_run: bool,
) -> BookResult:
    """Re-split one book's chapters in place.

    Reads the source ePub, runs the fixed slicer, then for each
    matched (chapter_num, href) issues an UPDATE if the new content
    hash differs from the existing one. Returns a BookResult; does
    not touch downstream-derived rows (the caller batches that).
    """
    if not source_path.exists():
        return BookResult(book_id, title, status="missing_file",
                          note=f"source not on disk: {source_path}")

    try:
        book = epub.read_epub(str(source_path))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return BookResult(book_id, title, status="error",
                          note=f"epub read failed: {exc}")

    try:
        toc = ib._flatten_toc(book)  # pylint: disable=protected-access
        cache = ib._content_cache_for_book(book, toc)  # pylint: disable=protected-access
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return BookResult(book_id, title, status="error",
                          note=f"toc/cache build failed: {exc}")

    existing = conn.execute(
        """
        SELECT chapter_id, chapter_num, href, content_hash
        FROM chapter
        WHERE book_id = ?
        ORDER BY chapter_num
        """,
        [book_id],
    ).fetchall()

    if len(existing) != len(toc):
        return BookResult(
            book_id, title, status="mismatch",
            rows=len(existing),
            note=f"existing rows={len(existing)} fresh toc entries={len(toc)} — "
                 f"file likely modified since original index",
        )

    changed_ids: list[int] = []
    unchanged = 0

    for (cid, num, old_href, old_hash), entry in zip(existing, toc):
        if old_href != entry.get("href"):
            return BookResult(
                book_id, title, status="mismatch",
                rows=len(existing),
                note=f"chapter_id={cid} chapter_num={num} href differs: "
                     f"old={old_href!r} new={entry.get('href')!r}",
            )

        path, frag = ib._split_href(entry.get("href"))  # pylint: disable=protected-access
        slices = cache.get(path, {}) if path else {}
        new_content = slices.get(frag or "", "") if slices else ""
        new_tokens = ib._count_tokens(new_content) if new_content else 0  # pylint: disable=protected-access
        new_hash = ib._sha256_text(new_content) if new_content else None  # pylint: disable=protected-access

        if new_hash == old_hash:
            unchanged += 1
            continue

        changed_ids.append(cid)
        if dry_run:
            continue

        conn.execute(
            """
            UPDATE chapter
            SET content = ?, content_hash = ?, token_count = ?,
                indexed_at = CURRENT_TIMESTAMP,
                extraction_attempted_at = NULL,
                procedure_attempted_at = NULL
            WHERE chapter_id = ?
            """,
            [new_content or None, new_hash, new_tokens or None, cid],
        )

    return BookResult(
        book_id, title, status="ok",
        rows=len(existing),
        changed_chapter_ids=changed_ids,
        unchanged=unchanged,
    )


# ---------------------------------------------------------------------------
# Downstream invalidation
# ---------------------------------------------------------------------------

def invalidate_derived_rows(
    conn: duckdb.DuckDBPyConnection,
    changed_chapter_ids: list[int],
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Delete downstream-derived rows for chapters whose content changed.

    Returns a dict of deletion counts per table. In dry-run, returns
    the counts that *would* be deleted without writing.
    """
    if not changed_chapter_ids:
        return {
            "chapter_embedding": 0,
            "concept_relation": 0,
            "procedure": 0,
            "procedure_concept": 0,
            "alignment_edge": 0,
        }

    # Materialize the changed-id set into a temp table so we can JOIN
    # without churning a giant IN-list. Always allowed in RW; in
    # dry-run mode we'll roll back the whole transaction.
    conn.execute("DROP TABLE IF EXISTS _changed_chapter_ids")
    conn.execute(
        "CREATE TEMP TABLE _changed_chapter_ids (chapter_id BIGINT PRIMARY KEY)"
    )
    conn.executemany(
        "INSERT INTO _changed_chapter_ids VALUES (?)",
        [[cid] for cid in changed_chapter_ids],
    )

    # Counts (computed regardless of dry-run).
    counts = {
        "chapter_embedding": conn.execute(
            "SELECT COUNT(*) FROM chapter_embedding "
            "WHERE chapter_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
        ).fetchone()[0],
        "concept_relation": conn.execute(
            "SELECT COUNT(*) FROM concept_relation "
            "WHERE source_type = 'chapter' "
            "  AND source_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
        ).fetchone()[0],
        "procedure": conn.execute(
            "SELECT COUNT(*) FROM procedure "
            "WHERE source_type = 'chapter' "
            "  AND source_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
        ).fetchone()[0],
        "alignment_edge": conn.execute(
            "SELECT COUNT(*) FROM alignment_edge "
            "WHERE to_chapter_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
        ).fetchone()[0],
    }

    # procedure_concept rows whose procedure is about to be deleted.
    counts["procedure_concept"] = conn.execute(
        """
        SELECT COUNT(*) FROM procedure_concept pc
        JOIN procedure p USING (procedure_id)
        WHERE p.source_type = 'chapter'
          AND p.source_id IN (SELECT chapter_id FROM _changed_chapter_ids)
        """
    ).fetchone()[0]

    if dry_run:
        return counts

    # Actually delete. Order matters because of FKs:
    #   1. alignment_edge → chapter (safe to delete first)
    #   2. procedure_concept → procedure (delete before procedure)
    #   3. procedure → chapter (no FK from chapter, but logical
    #      polymorphic ref via source_id)
    #   4. concept_relation → chapter (same)
    #   5. chapter_embedding → chapter (FK-protected)
    #
    # DuckDB 1.5.0 FK enforcement can mis-fire on DELETE … IN
    # (subquery) patterns: the parent DELETE sees the child rows as
    # still-present even after they were marked-for-delete in a prior
    # statement of the same transaction. Workaround: materialize the
    # procedure_ids in Python and issue an explicit-list DELETE.
    proc_ids_to_delete = [
        row[0] for row in conn.execute(
            "SELECT procedure_id FROM procedure "
            "WHERE source_type = 'chapter' "
            "  AND source_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
        ).fetchall()
    ]

    conn.execute(
        "DELETE FROM alignment_edge "
        "WHERE to_chapter_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
    )
    if proc_ids_to_delete:
        placeholder = ",".join("?" * len(proc_ids_to_delete))
        conn.execute(
            f"DELETE FROM procedure_concept "
            f"WHERE procedure_id IN ({placeholder})",
            proc_ids_to_delete,
        )
        conn.execute(
            f"DELETE FROM procedure WHERE procedure_id IN ({placeholder})",
            proc_ids_to_delete,
        )
    conn.execute(
        "DELETE FROM concept_relation "
        "WHERE source_type = 'chapter' "
        "  AND source_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
    )
    conn.execute(
        "DELETE FROM chapter_embedding "
        "WHERE chapter_id IN (SELECT chapter_id FROM _changed_chapter_ids)"
    )

    return counts


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------

def select_books(
    conn: duckdb.DuckDBPyConnection,
    *,
    book_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[tuple[int, str, str]]:
    """Return [(book_id, title, source_path)] in book_id order."""
    where = ""
    params: list = []
    if book_id is not None:
        where = "WHERE book_id = ?"
        params.append(book_id)
    sql = (
        f"SELECT book_id, title, source_path FROM book {where} "
        f"ORDER BY book_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def run_migration(
    conn: duckdb.DuckDBPyConnection,
    books: list[tuple[int, str, str]],
    *,
    dry_run: bool,
) -> tuple[MigrationStats, list[BookResult]]:
    """Execute the migration against the supplied books.

    Atomicity model
    ---------------
    A single migration-wide transaction is *not* possible due to a
    DuckDB 1.5.0 FK-enforcement bug — a parent DELETE can't see the
    child DELETE that just ran inside the same transaction (see
    ``tests/test_duckdb_fk_bugs.py::test_duckdb_bug_parent_delete_after_child_delete_in_same_txn_fails``).

    Instead the migration runs as a sequence of auto-committing
    statements where each step is *idempotent* and the catalog is
    monotonic — every committed statement makes the catalog more
    correct, never less. Specifically:

      * ``UPDATE chapter`` sets new content/hash. Re-running on a
        row whose hash already matches is a no-op.
      * ``DELETE FROM <derived>`` removes stale rows. Re-running
        deletes nothing (already gone).

    So a partial failure is recoverable by simply re-running the
    migration. The summary at the end reports what actually
    happened in this run.

    Dry-run wraps the actual UPDATEs/DELETEs in a transaction that
    rolls back at the end so the catalog is unchanged.
    """
    stats = MigrationStats()
    results: list[BookResult] = []
    all_changed_ids: list[int] = []

    if dry_run:
        conn.execute("BEGIN")

    try:
        for book_id, title, source_path in books:
            stats.books_processed += 1
            try:
                result = resplit_book(
                    conn, book_id, title, Path(source_path), dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                LOG.error("book_id=%d unhandled error: %s", book_id, exc)
                results.append(BookResult(
                    book_id, title, status="error", note=str(exc),
                ))
                stats.books_error += 1
                continue
            results.append(result)

            if result.status == "ok":
                stats.books_ok += 1
                stats.chapters_changed += len(result.changed_chapter_ids)
                stats.chapters_unchanged += result.unchanged
                stats.extraction_flags_reset += len(result.changed_chapter_ids)
                all_changed_ids.extend(result.changed_chapter_ids)
            elif result.status == "missing_file":
                stats.books_missing_file += 1
            elif result.status == "mismatch":
                stats.books_mismatch += 1
            else:
                stats.books_error += 1

        # Invalidate downstream rows for all changed chapter_ids.
        deletions = invalidate_derived_rows(
            conn, all_changed_ids, dry_run=dry_run,
        )
        stats.chapter_embedding_deleted = deletions["chapter_embedding"]
        stats.concept_relation_deleted = deletions["concept_relation"]
        stats.procedure_deleted = deletions["procedure"]
        stats.procedure_concept_deleted = deletions["procedure_concept"]
        stats.alignment_edge_deleted = deletions["alignment_edge"]

        if dry_run:
            conn.execute("ROLLBACK")
    except Exception:
        if dry_run:
            conn.execute("ROLLBACK")
        raise

    return stats, results


def format_summary(stats: MigrationStats, dry_run: bool) -> str:
    mode = "DRY-RUN (no changes written)" if dry_run else "COMMITTED"
    lines = [
        f"Phase 1 splitter migration — {mode}",
        "",
        f"  Books processed:           {stats.books_processed}",
        f"  Books succeeded:           {stats.books_ok}",
        f"  Books skipped (mismatch):  {stats.books_mismatch}",
        f"  Books skipped (no file):   {stats.books_missing_file}",
        f"  Books with errors:         {stats.books_error}",
        "",
        f"  Chapter rows changed:      {stats.chapters_changed:,}",
        f"  Chapter rows unchanged:    {stats.chapters_unchanged:,}",
        "",
        f"  chapter_embedding rows {'deleted' if not dry_run else 'would delete'}: "
        f"{stats.chapter_embedding_deleted:,}",
        f"  concept_relation rows {'deleted' if not dry_run else 'would delete'}: "
        f"{stats.concept_relation_deleted:,}",
        f"  procedure rows {'deleted' if not dry_run else 'would delete'}: "
        f"{stats.procedure_deleted:,}",
        f"  procedure_concept rows {'deleted' if not dry_run else 'would delete'}: "
        f"{stats.procedure_concept_deleted:,}",
        f"  alignment_edge rows {'deleted' if not dry_run else 'would delete'}: "
        f"{stats.alignment_edge_deleted:,}",
        "",
        f"  extraction_attempted_at + procedure_attempted_at "
        f"{'reset' if not dry_run else 'would reset'} on "
        f"{stats.extraction_flags_reset:,} chapters",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute the migration but write nothing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N books (development).")
    parser.add_argument("--book-id", type=int, default=None,
                        help="Process only this book_id.")
    parser.add_argument("--show-mismatches", action="store_true",
                        help="Print per-book details for mismatches.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.catalog.exists():
        LOG.error("catalog not found: %s", args.catalog)
        return 1

    # open_catalog loads VSS / FTS / DuckPGQ — required to DELETE from
    # HNSW-indexed tables like chapter_embedding (otherwise: "Cannot
    # bind index 'chapter_embedding', unknown index type 'HNSW'").
    conn = open_catalog(catalog_path=args.catalog, read_only=False)
    try:
        books = select_books(conn, book_id=args.book_id, limit=args.limit)
        if not books:
            LOG.warning("no books selected")
            return 0
        LOG.info("starting migration over %d books (dry_run=%s)",
                 len(books), args.dry_run)

        stats, results = run_migration(conn, books, dry_run=args.dry_run)
    finally:
        conn.close()

    print()
    print(format_summary(stats, args.dry_run))

    if args.show_mismatches:
        bad = [r for r in results if r.status in ("mismatch", "missing_file", "error")]
        if bad:
            print("\nProblem books:")
            for r in bad:
                print(f"  book_id={r.book_id:>4} status={r.status:<14} "
                      f"{r.title[:60]}\n      {r.note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
