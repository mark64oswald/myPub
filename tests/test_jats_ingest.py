"""Tests for scripts/jats.py + scripts/ingest_pmc.py — PubMed Central ingestion."""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ingest_pmc  # noqa: E402
from jats import parse_jats  # noqa: E402

SAMPLE = PROJECT_ROOT / "tests" / "fixtures" / "pmc_sample.xml"


def test_parse_front_matter():
    art = parse_jats(SAMPLE.read_bytes())
    assert art.pmcid == "PMC9000001" and art.pmid == "10000001"
    assert art.source_path == "pmc:PMC9000001"
    assert art.title == "Mapping HL7 v2 feeds to FHIR resources at scale"
    assert art.journal == "Journal of Synthetic Informatics"
    assert art.authors == ["Ana Rivera", "Li Chen"]          # editor excluded
    assert art.publication_date == date(2024, 3, 1)          # ppub preferred
    assert art.keywords == ["FHIR", "HL7 v2", "interoperability"]
    assert art.license == "https://creativecommons.org/licenses/by/4.0/"
    assert art.abstract.startswith("Background: Hospitals still emit")


def test_parse_body_sections():
    art = parse_jats(SAMPLE.read_bytes())
    assert [s.title for s in art.sections] == ["Abstract", "Introduction", "Methods", "Discussion"]
    intro = art.sections[1].content
    assert "[1]" not in intro and "translate messages between" in intro.replace("  ", " ")
    methods = art.sections[2].content
    assert "### Segment mapping" in methods and "PID-3 maps to Patient.identifier." in methods
    assert "[Figure 1] Pipeline overview." in methods
    assert "mml" not in methods and "Someone. A paper" not in methods


def test_index_inserts_book_and_chapters(tmp_path):
    db = tmp_path / "c.ddb"
    conn = duckdb.connect(str(db))
    conn.execute((PROJECT_ROOT / "schemas" / "catalog.sql").read_text())
    conn.close()
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()
    shutil.copy(SAMPLE, xml_dir / "PMC9000001.xml")

    assert ingest_pmc.main(["index", "--xml-dir", str(xml_dir), "--catalog", str(db)]) == 0
    # Second run skips; --replace re-indexes in place.
    assert ingest_pmc.main(["index", "--xml-dir", str(xml_dir), "--catalog", str(db)]) == 0
    assert ingest_pmc.main(["index", "--xml-dir", str(xml_dir), "--catalog", str(db),
                            "--replace"]) == 0

    conn = duckdb.connect(str(db))
    books = conn.execute("SELECT book_id, publisher, subjects, chapter_count FROM book").fetchall()
    assert len(books) == 1
    bid, publisher, subjects, n = books[0]
    assert publisher == "Journal of Synthetic Informatics"
    assert subjects == ["FHIR", "HL7 v2", "interoperability"] and n == 4
    titles = [r[0] for r in conn.execute(
        "SELECT title FROM chapter WHERE book_id = ? ORDER BY chapter_num", [bid]).fetchall()]
    assert titles == ["Abstract", "Introduction", "Methods", "Discussion"]
    authors = [r[0] for r in conn.execute(
        "SELECT a.name FROM book_author ba JOIN author a USING (author_id) "
        "WHERE ba.book_id = ? ORDER BY position", [bid]).fetchall()]
    assert authors == ["Ana Rivera", "Li Chen"]
