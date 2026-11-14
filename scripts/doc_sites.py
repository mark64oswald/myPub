#!/usr/bin/env python3
"""
doc_sites.py — whole documentation sites as live-doc sources, refreshed on demand.

A *doc site* is a vendor docs site enumerated from its sitemap.xml or
llms.txt (docs.databricks.com, code.claude.com/docs). Every page becomes
its own doc_source row (source_type ``web_md`` for pages served as
Markdown, ``web_html`` for HTML pages), so search results and generator
citations name the page, and a page can be refreshed on its own.

Freshness is a command, not a schedule::

    plan    [--site NAME | --all] [--check] [--limit N]
            Read-only. Re-reads the index and reports pages that are new,
            removed upstream, never fetched, or not checked within the
            site's TTL. --check also asks the server whether each known
            page changed (conditional GET: ETag / Last-Modified, then a
            content-hash compare), still writing nothing.

    sync    [--site NAME | --all] [--max-pages N] [--only new|changed|removed]
            [--prep-extract] [--force-retire]
            Writer. Registers new pages, re-fetches pages with conditional
            GET, and re-ingests only pages whose content hash changed.
            Within a changed page, sections whose heading + body are
            unchanged keep their embedding and extracted concepts; only the
            rest are embedded (and, with --prep-extract, prompted for
            sub-agent extraction). Pages gone from the index (or 404/410)
            are retired: their sections leave search, snapshots stay as
            history. FTS is rebuilt once at the end.

    status  [--site NAME]
            Per site: pages live / retired / never fetched / overdue, last
            sync, recent errors.

    add-site --preset databricks|claude-code  |  --name … --index-url … …
            Writer. Registers (or updates) a site definition.

    stale   [--domain example.com] [--source-type T]
            Read-only. Every doc_source (sites and single sources) whose
            last refresh is older than its refresh_ttl_days.

Tables (created on first write, additive — see ``ensure_schema``):

    doc_site          one row per site: index URL, page format, URL filters
    doc_fetch_state   one row per page source: URL, ETag, Last-Modified,
                      content hash, last check / change, retired_at
    doc_refresh_run   one row per sync: counts per action

doc_fetch_state is a side table rather than new doc_source columns because
DuckDB refuses ALTER TABLE on a table other tables reference by FK.

No LLM calls happen here. --prep-extract writes the usual prompt
manifests (refresh_docs.prep_extraction) for Claude Code sub-agents.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlparse

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

log = logging.getLogger("doc_sites")

USER_AGENT = "myPub-doc-sites/1.0 (personal knowledge base; polite crawler)"
HTTP_TIMEOUT_S = 30.0
# Retiring pages because the index shrank is the one destructive step. If
# the index lost more than this share of the live pages at once, it's more
# likely a broken sitemap than a docs reorg — refuse unless --force-retire.
MAX_RETIRE_SHARE = 0.10

DDL = """
CREATE SEQUENCE IF NOT EXISTS seq_doc_site_id START 1;
CREATE TABLE IF NOT EXISTS doc_site (
    site_id            BIGINT PRIMARY KEY DEFAULT nextval('seq_doc_site_id'),
    name               VARCHAR NOT NULL UNIQUE,
    index_url          VARCHAR NOT NULL,
    index_kind         VARCHAR NOT NULL,          -- 'sitemap' | 'llms_txt'
    page_format        VARCHAR NOT NULL,          -- 'markdown' | 'html'
    md_url_suffix      VARCHAR,                   -- page URL + suffix = Markdown URL
    content_selector   VARCHAR,                   -- CSS selector for the article (html)
    include_patterns   VARCHAR[],                 -- glob on page URL; empty = all
    exclude_patterns   VARCHAR[],
    name_prefix        VARCHAR,
    authority_score    DOUBLE,
    refresh_ttl_days   INTEGER DEFAULT 14,
    max_workers        INTEGER DEFAULT 4,
    request_interval_s DOUBLE  DEFAULT 0.25,      -- per worker
    last_index_at      TIMESTAMP,
    last_sync_at       TIMESTAMP,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS doc_fetch_state (
    doc_source_id      BIGINT PRIMARY KEY,        -- logical FK → doc_source
    site_id            BIGINT,                    -- logical FK → doc_site
    page_url           VARCHAR NOT NULL,
    fetch_url          VARCHAR,
    etag               VARCHAR,
    last_modified      VARCHAR,
    content_hash       VARCHAR,
    first_seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_in_index TIMESTAMP,
    last_checked_at    TIMESTAMP,
    last_changed_at    TIMESTAMP,
    content_date       VARCHAR,                   -- page's own "last updated" stamp
    last_status        VARCHAR,                   -- new|not_modified|unchanged|changed|error|retired
    last_http_status   INTEGER,
    last_error         VARCHAR,
    retired_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_doc_fetch_state_site ON doc_fetch_state(site_id);
CREATE TABLE IF NOT EXISTS doc_refresh_run (
    run_id        VARCHAR PRIMARY KEY,
    site_id       BIGINT,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    counts        VARCHAR,                        -- JSON {action: n}
    report_path   VARCHAR
);
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(DDL)
    # Columns added after the first release of these tables.
    conn.execute("ALTER TABLE doc_fetch_state ADD COLUMN IF NOT EXISTS content_date VARCHAR")


def _schema_present(conn: duckdb.DuckDBPyConnection) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' "
        "AND table_name='doc_site'"
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Site definitions
# ---------------------------------------------------------------------------


@dataclass
class Site:
    name: str
    index_url: str
    index_kind: str
    page_format: str
    md_url_suffix: Optional[str] = None
    content_selector: Optional[str] = None
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    name_prefix: Optional[str] = None
    authority_score: float = 0.85
    refresh_ttl_days: int = 14
    max_workers: int = 4
    request_interval_s: float = 0.25
    site_id: Optional[int] = None
    last_sync_at: Optional[datetime] = None

    def wants(self, url: str) -> bool:
        if self.include_patterns and not any(fnmatch.fnmatchcase(url, p)
                                             for p in self.include_patterns):
            return False
        return not any(fnmatch.fnmatchcase(url, p) for p in self.exclude_patterns)

    def fetch_url(self, page_url: str) -> str:
        if self.page_format != "markdown" or page_url.endswith(".md"):
            return page_url
        suffix = self.md_url_suffix or ".md"
        if page_url.endswith("/"):
            return page_url + "index" + suffix if suffix.startswith(".") else page_url + suffix
        return page_url + suffix

    @property
    def source_type(self) -> str:
        return "web_md" if self.page_format == "markdown" else "web_html"


_SITE_COLS = ("site_id", "name", "index_url", "index_kind", "page_format", "md_url_suffix",
              "content_selector", "include_patterns", "exclude_patterns", "name_prefix",
              "authority_score", "refresh_ttl_days", "max_workers", "request_interval_s",
              "last_sync_at")


# Presets. Patterns are fnmatch globs on the page URL (``*`` crosses "/").
PRESETS: dict[str, Site] = {
    # docs.databricks.com publishes one sitemap of /aws/en/ pages (Azure and
    # GCP mirror the same docs under their own prefixes; AWS is the
    # canonical copy). See docs/live-docs-refresh.md for the sizing run.
    "databricks": Site(
        name="databricks",
        index_url="https://docs.databricks.com/sitemap.xml",
        index_kind="sitemap",
        page_format="html",
        # Docusaurus: the article body. Falls back to <article>/<main>.
        content_selector="div.theme-doc-markdown",
        include_patterns=["https://docs.databricks.com/aws/en/*"],
        # Archived pages describe retired features — they work against
        # freshness. Release notes stay: they're the change log.
        exclude_patterns=["*/archive/*"],
        name_prefix="Databricks",
        authority_score=0.85,
        refresh_ttl_days=14,
        max_workers=2,
        request_interval_s=1.0,      # 2 requests/second overall
    ),
    # Databricks REST API reference: one unified page per endpoint or object
    # (the old /api/workspace|gcp|azure URLs redirect to it; cloud
    # differences are noted inline). /api/llms.txt lists ~1,580 pages, each
    # with a Markdown twin at <url>.md. Not in the main sitemap.
    "databricks-api": Site(
        name="databricks-api",
        index_url="https://docs.databricks.com/api/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        md_url_suffix=".md",
        include_patterns=["https://docs.databricks.com/api/*"],
        name_prefix="Databricks API",
        authority_score=0.85,
        refresh_ttl_days=14,
        max_workers=2,
        request_interval_s=1.0,      # 2 requests/second overall
    ),
    # Claude Code docs: llms.txt lists every page as a Markdown URL.
    "claude-code": Site(
        name="claude-code",
        index_url="https://code.claude.com/docs/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        include_patterns=["https://code.claude.com/docs/en/*"],
        name_prefix="Claude Code",
        authority_score=0.85,
        refresh_ttl_days=7,
    ),
    # Claude Developer Platform: API reference, build-with-claude guides,
    # agents and tools, Managed Agents, models, release notes (English only;
    # the translated copies are partial mirrors). llms.txt lists .md URLs.
    "claude-platform": Site(
        name="claude-platform",
        index_url="https://platform.claude.com/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        include_patterns=["https://platform.claude.com/docs/en/*"],
        name_prefix="Claude Platform",
        authority_score=0.85,
        refresh_ttl_days=7,
    ),
    # claude.com/docs, developer subset: building connectors (incl. MCP Apps),
    # skills, plugins, the directory, Cowork. The rest of the site is admin
    # and end-user material (government, Claude in Slack, desktop deployment,
    # Claude for Science gallery).
    "claude-docs": Site(
        name="claude-docs",
        index_url="https://claude.com/docs/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        include_patterns=[f"https://claude.com/docs/{p}" for p in (
            "index.md", "extend/*", "build/*", "connectors/*", "plugins/*",
            "skills/*", "directory/*", "cowork/*")],
        name_prefix="Claude Docs",
        authority_score=0.85,
        refresh_ttl_days=7,
    ),
    # modelcontextprotocol.io: MCP docs, extensions and the specification.
    # Only the current revision (spec and docs) — older dated revisions and the draft
    # would put three or four near-copies of every spec page in search.
    # When a new revision ships, add the old one to the excludes.
    "mcp": Site(
        name="mcp",
        index_url="https://modelcontextprotocol.io/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        md_url_suffix=".md",
        include_patterns=["https://modelcontextprotocol.io/*"],
        # Both specification/ and docs/ are versioned by revision date.
        exclude_patterns=[f"*/{tree}/{v}*" for tree in ("specification", "docs")
                          for v in ("2024-", "2025-", "draft")] + ["*/community/*"],
        name_prefix="MCP",
        authority_score=0.85,
        refresh_ttl_days=14,
    ),
    # agentskills.io: the open Agent Skills specification and guides.
    "agent-skills": Site(
        name="agent-skills",
        index_url="https://agentskills.io/llms.txt",
        index_kind="llms_txt",
        page_format="markdown",
        md_url_suffix=".md",
        include_patterns=["https://agentskills.io/*"],
        name_prefix="Agent Skills",
        authority_score=0.85,
        refresh_ttl_days=14,
    ),
}


