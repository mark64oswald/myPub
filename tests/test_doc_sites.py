"""Tests for doc-site refresh (scripts/doc_sites.py) and snapshot supersession
(refresh_docs.ingest_snapshot). No network, no DuckDB extensions: FTS
rebuilds are switched off and the fetcher is faked."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "mcp-servers" / "kb-mcp"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import doc_sites  # noqa: E402
import refresh_docs  # noqa: E402

SCHEMA = (PROJECT_ROOT / "schemas" / "catalog.sql").read_text()


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.texts = 0

    def encode(self, texts, *, show_progress_bar=False):
        self.calls += 1
        self.texts += len(texts)
        return [[float(len(t) % 7)] * 384 for t in texts]


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA)
    yield c
    c.close()


def _source(conn, dsid=1, source_type="web_md"):
    conn.execute(
        "INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, identifier) "
        "VALUES (?, 'S', ?, 'web', ?)", [dsid, source_type, f"https://x.test/{dsid}.md"])
    return dsid


def _ingest(conn, dsid, md, *, source_type="web_md", embedder=None):
    fetched = refresh_docs.FetchResult(url="https://x.test", content=md, source_type="github_md")
    return refresh_docs.ingest_snapshot(
        conn, dsid, fetched, snapshot_source_type=source_type,
        content_hash=refresh_docs.compute_content_hash(md),
        embedder=embedder or FakeEmbedder(), rebuild_fts=False)


def _sections(conn, dsid):
    return conn.execute(
        "SELECT s.doc_section_id, s.heading_text FROM doc_section s "
        "JOIN doc_snapshot sn USING (snapshot_id) WHERE sn.doc_source_id = ? "
        "ORDER BY s.doc_section_id", [dsid]).fetchall()


V1 = "# Title\n\nintro\n\n## Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n"
V2 = "# Title\n\nintro\n\n## Alpha\n\nalpha body CHANGED\n\n## Beta\n\nbeta body\n"


# ---- supersession ---------------------------------------------------------


def test_changed_page_keeps_only_new_sections_and_carries_over_unchanged(conn):
    dsid = _source(conn)
    first = _ingest(conn, dsid, V1)
    old = dict((h, i) for i, h in _sections(conn, dsid))
    # Something extracted from the unchanged "Beta" section and the changed "Alpha" one.
    conn.execute("INSERT INTO concept (concept_id, name) VALUES (1, 'a'), (2, 'b')")
    conn.execute("INSERT INTO concept_relation VALUES (1, 2, 'REQUIRES', 1.0, 'doc_section', ?, now())",
                 [old["Beta"]])
    conn.execute("INSERT INTO concept_relation VALUES (2, 1, 'REQUIRES', 1.0, 'doc_section', ?, now())",
                 [old["Alpha"]])

    emb = FakeEmbedder()
    second = _ingest(conn, dsid, V2, embedder=emb)

    rows = _sections(conn, dsid)
    assert len(rows) == 3  # old snapshot's sections are gone
    assert {r[0] for r in rows}.isdisjoint(set(old.values()))
    new = dict((h, i) for i, h in rows)
    assert second.new_section_ids == [new["Alpha"]]
    assert sorted(second.kept_section_ids) == sorted([new["Title"], new["Beta"]])
    assert second.dropped_sections == 3
    assert emb.texts == 1  # only the changed section was embedded
    # Beta's relation followed it; Alpha's was dropped with the old section.
    rels = conn.execute("SELECT from_concept_id, source_id FROM concept_relation").fetchall()
    assert rels == [(1, new["Beta"])]
    # Every live section has exactly one embedding.
    assert conn.execute("SELECT COUNT(*) FROM doc_section_embedding").fetchone()[0] == 3
    # Snapshot history is kept.
    assert conn.execute("SELECT COUNT(*) FROM doc_snapshot WHERE doc_source_id = ?",
                        [dsid]).fetchone()[0] == 2
    assert first.snapshot_id != second.snapshot_id


def test_procedures_follow_carried_sections_and_die_with_dropped_ones(conn):
    dsid = _source(conn)
    _ingest(conn, dsid, V1)
    old = dict((h, i) for i, h in _sections(conn, dsid))
    conn.execute("INSERT INTO concept (concept_id, name) VALUES (1, 'a')")
    for pid, head in ((10, "Beta"), (11, "Alpha")):
        conn.execute("INSERT INTO procedure (procedure_id, name, source_type, source_id) "
                     "VALUES (?, 'p', 'doc_section', ?)", [pid, old[head]])
        conn.execute("INSERT INTO procedure_concept VALUES (?, 1)", [pid])
    _ingest(conn, dsid, V2)
    new = dict((h, i) for i, h in _sections(conn, dsid))
    assert conn.execute("SELECT procedure_id, source_id FROM procedure").fetchall() == [
        (10, new["Beta"])]
    assert conn.execute("SELECT procedure_id FROM procedure_concept").fetchall() == [(10,)]


def test_sampled_sources_are_not_superseded(conn):
    dsid = _source(conn, source_type="context7")
    _ingest(conn, dsid, V1, source_type="context7")
    res = _ingest(conn, dsid, V2, source_type="context7")
    assert len(_sections(conn, dsid)) == 6
    assert res.dropped_sections == 0
    assert len(res.new_section_ids) == 3


def test_retire_source_sections_removes_everything_but_snapshots(conn):
    dsid = _source(conn)
    _ingest(conn, dsid, V1)
    assert refresh_docs.retire_source_sections(conn, dsid) == 3
    assert _sections(conn, dsid) == []
    assert conn.execute("SELECT COUNT(*) FROM doc_section_embedding").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM doc_snapshot").fetchone()[0] == 1


def test_prep_extraction_limits_to_section_ids(conn, tmp_path):
    dsid = _source(conn)
    _ingest(conn, dsid, V1)
    res = _ingest(conn, dsid, V2)
    m = refresh_docs.prep_extraction(conn, res.snapshot_id, tmp_path,
                                     section_ids=res.new_section_ids)
    assert [s.doc_section_id for s in m.sections] == res.new_section_ids


# ---- index parsing + URL rules -----------------------------------------------


def test_parse_sitemap_urlset_and_index():
    urlset = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              "<url><loc>https://d.test/a</loc></url><url><loc>https://d.test/b/</loc></url>"
              "</urlset>")
    assert doc_sites.parse_sitemap(urlset) == (["https://d.test/a", "https://d.test/b/"], [])
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             "<sitemap><loc>https://d.test/s1.xml</loc></sitemap></sitemapindex>")
    assert doc_sites.parse_sitemap(index) == ([], ["https://d.test/s1.xml"])


def test_parse_llms_txt_keeps_same_host_links_once():
    text = ("# Docs\n- [A](https://c.test/docs/en/a.md): x\n- [A again](https://c.test/docs/en/a.md#h)\n"
            "- [Other](https://elsewhere.test/b.md)\n- [B](https://c.test/docs/en/b.md)\n")
    assert doc_sites.parse_llms_txt(text, "https://c.test/docs/llms.txt") == [
        "https://c.test/docs/en/a.md", "https://c.test/docs/en/b.md"]


def test_parse_llms_txt_resolves_relative_links():
    text = ("# API\n- [Role](access-management/v1/assignable-role.md): x\n"
            "- [Abs](/api/jobs/v2/create.md#body)\n- [Mail](mailto:a@b.test)\n")
    assert doc_sites.parse_llms_txt(text, "https://d.test/api/llms.txt") == [
        "https://d.test/api/access-management/v1/assignable-role.md",
        "https://d.test/api/jobs/v2/create.md"]


def test_site_filters_and_markdown_urls():
    s = doc_sites.Site(name="t", index_url="", index_kind="sitemap", page_format="markdown",
                       include_patterns=["https://d.test/aws/en/*"],
                       exclude_patterns=["*/release-notes/*"])
    assert s.wants("https://d.test/aws/en/jobs/")
    assert not s.wants("https://d.test/azure/en/jobs/")
    assert not s.wants("https://d.test/aws/en/release-notes/2025")
    assert s.fetch_url("https://d.test/aws/en/jobs") == "https://d.test/aws/en/jobs.md"
    assert s.fetch_url("https://d.test/aws/en/jobs/") == "https://d.test/aws/en/jobs/index.md"
    assert s.fetch_url("https://d.test/x.md") == "https://d.test/x.md"
    h = doc_sites.Site(name="h", index_url="", index_kind="sitemap", page_format="html")
    assert h.fetch_url("https://d.test/a/") == "https://d.test/a/"
    assert h.source_type == "web_html"


def test_html_page_to_markdown_uses_the_article():
    html = ("<html><head><title>Jobs | Databricks</title></head><body><nav>menu junk</nav>"
            "<article><h1>Jobs</h1><p>Run things.</p><h2>Triggers</h2><p>On a schedule.</p>"
            "<pre>databricks jobs create</pre></article><footer>legal</footer></body></html>")
    title, md, _ = doc_sites.html_page_to_markdown(html)
    assert title == "Jobs | Databricks"
    assert "# Jobs" in md and "## Triggers" in md and "databricks jobs create" in md
    assert "menu junk" not in md and "legal" not in md


DOCUSAURUS = """<html><head><title>SELECT | Databricks</title></head><body>
<article>
 <div class="wrapper_x"><nav class="breadcrumbs">SQL language reference</nav></div>
 <div class="theme-doc-toc-mobile">On this page</div>
 <div class="theme-doc-markdown markdown">
  <header><h1><code>SELECT</code> (subselect)<a class="hash-link" href="#x">​</a></h1></header>
  <div class="theme-admonition"><div class="admonitionHeading_x"><span class="admonitionIcon_x"><svg></svg></span>Preview</div>
   <div class="admonitionContent_x"><p>This feature is in Beta.</p></div></div>
  <h2>Syntax<a class="hash-link" href="#s">​</a></h2>
  <pre>SELECT * FROM t</pre>
  <div class="scrollable-table-wrapper">
   <button class="table-expand-button">Expand</button>
   <div class="scrollable-table-container"><table><tr><th>Feature</th><th>Supported</th></tr>
     <tr><td>Streaming</td><td><img alt="Green check icon"></td></tr></table></div>
   <dialog class="table-expand-overlay"><button>Back</button><table><tr><th>Feature</th></tr></table></dialog>
  </div>
 </div>
 <div class="theme-doc-footer-edit-meta-row"><span class="theme-last-updated">Last updated on
   <time datetime="2026-11-11T00:00:00.000Z" itemprop="dateModified">Nov 11, 2026</time></span></div>
