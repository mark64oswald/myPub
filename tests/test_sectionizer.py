"""
Sectionizer parser tests (Phase 4.3).

The four core fixture cases the plan calls for:

    1. Well-structured README — multiple H2s with H3 subsections folded in.
    2. Flat README — H1 only, no deeper headings.
    3. Deeply nested doc — H1/H2/H3/H4 with split=2, verifying H3+ collapse.
    4. Empty doc — exercises the shapeless fallback.

Plus light coverage for the Context7 and DeepWiki parsers and the
``sectionize`` dispatch entry point. Pure parsing — no MCP, no DuckDB.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from sectionizer import (  # noqa: E402
    Section,
    _derive_body_heading,
    _is_url_heading,
    sectionize,
    sectionize_context7,
    sectionize_deepwiki,
    sectionize_markdown,
    sectionize_shapeless,
)


# --- fixture content ---------------------------------------------------------

WELL_STRUCTURED = """\
# FastMCP

A lightweight Model Context Protocol server framework.

## Installation

Install with pip:

```
pip install fastmcp
```

### Requirements

Python 3.10+ required.

## Quickstart

Build a server in three lines.

### Server example

```python
from fastmcp import FastMCP
mcp = FastMCP("demo")
```

### Client example

```python
from fastmcp import Client
```
"""

FLAT = """\
# Quick Notes

Just a paragraph of text and nothing else worth heading out.
A second line for shape.
"""

DEEPLY_NESTED = """\
# Top

## A
intro to A

### A1
detail a1

#### A1a
deeper still

### A2
detail a2

## B
intro to B

