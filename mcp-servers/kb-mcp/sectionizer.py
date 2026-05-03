"""
sectionizer.py — Parse fetched doc snapshots into doc_section trees.

Per the v2 architecture (§6.2), the snapshot ingestion pipeline separates
fetch (steps 1–3) from parse (step 4). This module is the parse stage and
holds no I/O — every parser takes already-fetched data as input. That keeps
the module pure (deterministic, testable without MCP, re-runnable against a
stored snapshot) and lets the orchestrator (`scripts/refresh_docs.py`,
Prompt 4.4) own all upstream calls.

Three concrete parsers plus a shapeless fallback:

  * ``sectionize_markdown`` — heading-tree split for GitHub READMEs and any
    Markdown source. Default split at H2; H3+ fold into the enclosing
    H≤2 section's content rather than becoming new section rows.
  * ``sectionize_context7`` — Context7 returns pre-chunked content; each
    chunk becomes a leaf section. No heading hierarchy is inferred.
  * ``sectionize_deepwiki`` — DeepWiki's ``read_wiki_structure`` produces a
    page tree; ``read_wiki_contents`` produces per-page bodies. The
    orchestrator merges them into a {structure, pages} payload that this
    parser walks into a Section tree.
  * ``sectionize_shapeless`` — fallback for content with no detectable
    structure. Returns a single section that covers the whole doc. Also
    used by ``sectionize_markdown`` when no headings are found.

The output type, ``Section``, is a recursive tree. The ``ordinal`` field
captures sibling order so the persistence layer can write deterministic
``doc_section.ordinal`` values. Tree-to-rows flattening (assigning
``parent_id`` after INSERT returns ``doc_section_id``) lives in the
caller — this module commits nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from markdown_it import MarkdownIt

DEFAULT_SPLIT_LEVEL = 2  # split at H2; H3+ fold into parent content


@dataclass
class Section:
    """One node in the doc_section tree.

    ``heading_level`` and ``heading_text`` are None for shapeless sections
    (no detectable heading) — the schema allows NULLs for both. ``ordinal``
    is the 0-based position among siblings.
    """

    heading_level: Optional[int]
    heading_text: Optional[str]
    content: str
    ordinal: int
    children: list["Section"] = field(default_factory=list)


def sectionize_markdown(
    content: str,
    *,
    split_level: int = DEFAULT_SPLIT_LEVEL,
) -> list[Section]:
    """Split Markdown into a Section tree at headings of level ≤ split_level.

    Headings deeper than ``split_level`` are not new sections — their source
    text remains inside the enclosing section's ``content`` so downstream
    indexing still sees the subheading inline.

    With no detectable headings, falls back to ``sectionize_shapeless``.
    """
    if not content.strip():
        return sectionize_shapeless(content)

    md = MarkdownIt()
    tokens = md.parse(content)
    lines = content.splitlines(keepends=True)

    breaks: list[tuple[int, str, int, int]] = []  # (level, text, hstart, hend)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open" and tok.map is not None:
            level = int(tok.tag[1:])  # 'h2' -> 2
            if level <= split_level:
                heading_text = ""
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    heading_text = tokens[i + 1].content
                hstart, hend = tok.map  # markdown-it: [start, end) line indices
                breaks.append((level, heading_text, hstart, hend))
        i += 1

    if not breaks:
        return sectionize_shapeless(content)

    flat: list[tuple[int, str, str]] = []
    for j, (level, heading_text, _hstart, hend) in enumerate(breaks):
        next_start = breaks[j + 1][2] if j + 1 < len(breaks) else len(lines)
        body = "".join(lines[hend:next_start]).strip("\n")
        flat.append((level, heading_text, body))

    return _build_tree(flat)


def sectionize_context7(chunks: list[dict[str, Any]]) -> list[Section]:
    """Each Context7 chunk becomes a leaf Section.

    Context7 returns content already split into retrieval-sized chunks —
    re-splitting would be guessing at heading structure that may not be
    there. We keep the chunks as the parser sees them. Each section has
    no children and no heading_level (not a true heading hierarchy).
    """
    sections: list[Section] = []
    for ordinal, chunk in enumerate(chunks):
        title = chunk.get("title") or chunk.get("source") or None
        body = chunk.get("content") or chunk.get("snippet") or ""
        sections.append(
            Section(
                heading_level=None,
                heading_text=title,
                content=body,
                ordinal=ordinal,
                children=[],
            )
        )
    return sections


def sectionize_deepwiki(
    structure: list[dict[str, Any]],
    pages: dict[str, str],
) -> list[Section]:
    """Walk a DeepWiki page tree into a Section tree.

    ``structure`` is a list of page nodes shaped like::

        {"id": "1.2", "title": "Quickstart Guide", "children": [...]}

    ``pages`` maps page id → markdown body. Missing bodies degrade to an
    empty string rather than raising — DeepWiki occasionally returns a
    structure entry with no fetched content, and an empty section is
    still a valid placeholder.

    The DeepWiki ``id`` ("1.2") is captured as ``heading_text`` prefix so
    cross-references in source content stay traceable. The numeric depth
    of the path (1.2 → depth 2) becomes ``heading_level``.
    """
    sections: list[Section] = []
    for ordinal, node in enumerate(structure):
        sections.append(_deepwiki_node_to_section(node, pages, ordinal))
    return sections


def sectionize_shapeless(content: str) -> list[Section]:
    """Fallback: one section covers the whole doc, no heading metadata."""
    return [
        Section(
            heading_level=None,
            heading_text=None,
            content=content.strip("\n"),
            ordinal=0,
            children=[],
        )
    ]


def sectionize(snapshot: dict[str, Any]) -> list[Section]:
    """Dispatch on ``snapshot['source_type']``.

    Expected snapshot shape::

        {
          "source_type": "markdown" | "github_md" | "context7" | "deepwiki" | ...,
          "content":     <str>,   # raw text for markdown / shapeless
                                  # JSON-encoded list[dict] for context7
                                  # JSON-encoded {structure, pages} for deepwiki
        }

    Unknown source types fall through to shapeless. The orchestrator is
    responsible for normalizing fetched payloads into this shape before
    persisting the snapshot row.
    """
    source_type = (snapshot.get("source_type") or "").lower()
    content = snapshot.get("content") or ""

    if source_type in {"markdown", "github_md", "github"}:
        return sectionize_markdown(content)

    if source_type == "context7":
        chunks = _maybe_json_load(content, default=None)
        if not isinstance(chunks, list) or not chunks:
            # Unparseable or empty payload — keep the §6.2 invariant that every
            # snapshot yields ≥1 section by falling back to shapeless on the raw
            # content. An empty list of chunks is real data the orchestrator
            # may legitimately persist; we still want one placeholder row.
            return sectionize_shapeless(content)
        return sectionize_context7(chunks)

    if source_type == "deepwiki":
        payload = _maybe_json_load(content, default=None)
        if not isinstance(payload, dict):
            return sectionize_shapeless(content)
        sections = sectionize_deepwiki(
            payload.get("structure", []),
            payload.get("pages", {}),
        )
        return sections or sectionize_shapeless(content)

    return sectionize_shapeless(content)


# --- internals ---------------------------------------------------------------


def _build_tree(flat: list[tuple[int, str, str]]) -> list[Section]:
    """Stack-based: each section's parent is the most recent shallower-level
    section. Siblings get sequential ordinals scoped to their parent."""
    roots: list[Section] = []
    stack: list[Section] = []
    for level, text, body in flat:
        while stack and stack[-1].heading_level is not None and stack[-1].heading_level >= level:
            stack.pop()
        sec = Section(
            heading_level=level,
            heading_text=text,
            content=body,
            ordinal=len(stack[-1].children) if stack else len(roots),
            children=[],
        )
        if stack:
            stack[-1].children.append(sec)
        else:
            roots.append(sec)
        stack.append(sec)
    return roots


def _deepwiki_node_to_section(
    node: dict[str, Any],
    pages: dict[str, str],
    ordinal: int,
) -> Section:
    page_id = str(node.get("id", ""))
    title = node.get("title") or page_id or None
    heading_text = f"{page_id} {title}".strip() if page_id and title else (title or page_id or None)
    level = page_id.count(".") + 1 if page_id else None
    body = pages.get(page_id, "")
    children = [
        _deepwiki_node_to_section(child, pages, idx)
        for idx, child in enumerate(node.get("children", []) or [])
    ]
    return Section(
        heading_level=level,
        heading_text=heading_text,
        content=body,
        ordinal=ordinal,
        children=children,
    )


def _maybe_json_load(value: Any, *, default: Any) -> Any:
    """Accept either an already-decoded structure or a JSON string. Returns
    ``default`` on parse error rather than raising — the caller decides
    how to surface 'unparseable snapshot'."""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default
