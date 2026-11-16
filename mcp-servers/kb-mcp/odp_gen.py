"""odp_gen.py — shared pieces for the Open Data Product generators (2026-11).

Used by ``/kb-data-contract`` (odcs_contract_gen), ``/kb-dp-portfolio``
(odp_portfolio_gen), ``/kb-dp-recipe`` (odp_recipe_gen), ``/kb-dp-review``
(odp_review_gen) and the Bootstrap ``data-product`` profile (odp_profile):

* ODCS v3.1.0 building blocks: column parsing (``name type`` lines or a
  ``CREATE TABLE``), SQL → ODCS logical types, platform-specific ``servers``
  entries, default quality rules and SLA properties.
* Schema checks without the SDK: ODCS against the vendored JSON Schema
  (``data/odp/odcs-json-schema-v3.1.0.json``, Apache 2.0, from
  bitol-io/open-data-contract-standard) when ``jsonschema`` is installed.
* SDK checks when ``open-data-products`` is on PATH: ``validate``,
  ``recipe validate``, ``odpc-build``, ``odpg-analyze``, ``product audit``.
  Every SDK call is optional; the generators still run without it and say so.
* Live-doc citations for the ODP specs and ODCS (sources seeded by
  scripts/seed_odp_family.py).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ODCS_SCHEMA_FILE = REPO_ROOT / "data" / "odp" / "odcs-json-schema-v3.1.0.json"
ODCS_VERSION = "v3.1.0"
MARKER = "TODO: populate from stack context"

# doc_source.name values written by scripts/seed_odp_family.py.
ODPS_SOURCES = ("Open Data Product Specification (ODPS 4.2)",)
ODPC_SOURCES = ("Open Data Product Catalogs (ODPC 1.0)",)
ODPG_SOURCES = ("Open Data Product Graphs (ODPG 1.0)",)
ODPR_SOURCES = ("Open Data Product Recipe Specification (ODPR 1.0)",)
ODCS_SOURCES = ("ODCS: overview", "ODCS: fundamentals", "ODCS: schema",
                "ODCS: data quality", "ODCS: service-level agreement")
FAMILY_MAP_SOURCES = ("ODP family map (myPub)",)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:limit].rstrip("-") or "data-product")


def snake(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s or "dataset"


def title(text: str) -> str:
    words = re.split(r"[-_\s]+", text or "")
    return " ".join(w if w.isupper() else w.capitalize() for w in words if w)


def upper_id(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", (text or "").upper()).strip("-")


def first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def dump_yaml(doc: Any, header: str = "") -> str:
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    if header:
        lines = "".join(f"# {h}\n" if h else "#\n" for h in header.splitlines())
        return lines + body
    return body


# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------

_PLATFORM_KEYWORDS = [
    ("databricks", ("databricks", "unity catalog", "delta sharing", "lakeflow", "delta lake")),
    ("snowflake", ("snowflake",)),
    ("bigquery", ("bigquery", "big query")),
    ("postgres", ("postgres", "postgresql")),
    ("api", (" rest api", " api ", "endpoint", "graphql")),
]


def detect_platform(text: str) -> str:
    padded = f" {(text or '').lower()} "
    for name, words in _PLATFORM_KEYWORDS:
        if any(w in padded for w in words):
            return name
    return "generic"


def odcs_server(platform: str, location: Optional[str] = None, *, name: str = "production"
                ) -> dict[str, Any]:
    """One ODCS ``servers`` entry with the fields the schema requires for its type.

    ``location`` is a dotted name (``catalog.schema.table``) or a URL/path;
    parts it doesn't give become ``TODO`` placeholders that still validate.
    """
    parts = [p for p in (location or "").split(".") if p] if location and "/" not in location else []

    def part(i: int, default: str) -> str:
        return parts[i] if len(parts) > i else default

    base: dict[str, Any] = {"server": name, "environment": "prod"}
    if platform == "databricks":
        base.update(type="databricks", host="TODO.cloud.databricks.com",
                    catalog=part(0, "TODO_catalog"), schema=part(1, "TODO_schema"))
    elif platform == "snowflake":
        base.update(type="snowflake", account="TODO_account",
                    database=part(0, "TODO_database"), schema=part(1, "TODO_schema"))
    elif platform == "bigquery":
        base.update(type="bigquery", project=part(0, "TODO_project"),
                    dataset=part(1, "TODO_dataset"))
    elif platform == "postgres":
        base.update(type="postgres", host="TODO_host", port=5432,
                    database=part(0, "TODO_database"), schema=part(1, "public"))
    elif platform == "api":
        base.update(type="api", location=location or "https://TODO.example.com/api")
    else:
        base.update(type="local", path=location or "data/TODO.parquet", format="parquet")
    return base


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

_LOGICAL = [
    (("bigint", "smallint", "tinyint", "integer", "int", "long", "serial"), "integer"),
    (("decimal", "numeric", "float", "double", "real", "number", "money"), "number"),
    (("bool",), "boolean"),
    (("timestamp", "datetime"), "timestamp"),
    (("date",), "date"),
    (("time",), "time"),
    (("array", "list"), "array"),
    (("struct", "map", "json", "variant", "object", "record"), "object"),
]

# Column names that usually hold personal data. They are marked
# ``classification: restricted`` so the contract owner reviews them.
_PII = re.compile(r"(^|_)(email|e_mail|phone|mobile|ssn|social_security|first_name|last_name|"
                  r"full_name|name|dob|birth|address|street|zip|postal|mrn|patient|member_id|"
                  r"passport|license_no|ip_address|lat|lon|latitude|longitude)($|_)")


def logical_type(physical: str) -> str:
    p = (physical or "").lower()
    for words, logical in _LOGICAL:
        if any(p.startswith(w) for w in words):
            return logical
    return "string"


@dataclass
class Column:
    name: str
    physical_type: str = "string"
    required: bool = False
    primary_key: bool = False
    description: str = ""

    @property
    def logical_type(self) -> str:
        return logical_type(self.physical_type)

    @property
    def personal(self) -> bool:
        return bool(_PII.search(self.name.lower()))


def _split_top(text: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def parse_ddl(ddl: str) -> tuple[Optional[str], list[Column]]:
    """Columns from a ``CREATE TABLE`` statement: ``(table_name, columns)``."""
    m = re.search(r"create\s+(?:or\s+replace\s+)?(?:temporary\s+|temp\s+)?table\s+"
                  r"(?:if\s+not\s+exists\s+)?([`\"\[\]\w.]+)\s*\((.*)\)", ddl or "",
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return None, []
    name = re.sub(r"[`\"\[\]]", "", m.group(1))
    cols: list[Column] = []
    pk: set[str] = set()
    for raw in _split_top(m.group(2)):
        item = " ".join(raw.split())
        low = item.lower()
        if low.startswith(("primary key", "constraint", "unique", "foreign key", "check", "index", "key ")):
            pk_m = re.search(r"primary\s+key\s*\(([^)]*)\)", item, re.IGNORECASE)
            if pk_m:
                pk |= {c.strip(" `\"[]").lower() for c in pk_m.group(1).split(",")}
            continue
        head, _, tail = item.partition(" ")
        if not tail:
            continue
        cname = head.strip("`\"[]")
        depth, end = 0, len(tail)
        for i, ch in enumerate(tail):          # the type ends at a space outside <> / ()
            if ch in "(<":
                depth += 1
            elif ch in ")>":
                depth -= 1
            elif ch == " " and depth == 0:
                end = i
                break
        ctype, rest = tail[:end], tail[end:].lower()
        comment = re.search(r"comment\s+'([^']*)'", item, re.IGNORECASE)
        cols.append(Column(cname, ctype, required="not null" in rest or "primary key" in rest,
                           primary_key="primary key" in rest,
                           description=comment.group(1) if comment else ""))
    for c in cols:
        if c.name.lower() in pk:
            c.primary_key, c.required = True, True
    return name, cols


def parse_columns(specs: Iterable[str]) -> list[Column]:
    """Columns from short specs: ``"order_id bigint pk"``, ``"email string required"``,
    ``"amount decimal(10,2) -- order total"``."""
    out: list[Column] = []
    for spec in specs or []:
        text, _, desc = str(spec).partition("--")
        words = text.replace(":", " ").split()
        if not words:
            continue
        flags = {w.lower() for w in words[2:]}
        out.append(Column(words[0], words[1] if len(words) > 1 else "string",
                          required=bool(flags & {"required", "not_null", "notnull", "pk"}),
                          primary_key=bool(flags & {"pk", "primary_key", "key"}),
                          description=desc.strip()))
    return out


# ---------------------------------------------------------------------------
# ODCS contract
# ---------------------------------------------------------------------------

def odcs_property(c: Column) -> dict[str, Any]:
    prop: dict[str, Any] = {"name": c.name, "logicalType": c.logical_type,
                            "physicalType": c.physical_type}
    if c.primary_key:
        prop["primaryKey"] = True
        prop["unique"] = True
    if c.required:
        prop["required"] = True
    prop["description"] = c.description or f"TODO: describe {c.name}."
    if c.personal:
        prop["classification"] = "restricted"
    return prop


def default_quality(cols: Sequence[Column]) -> list[dict[str, Any]]:
    """Table-level checks the columns imply: rows exist, keys unique, required filled."""
    rules: list[dict[str, Any]] = [{
        "name": "table_not_empty", "type": "library", "metric": "rowCount",
        "mustBeGreaterThan": 0, "dimension": "completeness", "severity": "error",
        "description": "The table has at least one row after each load.",
    }]
    for c in cols:
        if c.primary_key:
            rules.append({
                "name": f"{c.name}_unique", "type": "library", "metric": "duplicateValues",
                "arguments": {"properties": [c.name]}, "mustBe": 0, "unit": "rows",
                "dimension": "uniqueness", "severity": "error",
                "description": f"{c.name} identifies one row.",
            })
    return rules


def column_quality(c: Column) -> list[dict[str, Any]]:
    if c.required and not c.primary_key:
        return [{"name": f"{c.name}_not_null", "type": "library", "metric": "nullValues",
                 "mustBe": 0, "unit": "rows", "dimension": "completeness", "severity": "error",
                 "description": f"Every row has a {c.name}."}]
    return []


DEFAULT_SLA = [
    {"property": "availability", "value": 99.5, "unit": "percent",
     "description": "TODO: confirm with the consumers."},
    {"property": "frequency", "value": 1, "unit": "d",
     "description": "TODO: how often the table is refreshed."},
    {"property": "latency", "value": 4, "unit": "h", "element": "updated_at",
     "description": "TODO: maximum delay between the source change and the table."},
    {"property": "retention", "value": 3, "unit": "y",
     "description": "TODO: how long rows are kept."},
    {"property": "timeToRepair", "value": 2, "unit": "d",
     "description": "TODO: how quickly a broken load is fixed."},
]


def odcs_contract(*, contract_id: str, name: str, table: str, columns: Sequence[Column],
                  purpose: str, platform: str, location: Optional[str] = None,
                  domain: Optional[str] = None, owner: Optional[str] = None,
                  version: str = "0.1.0", status: str = "draft",
                  sla: Optional[list[dict[str, Any]]] = None,
                  physical_type: str = "table") -> dict[str, Any]:
    """A complete ODCS v3.1.0 contract as a dict (dump with :func:`dump_yaml`)."""
    obj: dict[str, Any] = {
        "name": table, "physicalName": table, "physicalType": physical_type,
        "logicalType": "object",
        "description": f"TODO: what one row of {table} represents.",
        "properties": [],
    }
    for c in columns:
        prop = odcs_property(c)
        q = column_quality(c)
        if q:
            prop["quality"] = q
        obj["properties"].append(prop)
    obj["quality"] = default_quality(columns)
    has_updated = any(c.name.lower() in ("updated_at", "modified_at", "last_updated")
                      for c in columns)
    sla_rows = [dict(r) for r in (sla or DEFAULT_SLA)]
    for r in sla_rows:
        if r.get("element") == "updated_at":
            if has_updated:
                upd = next(c.name for c in columns
                           if c.name.lower() in ("updated_at", "modified_at", "last_updated"))
                r["element"] = f"{table}.{upd}"
            else:
                r.pop("element")
    doc: dict[str, Any] = {
        "apiVersion": ODCS_VERSION, "kind": "DataContract", "id": contract_id,
        "name": name, "version": version, "status": status,
    }
    if domain:
        doc["domain"] = domain
    doc["description"] = {
        "purpose": purpose or f"TODO: why {table} exists.",
        "usage": "TODO: who reads it and for what.",
        "limitations": "TODO: known gaps, lag or exclusions.",
    }
    doc["schema"] = [obj]
    doc["servers"] = [odcs_server(platform, location)]
    doc["slaProperties"] = sla_rows
    doc["team"] = {"name": owner or "TODO: owning team",
                   "members": [{"username": "TODO-owner", "role": "Data Product Owner"}]}
    doc["support"] = [{"channel": "TODO-channel", "tool": "slack",
                       "description": "Questions and incident reports."}]
    return doc


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    tool: str                    # "odcs-schema", "sdk validate", ...
    target: str
    ok: Optional[bool]           # None = not run
    messages: list[str] = field(default_factory=list)

    def line(self) -> str:
        status = {True: "passes", False: "FAILS", None: "not run"}[self.ok]
        out = f"- `{self.target}` ({self.tool}): {status}"
        return out + "".join(f"\n  - {m}" for m in self.messages)


def check_odcs(doc: dict[str, Any], target: str) -> CheckResult:
    """Validate an ODCS document against the vendored v3.1.0 JSON Schema."""
    try:
        import jsonschema  # noqa: PLC0415
    except ImportError:
        missing = [k for k in ("apiVersion", "kind", "id", "version", "status") if k not in doc]
        return CheckResult("odcs-schema", target, None if not missing else False,
                           [f"missing {k}" for k in missing]
                           or ["jsonschema not installed; checked required keys only "
                               "(pip install jsonschema for the full check)"])
    schema = json.loads(ODCS_SCHEMA_FILE.read_text())
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    msgs = [f"/{'/'.join(str(p) for p in e.path)}: {e.message[:200]}" for e in errors[:20]]
    return CheckResult("odcs-schema v3.1.0", target, not errors, msgs)


def sdk_path() -> Optional[str]:
    return shutil.which("open-data-products")


def run_sdk(args: Sequence[str], cwd: Path, *, timeout: int = 120) -> tuple[Optional[int], str]:
    """Run the SDK CLI; ``(None, reason)`` when it isn't installed."""
    exe = sdk_path()
    if exe is None:
        return None, "open-data-products is not installed (pip install open-data-products)"
    try:
        proc = subprocess.run([exe, *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"SDK call failed: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def sdk_check(files: dict[str, str], commands: Sequence[tuple[str, Sequence[str]]],
              ) -> list[CheckResult]:
    """Write ``files`` to a temp folder and run each ``(label, args)`` SDK command there."""
    if sdk_path() is None:
        return [CheckResult("sdk", label, None,
                            ["open-data-products not installed; run `make validate` later"])
                for label, _ in commands]
    out: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="mypub-odp-") as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        for label, args in commands:
            code, text = run_sdk(args, root)
            msgs = [] if code == 0 else [ln for ln in text.splitlines()
                                         if ln.strip() and not ln.startswith("✓")][-8:]
            out.append(CheckResult(f"sdk {args[0]}", label, None if code is None else code == 0,
                                   msgs))
    return out


def render_checks(title_: str, results: Sequence[CheckResult], footer: str = "") -> str:
    lines = [f"# {title_}", ""]
    lines += [r.line() for r in results] or ["_No checks ran._"]
    if footer:
        lines += ["", footer]
    return "\n".join(lines) + "\n"


def check_errors(results: Sequence[CheckResult]) -> list[str]:
    return [f"{r.target}: {m}" for r in results if r.ok is False for m in (r.messages or ["failed"])]
