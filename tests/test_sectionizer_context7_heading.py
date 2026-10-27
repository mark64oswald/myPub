"""Context7 headings: '### title' lines and URL-only titles."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
sys.path.insert(0, str(ROOT / "scripts"))

import sectionizer  # noqa: E402


def test_url_title_falls_back_to_body_heading():
    secs = sectionizer.sectionize_context7([
        {"title": None, "source": "https://github.com/apache/kafka/blob/trunk/docs/connect.html",
         "content": "### POST /connectors\nCreate a new connector."},
    ])
    assert secs[0].heading_text == "POST /connectors"


def test_plain_title_kept():
    secs = sectionizer.sectionize_context7([{"title": "Consumers", "source": "https://x", "content": "body"}])
    assert secs[0].heading_text == "Consumers"


def test_fetcher_reads_markdown_title_line():
    import refresh_docs
    chunks = refresh_docs.Context7Fetcher._text_to_chunks(
        "### Create a connector\nSource: https://github.com/apache/kafka/x\n\nPOST /connectors ...\n"
        "----------\nTitle: Old style\nSource: https://y\nbody")
    assert [c["title"] for c in chunks] == ["Create a connector", "Old style"]
    assert chunks[0]["source"] == "https://github.com/apache/kafka/x"
