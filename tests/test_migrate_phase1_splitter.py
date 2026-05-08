"""
Tests for scripts/migrate_phase1_splitter.py.

Verifies the in-place re-split:
  * preserves chapter_id (FK references survive)
  * UPDATEs content/content_hash/token_count for chapters whose new
    sliced content differs from the bug-bloated original
  * DELETEs stale derived rows (chapter_embedding, concept_relation,
    procedure, procedure_concept, alignment_edge) for changed
    chapters
  * resets extraction_attempted_at and procedure_attempted_at on
    changed chapters
  * is idempotent (a second run produces no further changes)
  * skips books with TOC mismatch (file modified on disk) without
    corrupting the catalog
  * dry-run mode writes nothing
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position
import migrate_phase1_splitter as mig  # noqa: E402  # pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# Test ePub: a chapter file with three anchored sections referenced as
# three separate TOC entries — the exact shape that triggered the bug.
# ---------------------------------------------------------------------------

def _make_multi_anchor_epub(dest: Path, *, title: str, author: str) -> Path:
    book = epub.EpubBook()
    book.set_identifier("test-multi-anchor")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    ch1 = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        "<html><body>"
        "<p>Chapter intro paragraph.</p>"
        "<h2 id='intro'>Introduction</h2>"
        "<p>" + ("Intro content alpha. " * 30) + "</p>"
        "<h2 id='middle'>The Middle</h2>"
        "<p>" + ("Middle content bravo. " * 30) + "</p>"
        "<h2 id='end'>Final Section</h2>"
        "<p>" + ("End content charlie. " * 30) + "</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.toc = [
        epub.Link("ch1.xhtml#intro", "1. Introduction", "ch1-intro"),
        epub.Link("ch1.xhtml#middle", "2. The Middle", "ch1-middle"),
        epub.Link("ch1.xhtml#end", "3. Final Section", "ch1-end"),
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch1]
    epub.write_epub(str(dest), book, {})
    return dest


def _seed_buggy_catalog(conn, src: Path) -> int:
    """Index the multi-anchor ePub, then *manually* corrupt the
    chapter rows to simulate what the buggy splitter would have
    produced (all three rows with identical full-file content).

    Returns the book_id.
    """
    assert ib.index_book(conn, src)

    book_id = conn.execute(
        "SELECT book_id FROM book WHERE source_path = ?", [str(src)]
    ).fetchone()[0]

    # Inject the bug: overwrite all three chapters with the same
    # whole-file text + identical content_hash, mimicking the old
    # splitter's behavior.
    rows = conn.execute(
        "SELECT chapter_id FROM chapter "
        "WHERE book_id = ? AND token_count > 0 ORDER BY chapter_num",
        [book_id],
    ).fetchall()
    assert len(rows) == 3, "test fixture expects three TOC entries"

    bloated = (
        "Chapter intro paragraph.\nIntroduction\n"
        + ("Intro content alpha. " * 30) + "\n"
        + "The Middle\n"
        + ("Middle content bravo. " * 30) + "\n"
        + "Final Section\n"
        + ("End content charlie. " * 30)
    )
    bloated_hash = ib._sha256_text(bloated)  # pylint: disable=protected-access
    for (cid,) in rows:
        conn.execute(
            "UPDATE chapter SET content = ?, content_hash = ?, "
            "token_count = ? WHERE chapter_id = ?",
            [bloated, bloated_hash, ib._count_tokens(bloated), cid],  # pylint: disable=protected-access
        )
    return book_id


# ---------------------------------------------------------------------------
# Core migration behavior
# ---------------------------------------------------------------------------

def test_resplit_book_updates_in_place_and_preserves_chapter_ids(
    realistic_conn, tmp_path,
):
    """The migration must UPDATE chapter rows in place — chapter_ids
    must be the same before and after, and content must change."""
    src = tmp_path / "Multi Anchor-1.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)

    ids_and_hashes_before = realistic_conn.execute(
        "SELECT chapter_id, content_hash FROM chapter "
        "WHERE book_id = ? ORDER BY chapter_num",
        [book_id],
    ).fetchall()

    result = mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor", src, dry_run=False,
    )

    assert result.status == "ok"
    assert result.rows == 3
    # All three rows changed — the bug had them identical, the fix
    # makes them distinct.
    assert len(result.changed_chapter_ids) == 3
    assert result.unchanged == 0

    ids_and_hashes_after = realistic_conn.execute(
        "SELECT chapter_id, content_hash FROM chapter "
        "WHERE book_id = ? ORDER BY chapter_num",
        [book_id],
    ).fetchall()

    # chapter_ids preserved exactly.
    assert [c for c, _ in ids_and_hashes_before] == [
        c for c, _ in ids_and_hashes_after
    ]
    # content_hashes all changed AND are now mutually distinct.
    new_hashes = [h for _, h in ids_and_hashes_after]
    old_hashes = [h for _, h in ids_and_hashes_before]
    assert all(n != o for n, o in zip(new_hashes, old_hashes))
    assert len(set(new_hashes)) == 3


def test_resplit_book_dry_run_writes_nothing(realistic_conn, tmp_path):
    src = tmp_path / "Multi Anchor-2.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor 2", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)

    hashes_before = realistic_conn.execute(
        "SELECT content_hash FROM chapter WHERE book_id = ?", [book_id]
    ).fetchall()

    result = mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor 2", src, dry_run=True,
    )

    # Dry-run still reports what *would* change.
    assert result.status == "ok"
    assert len(result.changed_chapter_ids) == 3

    # But nothing was written.
    hashes_after = realistic_conn.execute(
        "SELECT content_hash FROM chapter WHERE book_id = ?", [book_id]
    ).fetchall()
    assert hashes_before == hashes_after


def test_resplit_book_idempotent_on_clean_catalog(realistic_conn, tmp_path):
    """A second run after the first must produce zero changes — the
    catalog already has the correct sliced content."""
    src = tmp_path / "Multi Anchor-3.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor 3", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)

    # First run: fixes everything.
    first = mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor 3", src, dry_run=False,
    )
    assert len(first.changed_chapter_ids) == 3

    # Second run: should be a no-op.
    second = mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor 3", src, dry_run=False,
    )
    assert second.status == "ok"
    assert len(second.changed_chapter_ids) == 0
    assert second.unchanged == 3


def test_resplit_book_resets_extraction_flags_for_changed_rows(
    realistic_conn, tmp_path,
):
    """Changed chapters must have extraction_attempted_at and
    procedure_attempted_at set back to NULL so the prep scripts
    re-process them."""
    src = tmp_path / "Multi Anchor-4.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor 4", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)

    # Pretend extraction had already run — set both flags non-NULL.
    realistic_conn.execute(
        "UPDATE chapter SET extraction_attempted_at = CURRENT_TIMESTAMP, "
        "procedure_attempted_at = CURRENT_TIMESTAMP WHERE book_id = ?",
        [book_id],
    )

    mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor 4", src, dry_run=False,
    )

    # All three changed chapters now have NULL flags.
    flags = realistic_conn.execute(
        "SELECT extraction_attempted_at, procedure_attempted_at FROM chapter "
        "WHERE book_id = ?",
        [book_id],
    ).fetchall()
    for ext_at, proc_at in flags:
        assert ext_at is None
        assert proc_at is None


def test_resplit_book_missing_source_returns_missing_file(
    realistic_conn, tmp_path,
):
    src = tmp_path / "Gone-5.epub"
    _make_multi_anchor_epub(src, title="Gone", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)
    src.unlink()  # simulate file removed from disk

    result = mig.resplit_book(
        realistic_conn, book_id, "Gone", src, dry_run=False,
    )
    assert result.status == "missing_file"
    assert "not on disk" in result.note


def test_resplit_book_toc_count_mismatch_skips_safely(
    realistic_conn, tmp_path,
):
    """If the existing chapter row count differs from the fresh TOC,
    we skip the book and DON'T write anything."""
    src = tmp_path / "Mismatch-6.epub"
    _make_multi_anchor_epub(src, title="Mismatch", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)

    # Inject a phantom 4th chapter row to break the row-count match.
    realistic_conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, href, "
        "content, content_hash, token_count) "
        "VALUES (?, 99, 'Phantom', 'ghost.xhtml', 'p', 'h', 1)",
        [book_id],
    )

    hashes_before = realistic_conn.execute(
        "SELECT content_hash FROM chapter WHERE book_id = ? ORDER BY chapter_num",
        [book_id],
    ).fetchall()

    result = mig.resplit_book(
        realistic_conn, book_id, "Mismatch", src, dry_run=False,
    )
    assert result.status == "mismatch"
    assert "existing rows" in result.note

    hashes_after = realistic_conn.execute(
        "SELECT content_hash FROM chapter WHERE book_id = ? ORDER BY chapter_num",
        [book_id],
    ).fetchall()
    # Nothing changed.
    assert hashes_before == hashes_after


