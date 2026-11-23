"""ui_gen.py — ``/kb-ui``: a working shadcn/ui app from a schema or a data product (2026-11).

One pipeline, several inputs:

    input ──► UI spec (ui-spec.json) ──► ground (KB books + shadcn docs) ──► package

Inputs (v1):

* ``schema``        tables as ``CREATE TABLE`` DDL, an ODCS contract, or a Databricks
                    ``model.json`` (+ table names). Each table gets a list page
                    (Data Table + record sheet) and a "New" form (React Hook Form + zod).
                    Metric views (``metrics_sql``) add an overview dashboard.
* ``data-product``  an ODPS product file or a ``/kb-dp-from-model`` package: an overview
                    dashboard from the product KPIs and metric views (KPI cards, trend,
                    breakdown) and read-only list pages for the contract's tables.

Stacks: ``vite`` (default) or ``nextjs``; primitives: ``base`` (Base UI, default) or
``radix``. The package is an overlay for ``npx shadcn@latest init``; ``setup.sh``
creates the project, adds the shadcn components and copies the overlay in.

    <package>/
      ui-spec.json            the app as data (pages, regions, bindings, why)
      ui-spec.schema.json     JSON Schema it was checked against
      app-src/                files that go under the project's src/ (the @/ root)
      setup.sh                init + add + npm install + copy overlay (+ --build)
      design-notes.md         every region's pattern and why: rule, book, shadcn docs
      README.md
      _provenance.md  _validation.md  _prompts/refine-ui.md

Deterministic. Sample data is generated (never copied from a table); restricted
columns hold SAMPLE- placeholders and stay masked in the UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb

import kb_scaffold as ks
import ui_emit as ue
import ui_inputs as ui
import ui_spec as us
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "ui"
MAX_LIST_PAGES = 6

# Base install list: the shell, tables, record sheet, KPI cards and forms.
BASE_INSTALL = ["sidebar", "separator", "card", "badge", "button", "input", "table", "select",
                "sheet"]
CHART_INSTALL = ["chart"]
NPM_BASE = ["@tanstack/react-table@^9"]
NPM_FORMS = ["react-hook-form", "@hookform/resolvers", "zod"]

# shadcn doc pages (doc_source names) behind each region pattern.
DOCS_BY_ROLE: dict[str, list[str]] = {
    "shell": ["shadcn/ui: Sidebar"],
    "kpi": ["shadcn/ui Registry: Block: dashboard-01", "shadcn/ui: Card"],
    "trend": ["shadcn/ui: Chart", "shadcn/ui Registry: Chart: chart-area-interactive"],
    "breakdown": ["shadcn/ui: Chart", "shadcn/ui Registry: Chart: chart-bar-horizontal"],
    "table": ["shadcn/ui: Data Table", "shadcn/ui: Table", "shadcn/ui: Sheet"],
    "form": ["shadcn/ui: Forms / React Hook Form", "shadcn/ui: Field", "shadcn/ui: Date Picker"],
}
KEYWORDS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "shell": ("sidebar", "navigation"),
    "kpi": ("card", "section-cards", "dashboard"),
    "trend": ("area", "line", "tooltip", "chartconfig"),
    "breakdown": ("bar", "layout", "tooltip", "chartconfig"),
    "table": ("sorting", "pagination", "filtering", "columns"),
    "form": ("controller", "zod", "field", "validation"),
}
INSTALL_DOCS = {"vite": "shadcn/ui: Installation / Vite", "nextjs": "shadcn/ui: Installation / Next.js"}

# Book chapters behind each role: (book title prefix, chapter title prefix), checked
# against the library (2026-11 coverage check). The healthcare dataviz book goes
# first for healthcare apps, last otherwise.
HEALTH_VIZ = "Visualizing Health and Healthcare Data"
CURATED_CHAPTERS: dict[str, list[tuple[str, str]]] = {
    "shell": [("Designing Interfaces", "2. Organizing the Content"),
              ("Designing Interfaces", "Navigation and Browsing")],
    "kpi": [(HEALTH_VIZ, "Dashboards"), ("Designing Interfaces", "Dashboard")],
    "trend": [(HEALTH_VIZ, "Shape Two: Lines"),
              ("Fundamentals of Data Visualization", "Individual Time Series"),
              ("Visualize This", "TRENDS")],
    "breakdown": [(HEALTH_VIZ, "Shape One: Bars"),
                  ("Fundamentals of Data Visualization", "Bar Plots"),
                  ("Visualize This", "AMOUNTS")],
    "table": [(HEALTH_VIZ, "Fundamentals of Table Design"),
              ("Designing Interfaces", "Searching and Filtering"),
              ("Designing Interfaces", "Sorting and Rearranging")],
    "form": [("Designing Interfaces", "The Basics of Form Design"),
             ("Learn React with TypeScript", "Using React Hook Form"),
             ("React Design Patterns and Best Practices", "The challenges of form handling")],
}
# Fallback when a curated chapter is missing: title search in these books only, so a
# chapter called "Tables" in a database book never gets cited for a UI table.
UI_BOOKS = ("Designing Interfaces", "Laws of UX", "Practical UI Patterns", "Building Design Systems",
            "The Joy of UX", "A Project Guide to UX Design", "The Art and Science of UX Design",
            "Fundamentals of Data Visualization", HEALTH_VIZ, "Visualize This",
            "Hands-On Data Visualization", "Learn React with TypeScript",
            "React Design Patterns and Best Practices", "Real-World Next.js")
BOOK_TERMS: dict[str, tuple[str, ...]] = {
    "kpi": ("dashboard",),
    "trend": ("time series", "lines", "trend"),
    "breakdown": ("bar", "amounts", "comparison"),
    "table": ("table", "filter", "sort"),
    "form": ("form",),
    "shell": ("navigation", "information architecture"),
}
KB_TOPICS = ("Dashboard", "Data Visualization", "Form Validation", "User Interface Design")

# Why a region looks the way it does (the deterministic rule).
RULES: dict[str, str] = {
    "kpi": "Up to four headline numbers, each with its change against the previous period; "
           "more than four and none of them is the headline.",
    "trend_area": "Amounts and counts over time draw as an area: the filled area reads as volume.",
    "trend_line": "Rates over time draw as a line: rates don't add up, so a filled area would "
                  "suggest a total.",
    "breakdown": "A measure across categories is a horizontal bar chart sorted largest first; "
                 "long category names stay readable and the ranking reads top-down (better than "
                 "a pie past a handful of parts).",
    "table": "Columns: the key, a name or number, statuses, at most two money and two date "
             "columns; never foreign-key ids, long text, audit columns or restricted data. "
             "Search on the main text column, facets on up to two status-like columns.",
    "table_restricted": "Restricted columns (personal or health data) are left out of the table "
                        "and masked in the record sheet until asked for.",
    "form": "Required fields first; Radio Group for up to four values, Select beyond that, "
            "Date Picker for dates, Switch for flags; zod rules from types and required flags.",
    "shell": "A collapsible Sidebar lists the pages; the header names the current page.",
}


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def _chapter(conn: duckdb.DuckDBPyConnection, book: str, chapter: str
             ) -> Optional[ks.Citation]:
    row = conn.execute(
        "SELECT c.chapter_id, b.title, c.title FROM chapter c JOIN book b ON b.book_id = c.book_id "
        "WHERE b.title ILIKE ? AND c.title ILIKE ? AND coalesce(b.status, 'active') = 'active' "
        "ORDER BY length(coalesce(c.content, '')) DESC LIMIT 1",
        [book + "%", chapter + "%"]).fetchone()
    return ks.Citation("chapter", int(row[0]), f"{row[1]} — {row[2]}") if row else None


def chapter_citations(conn: duckdb.DuckDBPyConnection, role: str, *, healthcare: bool = False,
                      limit: int = 2) -> list[ks.Citation]:
    """Book chapters for a region role: the curated chapters first (the healthcare
    dataviz book leads only for healthcare apps), then a title search in UI_BOOKS."""
    out: list[ks.Citation] = []
    picks = list(CURATED_CHAPTERS.get(role, []))
    if not healthcare:
        picks.sort(key=lambda bc: bc[0] == HEALTH_VIZ)
    try:
        for book, chapter in picks:
            c = _chapter(conn, book, chapter)
            if c and all(x.source_id != c.source_id for x in out):
                out.append(c)
            if len(out) >= limit:
                return out
        for t in BOOK_TERMS.get(role, ()):
            for book in UI_BOOKS:
                rows = conn.execute(
                    "SELECT c.chapter_id, b.title, c.title FROM chapter c "
                    "JOIN book b ON b.book_id = c.book_id WHERE b.title ILIKE ? "
                    "AND lower(c.title) LIKE ? AND coalesce(b.status, 'active') = 'active' "
                    "AND length(coalesce(c.content, '')) > 1500 "
                    "ORDER BY length(c.title), c.chapter_id LIMIT 1",
                    [book + "%", f"%{t}%"]).fetchall()
                for cid, btitle, ctitle in rows:
                    if all(x.source_id != cid for x in out):
                        out.append(ks.Citation("chapter", int(cid), f"{btitle} — {ctitle}"))
                    if len(out) >= limit:
                        return out
    except Exception:  # catalog without book tables (tests)
        return out
    return out


def _why(c: ks.Citation, source: str) -> us.Why:
    return us.Why(source=source, label=c.label,
                  ref=c.url or f"{c.source_type}:{c.source_id}")


def ground(conn: duckdb.DuckDBPyConnection, roles: Sequence[str], *, healthcare: bool = False
           ) -> tuple[dict[str, list[ks.Citation]], dict[str, list[ks.Citation]], list[str]]:
    """(docs by role, books by role, doc pages that were not in the catalog)."""
    docs: dict[str, list[ks.Citation]] = {}
    books: dict[str, list[ks.Citation]] = {}
    missing: list[str] = []
    for role in roles:
        names = DOCS_BY_ROLE.get(role, [])
        cites = ks.doc_citations(conn, names, keywords=KEYWORDS_BY_ROLE.get(role, ()),
                                 per_source=1)
        found = {c.label.split(" — ")[0] for c in cites}
        for n in names:
            if not any(f.startswith(n) or n in f for f in found) and not _has_source(conn, n):
                missing.append(n)
        docs[role] = cites
        books[role] = chapter_citations(conn, role, healthcare=healthcare)
    return docs, books, us.unique(missing)


def _has_source(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM doc_source WHERE name = ? LIMIT 1",
                            [name]).fetchone() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Spec building
# ---------------------------------------------------------------------------

def _list_page(e: us.Entity, *, with_form: bool) -> us.Page:
    shown = us.table_fields(e)
    search, facets = us.filter_fields(e, shown)
    regions = [us.Region(role="table", pattern="Data Table + record Sheet",
                         title=e.plural,
                         bindings={"entity": e.name, "columns": [f.name for f in shown],
                                   "search": search.name if search else None,
                                   "facets": [f.name for f in facets]},
                         why=[us.Why("rule", RULES["table"])]
                         + ([us.Why("rule", RULES["table_restricted"])]
                            if any(f.restricted for f in e.fields) else []))]
    if with_form:
        ff = us.form_fields(e)
        regions.append(us.Region(
            role="form", pattern="Form (React Hook Form + zod + Field) in a Sheet",
            title=f"New {e.label.lower()}",
            bindings={"fields": [f.name for f in ff],
                      "controls": {f.name: us.control_for(f)[0] for f in ff}},
            why=[us.Why("rule", RULES["form"])]))
    return us.Page(slug=us.slug(e.plural), title=e.plural, archetype="list", entity=e.name,
                   description=e.description, regions=regions)


def _dashboard(metrics: Sequence[us.Metric], all_metrics: Sequence[us.Metric], title: str,
               description: str) -> Optional[us.Page]:
    if not metrics:
        return None
    regions: list[us.Region] = []
    cards = [{"metric": m.name, "label": m.label, "kind": m.format,
              "note": "Latest month against the month before"} for m in metrics[:4]]
    regions.append(us.Region(role="kpi", pattern="KPI cards (section-cards, dashboard-01)",
                             title="Headline KPIs", bindings={"cards": cards},
                             why=[us.Why("rule", RULES["kpi"])]))
    trend_m = next((m for m in metrics if m.time_dims), None) or \
        next((m for m in all_metrics if m.time_dims), None) or metrics[0]
    pattern, example = us.chart_for(trend_m, "trend")
    tdim = us.pick_time_dim(trend_m)
    regions.append(us.Region(
        role="trend", pattern=f"{pattern} ({example})", title=f"{trend_m.label} by "
        f"{(tdim.time_grain or 'month') if tdim else 'month'}",
        bindings={"metric": trend_m.name, "dimension": tdim.name if tdim else None,
                  "kind": trend_m.format, "label": trend_m.label,
                  "description": _metric_blurb(trend_m)},
        why=[us.Why("rule", RULES["trend_line" if trend_m.format == "percent" else "trend_area"])]))
    cands = [m for m in list(metrics) + list(all_metrics) if us.pick_category_dim(m)]
    # Totals and counts split cleanly across categories; averages and rates less so.
    cands.sort(key=lambda m: (m.format == "percent", bool(re.match(r"(avg|average|mean)_", m.name)),
                              m.format != "currency"))
    brk_m = cands[0] if cands else None
    if brk_m:
        dim = us.pick_category_dim(brk_m)
        assert dim is not None
        pattern, example = us.chart_for(brk_m, "breakdown")
        regions.append(us.Region(
            role="breakdown", pattern=f"{pattern} ({example})",
            title=f"{brk_m.label} by {dim.label.lower()}",
            bindings={"metric": brk_m.name, "dimension": dim.name,
                      "key": f"{brk_m.name}_by_{dim.name}",
                      "kind": brk_m.format, "label": brk_m.label,
                      "description": f"From metric view {brk_m.view}." if brk_m.view else ""},
            why=[us.Why("rule", RULES["breakdown"])]))
    return us.Page(slug="overview", title="Overview", archetype="dashboard",
                   description=description or title, regions=regions)


def _metric_blurb(m: us.Metric) -> str:
    if m.numerator and m.denominator:
        part = lambda n: re.sub(r"\s+(numerator|denominator)$", "", us.label(n), flags=re.I)
        return f"{part(m.numerator)} ÷ {part(m.denominator)}" + \
            (f", from metric view {m.view}" if m.view else "")
    text = ks.first_sentence(m.description, 140)
    if not text or re.match(r"(numerator|denominator)\b", text, re.I):
        return f"From metric view {m.view}" if m.view else ""
    return text


def build_spec(d: dict[str, Any]) -> us.AppSpec:
    entities: list[us.Entity] = d["entities"]
    measures: list[us.Metric] = d["metrics"]
    mode = d["mode"]
    spec = us.AppSpec(name=d["name"], title=d["title"], stack=d["stack"], base=d["base"],
                      mode=mode, description=d["description"], domain=d.get("domain") or "",
                      healthcare=d["healthcare"], read_only=mode == "data-product")
    headline = d.get("kpis") or ui.headline_measures(measures)
    used_metrics = {m.name: m for m in headline}
    dash = _dashboard(headline, measures, d["title"], d["description"])
    if dash:
        spec.pages.append(dash)
        for r in dash.regions:
            for key in ("metric",):
                name = r.bindings.get(key)
                if name and name not in used_metrics:
                    m = next((x for x in measures if x.name == name), None)
                    if m:
                        used_metrics[name] = m
    spec.metrics = list(used_metrics.values())
    lists = entities[:MAX_LIST_PAGES]
    if len(entities) > MAX_LIST_PAGES:
        spec.notes.append(f"{len(entities)} tables; list pages for the first {MAX_LIST_PAGES} "
                          "(pass tables=[...] to choose)")
    for e in lists:
        spec.pages.append(_list_page(e, with_form=not spec.read_only))
    spec.entities = lists
    # Slugs must be unique (two tables can share a plural).
    seen: dict[str, int] = {}
    for p in spec.pages:
        if p.slug in seen:
            seen[p.slug] += 1
            p.slug = f"{p.slug}-{seen[p.slug]}"
        else:
            seen[p.slug] = 1
    has_charts = any(r.role in ("trend", "breakdown") for p in spec.pages for r in p.regions)
    forms = [r for p in spec.pages for r in p.regions if r.role == "form"]
    install = list(BASE_INSTALL) + (CHART_INSTALL if has_charts else [])
    npm = list(NPM_BASE)
    if forms:
        install += ["field", "label"]
        npm += NPM_FORMS
        kinds = {f.kind for e in spec.entities for f in e.fields
                 if any(f.name in r.bindings.get("fields", []) for r in forms)}
        if kinds & {"date", "timestamp"}:
            install += ["popover", "calendar"]
            npm.append("date-fns")
        if "boolean" in kinds:
            install.append("switch")
        if "longtext" in kinds:
            install.append("textarea")
        if any(f.kind == "enum" and 0 < len(f.enum_values) <= 4 for e in spec.entities
               for f in e.fields):
            install.append("radio-group")
    spec.install = us.unique(install)
    spec.npm = us.unique(npm)
    if spec.healthcare:
        spec.notes.append("healthcare data: restricted columns are masked and sample data uses "
                          "SAMPLE- placeholders; connect real data only behind access controls")
    return spec


def attach_grounding(spec: us.AppSpec, docs: dict[str, list[ks.Citation]],
                     books: dict[str, list[ks.Citation]]) -> None:
    for p in spec.pages:
        for r in p.regions:
            r.why += [_why(c, "registry" if "Registry" in c.label else "doc")
                      for c in docs.get(r.role, [])]
            r.why += [_why(c, "book") for c in books.get(r.role, [])]


# ---------------------------------------------------------------------------
# Package text
# ---------------------------------------------------------------------------

def setup_sh(spec: us.AppSpec) -> str:
    template = "vite" if spec.stack == "vite" else "next"
    return f"""#!/usr/bin/env bash