</article><footer>legal</footer></body></html>"""


MINIFIED = """<html><body><article><div class="theme-doc-markdown">
<h1>Productboard</h1><p>Use <code>SELECT</code> in <a href="#">Databricks</a> .
<table><thead><tr><th>Feature<th>Availability</thead>
<tbody><tr><td>UI-based pipeline authoring<td><img alt="Green check icon"> Supported
<tr><td>API-based pipeline authoring<td><img alt="Red X icon"> Not supported
<tr><td>Long<td>""" + "x" * 300 + """</tbody></table>
<ul role="tablist"><li>Python</li><li>SQL</li></ul>
<div class="theme-admonition"><div class="admonitionHeading_x">note</div><div><p>Heads up.</p></div></div>
<pre class="language-sql"><code><div class="token-line">SELECT a<br></div><div class="token-line">FROM t<br></div><div class="token-line">WHERE x</div></code></pre>
<ul><li>can be any one of:<ul><li>a timestamp</li><li>a date</li></ul></li><li>last</li></ul>
</div></article></body></html>"""


def test_minified_html_tables_code_and_nested_lists():
    _, md, _ = doc_sites.html_page_to_markdown(MINIFIED, "div.theme-doc-markdown")
    assert "| UI-based pipeline authoring | ✓ Supported |" in md
    assert "| API-based pipeline authoring | ✗ Not supported |" in md
    assert "x" * 300 in md                      # cells are not truncated
    assert "SELECT a\nFROM t\nWHERE x" in md   # code keeps its lines
    assert md.count("a timestamp") == 1 and md.count("a date") == 1
    assert "- can be any one of:" in md
    assert "Databricks." in md and "Databricks ." not in md
    assert "```sql\nSELECT a" in md and "\x00" not in md
    assert "- Python" not in md
    assert "Note:\n\nHeads up." in md


def test_site_sectionizer_splits_deep_and_long_sections():
    from sectionizer import sectionize
    rows = "\n".join(f"| event_{i} | {'d' * 60} |" for i in range(200))
    md = (f"# Audit logs\n\nintro\n\n## Workspace events\n\n### Clusters\n\n"
          f"| action | description |\n|---|---|\n{rows}\n\n"
          f"### Jobs\n\n```python\n" + "x = 1\n\n" * 5 + "```\n\n#### Deep\n\ntext\n")
    flat = refresh_docs.flatten_section_tree(sectionize({"source_type": "site_md", "content": md}))
    heads = [f.heading_text for f in flat]
    assert "Jobs" in heads and "Deep" in heads
    parts = [f for f in flat if (f.heading_text or "").startswith("Clusters (part")]
    assert len(parts) >= 3
    for f in parts:
        assert len(f.content) <= 4200
        assert f.content.startswith("| action | description |\n|---|---|")
    jobs = next(f for f in flat if f.heading_text == "Jobs")
    assert jobs.content.count("```") == 2       # the fence was not cut
    # Old dispatch keys are untouched: github_md still splits at H2 only.
    old = refresh_docs.flatten_section_tree(sectionize({"source_type": "github_md", "content": md}))
    assert "Jobs" not in [f.heading_text for f in old]


def test_docusaurus_article_is_cleaned():
    title, md, content_date = doc_sites.html_page_to_markdown(
        DOCUSAURUS, "div.theme-doc-markdown")
    assert content_date == "2026-11-11T00:00:00.000Z"
    assert md.count("| Feature") == 1            # the dialog copy is gone
    assert "| Streaming | ✓ |" in md
    assert "# SELECT (subselect)" in md
    assert "\u200b" not in md
    assert "Preview:\n\nThis feature is in Beta." in md
    for junk in ("On this page", "SQL language reference", "Last updated", "Expand", "Back"):
        assert junk not in md
    assert doc_sites.selector_for(
        "https://docs.databricks.com/aws/en/sql/language-manual/x") == "div.theme-doc-markdown"


# ---- sync -------------------------------------------------------------------


BODY = " ".join(["word"] * 30)


class FakeWeb:
    """pages: url -> markdown (or None for 404). Records conditional headers."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.etags = {u: f'"{hash(v)}"' for u, v in self.pages.items() if v}
        self.seen = []

    def __call__(self, site, jobs):
        for url, etag, lm in jobs:
            self.seen.append((url, etag))
            md = self.pages.get(url)
            if md is None:
                yield doc_sites.PageFetch(url, url, "gone", http_status=404)
            elif etag and etag == self.etags[url]:
                yield doc_sites.PageFetch(url, url, "not_modified", http_status=304, etag=etag)
            else:
                yield doc_sites.PageFetch(url, url, "ok", http_status=200, content=md,
                                          title=doc_sites.markdown_title(md),
                                          etag=self.etags[url])

    def set(self, url, md):
        self.pages[url] = md
        self.etags[url] = f'"{hash(md)}"'


