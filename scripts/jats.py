"""jats.py — Parse JATS XML (PubMed Central articles) into book-shaped records.

A PMC article maps onto the existing book / chapter tables so search,
embedding, concept extraction and every generator work on it unchanged:

    book     one article: title, journal (publisher), pub date,
             abstract (description), keywords (subjects),
             source_path = "pmc:PMC<id>"
    chapter  "Abstract", then each top-level <sec> of <body>
             (Introduction, Methods, Results, Discussion, …). Nested
             <sec>s fold into their parent with "### heading" lines so
             a chapter stays one coherent unit. Figure and table
             captions are kept inline; reference lists are dropped.

Pure parsing only; ``scripts/ingest_pmc.py`` does the fetching and DB I/O.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Elements whose text is noise inside a chapter body.
_SKIP_TAGS = {"ref-list", "fn-group", "ack", "glossary", "app-group",
              "inline-formula", "disp-formula", "tex-math", "math",
              "supplementary-material", "graphic", "media", "object-id"}
_BLOCK_TAGS = {"p", "title", "caption", "list-item", "td", "th", "label",
               "def", "term", "statement", "boxed-text"}


@dataclass
class JatsSection:
    title: str
    content: str


@dataclass
class JatsArticle:
    pmcid: Optional[str]
    pmid: Optional[str]
    doi: Optional[str]
    title: str
    journal: Optional[str]
    publication_date: Optional[date]
    authors: list[str]
    abstract: str
    keywords: list[str]
    sections: list[JatsSection] = field(default_factory=list)
    license: Optional[str] = None

    @property
    def source_path(self) -> str:
        return f"pmc:{self.pmcid}" if self.pmcid else f"doi:{self.doi}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: Optional[ET.Element]) -> str:
    """Flatten an element's text, skipping noise and separating blocks."""
    if el is None:
        return ""
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = _local(node.tag)
        if tag in _SKIP_TAGS:
            if node.tail:
                parts.append(node.tail)
            return
        if tag == "xref" and node.get("ref-type") == "bibr":
            # "[12]" style citation markers carry no meaning out of context
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if tag in _BLOCK_TAGS:
            parts.append("\n")
        if node.tail:
            parts.append(node.tail)

    walk(el)
    text = "".join(parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _find(root: ET.Element, path: str) -> Optional[ET.Element]:
    """Namespace-agnostic find over a slash path of local names."""
    nodes = [root]
    for step in path.split("/"):
        nodes = [c for n in nodes for c in n if _local(c.tag) == step]
        if not nodes:
            return None
    return nodes[0]


def _findall(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def _pub_date(meta: ET.Element) -> Optional[date]:
    best = None
    for pd in [e for e in meta if _local(e.tag) == "pub-date"]:
        kind = pd.get("pub-type") or pd.get("date-type") or ""
        try:
            y = int(_text(_find(pd, "year")) or 0)
        except ValueError:
            continue
        if not y:
            continue
        m = _text(_find(pd, "month"))
        d = _text(_find(pd, "day"))
        try:
            val = date(y, int(m) if m.isdigit() else 1, int(d) if d.isdigit() else 1)
        except ValueError:
            val = date(y, 1, 1)
        # Prefer the print/issue date; fall back to whatever comes first.
        if kind in ("ppub", "collection", "pub") or best is None:
            best = val
            if kind in ("ppub", "collection", "pub"):
                break
    return best


def _section(sec: ET.Element, depth: int = 0) -> JatsSection:
    title = _text(_find(sec, "title")) or "Untitled section"
    lines: list[str] = []
    for child in sec:
        tag = _local(child.tag)
        if tag in ("title", "label"):
            continue
        if tag == "sec":
            sub = _section(child, depth + 1)
            lines.append(f"\n{'#' * min(3 + depth, 6)} {sub.title}\n")
            if sub.content:
                lines.append(sub.content)
        elif tag in ("fig", "table-wrap"):
            label = _text(_find(child, "label"))
            caption = _text(_find(child, "caption"))
            if caption:
                lines.append(f"[{label or tag}] {caption}")
        elif tag not in _SKIP_TAGS:
            t = _text(child)
            if t:
                lines.append(t)
    return JatsSection(title=title, content="\n\n".join(l.strip() for l in lines if l.strip()))


def parse_jats(xml: str | bytes) -> JatsArticle:
    """Parse one <article>. Accepts a bare article or a <pmc-articleset>."""
    root = ET.fromstring(xml)
    if _local(root.tag) != "article":
        art = next((e for e in root.iter() if _local(e.tag) == "article"), None)
        if art is None:
            raise ValueError("no <article> element")
        root = art
    meta = _find(root, "front/article-meta")
    if meta is None:
        raise ValueError("no <front>/<article-meta>")

    ids = {e.get("pub-id-type"): (e.text or "").strip()
           for e in meta if _local(e.tag) == "article-id"}
    pmcid = ids.get("pmc") or ids.get("pmcid")
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"

    authors = []
    for contrib in _findall(meta, "contrib"):
        if contrib.get("contrib-type", "author") != "author":
            continue
        name = _find(contrib, "name")
        if name is not None:
            given, surname = _text(_find(name, "given-names")), _text(_find(name, "surname"))
            full = f"{given} {surname}".strip()
        else:
            full = _text(_find(contrib, "collab"))
        if full:
            authors.append(full)

    # Elements are falsy when childless, so compare with None explicitly.
    abstract_el = next((e for e in meta if _local(e.tag) == "abstract"
                        and not e.get("abstract-type")), None)
    if abstract_el is None:
        abstract_el = _find(meta, "abstract")
    if abstract_el is not None and any(_local(c.tag) == "sec" for c in abstract_el):
        # Structured abstract: "Background: … Methods: …"
        abstract = "\n\n".join(
            f"{s.title}: {s.content}" for s in
            (_section(c) for c in abstract_el if _local(c.tag) == "sec"))
    else:
        abstract = _text(abstract_el)

    keywords = [_text(k) for k in _findall(meta, "kwd") if _text(k)]
    license_el = _find(meta, "permissions/license")
    license_ = (license_el.get("{http://www.w3.org/1999/xlink}href")
                if license_el is not None else None) or (
                _text(license_el)[:200] if license_el is not None else None)

    body = _find(root, "body")
    sections: list[JatsSection] = []
    if abstract:
        sections.append(JatsSection("Abstract", abstract))
    if body is not None:
        loose: list[str] = []
        for child in body:
            if _local(child.tag) == "sec":
                sections.append(_section(child))
            else:
                t = _text(child)
                if t:
                    loose.append(t)
        if loose:   # body text outside any <sec> (letters, short reports)
            sections.insert(1 if abstract else 0, JatsSection("Main text", "\n\n".join(loose)))

    journal = _text(_find(root, "front/journal-meta/journal-title-group/journal-title")) or \
        _text(_find(root, "front/journal-meta/journal-title")) or None

    return JatsArticle(
        pmcid=pmcid, pmid=ids.get("pmid"), doi=ids.get("doi"),
        title=_text(_find(meta, "title-group/article-title")) or "(untitled article)",
        journal=journal, publication_date=_pub_date(meta), authors=authors,
        abstract=abstract, keywords=keywords[:12], sections=sections, license=license_,
    )
