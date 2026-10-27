#!/usr/bin/env python3
"""
html_doc.py — static HTML spec pages as a doc source (source_type 'html_site').

For spec text published as plain HTML (DICOM PS3.x parts, HL7 v2 segment
references): one or more pages are turned into Markdown, headings become
"#" sections, tables become pipe tables, and the result goes through the
normal doc pipeline via ``refresh_docs.refresh_one_source`` with a local
fetcher — the same route as fhir_spec.py. Pages that need JavaScript to
render produce almost no text; ``render`` shows that before anything is
written.

    render    (--url U | --html-file F)... [--out spec.md]
              No DB. Writes the Markdown for review.
    register  (--url U | --html-file F)... --identifier ID [--name NAME]
              [--catalog data/catalog.ddb] [--authority 0.7]
              Writer. Upserts doc_source (source_type 'html_site',
              identifier ID, usually the base URL) and runs steps 1–6 of
              the doc pipeline. Then run refresh_docs.py prep / process as
              usual for concept + procedure extraction.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
SOURCE_TYPE = "html_site"
DROP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "svg", "form")
MAX_CELL = 160


@dataclass
class FetchResultLike:
    url: str
    content: str
    source_type: str


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def _cell(el) -> str:
    s = _text(el).replace("|", "\\|")
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def _table(tbl) -> list[str]:
    rows = []
    for tr in tbl.find_all("tr"):
        cells = [_cell(c) for c in tr.find_all(["th", "td"], recursive=False)]
        if any(cells):
            rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    caption = tbl.find("caption")
    return ([f"**{_text(caption)}**", ""] if caption else []) + out + [""]


def html_to_markdown(html: str) -> tuple[str, str]:
    """Return (title, markdown) for one HTML page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    title = _text(soup.title) if soup.title else ""
    body = soup.body or soup
    lines: list[str] = []
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "table", "dt", "dd"]):
        # Tables are rendered whole; skip anything nested inside one, and
        # list items / paragraphs nested in an already-emitted block.
        if el.name != "table" and el.find_parent("table"):
            continue
        if el.name == "table":
            if not el.find_parent("table"):
                lines += _table(el)
            continue
        if el.name in ("p", "li", "dd", "dt") and el.find_parent(["li", "dd"]) and el.name != "li":
            continue
        if el.name[0] == "h":
            text = _text(el)
            if text:
                lines += ["", "#" * int(el.name[1]) + " " + text, ""]
        elif el.name == "pre":
            lines += ["```", el.get_text().rstrip(), "```", ""]
        else:
            text = _text(el)
            if text:
                lines.append(("- " if el.name == "li" else "") + text)
                if el.name != "li":
                    lines.append("")
    md = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
    return title, md


def _read_pages(urls: Iterable[str], files: Iterable[Path]) -> list[tuple[str, str]]:
    pages = []
    for f in files:
        pages.append((f.as_uri(), f.read_text(errors="replace")))
    if urls:
        import requests
        for u in urls:
            r = requests.get(u, timeout=60, headers={"User-Agent": "myPub html_doc"})
            r.raise_for_status()
            r.encoding = r.encoding or r.apparent_encoding
            pages.append((u, r.text))
    return pages


def site_markdown(pages: list[tuple[str, str]], name: str = "") -> str:
    parts = [f"# {name}", ""] if name else []
    for url, html in pages:
        title, md = html_to_markdown(html)
        if len(pages) > 1 and not md.lstrip().startswith("# "):
            parts += [f"# {title or url}", ""]
        parts += [md, f"Source: {url}", ""]
    return "\n".join(parts)


class HtmlSiteFetcher:
    """Local fetcher with the refresh_docs fetcher interface."""

    def __init__(self, md: str, url: str):
        self.md, self.url = md, url

    def fetch(self, identifier: str) -> FetchResultLike:
        return FetchResultLike(url=self.url, content=self.md, source_type="markdown")


def _summary(md: str) -> str:
    n_sec = len(re.findall(r"^#{1,4} ", md, flags=re.M))
    return f"{n_sec} heading(s), {len(md.split())} words"


def do_render(args: argparse.Namespace) -> int:
    md = site_markdown(_read_pages(args.url, args.html_file), args.name or "")
    if args.out:
        args.out.write_text(md)
        print(f"{_summary(md)} → {args.out}")
    else:
        print(md)
    return 0


def do_register(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import refresh_docs  # noqa: PLC0415 — heavy imports, writer path only

    pages = _read_pages(args.url, args.html_file)
    md = site_markdown(pages, args.name or "")
    if len(md.split()) < 200:
        print(f"refusing: only {_summary(md)}; the page probably needs JavaScript")
        return 1
    conn = refresh_docs.open_writer(args.catalog)
    try:
        row = conn.execute("SELECT doc_source_id FROM doc_source WHERE source_type = ? "
                           "AND identifier = ?", [SOURCE_TYPE, args.identifier]).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
                "authority_score, refresh_ttl_days, priority_tier, pinned) "
                "VALUES (?, ?, 'local', ?, ?, 365, 'cold', TRUE) RETURNING doc_source_id",
                [args.name or args.identifier, SOURCE_TYPE, args.identifier,
                 args.authority]).fetchone()
        result = refresh_docs.refresh_one_source(
            conn, int(row[0]), fetcher=HtmlSiteFetcher(md, pages[0][0]))
    finally:
        conn.close()
    print(f"{args.identifier}: {result.status} (doc_source_id={row[0]}, "
          f"sections={getattr(result, 'section_count', None)}, error={result.error})")
    return 0 if result.status in ("refreshed", "no_change") else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("render", "register"):
        p = sub.add_parser(name)
        p.add_argument("--url", action="append", default=[])
        p.add_argument("--html-file", type=Path, action="append", default=[])
        p.add_argument("--name")
        if name == "render":
            p.add_argument("--out", type=Path)
        else:
            p.add_argument("--identifier", required=True)
            p.add_argument("--authority", type=float, default=0.7)
            p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)
    if not args.url and not args.html_file:
        parser.error("give at least one --url or --html-file")
    return do_render(args) if args.cmd == "render" else do_register(args)


if __name__ == "__main__":
    sys.exit(main())