def _site(conn, **kw):
    s = doc_sites.Site(name="t", index_url="https://d.test/sitemap.xml", index_kind="sitemap",
                       page_format="markdown", name_prefix="T", **kw)
    s.site_id = doc_sites.upsert_site(conn, s)
    return s


def _pages(n):
    return {f"https://d.test/p{i}.md": f"# Page {i}\n\n{BODY}\n\n## Part\n\n{BODY} {i}\n"
            for i in range(n)}


def test_sync_force_and_path_reingest_selected_pages(conn):
    site = _site(conn)
    web = FakeWeb(_pages(3))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)
    emb = FakeEmbedder()
    rep = doc_sites.sync_site(conn, site, embedder=emb, index_urls=list(web.pages),
                              rebuild_fts=False, fetcher=web, paths=["p1"], force=True)
    assert rep.counts == {"changed": 1}
    assert rep.kept_sections == 2 and rep.new_sections == 0 and emb.texts == 0



def test_sync_force_skips_never_fetched_pages(conn):
    site = _site(conn)
    web = FakeWeb(_pages(3))
    first = list(web.pages)[:2]
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=first,
                        rebuild_fts=False, fetcher=web)
    # p2 is new in the index; --force with a cap must still reach a fetched page.
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                              rebuild_fts=False, fetcher=web, force=True, max_pages=1)
    assert rep.counts == {"registered": 1, "changed": 1}