### B1
detail b1
"""

EMPTY = ""


# --- well-structured README --------------------------------------------------


def test_well_structured_returns_h1_root_with_two_h2_children():
    sections = sectionize_markdown(WELL_STRUCTURED)

    assert len(sections) == 1, "single H1 should be the lone root"
    root = sections[0]
    assert root.heading_level == 1
    assert root.heading_text == "FastMCP"
    assert len(root.children) == 2

    install, quickstart = root.children
    assert install.heading_text == "Installation"
    assert quickstart.heading_text == "Quickstart"
    # Ordinals reflect document order under the parent.
    assert install.ordinal == 0
    assert quickstart.ordinal == 1


def test_well_structured_h3_subheadings_fold_into_h2_content():
    sections = sectionize_markdown(WELL_STRUCTURED)
    install = sections[0].children[0]
    quickstart = sections[0].children[1]

    # H3 must NOT spawn its own section node.
    assert install.children == []
    assert quickstart.children == []

    # But the H3 source must remain inside the H2's content for indexing.
    assert "### Requirements" in install.content
    assert "Python 3.10+ required." in install.content
    assert "### Server example" in quickstart.content
    assert "### Client example" in quickstart.content


# --- flat README -------------------------------------------------------------


def test_flat_readme_yields_single_h1_section():
    sections = sectionize_markdown(FLAT)

    assert len(sections) == 1
    sec = sections[0]
    assert sec.heading_level == 1
    assert sec.heading_text == "Quick Notes"
    assert sec.children == []
    assert "paragraph of text" in sec.content
    assert "A second line for shape." in sec.content


# --- deeply nested H1/H2/H3/H4 -----------------------------------------------


def test_deeply_nested_at_split_2_creates_one_h1_with_two_h2():
    sections = sectionize_markdown(DEEPLY_NESTED, split_level=2)

    assert len(sections) == 1
    top = sections[0]
    assert top.heading_level == 1
    assert top.heading_text == "Top"
    assert len(top.children) == 2

    a, b = top.children
    assert a.heading_text == "A"
    assert b.heading_text == "B"
    # No grandchildren — H3+ folded into H2 content.
    assert a.children == []
    assert b.children == []


def test_deeply_nested_h3_and_h4_text_preserved_in_h2_content():
    sections = sectionize_markdown(DEEPLY_NESTED, split_level=2)
    a = sections[0].children[0]

    # All deeper-than-split headings remain visible in the body.
    assert "### A1" in a.content
    assert "#### A1a" in a.content
    assert "### A2" in a.content
    assert "detail a1" in a.content
    assert "deeper still" in a.content
    assert "detail a2" in a.content

    # B's content must NOT leak into A.
    assert "intro to B" not in a.content
    assert "detail b1" not in a.content


# --- empty / shapeless fallback ----------------------------------------------


def test_empty_doc_yields_one_shapeless_section():
    sections = sectionize_markdown(EMPTY)

    assert len(sections) == 1
    sec = sections[0]
    assert sec.heading_level is None
    assert sec.heading_text is None
    assert sec.content == ""
    assert sec.children == []


def test_no_headings_falls_back_to_shapeless():
    sections = sectionize_markdown("Just prose, no headings at all.\nLine two.")

    assert len(sections) == 1
    sec = sections[0]
    assert sec.heading_level is None
    assert sec.heading_text is None
    assert "Just prose" in sec.content
    assert "Line two." in sec.content


def test_sectionize_shapeless_direct():
    sections = sectionize_shapeless("hello\nworld")
    assert len(sections) == 1
    assert sections[0].heading_level is None
    assert sections[0].heading_text is None
    assert sections[0].content == "hello\nworld"


# --- Context7 parser ---------------------------------------------------------


def test_context7_chunks_become_leaf_sections_in_order():
    chunks = [
        {"title": "Install", "content": "pip install foo"},
        {"title": "Configure", "content": "foo --init"},
        {"title": "Run", "content": "foo run"},
    ]
    sections = sectionize_context7(chunks)

    assert len(sections) == 3
    assert [s.heading_text for s in sections] == ["Install", "Configure", "Run"]
    assert [s.content for s in sections] == ["pip install foo", "foo --init", "foo run"]
    assert all(s.heading_level is None for s in sections)
    assert all(s.children == [] for s in sections)
    assert [s.ordinal for s in sections] == [0, 1, 2]


def test_context7_handles_missing_fields_gracefully():
    chunks = [{"snippet": "fallback body"}, {}]
    sections = sectionize_context7(chunks)

    assert len(sections) == 2
    assert sections[0].content == "fallback body"
    assert sections[0].heading_text is None
    assert sections[1].content == ""


# --- DeepWiki parser ---------------------------------------------------------


def test_deepwiki_structure_walks_into_section_tree():
    structure = [
        {
            "id": "1",
            "title": "Overview",
            "children": [
                {"id": "1.1", "title": "Install", "children": []},
                {"id": "1.2", "title": "Quickstart", "children": []},
            ],
        },
        {
            "id": "2",
            "title": "Architecture",
            "children": [],
        },
    ]
    pages = {
        "1": "overview body",
        "1.1": "install body",
        "1.2": "quickstart body",
        "2": "architecture body",
    }

    sections = sectionize_deepwiki(structure, pages)

    assert len(sections) == 2
    overview, arch = sections
    assert overview.heading_level == 1
    assert overview.content == "overview body"
    assert "Overview" in overview.heading_text
    assert len(overview.children) == 2
    assert overview.children[0].heading_level == 2
    assert overview.children[0].content == "install body"
    assert arch.heading_level == 1
    assert arch.children == []


def test_deepwiki_missing_page_body_yields_empty_string():
    structure = [{"id": "9", "title": "Orphan", "children": []}]
    sections = sectionize_deepwiki(structure, pages={})
    assert sections[0].content == ""


# --- dispatch ----------------------------------------------------------------


def test_sectionize_dispatches_markdown():
    snap = {"source_type": "markdown", "content": "# Title\n\nbody"}
    sections = sectionize(snap)
    assert len(sections) == 1
    assert sections[0].heading_text == "Title"


def test_sectionize_dispatches_context7_with_json_content():
    snap = {
        "source_type": "context7",
        "content": json.dumps([{"title": "X", "content": "x body"}]),
    }
    sections = sectionize(snap)
    assert len(sections) == 1
    assert sections[0].heading_text == "X"
    assert sections[0].content == "x body"


def test_sectionize_dispatches_deepwiki_with_json_content():
    payload = {
        "structure": [{"id": "1", "title": "Root", "children": []}],
        "pages": {"1": "root body"},
    }
    snap = {"source_type": "deepwiki", "content": json.dumps(payload)}
    sections = sectionize(snap)
    assert len(sections) == 1
    assert sections[0].content == "root body"


def test_sectionize_unknown_source_type_falls_through_to_shapeless():
    snap = {"source_type": "mystery", "content": "raw text"}
    sections = sectionize(snap)
    assert len(sections) == 1
    assert sections[0].heading_level is None
    assert sections[0].content == "raw text"


def test_sectionize_unparseable_context7_json_falls_through_cleanly():
    snap = {"source_type": "context7", "content": "not-json"}
    sections = sectionize(snap)
    assert len(sections) == 1
    assert sections[0].heading_level is None


# ---------------------------------------------------------------------------
# URL-shaped heading replacement (deferred fix #6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://github.com/foo/bar", True),
        ("  https://example.com/page  ", True),
        ("<https://example.com>", True),
        ("[Example](https://example.com)", True),
        ("[Example](https://example.com \"title\")", True),
        ("ftp://files.example.com/dl", True),
        ("Real Heading", False),
        ("API Reference", False),
        ("See https://example.com for details", False),  # not entirely a URL
        ("", False),
        (None, False),
    ],
)
def test_is_url_heading(text, expected):
    assert _is_url_heading(text) is expected


def test_derive_body_heading_prefers_h3_subhead():
    body = "Some intro paragraph.\n\n### Real Subheading\n\nMore body text."
    assert _derive_body_heading(body) == "Real Subheading"


def test_derive_body_heading_strips_h3_trailing_hashes():
    body = "### Real Subheading ###\n\nMore body."
    assert _derive_body_heading(body) == "Real Subheading"


def test_derive_body_heading_skips_h3_thats_also_a_url():
    body = (
        "### https://github.com/foo/bar\n\n"
        "### Quick Start\n\n"
        "Body text."
    )
    assert _derive_body_heading(body) == "Quick Start"


def test_derive_body_heading_falls_back_to_first_short_line():
    body = "Quick Start Guide\n\nThis is the first paragraph of body text."
    assert _derive_body_heading(body) == "Quick Start Guide"


def test_derive_body_heading_skips_long_sentence_first_lines():
    body = (
        "This first line is far too long to be a heading and clearly "
        "reads like a sentence with multiple clauses and a period.\n\n"
        "Body text."
    )
    assert _derive_body_heading(body) is None


def test_derive_body_heading_skips_emphasis_markers():
    body = "**Configuration**\n\nBody text."
    assert _derive_body_heading(body) == "Configuration"


def test_derive_body_heading_skips_code_fence_and_list_lines():
    body = "```python\ndef foo(): pass\n```\n\n- bullet one\n- bullet two"
    assert _derive_body_heading(body) is None


def test_derive_body_heading_empty_body_returns_none():
    assert _derive_body_heading("") is None
    assert _derive_body_heading("   \n   ") is None


# Integration: sectionize_markdown handles URL-shaped headings end-to-end.


def test_sectionize_markdown_replaces_url_heading_with_h3():
    """A URL-shaped H2 with an H3 subhead inside its body should expose
    the H3 text as the section's heading_text instead of the URL."""
    md = (
        "# Project\n\n"
        "Intro paragraph.\n\n"
        "## https://github.com/foo/bar\n\n"
        "Body before subhead.\n\n"
        "### Authentication\n\n"
        "Body after subhead.\n"
    )
    roots = sectionize_markdown(md)
    # H1 root has one H2 child (the URL one)
    assert len(roots) == 1
    h1 = roots[0]
    assert h1.heading_text == "Project"
    assert len(h1.children) == 1
    h2 = h1.children[0]
    # The URL heading was replaced with the body's H3
    assert h2.heading_text == "Authentication"


def test_sectionize_markdown_url_heading_with_no_recoverable_subhead_becomes_none():
    """If neither an H3 nor a heading-shaped first line exists, the
    section's heading_text should be None — the title-coverage scorer
    skips Nones cleanly, whereas a URL would have to be filtered."""
    md = (
        "## https://github.com/foo/bar\n\n"
        "This is a long paragraph of body text that doesn't look like a "
        "heading because it's a full sentence with a period at the end.\n"
    )
    roots = sectionize_markdown(md)
    assert len(roots) == 1
    assert roots[0].heading_text is None


def test_sectionize_markdown_keeps_real_heading_intact():
    """Sanity check: non-URL headings are unchanged."""
    md = "## Configuration\n\nBody text.\n"
    roots = sectionize_markdown(md)
    assert roots[0].heading_text == "Configuration"