def selector_for(url: str) -> str:
    """The content selector of the preset whose pages include ``url``."""
    for site in PRESETS.values():
        if site.content_selector and site.include_patterns and site.wants(url):
            return site.content_selector
    return "article"


def load_sites(conn: duckdb.DuckDBPyConnection, name: Optional[str] = None) -> list[Site]:
    if not _schema_present(conn):
        return []
    sql = f"SELECT {', '.join(_SITE_COLS)} FROM doc_site"
    params: list[Any] = []
    if name:
        sql += " WHERE name = ?"
        params.append(name)
    rows = conn.execute(sql + " ORDER BY site_id", params).fetchall()
    sites = []
    for r in rows:
        d = dict(zip(_SITE_COLS, r))
        d["include_patterns"] = list(d["include_patterns"] or [])
        d["exclude_patterns"] = list(d["exclude_patterns"] or [])
        sites.append(Site(**d))
    return sites


def upsert_site(conn: duckdb.DuckDBPyConnection, site: Site) -> int:
    ensure_schema(conn)
    vals = [site.index_url, site.index_kind, site.page_format, site.md_url_suffix,
            site.content_selector, site.include_patterns, site.exclude_patterns,
            site.name_prefix, site.authority_score, site.refresh_ttl_days,
            site.max_workers, site.request_interval_s]
    row = conn.execute("SELECT site_id FROM doc_site WHERE name = ?", [site.name]).fetchone()
    if row:
        conn.execute(
            "UPDATE doc_site SET index_url=?, index_kind=?, page_format=?, md_url_suffix=?, "
            "content_selector=?, include_patterns=?, exclude_patterns=?, name_prefix=?, "
            "authority_score=?, refresh_ttl_days=?, max_workers=?, request_interval_s=? "
            "WHERE site_id=?", vals + [row[0]])
        return int(row[0])
    row = conn.execute(
        "INSERT INTO doc_site (name, index_url, index_kind, page_format, md_url_suffix, "
        "content_selector, include_patterns, exclude_patterns, name_prefix, authority_score, "
        "refresh_ttl_days, max_workers, request_interval_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING site_id",
        [site.name] + vals).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Index readers
