#!/usr/bin/env python3
"""
migrate_add_content_hashes.py — Add content-hash columns and backfill.

Per arch doc §6.1 (revised) — adds the columns needed for content-hash-
based incremental re-indexing of updated ePubs:

    book:     content_hash VARCHAR, last_indexed_at TIMESTAMP,
              status VARCHAR DEFAULT 'active'
    chapter:  content_hash VARCHAR

Applies the schema change additively (ALTER TABLE) so no existing data
is lost, then backfills:

    book.content_hash     = SHA-256 of the .epub file at book.source_path
                             (NULL if the file is no longer on disk)
    book.last_indexed_at  = book.indexed_at (seed value; future runs of
                             /kb-index overwrite it)
    book.status           = 'active'
    chapter.content_hash  = SHA-256 of chapter.content (NULL-safe)

Idempotent: re-runs do nothing if the columns are already present and
the hashes are already populated.

Usage:
    .venv/bin/python3 scripts/migrate_add_content_hashes.py
    .venv/bin/python3 scripts/migrate_add_content_hashes.py --catalog PATH
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("migrate_add_content_hashes")


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


def _alter_add(
    conn: duckdb.DuckDBPyConnection, table: str, column: str, ddl: str
) -> bool:
    """ALTER TABLE ... ADD COLUMN unless already present; return True if added."""
    if _column_exists(conn, table, column):
        LOG.info("  %-10s %-20s already present", table, column)
        return False
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
    LOG.info("  %-10s %-20s added", table, column)
    return True


def _backfill_book_hashes(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Hash each ePub file on disk; return (hashed, missing) counts."""
    rows = conn.execute(
        "SELECT book_id, source_path FROM book "
        "WHERE content_hash IS NULL"
    ).fetchall()
    if not rows:
        LOG.info("  book.content_hash already populated for all rows")
        return 0, 0

    hashed = 0
    missing = 0
    for i, (book_id, source_path) in enumerate(rows, 1):
        p = Path(source_path)
        if not p.exists():
            LOG.warning("  book_id=%d: file not found at %s", book_id, source_path)
            missing += 1
            continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        conn.execute(
            "UPDATE book SET content_hash = ? WHERE book_id = ?",
            [h.hexdigest(), book_id],
        )
        hashed += 1
        if i % 50 == 0:
            LOG.info("  hashed %d/%d books", i, len(rows))
    return hashed, missing


def _backfill_book_metadata(conn: duckdb.DuckDBPyConnection) -> int:
    """Seed last_indexed_at from indexed_at, and status='active' where NULL."""
    conn.execute(
        "UPDATE book SET last_indexed_at = indexed_at "
        "WHERE last_indexed_at IS NULL AND indexed_at IS NOT NULL"
    )
    conn.execute("UPDATE book SET status = 'active' WHERE status IS NULL")
    n = conn.execute(
        "SELECT COUNT(*) FROM book "
        "WHERE last_indexed_at IS NOT NULL AND status = 'active'"
    ).fetchone()[0]
    return n


def _backfill_chapter_hashes(conn: duckdb.DuckDBPyConnection) -> int:
    """Populate chapter.content_hash using sha256(content) for every chapter.

    DuckDB's `sha256()` returns a BLOB/hex depending on version; we use
    `md5()`-style helpers via Python rather than a pure-SQL update to
    stay portable. Runs in one scan-and-batch-update.
    """
    pending = conn.execute(
        "SELECT COUNT(*) FROM chapter "
        "WHERE content_hash IS NULL AND content IS NOT NULL"
    ).fetchone()[0]
    if pending == 0:
        LOG.info("  chapter.content_hash already populated")
        return 0

    LOG.info("  hashing %d chapters…", pending)
    start = time.time()
    # Stream in batches. Do NOT use OFFSET with a WHERE content_hash IS NULL
    # predicate — as the UPDATEs land, the pending set shrinks and OFFSET
    # advances past rows that haven't been processed yet. Instead, re-query
    # from the beginning each time; the WHERE clause naturally consumes the
    # queue.
    batch_size = 5000
    updated = 0
    while True:
        rows = conn.execute(
            "SELECT chapter_id, content FROM chapter "
            "WHERE content_hash IS NULL AND content IS NOT NULL "
            "ORDER BY chapter_id LIMIT ?",
            [batch_size],
        ).fetchall()
        if not rows:
            break
        updates = [
            (hashlib.sha256(content.encode("utf-8")).hexdigest(), cid)
            for cid, content in rows
        ]
        conn.executemany(
            "UPDATE chapter SET content_hash = ? WHERE chapter_id = ?",
            updates,
        )
        updated += len(updates)
        elapsed = time.time() - start
        LOG.info("  hashed %d/%d chapters (%.1fs)", updated, pending, elapsed)
    return updated


def main() -> int:
    """Apply the content-hash additive migration and backfill."""
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
        LOG.info("--- ALTER TABLE book ---")
        _alter_add(conn, "book", "content_hash", "content_hash VARCHAR")
        _alter_add(conn, "book", "last_indexed_at", "last_indexed_at TIMESTAMP")
        _alter_add(conn, "book", "status", "status VARCHAR DEFAULT 'active'")

        LOG.info("--- ALTER TABLE chapter ---")
        _alter_add(conn, "chapter", "content_hash", "content_hash VARCHAR")

        LOG.info("--- backfill book hashes ---")
        hashed, missing = _backfill_book_hashes(conn)
        LOG.info("book.content_hash: %d hashed, %d files missing", hashed, missing)

        LOG.info("--- backfill book metadata (last_indexed_at, status) ---")
        ready = _backfill_book_metadata(conn)
        LOG.info("books with last_indexed_at + status='active': %d", ready)

        LOG.info("--- backfill chapter hashes ---")
        ch_hashed = _backfill_chapter_hashes(conn)
        LOG.info("chapter.content_hash: %d hashed", ch_hashed)

        conn.commit()
        LOG.info("migration complete")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
