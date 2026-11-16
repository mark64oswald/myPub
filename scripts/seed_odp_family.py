#!/usr/bin/env python3
"""
seed_odp_family.py — the Open Data Product (ODP) standards family in myPub (2026-11-16).

The ODP family (opendataproducts.org, Linux Foundation) is five specs for
data products: ODPS (one product), ODPC (catalogs), ODPG (graphs), ODPV
(vocabulary) and ODPR (recipes, draft), plus the `open-data-products`
Python SDK. This script loads it in two layers:

1. Live docs.
   ``specs``    the five spec pages as ``html_site`` sources (html_doc.py
                does the HTML → Markdown; each page is one long static page).
   ``sources``  SDK guides, the ODP knowledge-base FAQs, ODCS (the Bitol
                data contract standard ODPS and ODPR build on), and this
                repo's docs/odp-family-map.md, as ``github_raw`` sources.
   Then run refresh_docs.py prep / process for concept + procedure
   extraction, as for any other source.

2. The curated spec map (no LLM calls).
   ``map``      Run after extraction. Writes, from data/odp/odp_family.yaml
                and the ODPV 1.1 vocabulary (data/odp/odpv-1.1.yaml, from the
                SDK wheel, Apache 2.0):
                  * one concept per spec, with aliases;
                  * one concept per ODPV object term (core, value,
                    governance), with its distinctive aliases;
                  * hand-checked spec edges, plus ODPV edges: a spec
                    REQUIRES each term it uses (``usedIn``), and a term
                    CITES its related terms;
                  * concept_doc_link rows to the spec pages.
                Edges are concept_relation rows with source_type
                'doc_section', anchored to the section of the family map
                that states them, so the family map must be loaded first.
                Existing concepts are found by name or alias only (never by
                embedding), so "Data Product Graph" can't fold into
                "Knowledge Graph". Dry run unless --write.

   ``render-map`` regenerates the ODPV term tables in docs/odp-family-map.md.
   ``status``     what is loaded.

Usage:
    .venv/bin/python3 scripts/seed_odp_family.py specs --render-dir /tmp/odp   # review, no DB
    .venv/bin/python3 scripts/seed_odp_family.py specs
    .venv/bin/python3 scripts/seed_odp_family.py sources [--dry-run]
    .venv/bin/python3 scripts/refresh_docs.py refresh --source-id <id> ...
    .venv/bin/python3 scripts/seed_odp_family.py map            # plan
    .venv/bin/python3 scripts/seed_odp_family.py map --write
    .venv/bin/python3 scripts/seed_odp_family.py map --pin odps=<concept_id>   # name taken
    .venv/bin/python3 scripts/seed_odp_family.py status
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import duckdb
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
for _p in (SCRIPTS_DIR, MCP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from seed_doc_sources import _upsert  # noqa: E402

DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
FAMILY_FILE = PROJECT_ROOT / "data" / "odp" / "odp_family.yaml"
FAMILY_MAP_MD = PROJECT_ROOT / "docs" / "odp-family-map.md"
LOG = logging.getLogger("seed_odp_family")

SPEC_AUTHORITY = 0.90
GUIDE_AUTHORITY = 0.80
FAQ_AUTHORITY = 0.70
MAP_AUTHORITY = 0.60      # our own summary: never outranks the specs

RELATION_TYPES = {"REQUIRES", "EXTENDS", "CONTRASTS_WITH", "IMPLEMENTS", "CITES"}
FAMILY_MAP_ID = "mark64oswald/myPub:main:docs/odp-family-map.md"
LINK_NOTE = "odp family seed"

ODPV_BEGIN, ODPV_END = "<!-- odpv:begin -->", "<!-- odpv:end -->"
ODPV_GROUP_HEADINGS = {
    "core": "ODPV core terms",
    "value": "ODPV value terms",
    "governance": "ODPV governance terms",
    "relationships": "ODPV relationship terms",
}


# ---------------------------------------------------------------------------
# Live-doc sources
# ---------------------------------------------------------------------------

SDK = "Open-Data-Product-Initiative/odp-agent-sdk:main"
KB = "Open-Data-Product-Initiative/odps-knowledge-base:main"
BITOL = "bitol-io/open-data-contract-standard:main"


def _gh(name: str, repo_branch: str, path: str, authority: float, ttl: int = 14) -> dict[str, Any]:
    return {"name": name, "source_type": "github_raw", "identifier": f"{repo_branch}:{path}",
            "authority_score": authority, "refresh_ttl_days": ttl}


SDK_GUIDES = [
    ("README", "README.md"),
    ("full SDK guide", "docs/user/full-sdk-guide.md"),
    ("command guide", "docs/user/commands.md"),
    ("LLM generation", "docs/user/generation.md"),
    ("LLM selection guide", "docs/user/llm-selection-guide.md"),
    ("agent surface (MCP)", "docs/user/agent-surface.md"),
    ("recipe workflows", "docs/user/recipe-workflows.md"),
    ("recipe quick start", "docs/recipes/quick-start.md"),
    ("recipes for agents", "docs/recipes/agent-usage.md"),
    ("Data Contract workflows", "docs/user/data-contracts.md"),
    ("portfolio intake", "docs/user/portfolio-intake.md"),
    ("API reference", "docs/user/API.md"),
]

# odps-knowledge-base/faq (validate.md is a one-line pointer, left out).
KB_FAQS = [
    "what-is-odps", "what-is-odpc", "what-is-odpv", "odpc-vs-odps",
    "odpv-standards-family", "odpv-vocabulary-groups", "odpv-relationship-terms",
    "odpv-python-tools", "odpc-python-tools", "odpc-catalog-example",
    "odpc-product-reference", "required-optional", "define-metadata", "define-related",
    "full-example", "templates", "reuse-components", "external-ref",
    "internal-vs-external-ref", "mixed-tiers", "productstrategy", "productKPIs",
    "contract", "data-quality", "sla-linking", "access-control", "license", "pricing",
    "payment-gateways", "validation", "ai-agent-consumption", "odps-llms-txt",
]

ODCS_PAGES = [
    ("overview", "docs/README.md"),
    ("fundamentals", "docs/fundamentals.md"),
    ("schema", "docs/schema.md"),
    ("data quality", "docs/data-quality.md"),
    ("service-level agreement", "docs/service-level-agreement.md"),
]

ODP_SOURCE_SEEDS: list[dict[str, Any]] = [
    *[_gh(f"ODP SDK: {title}", SDK, path, GUIDE_AUTHORITY) for title, path in SDK_GUIDES],
    *[_gh(f"ODP knowledge base FAQ: {slug}", KB, f"faq/{slug}.md", FAQ_AUTHORITY, ttl=30)
      for slug in KB_FAQS],
    *[_gh(f"ODCS: {title}", BITOL, path, GUIDE_AUTHORITY + 0.05, ttl=30)
      for title, path in ODCS_PAGES],
    {"name": "ODP family map (myPub)", "source_type": "github_raw",
     "identifier": FAMILY_MAP_ID, "authority_score": MAP_AUTHORITY, "refresh_ttl_days": 30},
]

# Pages that stay searchable but aren't worth LLM extraction.
NO_EXTRACT = {"docs/user/API.md"}


# ---------------------------------------------------------------------------
# The curated family file + ODPV
# ---------------------------------------------------------------------------

@dataclass
class Spec:
    key: str
    name: str
    concept_type: str
    aliases: list[str]
    section: str
    description: str
    url: Optional[str] = None
    odpv_code: Optional[str] = None


@dataclass
class Edge:
    from_ref: str
    to_ref: str
    type: str
    confidence: float
    section: str


@dataclass
class Term:
    id: str
    group: str
    label: str
    definition: str
    aliases: list[str]
    related: list[str]
    used_in: list[str]
    domain: list[str] = field(default_factory=list)
    range: list[str] = field(default_factory=list)


@dataclass
class Family:
    specs: dict[str, Spec]
    edges: list[Edge]
    terms: list[Term]                 # object terms (core, value, governance)
    relationships: list[Term]         # relationship terms
    merge_single_word: set[str]
    term_concept_type: str
    used_in_confidence: float
    related_confidence: float


def _en(v: Any) -> Any:
    return v.get("en") if isinstance(v, dict) else v


def load_odpv(path: Path) -> tuple[list[Term], list[Term]]:
    doc = yaml.safe_load(path.read_text())
    objects, rels = [], []
    for section in doc["sections"]:
        for t in section["terms"]:
            term = Term(
                id=t["id"], group=section["id"], label=_en(t["preferredLabel"]),
                definition=_en(t.get("definition")) or "",
                aliases=list(_en(t.get("alsoKnownAs")) or []),
                related=list(t.get("relatedTerms") or []),
                used_in=list(t.get("usedIn") or []),
                domain=list(t.get("domain") or []), range=list(t.get("range") or []))
            (rels if t.get("type") == "relationship" else objects).append(term)
    return objects, rels


def load_family(path: Path = FAMILY_FILE) -> Family:
    doc = yaml.safe_load(path.read_text())
    specs = {}
    for s in doc["specs"]:
        specs[s["key"]] = Spec(
            key=s["key"], name=s["name"], concept_type=s.get("concept_type", "Concept"),
            aliases=list(s.get("aliases") or []), section=s["section"],
            description=" ".join(str(s.get("description", "")).split()),
            url=s.get("url"), odpv_code=s.get("odpv_code"))
    edges = []
    for e in doc["edges"]:
        frm = e["from"]
        default_section = specs[frm].section if frm in specs else None
        edges.append(Edge(frm, e["to"], e["type"], float(e.get("confidence", 1.0)),
                          e.get("section") or default_section))
    o = doc["odpv"]
    objects, rels = load_odpv(PROJECT_ROOT / o["file"])
    return Family(specs, edges, objects, rels, set(o.get("merge_single_word") or []),
                  o.get("term_concept_type", "Concept"),
                  float(o.get("used_in_confidence", 0.8)),
                  float(o.get("related_confidence", 0.6)))


def validate_family(fam: Family) -> list[str]:
    """Problems with the curated file; empty when it is consistent."""
    errors = []
    headings = set(markdown_headings(FAMILY_MAP_MD.read_text()))
    for s in fam.specs.values():
        if s.section not in headings:
            errors.append(f"spec {s.key}: section {s.section!r} not in {FAMILY_MAP_MD.name}")
    for e in fam.edges:
        if e.type not in RELATION_TYPES:
            errors.append(f"edge {e.from_ref}->{e.to_ref}: bad type {e.type}")
        for end in (e.from_ref, e.to_ref):
            if end not in fam.specs and not end.startswith("concept:"):
                errors.append(f"edge {e.from_ref}->{e.to_ref}: unknown endpoint {end!r}")
        if e.section not in headings:
            errors.append(f"edge {e.from_ref}->{e.to_ref}: section {e.section!r} missing")
    codes = {s.odpv_code for s in fam.specs.values() if s.odpv_code}
    for t in fam.terms:
        for code in t.used_in:
            if code not in codes:
                errors.append(f"term {t.id}: usedIn {code} has no spec with that odpv_code")
    return errors


def markdown_headings(md: str) -> list[str]:
    out, fenced = [], False
    for line in md.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = re.match(r"#{1,6}\s+(.*\S)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def term_concept_name(term: Term, merge_single_word: set[str]) -> str:
    """The concept name for an ODPV object term.

    A one-word label that is an ordinary English word ("Domain", "Owner",
    "Risk") is qualified, so it can't merge into a different concept of
    the same name elsewhere in the KB.
    """
    label = term.label
    if " " in label or label in merge_single_word:
        return label
    return f"{label} (ODPV)"


def _distinctive_word(word: str) -> bool:
    """A one-word name that can't be an ordinary word: camelCase or an acronym."""
    return bool(re.search(r"[a-z][A-Z]", word)) or (len(word) >= 2 and word.isupper())


