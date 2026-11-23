"""ui_inputs.py — input adapters for ``/kb-ui`` (2026-11).

Each adapter turns one kind of input into UI-spec entities and metrics:

* ``entities_from_ddl``       one or more ``CREATE TABLE`` statements
* ``entities_from_contract``  an ODCS contract (YAML text)
* ``model_from_json``         a Databricks industry ``model.json`` (entities + metric views)
* ``load_product``            an ODPS product file or a ``/kb-dp-from-model`` package:
                              product KPIs, its ODCS contract and its metric views

Pure parsing; no catalog access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

import odp_gen as og
import ui_spec as us

_HEALTH_WORDS = re.compile(r"\b(claim|claims|patient|encounter|diagnosis|payer|icd|drg|cpt|"
                           r"clinical|provider|member|coverage|denial|remittance|admission|"
                           r"discharge|medication|hospital|ehr|fhir|hl7)\b", re.IGNORECASE)


def looks_healthcare(*texts: str) -> bool:
    return sum(len(_HEALTH_WORDS.findall(t or "")) for t in texts) >= 2


# ---------------------------------------------------------------------------
# Fields and entities
# ---------------------------------------------------------------------------

# Beyond odp_gen's personal-data names: identifiers and accounts that point at a
# person or their money. Shown masked, never in list columns or sample data.
_SENSITIVE = re.compile(r"(medical_record|(^|_)mrn($|_)|account_number|bank_|routing_number|"
                        r"(^|_)ssn($|_)|social_security|member_id|subscriber_id|policy_number|"
                        r"birth|(^|_)dob($|_)|patient_name|guarantor_name|email|phone|address|"
                        r"(^|_)zip|postal|driver_license|passport|tax_id|(^|_)ein($|_)|"
                        r"responsibility_amount)", re.IGNORECASE)


def sensitive(name: str) -> bool:
    return bool(_SENSITIVE.search(name or "")) or og.Column(name).personal


def make_field(name: str, physical: str = "", *, logical: Optional[str] = None,
               primary_key: bool = False, required: bool = False,
               restricted: Optional[bool] = None, description: str = "",
               references: Optional[str] = None) -> us.Field:
    lt = logical or og.logical_type(physical or "string")
    kind = us.field_kind(name, lt, primary_key=primary_key)
    if references and kind not in ("id",):
        kind = "fk"
    personal = sensitive(name) if restricted is None else (restricted or sensitive(name))
    values = us.enum_values_from_text(description) if kind in ("enum", "text") else []
    if values and kind == "text" and len(values) <= 12:
        kind = "enum"
    return us.Field(name=name, label=us.label(name), kind=kind, physical_type=physical or lt,
                    required=required or primary_key, restricted=bool(personal),
                    enum_values=values if kind == "enum" else [],
                    description=" ".join((description or "").split())[:400],
                    references=references)


def make_entity(table: str, fields: Sequence[us.Field], *, description: str = "",
                physical: str = "") -> us.Entity:
    name = og.snake(table.split(".")[-1])
    single = us.singular(name)
    return us.Entity(name=name, label=us.label(single), plural=us.label(us.plural(single)),
                     table=physical or table, description=" ".join(description.split())[:600],
                     fields=list(fields))


_CREATE = re.compile(r"create\s+(?:or\s+replace\s+)?(?:temporary\s+|temp\s+)?table\s",
                     re.IGNORECASE)
_REF = re.compile(r"^\s*[`\"\[]?(\w+)[`\"\]]?\s.*?\breferences\s+([`\"\[\]\w.]+)",
                  re.IGNORECASE)
_FK_CONSTRAINT = re.compile(r"foreign\s+key\s*\(\s*[`\"\[]?(\w+)[`\"\]]?\s*\)\s*references\s+"
                            r"([`\"\[\]\w.]+)", re.IGNORECASE)
_TABLE_COMMENT = re.compile(r"\)\s*(?:using\s+\w+\s*)?comment\s+'([^']*)'", re.IGNORECASE)


def split_ddl(ddl: str) -> list[str]:
    starts = [m.start() for m in _CREATE.finditer(ddl or "")]
    return [ddl[a:b] for a, b in zip(starts, starts[1:] + [len(ddl)])]


def entities_from_ddl(ddl: str) -> list[us.Entity]:
    out: list[us.Entity] = []
    for stmt in split_ddl(ddl):
        table, cols = og.parse_ddl(stmt)
        if not table or not cols:
            continue
        refs: dict[str, str] = {}
        for line in stmt.splitlines():
            m = _REF.match(line)
            if m and not line.strip().lower().startswith(("foreign", "constraint")):
                refs[m.group(1).lower()] = re.sub(r"[`\"\[\]]", "", m.group(2)).split(".")[-1]
        for m in _FK_CONSTRAINT.finditer(stmt):
            refs[m.group(1).lower()] = re.sub(r"[`\"\[\]]", "", m.group(2)).split(".")[-1]
        tc = _TABLE_COMMENT.search(stmt)
        fields = [make_field(c.name, c.physical_type, primary_key=c.primary_key,
                             required=c.required, description=c.description,
                             references=refs.get(c.name.lower())) for c in cols]
        out.append(make_entity(table, fields, description=tc.group(1) if tc else "",
                               physical=table))
    return out


def entities_from_contract(text: str) -> tuple[list[us.Entity], dict[str, Any]]:
    """(entities, contract header) from an ODCS v3 contract."""
    doc = yaml.safe_load(text) or {}
    out: list[us.Entity] = []
    for obj in doc.get("schema") or []:
        if not isinstance(obj, dict):
            continue
        fields = []
        for p in obj.get("properties") or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            desc = p.get("description") or ""
            ref = None
            m = re.search(r"(?:foreign key|reference) (?:linking )?to ([\w.]+)", desc, re.IGNORECASE)
            if m:
                ref = m.group(1).rstrip(".").split(".")[-1]
            fields.append(make_field(
                p["name"], p.get("physicalType") or "", logical=p.get("logicalType"),
                primary_key=bool(p.get("primaryKey")), required=bool(p.get("required")),
                restricted=(p.get("classification") or "").lower() in ("restricted", "confidential",
                                                                       "pii", "phi"),
                description="" if desc.startswith("TODO") else desc, references=ref))
        out.append(make_entity(obj.get("name") or "table", fields,
                               description=obj.get("description") or "",
                               physical=obj.get("physicalName") or obj.get("name") or ""))
    return out, {k: doc.get(k) for k in ("id", "name", "domain", "description")}


# ---------------------------------------------------------------------------
# Metric views (Databricks WITH METRICS LANGUAGE YAML)
# ---------------------------------------------------------------------------

_VIEW = re.compile(r"create\s+(?:or\s+replace\s+)?view\s+([`\w.]+)\s+with\s+metrics.*?\$\$(.*?)\$\$",
                   re.IGNORECASE | re.DOTALL)


def _dim(d: dict) -> us.Dimension:
    name = str(d.get("name") or "")
    expr = str(d.get("expr") or "")
    comment = str(d.get("comment") or "")
    return us.Dimension(name=name, label=us.label(name), expr=expr,
                        time_grain=us.time_grain(name, expr),
                        values=us.enum_values_from_text(comment))


def metrics_from_view_yaml(view: str, body: str) -> list[us.Metric]:
    try:
        doc = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return []
    source = str(doc.get("source") or "")
    entity = og.snake(re.sub(r"[`\"]", "", source).split(".")[-1]) if source else None
    dims = [_dim(d) for d in doc.get("dimensions") or [] if isinstance(d, dict) and d.get("name")]
    time_dims = [d for d in dims if d.time_grain]
    cat_dims = [d for d in dims if not d.time_grain]
    out = []
    for m in doc.get("measures") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m["name"])
        expr = str(m.get("expr") or "")
        out.append(us.Metric(name=name, label=us.label(name), expr=expr,
                             format=us.metric_format(name, expr),
                             description=" ".join(str(m.get("comment") or "").split()),
                             view=view, entity=entity, time_dims=time_dims,
                             category_dims=cat_dims))
    return out


def metrics_from_sql(sql: str) -> list[us.Metric]:
    out: list[us.Metric] = []
    for m in _VIEW.finditer(sql or ""):
        view = re.sub(r"[`\"]", "", m.group(1)).split(".")[-1]
        out.extend(metrics_from_view_yaml(view, m.group(2)))
    return out


# ---------------------------------------------------------------------------
# Databricks model.json
# ---------------------------------------------------------------------------

def model_from_json(path: Path, tables: Sequence[str] = ()) -> tuple[list[us.Entity], list[us.Metric], str]:
    import ref_models as rm  # pylint: disable=import-outside-toplevel
    model = rm.parse_databricks_model(Path(path))
    want = {t.lower().split(".")[-1] for t in tables}
    ents = []
    for e in model.entities:
        short = e.name.split(".")[-1]
        if want and short.lower() not in want and e.name.lower() not in want:
            continue
        fields = [make_field(a.name, a.data_type, primary_key=a.is_pk, required=a.is_pk,
                             description=a.description,
                             references=(a.fk_to.split(".")[-1] if a.fk_to else None))
                  for a in e.attributes]
        ents.append(make_entity(short, fields, description=e.description, physical=e.name))
    keep = {e.name for e in ents}
    mets: list[us.Metric] = []
    for m in model.metrics:
        for met in metrics_from_sql(m.definition) or []:
            if not keep or (met.entity in keep):
                mets.append(met)
    return ents, mets, model.description or model.name


# ---------------------------------------------------------------------------
# ODPS products
# ---------------------------------------------------------------------------

@dataclass
class ProductInput:
    product_id: str
    name: str
    description: str
    domain: str
    kpis: list[dict[str, Any]] = field(default_factory=list)
    entities: list[us.Entity] = field(default_factory=list)
    metrics: list[us.Metric] = field(default_factory=list)
    files: list[str] = field(default_factory=list)       # what was read
    notes: list[str] = field(default_factory=list)


def _find_product_file(path: Path, product: Optional[str]) -> Optional[Path]:
    if path.is_file():
        return path
    cands = sorted(path.glob("products/*.odps.yaml")) + sorted(path.glob("*.odps.yaml"))
    if product:
        want = us.slug(product)
        cands = [c for c in cands if us.slug(c.name.split(".")[0]) == want] or cands
    return cands[0] if cands else None


def load_product(path: str | Path, product: Optional[str] = None) -> ProductInput:
    p = Path(path).expanduser()
    pf = _find_product_file(p, product)
    if pf is None:
        raise FileNotFoundError(f"no ODPS product (*.odps.yaml) at {p}")
    doc = yaml.safe_load(pf.read_text()) or {}
    prod = doc.get("product") or {}
    det = ((prod.get("details") or {}).get("en") or {})
    pid = det.get("productID") or pf.name.split(".")[0]
    tags = det.get("tags") or []
    out = ProductInput(product_id=str(pid), name=str(det.get("name") or us.label(pid)),
                       description=str(det.get("description") or det.get("valueProposition") or ""),
                       domain=str(tags[0] if tags else ""), files=[str(pf)])
    strat = prod.get("productStrategy") or {}
    out.kpis = [k for k in strat.get("productKPIs") or [] if isinstance(k, dict)]
    root = pf.parent.parent if pf.parent.name == "products" else pf.parent

    # Contract: the product's contractURL, else contracts/<id>.odcs.yaml.
    curl = (((prod.get("contract") or {}).get("default") or {}).get("contractURL") or "")
    cands = []
    if curl and not re.match(r"^\w+://", curl):
        cands.append((pf.parent / curl).resolve())
    cands += [root / "contracts" / f"{pid}.odcs.yaml", root / f"{pid}.odcs.yaml"]
    cf = next((c for c in cands if c.is_file()), None)
    if cf:
        out.entities, header = entities_from_contract(cf.read_text())
        out.domain = out.domain or str(header.get("domain") or "")
        out.files.append(str(cf))
    else:
        out.notes.append("no ODCS contract found next to the product; no tables")

    mf = root / "metrics" / f"{pid}.sql"
    if mf.is_file():
        out.metrics = metrics_from_sql(mf.read_text())
        out.files.append(str(mf))
    else:
        out.notes.append("no metric views (metrics/<product>.sql); KPI cards only")
    return out


# ---------------------------------------------------------------------------
# KPIs → metrics
# ---------------------------------------------------------------------------

_NUM_DEN = re.compile(r"\s+(numerator|denominator)$", re.IGNORECASE)


def _calc_expr(calc: str) -> str:
    return re.split(r"\s+over\s+", calc or "", maxsplit=1)[0].strip()


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", (expr or "").lower())


def kpi_metrics(kpis: Sequence[dict], measures: Sequence[us.Metric], *, limit: int = 4
                ) -> list[us.Metric]:
    """Headline KPI metrics: numerator/denominator pairs become one ratio; each KPI is
    matched to the metric-view measure with the same expression. KPIs about the
    product itself (adoption) are left out of the business dashboard."""
    by_expr = {_norm(m.expr): m for m in measures}
    by_name = {m.name: m for m in measures}
    singles: list[tuple[str, dict]] = []
    pairs: dict[str, dict[str, Any]] = {}
    for k in kpis:
        name = str(k.get("name") or "")
        if re.search(r"consum|adopt|active consuming", name, re.IGNORECASE):
            continue
        m = _NUM_DEN.search(name)
        if m:
            stem = _NUM_DEN.sub("", name)
            pairs.setdefault(stem, {})[m.group(1).lower()] = k
        else:
            singles.append((name, k))
    out: list[us.Metric] = []

    def measure_for(k: dict) -> Optional[us.Metric]:
        mm = by_expr.get(_norm(_calc_expr(str(k.get("calculation") or ""))))
        if mm:
            return mm
        kid = og.snake(str(k.get("name") or ""))
        return by_name.get(kid)

    for stem, pk in pairs.items():
        num, den = pk.get("numerator"), pk.get("denominator")
        if not (num and den):
            continue
        mn, md = measure_for(num), measure_for(den)
        name = og.snake(stem)
        out.append(us.Metric(
            name=name, label=us.label(name), format="percent",
            description=str(num.get("description") or "").split(" - ")[0],
            view=mn.view if mn else "", entity=mn.entity if mn else None,
            time_dims=mn.time_dims if mn else [], category_dims=mn.category_dims if mn else [],
            numerator=mn.name if mn else None, denominator=md.name if md else None,
            expr=f"{mn.expr} / {md.expr}" if mn and md else ""))
    for name, k in singles:
        mm = measure_for(k)
        unit = str(k.get("unit") or "")
        out.append(us.Metric(
            name=mm.name if mm else og.snake(name), label=str(name),
            expr=mm.expr if mm else _calc_expr(str(k.get("calculation") or "")),
            format=us.metric_format(name, mm.expr if mm else "", unit),
            description=str(k.get("description") or ""), view=mm.view if mm else "",
            entity=mm.entity if mm else None, time_dims=mm.time_dims if mm else [],
            category_dims=mm.category_dims if mm else []))
    # Ratios read first: they are the rates a revenue-cycle lead watches.
    out.sort(key=lambda m: (m.numerator is None, m.format != "percent"))
    return out[:limit]


def headline_measures(measures: Sequence[us.Metric], limit: int = 4) -> list[us.Metric]:
    """When there are no KPIs: the first count, then money, then rates, one per view."""
    seen_views: set[str] = set()
    ranked = sorted(measures, key=lambda m: ({"integer": 0, "currency": 1, "percent": 2}
                                             .get(m.format, 3), m.view, m.name))
    out = []
    for m in ranked:
        if m.view in seen_views and len(out) < len({x.view for x in measures}):
            continue
        seen_views.add(m.view)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def pick_tables(entities: Sequence[us.Entity], tables: Iterable[str]) -> list[us.Entity]:
    want = [t.lower().split(".")[-1] for t in tables if t]
    if not want:
        return list(entities)
    return [e for e in entities if e.name.lower() in want or e.table.lower() in want]
