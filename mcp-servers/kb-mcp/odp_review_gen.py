"""odp_review_gen.py — ``/kb-dp-review``: grade an ODPS product file and suggest fixes (2026-11).

Input: a path to an ODPS 4.2 product (``odps_path``), optionally its ODCS
contract and ODPG graph.

Checks, all deterministic:

1. Schema: ``open-data-products validate`` (when installed), else a check of
   the keys ODPS 4.2 requires.
2. Completeness: each ODPS section scored 0 (missing), 1 (present but draft:
   ``TODO``, ``example.com``, zero targets, placeholder holders) or 2 (filled),
   weighted into a letter grade.
3. Consistency: the contract the product points at exists and passes the ODCS
   schema; SDK ``product audit`` findings; ``odpg-analyze`` advice for the graph.
4. Backing: each finding cites the ODPS spec section and, where the KB has
   one, a procedure from the books on that topic (SLA, data quality, data
   contracts, data products).

Output:

    <package>/
      review.md                   grade, section table, findings with fixes and citations
      findings.json               the same findings, machine-readable
      _provenance.md
      _prompts/apply-fixes.md     optional subagent pass that edits the product file
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb
import yaml

import kb_scaffold as ks
import odp_gen as og
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "dp_review"

# (section, weight, KB topic for book backing, spec keyword, why it matters)
SECTIONS = [
    ("details", 3, "Data Product", "details",
     "Who the product is for and what it is; consumers find and trust it through this."),
    ("productStrategy", 2, "Data Product Strategy", "productStrategy",
     "Ties the product to business objectives and KPIs, so its value can be measured."),
    ("contract", 2, "Data Contract", "contract",
     "The agreement on schema and semantics consumers build against."),
    ("SLA", 2, "Service Level Agreement", "SLA",
     "Availability and freshness promises consumers plan around."),
    ("dataQuality", 2, "Data Quality", "dataQuality",
     "Measurable quality targets, so 'good enough' is agreed, not assumed."),
    ("dataAccess", 2, "Data Access", "dataAccess",
     "How consumers actually get the data."),
    ("license", 1, "Data License", "license",
     "What consumers may and may not do with the data."),
    ("dataHolder", 1, "Data Product Owner", "dataHolder",
     "Who is accountable and how to reach them."),
    ("pricingPlans", 0, "Data Monetization", "pricingPlans",
     "Only needed when the product is charged for or cost-allocated."),
]

REQUIRED_DETAILS = ("name", "productID", "visibility", "status", "type")
RECOMMENDED_DETAILS = ("description", "valueProposition", "productVersion", "tags")
_PLACEHOLDER = re.compile(r"\bTODO\b|example\.com|Your organisation|lorem ipsum", re.IGNORECASE)


@dataclass
class Finding:
    section: str
    severity: str            # "error" | "warning" | "advice"
    message: str
    fix: str
    spec: list[str] = field(default_factory=list)       # citation labels
    library: list[str] = field(default_factory=list)    # procedure / book labels


def _placeholders(node: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _placeholders(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _placeholders(v, f"{path}[{i}]")
    elif isinstance(node, str) and _PLACEHOLDER.search(node):
        out.append(path)
    return out


def _zero_targets(node: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if k in ("target", "objective") and v in (0, "0"):
                out.append(p)
            else:
                out += _zero_targets(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _zero_targets(v, f"{path}[{i}]")
    return out


def score_section(name: str, value: Any) -> tuple[int, list[str]]:
    """``(score, notes)``: 0 missing, 1 draft, 2 filled."""
    if value in (None, {}, []):
        return 0, ["missing"]
    notes: list[str] = []
    if name == "details":
        en = value.get("en") if isinstance(value, dict) else None
        en = en or (next(iter(value.values()), {}) if isinstance(value, dict) else {})
        missing = [k for k in REQUIRED_DETAILS if not en.get(k)]
        if missing:
            return 0, [f"required field(s) missing: {', '.join(missing)}"]
        rec = [k for k in RECOMMENDED_DETAILS if not en.get(k)]
        if rec:
            notes.append(f"recommended field(s) missing: {', '.join(rec)}")
        if en.get("status") == "draft":
            notes.append("status is draft")
    ph = _placeholders(value)
    zero = _zero_targets(value)
    if ph:
        notes.append(f"{len(ph)} placeholder value(s), e.g. {ph[0]}")
    if zero:
        notes.append(f"{len(zero)} zero target(s), e.g. {zero[0]}")
    return (1 if notes and (ph or zero or name == "details" and any("missing" in n for n in notes))
            else 2), notes


def grade(scores: dict[str, int]) -> tuple[str, float]:
    total = sum(w * 2 for s, w, *_ in SECTIONS if w)
    got = sum(w * scores.get(s, 0) for s, w, *_ in SECTIONS if w)
    pct = got / total if total else 0.0
    letter = "A" if pct >= 0.9 else "B" if pct >= 0.75 else "C" if pct >= 0.6 else \
        "D" if pct >= 0.4 else "F"
    return letter, pct


FIXES = {
    "details": "Fill name, productID, visibility, status and type; add a valueProposition "
               "a consumer understands in one sentence, and tags.",
    "productStrategy": "Link the product to an objective: contributesToKPI with a real target, "
                       "plus one or two productKPIs (id, name, unit, target, calculation).",
    "contract": "Point contract.default at an ODCS contract (generate one with /kb-data-contract).",
    "SLA": "Declare uptime, update frequency and time to repair with units, agreed with consumers.",
    "dataQuality": "Declare completeness, timeliness and the other dimensions that matter, "
                   "with objectives the pipeline can measure.",
    "dataAccess": "Give outputPortType, authenticationMethod and a documentation or access URL.",
    "license": "State the scope (rights, permanence, exclusivity), governance and termination.",
    "dataHolder": "Name the accountable organisation with a contact email (4.2: dataHolder.en). "
                  "Note: SDK 0.7's checker reads a flat dataHolder.email, so a schema-correct "
                  "block can fail `validate` until the SDK is fixed.",
    "pricingPlans": "Add pricing plans only if the product is charged for or cost-allocated.",
}


def review_product(doc: dict[str, Any]) -> tuple[dict[str, int], list[Finding]]:
    product = doc.get("product") or {}
    scores: dict[str, int] = {}
    findings: list[Finding] = []
    if doc.get("version") != "4.2" or "opendataproducts.org" not in str(doc.get("schema", "")):
        findings.append(Finding("document", "warning",
                                f"schema/version is {doc.get('schema')!r} / {doc.get('version')!r}",
                                "Use schema https://opendataproducts.org/v4.2/schema/odps.json "
                                "and version \"4.2\"."))
    for name, weight, *_ in SECTIONS:
        score, notes = score_section(name, product.get(name))
        scores[name] = score
        if score == 2 and not notes:
            continue
        if weight == 0 and score == 0:
            findings.append(Finding(name, "advice", "not present (optional)", FIXES[name]))
            continue
        sev = "error" if (score == 0 and weight >= 2) else "warning" if score < 2 else "advice"
        findings.append(Finding(name, sev, "; ".join(notes), FIXES[name]))
    return scores, findings


def render_review(name: str, letter: str, pct: float, scores: dict[str, int],
                  findings: Sequence[Finding], checks: Sequence[og.CheckResult]) -> str:
    label = {0: "missing", 1: "draft", 2: "filled"}
    rows = ["| Section | Weight | State |", "|---|---|---|"]
    rows += [f"| `{s}` | {w} | {label[scores.get(s, 0)]} |" for s, w, *_ in SECTIONS]
    lines = [f"# Review: {name}", "",
             f"**Grade {letter}** ({pct:.0%} of the weighted sections filled).", "",
             *rows, "", "## Findings", ""]
    order = {"error": 0, "warning": 1, "advice": 2}
    for f in sorted(findings, key=lambda x: (order[x.severity], x.section)):
        lines += [f"### {f.severity.upper()} · `{f.section}`", "", f"{f.message}.", "",
                  f"**Fix.** {f.fix}", ""]
        if f.spec:
            lines.append("Spec: " + "; ".join(f.spec))
        if f.library:
            lines.append("From the library: " + "; ".join(f.library))
        lines.append("")
    if not findings:
        lines += ["No findings.", ""]
    lines += ["## Tool checks", ""] + [c.line() for c in checks]
    return "\n".join(lines) + "\n"


APPLY_PROMPT = """\
# Apply the review to {file}