# Create the {spec.title} app: shadcn/ui ({spec.base} primitives) on {spec.stack}.
# Generated by myPub /kb-ui.  Usage: ./setup.sh [app-dir] [--build]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="{spec.name}"
BUILD=0
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    *) APP="$arg" ;;
  esac
done

if [ ! -f "$APP/components.json" ]; then
  npx -y shadcn@latest init --name "$APP" --template {template} --base {spec.base} \\
    --preset nova --yes --no-monorepo --no-rtl
fi
cd "$APP"
npx -y shadcn@latest add -y {' '.join(spec.install)}
npm install {' '.join(spec.npm)}

# The overlay goes under the @/ alias root: src/ when the project has one.
ROOT=.
[ -d src ] && ROOT=src
cp -R "$HERE/app-src/." "$ROOT/"
echo "Copied the /kb-ui overlay into $APP/$ROOT"

if [ "$BUILD" = 1 ]; then
  npm run build
fi
echo "Done. cd $APP && npm run dev"
"""


def readme(spec: us.AppSpec, d: dict[str, Any]) -> str:
    pages = "\n".join(f"- **{p.title}** ({p.archetype})"
                      + (f": {len([r for r in p.regions])} region(s)" if p.regions else "")
                      for p in spec.pages)
    run = "npm run dev" + ("  # http://localhost:5173" if spec.stack == "vite"
                           else "  # http://localhost:3000")
    stack = "Vite + React" if spec.stack == "vite" else "Next.js (App Router)"
    restricted = sorted({f"{e.name}.{f.name}" for e in spec.entities for f in e.fields
                         if f.restricted})
    return f"""# {spec.title}