def test_sync_registers_ingests_then_skips_unchanged(conn):
    site = _site(conn)
    web = FakeWeb(_pages(3))
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                              rebuild_fts=False, fetcher=web)
    assert rep.counts == {"registered": 3, "ingested_new": 3}
    names = [r[0] for r in conn.execute("SELECT name FROM doc_source ORDER BY 1").fetchall()]
    assert names == ["T: Page 0", "T: Page 1", "T: Page 2"]

    web.seen.clear()
    rep2 = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                               rebuild_fts=False, fetcher=web)
    assert rep2.counts == {"not_modified": 3}
    assert all(etag for _, etag in web.seen)  # conditional GETs were sent


def test_sync_reingests_changed_page_only(conn):
    site = _site(conn)
    web = FakeWeb(_pages(3))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)
    web.set("https://d.test/p1.md", f"# Page 1\n\n{BODY}\n\n## Part\n\n{BODY} edited\n")
    emb = FakeEmbedder()
    rep = doc_sites.sync_site(conn, site, embedder=emb, index_urls=list(web.pages),
                              rebuild_fts=False, fetcher=web)
    assert rep.counts == {"not_modified": 2, "changed": 1}
    assert rep.new_sections == 1 and rep.kept_sections == 1
    assert emb.texts == 1
    assert conn.execute("SELECT COUNT(*) FROM doc_section").fetchone()[0] == 6


