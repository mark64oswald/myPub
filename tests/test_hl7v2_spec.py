"""Tests for scripts/hl7v2_spec.py — HL7 v2 structure reference from hl7apy."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

pytest.importorskip("hl7apy")
import hl7v2_spec as hs  # noqa: E402
from sectionizer import sectionize  # noqa: E402


@pytest.fixture(scope="module")
def md():
    return hs.spec_markdown("2.5.1")


def test_identifier_and_sections(md):
    ident, text = md
    assert ident == "hl7v2@2.5.1"
    assert "## PID segment — Patient Identification" in text
    assert "| PID-3 | Patient Identifier List | CX | R repeating | 1..* |  |" in text
    assert "## ADT_A01 message structure" in text
    assert "- PID (Patient Identification), R (1..1)" in text
    assert "## CX data type" in text
    assert "## HL7 table" not in text


def test_tables_only_when_asked():
    _, text = hs.spec_markdown("2.5.1", include_tables=True)
    assert "## HL7 table 0001 — Administrative Sex" in text


def test_fetcher_output_sectionizes_one_section_per_heading():
    fetched = hs.Hl7v2SpecFetcher("2.5.1").fetch("hl7v2@2.5.1")
    roots = sectionize({"source_type": fetched.source_type, "content": fetched.content})
    headings = {c.heading_text for r in roots for c in (r.children or [])}
    assert "PID segment — Patient Identification" in headings
    assert "ADT_A01 message structure" in headings