# ---------------------------------------------------------------------------
# Downstream invalidation
# ---------------------------------------------------------------------------

def _seed_derived_rows(conn, book_id: int) -> dict[str, list[int]]:
    """Plant a chapter_embedding, a concept_relation, a procedure,
    a procedure_concept, and an alignment_edge for each chapter of
    the book so we can verify the migration deletes them.

    All surrogate keys are sequence-generated and captured via
    RETURNING so the fixture is robust against any prior sequence
    advancement done by index_book.
    """
    chapter_ids = [
        cid for (cid,) in conn.execute(
            "SELECT chapter_id FROM chapter WHERE book_id = ? "
            "ORDER BY chapter_num",
            [book_id],
        ).fetchall()
    ]
    # Two concepts — used by concept_relation and procedure_concept.
    test_concept_id = conn.execute(
        "INSERT INTO concept (name, concept_type, description, domain) "
        "VALUES ('TestConcept', 'Concept', 'desc', 'test') "
        "RETURNING concept_id"
    ).fetchone()[0]
    other_concept_id = conn.execute(
        "INSERT INTO concept (name, concept_type, description, domain) "
        "VALUES ('OtherConcept', 'Concept', 'desc', 'test') "
        "RETURNING concept_id"
    ).fetchone()[0]
    doc_source_id = conn.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
        "authority_score) "
        "VALUES ('TestDoc', 'context7', 'context7', '/test', 0.6) "
        "RETURNING doc_source_id"
    ).fetchone()[0]
    snapshot_id = conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content) "
        "VALUES (?, 'context7', 'http://x', 'body') "
        "RETURNING snapshot_id",
        [doc_source_id],
    ).fetchone()[0]
    doc_section_id = conn.execute(
        "INSERT INTO doc_section (snapshot_id, heading_level, heading_text, "
        "ordinal, content) VALUES (?, 1, 'h', 0, 'doc body') "
        "RETURNING doc_section_id",
        [snapshot_id],
    ).fetchone()[0]

    derived: dict[str, list[int]] = {
        "chapter_embedding": [],
        "concept_relation": [],
        "procedure": [],
        "procedure_concept": [],
        "alignment_edge": [],
    }

    for i, cid in enumerate(chapter_ids):
        embedding_str = "[" + ",".join(["0.0"] * 384) + "]"
        conn.execute(
            f"INSERT INTO chapter_embedding (chapter_id, embedding) "
            f"VALUES ({cid}, {embedding_str}::FLOAT[384])"
        )
        derived["chapter_embedding"].append(cid)

        conn.execute(
            "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
            "relation_type, source_type, source_id) "
            "VALUES (?, ?, 'REQUIRES', 'chapter', ?)",
            [test_concept_id, other_concept_id, cid],
        )
        derived["concept_relation"].append(cid)

        proc_id = conn.execute(
            "INSERT INTO procedure (name, source_type, source_id) "
            "VALUES (?, 'chapter', ?) RETURNING procedure_id",
            [f"Proc {i}", cid],
        ).fetchone()[0]
        derived["procedure"].append(proc_id)
        conn.execute(
            "INSERT INTO procedure_concept (procedure_id, concept_id) "
            "VALUES (?, ?)",
            [proc_id, test_concept_id],
        )
        derived["procedure_concept"].append(proc_id)

        edge_id = conn.execute(
            "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, "
            "concept_id, relation_type, confidence) "
            "VALUES (?, ?, ?, 'CORROBORATES', 0.8) "
            "RETURNING alignment_edge_id",
            [doc_section_id, cid, test_concept_id],
        ).fetchone()[0]
        derived["alignment_edge"].append(edge_id)

    return derived


