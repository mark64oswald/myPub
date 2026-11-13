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
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from markdown_it import MarkdownIt

DEFAULT_SPLIT_LEVEL = 2  # split at H2; H3+ fold into parent content


# A "URL-shaped" heading is one whose entire content is a hyperlink — the
# common pattern in auto-generated READMEs / wikis where a section header
# is just `# https://github.com/foo/bar`. Such headings carry no semantic
# signal for retrieval (every section gets the same URL prefix), and the
# title-coverage scorer has to zero them out at query time.
#
# We strip these at parse time so a meaningful body-derived heading can
# replace them — boosting the section's chance of matching real queries.
_URL_HEADING_RE = re.compile(
    r"""
    ^\s*
    (?:
        \[[^\]]*\]\(\s*(?:https?|ftp)://[^)\s]+\s*(?:\s+\"[^\"]*\")?\)  # markdown link
        |
        <?(?:https?|ftp)://[^\s>]+>?                                    # bare URL
    )
    \s*$
    """,
    re.VERBOSE,
)

# Strip leading / trailing markdown emphasis tokens off a candidate
# replacement heading (they survive parsing because we read raw lines,
# not the markdown-it AST). Excludes backticks — those delimit code
# fences and should fall through to the explicit fence skip below.
_EMPHASIS_PREFIX_RE = re.compile(r"^[\*_]+|[\*_]+$")


def _is_url_heading(text: Optional[str]) -> bool:
    """True if ``text`` is essentially just a URL (bare or markdown-linked)."""
    if not text:
        return False
    return bool(_URL_HEADING_RE.match(text.strip()))


def _derive_body_heading(body: str, max_words: int = 12) -> Optional[str]:
    """Find a meaningful heading inside ``body`` when the source heading
    was URL-shaped or otherwise unhelpful.

    Strategy, in order:
      1. The first H3+ heading inside the body (``### Subhead``) — these
         got folded into content by the H2-split rule, so they're real
         headings the author wrote.
      2. The first non-empty line, if it looks heading-ish (short, no
         block punctuation, not a URL itself).
    Returns ``None`` if nothing reasonable surfaces — the caller should
    leave heading_text=None rather than fall back to the URL.
    """
    if not body or not body.strip():
        return None

    for line in body.splitlines():
        stripped = line.strip()
        # H3+ heading folded into the body
        m = re.match(r"^(#{3,6})\s+(.+?)\s*#*\s*$", stripped)
        if m:
            candidate = m.group(2).strip()
            if candidate and not _is_url_heading(candidate):
                return candidate

    # First plausible non-empty line, with fence-state tracking so
    # code blocks are skipped wholesale.
    in_fence = False
    for line in body.splitlines():
        raw = line.strip()
        if raw.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = _EMPHASIS_PREFIX_RE.sub("", raw).strip()
        if not stripped:
            continue
        if _is_url_heading(stripped):
            continue
        # Skip blockquote markers, list bullets, table pipes —
        # rarely good headings.
        if stripped.startswith((">", "- ", "* ", "+ ", "|")):
            continue
        # Likely a sentence, not a heading: too long or ends with period.
        words = stripped.split()
        if len(words) > max_words:
            continue
        if stripped.endswith((".", "!", "?")):
            continue
        return stripped

    return None


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
    flat = _markdown_flat(content, split_level)
    if not flat:
        return sectionize_shapeless(content)
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
        body = chunk.get("content") or chunk.get("snippet") or ""
        title = chunk.get("title") or chunk.get("source") or None
        if _is_url_heading(title):
            # A source URL makes a poor heading (and changes when Context7
            # changes its output format); prefer one from the body.
            title = _derive_body_heading(body) or title
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


# APT (Almost Plain Text / Apache Doxia) heading: one or more `*` at column 0
# followed by space + heading text. Bullets in APT also use `*` but are always
# indented, so column-0 anchoring is the disambiguator. `* H2`, `** H3`, ...
# Title is the first non-blank line of the file (no marker).
_APT_HEADING_RE = re.compile(r"^(\*+)\s+(.+?)\s*$")


