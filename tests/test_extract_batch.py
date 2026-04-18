"""
Smoke tests for scripts/extract_batch.py.

Exercises the chapter-selection logic (dedup-by-hash, skip-extracted,
front-matter filter, order-by, min-content-chars, per-book limits) and
the prep → process flow against a realistic fixture. These are the
paths that drive Phase 2.4 full-corpus extraction; regressions here
silently waste sub-agent invocations or miss chapters.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
for _p in (SCRIPTS_DIR, MCP_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import extract_batch as eb  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# Helpers that populate extra chapters on the realistic fixture
# ----------------------------------------------------------------------------

def _add_book(conn, title: str) -> int:
    return conn.execute(
        "INSERT INTO book (title, source_path, content_hash, last_indexed_at, status) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'active') RETURNING book_id",
        [title, f"/tmp/{title}.epub", f"hash-{title}"],
    ).fetchone()[0]


def _add_chapter(
    conn,
    book_id: int,
    *,
    title: str,
    content: str,
    content_hash: str | None = None,
    chapter_num: int | None = None,
) -> int:
    import hashlib  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    if content_hash is None:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content, content_hash) "
        "VALUES (?, ?, ?, ?, ?) RETURNING chapter_id",
        [book_id, chapter_num, title, content, content_hash],
    ).fetchone()[0]


def _add_relation(conn, src_id: int, chapter_id: int, target_id: int | None = None):
    """Mark a chapter as 'extracted' by giving it at least one concept_relation row.

    If target_id is omitted, reuse a seed concept from the realistic fixture.
    """
    if target_id is None:
        target_id = conn.execute(
            "SELECT concept_id FROM concept WHERE name = '__seed_a'"
        ).fetchone()[0]
    conn.execute(
        "INSERT INTO concept_relation "
        "(from_concept_id, to_concept_id, relation_type, confidence, "
        " source_type, source_id) VALUES (?, ?, 'REQUIRES', 0.9, 'chapter', ?)",
        [src_id, target_id, chapter_id],
    )


# ----------------------------------------------------------------------------
# _select_chapters behavior
# ----------------------------------------------------------------------------

def test_select_respects_min_content_chars(realistic_conn):
    book = _add_book(realistic_conn, "Book A")
    # Short content gets filtered out at 2000 chars threshold.
    _add_chapter(realistic_conn, book, title="Short",
                 content="x" * 100, chapter_num=1)
    long_id = _add_chapter(realistic_conn, book, title="Long",
                           content="x" * 3000, chapter_num=2)

    rows = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="book_id",
        limit=None, min_content_chars=2000,
    )
    ids = {r[0] for r in rows}
    assert long_id in ids
    assert len(rows) == 1  # short one filtered


def test_select_dedups_by_content_hash_within_book(realistic_conn):
    """TOC entries sharing the same href map to the same content_hash.
    With dedup on, only the lowest-chapter_id representative is returned."""
    book = _add_book(realistic_conn, "Book B")
    shared = "This is a substantial chapter's worth of prose. " * 60  # > 2000 chars
    ch1 = _add_chapter(realistic_conn, book, title="Sec 1.1",
                      content=shared, chapter_num=1)
    _add_chapter(realistic_conn, book, title="Sec 1.2",
                 content=shared, chapter_num=2)
    _add_chapter(realistic_conn, book, title="Sec 1.3",
                 content=shared, chapter_num=3)

    rows = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="book_id",
        limit=None, min_content_chars=500,
    )
    assert [r[0] for r in rows] == [ch1]


def test_select_skip_extracted_excludes_siblings(realistic_conn):
    """If any sibling of a content_hash has been extracted already, skip
    the whole hash — no point re-extracting the same content."""
    book = _add_book(realistic_conn, "Book C")
    shared = "Repeated content across TOC entries. " * 60
    ch1 = _add_chapter(realistic_conn, book, title="Sec 1.1",
                      content=shared, chapter_num=1)
    ch2 = _add_chapter(realistic_conn, book, title="Sec 1.2",
                      content=shared, chapter_num=2)
    # Mark ch1 as already extracted.
    _add_relation(realistic_conn, src_id=ch1, chapter_id=ch1)

    rows = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="book_id",
        limit=None, min_content_chars=500,
    )
    ids = {r[0] for r in rows}
    assert ch1 not in ids
    assert ch2 not in ids  # sibling is skipped too


def test_select_front_matter_filter_excludes_common_titles(realistic_conn):
    book = _add_book(realistic_conn, "Book D")
    long_content = "x" * 3000
    for title in ("copyright", "preface", "Foreword", "ACKNOWLEDGMENTS",
                  "about this book", "index"):
        _add_chapter(realistic_conn, book, title=title,
                     content=long_content + title, chapter_num=None)
    keeper = _add_chapter(realistic_conn, book,
                          title="1. Real content",
                          content=long_content + "real", chapter_num=10)

    rows = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="book_id",
        limit=None, min_content_chars=500,
    )
    assert [r[0] for r in rows] == [keeper]


def test_select_order_by_title_vs_book_id(realistic_conn):
    """order_by='title' puts alphabetically-first book first, even if its
    book_id is higher. order_by='book_id' preserves insertion order."""
    # Insert "Z Book" first so it gets a lower book_id than "A Book".
    z_book = _add_book(realistic_conn, "Z Book")
    a_book = _add_book(realistic_conn, "A Book")
    for bid in (z_book, a_book):
        _add_chapter(realistic_conn, bid, title="ch", content="x" * 3000)

    by_id = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[z_book, a_book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="book_id",
        limit=None, min_content_chars=500,
    )
    by_title = eb._select_chapters(  # pylint: disable=protected-access
        realistic_conn,
        book_ids=[z_book, a_book], chapter_ids=None,
        skip_extracted=True, dedup_by_hash=True, order_by="title",
        limit=None, min_content_chars=500,
    )

    # by_id: lower book_id first (Z Book was inserted first).
    assert by_id[0][1] == z_book
    # by_title: 'A Book' comes before 'Z Book' alphabetically.
    assert by_title[0][1] == a_book


def test_select_rejects_unknown_order_by(realistic_conn):
    with pytest.raises(ValueError, match="order_by"):
        eb._select_chapters(  # pylint: disable=protected-access
            realistic_conn,
            book_ids=None, chapter_ids=None,
            skip_extracted=True, dedup_by_hash=True, order_by="random",
            limit=None, min_content_chars=500,
        )


# ----------------------------------------------------------------------------
# prep + process round-trip
# ----------------------------------------------------------------------------

def test_prep_writes_prompts_and_manifest(realistic_conn, tmp_path, seed_ids):
    book = _add_book(realistic_conn, "Round Trip Book")
    ch_a = _add_chapter(realistic_conn, book, title="Chapter A",
                       content="Alpha content. " * 200, chapter_num=1)
    ch_b = _add_chapter(realistic_conn, book, title="Chapter B",
                       content="Beta content. " * 200, chapter_num=2)

    args = types.SimpleNamespace(
        books=[book],
        chapter_ids=None,
        skip_extracted=True,
        dedup_by_hash=True,
        order_by="book_id",
        limit=None,
        per_batch=1,
        min_content_chars=500,
        output_dir=tmp_path / "session",
    )
    rc = eb.do_prep(realistic_conn, args)
    assert rc == 0

    manifest_path = tmp_path / "session" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    chapter_ids = {c["chapter_id"] for c in manifest["chapters"]}
    assert chapter_ids == {ch_a, ch_b}
    assert len(manifest["batches"]) == 2  # per_batch=1
    # Every chapter should have a prompt file that actually contains the
    # chapter title + content.
    for ch in manifest["chapters"]:
        p = Path(ch["prompt_path"])
        assert p.exists()
        body = p.read_text()
        assert (ch["chapter_title"] or "") in body


def test_process_ingests_result_jsons(realistic_conn, tmp_path, seed_ids, embedder):
    book = _add_book(realistic_conn, "Process Book")
    ch = _add_chapter(realistic_conn, book, title="Content",
                     content="Substantive content. " * 200, chapter_num=1)

    # prep
    args = types.SimpleNamespace(
        books=[book], chapter_ids=None, skip_extracted=True, dedup_by_hash=True,
        order_by="book_id", limit=None, per_batch=5, min_content_chars=500,
        output_dir=tmp_path / "sess",
    )
    eb.do_prep(realistic_conn, args)

    # Write a synthetic LLM result for this chapter.
    result_path = tmp_path / "sess" / "results" / f"result_{ch}.json"
    result_path.write_text(json.dumps({
        "entities": [
            {"name": "Concept X", "type": "Concept", "description": "desc X"},
            {"name": "Concept Y", "type": "Concept", "description": "desc Y"},
        ],
        "relations": [
            {"from": "Concept X", "to": "Concept Y", "type": "REQUIRES",
             "confidence": 0.9},
        ],
    }))

    # process
    proc_args = types.SimpleNamespace(
        manifest=None,
        output_dir=tmp_path / "sess",
    )
    rc = eb.do_process(realistic_conn, proc_args)
    assert rc == 0

    # Verify the relation landed.
    assert realistic_conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE source_type='chapter' AND source_id = ?",
        [ch],
    ).fetchone()[0] == 1


def test_process_reports_missing_results_without_crashing(
    realistic_conn, tmp_path,
):
    """If sub-agents haven't produced result files yet, `process` should log
    them as pending and keep going — safe to re-run mid-wave."""
    book = _add_book(realistic_conn, "Partial Book")
    _add_chapter(realistic_conn, book, title="A",
                content="A." * 2000, chapter_num=1)
    _add_chapter(realistic_conn, book, title="B",
                content="B." * 2000, chapter_num=2)

    args = types.SimpleNamespace(
        books=[book], chapter_ids=None, skip_extracted=True, dedup_by_hash=True,
        order_by="book_id", limit=None, per_batch=2, min_content_chars=500,
        output_dir=tmp_path / "sess",
    )
    eb.do_prep(realistic_conn, args)

    # No result files written.
    proc_args = types.SimpleNamespace(
        manifest=None, output_dir=tmp_path / "sess",
    )
    rc = eb.do_process(realistic_conn, proc_args)
    # rc == 1 when nothing was processed; that's fine, the point is "no crash".
    assert rc in (0, 1)


# ----------------------------------------------------------------------------
# status
# ----------------------------------------------------------------------------

def test_status_reports_per_book_progress(realistic_conn, capsys):
    book = _add_book(realistic_conn, "Status Book")
    ch1 = _add_chapter(realistic_conn, book, title="a",
                       content="a" * 3000, chapter_num=1)
    _add_chapter(realistic_conn, book, title="b",
                content="b" * 3000, chapter_num=2)
    # Mark ch1 extracted.
    _add_relation(realistic_conn, src_id=ch1, chapter_id=ch1)

    args = types.SimpleNamespace(books=[book], verbose=True)
    rc = eb.do_status(realistic_conn, args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1/2" in out  # 1 of 2 chapters done
    assert "Status Book" in out
