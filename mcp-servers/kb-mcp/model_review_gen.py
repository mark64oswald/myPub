"""model_review_gen.py — ``/kb-model-review``: review a data model against the patterns (2026-11).

Input: a data model as a Databricks ``model.json``, a DDL file or folder of
``.sql`` files, or a reference model already in the catalog
(``ref:<id>`` or ``<source>:<industry>[:<flavour>]``).

Structural rules (no FK cycles, naming, PK suffixes) are what generators
like Databricks' Vibe Data Modeling agent already enforce. This review
checks what they don't: whether the model uses the data model patterns
the library teaches (Silverston, Hay, Fowler, Blaha). All checks are
deterministic:

1. Party and roles: role tables (patient, clinician, guarantor...) that
   each copy a person's identity instead of referencing one Party.
2. One owner per concept: the same business concept modeled in several
   domains (coverage four ways, eligibility twice), found by pattern links
   plus name and attribute overlap.
3. Contact mechanisms: address/phone/e-mail columns repeated across tables.
4. Status history: transactional entities with a status column and no
   dated status history.
5. Classification: free-text *_type / *_code / *_category columns with no
   reference table behind them.
6. Provenance: FKs whose justification the generating LLM invented
   (Databricks marks them "LLM Generated Cross-Domain Link").
7. Structure: FK cycles, tables with no PK, tables with no relationships.
8. Reference comparison: entities of the same industry in Silverston's
   Vol. 2 model and OMOP CDM (when loaded) with no counterpart here.

Each finding cites the pattern's books from the library and carries a
**vibe**: a plain-English change request written so it can be pasted into
the Vibe Data Modeling agent (or any modeling agent) as-is.

Output:

    <package>/
      review.md          grade, section table, findings with books and vibes
      findings.json      the same, machine-readable
      vibes.txt          numbered change requests, ready to paste into the agent
      _provenance.md
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import duckdb

import ref_models as rm
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "model_review"

# Databricks industry names -> the Silverston Vol. 2 chapter that covers them.
SILVERSTON_COUNTERPART = {
    "healthcare": "healthcare", "health_insurance": "insurance", "life_insurance": "insurance",
    "pc_insurance": "insurance", "banking": "financial_services",
    "payments_fintech": "financial_services", "telecommunication": "telecommunications",
    "manufacturing": "manufacturing", "travel_hospitality": "travel", "airlines": "travel",
    "ecommerce": "ecommerce", "retail": "ecommerce", "staffing_hr": "professional_services",
    "legal": "professional_services",
}
OMOP_INDUSTRIES = {"healthcare", "clinical_trials", "health_insurance"}

# (section, weight, title, pattern key for book backing)
SECTIONS = [
    ("party_role", 3, "Party and roles", "party_role"),
    ("single_owner", 3, "One owner per concept", "coverage"),
    ("contact_mechanism", 2, "Contact mechanisms", "contact_mechanism"),
    ("status_history", 1, "Status history", "status"),
    ("classification", 1, "Classification", "classification"),
    ("provenance", 2, "Relationship provenance", None),
    ("structure", 2, "Structure", None),
    ("reference", 1, "Reference comparison", None),
]

_GENERIC = {"id", "key", "record", "detail", "details", "info", "information", "data", "master",
            "entry", "item", "line", "event", "history", "log", "reference", "type", "code",
            "status", "date", "flag", "number", "name", "list", "set", "table", "fact", "dim"}
_ADDRESS = re.compile(r"(^|_)(address(_line)?(_?\d)?|street(_address)?|city|postal_code|zip(_code)?|"
                      r"phone(_number)?|mobile(_phone)?|email(_address)?|fax)($|_)")
_TRANSACTIONAL = {"order", "claim", "encounter", "invoice", "payment", "agreement", "coverage",
                  "work_effort"}


@dataclass
class Finding:
    section: str
    severity: str                     # "error" | "warning" | "advice"
    title: str
    detail: str
    entities: list[str] = field(default_factory=list)
    vibe: str = ""
    books: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_input(conn: Any, spec: str) -> tuple[Optional[rm.RefModel], Optional[int], str]:
    """Resolve ``spec`` to (model, ref_model_id or None, error)."""
    spec = (spec or "").strip()
    m = re.fullmatch(r"ref:(\d+)", spec)
    if m:
        try:
            return rm.read_model(conn, int(m.group(1))), int(m.group(1)), ""
        except Exception as exc:
            return None, None, str(exc)
    m = re.fullmatch(r"(databricks|silverston|omop|user|generated):([a-z0-9_]+)(?::([a-z0-9_]+))?",
                     spec)
    if m:
        try:
            mid = rm.find_model(conn, m.group(1), m.group(2), m.group(3) or
                                {"databricks": "mvm", "silverston": "book", "omop": "cdm"}.get(
                                    m.group(1), "ddl"))
        except Exception:
            mid = None
        if mid is None:
            return None, None, f"no reference model {spec} in the catalog (load it first)"
        return rm.read_model(conn, mid), mid, ""
    path = Path(spec).expanduser()
    if path.is_dir():
        if (path / "model.json").is_file():
            path = path / "model.json"
        else:
            sqls = sorted(path.rglob("*.sql"))
            if not sqls:
                return None, None, f"no model.json or .sql files under {path}"
            ddl = "\n".join(p.read_text() for p in sqls)
            return rm.parse_ddl_model(ddl, industry=_guess_industry(path), name=path.name), None, ""
    if not path.is_file():
        return None, None, f"not found: {spec}"
    if path.suffix == ".json":
        try:
            return rm.parse_databricks_model(path), None, ""
        except Exception as exc:
            return None, None, f"can't read {path.name} as a Databricks model.json: {exc}"
    return rm.parse_ddl_model(path.read_text(), industry=_guess_industry(path),
                              name=path.stem), None, ""


def _guess_industry(path: Path) -> str:
    for part in reversed(path.resolve().parts):
        if part in SILVERSTON_COUNTERPART or part in OMOP_INDUSTRIES:
            return part
    return "custom"


def pattern_links(conn: Any, model: rm.RefModel, ref_model_id: Optional[int],
                  catalog: rm.PatternCatalog) -> dict[str, dict[str, float]]:
    """entity_key -> {pattern_key: confidence}. Uses the catalog's confirmed
    links when the model is loaded, else the heuristics."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    if ref_model_id is not None:
        for key, links in rm.entity_patterns(conn, ref_model_id, min_confidence=0.0).items():
            for pk, conf, _m in links:
                out[key][pk] = max(conf, out[key].get(pk, 0.0))
        if out:
            return out
    for e in model.entities:
        for pk, conf, _r in rm.classify_entity(e, catalog):
            out[e.key][pk] = conf
        if len(rm.carries_person_identity(e)) >= 2 and not ({"party", "party_role"} & set(out[e.key])):
            out[e.key]["party_role"] = 0.45
    return out


