"""model_products_gen.py — ``/kb-dp-from-model``: data products cut from a data model (2026-11).

Bridges an industry data model (a Databricks gallery ``model.json``, DDL,
or a reference model in the catalog) to Open Data Products. Each product is
a named slice of the model's tables:

    products = ["Claims and denials: claim lifecycle to payment => claim.claim, claim.denial",
                ...]

For each product the package holds:

* ``products/<p>.odps.yaml``: ODPS 4.2, with ``productKPIs`` taken from the
  model's metric views over the product's tables (name, comment and the
  measure's SQL as the calculation);
* ``contracts/<p>.odcs.yaml``: an ODCS v3.1.0 contract with one schema
  object per table, columns, types, keys, descriptions and the quality
  rules the keys imply (not the portfolio generator's placeholder);
* ``ddl/<p>.sql`` (Unity Catalog DDL for the slice) and ``metrics/<p>.sql``
  (the metric views, when the model has them);
* ``model-notes.md``: ``/kb-model-review`` run on the slice, so pattern
  problems (identity copied onto roles, coverage held twice) are visible
  before the product ships;

plus, from ``/kb-dp-portfolio``: ODPC fragments and catalog, the ODPG value
graph, policy, recipe and Makefile; and ``sample-data/healthsim-request.md``,
a request for synthetic rows in dependency order (HealthSim for healthcare
models, any generator otherwise).

With no ``products`` and a healthcare model, an example portfolio of three
provider/payer products is used (Encounter activity, Claims and denials,
Patient coverage).

The output carries the source model's content: for the Databricks gallery
(Databricks License) keep it in the local package folder, not a public repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import duckdb
import yaml

import model_emit as me
import model_review_gen as mr
import odp_gen as og
import odp_portfolio_gen as opg
import ref_models as rm
from generator import GenFile, GenPlan, Generator, ValidationIssue

GENERATOR_TYPE = "dp_from_model"

HEALTHCARE_EXAMPLE = {
    "products": [
        "Encounter activity: inpatient and outpatient visits with their diagnoses, procedures "
        "and DRG assignment, one row per visit event => encounter.visit, "
        "encounter.visit_diagnosis, encounter.visit_procedure, encounter.drg_assignment, "
        "encounter.adt_event",
        "Claims and denials: the claim lifecycle from submission to remittance, with denials "
        "and appeals => claim.claim, claim.line, claim.submission, claim.denial, claim.appeal, "
        "claim.remittance",
        "Patient coverage: who each patient is and which insurance covers them, as one "
        "coverage record per patient and payer => patient.mpi_record, patient.demographics, "
        "patient.insurance_coverage, patient.pcp_attribution",
    ],
    "use_cases": [
        "Denial prevention: find the coverage and coding causes of denials before claims "
        "go out => Claims and denials, Patient coverage",
        "Length of stay management: compare length of stay and DRG mix across units and "
        "clinicians => Encounter activity",
    ],
    "objectives": [
        "Reduce initial denial rate: fewer claims denied on first submission => Denial prevention",
        "Improve inpatient throughput: shorter avoidable length of stay => "
        "Length of stay management",
    ],
}


@dataclass
class ProductSlice:
    item: opg.Item
    tables: list[str] = field(default_factory=list)      # entity keys
    missing: list[str] = field(default_factory=list)     # names that matched nothing


def resolve_tables(names: Sequence[str], model: rm.RefModel) -> tuple[list[str], list[str]]:
    """Match ``domain.table``, ``table`` or ``domain:<name>`` (whole domain) to entity keys."""
    index = model.entity_index()
    by_name: dict[str, list[str]] = {}
    for e in model.entities:
        by_name.setdefault(e.name, []).append(e.key)
    found, missing = [], []
    for raw in names:
        n = raw.strip().strip("`").lower()
        if n.startswith("domain:"):
            keys = [e.key for e in model.entities if e.domain == n[7:]]
        elif n in index:
            keys = [n]
        else:
            keys = by_name.get(n.split(".")[-1], [])[:1]
        if keys:
            found += [k for k in keys if k not in found]
        else:
            missing.append(raw)
    return found, missing


def _columns(e: rm.RefEntity) -> list[og.Column]:
    pk = set(e.primary_key.split(",")) | {a.name for a in e.attributes if a.is_pk}
    return [og.Column(a.name, (a.data_type or "string").lower(), required=a.name in pk,
                      primary_key=a.name in pk, description=a.description) for a in e.attributes]


def contract_doc(p: opg.Item, entities: Sequence[rm.RefEntity], *, platform: str,
                 catalog: Optional[str], source_label: str) -> dict[str, Any]:
    first = entities[0]
    loc = ".".join(x for x in (catalog, first.domain, first.name) if x)
    doc = og.odcs_contract(contract_id=f"{p.slug}-contract", name=p.name, table=first.name,
                           columns=_columns(first), purpose=p.description or p.name,
                           platform=platform, location=loc, domain=first.domain or None)
    objs = []
    for e in entities:
        obj = og.odcs_contract(contract_id="x", name="x", table=e.name, columns=_columns(e),
                               purpose="", platform=platform)["schema"][0]
        obj["description"] = e.description or f"TODO: what one row of {e.name} represents."
        if e.domain:
            obj["physicalName"] = ".".join(x for x in (catalog, e.domain, e.name) if x)
        objs.append(obj)
    doc["schema"] = objs
    doc["description"]["usage"] = (f"Tables cut from {source_label}: "
                                   + ", ".join(e.key for e in entities) + ".")
    return doc


_UNIT = [(re.compile(r"(rate|pct|percent|ratio)"), "percentage"),
         (re.compile(r"(amount|revenue|cost|charge|paid|billed|allowed|balance)"), "currency"),
         (re.compile(r"(days|los|length_of_stay)"), "days"),
         (re.compile(r"(hours|minutes|time|duration|turnaround|tat)"), "hours")]


def metric_measures(metric: rm.RefMetric) -> list[dict[str, str]]:
    body = metric.definition
    if "$$" in body:
        body = body.split("$$")[1]
    try:
        doc = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return []
    return [m for m in (doc.get("measures") or []) if isinstance(m, dict) and m.get("name")]


def product_kpis(p: opg.Item, metrics: Sequence[rm.RefMetric], limit: int = 6) -> list[dict]:
    """ODPS productKPIs from the measures of the slice's metric views: ratio and
    average measures first (they make better KPIs than raw counts)."""
    rows = []
    for m in metrics:
        for meas in metric_measures(m):
            name = str(meas["name"])
            rank = 0 if re.search(r"rate|pct|percent|ratio|avg|average|days", name) else 1
            rows.append((rank, m, meas))
    out = []
    for _r, m, meas in sorted(rows, key=lambda r: r[0])[:limit]:
        name = str(meas["name"])
        unit = next((u for rx, u in _UNIT if rx.search(name)), "count")
        out.append({
            "id": f"{p.slug}-{og.slug(name)}", "name": og.title(name.replace("_", " ")),
            "description": str(meas.get("comment") or f"{name} over {m.entity_key}."),
            "unit": unit, "target": "TODO",
            "direction": "decrease" if re.search(r"denial|error|delay|cancel|readmit|los|days",
                                                 name) else "increase",
            "timeframe": "12 months", "frequency": "monthly", "owner": "Data product owner",
            "calculation": f"{meas.get('expr', '')} over {m.entity_key} "
                           f"(metric view {m.name})"})
    return out


def patch_odps(text: str, p: opg.Item, kpis: Sequence[dict], tables: Sequence[str]) -> str:
    head = "\n".join(ln for ln in text.splitlines() if ln.startswith("#"))
    doc = yaml.safe_load(text)
    det = doc["product"]["details"]["en"]
    det["tags"] = sorted({p.slug, *{t.split(".")[0] for t in tables}})
    if kpis:
        doc["product"]["productStrategy"]["productKPIs"] = list(kpis) + \
            doc["product"]["productStrategy"].get("productKPIs", [])
    return og.dump_yaml(doc, head.lstrip("# ").replace("\n# ", "\n"))


def healthsim_request(portfolio: str, slices: Sequence[ProductSlice], model: rm.RefModel,
                      resolver: me.FkResolver, rows: int) -> str:
    keys = list(dict.fromkeys(k for s in slices for k in s.tables))
    order = me.topo_order([resolver[k] for k in keys], resolver)
    lines = [f"# Sample data request: {portfolio}", "",
             f"Synthetic rows for the {len(order)} tables behind the portfolio, generated in "
             "dependency order so every foreign key points at a row that exists. No real "
             "patient data: everything here is synthetic.", ""]
    if model.industry in mr.OMOP_INDUSTRIES or model.industry == "healthcare":
        lines += ["Use the **healthsim** skill: PatientSim for patients, encounters, diagnoses "
                  "and procedures; MemberSim for coverage, claims, remittances, denials and "
                  "appeals. Generate one coherent population (the same patients across "
                  "encounters, coverage and claims), then map HealthSim's output to the columns "
                  "below and write one CSV per table to `sample-data/<schema>.<table>.csv`.", ""]
    else:
        lines += ["Generate the rows with any synthetic-data tool, one CSV per table in "
                  "`sample-data/<schema>.<table>.csv`.", ""]
    lines += [f"Target about {rows} rows for the root tables and 1–5 child rows per parent. "
              "Code columns (ICD-10, CPT, DRG, CARC denial reasons, NPI) should hold valid-looking "
              "codes, not free text.", "", "| order | table | rows | parents | columns |",
              "|---|---|---|---|---|"]
    keyset = set(keys)
    for i, e in enumerate(order, start=1):
        parents = sorted({t[0] for a in e.attributes if (t := resolver.target(a)) and
                          t[0] in keyset and t[0] != e.key})
        cols = ", ".join(a.name for a in e.attributes[:14]) + \
            (f" … (+{len(e.attributes) - 14})" if len(e.attributes) > 14 else "")
        lines.append(f"| {i} | {e.key} | {'~' + str(rows) if not parents else 'per parent'} | "
                     f"{', '.join(parents) or '—'} | {cols} |")
    lines += ["", "Load: run `ddl/*.sql` first, then `COPY INTO` (Databricks) or `read_csv` "
              "(DuckDB) each CSV in the order above.", ""]
    return "\n".join(lines)


def model_notes(model: rm.RefModel, slices: Sequence[ProductSlice],
                links: dict[str, dict[str, float]], catalog: rm.PatternCatalog) -> str:
    """Model-wide findings that touch each product's tables, plus the slice's
    own provenance count. Pattern problems such as identity copied onto
    roles only show up against the whole model, so the full review runs once."""
    full = [f for f in mr.run_checks(model, links, catalog)
            if f.severity != "advice" and f.section not in ("provenance", "structure")]
    out = ["# Model notes", "",
           "`/kb-model-review` findings that touch each product's tables. Fix errors before "
           "the product ships; the vibes can go straight to the modeling agent.", ""]
    for s in slices:
        keys = set(s.tables)
        sub = rm.RefModel(source=model.source, industry=model.industry, flavour=model.flavour,
                          version=model.version, name=s.item.name,
                          entities=[e for e in model.entities if e.key in keys])
        fs = [f for f in full if keys & set(f.entities)] + mr.check_provenance(sub)
        out += [f"## {s.item.name}", ""]
        if not fs:
            out += ["No errors or warnings on these tables.", ""]
        for f in fs:
            mine = sorted(keys & set(f.entities))
            where = f" (here: {', '.join(mine)})" if mine else ""
            out += [f"- **{f.severity}: {f.title}**{where}. {f.detail}", f"  - Vibe: {f.vibe}"]
        out.append("")
    return "\n".join(out)


def _strip_title(text: str, name: str) -> str:
    lines = text.splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("# ")
                     or lines[0].strip() == name):
        lines.pop(0)
    return "\n".join(lines) + "\n"


class DpFromModelDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        spec = kw.get("model") or query
        model, mid, error = mr.load_input(conn, spec)
        if model is not None and kw.get("industry"):
            model.industry = kw["industry"]
        products, use_cases, objectives = (kw.get("products") or [], kw.get("use_cases") or [],
                                           kw.get("objectives") or [])
        example = False
        if not products and model is not None and model.industry == "healthcare":
            products, use_cases, objectives = (HEALTHCARE_EXAMPLE["products"],
                                               use_cases or HEALTHCARE_EXAMPLE["use_cases"],
                                               objectives or HEALTHCARE_EXAMPLE["objectives"])
            example = True
        name = kw.get("portfolio") or (f"{(model.name if model else '') or 'Model'} data products")
        port = opg.PortfolioDecomposer().decompose(
            conn, resolver, name, products=[p.split("=>")[0] for p in products],
            use_cases=use_cases, objectives=objectives, platform=kw.get("platform") or "databricks")
        return {"spec": spec, "model": model, "ref_model_id": mid, "error": error,
                "products_raw": products, "portfolio": port, "example": example,
                "catalog": kw.get("catalog"), "rows": int(kw.get("sample_rows") or 500)}


class DpFromModelPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **kw: Any) -> GenPlan:
        model: Optional[rm.RefModel] = d["model"]
        port = d["portfolio"]
        if model is None or not d["products_raw"]:
            plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or "dp-from-model",
                           domain="data products from a model", source_query=d["spec"])
            plan.package_metadata = {"n_products": 0, "error": d["error"] or
                                     "give products (\"Name: description => table, table\")"}
            return plan
        resolver = me.FkResolver(model)
        slices = []
        for raw in d["products_raw"]:
            item = opg.parse_items([raw])[0]
            tables, missing = resolve_tables(item.links, model)
            item.links = []
            slices.append(ProductSlice(item, tables, missing))
        port["products"] = [s.item for s in slices]
        plan = opg.PortfolioPlanner().plan(conn, port, package_name=package_name or
                                           og.slug(port["name"]))
        plan.generator_type = GENERATOR_TYPE
        plan.domain = "data products from a model"
        plan.source_query = d["spec"]
        files = {f.filename: f.content for f in plan.files}

        metrics_by_entity: dict[str, list[rm.RefMetric]] = {}
        for m in model.metrics:
            if m.entity_key:
                metrics_by_entity.setdefault(m.entity_key, []).append(m)
        label = f"{model.source} {model.industry} {model.flavour} {model.version}".strip()
        platform = port["platform"]
        empty = [s.item.name for s in slices if not s.tables]
        checks: list[og.CheckResult] = []
        for s in slices:
            if not s.tables:
                continue
            p, ents = s.item, [resolver[k] for k in s.tables]
            cdoc = contract_doc(p, ents, platform=platform, catalog=d["catalog"],
                                source_label=label)
            files[f"contracts/{p.slug}.odcs.yaml"] = og.dump_yaml(
                cdoc, f"ODCS v3.1.0 contract for {p.name}: {len(ents)} tables from {label}.\n"
                      f"{og.MARKER}: SLA values, team and support channel.")
            checks.append(og.check_odcs(cdoc, f"contracts/{p.slug}.odcs.yaml"))
            mets = [m for k in s.tables for m in metrics_by_entity.get(k, [])]
            kpis = product_kpis(p, mets)
            files[f"products/{p.slug}.odps.yaml"] = patch_odps(
                files[f"products/{p.slug}.odps.yaml"], p, kpis, s.tables)
            files[f"ddl/{p.slug}.sql"] = me.uc_ddl(
                ents, resolver, catalog=d["catalog"],
                header=f"{p.name}: {len(ents)} tables from {label}.")
            if mets:
                files[f"metrics/{p.slug}.sql"] = "\n\n".join(m.definition.strip() for m in mets) + "\n"
        cat = rm.load_patterns()
        links = mr.pattern_links(conn, model, d["ref_model_id"], cat)
        files["model-notes.md"] = model_notes(model, [s for s in slices if s.tables], links, cat)
        files["sample-data/healthsim-request.md"] = healthsim_request(
            port["name"], [s for s in slices if s.tables], model, resolver, d["rows"])

        # Re-run the SDK on the patched products and the graph.
        cmds = [(f"products/{s.item.slug}.odps.yaml", ["validate",
                                                       f"products/{s.item.slug}.odps.yaml"])
                for s in slices if s.tables]
        checks += og.sdk_check(files, cmds)
        prev = files.get("_validation.md", "")
        files["_validation.md"] = og.render_checks(
            f"Product checks after slicing: {port['name']}", checks) + "\n" + prev
        table_lines = "".join(
            f"| {s.item.name} | {len(s.tables)} | {', '.join(s.tables)} |\n" for s in slices)
        files["README.md"] = (
            f"# {port['name']}\n\nData products cut from **{label}**"
            + (" (built-in example portfolio)" if d["example"] else "") + ".\n\n"
            "| product | tables | from the model |\n|---|---|---|\n" + table_lines + "\n"
            "Each product has a real ODCS contract (`contracts/`), Unity Catalog DDL "
            "(`ddl/`), KPIs from the model's metric views (`products/`, `metrics/`), and "
            "pattern notes from `/kb-model-review` (`model-notes.md`). "
            "`sample-data/healthsim-request.md` asks for synthetic rows to load.\n\n"
            "This package contains the source model's content; if it came from the "
            "Databricks gallery (Databricks License), keep it out of public repos.\n\n"
            + "## Portfolio\n\n" + _strip_title(files.get("README.md", ""), port["name"]))
        plan.files = [GenFile(filename=k, content=v, purpose=k.split("/")[0].split(".")[0])
                      for k, v in files.items()]
        meta = plan.package_metadata
        meta.update({"model": model.name, "source": label, "example": d["example"],
                     "tables": {s.item.slug: s.tables for s in slices},
                     "unmatched_tables": {s.item.slug: s.missing for s in slices if s.missing},
                     "empty_products": empty,
                     "check_errors": meta.get("check_errors", []) + og.check_errors(checks)})
        return plan


class DpFromModelValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        if not meta.get("n_products"):
            return [ValidationIssue("", "error", meta.get("error") or "no products")]
        out = [ValidationIssue("", "error", m) for m in meta.get("check_errors", [])]
        for name in meta.get("empty_products", []):
            out.append(ValidationIssue("", "error", f"no tables matched for product {name}"))
        for slug, miss in meta.get("unmatched_tables", {}).items():
            out.append(ValidationIssue("", "warning", f"{slug}: no table named {', '.join(miss)}"))
        return out


def make_dp_from_model_generator() -> Generator:
    import kb_scaffold as ks  # noqa: PLC0415
    return Generator(generator_type=GENERATOR_TYPE, decomposer=DpFromModelDecomposer(),
                     planner=DpFromModelPlanner(), ranking_mode="generation",
                     validator=DpFromModelValidator(), materializer=ks.TreeMaterializer())
