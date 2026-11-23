"""ui_spec.py — the UI spec behind ``/kb-ui`` and the rules that fill it (2026-11).

The spec is the reusable middle of the UI generator: every input mode
(a schema, a data product, later a free-text description or a page type)
is turned into the same structure, and everything after it (grounding,
code, install list) reads only the spec.

    App ─┬─ entities: Entity ── fields: Field          (what the data is)
         ├─ metrics:  Metric ── dims: Dimension         (what is measured)
         └─ pages:    Page   ── regions: Region          (what the screen is)
                                 └─ bindings, why[]

The mapping from data to components is deterministic (``field_kind``,
``control_for``, ``chart_for`` …) so the same input always gives the
same screen, and every choice can say where it came from (``Region.why``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

SPEC_VERSION = "1.0"
STACKS = ("vite", "nextjs")
BASES = ("base", "radix")
ARCHETYPES = ("dashboard", "list", "form")
FIELD_KINDS = ("id", "fk", "text", "longtext", "enum", "integer", "number", "currency",
               "percent", "date", "timestamp", "boolean")
REGION_ROLES = ("kpi", "trend", "breakdown", "table", "form")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Field:
    name: str
    label: str
    kind: str
    physical_type: str = ""
    required: bool = False
    restricted: bool = False
    enum_values: list[str] = field(default_factory=list)
    description: str = ""
    references: Optional[str] = None      # fk target entity, when known


@dataclass
class Entity:
    name: str                              # snake_case table name
    label: str                             # "Claim"
    plural: str                            # "Claims"
    table: str = ""                        # physical name, when known
    description: str = ""
    fields: list[Field] = field(default_factory=list)

    @property
    def pk(self) -> Optional[Field]:
        return next((f for f in self.fields if f.kind == "id"), None)


@dataclass
class Dimension:
    name: str
    label: str
    expr: str = ""
    time_grain: Optional[str] = None       # 'month' | 'year' | 'day' | …
    values: list[str] = field(default_factory=list)


@dataclass
class Metric:
    name: str
    label: str
    expr: str = ""
    format: str = "number"                 # number | integer | currency | percent
    description: str = ""
    view: str = ""                         # metric view it came from
    entity: Optional[str] = None
    time_dims: list[Dimension] = field(default_factory=list)
    category_dims: list[Dimension] = field(default_factory=list)
    numerator: Optional[str] = None        # ratio KPIs built from two measures
    denominator: Optional[str] = None


@dataclass
class Why:
    source: str                            # 'book' | 'doc' | 'registry' | 'rule'
    label: str
    ref: str = ""                          # 'chapter:123', 'doc_section:45', a URL


@dataclass
class Region:
    role: str
    pattern: str                           # shadcn component or block pattern
    title: str = ""
    bindings: dict[str, Any] = field(default_factory=dict)
    why: list[Why] = field(default_factory=list)


@dataclass
class Page:
    slug: str
    title: str
    archetype: str
    entity: Optional[str] = None
    description: str = ""
    regions: list[Region] = field(default_factory=list)


@dataclass
class AppSpec:
    name: str
    title: str
    stack: str = "vite"
    base: str = "base"
    mode: str = "schema"
    description: str = ""
    domain: str = ""
    healthcare: bool = False
    read_only: bool = False
    entities: list[Entity] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    npm: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def entity(self, name: Optional[str]) -> Optional[Entity]:
        return next((e for e in self.entities if e.name == name), None)

    def metric(self, name: Optional[str]) -> Optional[Metric]:
        return next((m for m in self.metrics if m.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d = {"specVersion": SPEC_VERSION, **d}
        return d


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_ACRONYMS = {"id", "drg", "mdc", "cpt", "icd", "npi", "edi", "pos", "ndc", "hcpcs", "carc",
             "rarc", "ub", "cms", "rac", "pcp", "mpi", "adt", "los", "kpi", "sla", "eft", "url",
             "api", "sku", "po", "ein", "ssn", "mrn", "dob", "hl7", "fhir"}


def label(name: str) -> str:
    """claim_status -> 'Claim status'; drg_code -> 'DRG code'."""
    words = [w for w in re.split(r"[_\-\s]+", name or "") if w]
    if not words:
        return name
    out = [w.upper() if w.lower() in _ACRONYMS else w.lower() for w in words]
    if out[0].islower():
        out[0] = out[0][0].upper() + out[0][1:]
    return " ".join(out)


def singular(word: str) -> str:
    w = word or ""
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        return w[:-1]
    return w


def plural(word: str) -> str:
    w = word or ""
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "ch", "sh")):
        return w + "es"
    return w + "s"


def pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", name or "") if w) or "X"


def camel(name: str) -> str:
    p = pascal(name)
    return p[:1].lower() + p[1:]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "app"


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------

_MONEY = re.compile(r"(amount|price|cost|charge|charges|paid|billed|balance|payment|revenue|"
                    r"fee|fees|copay|deductible|premium|salary|total_due|allowed)($|_)")
_PERCENT = re.compile(r"(^|_)(rate|pct|percent|percentage|ratio|share)($|_)")
_LONG = re.compile(r"(description|notes?|comment|comments|rationale|narrative|reason_text|"
                   r"summary|body|text)$")
_ENUMISH = re.compile(r"(^|_)(status|type|code|level|category|method|outcome|priority|stage|"
                      r"class|kind|mode|channel|tier|gender|sex|state|reason|indicator|"
                      r"currency_code|unit|units_of_measure)($|_)")
_BOOL_NAME = re.compile(r"(^is_|^has_|_flag$|_ind$|^can_|_yn$)")
_AUDIT = re.compile(r"^(created|updated|modified|last_modified|deleted|inserted|load|loaded|"
                    r"etl|ingested|record)_(at|on|ts|timestamp|date|by|time)$|^_")
_EG = re.compile(r"\((?:e\.g\.?|eg|for example|such as)[,:]?\s*([^)]*)\)", re.IGNORECASE)
_EG_LOOSE = re.compile(r"(?:e\.g\.?|for example|such as)[,:]?\s*([^.;]*)", re.IGNORECASE)


def enum_values_from_text(text: str, limit: int = 8) -> list[str]:
    """Values a description lists as examples: '(e.g., submitted, paid, denied)'."""
    m = _EG.search(text or "") or _EG_LOOSE.search(text or "")
    if not m:
        return []
    raw = re.split(r",|\bor\b|/", m.group(1))
    out: list[str] = []
    for v in raw:
        v = v.strip().strip("'\"` ").rstrip(".")
        v = re.sub(r"^(and|or)\s+", "", v, flags=re.IGNORECASE)
        if v and len(v) <= 32 and len(v.split()) <= 3 and v.lower() not in ("etc", "etc."):
            out.append(v)
    seen: dict[str, None] = {}
    for v in out:
        seen.setdefault(v, None)
    vals = list(seen)[:limit]
    return vals if len(vals) >= 2 else []


def field_kind(name: str, logical: str, *, primary_key: bool = False) -> str:
    """The UI kind of a column, from its name and logical type."""
    n = (name or "").lower()
    lt = (logical or "string").lower()
    if primary_key:
        return "id"
    if lt in ("integer", "number") and re.search(r"(^|_)id$", n):
        return "fk"
    if lt == "string" and re.search(r"_id$", n):
        return "fk"
    if lt == "boolean" or (_BOOL_NAME.search(n) and lt in ("string", "integer", "boolean")):
        return "boolean"
    if lt == "date":
        return "date"
    if lt == "timestamp":
        return "timestamp"
    if lt in ("number", "integer"):
        if _PERCENT.search(n):
            return "percent"
        if _MONEY.search(n):
            return "currency"
        if lt == "integer" or re.search(r"(count|number|qty|quantity|units)$", n):
            return "integer"
        return "number"
    if lt == "string":
        if _LONG.search(n):
            return "longtext"
        if _ENUMISH.search(n) and not n.endswith(("_description", "_name", "_text")):
            return "enum"
    return "text"


def is_audit(name: str) -> bool:
    return bool(_AUDIT.search((name or "").lower()))


CONTROL_BY_KIND = {
    # kind -> (component pattern, shadcn registry items it needs)
    "text": ("Input", ["input"]),
    "longtext": ("Textarea", ["textarea"]),
    "integer": ("Input type=number", ["input"]),
    "number": ("Input type=number", ["input"]),
    "currency": ("Input type=number step=0.01", ["input"]),
    "percent": ("Input type=number", ["input"]),
    "date": ("Date Picker (Popover + Calendar)", ["popover", "calendar", "button"]),
    "timestamp": ("Date Picker (Popover + Calendar)", ["popover", "calendar", "button"]),
    "boolean": ("Switch", ["switch"]),
    "fk": ("Input (lookup)", ["input"]),
}


def control_for(f: Field) -> tuple[str, list[str]]:
    """Form control for a field: (pattern, registry items)."""
    if f.kind == "enum":
        if f.enum_values and len(f.enum_values) <= 4:
            return "Radio Group", ["radio-group"]
        if f.enum_values:
            return "Select", ["select"]
        return "Input", ["input"]                 # values unknown: free text + TODO
    return CONTROL_BY_KIND.get(f.kind, ("Input", ["input"]))


def zod_for(f: Field) -> str:
    """zod expression for a form field."""
    k = f.kind
    if k == "boolean":
        z = "z.boolean()"
    elif k in ("integer", "fk") and f.physical_type.lower() not in ("string", "varchar"):
        z = "z.number().int()"
    elif k in ("number", "currency"):
        z = "z.number()" + (".nonnegative()" if k == "currency" else "")
    elif k == "percent":
        z = "z.number().min(0).max(100)"
    elif k in ("date", "timestamp"):
        z = "z.date()"
    elif k == "enum" and f.enum_values:
        z = "z.enum([" + ", ".join(_ts_str(v) for v in f.enum_values) + "])"
    else:
        z = "z.string()"
        if f.required:
            z += f'.min(1, "{f.label} is required.")'
        else:
            z += ".max(4000)"
    if not f.required and k not in ("boolean",):
        z += ".optional()"
    return z


def _ts_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Table columns: what a list shows first, and what it never shows.
_STATUSISH = re.compile(r"(^|_)(status|type|category|outcome|level|priority|stage|state|class)$")
_KEY_LABEL = re.compile(r"(^|_)(name|number|title)$")
_LOW_VALUE = re.compile(r"(^|_)(currency_code|batch_|control_number|sequence|version)")


def _table_rank(f: Field) -> tuple[int, str]:
    n = f.name.lower()
    if f.kind == "id":
        return 0, n
    if f.kind == "text" and _KEY_LABEL.search(n):
        return 1, n
    if f.kind == "enum" and _STATUSISH.search(n):
        return 2, n
    if f.kind == "currency":
        if n.startswith("total_") or "denied" in n:
            return 3, n
        return (4 if re.search(r"(billed|paid|denied|charge|payment)", n) else 5), n
    if f.kind in ("date", "timestamp"):
        return 4, n
    if _LOW_VALUE.search(n):
        return 9, n
    if f.kind == "enum":
        return 6, n
    if f.kind in ("integer", "number", "percent", "boolean"):
        return 7, n
    return 8, n


def table_fields(e: Entity, limit: int = 7) -> list[Field]:
    """Columns for the entity's Data Table: the key, a label (name/number), statuses,
    money, dates; never foreign-key ids, long text, audit or restricted columns.
    At most two money and two date columns, so one kind can't fill the table."""
    keep = [f for f in e.fields if f.kind not in ("fk", "longtext")
            and not f.restricted and not is_audit(f.name)]
    out: list[Field] = []
    per_kind: dict[str, int] = {}
    for f in sorted(keep, key=_table_rank):
        group = "date" if f.kind in ("date", "timestamp") else f.kind
        cap = {"currency": 2, "date": 2, "text": 2, "boolean": 1}.get(group, 3)
        if per_kind.get(group, 0) >= cap and f.kind != "id":
            continue
        per_kind[group] = per_kind.get(group, 0) + 1
        out.append(f)
        if len(out) >= limit:
            break
    return out