# ---------------------------------------------------------------------------

_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_MD_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from a urlset or sitemapindex."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    tag = root.tag.replace(_SM_NS, "")
    locs = [(el.text or "").strip() for el in root.iter(f"{_SM_NS}loc")] or \
           [(el.text or "").strip() for el in root.iter("loc")]
    locs = [u for u in locs if u]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


def parse_llms_txt(text: str, index_url: str) -> list[str]:
    """Markdown links in an llms.txt that stay on the index's host."""
    host = urlparse(index_url).netloc
    seen: dict[str, None] = {}
    for url in _MD_LINK.findall(text):
        url = url.split("#", 1)[0]
        if urlparse(url).netloc == host:
            seen.setdefault(url, None)
    return list(seen)


def _client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def read_index(site: Site, client: Optional[httpx.Client] = None) -> list[str]:
    """All page URLs the site's index lists, filtered by include/exclude, de-duplicated."""
    own = client is None
    client = client or _client()
    try:
        urls: list[str] = []
        if site.index_kind == "llms_txt":
            r = client.get(site.index_url)
            r.raise_for_status()
            urls = parse_llms_txt(r.text, site.index_url)
        elif site.index_kind == "sitemap":
            queue, seen_maps = [site.index_url], set()
            while queue:
                sm = queue.pop(0)
                if sm in seen_maps:
                    continue
                seen_maps.add(sm)
                r = client.get(sm)
                r.raise_for_status()
                pages, children = parse_sitemap(r.text)
                urls.extend(pages)
                queue.extend(children)
        else:
            raise ValueError(f"unknown index_kind {site.index_kind!r}")
    finally:
        if own:
            client.close()
    return list(dict.fromkeys(u for u in urls if site.wants(u)))


# ---------------------------------------------------------------------------
# Page fetching (conditional GET) and HTML → Markdown
# ---------------------------------------------------------------------------


@dataclass
class PageFetch:
    page_url: str
    fetch_url: str
    status: str                  # ok | not_modified | gone | error
    http_status: Optional[int] = None
    content: Optional[str] = None
    title: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None
    content_date: Optional[str] = None   # the page's own "last updated" stamp


# Inside the article: widgets that would otherwise land in the text.
# Docusaurus (docs.databricks.com) wraps every table in an expand button
# plus a <dialog> holding a second copy of the table; heading anchors carry
# a zero-width space; icons are inline SVGs.
_DROP_IN_ARTICLE = ("dialog", "button", "svg", "a.hash-link", "script", "style", "noscript")


def _clean_article(el) -> None:
    for sel in _DROP_IN_ARTICLE:
        for junk in el.select(sel):
            junk.decompose()
    # html_doc drops <header>/<footer> as page chrome; inside the article a
    # <header> wraps the page's own <h1>.
    for wrapper in el.find_all("header"):
        wrapper.unwrap()
    # Admonition titles ("Preview", "Beta", "Note") are bare <div> text, which
    # html_doc skips; they carry release status, so make them paragraphs.
    for head in el.select("div.theme-admonition [class*=admonitionHeading]"):
        head.name = "p"
        label = head.get_text(" ", strip=True)
        if label:
            head.string = label[:1].upper() + label[1:] + ":"
    # Code-tab widgets: the tab labels ("Python", "SQL") would become stray
    # bullets; the language moves onto the fence instead (see _tidy_spacing).
    for tablist in el.select("[role=tablist]"):
        tablist.decompose()
    # Feature tables mark support with check-icon images; keep the meaning.
    for img in el.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if re.search(r"check|yes", alt, re.I):
            img.replace_with("✓")
        elif re.search(r"red x|\bx icon|cross|not supported|\bno\b", alt, re.I):
            img.replace_with("✗")
        else:
            img.replace_with(alt and f"[{alt}]")
    # Prism renders code one <div class="token-line"> per line with <br>;
    # get_text() would fuse the lines.
    for pre in el.find_all("pre"):
        classes = " ".join(pre.get("class", []) + sum(
            (c.get("class", []) for c in pre.find_all("code")), []))
        m = re.search(r"language-([\w+-]+)", classes)
        if m:
            pre.insert(0, f"\x00lang={m.group(1)}\n")
        for br in pre.find_all("br"):
            br.replace_with("\n")
        for line in pre.select("div.token-line, span.token-line"):
            if not line.get_text().endswith("\n"):
                line.append("\n")
    # html_doc emits an <li> with its full text, nested lists included, and
    # then each nested item again. Lift nested lists out to follow their
    # parent item (deepest first) so every item is emitted once.
    for li in reversed(el.find_all("li")):
        for sub in reversed(li.find_all(["ul", "ol"], recursive=False)):
            li.insert_after(sub.extract())


def _main_html(html: str, selector: Optional[str]) -> tuple[str, Optional[str]]:
    """(article HTML, content date) — the first matching container, cleaned.

    The content date is the page's own "Last updated" stamp
    (``time[itemprop=dateModified]``), which on Docusaurus sites sits next to
    the article body, not in it. It's metadata, not part of the hash.
    """
    from bs4 import BeautifulSoup
    # lxml, not html.parser: minified docs HTML omits optional end tags
    # (</tr>, </td>, </p>), and html.parser nests every row inside the
    # previous one, which turns tables into run-on text.
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # bs4.FeatureNotFound when lxml isn't installed
        soup = BeautifulSoup(html, "html.parser")
    t = soup.select_one("time[itemprop=dateModified][datetime]") or \
        soup.select_one(".theme-last-updated time[datetime]")
    content_date = t.get("datetime") if t else None
    for sel in [s for s in (selector, "article", "main", "[role=main]") if s]:
        el = soup.select_one(sel)
        if el is not None:
            _clean_article(el)
            return str(el), content_date
    return html, content_date


