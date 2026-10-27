"""prune_stale_books: non-file books are never stale; --book-ids picks exact rows."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prune_stale_books as psb  # noqa: E402


def _conn(tmp_path):
    conn = duckdb.connect()
    conn.execute("CREATE TABLE book (book_id BIGINT, source_path VARCHAR)")
    conn.execute("CREATE TABLE chapter (chapter_id BIGINT, book_id BIGINT, chapter_num INT, title VARCHAR)")
    live = tmp_path / "live.epub"
    live.write_text("x")
    conn.execute("INSERT INTO book VALUES (1, ?), (2, '/gone.epub'), (3, 'pmc:PMC123')", [str(live)])
    return conn


def test_pmc_books_are_never_stale(tmp_path):
    stale, _, _ = psb.find_plan(_conn(tmp_path))
    assert [b for b, _ in stale] == [2]


def test_book_ids_selects_exactly_those(tmp_path):
    stale, twins, pairs = psb.find_plan(_conn(tmp_path), [3])
    assert [b for b, _ in stale] == [3] and twins == {} and pairs == []