ALIAS_ANCHOR_WORDS = {"data", "product", "portfolio"}


def term_aliases(term: Term, concept_name: str) -> list[str]:
    """Aliases worth registering for an ODPV term.

    An alias is matched before anything else when later extractions resolve
    names, so a loose one ("API", "metric", "trigger", "subject area") would
    pull unrelated mentions into this term. Kept:
      * the camelCase ODPV id or field name ("DataProduct", "dataAccess"),
        or the id when it is an acronym ("KPI");
      * multi-word aliases that share a word with the label or say
        data / product / portfolio ("data steward", "business goal"),
        or spell out an acronym label ("key performance indicator").
    """
    label_words = {w.lower() for w in term.label.split()}
    out: list[str] = []
    for a in [term.id, term.label, *term.aliases]:
        a = (a or "").strip()
        if not a or a.lower() == concept_name.lower():
            continue
        words = a.split()
        if len(words) == 1:
            camel = bool(re.search(r"[a-z][A-Z]", a))
            if not (camel or (a == term.id and a.isupper() and len(a) >= 2)):
                continue
        else:
            lw = {w.lower() for w in words}
            initials = "".join(w[0] for w in words).upper()
            if not (lw & (label_words | ALIAS_ANCHOR_WORDS) or initials == term.label):
                continue
        if a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


