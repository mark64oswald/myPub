"""domain_model_gen.py — ``/kb-domain-model``: a pattern-first domain model (2026-11).

Starts from a base model (a Databricks gallery model, DDL, or a reference
model in the catalog), keeps the part that fits the business description,
and rewrites it with the data model patterns the library teaches:

1. **Party**: a ``party`` domain (party, person, organization, party_role,
   party_relationship). Every table that copies a person's identity becomes
   a role: its name, birth date, SSN... columns move to ``person`` and it
   references ``party_role``.
2. **Contact mechanisms**: postal_address, telecom_number,
   electronic_address and party_contact_mechanism replace inline address,
   phone and e-mail columns.
3. **One owner per concept**: when a single-owner pattern (coverage) is held
   by several tables, the largest becomes the owner; the others keep only
   their own columns and reference it.
4. **Status history**: transactional tables with a status column get a
   dated ``<table>_status_history``.
5. **Scope**: FKs to tables outside the kept set are dropped (and listed).

The result is written in the Databricks ``model.json`` shape (so the Vibe
Data Modeling app, viewer and installer can read it), as Unity Catalog DDL
and as DBML, with ``changes.md`` (every change, why, and the books behind
it) and a before/after ``/kb-model-review`` grade. A subagent prompt
(``_prompts/refine-model.md``) covers what deterministic rules can't: tables
the description needs that the base model lacks.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import duckdb

import model_emit as me
import model_review_gen as mr
import ref_models as rm
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "domain_model"
REFACTORINGS = ("party", "contact_mechanism", "single_owner", "status_history")
_STOP = {"the", "and", "for", "with", "our", "their", "from", "into", "that", "this", "who",
         "what", "which", "are", "is", "of", "to", "in", "on", "a", "an", "we", "by", "or",
         "each", "all", "any", "data", "model", "table", "tables", "record", "records",
         "management", "system", "information", "track", "tracking", "business"}


@dataclass
class Change:
    kind: str
    title: str
    detail: str
    entities: list[str] = field(default_factory=list)
    pattern: str = ""
    books: list[str] = field(default_factory=list)


def _a(name: str, typ: str = "STRING", desc: str = "", fk: Optional[str] = None,
       pk: bool = False) -> rm.RefAttribute:
    return rm.RefAttribute(name=name, data_type=typ, is_pk=pk, fk_to=fk, description=desc,
                           reference="myPub pattern rewrite" if fk else "")


def _e(name: str, domain: str, desc: str, attrs: list[rm.RefAttribute],
       ref: str = "") -> rm.RefEntity:
    pk = next((a.name for a in attrs if a.is_pk), "")
    return rm.RefEntity(name=name, domain=domain, description=desc, primary_key=pk,
                        standard_refs=ref, attributes=attrs)


_AUDIT = [("created_timestamp", "TIMESTAMP", "When the row was created."),
          ("updated_timestamp", "TIMESTAMP", "When the row last changed.")]


def _audit() -> list[rm.RefAttribute]:
    return [_a(n, t, d) for n, t, d in _AUDIT]


SILVERSTON_V1 = "Silverston, The Data Model Resource Book Vol. 1/3"


def party_core(identity_cols: Sequence[rm.RefAttribute]) -> list[rm.RefEntity]:
    person_attrs = [_a("party_id", "BIGINT", "The party this person is.", "party.party.party_id",
                       pk=True)]
    seen = {"party_id"}
    for a in identity_cols:
        if a.name not in seen:
            seen.add(a.name)
            person_attrs.append(rm.RefAttribute(name=a.name, data_type=a.data_type or "STRING",
                                                description=a.description))
    return [
        _e("party", "party", "Any person or organization the business deals with. Roles, "
           "contact mechanisms and relationships hang off the party, not off each role.",
           [_a("party_id", "BIGINT", "Surrogate key.", pk=True),
            _a("party_type", "STRING", "person | organization"),
            _a("display_name", "STRING", "Name to show for the party.")] + _audit(),
           ref=f"Party pattern ({SILVERSTON_V1}; Hay, Data Model Patterns)"),
        _e("person", "party", "A human being. Identity attributes live here once, whatever "
           "roles the person plays.", person_attrs + _audit(), ref="Party pattern: Person"),
        _e("organization", "party", "A company, payer, facility or other legal entity.",
           [_a("party_id", "BIGINT", "The party this organization is.", "party.party.party_id",
               pk=True), _a("organization_name", "STRING", "Legal name."),
            _a("tax_id", "STRING", "Tax identifier.")] + _audit(),
           ref="Party pattern: Organization"),
        _e("party_role", "party", "A role a party plays (patient, guarantor, clinician, "
           "payer...), with the dates it applied.",
           [_a("party_role_id", "BIGINT", "Surrogate key.", pk=True),
            _a("party_id", "BIGINT", "The party playing the role.", "party.party.party_id"),
            _a("role_type", "STRING", "The role, from the role type classification."),
            _a("from_date", "DATE", "When the role started."),
            _a("thru_date", "DATE", "When the role ended.")] + _audit(),
           ref="Party Role pattern"),
        _e("party_relationship", "party", "A relationship between two party roles "
           "(guarantor for patient, emergency contact of, member of group).",
           [_a("party_relationship_id", "BIGINT", "Surrogate key.", pk=True),
            _a("from_party_role_id", "BIGINT", "Role on the from side.",
               "party.party_role.party_role_id"),
            _a("to_party_role_id", "BIGINT", "Role on the to side.",
               "party.party_role.party_role_id"),
            _a("relationship_type", "STRING", "Kind of relationship."),
            _a("from_date", "DATE", "When it started."),
            _a("thru_date", "DATE", "When it ended.")] + _audit(),
           ref="Party Relationship pattern"),
    ]


def contact_core() -> list[rm.RefEntity]:
    cm = "party.contact_mechanism.contact_mechanism_id"
    return [
        _e("contact_mechanism", "party", "A way to reach a party: a postal address, a phone "
           "number or an electronic address.",
           [_a("contact_mechanism_id", "BIGINT", "Surrogate key.", pk=True),
            _a("mechanism_type", "STRING", "postal | telecom | electronic")] + _audit(),
           ref="Contact Mechanism pattern"),
        _e("postal_address", "party", "A postal address.",
           [_a("contact_mechanism_id", "BIGINT", "The contact mechanism.", cm, pk=True),
            _a("address_line1"), _a("address_line2"), _a("city"), _a("state_province"),
            _a("postal_code"), _a("country_code")], ref="Contact Mechanism: Postal Address"),
        _e("telecom_number", "party", "A phone or fax number.",
           [_a("contact_mechanism_id", "BIGINT", "The contact mechanism.", cm, pk=True),
            _a("country_code"), _a("area_code"), _a("phone_number"), _a("extension")],
           ref="Contact Mechanism: Telecommunications Number"),
        _e("electronic_address", "party", "An e-mail address, portal handle or URL.",
           [_a("contact_mechanism_id", "BIGINT", "The contact mechanism.", cm, pk=True),
            _a("address_string", "STRING", "The address itself.")],
           ref="Contact Mechanism: Electronic Address"),
        _e("party_contact_mechanism", "party", "Which party uses which contact mechanism, "
           "for what purpose (home, billing, emergency), and when.",
           [_a("party_contact_mechanism_id", "BIGINT", "Surrogate key.", pk=True),
            _a("party_id", "BIGINT", "The party.", "party.party.party_id"),
            _a("contact_mechanism_id", "BIGINT", "The contact mechanism.", cm),
            _a("purpose_type", "STRING", "home | work | billing | emergency ..."),
            _a("from_date", "DATE", "When it started."),
            _a("thru_date", "DATE", "When it ended."),
            _a("non_solicitation_flag", "BOOLEAN", "Do not use for marketing.")],
           ref="Party Contact Mechanism pattern"),
    ]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w
            for w in re.findall(r"[a-z]+", (text or "").lower().replace("_", " "))
            if len(w) > 2 and w not in _STOP}


def select_entities(model: rm.RefModel, *, description: str = "",
                    domains: Sequence[str] = (), tables: Sequence[str] = ()) -> list[str]:
    """Entity keys to keep: named tables and domains, else the domains the
    description talks about, else everything."""
    keep: list[str] = []
    if tables or domains:
        import model_products_gen as mpg  # noqa: PLC0415
        found, _missing = mpg.resolve_tables(list(tables) + [f"domain:{d}" for d in domains],
                                             model)
        return found
    words = _words(description)
    if words:
        score: dict[str, float] = {}
        for e in model.entities:
            hit = len(words & _words(e.name)) * 2 + len(words & _words(e.description)) * 0.25
            score[e.domain] = score.get(e.domain, 0.0) + hit
        if score and max(score.values()) > 0:
            top = max(score.values())
            chosen = {d for d, s in score.items() if s >= top * 0.25}
            keep = [e.key for e in model.entities if e.domain in chosen]
    return keep or [e.key for e in model.entities]


# ---------------------------------------------------------------------------
# Rewrite
# ---------------------------------------------------------------------------

_ADDR_TARGET = [(re.compile(r"phone|mobile|fax"), "telecom_number"),
                (re.compile(r"email"), "electronic_address")]


def rewrite(model: rm.RefModel, keep: Sequence[str], *, name: str,
            refactor: Sequence[str] = REFACTORINGS,
            catalog: Optional[rm.PatternCatalog] = None) -> tuple[rm.RefModel, list[Change]]:
    catalog = catalog or rm.load_patterns()
    keepset = set(keep)
    out = rm.RefModel(source="generated", industry=model.industry, flavour="domain",
                      version="v1", name=name,
                      description=f"Pattern-first rewrite of {model.name or model.industry}.",
                      entities=[copy.deepcopy(e) for e in model.entities if e.key in keepset])
    out.metrics = [copy.deepcopy(m) for m in model.metrics if m.entity_key in keepset]
    changes: list[Change] = []
    index = out.entity_index()

    # Scope: FKs leaving the kept set.
    resolver = me.FkResolver(model)
    dropped = []
    for e in out.entities:
        for a in e.attributes:
            t = resolver.target(a) if a.fk_to else None
            if a.fk_to and (t is None or t[0] not in keepset):
                dropped.append(f"{e.key}.{a.name} -> {a.fk_to}")
                a.fk_to = None
    if dropped:
        changes.append(Change("scope", f"{len(dropped)} foreign keys to tables outside the model "
                              "were dropped", "; ".join(dropped[:12])
                              + ("…" if len(dropped) > 12 else "") + ". The columns are kept as "
                              "plain identifiers; add the referenced tables to restore them.",
                              sorted({d.split(" ")[0].rsplit(".", 1)[0] for d in dropped})))

    links = mr.pattern_links(None, out, None, catalog)
    added: list[rm.RefEntity] = []

    # 1. Party.
    if "party" in refactor:
        roles = [e for e in out.entities if len(rm.carries_person_identity(e)) >= 2]
        if roles:
            ident: list[rm.RefAttribute] = []
            for e in roles:
                cols = set(rm.carries_person_identity(e))
                ident += [a for a in e.attributes if a.name in cols]
                e.attributes = [a for a in e.attributes if a.name not in cols]
                pos = 1 if e.attributes and e.attributes[0].is_pk else 0
                e.attributes.insert(pos, _a("party_role_id", "BIGINT",
                                            f"The party role this {e.name} is.",
                                            "party.party_role.party_role_id"))
            added += party_core(ident)
            changes.append(Change(
                "party", f"{len(roles)} tables became roles of one Party",
                "Identity columns (" + ", ".join(sorted({a.name for a in ident})[:10])
                + ") moved to party.person; " + ", ".join(e.key for e in roles)
                + " now reference party.party_role. One person who is both patient and "
                "guarantor, or patient and clinician, exists once.",
                [e.key for e in roles], "party_role"))

    # 2. Contact mechanisms.
    if "contact_mechanism" in refactor:
        hits = []
        for e in out.entities:
            cols = [a.name for a in e.attributes if mr._ADDRESS.search(a.name)]
            if len(cols) >= 3:
                hits.append((e, cols))
        if len(hits) >= 1:
            for e, cols in hits:
                e.attributes = [a for a in e.attributes if a.name not in set(cols)]
            if not any(x.name == "party" for x in added):
                added += party_core([])
            added += contact_core()
            changes.append(Change(
                "contact_mechanism", f"Inline addresses, phones and e-mails removed from "
                f"{len(hits)} tables", "; ".join(f"{e.key} ({len(c)} columns)" for e, c in hits)
                + ". They are now rows of party.postal_address, party.telecom_number and "
                "party.electronic_address, linked through party.party_contact_mechanism with a "
                "purpose and dates.", [e.key for e, _ in hits], "contact_mechanism"))

    # 3. One owner per concept.
    if "single_owner" in refactor:
        for p in catalog.patterns:
            if p.review != "single_owner":
                continue
            members = [e for e in out.entities if links.get(e.key, {}).get(p.key, 0) >= 0.6]
            if len(members) < 2 or len({e.domain for e in members}) < 2:
                continue
            owner = max(members, key=lambda e: len(e.attributes))
            opk = owner.primary_key.split(",")[0]
            owner_cols = {a.name for a in owner.attributes}
            for e in members:
                if e is owner:
                    continue
                epk = set(e.primary_key.split(","))
                e.attributes = [a for a in e.attributes if a.name in epk or a.fk_to
                                or a.name not in owner_cols or a.name.endswith("_id")]
                if not any(a.name == opk for a in e.attributes):
                    pos = 1 if e.attributes and e.attributes[0].is_pk else 0
                    e.attributes.insert(pos, _a(opk, "BIGINT", f"The {owner.name} this refers to.",
                                                f"{owner.key}.{opk}"))
                else:
                    for a in e.attributes:
                        if a.name == opk and not a.is_pk:
                            a.fk_to = f"{owner.key}.{opk}"
            changes.append(Change(
                "single_owner", f"{owner.key} owns {p.name.lower()}",
                f"{', '.join(e.key for e in members if e is not owner)} kept only their own "
                f"columns and reference {owner.key}.{opk}.", [e.key for e in members], p.key))

    # 4. Status history.
    if "status_history" in refactor:
        names = {e.name for e in out.entities}
        made = []
        for e in list(out.entities):
            lk = links.get(e.key, {})
            if not any(lk.get(k, 0) >= 0.6 for k in mr._TRANSACTIONAL):
                continue
            status = next((a for a in e.attributes if a.name.endswith("status")), None)
            if status is None or f"{e.name}_status_history" in names or not e.primary_key:
                continue
            pk = e.primary_key.split(",")[0]
            pk_type = next((a.data_type for a in e.attributes if a.name == pk), "BIGINT")
            added.append(_e(f"{e.name}_status_history", e.domain,
                            f"Every {status.name} {e.name} has had, with when it applied.",
                            [_a(f"{e.name}_status_history_id", "BIGINT", "Surrogate key.", pk=True),
                             _a(pk, pk_type, f"The {e.name}.", f"{e.key}.{pk}"),
                             _a(status.name, status.data_type or "STRING", status.description),
                             _a("status_reason", "STRING", "Why the status changed."),
                             _a("status_from_datetime", "TIMESTAMP", "When it took effect."),
                             _a("status_thru_datetime", "TIMESTAMP", "When it was superseded."),
                             _a("entered_by", "STRING", "Who or what recorded the change.")],
                            ref="Status pattern (Silverston; Hay)"))
            made.append(e.key)
        if made:
            changes.append(Change("status_history", f"{len(made)} status history tables added",
                                  ", ".join(made), made, "status"))

    out.entities = [e for e in added if e.key not in index] + out.entities
    # de-duplicate (party core may be added once only)
    seen: set[str] = set()
    out.entities = [e for e in out.entities if not (e.key in seen or seen.add(e.key))]
    return out, changes


# ---------------------------------------------------------------------------
# model.json
# ---------------------------------------------------------------------------

def to_model_json(model: rm.RefModel, changes: Sequence[Change], *, base_label: str,
                  description: str) -> dict[str, Any]:
    domains: dict[str, list[dict[str, Any]]] = {}
    for e in model.entities:
        domains.setdefault(e.domain or "core", []).append({
            "name": e.display_name or e.name, "table_name": e.name,
            "primary_key": e.primary_key, "subdomain": e.subdomain,
            "description": e.description, "reference": e.standard_refs,
            "attributes": [{
                "name": a.name, "column_name": a.name, "type": a.data_type or "STRING",
                "description": a.description, "foreign_key_to": a.fk_to or "",
                "references": a.reference, "tags": a.tags} for a in e.attributes]})
    return {
        "agent_version": "mypub-kb-domain-model",
        "model": {
            "type": "business", "name": model.name, "version": model.version,
            "description": description or model.description,
            "model_conventions": {"source": base_label,
                                  "patterns": sorted({c.pattern for c in changes if c.pattern})},
            "domains": [{"name": d, "description": "", "products": ps}
                        for d, ps in domains.items()],
            "metric_views": [{"view_name": m.name, "description": m.description,
                              "owner_domain": (m.entity_key or ".").split(".")[0],
                              "owner_product": (m.entity_key or ".").split(".")[-1],
                              "dimensions_count": m.n_dimensions,
                              "measures_count": m.n_measures, "sql": m.definition}
                             for m in model.metrics],
        },
        "x_mypub_changes": [c.__dict__ for c in changes],
    }


def render_changes(changes: Sequence[Change], before: tuple[str, float],
                   after: tuple[str, float], n_before: int, n_after: int) -> str:
    lines = ["# Changes from the base model", "",
             f"Review grade **{before[0]} ({before[1]:.0%}) → {after[0]} ({after[1]:.0%})**; "
             f"{n_before} tables kept from the base model, {n_after} in the result.", ""]
    for c in changes:
        lines += [f"## {c.title}", "", c.detail, ""]
        if c.books:
            lines += ["Read: " + "; ".join(c.books), ""]
    if not changes:
        lines.append("No pattern changes were needed.")
    return "\n".join(lines).rstrip() + "\n"


REFINE_PROMPT = """# Refine the domain model

