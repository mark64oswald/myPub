"""Tests for scripts/enrich_book_metadata.py — OpenLibrary / Google Books fill."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import enrich_book_metadata as em  # noqa: E402


def test_isbn_checksums_and_extraction():
    assert em.isbn_valid("9781449373320")
    assert not em.isbn_valid("9781449373321")
    assert em.isbn_valid("0306406152")
    assert em.find_isbns("urn:isbn:978-1-4493-7332-0", "Designing Data-9781449373320") == ["9781449373320"]


def test_title_overlap_ignores_subtitle_and_stopwords():
    assert em.title_overlap("Designing Data-Intensive Applications",
                            "Designing data-intensive applications: the big ideas") == 1.0
    assert em.title_overlap("Kafka: The Definitive Guide", "Cooking for Kafka fans") < 0.6


def test_clean_subjects_drops_noise_and_splits_paths():
    raw = ["Accessible book", "Computers / Databases / General", "Protected DAISY",
           "nyt:best", "Databases", "Distributed systems"]
    assert em.clean_subjects(raw) == ["Computers", "Databases", "Distributed systems"]


def test_parse_date_variants():
    assert em.parse_date("March 2017") == "2017-01-01"
    assert em.parse_date("2017-03-16") == "2017-03-16"
    assert em.parse_date("2017-03") == "2017-03-01"
    assert em.parse_date("n.d.") is None


def _fetcher(routes):
    def fetch(url):
        for key, body in routes.items():
            if key in url:
                return body
        return None
    return fetch


def test_enrich_prefers_isbn_then_fills_remaining_gap_from_search():
    gap = em.BookGap(1, "Designing Data-Intensive Applications", "/x.epub", ["Martin Kleppmann"],
                     needs_subjects=True, needs_date=True, needs_publisher=True)
    fetch = _fetcher({
        "openlibrary.org/isbn/9781449373320": {"title": "Designing Data-Intensive Applications",
                                               "publishers": ["O'Reilly Media"],
                                               "works": [{"key": "/works/W1"}]},
        "openlibrary.org/works/W1": {"subjects": ["Database design", "Accessible book"]},
        "googleapis.com": {"items": [{"volumeInfo": {
            "title": "Designing Data-Intensive Applications", "publishedDate": "2017-03-16",
            "authors": ["Martin Kleppmann"], "categories": ["Computers"]}}]},
    })
    e = em.enrich_one(gap, ["9781449373320"], fetch)
    assert (e.source, e.matched_by) == ("openlibrary", "isbn")
    assert e.subjects == ["Database design"]
    assert e.publisher == "O'Reilly Media"
    assert e.publication_date == "2017-03-16"     # from Google, the next source


def test_title_search_rejects_wrong_author():
    gap = em.BookGap(1, "Streaming Systems", "/x.epub", ["Tyler Akidau"], True, False, False)
    fetch = _fetcher({"search.json": {"docs": [{"title": "Streaming Systems",
                                                 "author_name": ["Someone Else"],
                                                 "subject": ["Rivers"]}]}})
    e = em.enrich_one(gap, [], fetch)
    assert e.source is None and e.note == "no confident match"


def test_apply_fills_only_empty_fields(tmp_path):
    db = tmp_path / "c.ddb"
    conn = duckdb.connect(str(db))
    conn.execute((PROJECT_ROOT / "schemas" / "catalog.sql").read_text())
    b1 = conn.execute("INSERT INTO book (title, source_path) VALUES ('A', '/a') RETURNING book_id").fetchone()[0]
    b2 = conn.execute("INSERT INTO book (title, source_path, publisher) VALUES ('B', '/b', 'Keep Me') "
                      "RETURNING book_id").fetchone()[0]
    conn.execute("INSERT INTO chapter (book_id, title) VALUES (?, 'c1'), (?, 'c2')", [b1, b2])
    gaps = {g.book_id: g for g in em.select_gaps(conn, None)}
    assert gaps[b2].needs_publisher is False and gaps[b1].needs_subjects
    rows = [
        {"book_id": b1, "source": "openlibrary", "subjects": ["Databases"],
         "publication_date": "2019-01-01", "publisher": "Manning"},
        {"book_id": b2, "source": "googlebooks", "subjects": [], "publication_date": None,
         "publisher": "Overwrite Attempt"},
    ]
    stats = em.apply_enrichment(conn, rows)
    assert stats == {"subjects": 1, "dates": 1, "publishers": 1}
    assert conn.execute("SELECT publisher FROM book WHERE book_id = ?", [b2]).fetchone()[0] == "Keep Me"
    # Re-selecting no longer lists b1's subjects as a gap
    gaps = {g.book_id: g for g in em.select_gaps(conn, None)}
    assert b1 not in gaps
    assert em.apply_enrichment(conn, rows)["subjects"] == 0   # idempotent