def relationship_lines(term: Term, rels: list[Term]) -> list[str]:
    """ODPV relationships whose domain includes this term, as text."""
    lines = []
    for r in rels:
        if term.id in r.domain and r.range:
            lines.append(f"{term.id} {r.id} {', '.join(r.range)}")
    return lines


# ---------------------------------------------------------------------------
# Family map rendering (ODPV tables)
# ---------------------------------------------------------------------------

def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_odpv_tables(fam: Family) -> str:
    parts = [ODPV_BEGIN, ""]
    by_group: dict[str, list[Term]] = {}
    for t in fam.terms:
        by_group.setdefault(t.group, []).append(t)
    for group in ("core", "value", "governance"):
        parts += [f"## {ODPV_GROUP_HEADINGS[group]}", "",
                  "| Term | Definition | Also known as | Used in |",
                  "| --- | --- | --- | --- |"]
        for t in by_group.get(group, []):
            parts.append(f"| {t.label} (`{t.id}`) | {_cell(t.definition)} | "
                         f"{_cell(', '.join(t.aliases))} | {', '.join(t.used_in)} |")
        parts.append("")
    parts += [f"## {ODPV_GROUP_HEADINGS['relationships']}", "",
              "Relationship terms name the edges in ODPG graphs, with the object types they usually connect.",
              "", "| Relationship | Definition | From | To |", "| --- | --- | --- | --- |"]
    for r in fam.relationships:
        parts.append(f"| `{r.id}` | {_cell(r.definition)} | {', '.join(r.domain) or 'any'} | "
                     f"{', '.join(r.range) or 'any'} |")
    parts += ["", ODPV_END]
    return "\n".join(parts)