def test_sync_retires_removed_pages_with_guard(conn):
    site = _site(conn)
    web = FakeWeb(_pages(3))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)
    shrunk = list(web.pages)[:2]
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=shrunk,
                              rebuild_fts=False, fetcher=web)
    assert rep.counts.get("retire_blocked") == 1  # 1 of 3 > 10%: looks like a broken index
    assert conn.execute("SELECT COUNT(*) FROM doc_section").fetchone()[0] == 6

    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=shrunk,
                              rebuild_fts=False, fetcher=web, force_retire=True)
    assert rep.counts.get("retired") == 1
    assert conn.execute("SELECT COUNT(*) FROM doc_section").fetchone()[0] == 4
    st = doc_sites.site_status(conn, site)
    assert st["live_pages"] == 2 and st["retired"] == 1


def test_sync_retires_404_pages_and_revives_returning_ones(conn):
    site = _site(conn)
    web = FakeWeb(_pages(2))
    urls = list(web.pages)
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=urls,
                        rebuild_fts=False, fetcher=web)
    gone = urls[0]
    web.pages[gone] = None
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=urls,
                              rebuild_fts=False, fetcher=web)
    assert rep.counts.get("retired") == 1
    # Back with the very same content: still re-ingested, since retiring
    # removed its sections.
    web.set(gone, _pages(2)[gone])
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=urls,
                              rebuild_fts=False, fetcher=web)
    assert rep.counts.get("registered") == 1 and rep.counts.get("ingested_new") == 1
    assert conn.execute("SELECT COUNT(*) FROM doc_section").fetchone()[0] == 4


def test_sync_adopts_existing_web_md_source(conn):
    conn.execute("INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, identifier) "
                 "VALUES (77, 'Claude Code: plugins', 'web_md', 'web_md', 'https://d.test/p0.md')")
    site = _site(conn)
    web = FakeWeb(_pages(1))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)
    assert conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == 1
    assert conn.execute("SELECT doc_source_id FROM doc_fetch_state").fetchone()[0] == 77


