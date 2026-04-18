"""
Tests for scripts/extraction_eval.py — the Phase 2.6 quality-metrics CLI.

These tests seed a small catalog with known extractions + resolutions and
verify the eval computes the right numbers. They don't hit the real
golden-set file; each test builds its own minimal golden dict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
for _p in (SCRIPTS_DIR,):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import extraction_eval as ev  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _seed(conn, book_id=1):
    """Seed two concepts (A, B), a chapter, and a concept_relation row."""
    conn.execute(
        "INSERT INTO author (name) VALUES ('Test Author') RETURNING author_id"
    )
    conn.execute(
        "INSERT INTO book (title, source_path, status) "
        "VALUES ('Book', '/tmp/b.epub', 'active')"
    )
    bid = conn.execute("SELECT book_id FROM book").fetchone()[0]
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'Ch', 'some content') RETURNING chapter_id",
        [bid],
    )
    cid_ch = conn.execute("SELECT chapter_id FROM chapter").fetchone()[0]
    # Two concepts A and B.
    conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES ('A', 'Concept', 'desc A') RETURNING concept_id"
    )
    conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES ('B', 'Concept', 'desc B') RETURNING concept_id"
    )
    cid_a, cid_b = (r[0] for r in conn.execute(
        "SELECT concept_id FROM concept ORDER BY concept_id"
    ).fetchall())
    # Relation A → B from this chapter.
    conn.execute(
        "INSERT INTO concept_relation "
        "(from_concept_id, to_concept_id, relation_type, confidence, "
        " source_type, source_id) VALUES (?, ?, 'REQUIRES', 0.9, 'chapter', ?)",
        [cid_a, cid_b, cid_ch],
    )
    return {"book_id": bid, "chapter_id": cid_ch, "a_id": cid_a, "b_id": cid_b}


# ----------------------------------------------------------------------------
# evaluate_extraction
# ----------------------------------------------------------------------------

def test_extraction_all_hits(schema_only_conn):
    ids = _seed(schema_only_conn)
    report = ev.evaluate_extraction(
        schema_only_conn,
        [
            {"chapter_id": ids["chapter_id"], "concept_name": "A", "concept_type": "Concept"},
            {"chapter_id": ids["chapter_id"], "concept_name": "B", "concept_type": "Concept"},
        ],
    )
    assert report.total_pairs == 2
    assert report.hits == 2
    assert report.recall == 1.0
    assert report.precision == 1.0
    assert report.f1 == 1.0
    assert report.misses == []


def test_extraction_misses_are_reported(schema_only_conn):
    ids = _seed(schema_only_conn)
    report = ev.evaluate_extraction(
        schema_only_conn,
        [
            {"chapter_id": ids["chapter_id"], "concept_name": "A", "concept_type": "Concept"},
            {"chapter_id": ids["chapter_id"], "concept_name": "Nonexistent",
             "concept_type": "Concept"},
        ],
    )
    assert report.hits == 1
    assert report.misses == [(ids["chapter_id"], "Nonexistent")]
    assert 0.0 < report.recall < 1.0


def test_extraction_case_insensitive_match(schema_only_conn):
    ids = _seed(schema_only_conn)
    report = ev.evaluate_extraction(
        schema_only_conn,
        [{"chapter_id": ids["chapter_id"], "concept_name": "a",
          "concept_type": "Concept"}],
    )
    assert report.hits == 1


def test_extraction_finds_concept_via_alias(schema_only_conn):
    """If the golden name matches an alias, the eval should still count a hit."""
    ids = _seed(schema_only_conn)
    schema_only_conn.execute(
        "INSERT INTO concept_alias (concept_id, alias, alias_type) "
        "VALUES (?, 'A-alias', 'synonym')", [ids["a_id"]]
    )
    report = ev.evaluate_extraction(
        schema_only_conn,
        [{"chapter_id": ids["chapter_id"], "concept_name": "A-alias",
          "concept_type": "Concept"}],
    )
    assert report.hits == 1


def test_extraction_extras_affect_precision(schema_only_conn):
    """Extras in a chapter (concepts extracted but NOT in golden) lower precision."""
    ids = _seed(schema_only_conn)
    # Only name 'A' in the golden set; the chapter also has 'B'.
    report = ev.evaluate_extraction(
        schema_only_conn,
        [{"chapter_id": ids["chapter_id"], "concept_name": "A",
          "concept_type": "Concept"}],
    )
    assert report.recall == 1.0  # we wanted A, got A
    # B is extra → precision < 1.
    assert report.precision < 1.0


# ----------------------------------------------------------------------------
# evaluate_resolution
# ----------------------------------------------------------------------------

def test_resolution_same_pair_recognizes_alias(schema_only_conn):
    ids = _seed(schema_only_conn)
    schema_only_conn.execute(
        "INSERT INTO concept_alias (concept_id, alias, alias_type) "
        "VALUES (?, 'A-short', 'abbreviation')", [ids["a_id"]]
    )
    report = ev.evaluate_resolution(
        schema_only_conn,
        {"same": [{"candidate": "A-short", "existing": "A"}], "different": []},
    )
    assert report.same_pairs == 1
    assert report.same_correct == 1
    assert report.accuracy == 1.0


def test_resolution_different_pair_flags_wrongful_merge(schema_only_conn):
    ids = _seed(schema_only_conn)
    # A and B are different concepts in the DB — the resolver is "correct".
    report_correct = ev.evaluate_resolution(
        schema_only_conn,
        {"same": [], "different": [{"a": "A", "b": "B"}]},
    )
    assert report_correct.different_correct == 1
    # Now wrongfully register B as an alias of A → resolution would map both to A.
    schema_only_conn.execute(
        "INSERT INTO concept_alias (concept_id, alias, alias_type) "
        "VALUES (?, 'B', 'synonym')", [ids["a_id"]]
    )
    # Our _concept_id_by_name prefers exact name match before alias, so B still
    # resolves to its own id. Drop the concept B entry to force alias lookup.
    schema_only_conn.execute("DELETE FROM concept_relation WHERE to_concept_id = ?",
                             [ids["b_id"]])
    schema_only_conn.execute("DELETE FROM concept WHERE concept_id = ?", [ids["b_id"]])
    report_wrong = ev.evaluate_resolution(
        schema_only_conn,
        {"same": [], "different": [{"a": "A", "b": "B"}]},
    )
    assert report_wrong.different_correct == 0  # now both resolve to same id


def test_resolution_skips_pairs_with_missing_sides(schema_only_conn):
    _seed(schema_only_conn)
    # A exists, "Ghost" doesn't — pair is not counted.
    report = ev.evaluate_resolution(
        schema_only_conn,
        {"same": [], "different": [{"a": "A", "b": "Ghost"}]},
    )
    assert report.different_pairs == 0
    assert report.different_correct == 0


# ----------------------------------------------------------------------------
# _format_report
# ----------------------------------------------------------------------------

def test_format_report_includes_metrics():
    ext = ev.ExtractionReport(total_pairs=10, hits=7)
    res = ev.ResolutionReport(same_pairs=4, same_correct=3,
                              different_pairs=5, different_correct=5)
    text = ev._format_report(ext, res)  # pylint: disable=protected-access
    assert "golden pairs:   10" in text
    assert "hits:           7" in text
    assert "recall:" in text
    assert "precision:" in text
    assert "f1:" in text


# ----------------------------------------------------------------------------
# The shipped golden-set JSON parses and has sensible structure
# ----------------------------------------------------------------------------

def test_shipped_golden_set_is_valid_json():
    path = PROJECT_ROOT / "tests" / "eval" / "golden_extractions.json"
    golden = json.loads(path.read_text())
    assert "extraction_pairs" in golden
    assert "resolution_pairs" in golden
    assert isinstance(golden["extraction_pairs"], list)
    assert golden["extraction_pairs"], "golden set must have at least one pair"
    for pair in golden["extraction_pairs"]:
        assert "chapter_id" in pair
        assert "concept_name" in pair
    assert "same" in golden["resolution_pairs"]
    assert "different" in golden["resolution_pairs"]