def render_family_map(fam: Family, md: str) -> str:
    start, end = md.index(ODPV_BEGIN), md.index(ODPV_END) + len(ODPV_END)
    return md[:start] + render_odpv_tables(fam) + md[end:]


# ---------------------------------------------------------------------------
# Map plan + apply
# ---------------------------------------------------------------------------

@dataclass
class ConceptPlan:
    key: str                  # spec key, or "odpv:<id>"
    name: str
    concept_type: str
    description: str
    aliases: list[str]
    concept_id: Optional[int] = None    # existing match
    doc_source_ids: list[int] = field(default_factory=list)


@dataclass
class EdgePlan:
    from_key: str
    to_key: str
    type: str
    confidence: float
    section_id: int


@dataclass
class MapPlan:
    concepts: dict[str, ConceptPlan]
    edges: list[EdgePlan]
    skipped: list[str]


def _source_id(conn, source_type: str, identifier: str) -> Optional[int]:
    row = conn.execute("SELECT doc_source_id FROM doc_source WHERE source_type = ? "
                       "AND identifier = ?", [source_type, identifier]).fetchone()
    return int(row[0]) if row else None


def section_ids_by_heading(conn, doc_source_id: int) -> dict[str, int]:
    """Heading → first section id in the latest snapshot of a source."""
    rows = conn.execute(
        """
        SELECT heading_text, doc_section_id FROM doc_section
         WHERE snapshot_id = (SELECT max(snapshot_id) FROM doc_snapshot
                               WHERE doc_source_id = ?)
           AND heading_text IS NOT NULL
         ORDER BY ordinal, doc_section_id
        """, [doc_source_id]).fetchall()
    out: dict[str, int] = {}
    for heading, sid in rows:
        out.setdefault(heading.strip(), int(sid))
    return out


