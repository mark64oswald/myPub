"""
Tests for scripts/index_new_books.py: classification of candidate ePubs and
carry-over of chapter-sourced derived data when a book is updated or retitled.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position
import index_new_books as inb  # noqa: E402  # pylint: disable=wrong-import-position


def _make_epub(dest: Path, chapters: list[tuple[str, str]]) -> Path:
    book = epub.EpubBook()
    book.set_identifier("test-" + dest.stem)
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("A. Author")
    items = []
    for i, (title, text) in enumerate(chapters, 1):
        c = epub.EpubHtml(title=title, file_name="ch%d.xhtml" % i, lang="en")
        c.content = "<h1>%s</h1><p>%s</p>" % (title, (text + " ") * 40)
        book.add_item(c)
        items.append(c)
    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items
    epub.write_epub(str(dest), book, {})
    return dest


def _chapter_id(conn, path: Path, title: str) -> int:
    return conn.execute(
        "SELECT chapter_id FROM chapter JOIN book USING (book_id) "
        "WHERE source_path = ? AND chapter.title = ?", [str(path), title]).fetchone()[0]


def _seed_relation(conn, chapter_id: int) -> None:
    a = conn.execute("INSERT INTO concept (name, concept_type) VALUES ('Alpha', 'technique') "
                     "RETURNING concept_id").fetchone()[0]
    b = conn.execute("INSERT INTO concept (name, concept_type) VALUES ('Beta', 'technique') "
                     "RETURNING concept_id").fetchone()[0]
    conn.execute("INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
                 "source_type, source_id) VALUES (?, ?, 'REQUIRES', 'chapter', ?)", [a, b, chapter_id])
    conn.execute("UPDATE chapter SET extraction_attempted_at = CURRENT_TIMESTAMP WHERE chapter_id = ?",
                 [chapter_id])


def test_plan_classifies_new_unchanged_updated_and_retitled(schema_only_conn, tmp_path):
    same = _make_epub(tmp_path / "Same Book-0642570000001.epub", [("One", "same")])
    changed = _make_epub(tmp_path / "Changed Book-0642570000002.epub", [("One", "v1")])
    old_title = _make_epub(tmp_path / "Old Title-0642570000003.epub", [("One", "x")])
    for f in (same, changed, old_title):
        assert ib.index_book(schema_only_conn, f)

    _make_epub(changed, [("One", "v2 has more text")])          # bytes change in place
    new_title = tmp_path / "New Title-0642570000003.epub"         # O'Reilly renamed the book
    old_title.rename(new_title)
    brand_new = _make_epub(tmp_path / "Brand New-0642570000004.epub", [("One", "n")])

    work = inb.plan(schema_only_conn, [same, changed, new_title, brand_new])
    assert work["unchanged"] == [same]
    assert [f for f, _ in work["updated"]] == [changed]
    assert [f for f, _ in work["retitled"]] == [new_title]
    assert work["new"] == [brand_new]


def test_carry_over_keeps_derived_data_on_matching_chapters(schema_only_conn, tmp_path):
    src = _make_epub(tmp_path / "Evolving-0642570000010.epub",
                     [("Intro", "hello"), ("Deep Dive", "first draft")])
    assert ib.index_book(schema_only_conn, src)
    old_book = schema_only_conn.execute("SELECT book_id FROM book WHERE source_path = ?",
                                        [str(src)]).fetchone()[0]
    _seed_relation(schema_only_conn, _chapter_id(schema_only_conn, src, "Deep Dive"))

    # Early Release update: chapter text changes and a new chapter appears.
    _make_epub(src, [("Intro", "hello"), ("Deep Dive", "second draft, longer"), ("New Ch", "fresh")])
    work = inb.plan(schema_only_conn, [src])
    stats = inb.apply(schema_only_conn, work, carry_over=True)

    assert stats["updated"] == 1 and stats["chapters_carried"] == 2 and stats["chapters_dropped"] == 0
    books = schema_only_conn.execute("SELECT book_id, source_path FROM book").fetchall()
    assert len(books) == 1 and books[0][0] != old_book and books[0][1] == str(src)

    new_deep = _chapter_id(schema_only_conn, src, "Deep Dive")
    rel = schema_only_conn.execute("SELECT source_id FROM concept_relation").fetchall()
    assert rel == [(new_deep,)]
    flags = dict(schema_only_conn.execute(
        "SELECT title, extraction_attempted_at IS NOT NULL FROM chapter").fetchall())
    assert flags == {"Intro": False, "Deep Dive": True, "New Ch": False}
    # No chapters of the old book survive.
    assert schema_only_conn.execute("SELECT count(*) FROM chapter WHERE book_id = ?",
                                    [old_book]).fetchone()[0] == 0


def test_carry_over_retitled_book_replaces_old_row(schema_only_conn, tmp_path):
    old = _make_epub(tmp_path / "Skills for AI Agents-0642570000020.epub", [("Skills", "s")])
    assert ib.index_book(schema_only_conn, old)
    _seed_relation(schema_only_conn, _chapter_id(schema_only_conn, old, "Skills"))
    new = tmp_path / "Agent Skills-0642570000020.epub"
    old.rename(new)

    stats = inb.apply(schema_only_conn, inb.plan(schema_only_conn, [new]), carry_over=True)
    assert stats["retitled"] == 1
    assert schema_only_conn.execute("SELECT source_path FROM book").fetchall() == [(str(new),)]
    assert schema_only_conn.execute("SELECT source_id FROM concept_relation").fetchall() == \
        [(_chapter_id(schema_only_conn, new, "Skills"),)]


def test_duplicate_titles_match_by_occurrence(schema_only_conn, tmp_path):
    a = _make_epub(tmp_path / "Dupes-0642570000030.epub",
                   [("Summary", "a"), ("Body", "b"), ("Summary", "c")])
    b = _make_epub(tmp_path / "Dupes v2-0642570000031.epub",
                   [("Summary", "a2"), ("Body", "b2"), ("Summary", "c2")])
    assert ib.index_book(schema_only_conn, a) and ib.index_book(schema_only_conn, b)
    ids = lambda p: [c for c, in schema_only_conn.execute(  # noqa: E731
        "SELECT chapter_id FROM chapter JOIN book USING (book_id) WHERE source_path = ? "
        "ORDER BY chapter_num", [str(p)]).fetchall()]
    book = lambda p: schema_only_conn.execute(  # noqa: E731
        "SELECT book_id FROM book WHERE source_path = ?", [str(p)]).fetchone()[0]
    assert sorted(inb.chapter_pairs(schema_only_conn, book(a), book(b))) == sorted(zip(ids(a), ids(b)))


def test_default_drops_old_version_and_its_extractions(schema_only_conn, tmp_path):
    """Default mode: a new revision gets a fresh extraction, so nothing from the old one survives."""
    src = _make_epub(tmp_path / "Evolving-0642570000040.epub", [("Intro", "hello"), ("Deep Dive", "v1")])
    assert ib.index_book(schema_only_conn, src)
    old_book = schema_only_conn.execute("SELECT book_id FROM book").fetchone()[0]
    deep = _chapter_id(schema_only_conn, src, "Deep Dive")
    _seed_relation(schema_only_conn, deep)
    proc = schema_only_conn.execute(
        "INSERT INTO procedure (name, source_type, source_id) VALUES ('Do it', 'chapter', ?) "
        "RETURNING procedure_id", [deep]).fetchone()[0]
    concept = schema_only_conn.execute("SELECT min(concept_id) FROM concept").fetchone()[0]
    schema_only_conn.execute("INSERT INTO procedure_concept VALUES (?, ?)", [proc, concept])

    _make_epub(src, [("Intro", "hello"), ("Deep Dive", "v2, more current")])
    stats = inb.apply(schema_only_conn, inb.plan(schema_only_conn, [src]))

    assert stats["updated"] == 1 and stats["chapters_dropped"] == 2 and stats["chapters_carried"] == 0
    assert schema_only_conn.execute("SELECT count(*) FROM book WHERE book_id = ?", [old_book]).fetchone()[0] == 0
    for table in ("concept_relation", "procedure", "procedure_concept"):
        assert schema_only_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert schema_only_conn.execute(
        "SELECT count(*) FROM chapter WHERE extraction_attempted_at IS NOT NULL "
        "OR procedure_attempted_at IS NOT NULL").fetchone()[0] == 0
    # Shared concepts themselves stay: other books may use them.
    assert schema_only_conn.execute("SELECT count(*) FROM concept").fetchone()[0] == 2