def html_page_to_markdown(html: str, selector: Optional[str] = None,
                          ) -> tuple[str, str, Optional[str]]:
    """(page title, markdown, content date) for the article part of a docs page."""
    from bs4 import BeautifulSoup
    import html_doc
    title_el = BeautifulSoup(html, "html.parser").title
    page_title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
    body, content_date = _main_html(html, selector)
    _, md = html_doc.html_to_markdown(body, max_cell=None)
    md = _tidy_spacing(md.replace("\u200b", ""))
    return page_title, md, content_date


_SPACE_BEFORE_PUNCT = re.compile(r"(?<=\S) +([.,;:!?)\]])(?=\s|$)")
_SPACE_AFTER_OPEN = re.compile(r"([(\[]) +(?=\S)")


def _tidy_spacing(md: str) -> str:
    """Undo the space html_doc puts before punctuation after inline elements
    ("in Databricks ." → "in Databricks."), outside code fences only."""
    md = _FENCE_LANG.sub(r"```\1\n", md)
    out, in_fence = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            line = _SPACE_AFTER_OPEN.sub(r"\1", _SPACE_BEFORE_PUNCT.sub(r"\1", line))
            # A paragraph right after a list item needs a blank line, or
            # Markdown reads it as part of the item.
            if (out and line.strip() and out[-1].startswith("- ")
                    and not line.startswith(("- ", "  ", "|", "#"))):
                out.append("")
        out.append(line)
    return "\n".join(out)


_FENCE_LANG = re.compile(r"```\n\x00lang=([\w+-]+)\n")


def markdown_title(md: str) -> Optional[str]:
    m = re.search(r"^#\s+(.+)$", md, flags=re.M)
    if m:
        return m.group(1).strip()
    m = re.search(r"^title:\s*(.+)$", md[:500], flags=re.M)
    return m.group(1).strip().strip("'\"") if m else None


def fetch_page(site: Site, page_url: str, *, etag: Optional[str] = None,
               last_modified: Optional[str] = None,
               client: Optional[httpx.Client] = None) -> PageFetch:
    furl = site.fetch_url(page_url)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    own = client is None
    client = client or _client()
    try:
        r = client.get(furl, headers=headers)
    except httpx.HTTPError as e:
        return PageFetch(page_url, furl, "error", error=f"transport: {e}")
    finally:
        if own:
            client.close()
    pf = PageFetch(page_url, furl, "ok", http_status=r.status_code,
                   etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"))
    if r.status_code == 304:
        pf.status = "not_modified"
        pf.etag, pf.last_modified = pf.etag or etag, pf.last_modified or last_modified
        return pf
    if r.status_code in (404, 410):
        pf.status = "gone"
        return pf
    if r.status_code >= 400:
        pf.status, pf.error = "error", f"HTTP {r.status_code}"
        return pf
    text = r.text
    looks_html = "html" in r.headers.get("content-type", "").lower() or \
        text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
    if site.page_format == "markdown":
        if looks_html:
            pf.status, pf.error = "error", "expected Markdown, got HTML"
            return pf
        import refresh_docs
        text = refresh_docs.WebMarkdownFetcher.strip_index_note(text)
        pf.title = markdown_title(text)
    else:
        page_title, text, pf.content_date = html_page_to_markdown(text, site.content_selector)
        pf.title = markdown_title(text) or page_title
    if len(text.split()) < 20:
        pf.status, pf.error = "error", "page has almost no text (needs JavaScript?)"
        return pf
    pf.content = text
    return pf


class _RateLimiter:
    """At most one request per ``interval / workers`` seconds across threads."""

    def __init__(self, per_request_s: float):
        self.gap = max(per_request_s, 0.0)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.gap
        if delay > 0:
            time.sleep(delay)


def fetch_many(site: Site, jobs: list[tuple[str, Optional[str], Optional[str]]],
               ) -> Iterator[PageFetch]:
    """Fetch (page_url, etag, last_modified) jobs politely in parallel; yields in order."""
    if not jobs:
        return
    limiter = _RateLimiter(site.request_interval_s / max(site.max_workers, 1))
    local = threading.local()

    def one(job):
        if not hasattr(local, "client"):
            local.client = _client()
        limiter.wait()
        url, etag, lm = job
        return fetch_page(site, url, etag=etag, last_modified=lm, client=local.client)

    with ThreadPoolExecutor(max_workers=max(site.max_workers, 1)) as pool:
        yield from pool.map(one, jobs)


# ---------------------------------------------------------------------------
# Planning (read-only)
# ---------------------------------------------------------------------------


@dataclass
class PageState:
    doc_source_id: int
    page_url: str
    etag: Optional[str]
    last_modified: Optional[str]
    content_hash: Optional[str]
    last_checked_at: Optional[datetime]
    retired_at: Optional[datetime]


def page_states(conn: duckdb.DuckDBPyConnection, site_id: int) -> dict[str, PageState]:
    if not _schema_present(conn):
        return {}
    rows = conn.execute(
        "SELECT doc_source_id, page_url, etag, last_modified, content_hash, last_checked_at, "
        "retired_at FROM doc_fetch_state WHERE site_id = ?", [site_id]).fetchall()
    return {r[1]: PageState(*r) for r in rows}


@dataclass
class Plan:
    site: str
    index_pages: int
    new: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    revived: list[str] = field(default_factory=list)
    never_fetched: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    fresh: int = 0
    checked: dict[str, list[str]] = field(default_factory=dict)  # --check results
    retire_blocked: bool = False

    def summary(self) -> dict[str, Any]:
        d = {k: (len(v) if isinstance(v, list) else v) for k, v in asdict(self).items()
             if k != "checked"}
        d["checked"] = {k: len(v) for k, v in self.checked.items()}
        return d


def build_plan(site: Site, index_urls: list[str], states: dict[str, PageState],
               *, now: Optional[datetime] = None) -> Plan:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=site.refresh_ttl_days)
    plan = Plan(site=site.name, index_pages=len(index_urls))
    in_index = set(index_urls)
    for url in index_urls:
        st = states.get(url)
        if st is None:
            plan.new.append(url)
        elif st.retired_at is not None:
            plan.revived.append(url)
        elif st.last_checked_at is None or st.content_hash is None:
            plan.never_fetched.append(url)
        elif st.last_checked_at < cutoff:
            plan.overdue.append(url)
        else:
            plan.fresh += 1
    live = [u for u, st in states.items() if st.retired_at is None]
    plan.removed = sorted(u for u in live if u not in in_index)
    # Measured against the larger of the known and the indexed page counts:
    # an index that suddenly lists far fewer pages trips it, a first sync
    # that adopts a few seeded pages next to hundreds of new ones doesn't.
    if live and len(plan.removed) > MAX_RETIRE_SHARE * max(len(live), len(in_index)):
        plan.retire_blocked = True
    return plan