def _lookup(resolver, names: list[str]) -> Optional[int]:
    for n in names:
        cid = resolver.resolve_lookup_only(n)
        if cid is not None:
            return int(cid)
    return None


def plan_map(conn, fam: Family, resolver,
             pins: Optional[dict[str, int]] = None) -> MapPlan:
    """Plan the spec map. ``pins`` maps a spec key or ``odpv:<Id>`` to an
    existing concept_id, overriding the name/alias lookup. Use it when a
    name or acronym is already taken by a different standard (Bitol's
    "Open Data Product Standard" is also "ODPS")."""
    pins = dict(pins or {})
    for key, cid in pins.items():
        if not conn.execute("SELECT 1 FROM concept WHERE concept_id = ?", [cid]).fetchone():
            raise SystemExit(f"--pin {key}={cid}: no concept #{cid}")
    map_src = _source_id(conn, "github_raw", FAMILY_MAP_ID)
    if map_src is None:
        raise SystemExit("the family map isn't loaded: run `sources` and refresh it first")
    sections = section_ids_by_heading(conn, map_src)
    if not sections:
        raise SystemExit(f"the family map (doc_source {map_src}) has no sections yet: refresh it")

    concepts: dict[str, ConceptPlan] = {}
    skipped: list[str] = []
    spec_src = {s.key: _source_id(conn, "html_site", s.url) if s.url else None
                for s in fam.specs.values()}
    code_to_key = {s.odpv_code: s.key for s in fam.specs.values() if s.odpv_code}

    for s in fam.specs.values():
        cp = ConceptPlan(s.key, s.name, s.concept_type, s.description, list(s.aliases))
        cp.concept_id = pins.pop(s.key, None) or _lookup(resolver, [s.name, *s.aliases])
        cp.doc_source_ids = [i for i in (spec_src.get(s.key), map_src) if i]
        concepts[s.key] = cp

    odpv_src = spec_src.get("odpv")
    for t in fam.terms:
        name = term_concept_name(t, fam.merge_single_word)
        desc = f"ODPV {t.group} term `{t.id}`: {t.definition}"
        rel = relationship_lines(t, fam.relationships)
        if rel:
            desc += " ODPV relationships: " + "; ".join(rel) + "."
        cp = ConceptPlan(f"odpv:{t.id}", name, fam.term_concept_type, desc,
                         term_aliases(t, name))
        # A qualified name ("Domain (ODPV)") is looked up as itself only: its
        # bare id or aliases would find the unrelated "Domain" or "Product Owner".
        lookups = [name] if name != t.label else [name, t.id]
        cp.concept_id = pins.pop(f"odpv:{t.id}", None) or _lookup(resolver, lookups)
        cp.doc_source_ids = [i for i in (odpv_src, map_src) if i]
        concepts[cp.key] = cp

    if pins:
        raise SystemExit(f"--pin: unknown key(s) {sorted(pins)}")

    edges: list[EdgePlan] = []

    def _endpoint(ref: str) -> Optional[str]:
        if ref in concepts:
            return ref
        if ref.startswith("concept:"):
            # Our own concepts first (an ODPV term of that name), then an
            # existing concept by exact name. Not by alias: our aliases
            # ("knowledge graph" for Data Product Graph) would answer.
            name = ref.split(":", 1)[1]
            own = next((k for k, c in concepts.items()
                        if not k.startswith("concept:") and c.name.lower() == name.lower()), None)
            if own:
                return own
            cid = resolver._exact_match(name, None)  # pylint: disable=protected-access
            if cid is None:
                skipped.append(f"{ref} not in the catalog")
                return None
            concepts.setdefault(ref, ConceptPlan(ref, name, "", "", [], int(cid)))
            return ref
        skipped.append(f"unknown endpoint {ref}")
        return None

    for e in fam.edges:
        a, b = _endpoint(e.from_ref), _endpoint(e.to_ref)
        sid = sections.get(e.section)
        if sid is None:
            skipped.append(f"{e.from_ref}->{e.to_ref}: section {e.section!r} not loaded")
            continue
        if a and b:
            edges.append(EdgePlan(a, b, e.type, e.confidence, sid))

    for t in fam.terms:
        sid = sections.get(ODPV_GROUP_HEADINGS[t.group])
        if sid is None:
            skipped.append(f"ODPV {t.group} section not loaded")
            continue
        for code in t.used_in:
            if code in code_to_key:
                edges.append(EdgePlan(code_to_key[code], f"odpv:{t.id}", "REQUIRES",
                                      fam.used_in_confidence, sid))
        for rel in t.related:
            if f"odpv:{rel}" in concepts and rel != t.id:
                edges.append(EdgePlan(f"odpv:{t.id}", f"odpv:{rel}", "CITES",
                                      fam.related_confidence, sid))
    return MapPlan(concepts, edges, skipped)