{ks.first_sentence(spec.description, 400) or 'A shadcn/ui app generated from ' + d['source_label'] + '.'}

Generated by myPub `/kb-ui` from {d['source_label']} (mode `{spec.mode}`).
Stack: **{stack}**, shadcn/ui with **{'Base UI' if spec.base == 'base' else 'Radix'}** primitives.

## Pages

{pages}

## Run it

```bash
./setup.sh            # npx shadcn init + add {len(spec.install)} components + npm install + copy app-src/
cd {spec.name}
{run}
./setup.sh {spec.name} --build   # or: type-check and build
```

`setup.sh` needs Node 20+ and network access to ui.shadcn.com and npm.

## What's real and what's sample

- **Data**: `src/data/sample.ts` is generated sample data (deterministic, never copied
  from a table). Every view reads through `src/data/source.ts`: replace those
  function bodies to read your API; the types in `src/data/types.ts` stay.
{('- **Restricted columns** (' + ', '.join(restricted[:12]) + ('…' if len(restricted) > 12 else '') + ') are kept out of tables, masked in record sheets, and hold `SAMPLE-` placeholders.' + chr(10)) if restricted else ''}- **Forms** validate with zod and show what would be saved; wire `onSubmit` to your API.
- **Charts and KPIs** use the metric-view measures named in `ui-spec.json`.