def test_plan_classifies_pages(conn):
    site = _site(conn, refresh_ttl_days=7)
    web = FakeWeb(_pages(3))
    urls = list(web.pages)
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=urls,
                        rebuild_fts=False, fetcher=web)
    conn.execute("UPDATE doc_fetch_state SET last_checked_at = ? WHERE page_url = ?",
                 [datetime.now() - timedelta(days=30), urls[1]])
    states = doc_sites.page_states(conn, site.site_id)
    plan = doc_sites.build_plan(site, urls[1:] + ["https://d.test/new.md"], states)
    assert plan.new == ["https://d.test/new.md"]
    assert plan.removed == [urls[0]]
    assert plan.overdue == [urls[1]]
    assert plan.fresh == 1
    assert plan.retire_blocked  # 1 of 3


def test_refresh_selection_skips_site_pages_and_honours_stale(conn):
    site = _site(conn)
    web = FakeWeb(_pages(1))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)
    conn.execute("INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, identifier, "
                 "refresh_ttl_days, last_refresh_at) VALUES "
                 "(500, 'old', 'github_raw', 'github', 'o/r', 7, now() - INTERVAL 30 DAY), "
                 "(501, 'fresh', 'github_raw', 'github', 'o/s', 7, now())")
    sel = refresh_docs._select_doc_source_ids
    assert sel(conn, source_id=None, all_sources=True, tier=None) == [500, 501]
    assert sel(conn, source_id=None, all_sources=False, tier=None, stale=True) == [500]
    assert sel(conn, source_id=None, all_sources=True, tier=None, domain="o/s") == [501]
    stale = doc_sites.stale_sources(conn)
    assert [r[0] for r in stale] == [500]


def test_sync_adopts_seeded_pages_and_retires_ones_gone_from_the_index(conn):
    # Two single-page sources seeded before the site existed; one is no
    # longer in the index (a renamed page). Another site's page is ignored.
    for dsid, url in ((80, "https://d.test/p0.md"), (81, "https://d.test/old-name.md"),
                      (82, "https://elsewhere.test/x.md")):
        conn.execute("INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, "
                     "identifier) VALUES (?, 'seeded', 'web_md', 'web_md', ?)", [dsid, url])
        _ingest(conn, dsid, f"# Seeded {dsid}\n\n{BODY}\n")
    site = _site(conn, include_patterns=["https://d.test/*"])
    web = FakeWeb(_pages(10))
    rep = doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                              rebuild_fts=False, fetcher=web)
    assert rep.counts["adopted"] == 2
    assert rep.counts["retired"] == 1          # 1 of 11 live pages: under the guard
    assert rep.counts["changed"] == 1          # p0 adopted, new content
    assert rep.counts["ingested_new"] == 9
    live = {r[0] for r in conn.execute(
        "SELECT DISTINCT sn.doc_source_id FROM doc_section s JOIN doc_snapshot sn "
        "USING (snapshot_id)").fetchall()}
    assert 81 not in live and 80 in live and 82 in live


def test_site_sectionizer_mintlify_tables_and_link_tails():
    from sectionizer import sectionize
    rows = "\n".join(f"| VAR_{i} | {'p' * 60} |" for i in range(200))
    md = (f"# Env vars\n\n## Variables\n\n| Variable | Purpose |\n| :- | :- |\n{rows}\n\n"
          "## Additional resources\n\n- [Hooks](https://x.test/hooks)\n- [Settings](https://x.test/s)\n\n"
          "## Next steps\n\nRead the hooks guide, then try a PreToolUse hook.\n")
    flat = refresh_docs.flatten_section_tree(sectionize({"source_type": "site_md", "content": md}))
    parts = [f for f in flat if (f.heading_text or "").startswith("Variables (part")]
    assert len(parts) >= 3
    assert all(f.content.startswith("| Variable | Purpose |\n| :- | :- |") for f in parts)
    heads = [f.heading_text for f in flat]
    assert "Additional resources" not in heads   # link-only tail dropped
    assert "Next steps" in heads                 # has prose: kept


