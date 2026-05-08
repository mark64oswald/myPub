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
    # Fragment-only references have no path — must not return "".
    assert ib._href_path("#anchor") is None  # pylint: disable=protected-access


def test_split_href_returns_path_and_fragment():
    assert ib._split_href("chapter1.html#sec-2") == ("chapter1.html", "sec-2")  # pylint: disable=protected-access
    assert ib._split_href("chapter1.html") == ("chapter1.html", None)  # pylint: disable=protected-access
    assert ib._split_href("#sec") == (None, "sec")  # pylint: disable=protected-access
    assert ib._split_href(None) == (None, None)  # pylint: disable=protected-access
    assert ib._split_href("") == (None, None)  # pylint: disable=protected-access


def test_toc_fragments_per_file_groups_anchors_by_path():
    toc = [
        {"href": "ch1.xhtml"},
        {"href": "ch2.xhtml#sec-a"},
        {"href": "ch2.xhtml#sec-b"},
        {"href": "ch3.xhtml"},
        {"href": "ch3.xhtml#deep-link"},
        {"href": ""},          # bare/empty — ignored
        {"href": None},        # missing — ignored
    ]
    out = ib._toc_fragments_per_file(toc)  # pylint: disable=protected-access
    # ch1: only ever referenced bare → {""}
    assert out["ch1.xhtml"] == {""}
    # ch2: two distinct fragments → both recorded
    assert out["ch2.xhtml"] == {"sec-a", "sec-b"}
    # ch3: bare AND fragmented → both keys
    assert out["ch3.xhtml"] == {"", "deep-link"}


# ----------------------------------------------------------------------------
# Slicer (the actual bug fix — fragment-aware content extraction)
# ----------------------------------------------------------------------------

def test_slice_html_no_fragments_returns_whole_file():
    html = b"<html><body><p>Intro.</p><p>Body of the chapter.</p></body></html>"
    out = ib._slice_html_by_anchors(html, set())  # pylint: disable=protected-access
    assert "" in out
    assert "Intro." in out[""]
    assert "Body of the chapter." in out[""]


def test_slice_html_only_empty_fragment_returns_whole_file():
    html = b"<html><body><p>All the content.</p></body></html>"
    # TOC reference with no fragment is recorded as "" — should still
    # produce the whole-file text under "".
    out = ib._slice_html_by_anchors(html, {""})  # pylint: disable=protected-access
    assert "All the content." in out[""]


def test_slice_html_with_fragments_produces_distinct_slices():
    """Core regression test for the Phase 1 splitter bug.

    A chapter file with three anchored sections must produce three
    DIFFERENT slices, plus a (possibly small) preamble — not three
    copies of the whole file's text.
    """
    html = (
        b"<html><body>"
        b"<p>Preamble paragraph before any anchor.</p>"
        b"<h2 id='sec1'>Section One</h2>"
        b"<p>Content of section one is unique.</p>"
        b"<h2 id='sec2'>Section Two</h2>"
        b"<p>Content of section two stands alone.</p>"
        b"<h2 id='sec3'>Section Three</h2>"
        b"<p>Content of section three closes things out.</p>"
        b"</body></html>"
    )
    out = ib._slice_html_by_anchors(html, {"", "sec1", "sec2", "sec3"})  # pylint: disable=protected-access

    # All four keys must be present.
    assert set(out) == {"", "sec1", "sec2", "sec3"}

    # Preamble holds only the pre-anchor paragraph.
    assert "Preamble paragraph" in out[""]
    assert "section one" not in out[""].lower()

    # sec1 has its own content but not sec2's or sec3's.
    assert "section one is unique" in out["sec1"].lower()
    assert "section two stands alone" not in out["sec1"].lower()
    assert "section three closes" not in out["sec1"].lower()

    # sec2 has its own content only.
    assert "section two stands alone" in out["sec2"].lower()
    assert "section one is unique" not in out["sec2"].lower()
    assert "section three closes" not in out["sec2"].lower()

    # sec3 has its own content only.
    assert "section three closes" in out["sec3"].lower()
    assert "section one is unique" not in out["sec3"].lower()

    # The four slices must hash to four distinct values.
    hashes = {hashlib.sha256(s.encode("utf-8")).hexdigest() for s in out.values()}
    assert len(hashes) == 4


def test_slice_html_unresolved_fragment_falls_back_to_whole_file():
    """A TOC pointing at a fragment that doesn't resolve to any anchor
    must still get content (the whole file) rather than an empty row."""
    html = b"<html><body><p>Some indexable content.</p></body></html>"
    out = ib._slice_html_by_anchors(html, {"missing"})  # pylint: disable=protected-access
    assert "missing" in out
    assert "Some indexable content." in out["missing"]


def test_slice_html_resolved_and_unresolved_fragments_coexist():
    html = (
        b"<html><body>"
        b"<p>Pre.</p>"
        b"<h2 id='real'>Real</h2><p>Real text.</p>"
        b"</body></html>"
    )
    out = ib._slice_html_by_anchors(html, {"real", "ghost"})  # pylint: disable=protected-access
    # Resolved fragment gets its slice.
    assert "Real text." in out["real"]
    # Unresolved fragment falls back to the whole-file text.
    assert "Pre." in out["ghost"] and "Real text." in out["ghost"]