# ---------------------------------------------------------------------------
# Checks (pure functions over a RefModel + its pattern links)
# ---------------------------------------------------------------------------

def _has(links: dict[str, float], key: str, floor: float = 0.45) -> bool:
    return links.get(key, 0.0) >= floor


def check_party_role(model: rm.RefModel, links: dict[str, dict[str, float]]) -> list[Finding]:
    roles, supers = [], []
    for e in model.entities:
        lk = links.get(e.key, {})
        if _has(lk, "party", 0.6) and e.name in {"party", "person", "individual", "organization"}:
            supers.append(e.key)
        ident = rm.carries_person_identity(e)
        if _has(lk, "party_role") and len(ident) >= 2:
            roles.append((e.key, ident))
    if len(roles) < 2:
        return []
    names = [k for k, _ in roles]
    detail = (f"{len(roles)} role tables each carry their own copy of a person's identity: "
              + "; ".join(f"{k} ({', '.join(i[:4])})" for k, i in roles[:8])
              + ("" if not supers else f". A party table exists ({', '.join(supers)}) but these "
                 "roles don't use it for identity."))
    targets = ", ".join(k.split(".")[-1] for k in names[:8])
    vibe = (f"Introduce a party domain with party (supertype), person and organization, and a "
            f"party_role table typed by role. Make {targets} role tables that reference party_id, "
            "and move name, birth date, sex, SSN and similar identity columns from them to person, "
            "so one human who is both patient and guarantor (or clinician) exists once.")
    return [Finding("party_role", "error" if not supers else "warning",
                    "Person identity is copied onto each role", detail, names, vibe)]