def check_changes(site: Site, urls: list[str], states: dict[str, PageState]) -> dict[str, list[str]]:
    """Conditional-GET each known page; classify without writing anything."""
    import refresh_docs
    out: dict[str, list[str]] = {"not_modified": [], "unchanged": [], "changed": [],
                                 "gone": [], "error": []}
    jobs = [(u, states[u].etag, states[u].last_modified) for u in urls if u in states]
    for pf in fetch_many(site, jobs):
        if pf.status == "ok":
            same = refresh_docs.compute_content_hash(pf.content) == states[pf.page_url].content_hash
            out["unchanged" if same else "changed"].append(pf.page_url)
        else:
            out[pf.status].append(pf.page_url)
    return out


# ---------------------------------------------------------------------------
# Sync (writer)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# A site section that is its own product gets its own name prefix.
PREFIX_BY_URL = {
    "https://code.claude.com/docs/en/agent-sdk/": "Claude Agent SDK",
    "https://platform.claude.com/docs/en/api/": "Claude API",
    "https://platform.claude.com/docs/en/managed-agents/": "Claude Managed Agents",
}


def _page_name(site: Site, title: Optional[str], url: str) -> str:
    label = title or urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or url
    prefix = next((p for u, p in PREFIX_BY_URL.items() if url.startswith(u)), site.name_prefix)
    return f"{prefix}: {label}" if prefix else label


def register_page(conn: duckdb.DuckDBPyConnection, site: Site, page_url: str) -> int:
    """Create (or adopt) the doc_source for a page and its doc_fetch_state row."""
    furl = site.fetch_url(page_url)
    row = conn.execute(
        "SELECT doc_source_id FROM doc_source WHERE identifier IN (?, ?) "
        "AND source_type IN ('web_md', 'web_html') ORDER BY doc_source_id LIMIT 1",
        [furl, page_url]).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
            "authority_score, refresh_ttl_days, priority_tier) "
            "VALUES (?, ?, 'web', ?, ?, ?, 'cool') RETURNING doc_source_id",
            [_page_name(site, None, page_url), site.source_type, furl,
             site.authority_score, site.refresh_ttl_days]).fetchone()
    dsid = int(row[0])
    # Adopting a source that already has live sections: start from its
    # latest hash so an unchanged page isn't re-ingested. A retired page
    # has no sections left, so it starts from scratch.
    prior_hash = conn.execute(
        "SELECT sn.content_hash FROM doc_snapshot sn WHERE sn.doc_source_id = ? "
        "AND EXISTS (SELECT 1 FROM doc_section s WHERE s.snapshot_id = sn.snapshot_id) "
        "ORDER BY sn.retrieved_at DESC, sn.snapshot_id DESC LIMIT 1", [dsid]).fetchone()
    conn.execute(
        "INSERT INTO doc_fetch_state (doc_source_id, site_id, page_url, fetch_url, "
        "content_hash, last_seen_in_index, last_status) VALUES (?, ?, ?, ?, ?, ?, 'new') "
        "ON CONFLICT (doc_source_id) DO UPDATE SET site_id = excluded.site_id, "
        "page_url = excluded.page_url, fetch_url = excluded.fetch_url, retired_at = NULL, "
        "content_hash = excluded.content_hash, etag = NULL, last_modified = NULL",
        [dsid, site.site_id, page_url, furl, prior_hash[0] if prior_hash else None, _now()])
    return dsid


def adopt_existing_sources(conn: duckdb.DuckDBPyConnection, site: Site) -> int:
    """Bring single-page sources seeded earlier (seed_agent_standards' Claude
    Code pages) under the site, so the sync refreshes them — or retires
    them when their URL has left the index. Only for sites with include
    patterns, so a site never claims every web page in the catalog."""
    if not site.include_patterns:
        return 0
    rows = conn.execute(
        "SELECT doc_source_id, identifier FROM doc_source "
        "WHERE source_type IN ('web_md', 'web_html') AND doc_source_id NOT IN "
        "(SELECT doc_source_id FROM doc_fetch_state)").fetchall()
    n = 0
    for dsid, ident in rows:
        if site.wants(ident):
            prior = conn.execute(
                "SELECT sn.content_hash FROM doc_snapshot sn WHERE sn.doc_source_id = ? "
                "AND EXISTS (SELECT 1 FROM doc_section s WHERE s.snapshot_id = sn.snapshot_id) "
                "ORDER BY sn.retrieved_at DESC, sn.snapshot_id DESC LIMIT 1", [dsid]).fetchone()
            conn.execute(
                "INSERT INTO doc_fetch_state (doc_source_id, site_id, page_url, fetch_url, "
                "content_hash, last_status) VALUES (?, ?, ?, ?, ?, 'adopted')",
                [dsid, site.site_id, ident, ident, prior[0] if prior else None])
            n += 1
    return n


def _record(conn, dsid: int, pf: PageFetch, status: str, *, content_hash=None,
            changed=False) -> None:
    now = _now()
    conn.execute(
        "UPDATE doc_fetch_state SET last_checked_at = ?, last_status = ?, "
        "last_http_status = ?, last_error = ?, "
        "etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified), "
        "content_hash = COALESCE(?, content_hash), content_date = COALESCE(?, content_date), "
        "last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END "
        "WHERE doc_source_id = ?",
        [now, status, pf.http_status, pf.error, pf.etag, pf.last_modified, content_hash,
         pf.content_date, changed, now, dsid])


def retire_page(conn: duckdb.DuckDBPyConnection, dsid: int) -> int:
    import refresh_docs
    n = refresh_docs.retire_source_sections(conn, dsid)
    conn.execute("UPDATE doc_fetch_state SET retired_at = ?, last_status = 'retired' "
                 "WHERE doc_source_id = ?", [_now(), dsid])
    return n


@dataclass
class SyncReport:
    run_id: str
    site: str
    plan: dict[str, Any]
    counts: dict[str, int] = field(default_factory=dict)
    new_sections: int = 0
    kept_sections: int = 0
    dropped_sections: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    extraction_dirs: list[str] = field(default_factory=list)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