def filter_fields(e: Entity, shown: Sequence[Field]) -> tuple[Optional[Field], list[Field]]:
    """(text search column, enum facet columns) for the table toolbar."""
    text = next((f for f in shown if f.kind == "text"), None) or \
        next((f for f in shown if f.kind == "id"), None)
    facets = [f for f in shown if f.kind == "enum"][:2]
    return text, facets


def form_fields(e: Entity, limit: int = 12) -> list[Field]:
    """Fields a create/edit form asks for: not the key, not audit columns, required first."""
    keep = [f for f in e.fields if f.kind != "id" and not is_audit(f.name)]
    keep.sort(key=lambda f: (not f.required, f.kind == "fk", f.kind == "longtext"))
    return keep[:limit]


# ---------------------------------------------------------------------------
# Metric rules
# ---------------------------------------------------------------------------

def metric_format(name: str, expr: str = "", unit: str = "") -> str:
    n = f"{name} {unit}".lower()
    e = (expr or "").lower()
    if re.search(r"(rate|pct|percent|percentage|ratio)", n):
        return "percent"
    if re.search(r"(count|number_of|total_claims|volume|^num_)", n) or e.startswith("count("):
        return "integer"
    if re.search(r"(amount|billed|paid|revenue|cost|charge|payment|dollars|currency|balance)", n):
        return "currency"
    if "days" in n or "hours" in n or "minutes" in n:
        return "number"
    return "number"