def _concept_name(conn, concept_id: int) -> str:
    row = conn.execute("SELECT name FROM concept WHERE concept_id = ?", [concept_id]).fetchone()
    return row[0] if row else ""


def _name_taken(conn, alias: str, concept_id: int) -> bool:
    """True when ``alias`` already names or aliases a different concept."""
    row = conn.execute(
        """
        SELECT 1 FROM concept WHERE lower(name) = lower(?) AND concept_id <> ?
        UNION ALL
        SELECT 1 FROM concept_alias WHERE lower(alias) = lower(?) AND concept_id <> ?
        LIMIT 1
        """, [alias, concept_id, alias, concept_id]).fetchone()
    return row is not None


@dataclass
class ApplySummary:
    concepts_created: int = 0
    concepts_matched: int = 0
    aliases_added: int = 0
    aliases_skipped: int = 0
    edges_added: int = 0
    edges_existing: int = 0
    doc_links_added: int = 0


def apply_map(conn, plan: MapPlan, resolver) -> ApplySummary:
    out = ApplySummary()
    ids: dict[str, int] = {}
    for key, cp in plan.concepts.items():
        if cp.concept_id is not None:
            ids[key] = cp.concept_id
            out.concepts_matched += 1
        else:
            vec = resolver._embed(cp.name, cp.description)  # pylint: disable=protected-access
            ids[key] = resolver._create_concept(            # pylint: disable=protected-access
                cp.name, cp.description, cp.concept_type, vec, pending_review=False)
            out.concepts_created += 1
        cid = ids[key]
        # A concept matched under another name ("ODPR") also answers to the
        # map's own name ("Open Data Product Recipe Specification").
        names = list(cp.aliases)
        if cp.concept_id is not None and cp.name and cp.name not in names:
            names.insert(0, cp.name)
        for alias in names:
            if alias.lower() == _concept_name(conn, cid).lower():
                continue
            if _name_taken(conn, alias, cid):
                out.aliases_skipped += 1
            elif resolver.register_alias(cid, alias, "odp") is not None:
                out.aliases_added += 1
        for src in cp.doc_source_ids:
            if not conn.execute("SELECT 1 FROM concept_doc_link WHERE concept_id = ? "
                                "AND doc_source_id = ?", [cid, src]).fetchone():
                conn.execute("INSERT INTO concept_doc_link (concept_id, doc_source_id, notes) "
                             "VALUES (?, ?, ?)", [cid, src, LINK_NOTE])
                out.doc_links_added += 1
    for e in plan.edges:
        a, b = ids[e.from_key], ids[e.to_key]
        if a == b:
            continue
        if conn.execute(
                "SELECT 1 FROM concept_relation WHERE from_concept_id = ? AND to_concept_id = ? "
                "AND relation_type = ? AND source_type = 'doc_section' AND source_id = ?",
                [a, b, e.type, e.section_id]).fetchone():
            out.edges_existing += 1
            continue
        conn.execute(
            "INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
            "confidence, source_type, source_id) VALUES (?, ?, ?, ?, 'doc_section', ?)",
            [a, b, e.type, e.confidence, e.section_id])
        out.edges_added += 1
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_specs(args) -> int:
    import html_doc  # noqa: PLC0415
    fam = load_family()
    rc = 0
    for s in fam.specs.values():
        if not s.url:
            continue
        name = f"{s.name} ({s.aliases[1]})"
        if args.render_dir:
            args.render_dir.mkdir(parents=True, exist_ok=True)
            out = args.render_dir / f"{s.key}.md"
            rc |= html_doc.do_render(SimpleNamespace(url=[s.url], html_file=[], name=name, out=out))
            continue
        rc |= html_doc.do_register(SimpleNamespace(
            url=[s.url], html_file=[], name=name, identifier=s.url,
            authority=SPEC_AUTHORITY, catalog=args.catalog))
    return rc