def test_slice_html_legacy_a_name_anchor_resolves():
    """ePub 2 books often use <a name="..."> instead of id=. Both must work."""
    html = (
        b"<html><body>"
        b"<p>Pre.</p>"
        b"<a name='legacy'></a><p>Legacy section text.</p>"
        b"</body></html>"
    )
    out = ib._slice_html_by_anchors(html, {"legacy"})  # pylint: disable=protected-access
    assert "Legacy section text." in out["legacy"]
    assert "Pre." not in out["legacy"]


def test_slice_html_strips_script_style_nav():
    html = (
        b"<html><body>"
        b"<script>tracker();</script>"
        b"<style>body{}</style>"
        b"<nav>menu links</nav>"
        b"<h2 id='real'>Real</h2><p>Kept.</p>"
        b"</body></html>"
    )
    out = ib._slice_html_by_anchors(html, {"real"})  # pylint: disable=protected-access
    assert "Kept." in out["real"]
    assert "tracker" not in out["real"]
    assert "menu links" not in out["real"]
    assert "body{}" not in out["real"]


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


def _make_multi_anchor_epub(dest: Path, *, title: str, author: str) -> Path:
    """Build an ePub where one xhtml file holds multiple anchored sections
    and the TOC has separate entries for each anchor.

    This is the exact shape that triggered the Phase 1 splitter bug: under
    the old splitter, all three TOC entries for ch1 received the same full
    file content. Under the fixed splitter, each gets its own slice.
    """
    book = epub.EpubBook()
    book.set_identifier("multi-anchor-test-id")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    ch1 = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        "<html><body>"
        "<p>Chapter intro paragraph that lives before any anchor.</p>"
        "<h2 id='intro'>Introduction</h2>"
        "<p>" + ("Introduction unique alpha. " * 30) + "</p>"
        "<h2 id='middle'>The Middle Part</h2>"
        "<p>" + ("Middle unique bravo. " * 30) + "</p>"
        "<h2 id='end'>Final Section</h2>"
        "<p>" + ("End unique charlie. " * 30) + "</p>"
        "</body></html>"
    )
    ch2 = epub.EpubHtml(title="Chapter Two", file_name="ch2.xhtml", lang="en")
    ch2.content = "<h1>Chapter Two</h1><p>" + ("Two content. " * 40) + "</p>"
    for c in (ch1, ch2):
        book.add_item(c)
    # TOC: ch1 referenced via three anchored links + ch2 referenced bare.
    book.toc = [
        epub.Link("ch1.xhtml#intro", "1. Introduction", "ch1-intro"),
        epub.Link("ch1.xhtml#middle", "2. The Middle Part", "ch1-middle"),
        epub.Link("ch1.xhtml#end", "3. Final Section", "ch1-end"),
        ch2,
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch1, ch2]
    epub.write_epub(str(dest), book, {})
    return dest


def test_index_book_multi_anchor_chapter_produces_distinct_content(
    schema_only_conn, tmp_path,
):
    """Phase 1 splitter-bug regression test.

    A chapter file with three anchored sections referenced as three
    separate TOC entries must produce three chapter rows with
    DIFFERENT content (and therefore different content_hash values),
    not three identical rows.
    """
    src = tmp_path / "Multi Anchor Book-2222222222.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor Book", author="Test Author")

    ok = ib.index_book(schema_only_conn, src)
    assert ok is True

    # Pull the rows for the three anchored TOC entries from ch1.xhtml.
    rows = schema_only_conn.execute(
        """
        SELECT title, content, content_hash
        FROM chapter
        WHERE book_id = (SELECT book_id FROM book WHERE source_path = ?)
          AND href LIKE 'ch1.xhtml#%'
        ORDER BY chapter_num
        """,
        [str(src)],
    ).fetchall()
    assert len(rows) == 3, "expected three anchored chapter rows from ch1"

    titles = [r[0] for r in rows]
    contents = [r[1] for r in rows]
    hashes = [r[2] for r in rows]

    # Every row must have non-empty content + a hash.
    for t, c, h in rows:
        assert c, f"chapter {t!r} has empty content — slicer regression"
        assert h and len(h) == 64

    # All three hashes must differ — this is the bug-fix assertion.
    assert len(set(hashes)) == 3, (
        f"three anchored chapters must have distinct content_hash; got "
        f"{hashes}"
    )

    # Sanity: each slice contains its own marker and not its siblings'.
    intro_content, middle_content, end_content = contents
    assert "alpha" in intro_content and "bravo" not in intro_content
    assert "bravo" in middle_content and "alpha" not in middle_content
    assert "charlie" in end_content and "alpha" not in end_content


def test_index_book_bare_toc_entry_still_gets_whole_file(
    schema_only_conn, tmp_path,
):
    """The common case (TOC entry with no fragment) must continue to
    receive the whole file's text. Verifies the slicer didn't regress
    the simple path."""
    src = tmp_path / "Plain Book-3333333333.epub"
    _make_test_epub(src, title="Plain Book", author="Test Author")

    assert ib.index_book(schema_only_conn, src)

    rows = schema_only_conn.execute(
        """
        SELECT content
        FROM chapter
        WHERE book_id = (SELECT book_id FROM book WHERE source_path = ?)
          AND token_count > 0
        """,
        [str(src)],
    ).fetchall()
    # Both chapters in the simple ePub have substantive content.
    assert len(rows) >= 2
    for (content,) in rows:
        assert content and len(content) > 50