def test_invalidate_derived_rows_deletes_all_dependents(
    realistic_conn, tmp_path,
):
    """All five derived row types must be deleted when we re-split a
    book whose chapters had embeddings, relations, procedures, and
    alignment edges. Assertions are scoped to this test's book's
    chapter_ids — the realistic_conn fixture seeds an unrelated
    seed_book / seed_chapter / concept_embedding that we leave alone."""
    src = tmp_path / "Multi Anchor-7.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor 7", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)
    derived = _seed_derived_rows(realistic_conn, book_id)

    # Sanity: derived rows actually exist for this book.
    assert len(derived["chapter_embedding"]) == 3

    # Capture the chapter_ids so we can scope our assertions even
    # after the migration runs.
    test_chapter_ids = [
        cid for (cid,) in realistic_conn.execute(
            "SELECT chapter_id FROM chapter WHERE book_id = ?", [book_id]
        ).fetchall()
    ]

    # Run without an explicit transaction — invalidate_derived_rows
    # depends on auto-commit between procedure_concept and procedure
    # DELETEs to dodge the DuckDB 1.5.0 FK-enforcement bug pinned in
    # tests/test_duckdb_fk_bugs.py.
    result = mig.resplit_book(
        realistic_conn, book_id, "Multi Anchor 7", src, dry_run=False,
    )
    deletions = mig.invalidate_derived_rows(
        realistic_conn, result.changed_chapter_ids, dry_run=False,
    )

    # Five derived counts each match what we seeded (3 per chapter * 3 chapters).
    assert deletions["chapter_embedding"] == 3
    assert deletions["concept_relation"] == 3
    assert deletions["procedure"] == 3
    assert deletions["procedure_concept"] == 3
    assert deletions["alignment_edge"] == 3

    # And they're actually gone *for this book's chapters*.
    placeholder = ",".join("?" * len(test_chapter_ids))
    for table, where in [
        ("chapter_embedding",
         f"chapter_id IN ({placeholder})"),
        ("concept_relation",
         f"source_type = 'chapter' AND source_id IN ({placeholder})"),
        ("procedure",
         f"source_type = 'chapter' AND source_id IN ({placeholder})"),
        ("alignment_edge",
         f"to_chapter_id IN ({placeholder})"),
    ]:
        actual = realistic_conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}",
            test_chapter_ids,
        ).fetchone()[0]
        assert actual == 0, f"{table} still has {actual} rows for test book"