def sync_site(
    conn: duckdb.DuckDBPyConnection,
    site: Site,
    *,
    embedder: Any,
    index_urls: Optional[list[str]] = None,
    only: Optional[set[str]] = None,
    max_pages: Optional[int] = None,
    force_retire: bool = False,
    prep_extract_dir: Optional[Path] = None,
    rebuild_fts: bool = True,
    paths: Optional[list[str]] = None,
    force: bool = False,
    fetcher=fetch_many,
) -> SyncReport:
    """Bring one site's pages in line with its index. See module docstring."""
    import refresh_docs
    ensure_schema(conn)
    run_id = f"{site.name}-{_now().strftime('%Y%m%dT%H%M%S%f')}"
    started = _now()
    index_urls = read_index(site) if index_urls is None else index_urls
    conn.execute("UPDATE doc_site SET last_index_at = ? WHERE site_id = ?",
                 [_now(), site.site_id])
    adopted = adopt_existing_sources(conn, site)
    states = page_states(conn, site.site_id)
    plan = build_plan(site, index_urls, states)
    report = SyncReport(run_id=run_id, site=site.name, plan=plan.summary())
    if adopted:
        report.bump("adopted", adopted)
    only = only or {"new", "changed", "removed"}

    # 1. Register pages new to us (or back in the index after being retired).
    if "new" in only:
        for url in plan.new + plan.revived:
            register_page(conn, site, url)
            report.bump("registered")
        states = page_states(conn, site.site_id)
    now = _now()
    conn.execute(
        "UPDATE doc_fetch_state SET last_seen_in_index = ? WHERE site_id = ? AND page_url IN "
        "(SELECT unnest(?::VARCHAR[]))", [now, site.site_id, index_urls])

    # 2. Retire pages gone from the index.
    if "removed" in only and plan.removed:
        if plan.retire_blocked and not force_retire:
            log.warning("%s: %d pages missing from the index (>%d%% of live pages); "
                        "not retiring — re-run with --force-retire if that's real",
                        site.name, len(plan.removed), int(MAX_RETIRE_SHARE * 100))
            report.bump("retire_blocked", len(plan.removed))
        else:
            for url in plan.removed:
                report.dropped_sections += retire_page(conn, states[url].doc_source_id)
                report.bump("retired")

    # 3. Fetch: new + never fetched first, then oldest-checked.
    in_index = set(index_urls)
    candidates = [st for u, st in states.items() if u in in_index and st.retired_at is None]
    if paths:
        # Glob on the page URL; a bare fragment like "sql/" matches anywhere.
        pats = [p if any(c in p for c in "*?[") else f"*{p}*" for p in paths]
        candidates = [st for st in candidates
                      if any(fnmatch.fnmatchcase(st.page_url, p) for p in pats)]
    if "changed" not in only:
        candidates = [st for st in candidates if st.content_hash is None]
    if force:
        # --force re-ingests pages already in the catalog; never-fetched pages
        # would otherwise sort first and use up --max-pages.
        candidates = [st for st in candidates if st.content_hash is not None]
    candidates.sort(key=lambda st: (st.content_hash is not None,
                                    st.last_checked_at or datetime.min))
    if max_pages is not None:
        candidates = candidates[:max_pages]
    by_url = {st.page_url: st for st in candidates}
    # --force: no conditional headers and no hash short-cut, so every selected
    # page is re-ingested (unchanged sections still carry over — it's cheap).
    jobs = [(st.page_url, st.etag if st.content_hash and not force else None,
             st.last_modified if st.content_hash and not force else None) for st in candidates]

    for pf in fetcher(site, jobs):
        st = by_url[pf.page_url]
        dsid = st.doc_source_id
        if pf.status == "not_modified":
            _record(conn, dsid, pf, "not_modified")
            conn.execute("UPDATE doc_source SET last_refresh_at = CURRENT_TIMESTAMP "
                         "WHERE doc_source_id = ?", [dsid])
            report.bump("not_modified")
            continue
        if pf.status == "gone":
            report.dropped_sections += retire_page(conn, dsid)
            report.bump("retired")
            continue
        if pf.status == "error":
            _record(conn, dsid, pf, "error")
            report.bump("error")
            report.errors.append({"url": pf.page_url, "error": pf.error or ""})
            continue
        content_hash = refresh_docs.compute_content_hash(pf.content)
        if content_hash == st.content_hash and not force:
            _record(conn, dsid, pf, "unchanged")
            conn.execute("UPDATE doc_source SET last_refresh_at = CURRENT_TIMESTAMP "
                         "WHERE doc_source_id = ?", [dsid])
            report.bump("unchanged")
            continue
        # Autocommit, not one transaction per page: DuckDB checks FKs against
        # the transaction's start state, so the delete-then-update steps in
        # ingest_snapshot fail inside BEGIN. A page interrupted mid-ingest is
        # healed by the next sync — its hash is only recorded on success, so
        # it is re-ingested and the partial snapshot superseded.
        try:
            # Name only: identifier is in a UNIQUE index, and DuckDB can't
            # update an indexed column of a row doc_snapshot references.
            conn.execute("UPDATE doc_source SET name = ? WHERE doc_source_id = ? AND name <> ?",
                         [_page_name(site, pf.title, pf.page_url), dsid,
                          _page_name(site, pf.title, pf.page_url)])
            ing = refresh_docs.ingest_snapshot(
                conn, dsid,
                refresh_docs.FetchResult(url=pf.fetch_url, content=pf.content,
                                         source_type="site_md"),
                snapshot_source_type=site.source_type, content_hash=content_hash,
                embedder=embedder, rebuild_fts=False, embed_context=True)
            _record(conn, dsid, pf, "changed" if st.content_hash else "new",
                    content_hash=content_hash, changed=True)
        except Exception as e:  # keep going; one bad page shouldn't stop a crawl
            log.exception("ingest failed for %s", pf.page_url)
            report.bump("error")
            report.errors.append({"url": pf.page_url, "error": f"ingest: {e}"})
            continue
        report.bump("ingested_new" if st.content_hash is None else "changed")
        report.new_sections += len(ing.new_section_ids)
        report.kept_sections += len(ing.kept_section_ids)
        report.dropped_sections += ing.dropped_sections
        if prep_extract_dir is not None and ing.new_section_ids:
            sub = Path(prep_extract_dir) / f"snapshot_{ing.snapshot_id}"
            refresh_docs.prep_extraction(conn, ing.snapshot_id, sub,
                                         section_ids=ing.new_section_ids)
            report.extraction_dirs.append(str(sub))
        done = sum(report.counts.get(k, 0) for k in
                   ("not_modified", "unchanged", "changed", "ingested_new", "error", "retired"))
        if done % 100 == 0:
            log.info("%s: %d/%d pages processed %s", site.name, done, len(jobs), report.counts)

    touched = report.counts.get("changed", 0) + report.counts.get("ingested_new", 0) + \
        report.counts.get("retired", 0)
    if rebuild_fts and touched:
        log.info("%s: rebuilding the doc_section FTS index", site.name)
        refresh_docs.rebuild_doc_section_fts_index(conn)
    conn.execute("UPDATE doc_site SET last_sync_at = ? WHERE site_id = ?",
                 [_now(), site.site_id])
    conn.execute("INSERT INTO doc_refresh_run (run_id, site_id, started_at, finished_at, counts) "
                 "VALUES (?, ?, ?, ?, ?)",
                 [run_id, site.site_id, started, _now(), json.dumps(report.counts)])
    return report