def sectionize_apt(content: str) -> list[Section]:
    """Split APT (Almost Plain Text, Apache Maven Doxia format) into a tree.

    APT headings use `*+ ` at column 0 (not indented — that's a bullet).
    Heading level is `len(stars) + 1` so `*` = H2, `**` = H3, etc.
    The first non-blank line of the file (with no marker) is the H1 title.

    With no detectable headings, falls back to shapeless. Code blocks
    fenced by `------------` lines stay inside the enclosing section's
    body unmodified.
    """
    if not content.strip():
        return sectionize_shapeless(content)

    lines = content.splitlines()
    breaks: list[tuple[int, str, int]] = []  # (level, text, line_idx)

    # Title: first non-blank line that isn't a heading marker
    first_nonblank = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_nonblank = idx
            break
    if first_nonblank is not None and not _APT_HEADING_RE.match(lines[first_nonblank]):
        breaks.append((1, lines[first_nonblank].strip(), first_nonblank))

    in_code_block = False
    for idx, line in enumerate(lines):
        # Code block fence — APT uses `------------` (≥4 dashes) at column 0
        if re.match(r"^-{4,}\s*$", line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        m = _APT_HEADING_RE.match(line)
        if m:
            level = len(m.group(1)) + 1  # `*` -> 2, `**` -> 3
            heading = m.group(2).strip()
            breaks.append((level, heading, idx))

    if not breaks:
        return sectionize_shapeless(content)

    # Bodies: text between this break and the next, excluding the heading line
    flat: list[tuple[int, Optional[str], str]] = []
    for j, (level, heading, line_idx) in enumerate(breaks):
        # Body starts on the line *after* the heading; for the title (no
        # marker) the body starts after the title line.
        body_start = line_idx + 1
        body_end = breaks[j + 1][2] if j + 1 < len(breaks) else len(lines)
        body = "\n".join(lines[body_start:body_end]).strip("\n")
        flat.append((level, heading, body))

    return _build_tree(flat)


def _markdown_flat(content: str, split_level: int) -> list[tuple[int, Optional[str], str]]:
    """(level, heading, body) per heading of level ≤ split_level; [] if none."""
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
        return []

    flat: list[tuple[int, Optional[str], str]] = []
    for j, (level, heading_text, _hstart, hend) in enumerate(breaks):
        next_start = breaks[j + 1][2] if j + 1 < len(breaks) else len(lines)
        body = "".join(lines[hend:next_start]).strip("\n")
        # Replace URL-shaped headings with a body-derived alternative.
        # If no alternative surfaces, leave the heading None — None
        # already disables title-coverage scoring cleanly, whereas a
        # URL string would have to be filtered downstream.
        if _is_url_heading(heading_text):
            heading_text = _derive_body_heading(body)
        flat.append((level, heading_text, body))

    return flat


SITE_SPLIT_LEVEL = 4
SITE_MAX_CHARS = 4000


def sectionize_site_markdown(
    content: str,
    *,
    split_level: int = SITE_SPLIT_LEVEL,
    max_chars: int = SITE_MAX_CHARS,
) -> list[Section]:
    """Sections sized for retrieval, for whole-site doc pages.

    Vendor reference pages (Databricks audit-log tables, Claude Code hooks
    and env-var references, changelogs) run to 100 KB+ under one H2, while
    the embedder reads only the first ~1 KB of a section. So: split at
    H2–H4, then cut any section longer than ``max_chars`` at block
    boundaries — paragraphs, whole code fences, and table rows (the table
    header is repeated in each part). Parts become siblings titled
    "Heading (part 2/3)".
    """
    if not content.strip():
        return sectionize_shapeless(content)
    flat = _markdown_flat(content, split_level)
    if not flat:
        flat = [(1, None, content.strip("\n"))]
    out: list[tuple[int, Optional[str], str]] = []
    for level, heading, body in flat:
        if _is_link_list_tail(heading, body):
            continue
        parts = _split_body(body, max_chars)
        if len(parts) == 1:
            out.append((level, heading, body))
            continue
        for k, part in enumerate(parts, 1):
            title = f"{heading or 'Section'} (part {k}/{len(parts)})"
            out.append((level, title, part))
    return _build_tree(out)


_TAIL_HEADINGS = re.compile(
    r"^(additional resources|next steps|related (content|articles|topics|pages)|"
    r"learn more|see also|what's next)$", re.I)


def _is_link_list_tail(heading: Optional[str], body: str) -> bool:
    """'Additional resources' / 'Next steps' blocks that are only links.

    They're short and topic-dense, so they win vector search for the very
    topic whose real page they merely point to. Dropped when the body is
    nothing but list items / links.
    """
    if not heading or not _TAIL_HEADINGS.match(heading.strip()):
        return False
    text = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return all(ln.startswith(("- ", "* ", "[")) for ln in text) and len(body) < 2000


def _blocks(body: str) -> list[str]:
    """Paragraph-level blocks; a code fence is one block even with blank lines."""
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip() and cur:
            blocks.append("\n".join(cur))
            cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _split_table(block: str, max_chars: int) -> list[str]:
    lines = block.split("\n")
    # Separator styles: |---|---|, | --- | :-: |, Mintlify's | :- | :- |.
    if len(lines) < 3 or not _TABLE_SEP.match(lines[1]):
        return [block]
    header, rows = lines[:2], lines[2:]
    out, cur, size = [], [], 0
    for row in rows:
        if cur and size + len(row) > max_chars:
            out.append("\n".join(header + cur))
            cur, size = [], 0
        cur.append(row)
        size += len(row) + 1
    if cur:
        out.append("\n".join(header + cur))
    return out


def _split_body(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    # (text, separator before it): blocks re-join with a blank line, lines
    # of a split-up block with a single newline.
    pieces: list[tuple[str, str]] = []
    for b in _blocks(body):
        if len(b) > max_chars and b.lstrip().startswith("|"):
            pieces.extend((t, "\n\n") for t in _split_table(b, max_chars))
        elif len(b) > max_chars and not b.lstrip().startswith("```"):
            # One long block with no blank lines (a 2,700-item release-note
            # list, a changelog version): cut between lines.
            lines = b.split("\n")
            pieces.append((lines[0], "\n\n"))
            pieces.extend((ln, "\n") for ln in lines[1:])
        else:
            pieces.append((b, "\n\n"))
    parts: list[str] = []
    cur = ""
    for text, sep in pieces:
        if cur and len(cur) + len(text) > max_chars:
            parts.append(cur)
            cur = ""
        cur = text if not cur else cur + sep + text
    if cur:
        parts.append(cur)
    return parts


_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FM_FIELD = re.compile(r"^(title|description):[ \t]*(.*)$", re.M)


def strip_front_matter(content: str) -> str:
    """Drop a leading YAML front-matter block.

    Left in, markdown-it reads the closing ``---`` as a setext H2 underline
    and the ``title:/url:/description:`` lines become the first section's
    heading. ``title`` becomes an H1 when the body has none, and
    ``description`` is kept as the paragraph under the H1.
    """
    m = _FRONT_MATTER.match(content)
    if not m:
        return content
    fields = {k: v.strip().strip("'\"") for k, v in _FM_FIELD.findall(m.group(1))}
    body = content[m.end():].lstrip("\r\n")
    h1 = None
    if body.startswith("# "):
        h1, _, body = body.partition("\n")
    elif fields.get("title"):
        h1 = f"# {fields['title']}"
    parts = [p for p in (h1, fields.get("description"), body.lstrip("\r\n")) if p]
    return "\n\n".join(parts)


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
        return sectionize_markdown(strip_front_matter(content))

    if source_type == "site_md":
        return sectionize_site_markdown(strip_front_matter(content))

    if source_type == "apt":
        return sectionize_apt(content)

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


def _build_tree(flat: list[tuple[int, Optional[str], str]]) -> list[Section]:
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