def test_invalidate_dry_run_returns_counts_without_deleting(
    realistic_conn, tmp_path,
):
    src = tmp_path / "Multi Anchor-8.epub"
    _make_multi_anchor_epub(src, title="Multi Anchor 8", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)
    _seed_derived_rows(realistic_conn, book_id)

    chapter_ids = [
        cid for (cid,) in realistic_conn.execute(
            "SELECT chapter_id FROM chapter WHERE book_id = ?",
            [book_id],
        ).fetchall()
    ]

    realistic_conn.execute("BEGIN")
    deletions = mig.invalidate_derived_rows(
        realistic_conn, chapter_ids, dry_run=True,
    )
    # ROLLBACK so the temp table goes away cleanly.
    realistic_conn.execute("ROLLBACK")

    # Dry-run counted 3 of each but didn't write.
    assert deletions["chapter_embedding"] == 3
    assert deletions["alignment_edge"] == 3

    # Scoped check: this book's chapters still have all their seeded rows.
    placeholder = ",".join("?" * len(chapter_ids))
    embed_for_book = realistic_conn.execute(
        f"SELECT COUNT(*) FROM chapter_embedding "
        f"WHERE chapter_id IN ({placeholder})",
        chapter_ids,
    ).fetchone()[0]
    edges_for_book = realistic_conn.execute(
        f"SELECT COUNT(*) FROM alignment_edge "
        f"WHERE to_chapter_id IN ({placeholder})",
        chapter_ids,
    ).fetchone()[0]
    assert embed_for_book == 3
    assert edges_for_book == 3