Read `review.md` and `findings.json`, then edit `{file}` in place:

- Fix errors first, then warnings. Skip advice unless asked.
- Never invent SLA, quality, KPI or pricing numbers: where the review asks for
  a value you don't have, leave the TODO and list the question at the end of
  your reply.
- Keep the file valid: run `open-data-products validate {file}` after editing.
- Keep productID and any ids other files reference.
"""


class ReviewDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        path = Path(kw.get("odps_path") or query).expanduser()
        doc = None
        text = ""
        if path.is_file():
            text = path.read_text()
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                doc = {"_parse_error": str(exc)}
        library: dict[str, list[str]] = {}
        spec: dict[str, list[str]] = {}
        for name, _w, topic, keyword, _why in SECTIONS:
            tm = ks.topic_material(conn, resolver, topic, max_procedures=2)
            books = og.book_citations(conn, og.ODP_BOOKS[:2], keywords=(topic,), per_book=1)
            library[name] = [f"Read: {c.label}" for c in books] + [
                f"{p.name}" + (f" ({p.source.label})" if p.source else "")
                for p in (tm.procedures if tm else [])]
            spec[name] = [c.label for c in ks.doc_citations(conn, og.ODPS_SOURCES,
                                                            keywords=(keyword,), per_source=1)]
        return {"query": query, "path": path, "doc": doc, "text": text, "library": library,
                "spec": spec, "contract_path": kw.get("contract_path"),
                "graph_path": kw.get("graph_path")}


class ReviewPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        path: Path = d["path"]
        doc = d["doc"]
        plan = GenPlan(generator_type=GENERATOR_TYPE,
                       package_name=package_name or f"review-{og.slug(path.stem.replace('.odps', ''))}",
                       domain="data product review", source_query=str(path))
        if not isinstance(doc, dict) or "_parse_error" in doc or "product" not in doc:
            plan.package_metadata = {"readable": False, "path": str(path),
                                     "error": (doc or {}).get("_parse_error") if isinstance(doc, dict)
                                     else "file not found"}
            return plan
        scores, findings = review_product(doc)
        for f in findings:
            f.spec = d["spec"].get(f.section, [])
            f.library = d["library"].get(f.section, [])

        # Tool checks: SDK validate + audit; the referenced contract; the graph.
        files = {"product.odps.yaml": d["text"]}
        cmds: list[tuple[str, Sequence[str]]] = [(path.name, ["validate", "product.odps.yaml"])]
        checks = og.sdk_check(files, cmds)
        contract_file = d["contract_path"]
        if not contract_file:
            url = (((doc["product"].get("contract") or {}).get("default") or {})
                   .get("contractURL"))
            if url and "://" not in url:
                contract_file = str((path.parent / url).resolve())
        if contract_file and Path(contract_file).expanduser().is_file():
            try:
                cdoc = yaml.safe_load(Path(contract_file).expanduser().read_text())
                checks.append(og.check_odcs(cdoc, Path(contract_file).name))
            except yaml.YAMLError as exc:
                checks.append(og.CheckResult("yaml", Path(contract_file).name, False, [str(exc)]))
        elif contract_file:
            findings.append(Finding("contract", "error", f"contract file not found: {contract_file}",
                                    FIXES["contract"], d["spec"].get("contract", [])))
        if d["graph_path"] and Path(d["graph_path"]).expanduser().is_file():
            gtext = Path(d["graph_path"]).expanduser().read_text()
            code, out = og.run_sdk(["odpg-analyze", "graph.yaml", "--json"], _tmpdir_with(
                {"graph.yaml": gtext}))
            if code == 0:
                try:
                    analysis = json.loads(out).get("analysis", {})
                except ValueError:
                    analysis = {}
                for k, v in analysis.items():
                    if v:
                        findings.append(Finding("graph", "advice", f"{k}: {', '.join(map(str, v))}",
                                                "Add the missing edges or nodes in the ODPG graph "
                                                "(governedBy for ungoverned assets)."))
        letter, pct = grade(scores)
        name = (((doc["product"].get("details") or {}).get("en") or {}).get("name")
                or path.stem)
        sdk_failed = [c for c in checks if c.ok is False]
        if sdk_failed and letter in ("A", "B"):
            letter = "C"                      # a file that fails its schema can't grade above C
        review = render_review(name, letter, pct, scores, findings, checks)
        plan.package_metadata = {
            "readable": True, "path": str(path), "grade": letter, "score": round(pct, 3),
            "sections": scores, "n_errors": sum(f.severity == "error" for f in findings),
            "n_warnings": sum(f.severity == "warning" for f in findings),
            "schema_valid": None if all(c.ok is None for c in checks) else not sdk_failed,
        }
        plan.units.append(GenUnit(unit_type="dp_review", name=name, logical_key=og.slug(name),
                                  content_markdown=review, metadata={"grade": letter}))
        plan.files += [
            GenFile(filename="review.md", content=review, purpose="review"),
            GenFile(filename="findings.json", purpose="findings", content=json.dumps(
                {"product": str(path), "grade": letter, "score": round(pct, 3),
                 "sections": scores, "findings": [asdict(f) for f in findings],
                 "checks": [asdict(c) for c in checks]}, indent=2) + "\n"),
            GenFile(filename="_provenance.md", purpose="provenance", content=(
                f"# Provenance: review of {path.name}\n\nSection weights and fixes: "
                "mcp-servers/kb-mcp/odp_review_gen.py. Spec citations are ODPS 4.2 sections in "
                "the catalog; library backing is procedures linked to each section's topic.\n")),
            GenFile(filename="_prompts/apply-fixes.md", purpose="subagent_prompt",
                    content=APPLY_PROMPT.format(file=str(path))),
        ]
        return plan


_TMP_DIRS: list[Any] = []


def _tmpdir_with(files: dict[str, str]) -> Path:
    import tempfile  # noqa: PLC0415
    tmp = tempfile.TemporaryDirectory(prefix="mypub-odp-")
    _TMP_DIRS.append(tmp)                 # cleaned up at interpreter exit
    root = Path(tmp.name)
    for rel, content in files.items():
        (root / rel).write_text(content)
    return root


class ReviewValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        if not meta.get("readable"):
            return [ValidationIssue("", "error", f"can't read an ODPS product at {meta.get('path')}: "
                                                 f"{meta.get('error') or 'no product block'}")]
        return []


def make_dp_review_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=ReviewDecomposer(),
                     planner=ReviewPlanner(), ranking_mode="generation",
                     validator=ReviewValidator(), materializer=ks.TreeMaterializer())