def test_embedding_text_carries_page_and_heading_path(conn):
    assert refresh_docs.section_embedding_text(
        "Databricks: Audit log reference", ["Audit log reference", "Workspace events",
                                            "Genie (part 2/4)"], "| a | b |") == (
        "Databricks: Audit log reference › Workspace events › Genie (part 2/4)\n\n| a | b |")
    dsid = _source(conn)
    conn.execute("UPDATE doc_source SET name = 'T: Page' WHERE doc_source_id = ?", [dsid])

    class Capture(FakeEmbedder):
        def encode(self, texts, *, show_progress_bar=False):
            self.seen = list(texts)
            return super().encode(texts)

    emb = Capture()
    fetched = refresh_docs.FetchResult(url="u", content=V1, source_type="site_md")
    refresh_docs.ingest_snapshot(conn, dsid, fetched, snapshot_source_type="web_md",
                                 content_hash="h", embedder=emb, rebuild_fts=False,
                                 embed_context=True)
    assert "T: Page › Title › Beta\n\nbeta body" in emb.seen
    stored = [r[0] for r in conn.execute("SELECT content FROM doc_section").fetchall()]
    assert "beta body" in stored               # stored content has no prefix


def test_site_sectionizer_splits_one_long_list():
    from sectionizer import sectionize
    items = "\n".join(f"- November {i}: feature {'f' * 50}" for i in range(300))
    md = f"# Release notes\n\n## Platform\n\nIntro para.\n\n{items}\n"
    flat = refresh_docs.flatten_section_tree(sectionize({"source_type": "site_md", "content": md}))
    parts = [f for f in flat if (f.heading_text or "").startswith("Platform (part")]
    assert len(parts) >= 4
    assert all(len(f.content) <= 4000 for f in parts)
    assert parts[0].content.startswith("Intro para.\n\n- November 0")
    assert "\n- November 1:" in parts[0].content          # list lines keep single newlines
    joined = "\n".join(f.content for f in parts)
    assert joined.count("- November") == 300


def test_reembed_keeps_provenance_but_refreshes_vectors(conn):
    dsid = _source(conn)
    _ingest(conn, dsid, V1)
    old = dict((h, i) for i, h in _sections(conn, dsid))
    conn.execute("INSERT INTO concept (concept_id, name) VALUES (1, 'a'), (2, 'b')")
    conn.execute("INSERT INTO concept_relation VALUES (1, 2, 'REQUIRES', 1.0, 'doc_section', ?, now())",
                 [old["Beta"]])
    emb = FakeEmbedder()
    fetched = refresh_docs.FetchResult(url="u", content=V1, source_type="site_md")
    res = refresh_docs.ingest_snapshot(conn, dsid, fetched, snapshot_source_type="web_md",
                                       content_hash="h2", embedder=emb, rebuild_fts=False,
                                       reembed=True)
    assert emb.texts == 3 and res.new_section_ids == []
    new = dict((h, i) for i, h in _sections(conn, dsid))
    assert conn.execute("SELECT source_id FROM concept_relation").fetchall() == [(new["Beta"],)]
    assert conn.execute("SELECT COUNT(*) FROM doc_section_embedding").fetchone()[0] == 3


def test_reembed_site_uses_page_and_heading_path(conn):
    site = _site(conn)
    web = FakeWeb(_pages(1))
    doc_sites.sync_site(conn, site, embedder=FakeEmbedder(), index_urls=list(web.pages),
                        rebuild_fts=False, fetcher=web)

    class Capture(FakeEmbedder):
        def encode(self, texts, *, show_progress_bar=False):
            self.seen = list(texts)
            return super().encode(texts)

    emb = Capture()
    assert doc_sites.reembed_site(conn, site, embedder=emb) == 2
    assert any(t.startswith("T: Page 0 › Part\n\n") for t in emb.seen)
    assert conn.execute("SELECT COUNT(*) FROM doc_section_embedding").fetchone()[0] == 2