def test_invalidate_with_no_changed_ids_is_noop(realistic_conn):
    """When nothing changed, invalidate must skip SQL and return zeros."""
    deletions = mig.invalidate_derived_rows(
        realistic_conn, [], dry_run=False,
    )
    assert all(v == 0 for v in deletions.values())


# ---------------------------------------------------------------------------
# End-to-end: run_migration over a small book set
# ---------------------------------------------------------------------------

def test_run_migration_commits_on_success(realistic_conn, tmp_path):
    src = tmp_path / "Multi Anchor-E2E.epub"
    _make_multi_anchor_epub(src, title="E2E", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)
    _seed_derived_rows(realistic_conn, book_id)

    chapter_ids = [
        cid for (cid,) in realistic_conn.execute(
            "SELECT chapter_id FROM chapter WHERE book_id = ?", [book_id]
        ).fetchall()
    ]

    # select_books picks up BOTH the realistic_conn fixture's seed
    # book and our test book. The seed book's source_path
    # ('/tmp/seed.epub') won't exist, so it'll be skipped as
    # missing_file — that's harmless for this test.
    books = mig.select_books(realistic_conn)
    stats, _ = mig.run_migration(realistic_conn, books, dry_run=False)

    # Our book succeeded; stats include both books' processing.
    assert stats.books_ok == 1
    assert stats.chapters_changed == 3
    assert stats.chapter_embedding_deleted == 3
    assert stats.alignment_edge_deleted == 3
    assert stats.procedure_deleted == 3

    # Scoped check: our book's chapter_embeddings are gone.
    placeholder = ",".join("?" * len(chapter_ids))
    embed_count = realistic_conn.execute(
        f"SELECT COUNT(*) FROM chapter_embedding "
        f"WHERE chapter_id IN ({placeholder})",
        chapter_ids,
    ).fetchone()[0]
    assert embed_count == 0


def test_run_migration_dry_run_rolls_back(realistic_conn, tmp_path):
    src = tmp_path / "Multi Anchor-DRY.epub"
    _make_multi_anchor_epub(src, title="DRY", author="A")
    book_id = _seed_buggy_catalog(realistic_conn, src)
    _seed_derived_rows(realistic_conn, book_id)

    chapter_ids = [
        cid for (cid,) in realistic_conn.execute(
            "SELECT chapter_id FROM chapter WHERE book_id = ?", [book_id]
        ).fetchall()
    ]
    placeholder = ",".join("?" * len(chapter_ids))

    embed_before = realistic_conn.execute(
        f"SELECT COUNT(*) FROM chapter_embedding "
        f"WHERE chapter_id IN ({placeholder})",
        chapter_ids,
    ).fetchone()[0]
    assert embed_before == 3

    books = mig.select_books(realistic_conn)
    stats, _ = mig.run_migration(realistic_conn, books, dry_run=True)

    assert stats.chapters_changed == 3
    assert stats.chapter_embedding_deleted == 3

    # Catalog unchanged for our book's chapters.
    embed_after = realistic_conn.execute(
        f"SELECT COUNT(*) FROM chapter_embedding "
        f"WHERE chapter_id IN ({placeholder})",
        chapter_ids,
    ).fetchone()[0]
    assert embed_after == 3