def _name_tokens(name: str) -> set[str]:
    return {t for t in name.split("_") if t and t not in _GENERIC and len(t) > 2}


def _attr_set(e: rm.RefEntity) -> set[str]:
    pk = set(e.primary_key.split(","))
    return {a.name for a in e.attributes if a.name not in pk and not a.name.endswith("_id")
            and a.name not in {"created_at", "updated_at", "created_by", "updated_by",
                               "record_source", "load_timestamp", "is_active", "effective_date",
                               "created_timestamp", "updated_timestamp", "source_system_code",
                               "source_system_record_code",
                               "end_date", "start_date"}}


def check_single_owner(model: rm.RefModel, links: dict[str, dict[str, float]],
                       catalog: rm.PatternCatalog) -> list[Finding]:
    out: list[Finding] = []
    seen_groups: list[set[str]] = []
    # (a) patterns flagged single_owner (coverage...) held in more than one domain
    for p in catalog.patterns:
        if p.review != "single_owner":
            continue
        members = [e for e in model.entities if _has(links.get(e.key, {}), p.key, 0.6)
                   and not re.search(r"_(status_)?history$", e.name)]
        # A member that references another member and repeats at most one of
        # its columns depends on it (a use of the concept, not a second copy).
        mkeys = {e.key for e in members}
        idx = model.entity_index()
        by_name = {e.name: e.key for e in model.entities}

        def dependent(e: rm.RefEntity) -> bool:
            for a in e.attributes:
                t = _fk_key(a.fk_to, idx, by_name) if a.fk_to and not a.is_pk else None
                if t in mkeys - {e.key}:
                    if len(_attr_set(e) & _attr_set(idx[t])) <= 1:
                        return True
            return False

        members = [e for e in members if not dependent(e)] or members[:1]
        domains = {e.domain for e in members}
        if len(members) >= 2 and len(domains) >= 2:
            keys = [e.key for e in members]
            seen_groups.append(set(keys))
            owner = max(members, key=lambda e: len(e.attributes))
            others = [k for k in keys if k != owner.key]
            out.append(Finding(
                "single_owner", "error", f"{p.name} is modeled {len(members)} times",
                f"{', '.join(keys)} all hold {p.name.lower()} across {len(domains)} domains.",
                keys,
                f"Make {owner.key} the single owner of {p.name.lower()}. Replace "
                f"{', '.join(others)} with foreign keys to {owner.key} (keep only columns that are "
                f"genuinely specific to each use, such as a billing sequence number)."))
    # (b) same-concept pairs across domains by name tokens + attribute overlap
    by_token: dict[str, list[rm.RefEntity]] = defaultdict(list)
    for e in model.entities:
        if re.search(r"_(status_)?history$", e.name):
            continue      # history tables share their shape by design
        for t in _name_tokens(e.name):
            by_token[t].append(e)
    pairs = []
    for t, ents in by_token.items():
        if len(ents) < 2 or len(ents) > 6:
            continue
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                if a.domain == b.domain:
                    continue
                sa, sb = _attr_set(a), _attr_set(b)
                if not sa or not sb:
                    continue
                jac = len(sa & sb) / len(sa | sb)
                if jac >= 0.3:
                    pairs.append((jac, a.key, b.key, t))
    for jac, a, b, t in sorted(pairs, reverse=True):
        if any({a, b} <= g for g in seen_groups):
            continue
        seen_groups.append({a, b})
        out.append(Finding(
            "single_owner", "warning", f"'{t}' appears in two domains",
            f"{a} and {b} share {jac:.0%} of their descriptive columns.", [a, b],
            f"Decide whether {a} and {b} are the same business concept. If so, keep one owner "
            f"and have the other domain reference it; if not, rename them so the difference is "
            "visible."))
        if len(out) >= 12:
            break
    return out