def cmd_sources(args) -> int:
    if args.dry_run:
        for seed in ODP_SOURCE_SEEDS:
            print(f"would seed {seed['source_type']:<10} {seed['authority_score']:.2f}  "
                  f"{seed['name']}  ({seed['identifier']})")
        print(f"{len(ODP_SOURCE_SEEDS)} source(s)")
        return 0
    conn = duckdb.connect(str(args.catalog))
    try:
        counts: dict[str, int] = {}
        for seed in ODP_SOURCE_SEEDS:
            status = _upsert(conn, seed)
            counts[status] = counts.get(status, 0) + 1
        ids = [r[0] for r in conn.execute(
            "SELECT doc_source_id FROM doc_source WHERE identifier IN "
            f"({', '.join('?' * len(ODP_SOURCE_SEEDS))}) ORDER BY doc_source_id",
            [s["identifier"] for s in ODP_SOURCE_SEEDS]).fetchall()]
    finally:
        conn.close()
    print(f"{counts}; doc_source ids {ids[0]}-{ids[-1]}" if ids else counts)
    return 0


def _resolver(conn, *, load_model: bool):
    from resolution import EntityResolver  # noqa: PLC0415
    model = None
    if load_model:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return EntityResolver(conn, model=model)


def print_plan(plan: MapPlan) -> None:
    new = [c for c in plan.concepts.values() if c.concept_id is None]
    matched = [c for c in plan.concepts.values() if c.concept_id is not None]
    print(f"concepts: {len(matched)} existing, {len(new)} new")
    for c in sorted(matched, key=lambda c: c.key):
        print(f"  = #{c.concept_id:<8} {c.name}  [{c.key}]")
    for c in sorted(new, key=lambda c: c.key):
        print(f"  + {c.name}  [{c.key}]  aliases: {', '.join(c.aliases) or '-'}")
    by_type: dict[str, int] = {}
    for e in plan.edges:
        by_type[e.type] = by_type.get(e.type, 0) + 1
    print(f"edges: {len(plan.edges)} {by_type}")
    for line in plan.skipped:
        print(f"  skipped: {line}")