def time_grain(name: str, expr: str = "") -> Optional[str]:
    s = f"{name} {expr}".lower()
    m = re.search(r"date_trunc\(\s*'(\w+)'", s)
    if m:
        return m.group(1).lower()
    for g in ("month", "quarter", "week", "year", "day"):
        if re.search(rf"(^|_){g}($|_)", name.lower()) or f"{g}(" in s:
            return g
    return None


def chart_for(metric: Metric, role: str) -> tuple[str, str]:
    """(chart pattern, registry example it starts from) for a metric in a region role.

    Trend: a measure over time is an area chart (a line when it is a rate, since
    rates don't sum). Breakdown: a measure across categories is a horizontal bar
    chart, sorted, which reads better than a pie past a handful of parts."""
    if role == "trend":
        if metric.format == "percent":
            return "Line chart", "chart-line-default"
        return "Area chart", "chart-area-interactive"
    if role == "breakdown":
        return "Bar chart (horizontal)", "chart-bar-horizontal"
    return "Card", "section-cards"


def pick_time_dim(m: Metric) -> Optional[Dimension]:
    order = {"month": 0, "week": 1, "quarter": 2, "day": 3, "year": 4}
    dims = sorted(m.time_dims, key=lambda d: order.get(d.time_grain or "", 9))
    return dims[0] if dims else None