def _fk_key(fk_to: str, idx: dict[str, rm.RefEntity], by_name: dict[str, str]) -> Optional[str]:
    parts = fk_to.split(".")
    if len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in idx:
        return f"{parts[0]}.{parts[1]}"
    return by_name.get(parts[-2] if len(parts) >= 2 else parts[0])


def check_contact_mechanism(model: rm.RefModel, links: dict[str, dict[str, float]]) -> list[Finding]:
    hits = []
    for e in model.entities:
        if _has(links.get(e.key, {}), "contact_mechanism", 0.6):
            continue
        cols = [a.name for a in e.attributes if _ADDRESS.search(a.name)]
        if len(cols) >= 3:
            hits.append((e.key, cols))
    if len(hits) < 2:
        return []
    keys = [k for k, _ in hits]
    return [Finding(
        "contact_mechanism", "warning", f"Address and phone columns repeated on {len(hits)} tables",
        "; ".join(f"{k} ({', '.join(c[:4])}{'…' if len(c) > 4 else ''})" for k, c in hits[:8]),
        keys,
        "Hold postal addresses, phone numbers and e-mail addresses as contact mechanisms (postal "
        "address, telecommunications number, electronic address) linked to the party with a "
        f"purpose and from/thru dates, and remove the inline address columns from "
        f"{', '.join(k.split('.')[-1] for k in keys[:6])}.")]


def check_status_history(model: rm.RefModel, links: dict[str, dict[str, float]]) -> list[Finding]:
    names = {e.name for e in model.entities}
    missing = []
    for e in model.entities:
        lk = links.get(e.key, {})
        if not any(_has(lk, k, 0.6) for k in _TRANSACTIONAL):
            continue
        if not any(a.name.endswith("status") for a in e.attributes):
            continue
        if any(n.startswith(e.name) and ("status" in n or "history" in n) for n in names
               if n != e.name):
            continue
        missing.append(e.key)
    if not missing:
        return []
    return [Finding(
        "status_history", "advice", f"{len(missing)} transactional tables keep only their current status",
        "No dated status history for: " + ", ".join(missing[:12]) + ("…" if len(missing) > 12 else ""),
        missing,
        f"Add a status history table (status type, status datetime, entered by) for "
        f"{', '.join(k.split('.')[-1] for k in missing[:6])}, so the model records when each "
        "state applied and not only the latest one.")]


def check_classification(model: rm.RefModel) -> list[Finding]:
    rows = []
    for e in model.entities:
        free = [a.name for a in e.attributes
                if re.search(r"_(type|category|class|code)$", a.name) and not a.fk_to
                and not a.is_pk and (a.data_type or "").upper().startswith(("STRING", "VARCHAR",
                                                                             "CHAR", "TEXT", ""))]
        if free:
            rows.append((e.key, free))
    total = sum(len(f) for _k, f in rows)
    if total < 5:
        return []
    common = defaultdict(int)
    for _k, f in rows:
        for c in f:
            common[c] += 1
    top = sorted(common.items(), key=lambda kv: -kv[1])[:8]
    return [Finding(
        "classification", "advice", f"{total} type/code/category columns have no reference table",
        "Most repeated: " + ", ".join(f"{c} ({n})" for c, n in top), [k for k, _ in rows[:20]],
        "Create reference (classification) tables for the type and category columns used across "
        f"several tables, starting with {', '.join(c for c, _ in top[:4])}, and turn those columns "
        "into foreign keys; keep inline value lists only for small, stable enumerations.")]


def check_provenance(model: rm.RefModel) -> list[Finding]:
    gen = [(e.key, a.name, a.fk_to) for e in model.entities for a in e.attributes
           if a.generated_ref]
    if not gen:
        return []
    n_fk = sum(1 for e in model.entities for a in e.attributes if a.fk_to)
    sample = "; ".join(f"{k}.{c} -> {t}" for k, c, t in gen[:8])
    return [Finding(
        "provenance", "warning",
        f"{len(gen)} of {n_fk} foreign keys were justified by the generating model, not a source",
        f"Examples: {sample}", sorted({k for k, _c, _t in gen})[:30],
        "Review the cross-domain links marked 'LLM Generated Cross-Domain Link'. Remove any that "
        "no business process needs, and for the rest record the governing standard or source "
        "system as the reference.")]


