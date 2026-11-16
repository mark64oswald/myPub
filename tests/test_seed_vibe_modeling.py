"""Tests for the Vibe Data Modeling seeds and the CSV-to-markdown fetch path."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "mcp-servers" / "kb-mcp")]

import refresh_docs  # noqa: E402
import sectionizer  # noqa: E402
import seed_vibe_modeling as svm  # noqa: E402

RULES_CSV = (
    '"ID","Group","Scope","Name","Requirement","Example"\n'
    '"ATT-RUL-069","Attribute","ALL","ENUM cap","Max 6 values, else a reference product.","order status"\n'
    '"FK-RUL-001","Foreign Key","ALL","No cycles","FK graph is a DAG.",""\n'
)


def test_csv_to_markdown_one_h2_per_row():
    md = refresh_docs.csv_to_markdown(RULES_CSV, title="rules.csv")
    assert md.startswith("# rules.csv\n")
    assert "## ATT-RUL-069 ENUM cap" in md
    assert "## FK-RUL-001 No cycles" in md
    assert "**Group:** Foreign Key" in md
    assert "**Requirement:** FK graph is a DAG." in md
    # empty cells are dropped
    assert "**Example:** \n" not in md and md.count("**Example:**") == 1


def test_csv_rows_become_sections():
    md = refresh_docs.csv_to_markdown(RULES_CSV, title="rules.csv")
    flat = refresh_docs.flatten_section_tree(
        sectionizer.sectionize({"source_type": "github_md", "content": md}))
    headings = [s.heading_text for s in flat if s.heading_level == 2]
    assert headings == ["ATT-RUL-069 ENUM cap", "FK-RUL-001 No cycles"]


def test_csv_without_id_columns_uses_row_numbers():
    md = refresh_docs.csv_to_markdown("a,b\n1,2\n3,4\n")
    assert "## Row 1" in md and "## Row 2" in md and "**a:** 3" in md


def test_github_fetch_renders_csv():
    fake = SimpleNamespace(status_code=200, text=RULES_CSV)
    with patch.object(httpx, "get", return_value=fake):
        res = refresh_docs.GitHubFetcher().fetch("o/r:main:rules/vibe-rules.csv")
    assert res.source_type == "github_md"
    assert res.content.startswith("# vibe-rules.csv")
    assert "## FK-RUL-001 No cycles" in res.content


def test_seeds_are_unique_pinned_and_scored():
    ids = [s["identifier"] for s in svm.VIBE_SEEDS]
    assert len(ids) == len(set(ids))
    assert all(i.startswith(f"{svm.REPO}:main:") for i in ids)
    assert all(s["source_type"] == "github_raw" for s in svm.VIBE_SEEDS)
    assert {s["authority_score"] for s in svm.VIBE_SEEDS} == {0.70, 0.65}
    # models are loaded as structured metadata, never as doc sections
    assert not any("/data-models/" in i or i.endswith("model.json") for i in ids)
    assert any(i.endswith(".csv") for i in ids)


def test_seed_into_catalog(tmp_path):
    import duckdb
    conn = duckdb.connect(str(tmp_path / "c.ddb"))
    conn.execute("""CREATE SEQUENCE s; CREATE TABLE doc_source (
        doc_source_id INTEGER DEFAULT nextval('s') PRIMARY KEY, name VARCHAR,
        source_type VARCHAR, mcp_server VARCHAR, identifier VARCHAR,
        authority_score DOUBLE, refresh_ttl_days INTEGER)""")
    from seed_doc_sources import _upsert
    for seed in svm.VIBE_SEEDS:
        assert _upsert(conn, seed) == "inserted"
    assert _upsert(conn, svm.VIBE_SEEDS[0]) == "updated"
    n = conn.execute("SELECT count(*) FROM doc_source").fetchone()[0]
    assert n == len(svm.VIBE_SEEDS)
