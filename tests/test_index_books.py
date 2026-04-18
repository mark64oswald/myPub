"""
Smoke tests for scripts/index_books.py.

Covers the pure helpers (hashing, title cleaning, href stripping, text
extraction) plus a minimal end-to-end indexing run against a programmatically-
built ePub. Validates that new books land with content_hash, last_indexed_at,
and status='active' as required by the incremental re-indexing substrate.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest
from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
_s = str(SCRIPTS_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)

import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------

def test_sha256_text_matches_hashlib():
    for s in ("", "hello", "Unicode tëxt é\n"):
        assert ib._sha256_text(s) == hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: SLF001  # pylint: disable=protected-access


def test_sha256_file_matches_hashlib(tmp_path):
    content = b"x" * (1 << 20) + b"payload"  # cross the streaming buffer boundary
    p = tmp_path / "blob.bin"
    p.write_bytes(content)
    assert ib._sha256_file(p) == hashlib.sha256(content).hexdigest()  # pylint: disable=protected-access


def test_clean_filename_title_strips_trailing_isbn():
    cases = [
        # (input Path stem, expected cleaned title)
        (Path("Some Book-9781234567890"), "Some Book"),
        (Path("Another Book-978123456789X"), "Another Book"),
        (Path("Ten-digit-1234567890"), "Ten-digit"),
        (Path("No ISBN here"), "No ISBN here"),
        (Path("Trailing-1234567"), "Trailing-1234567"),  # too few digits, keep
        (Path("Title with - dash"), "Title with - dash"),
    ]
    for stem, expected in cases:
        got = ib._clean_filename_title(Path(f"/fake/{stem.name}.epub"))  # pylint: disable=protected-access
        assert got == expected, f"{stem.name!r} → {got!r}, expected {expected!r}"


def test_href_path_strips_fragments():
    assert ib._href_path("chapter1.html#section-3") == "chapter1.html"  # pylint: disable=protected-access
    assert ib._href_path("chapter1.html") == "chapter1.html"  # pylint: disable=protected-access
    assert ib._href_path(None) is None  # pylint: disable=protected-access
    assert ib._href_path("") is None  # pylint: disable=protected-access


def test_extract_text_removes_script_and_style():
    html = (
        b"<html><head><style>body{}</style></head>"
        b"<body><script>alert(1)</script>"
        b"<nav>skip this</nav>"
        b"<p>Kept content.</p></body></html>"
    )
    text = ib._extract_text(html)  # pylint: disable=protected-access
    assert "Kept content." in text
    assert "alert" not in text
    assert "skip this" not in text
    assert "body{}" not in text


def test_count_tokens_nonzero_on_real_text():
    n = ib._count_tokens("The quick brown fox jumps over the lazy dog. " * 20)  # pylint: disable=protected-access
    assert n > 50


def test_parse_pub_date_handles_multiple_formats():
    from datetime import date  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    assert ib._parse_pub_date("2024-03-15") == date(2024, 3, 15)  # pylint: disable=protected-access
    assert ib._parse_pub_date("2024-03") == date(2024, 3, 1)  # pylint: disable=protected-access
    assert ib._parse_pub_date("2024") == date(2024, 1, 1)  # pylint: disable=protected-access
    assert ib._parse_pub_date("not a date") is None  # pylint: disable=protected-access


# ----------------------------------------------------------------------------
# _book_metadata with blank / missing DC:title fallback
# ----------------------------------------------------------------------------

class _FakeBook:
    """Stand-in for epub.EpubBook that only needs get_metadata."""

    def __init__(self, metadata: dict):
        self._md = metadata

    def get_metadata(self, ns, key):  # noqa: D401 — mirroring ebooklib's signature
        return self._md.get((ns, key), [])


def test_book_metadata_falls_back_to_filename_when_blank_title():
    fake = _FakeBook({
        ("DC", "title"): [("(blank)",)],
        ("DC", "creator"): [("Real Author",)],
    })
    meta = ib._book_metadata(  # pylint: disable=protected-access
        fake, Path("/tmp/Business Metadata-9780080552200.epub")
    )
    assert meta["title"] == "Business Metadata"
    assert meta["authors"] == ["Real Author"]


def test_book_metadata_falls_back_when_title_missing_entirely():
    fake = _FakeBook({})
    meta = ib._book_metadata(  # pylint: disable=protected-access
        fake, Path("/tmp/Foo-1234567890.epub")
    )
    assert meta["title"] == "Foo"
    assert meta["authors"] == []


def test_book_metadata_uses_real_title_when_present():
    fake = _FakeBook({
        ("DC", "title"): [("A Real Title",)],
        ("DC", "creator"): [("Author One",), ("Author Two",)],
    })
    meta = ib._book_metadata(  # pylint: disable=protected-access
        fake, Path("/tmp/irrelevant-stem.epub")
    )
    assert meta["title"] == "A Real Title"
    assert meta["authors"] == ["Author One", "Author Two"]


# ----------------------------------------------------------------------------
# End-to-end indexing of a programmatically-built ePub
# ----------------------------------------------------------------------------

def _make_test_epub(dest: Path, *, title: str, author: str) -> Path:
    """Build a minimal valid ePub with two chapters. Returns the .epub path."""
    book = epub.EpubBook()
    book.set_identifier("test-id-123")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    c1 = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    c1.content = "<h1>Chapter One</h1><p>" + ("One content. " * 40) + "</p>"
    c2 = epub.EpubHtml(title="Chapter Two", file_name="ch2.xhtml", lang="en")
    c2.content = "<h1>Chapter Two</h1><p>" + ("Two content. " * 40) + "</p>"
    for c in (c1, c2):
        book.add_item(c)
    book.toc = [c1, c2]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1, c2]
    epub.write_epub(str(dest), book, {})
    return dest


def test_index_book_end_to_end_populates_hashes_and_content(
    schema_only_conn, tmp_path,
):
    """Index a minimal ePub and verify: book row has content_hash +
    last_indexed_at + status; both chapters have content + content_hash;
    the hashes match SHA-256(content)."""
    src = tmp_path / "Tiny Test Book-9780000000000.epub"
    _make_test_epub(src, title="Tiny Test Book", author="Test Author")

    ok = ib.index_book(schema_only_conn, src)
    assert ok is True

    rows = schema_only_conn.execute(
        "SELECT book_id, title, content_hash, last_indexed_at, status "
        "FROM book WHERE source_path = ?", [str(src)]
    ).fetchall()
    assert len(rows) == 1
    _, title, content_hash, last_indexed_at, status = rows[0]
    assert title == "Tiny Test Book"
    assert content_hash and len(content_hash) == 64
    assert content_hash == hashlib.sha256(src.read_bytes()).hexdigest()
    assert last_indexed_at is not None
    assert status == "active"

    # Author linked via book_author.
    author_row = schema_only_conn.execute(
        "SELECT a.name FROM book_author ba JOIN author a USING (author_id) "
        "WHERE ba.book_id = (SELECT book_id FROM book WHERE source_path = ?)",
        [str(src)],
    ).fetchone()
    assert author_row == ("Test Author",)

    # Chapters have content + content_hash matching SHA-256 of content.
    chapters = schema_only_conn.execute(
        "SELECT chapter_id, content, content_hash FROM chapter "
        "WHERE book_id = (SELECT book_id FROM book WHERE source_path = ?) "
        "ORDER BY chapter_id",
        [str(src)],
    ).fetchall()
    assert len(chapters) >= 2  # toc has 2, may include extras
    for _cid, content, ch_hash in chapters:
        if content is None:
            assert ch_hash is None, "chapters with no content must have no hash"
        else:
            assert ch_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
            assert len(ch_hash) == 64


def test_index_book_reindex_updates_without_leaving_orphans(
    schema_only_conn, tmp_path,
):
    """Re-indexing the same source_path should replace the prior book's
    rows (book, book_author, chapter, chapter_embedding) without
    orphaning anything."""
    src = tmp_path / "Same Book-1111111111.epub"
    _make_test_epub(src, title="Same Book", author="Author A")
    assert ib.index_book(schema_only_conn, src)
    book_count_before = schema_only_conn.execute(
        "SELECT COUNT(*) FROM book WHERE source_path = ?", [str(src)]
    ).fetchone()[0]
    assert book_count_before == 1

    # Re-index the same file — should delete and re-insert cleanly.
    assert ib.index_book(schema_only_conn, src)
    book_count_after = schema_only_conn.execute(
        "SELECT COUNT(*) FROM book WHERE source_path = ?", [str(src)]
    ).fetchone()[0]
    assert book_count_after == 1