def reembed_site(conn: duckdb.DuckDBPyConnection, site: Site, *, embedder: Any,
                 batch: int = 512) -> int:
    """Re-embed every live section of a site as 'page › heading path' + body.

    For sections that were embedded before context embeddings existed (or
    carried over from such a section), and after a change to the embedding
    text. Provenance is untouched; only vectors are replaced.
    """
    import refresh_docs
    rows = conn.execute(
        """
        SELECT s.doc_section_id, s.parent_id, s.heading_text, s.content, src.name
          FROM doc_section s
          JOIN doc_snapshot sn USING (snapshot_id)
          JOIN doc_source src ON src.doc_source_id = sn.doc_source_id
          JOIN doc_fetch_state f ON f.doc_source_id = sn.doc_source_id
         WHERE f.site_id = ? AND f.retired_at IS NULL
         ORDER BY s.doc_section_id
        """, [site.site_id]).fetchall()
    by_id = {r[0]: r for r in rows}

    def path(sid: int) -> list[str]:
        out, seen = [], set()
        while sid in by_id and sid not in seen:
            seen.add(sid)
            _, parent, heading, _, _ = by_id[sid]
            if heading:
                out.append(heading)
            sid = parent
        return out[::-1]

    done = 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        ids = [r[0] for r in chunk]
        texts = [refresh_docs.section_embedding_text(r[4], path(r[0]), r[3] or "") for r in chunk]
        vectors = embedder.encode(texts, show_progress_bar=False)
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM doc_section_embedding WHERE doc_section_id IN ({ph})", ids)
        refresh_docs.write_section_embeddings(conn, ids, vectors)
        done += len(ids)
        if (start // batch) % 20 == 0:
            log.info("%s: re-embedded %d/%d sections", site.name, done, len(rows))
    return done


# ---------------------------------------------------------------------------
# Status / stale (read-only)
# ---------------------------------------------------------------------------


def site_status(conn: duckdb.DuckDBPyConnection, site: Site) -> dict[str, Any]:
    cutoff = _now() - timedelta(days=site.refresh_ttl_days)
    r = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE retired_at IS NULL),
               COUNT(*) FILTER (WHERE retired_at IS NOT NULL),
               COUNT(*) FILTER (WHERE retired_at IS NULL AND content_hash IS NULL),
               COUNT(*) FILTER (WHERE retired_at IS NULL AND last_checked_at < ?),
               COUNT(*) FILTER (WHERE retired_at IS NULL AND last_status = 'error'),
               MIN(last_checked_at) FILTER (WHERE retired_at IS NULL)
          FROM doc_fetch_state WHERE site_id = ?
        """, [cutoff, site.site_id]).fetchone()
    sections = conn.execute(
        "SELECT COUNT(*) FROM doc_section s JOIN doc_snapshot sn USING (snapshot_id) "
        "JOIN doc_fetch_state f ON f.doc_source_id = sn.doc_source_id WHERE f.site_id = ?",
        [site.site_id]).fetchone()[0]
    return {"site": site.name, "live_pages": r[0], "retired": r[1], "never_fetched": r[2],
            "overdue": r[3], "errors": r[4], "oldest_check": str(r[5]) if r[5] else None,
            "sections": sections, "last_sync": str(site.last_sync_at) if site.last_sync_at else None,
            "ttl_days": site.refresh_ttl_days}


def stale_sources(conn: duckdb.DuckDBPyConnection, *, domain: Optional[str] = None,
                  source_type: Optional[str] = None,
                  include_site_pages: bool = False) -> list[tuple]:
    """doc_source rows past their TTL (or never refreshed). Read-only."""
    sql = """
        SELECT d.doc_source_id, d.name, d.source_type, d.identifier, d.last_refresh_at,
               d.refresh_ttl_days
          FROM doc_source d
         WHERE (d.last_refresh_at IS NULL
                OR d.last_refresh_at < CURRENT_TIMESTAMP - to_days(COALESCE(d.refresh_ttl_days, 14)))
    """
    params: list[Any] = []
    if domain:
        sql += " AND d.identifier LIKE ?"
        params.append(f"%{domain}%")
    if source_type:
        sql += " AND d.source_type = ?"
        params.append(source_type)
    if not include_site_pages and _schema_present(conn):
        sql += (" AND d.doc_source_id NOT IN (SELECT doc_source_id FROM doc_fetch_state "
                "WHERE site_id IS NOT NULL)")
    return conn.execute(sql + " ORDER BY d.last_refresh_at NULLS FIRST", params).fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ro(catalog: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(catalog), read_only=True)


def _sites_or_die(conn, args) -> list[Site]:
    sites = load_sites(conn, None if getattr(args, "all", False) else args.site)
    if not sites:
        raise SystemExit(f"no doc_site named {args.site!r}; run add-site first"
                         if args.site else "no doc sites registered; run add-site first")
    return sites


def _out_dir(args, run: str) -> Path:
    d = args.output_dir or PROJECT_ROOT / "data" / "refresh" / run
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_add_site(args) -> int:
    import refresh_docs
    if args.preset:
        site = PRESETS[args.preset]
    else:
        if not (args.name and args.index_url):
            raise SystemExit("give --preset or both --name and --index-url")
        site = Site(name=args.name, index_url=args.index_url,
                    index_kind="llms_txt" if args.index_url.endswith(".txt") else "sitemap",
                    page_format=args.page_format, md_url_suffix=args.md_url_suffix,
                    content_selector=args.selector, include_patterns=args.include or [],
                    exclude_patterns=args.exclude or [], name_prefix=args.name_prefix,
                    authority_score=args.authority, refresh_ttl_days=args.ttl)
    if args.exclude and args.preset:
        site.exclude_patterns = site.exclude_patterns + args.exclude
    conn = refresh_docs.open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        sid = upsert_site(conn, site)
    finally:
        conn.close()
    print(f"site {site.name}: site_id={sid}")
    return 0


def cmd_plan(args) -> int:
    conn = _ro(args.catalog)
    try:
        sites = _sites_or_die(conn, args)
        out = []
        for site in sites:
            urls = read_index(site)
            states = page_states(conn, site.site_id)
            plan = build_plan(site, urls, states)
            if args.check:
                known = [u for u in urls if u in states and states[u].content_hash]
                known.sort(key=lambda u: states[u].last_checked_at or datetime.min)
                if args.limit:
                    known = known[: args.limit]
                plan.checked = check_changes(site, known, states)
            out.append(plan)
    finally:
        conn.close()
    d = _out_dir(args, "plan_" + _now().strftime("%Y%m%dT%H%M%S"))
    (d / "plan.json").write_text(json.dumps([asdict(p) for p in out], indent=2, default=str))
    for p in out:
        print(json.dumps(p.summary()))
    print(f"full lists: {d / 'plan.json'}")
    return 0


def cmd_sync(args) -> int:
    import refresh_docs
    conn = refresh_docs.open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        sites = _sites_or_die(conn, args)
        embedder = refresh_docs._load_default_embedder()
        d = _out_dir(args, "sync_" + _now().strftime("%Y%m%dT%H%M%S"))
        reports = []
        for site in sites:
            rep = sync_site(
                conn, site, embedder=embedder,
                only=set(args.only) if args.only else None,
                max_pages=args.max_pages, force_retire=args.force_retire,
                paths=args.path, force=args.force,
                prep_extract_dir=(d / site.name) if args.prep_extract else None,
                rebuild_fts=False,
            )
            reports.append(rep)
            print(json.dumps({"site": rep.site, "counts": rep.counts,
                              "sections": {"new": rep.new_sections, "kept": rep.kept_sections,
                                           "dropped": rep.dropped_sections},
                              "errors": len(rep.errors)}))
        if any(r.counts.get(k) for r in reports for k in ("changed", "ingested_new", "retired")):
            log.info("rebuilding the doc_section FTS index")
            refresh_docs.rebuild_doc_section_fts_index(conn)
        (d / "sync_report.json").write_text(
            json.dumps([asdict(r) for r in reports], indent=2, default=str))
        print(f"report: {d / 'sync_report.json'}")
    finally:
        conn.close()
    return 0


def cmd_reembed(args) -> int:
    import refresh_docs
    conn = refresh_docs.open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        embedder = refresh_docs._load_default_embedder()
        for site in _sites_or_die(conn, args):
            n = reembed_site(conn, site, embedder=embedder)
            print(json.dumps({"site": site.name, "reembedded": n}))
    finally:
        conn.close()
    return 0


def cmd_status(args) -> int:
    conn = _ro(args.catalog)
    try:
        if not _schema_present(conn):
            print("no doc sites registered yet")
            return 0
        for site in load_sites(conn, args.site):
            print(json.dumps(site_status(conn, site)))
    finally:
        conn.close()
    return 0


def cmd_stale(args) -> int:
    conn = _ro(args.catalog)
    try:
        rows = stale_sources(conn, domain=args.domain, source_type=args.source_type,
                             include_site_pages=args.include_site_pages)
    finally:
        conn.close()
    for r in rows:
        print(f"{r[0]:>6}  {r[2]:<11} {str(r[4] or 'never')[:16]:<16} ttl={r[5]}  {r[1]}")
    print(f"{len(rows)} source(s) past their refresh TTL")
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-site")
    a.add_argument("--preset", choices=sorted(PRESETS))
    a.add_argument("--name")
    a.add_argument("--index-url")
    a.add_argument("--page-format", choices=("markdown", "html"), default="html")
    a.add_argument("--md-url-suffix")
    a.add_argument("--selector")
    a.add_argument("--include", action="append")
    a.add_argument("--exclude", action="append",
                   help="glob on page URL; with --preset, added to the preset's list")
    a.add_argument("--name-prefix")
    a.add_argument("--authority", type=float, default=0.85)
    a.add_argument("--ttl", type=int, default=14)
    a.add_argument("--no-auto-stop", action="store_true")

    for name in ("plan", "sync"):
        s = sub.add_parser(name)
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--site")
        g.add_argument("--all", action="store_true")
        s.add_argument("--output-dir", type=Path)
        if name == "plan":
            s.add_argument("--check", action="store_true",
                           help="conditional-GET known pages to see which changed (no writes)")
            s.add_argument("--limit", type=int, help="with --check: oldest-checked N pages only")
        else:
            s.add_argument("--max-pages", type=int)
            s.add_argument("--path", action="append",
                           help="only pages whose URL matches (glob, or a fragment like 'sql/')")
            s.add_argument("--force", action="store_true",
                           help="re-ingest already-fetched pages even if unchanged (skips never-fetched pages)")
            s.add_argument("--only", action="append", choices=("new", "changed", "removed"))
            s.add_argument("--force-retire", action="store_true")
            s.add_argument("--prep-extract", action="store_true",
                           help="write sub-agent extraction manifests for new/changed sections")
            s.add_argument("--no-auto-stop", action="store_true")

    re_ = sub.add_parser("reembed", help="re-embed a site's live sections with page/heading context")
    g = re_.add_mutually_exclusive_group(required=True)
    g.add_argument("--site")
    g.add_argument("--all", action="store_true")
    re_.add_argument("--no-auto-stop", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("--site")

    sl = sub.add_parser("stale")
    sl.add_argument("--domain")
    sl.add_argument("--source-type")
    sl.add_argument("--include-site-pages", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    args = _parse_args(argv)
    return {"add-site": cmd_add_site, "plan": cmd_plan, "sync": cmd_sync,
            "status": cmd_status, "stale": cmd_stale,
            "reembed": cmd_reembed}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