_GOOD_CATEGORY = re.compile(r"(status|type|reason|category|payer|department|service_line|"
                            r"specialty|level|outcome|method|channel|class|setting)")


def pick_category_dim(m: Metric) -> Optional[Dimension]:
    dims = [d for d in m.category_dims if not d.name.endswith(("_flag", "_id"))]
    dims.sort(key=lambda d: (not d.values, not _GOOD_CATEGORY.search(d.name), d.name))
    return dims[0] if dims else None


# ---------------------------------------------------------------------------
# JSON Schema (ui-spec.json is checked against it)
# ---------------------------------------------------------------------------

def _obj(props: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


_WHY = _obj({"source": {"enum": ["book", "doc", "registry", "rule"]},
             "label": {"type": "string"}, "ref": {"type": "string"}}, ["source", "label"])
_FIELD = _obj({"name": {"type": "string"}, "label": {"type": "string"},
               "kind": {"enum": list(FIELD_KINDS)}, "physical_type": {"type": "string"},
               "required": {"type": "boolean"}, "restricted": {"type": "boolean"},
               "enum_values": {"type": "array", "items": {"type": "string"}},
               "description": {"type": "string"},
               "references": {"type": ["string", "null"]}}, ["name", "label", "kind"])
_DIM = _obj({"name": {"type": "string"}, "label": {"type": "string"}, "expr": {"type": "string"},
             "time_grain": {"type": ["string", "null"]},
             "values": {"type": "array", "items": {"type": "string"}}}, ["name", "label"])

SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/mark64oswald/myPub/schemas/ui-spec.schema.json",
    "title": "myPub UI spec",
    **_obj({
        "specVersion": {"const": SPEC_VERSION},
        "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "title": {"type": "string"},
        "stack": {"enum": list(STACKS)},
        "base": {"enum": list(BASES)},
        "mode": {"enum": ["schema", "data-product", "describe", "page"]},
        "description": {"type": "string"},
        "domain": {"type": "string"},
        "healthcare": {"type": "boolean"},
        "read_only": {"type": "boolean"},
        "entities": {"type": "array", "items": _obj({
            "name": {"type": "string"}, "label": {"type": "string"},
            "plural": {"type": "string"}, "table": {"type": "string"},
            "description": {"type": "string"},
            "fields": {"type": "array", "items": _FIELD}}, ["name", "label", "fields"])},
        "metrics": {"type": "array", "items": _obj({
            "name": {"type": "string"}, "label": {"type": "string"}, "expr": {"type": "string"},
            "format": {"enum": ["number", "integer", "currency", "percent"]},
            "description": {"type": "string"}, "view": {"type": "string"},
            "entity": {"type": ["string", "null"]},
            "time_dims": {"type": "array", "items": _DIM},
            "category_dims": {"type": "array", "items": _DIM},
            "numerator": {"type": ["string", "null"]},
            "denominator": {"type": ["string", "null"]}}, ["name", "label", "format"])},
        "pages": {"type": "array", "minItems": 1, "items": _obj({
            "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
            "title": {"type": "string"}, "archetype": {"enum": list(ARCHETYPES)},
            "entity": {"type": ["string", "null"]}, "description": {"type": "string"},
            "regions": {"type": "array", "items": _obj({
                "role": {"enum": list(REGION_ROLES)}, "pattern": {"type": "string"},
                "title": {"type": "string"}, "bindings": {"type": "object"},
                "why": {"type": "array", "items": _WHY}}, ["role", "pattern"])}},
            ["slug", "title", "archetype", "regions"])},
        "install": {"type": "array", "items": {"type": "string"}},
        "npm": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    }, ["specVersion", "name", "title", "stack", "base", "mode", "entities", "pages", "install"]),
}