def test_claude_developer_presets_scope():
    P = doc_sites.PRESETS
    assert P["claude-platform"].wants("https://platform.claude.com/docs/en/api/messages.md")
    assert not P["claude-platform"].wants("https://platform.claude.com/docs/de/home.md")
    assert not P["claude-platform"].wants("https://platform.claude.com/llms-full.txt")
    assert P["claude-docs"].wants("https://claude.com/docs/connectors/building/mcp-apps/overview.md")
    assert not P["claude-docs"].wants("https://claude.com/docs/government/overview.md")
    mcp = P["mcp"]
    assert mcp.wants("https://modelcontextprotocol.io/specification/2026-07-28/server/tools")
    assert not mcp.wants("https://modelcontextprotocol.io/specification/2025-06-18/server/tools")
    assert not mcp.wants("https://modelcontextprotocol.io/specification/draft/server/tools")
    assert not mcp.wants("https://modelcontextprotocol.io/docs/2025-11-25/learn/architecture.md")
    assert not mcp.wants("https://modelcontextprotocol.io/docs/draft/learn/architecture.md")
    assert mcp.wants("https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture.md")
    assert mcp.wants("https://modelcontextprotocol.io/seps/2575-stateless-mcp.md")
    assert mcp.fetch_url("https://modelcontextprotocol.io/docs/learn/architecture") \
        == "https://modelcontextprotocol.io/docs/learn/architecture.md"
    assert doc_sites._page_name(P["claude-platform"], "Create a Message",
                                "https://platform.claude.com/docs/en/api/messages/create.md") \
        == "Claude API: Create a Message"


def test_databricks_api_preset_scope():
    api = doc_sites.PRESETS["databricks-api"]
    assert api.wants("https://docs.databricks.com/api/workspace/libraries/install.md")
    assert not api.wants("https://docs.databricks.com/aws/en/delta/optimize")
    assert not api.wants("https://docs.databricks.com/api/workspace/jobs/object.md")
    assert not doc_sites.PRESETS["databricks"].wants(
        "https://docs.databricks.com/api/workspace/libraries/install")
    assert api.fetch_url("https://docs.databricks.com/api/workspace/libraries/install") \
        == "https://docs.databricks.com/api/workspace/libraries/install.md"
    assert api.fetch_url("https://docs.databricks.com/api/workspace/libraries/install.md") \
        == "https://docs.databricks.com/api/workspace/libraries/install.md"


def test_with_endpoint_appends_single_rest_path():
    page = "# Add a library\n\n`POST /api/2.0/libraries/install`\n\nAdds libraries to a cluster.\n"
    assert doc_sites.with_endpoint("Add a library", page) \
        == "Add a library (POST /api/2.0/libraries/install)"
    many = page + "\nGET /api/2.0/libraries/cluster-status\n"
    assert doc_sites.with_endpoint("Libraries", many) == "Libraries"
    assert doc_sites.with_endpoint("Guide", "# Guide\n\nNo endpoints here.") == "Guide"


def test_front_matter_is_not_a_heading():
    import sectionizer
    page = ("---\ntitle: Tool use with prompt caching\nurl: https://x.test/p.md\n"
            "description: Cache tool definitions across turns.\n---\n\n"
            f"Intro paragraph. {BODY}\n\n## Details\n\n{BODY}\n")
    for st in ("site_md", "markdown"):
        secs = sectionizer.sectionize({"source_type": st, "content": page})
        flat, stack = [], list(secs)
        while stack:
            x = stack.pop(0)
            flat.append(x)
            stack[:0] = x.children
        heads = [x.heading_text for x in flat]
        assert heads[0] == "Tool use with prompt caching", (st, heads)
        assert not any(h and "title:" in h for h in heads)
        assert "Cache tool definitions" in flat[0].content
    # an existing H1 is kept; the description goes under it
    out = sectionizer.strip_front_matter("---\ntitle: T\ndescription: D\n---\n# Real\n\nbody\n")
    assert out.startswith("# Real\n\nD\n\nbody")
    assert sectionizer.strip_front_matter("no front matter\n---\n") == "no front matter\n---\n"
