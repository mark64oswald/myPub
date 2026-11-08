"""Tests for the web_md fetcher and the Agent Skills / MCP seed list."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import refresh_docs  # noqa: E402
import seed_agent_standards  # noqa: E402
import seed_doc_sources  # noqa: E402

MINTLIFY_PAGE = (
    "> ## Documentation Index\n"
    "> Fetch the complete documentation index at: https://code.claude.com/docs/llms.txt\n"
    "> Use this file to discover all available pages before exploring further.\n"
    "\n"
    "# Plugins overview\n\n> Understand what a plugin is.\n\nBody.\n"
)


def _resp(text: str, ctype: str = "text/markdown; charset=utf-8", status: int = 200):
    return SimpleNamespace(status_code=status, text=text, headers={"content-type": ctype})


def test_web_md_strips_mintlify_index_note():
    with patch.object(httpx, "get", return_value=_resp(MINTLIFY_PAGE)):
        result = refresh_docs.WebMarkdownFetcher().fetch("https://code.claude.com/docs/en/plugins.md")
    assert result.content.startswith("# Plugins overview")
    # The page's own description blockquote survives.
    assert "> Understand what a plugin is." in result.content
    assert result.source_type == "github_md"
    assert result.url == "https://code.claude.com/docs/en/plugins.md"


def test_web_md_leaves_pages_without_index_note_alone():
    page = "---\ntitle: Agent Skills\n---\n\n# Agent Skills\n"
    with patch.object(httpx, "get", return_value=_resp(page)):
        result = refresh_docs.WebMarkdownFetcher().fetch("https://platform.claude.com/x.md")
    assert result.content == page


def test_web_md_rejects_html():
    with patch.object(httpx, "get", return_value=_resp("<!DOCTYPE html><html>", "text/html")):
        with pytest.raises(refresh_docs.FetchError, match="HTML"):
            refresh_docs.WebMarkdownFetcher().fetch("https://example.com/page.md")


def test_web_md_rejects_http_errors_and_non_https():
    with patch.object(httpx, "get", return_value=_resp("", status=404)):
        with pytest.raises(refresh_docs.FetchError, match="HTTP 404"):
            refresh_docs.WebMarkdownFetcher().fetch("https://example.com/missing.md")
    with pytest.raises(refresh_docs.FetchError, match="https://"):
        refresh_docs.WebMarkdownFetcher().fetch("http://example.com/page.md")


def test_get_fetcher_web_md():
    assert isinstance(refresh_docs.get_fetcher("web_md"), refresh_docs.WebMarkdownFetcher)


def test_seed_list_is_valid_and_unique():
    seeds = seed_agent_standards.AGENT_STANDARD_SEEDS
    for s in seeds:
        seed_doc_sources._validate(s)
    keys = [(s["source_type"], s["identifier"]) for s in seeds]
    assert len(keys) == len(set(keys))
    names = [s["name"] for s in seeds]
    assert len(names) == len(set(names))


def test_seed_list_pins_specs_above_vendor_docs():
    seeds = seed_agent_standards.AGENT_STANDARD_SEEDS
    by_name = {s["name"]: s for s in seeds}
    spec = by_name["MCP spec 2026-07-28: tools"]
    assert spec["identifier"] == (
        "modelcontextprotocol/modelcontextprotocol:main:"
        "docs/specification/2026-07-28/server/tools.mdx"
    )
    assert spec["authority_score"] > seed_doc_sources.DEFAULT_AUTHORITY["context7"]
    assert by_name["Agent Skills specification"]["authority_score"] == spec["authority_score"]
    for s in seeds:
        if s["source_type"] == "web_md":
            assert s["identifier"].startswith("https://") and s["identifier"].endswith(".md")


def test_seed_upsert_is_idempotent(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.ddb"))
    conn.execute(
        """
        CREATE SEQUENCE s;
        CREATE TABLE doc_source (
            doc_source_id INTEGER DEFAULT nextval('s') PRIMARY KEY,
            name VARCHAR, source_type VARCHAR NOT NULL, mcp_server VARCHAR,
            identifier VARCHAR, authority_score DOUBLE, refresh_ttl_days INTEGER,
            UNIQUE (source_type, identifier));
        """
    )
    seeds = seed_agent_standards.AGENT_STANDARD_SEEDS
    assert {seed_doc_sources._upsert(conn, s) for s in seeds} == {"inserted"}
    assert {seed_doc_sources._upsert(conn, s) for s in seeds} == {"updated"}
    assert conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == len(seeds)
    assert conn.execute(
        "SELECT authority_score FROM doc_source WHERE source_type = 'web_md' LIMIT 1"
    ).fetchone()[0] == seed_doc_sources.DEFAULT_AUTHORITY["web_md"]