def check_spec(doc: dict[str, Any]) -> list[str]:
    """Errors from the JSON Schema plus cross-references (page entity, bound metrics)."""
    errors: list[str] = []
    try:
        import jsonschema  # pylint: disable=import-outside-toplevel
        v = jsonschema.Draft202012Validator(SPEC_SCHEMA)
        for e in sorted(v.iter_errors(doc), key=lambda e: list(e.path)):
            path = "/".join(str(p) for p in e.path) or "(root)"
            errors.append(f"{path}: {e.message}")
    except ImportError:
        errors.append("jsonschema not installed; schema not checked")
    ents = {e.get("name") for e in doc.get("entities", [])}
    mets = {m.get("name") for m in doc.get("metrics", [])}
    slugs: set[str] = set()
    for p in doc.get("pages", []):
        if p.get("slug") in slugs:
            errors.append(f"pages: duplicate slug {p.get('slug')!r}")
        slugs.add(p.get("slug"))
        if p.get("entity") and p["entity"] not in ents:
            errors.append(f"pages/{p.get('slug')}: unknown entity {p['entity']!r}")
        for r in p.get("regions", []):
            for key in ("metric", "numerator", "denominator"):
                m = (r.get("bindings") or {}).get(key)
                if m and m not in mets:
                    errors.append(f"pages/{p.get('slug')}: unknown metric {m!r}")
            for m in (r.get("bindings") or {}).get("metrics", []) or []:
                if m not in mets:
                    errors.append(f"pages/{p.get('slug')}: unknown metric {m!r}")
    return errors


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))