## Files

- `ui-spec.json`: the app as data; regenerate from it or edit it.
- `design-notes.md`: why each region looks the way it does, with sources.
- `app-src/`: the overlay (`components/kb/`, `views/`, `data/`, `lib/`, {'`App.tsx`' if spec.stack == 'vite' else '`app/`'}).
- `_prompts/refine-ui.md`: an optional subagent pass for copy and layout polish.
"""


def design_notes(spec: us.AppSpec, topics: Sequence[ks.TopicMaterial],
                 shell_why: Sequence[us.Why]) -> str:
    lines = [f"# Design notes: {spec.title}", "",
             "Each region of each page: the shadcn/ui pattern it uses and why, from the "
             "generator's rules, the library's books and the shadcn docs.", ""]
    lines += ["## App frame", "", f"- Pattern: Sidebar + header ({spec.stack}, {spec.base})"]
    lines += [f"- {_why_md(w)}" for w in shell_why]
    lines.append("")
    for p in spec.pages:
        lines += [f"## {p.title}", "", f"Archetype `{p.archetype}`"
                  + (f", entity `{p.entity}`" if p.entity else "") + ".", ""]
        for r in p.regions:
            lines += [f"### {r.title or r.role}", "", f"- Pattern: {r.pattern}"]
            b = r.bindings
            if r.role == "table":
                lines.append("- Columns: " + ", ".join(f"`{c}`" for c in b.get("columns", [])))
                if b.get("search"):
                    lines.append(f"- Search: `{b['search']}`")
                if b.get("facets"):
                    lines.append("- Facets: " + ", ".join(f"`{c}`" for c in b["facets"]))
            elif r.role == "form":
                lines.append("- Fields: " + ", ".join(
                    f"`{n}` ({c})" for n, c in (b.get("controls") or {}).items()))
            elif r.role == "kpi":
                lines.append("- Cards: " + ", ".join(
                    f"{c['label']} ({c['kind']})" for c in b.get("cards", [])))
            elif r.role in ("trend", "breakdown"):
                lines.append(f"- Measure: `{b.get('metric')}`"
                             + (f" by `{b['dimension']}`" if b.get("dimension") else ""))
            lines += [f"- {_why_md(w)}" for w in r.why]
            lines.append("")
    procs = [(t, pr) for t in topics for pr in t.procedures[:3]]
    if procs:
        lines += ["## From the library", ""]
        for t, pr in procs:
            src = f" _({pr.source.label})_" if pr.source else ""
            lines.append(f"- **{pr.name}** ({t.name}). {ks.first_sentence(pr.steps, 220)}{src}")
        lines.append("")
    if spec.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in spec.notes] + [""]
    return "\n".join(lines)


def _why_md(w: us.Why) -> str:
    tag = {"rule": "Rule", "book": "Book", "doc": "shadcn docs", "registry": "shadcn registry"}
    return f"{tag.get(w.source, w.source)}: {w.label}"


REFINE_PROMPT = """\
# Refine the {title} UI