You are a data modeler. The business: {description}

`model.json` in this package is a pattern-first model built from {base}.
Read `changes.md` and `review.md` first, then:

1. List the business processes in the description that no table supports.
   For each, add the smallest set of tables that does, in model.json's
   shape (domain → products → attributes with name, type, description,
   foreign_key_to). Reuse party.party_role for every person or organization
   role; never copy name/birth date/address columns onto a new table.
2. Remove tables the description doesn't need.
3. Write every change as one line in `refinements.md` with the reason.
4. Re-run `/kb-model-review` on the result and fix any error it reports.

Don't invent regulatory references; leave `references` empty when unsure.
"""


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class DomainModelDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        industry = kw.get("industry") or ""
        spec = kw.get("base") or (f"databricks:{industry}" if industry else "")
        model, mid, error = mr.load_input(conn, spec) if spec else (None, None, "no base model")
        if model is not None and industry:
            model.industry = industry
        return {"description": kw.get("description") or query, "spec": spec, "model": model,
                "ref_model_id": mid, "error": error, "name": kw.get("name") or "",
                "domains": kw.get("domains") or [], "tables": kw.get("tables") or [],
                "refactor": kw.get("refactor") or list(REFACTORINGS),
                "catalog": kw.get("catalog")}


class DomainModelPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        base: Optional[rm.RefModel] = d["model"]
        name = d["name"] or f"{(base.name if base else 'Domain')} domain model"
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or slug,
                       domain="domain model", source_query=d["description"])
        if base is None:
            plan.package_metadata = {"ok": False, "error": d["error"] or "no base model"}
            return plan
        cat = rm.load_patterns()
        keep = select_entities(base, description=d["description"] if not d["domains"] else "",
                               domains=d["domains"], tables=d["tables"])
        before = rm.RefModel(source=base.source, industry=base.industry, flavour=base.flavour,
                             version=base.version, name=base.name,
                             entities=[e for e in base.entities if e.key in set(keep)])
        result, changes = rewrite(base, keep, name=name, refactor=d["refactor"], catalog=cat)
        section = {"party": "party_role", "contact_mechanism": "contact_mechanism",
                   "single_owner": "coverage", "status_history": "status"}
        for c in changes:
            if c.kind in section:
                c.books = mr._books_for(conn, cat, section[c.kind])
        g_before = mr.grade(mr.score(mr.run_checks(
            before, mr.pattern_links(conn, before, None, cat), cat)))
        findings = mr.run_checks(result, mr.pattern_links(conn, result, None, cat), cat)
        scores = mr.score(findings)
        g_after = mr.grade(scores)
        resolver = me.FkResolver(result)
        base_label = f"{base.source} {base.industry} {base.flavour} {base.version}".strip()
        stats = {"entities": len(result.entities),
                 "attributes": sum(len(e.attributes) for e in result.entities),
                 "relationships": len(result.relationships())}
        review = mr.render_review(result, g_after[0], g_after[1], scores, findings,
                                  dict(stats, linked_entities=0, link_source="heuristic"))
        files = {
            "model.json": json.dumps(to_model_json(result, changes, base_label=base_label,
                                                   description=d["description"]),
                                     indent=2) + "\n",
            "ddl/model.sql": me.uc_ddl(result.entities, resolver, catalog=d["catalog"],
                                       header=f"{name}: pattern-first rewrite of {base_label}."),
            "model.dbml": me.dbml(result.entities, resolver, project=re.sub(r"\W+", "_", name)),
            "changes.md": render_changes(changes, g_before, g_after, len(keep),
                                         len(result.entities)),
            "review.md": review,
            "vibes.txt": mr.render_vibes(findings) or "No changes suggested.\n",
            "_prompts/refine-model.md": REFINE_PROMPT.format(description=d["description"],
                                                             base=base_label),
            "README.md": (
                f"# {name}\n\n{d['description']}\n\nBuilt from **{base_label}** "
                f"({len(keep)} tables kept) and rewritten with the library's data model "
                f"patterns: {', '.join(sorted({c.kind for c in changes})) or 'none needed'}. "
                f"Review grade {g_before[0]} → {g_after[0]}.\n\n"
                "- `model.json`: the Databricks Vibe Data Modeling format (app, viewer, "
                "installer).\n- `ddl/model.sql`: Unity Catalog DDL, parents first.\n"
                "- `model.dbml`: for dbdiagram.io and other DBML tools.\n"
                "- `changes.md`: every change and the books behind it.\n"
                "- `review.md`, `vibes.txt`: `/kb-model-review` of the result.\n"
                "- `_prompts/refine-model.md`: subagent prompt for tables the description "
                "needs that the base model lacks.\n"
                + ("\nThe base model is Databricks gallery content (Databricks License): keep "
                   "this package out of public repositories.\n" if base.source == "databricks"
                   else "")),
            "_provenance.md": (f"# Provenance: {name}\n\nBase: `{d['spec']}` ({base_label}). "
                               f"Selection: {len(keep)} tables. Refactorings: "
                               f"{', '.join(d['refactor'])}. Patterns: "
                               "data/ref_models/patterns.yaml. Code: domain_model_gen.py.\n"),
        }
        # Round-trip: the model.json must read back with the same shape.
        check = rm.RefModel(source="x", industry="x", flavour="x", version="x")
        try:
            import tempfile  # noqa: PLC0415
            from pathlib import Path  # noqa: PLC0415
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "model.json"
                p.write_text(files["model.json"])
                check = rm.parse_databricks_model(p, industry=base.industry, flavour="domain")
        except Exception:  # noqa: BLE001
            pass
        plan.package_metadata = {
            "ok": True, "base": base_label, "kept": len(keep), "stats": stats,
            "grade_before": g_before[0], "grade_after": g_after[0],
            "score_before": round(g_before[1], 3), "score_after": round(g_after[1], 3),
            "changes": [c.title for c in changes],
            "roundtrip_ok": len(check.entities) == len(result.entities)}
        plan.units.append(GenUnit(unit_type="domain_model", name=name,
                                  content_markdown=files["changes.md"],
                                  metadata={"grade": g_after[0]}))
        plan.files = [GenFile(filename=k, content=v, purpose=k.split("/")[0].split(".")[0])
                      for k, v in files.items()]
        return plan


class DomainModelValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        if not meta.get("ok"):
            return [ValidationIssue("", "error", f"no base model: {meta.get('error')}")]
        out = []
        if not meta.get("roundtrip_ok"):
            out.append(ValidationIssue("", "error", "model.json doesn't read back"))
        if meta["score_after"] < meta["score_before"]:
            out.append(ValidationIssue("", "warning", "the rewrite scored lower than the base"))
        return out


def make_domain_model_generator() -> Generator:
    import kb_scaffold as ks  # noqa: PLC0415
    return Generator(generator_type=GENERATOR_TYPE, decomposer=DomainModelDecomposer(),
                     planner=DomainModelPlanner(), ranking_mode="generation",
                     validator=DomainModelValidator(), materializer=ks.TreeMaterializer())