def _cycles(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan)."""
    graph: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        if a != b:
            graph[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        work = [(v, iter(graph[v]))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, it = work[-1]
            nxt = next(it, None)
            if nxt is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))
            elif nxt not in index:
                index[nxt] = low[nxt] = counter[0]
                counter[0] += 1
                stack.append(nxt)
                on.add(nxt)
                work.append((nxt, iter(graph[nxt])))
            elif nxt in on:
                low[node] = min(low[node], index[nxt])

    for n in nodes:
        if n not in index:
            strong(n)
    return out


def check_structure(model: rm.RefModel) -> list[Finding]:
    out = []
    rels = model.relationships()
    keys = [e.key for e in model.entities]
    cycles = _cycles(keys, [(a, b) for a, b, _ in rels])
    if cycles:
        out.append(Finding("structure", "warning", f"{len(cycles)} foreign-key cycles",
                           "; ".join(" ↔ ".join(c[:5]) for c in cycles[:5]),
                           [k for c in cycles for k in c][:20],
                           "Break each foreign-key cycle by keeping the reference on the child "
                           "side only, or by moving the shared fact to an associative table."))
    no_pk = [e.key for e in model.entities if not e.primary_key]
    if no_pk:
        out.append(Finding("structure", "warning", f"{len(no_pk)} tables without a primary key",
                           ", ".join(no_pk[:12]), no_pk,
                           f"Give {', '.join(k.split('.')[-1] for k in no_pk[:6])} a primary key."))
    touched = {a for a, _b, _ in rels} | {b for _a, b, _ in rels}
    silo = [k for k in keys if k not in touched]
    if silo and len(model.entities) > 3:
        out.append(Finding("structure", "advice", f"{len(silo)} tables with no relationships",
                           ", ".join(silo[:12]), silo,
                           f"Connect {', '.join(k.split('.')[-1] for k in silo[:6])} to the "
                           "entities they describe, or drop them if nothing uses them."))
    return out


def _match_score(a: rm.RefEntity, b: rm.RefEntity) -> float:
    ta, tb = _name_tokens(a.name) or {a.name}, _name_tokens(b.name) or {b.name}
    name = len(ta & tb) / max(1, len(ta | tb))
    return name


def check_reference(model: rm.RefModel, refs: Sequence[rm.RefModel]) -> list[Finding]:
    out = []
    for ref in refs:
        if not ref.entities:
            continue
        missing = []
        for r in ref.entities:
            if r.name.endswith("_type") or r.name in {"metadata", "cdm_source"}:
                continue
            best = max((_match_score(r, e) for e in model.entities), default=0.0)
            if best < 0.5:
                missing.append(r.name)
        if not missing:
            continue
        cov = 1 - len(missing) / max(1, len(ref.entities))
        label = ref.name or f"{ref.source} {ref.industry}"
        out.append(Finding(
            "reference", "advice",
            f"{len(missing)} entities in {label} have no obvious counterpart",
            f"Name-level coverage {cov:.0%}. Not matched: " + ", ".join(missing[:25])
            + ("…" if len(missing) > 25 else ""), [],
            f"Check whether these {label} entities matter for this business: "
            f"{', '.join(missing[:8])}. Add the ones that do, or note why they are out of scope."))
    return out


def run_checks(model: rm.RefModel, links: dict[str, dict[str, float]],
               catalog: rm.PatternCatalog, refs: Sequence[rm.RefModel] = ()) -> list[Finding]:
    return (check_party_role(model, links) + check_single_owner(model, links, catalog)
            + check_contact_mechanism(model, links) + check_status_history(model, links)
            + check_classification(model) + check_provenance(model) + check_structure(model)
            + check_reference(model, refs))


def score(findings: Sequence[Finding]) -> dict[str, int]:
    """2 = clean, 1 = advice/warnings, 0 = an error, per section."""
    out = {}
    for name, _w, _t, _p in SECTIONS:
        sev = {f.severity for f in findings if f.section == name}
        out[name] = 0 if "error" in sev else 1 if sev & {"warning"} else 2 if not sev else 1
        if sev == {"advice"}:
            out[name] = 2 if name in ("reference", "classification", "status_history") else 1
    return out


def grade(scores: dict[str, int]) -> tuple[str, float]:
    weights = {n: w for n, w, _t, _p in SECTIONS}
    total = sum(2 * w for w in weights.values())
    pct = sum(scores[n] * weights[n] for n in weights) / total
    letter = "A" if pct >= 0.9 else "B" if pct >= 0.75 else "C" if pct >= 0.6 else \
        "D" if pct >= 0.45 else "F"
    return letter, pct


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_review(model: rm.RefModel, letter: str, pct: float, scores: dict[str, int],
                  findings: Sequence[Finding], stats: dict[str, Any]) -> str:
    title = model.name or model.industry
    lines = [f"# Model review: {title}", "",
             f"**Grade {letter}** ({pct:.0%}) · {stats['entities']} entities · "
             f"{stats['attributes']} attributes · {stats['relationships']} relationships · "
             f"source: {model.source} {model.flavour} {model.version}".rstrip(), "",
             "This review checks the model against data model patterns from the library "
             "(Silverston, Hay, Fowler, Blaha), not only structural rules. Each finding ends "
             "with a vibe: a change request you can paste into the Vibe Data Modeling agent "
             "as-is (all of them are in `vibes.txt`).", "",
             "| Section | Score | Findings |", "|---|---|---|"]
    mark = {2: "✓ clean", 1: "△ review", 0: "✗ fix"}
    for name, _w, ttl, _p in SECTIONS:
        n = sum(1 for f in findings if f.section == name)
        lines.append(f"| {ttl} | {mark[scores[name]]} | {n} |")
    order = {"error": 0, "warning": 1, "advice": 2}
    for name, _w, ttl, _p in SECTIONS:
        fs = sorted((f for f in findings if f.section == name), key=lambda f: order[f.severity])
        if not fs:
            continue
        lines += ["", f"## {ttl}", ""]
        for f in fs:
            lines += [f"### {f.severity.upper()}: {f.title}", "", f.detail, ""]
            if f.books:
                lines += ["Read: " + "; ".join(f.books), ""]
            lines += [f"> **Vibe:** {f.vibe}", ""]
    if not findings:
        lines += ["", "No findings: the model follows the patterns this review checks."]
    return "\n".join(lines).rstrip() + "\n"


def render_vibes(findings: Sequence[Finding]) -> str:
    order = {"error": 0, "warning": 1, "advice": 2}
    fs = [f for f in sorted(findings, key=lambda f: order[f.severity]) if f.vibe]
    return "\n\n".join(f"{i}. {f.vibe}" for i, f in enumerate(fs, start=1)) + ("\n" if fs else "")


# ---------------------------------------------------------------------------
# Generator wiring
# ---------------------------------------------------------------------------

def _books_for(conn: Any, catalog: rm.PatternCatalog, pattern_key: Optional[str]) -> list[str]:
    if not pattern_key:
        return []
    import odp_gen as og  # noqa: PLC0415  (book_citations)
    try:
        p = catalog.get(pattern_key)
    except KeyError:
        return []
    labels: list[str] = []
    for b in p.books[:3]:
        cites = og.book_citations(conn, [catalog.book_title(b["book"])],
                                  keywords=tuple(b.get("keywords") or (p.name,)), per_book=1)
        labels += [c.label for c in cites]
    return labels


def _reference_models(conn: Any, model: rm.RefModel, ref_model_id: Optional[int]) -> list[rm.RefModel]:
    out = []
    wanted = []
    s = SILVERSTON_COUNTERPART.get(model.industry)
    if s and model.source != "silverston":
        wanted.append(("silverston", s, "book"))
    if model.industry in OMOP_INDUSTRIES and model.source != "omop":
        wanted.append(("omop", "healthcare", "cdm"))
    for src, ind, fl in wanted:
        try:
            mid = rm.find_model(conn, src, ind, fl)
        except Exception:
            mid = None
        if mid is not None and mid != ref_model_id:
            out.append(rm.read_model(conn, mid))
    return out


class ModelReviewDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        spec = kw.get("model") or query
        model, mid, error = load_input(conn, spec)
        if model is not None and kw.get("industry"):
            model.industry = kw["industry"]
        catalog = rm.load_patterns()
        return {"spec": spec, "model": model, "ref_model_id": mid, "error": error,
                "catalog": catalog, "compare": kw.get("compare", True)}


class ModelReviewPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        model: Optional[rm.RefModel] = d["model"]
        name = package_name or ("model-review-" + re.sub(
            r"[^a-z0-9]+", "-", ((model.name if model else "") or Path(d["spec"]).stem or "model")
            .lower()).strip("-")[:40])
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=name,
                       domain="data model review", source_query=d["spec"])
        if model is None:
            plan.package_metadata = {"readable": False, "error": d["error"]}
            return plan
        catalog: rm.PatternCatalog = d["catalog"]
        links = pattern_links(conn, model, d["ref_model_id"], catalog)
        refs = _reference_models(conn, model, d["ref_model_id"]) if d["compare"] else []
        findings = run_checks(model, links, catalog, refs)
        section_pattern = {n: p for n, _w, _t, p in SECTIONS}
        book_cache: dict[str, list[str]] = {}
        for f in findings:
            pk = section_pattern.get(f.section)
            if f.section == "single_owner" and "is modeled" not in f.title:
                pk = None
            if pk:
                if pk not in book_cache:
                    book_cache[pk] = _books_for(conn, catalog, pk)
                f.books = book_cache[pk]
        scores = score(findings)
        letter, pct = grade(scores)
        rels = model.relationships()
        stats = {"entities": len(model.entities),
                 "attributes": sum(len(e.attributes) for e in model.entities),
                 "relationships": len(rels),
                 "linked_entities": sum(1 for e in model.entities if links.get(e.key)),
                 "link_source": "catalog" if d["ref_model_id"] is not None else "heuristic"}
        review = render_review(model, letter, pct, scores, findings, stats)
        vibes = render_vibes(findings)
        plan.package_metadata = {
            "readable": True, "model": model.name, "source": model.source,
            "industry": model.industry, "grade": letter, "score": round(pct, 3),
            "sections": scores, "stats": stats,
            "compared_with": [r.name for r in refs],
            "n_errors": sum(f.severity == "error" for f in findings),
            "n_warnings": sum(f.severity == "warning" for f in findings),
            "n_vibes": sum(1 for f in findings if f.vibe)}
        plan.units.append(GenUnit(unit_type="model_review", name=model.name or model.industry,
                                  content_markdown=review, metadata={"grade": letter}))
        plan.files += [
            GenFile(filename="review.md", content=review, purpose="review"),
            GenFile(filename="findings.json", purpose="findings", content=json.dumps(
                {"model": model.name, "source": model.source, "industry": model.industry,
                 "grade": letter, "score": round(pct, 3), "sections": scores, "stats": stats,
                 "findings": [asdict(f) for f in findings]}, indent=2) + "\n"),
            GenFile(filename="vibes.txt", content=vibes or "No changes suggested.\n",
                    purpose="vibes"),
            GenFile(filename="_provenance.md", purpose="provenance", content=(
                f"# Provenance: review of {model.name or d['spec']}\n\n"
                f"Input: `{d['spec']}`. Pattern links: {stats['link_source']} "
                f"({stats['linked_entities']} of {stats['entities']} entities linked). Patterns and "
                "their books: data/ref_models/patterns.yaml. Checks: "
                "mcp-servers/kb-mcp/model_review_gen.py. Compared with: "
                f"{', '.join(r.name for r in refs) or 'nothing (no reference model loaded)'}.\n")),
        ]
        return plan


class ModelReviewValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        if not meta.get("readable"):
            return [ValidationIssue("", "error", f"can't read the model: {meta.get('error')}")]
        return []


def make_model_review_generator() -> Generator:
    import kb_scaffold as ks  # noqa: PLC0415
    return Generator(generator_type=GENERATOR_TYPE, decomposer=ModelReviewDecomposer(),
                     planner=ModelReviewPlanner(), ranking_mode="generation",
                     validator=ModelReviewValidator(), materializer=ks.TreeMaterializer())