The app in `app-src/` was generated deterministically from `ui-spec.json`. Polish it
without changing its structure:

- Page descriptions, card notes, empty states and button labels: plain, specific
  copy for the people who use this app ({domain}).
- Check each chart and KPI against `design-notes.md`; if a measure is misread
  (a rate shown as an amount, a count as money), fix `ui-spec.json` and the view.
- Keep every import path, component name and the `data/source.ts` signatures.
- Never put real records in `data/sample.ts`. Restricted columns stay masked.
- Run `./setup.sh <dir> --build` afterwards; it must type-check and build.
"""


# ---------------------------------------------------------------------------
# Generator components
# ---------------------------------------------------------------------------

def _read(text_or_path: Optional[str]) -> str:
    if not text_or_path:
        return ""
    s = str(text_or_path)
    if "\n" not in s and len(s) < 1024:
        p = Path(s).expanduser()
        if p.is_file():
            return p.read_text()
    return s


class UIDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        stack = (kw.get("stack") or "vite").lower().replace(".", "")
        stack = "nextjs" if stack in ("next", "nextjs") else stack
        if stack not in us.STACKS:
            raise ValueError(f"stack must be one of {us.STACKS}, got {kw.get('stack')!r}")
        base = (kw.get("base") or "base").lower()
        if base not in us.BASES:
            raise ValueError(f"base must be one of {us.BASES}, got {kw.get('base')!r}")
        tables = list(kw.get("tables") or [])
        notes: list[str] = []
        kpis: list[us.Metric] = []
        description = kw.get("description") or ""
        domain = kw.get("domain") or ""
        files: list[str] = []
        if kw.get("product_path"):
            mode = "data-product"
            prod = ui.load_product(kw["product_path"], kw.get("product"))
            entities = ui.pick_tables(prod.entities, tables)
            metrics = prod.metrics
            kpis = ui.kpi_metrics(prod.kpis, metrics)
            title = kw.get("title") or prod.name
            description = description or prod.description
            domain = domain or prod.domain
            files, notes = prod.files, list(prod.notes)
            source_label = f"ODPS product `{prod.product_id}`"
        else:
            mode = "schema"
            entities, metrics = [], []
            source_label = "the tables given"
            if kw.get("model_json"):
                entities, metrics, mdesc = ui.model_from_json(Path(kw["model_json"]).expanduser(),
                                                              tables)
                description = description or mdesc
                files.append(str(kw["model_json"]))
                source_label = f"model `{Path(kw['model_json']).name}`"
            if kw.get("contract"):
                ents, header = ui.entities_from_contract(_read(kw["contract"]))
                entities += ui.pick_tables(ents, tables)
                description = description or str(header.get("description", {}).get("purpose", "")
                                                  if isinstance(header.get("description"), dict)
                                                  else header.get("description") or "")
                domain = domain or str(header.get("domain") or "")
                source_label = "an ODCS contract"
            if kw.get("ddl"):
                entities += ui.pick_tables(ui.entities_from_ddl(_read(kw["ddl"])), tables)
                source_label = "DDL"
            if kw.get("metrics_sql"):
                metrics += ui.metrics_from_sql(_read(kw["metrics_sql"]))
            title = kw.get("title") or (us.label(entities[0].plural) if len(entities) == 1
                                        else (us.label(domain) if domain else us.label(query)))
            if not entities:
                notes.append("no tables found in the input; nothing to build pages from")
        name = us.slug(kw.get("app_name") or title or query) or "kb-ui-app"
        healthcare = ui.looks_healthcare(query, description, domain,
                                         " ".join(e.name for e in entities))
        roles = ["shell", "table"] + (["form"] if mode == "schema" else []) + \
            (["kpi", "trend", "breakdown"] if (kpis or metrics) else [])
        docs, books, missing_docs = ground(conn, roles, healthcare=healthcare)
        docs.setdefault("install", ks.doc_citations(conn, [INSTALL_DOCS[stack]], per_source=1))
        topics = [t for t in (ks.topic_material(conn, resolver, name_, max_procedures=4)
                              for name_ in KB_TOPICS) if t]
        return {"query": query, "mode": mode, "name": name, "title": title,
                "description": description, "domain": domain, "stack": stack, "base": base,
                "entities": entities, "metrics": metrics, "kpis": kpis, "healthcare": healthcare,
                "docs": docs, "books": books, "missing_docs": missing_docs, "topics": topics,
                "files": files, "notes": notes, "source_label": source_label}


class UIPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        spec = build_spec(d)
        spec.notes = d["notes"] + spec.notes
        attach_grounding(spec, d["docs"], d["books"])
        shell_why = [us.Why("rule", RULES["shell"])] + \
            [_why(c, "doc") for c in d["docs"].get("shell", []) + d["docs"].get("install", [])] + \
            [_why(c, "book") for c in d["books"].get("shell", [])]
        doc = spec.to_dict()
        errors = us.check_spec(doc) if spec.pages else ["no pages: the input had no tables or metrics"]
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or spec.name,
                       domain=spec.domain or "ui", source_query=d["query"])
        overlay = ue.emit(spec) if spec.pages else {}
        books = sorted({w.label for p in spec.pages for r in p.regions for w in r.why
                        if w.source == "book"})
        plan.package_metadata = {
            "mode": spec.mode, "stack": spec.stack, "base": spec.base, "title": spec.title,
            "pages": [{"slug": p.slug, "archetype": p.archetype} for p in spec.pages],
            "entities": [e.name for e in spec.entities],
            "metrics": [m.name for m in spec.metrics], "install": spec.install, "npm": spec.npm,
            "healthcare": spec.healthcare, "spec_errors": errors,
            "missing_docs": d["missing_docs"], "book_citations": len(books),
            "doc_citations": sum(1 for p in spec.pages for r in p.regions for w in r.why
                                 if w.source in ("doc", "registry")),
            "overlay_files": sorted(overlay), "inputs": d["files"],
        }
        all_cites = [c for cs in list(d["docs"].values()) + list(d["books"].values()) for c in cs]
        for i, p in enumerate(spec.pages):
            plan.units.append(GenUnit(
                unit_type="ui_page", name=p.title, ordinal=i, logical_key=p.slug,
                content_markdown=json.dumps(p.__dict__, default=lambda o: o.__dict__, indent=2),
                metadata={"archetype": p.archetype, "entity": p.entity,
                          "regions": [r.role for r in p.regions]},
                sources=[(c.source_type, c.source_id, 1.0, 1.0, None) for c in all_cites
                         if any(w.ref == f"{c.source_type}:{c.source_id}" or w.ref == c.url
                                for r in p.regions for w in r.why)]))
        plan.files.append(GenFile("ui-spec.json", json.dumps(doc, indent=2, ensure_ascii=False)
                                  + "\n", purpose="spec"))
        plan.files.append(GenFile("ui-spec.schema.json",
                                  json.dumps(us.SPEC_SCHEMA, indent=2) + "\n", purpose="schema"))
        for path, content in sorted(overlay.items()):
            plan.files.append(GenFile(f"app-src/{path}", content, purpose="app"))
        plan.files += [
            GenFile("setup.sh", setup_sh(spec), purpose="setup"),
            GenFile("README.md", readme(spec, d), purpose="overview"),
            GenFile("design-notes.md", design_notes(spec, d["topics"], shell_why),
                    purpose="guidance"),
            GenFile("_provenance.md", _provenance(spec, d, all_cites), purpose="provenance"),
            GenFile("_validation.md", _validation(spec, errors, d["missing_docs"]),
                    purpose="validation"),
            GenFile("_prompts/refine-ui.md", REFINE_PROMPT.format(
                title=spec.title, domain=spec.domain or "the app's users"),
                purpose="subagent_prompt"),
        ]
        plan.notes += spec.notes
        return plan


def _provenance(spec: us.AppSpec, d: dict, cites: Sequence[ks.Citation]) -> str:
    lines = [f"# Provenance: {spec.title}", "", f"Mode `{spec.mode}`, from {d['source_label']}.",
             ""]
    if d["files"]:
        lines += ["## Inputs read", ""] + [f"- `{f}`" for f in d["files"]] + [""]
    seen: set[tuple[str, int]] = set()
    uniq = [c for c in cites if not ((c.source_type, c.source_id) in seen
                                     or seen.add((c.source_type, c.source_id)))]
    lines.append(ks.render_citations([c for c in uniq if c.source_type == "doc_section"],
                                     "shadcn/ui docs and registry sections"))
    lines.append(ks.render_citations([c for c in uniq if c.source_type == "chapter"],
                                     "Book chapters"))
    tsrc = [c for t in d["topics"] for c in t.sources]
    if tsrc:
        lines.append(ks.render_citations(tsrc, "Knowledge-base concepts ("
                                         + ", ".join(t.name for t in d["topics"]) + ")"))
    return "\n".join(lines)


def _validation(spec: us.AppSpec, errors: Sequence[str], missing: Sequence[str]) -> str:
    lines = [f"# Validation: {spec.title}", "",
             f"- UI spec against `ui-spec.schema.json` + cross-references: "
             + ("**ok**" if not errors else f"**{len(errors)} error(s)**")]
    lines += [f"  - {e}" for e in errors]
    lines.append("- shadcn doc pages behind the patterns: "
                 + ("all in the catalog" if not missing else
                    f"{len(missing)} not in the catalog ({', '.join(missing)})"))
    lines += ["- Type-check and build: not run by the generator. Run `./setup.sh "
              f"{spec.name} --build` (needs Node and network).", ""]
    return "\n".join(lines)


class UIValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        out = [ValidationIssue("ui-spec.json", "error", e) for e in meta.get("spec_errors", [])
               if "not installed" not in e]
        out += [ValidationIssue("ui-spec.json", "warning", e) for e in meta.get("spec_errors", [])
                if "not installed" in e]
        if meta.get("missing_docs"):
            out.append(ValidationIssue("design-notes.md", "warning",
                                       "shadcn doc pages not in the catalog: "
                                       + ", ".join(meta["missing_docs"])))
        if not meta.get("book_citations"):
            out.append(ValidationIssue("design-notes.md", "warning",
                                       "no book chapters matched the UI patterns"))
        return out


def make_ui_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=UIDecomposer(),
                     planner=UIPlanner(), ranking_mode="generation", validator=UIValidator(),
                     materializer=ks.TreeMaterializer())