def _parse_pins(items: Optional[list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items or []:
        key, sep, cid = item.partition("=")
        if not sep or not cid.strip().isdigit():
            raise SystemExit(f"--pin expects KEY=CONCEPT_ID, got {item!r}")
        out[key.strip()] = int(cid)
    return out


def cmd_map(args) -> int:
    fam = load_family()
    errors = validate_family(fam)
    if errors:
        print("\n".join(errors))
        return 1
    if args.write:
        import refresh_docs  # noqa: PLC0415
        conn = refresh_docs.open_writer(args.catalog)
    else:
        conn = duckdb.connect(str(args.catalog), read_only=True)
    try:
        resolver = _resolver(conn, load_model=args.write)
        plan = plan_map(conn, fam, resolver, pins=_parse_pins(args.pin))
        print_plan(plan)
        if args.write:
            print(apply_map(conn, plan, resolver))
        else:
            print("dry run: add --write to apply")
    finally:
        conn.close()
    return 0


def cmd_render_map(args) -> int:
    fam = load_family()
    md = FAMILY_MAP_MD.read_text()
    new = render_family_map(fam, md)
    if args.check:
        if new != md:
            print(f"{FAMILY_MAP_MD} is out of date: run render-map")
            return 1
        print("up to date")
        return 0
    FAMILY_MAP_MD.write_text(new)
    print(f"wrote {FAMILY_MAP_MD}")
    return 0


def cmd_status(args) -> int:
    fam = load_family()
    conn = duckdb.connect(str(args.catalog), read_only=True)
    try:
        idents = [s["identifier"] for s in ODP_SOURCE_SEEDS] + \
                 [s.url for s in fam.specs.values() if s.url]
        rows = conn.execute(
            f"""
            SELECT d.doc_source_id, d.source_type, d.name,
                   (SELECT count(*) FROM doc_section ds
                     WHERE ds.snapshot_id = (SELECT max(snapshot_id) FROM doc_snapshot
                                              WHERE doc_source_id = d.doc_source_id)) AS sections
              FROM doc_source d WHERE d.identifier IN ({', '.join('?' * len(idents))})
             ORDER BY d.doc_source_id
            """, idents).fetchall()
        for r in rows:
            print(f"#{r[0]:<6} {r[1]:<10} {r[3]:>4} sections  {r[2]}")
        print(f"{len(rows)}/{len(idents)} sources loaded")
        skip = {f"{SDK}:{p}" for p in NO_EXTRACT}
        extract = conn.execute(
            f"SELECT doc_source_id FROM doc_source WHERE identifier IN "
            f"({', '.join('?' * len(idents))}) ORDER BY doc_source_id",
            idents).fetchall()
        skip_ids = {r[0] for r in conn.execute(
            f"SELECT doc_source_id FROM doc_source WHERE identifier IN "
            f"({', '.join('?' * len(skip))})", list(skip)).fetchall()} if skip else set()
        print("extract: " + " ".join(str(r[0]) for r in extract if r[0] not in skip_ids))
        n_edges = conn.execute(
            "SELECT count(*) FROM concept_doc_link WHERE notes = ?", [LINK_NOTE]).fetchone()[0]
        print(f"{n_edges} concept_doc_link rows from the map")
    finally:
        conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("specs", help="register the five spec pages (html_site)")
    sp.add_argument("--render-dir", type=Path, help="write Markdown here for review; no DB")
    sp.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    sp.set_defaults(func=cmd_specs)

    so = sub.add_parser("sources", help="seed SDK, FAQ, ODCS and family-map sources")
    so.add_argument("--dry-run", action="store_true")
    so.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    so.set_defaults(func=cmd_sources)

    mp = sub.add_parser("map", help="seed spec + ODPV concepts and curated edges")
    mp.add_argument("--write", action="store_true")
    mp.add_argument("--pin", action="append", metavar="KEY=CONCEPT_ID",
                    help="use this existing concept for a spec key (odps) or "
                         "ODPV term (odpv:DataProduct); repeatable")
    mp.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    mp.set_defaults(func=cmd_map)

    rm = sub.add_parser("render-map", help="regenerate the ODPV tables in the family map")
    rm.add_argument("--check", action="store_true")
    rm.set_defaults(func=cmd_render_map)

    st = sub.add_parser("status")
    st.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
