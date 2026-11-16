"""odp_recipe_gen.py — ``/kb-dp-recipe``: ODPR recipes that build and check one data product (2026-11).

Input: an existing ODPS product file (``odps_path``) or a description, plus
optional paths to its ODCS contract and ODPG graph inside the product's repo.

Output, meant to be copied into the product's repository:

    <package>/
      recipes/<p>-ci.recipe.yaml      ODPR 1.0 Recipe, type ci: validate the product and
                                       graph, explain the product; required validation gate
      recipes/<p>-agent.recipe.yaml   ODPR 1.0 Recipe, type agent: inputs, context policy,
                                       validate + explain (+ render the graph) for an agent,
                                       review by human and agent; the build plan rides in
                                       ``x-mypub-buildPlan``
      handoff/README.md               the ODPR DataProductRecipe sections (draft kind the SDK
                                       can't validate yet) as Markdown: summary, delivery
                                       plan, relationship context, contract plan, agent brief
      .github/workflows/odp-recipe.yml  CI that runs the ci recipe
      _provenance.md, _validation.md (SDK ``recipe validate`` + ``recipe dry-run``)

The delivery plan comes from the KB: procedures for the target platform
(Delta Sharing / Unity Catalog, Snowflake sharing, REST APIs) and for data
products in general. Deterministic.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

import duckdb
import yaml

import kb_scaffold as ks
import odp_gen as og
import odp_profile
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "dp_recipe"

PLATFORM_TOPICS = {
    "databricks": ("Delta Sharing", "Unity Catalog", "Databricks Asset Bundles"),
    "snowflake": ("Secure Data Sharing", "Snowflake"),
    "bigquery": ("BigQuery",),
    "postgres": ("PostgreSQL",),
    "api": ("REST API", "API Design"),
    "generic": ("Data Pipeline",),
}


def read_product(path: Optional[str]) -> Optional[dict[str, Any]]:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("product"), dict) else None


def product_facts(doc: Optional[dict[str, Any]], query: str) -> dict[str, Any]:
    details = ((doc or {}).get("product", {}).get("details") or {})
    en = details.get("en") or next(iter(details.values()), {}) if details else {}
    access = ((doc or {}).get("product", {}).get("dataAccess") or {}).get("default") or {}
    contract = ((doc or {}).get("product", {}).get("contract") or {}).get("default") or {}
    return {
        "id": en.get("productID") or og.slug(query),
        "name": en.get("name") or og.title(og.slug(query)),
        "summary": en.get("valueProposition") or en.get("description") or query,
        "status": en.get("status"),
        "port": access.get("outputPortType"),
        "contract_url": contract.get("contractURL"),
    }


def _recipe(meta_id: str, name: str, desc: str, rtype: str, steps: list[dict[str, Any]],
            *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    recipe: dict[str, Any] = {
        "metadata": {"id": meta_id, "name": {"en": name}, "description": {"en": desc},
                     "tags": ["data-product", rtype]},
        "version": "1.0.0", "type": rtype,
    }
    recipe.update(extra or {})
    recipe["steps"] = steps
    recipe["gates"] = [{"id": "schemas-valid", "type": "validation", "required": True,
                        "description": "The product (and graph) validate against their schemas."}]
    recipe["runPolicy"] = {"stopOnFailure": True}
    return {"schema": odp_profile.ODPR_SCHEMA, "version": "1.0", "kind": "Recipe",
            "recipe": recipe}


def ci_recipe(f: dict[str, Any], product_file: str, graph_file: Optional[str]) -> dict[str, Any]:
    steps = [{"id": "validate-product", "command": "validate", "with": {"document": product_file}}]
    if graph_file:
        steps.append({"id": "validate-graph", "command": "validate",
                      "with": {"document": graph_file}})
    steps.append({"id": "explain-product", "command": "explain",
                  "with": {"document": product_file}})
    doc = _recipe(f"RCP-{og.upper_id(f['id'])}-CI", f"Check {f['name']}",
                  "Validate the product and its graph on every change.", "ci", steps)
    doc["recipe"]["review"] = {"required": False, "mode": "none"}
    return doc


def agent_recipe(f: dict[str, Any], product_file: str, contract_file: Optional[str],
                 graph_file: Optional[str], build_plan: list[str]) -> dict[str, Any]:
    inputs = [{"id": "product", "path": product_file, "kind": "yaml",
               "description": {"en": "ODPS product: the source of truth."}}]
    if contract_file:
        inputs.append({"id": "contract", "path": contract_file, "kind": "yaml",
                       "description": {"en": "ODCS data contract."}})
    if graph_file:
        inputs.append({"id": "graph", "path": graph_file, "kind": "yaml",
                       "description": {"en": "ODPG value graph."}})
    steps = [{"id": "validate-product", "command": "validate", "with": {"document": product_file}},
             {"id": "brief-agent", "command": "explain", "with": {"document": product_file}}]
    outputs: list[dict[str, Any]] = []
    if graph_file:
        steps.append({"id": "render-graph", "command": "odpg.render",
                      "with": {"graph": graph_file, "output": "build/graph.html"}})
        outputs.append({"id": "graph-view", "path": "build/graph.html", "kind": "html"})
    extra: dict[str, Any] = {"inputs": inputs}
    if outputs:
        extra["outputs"] = outputs
    extra["context"] = {"format": "yaml", "fallback": ["toon"]}
    extra["execution"] = {"mode": "none"}
    doc = _recipe(f"RCP-{og.upper_id(f['id'])}-AGENT", f"Build {f['name']}",
                  "Hand the product spec to a builder agent: validate, brief, and review.",
                  "agent", steps, extra=extra)
    doc["recipe"]["review"] = {
        "required": True, "mode": "both",
        "instructions": {"en": "A person approves the delivery plan before the agent changes "
                               "pipelines or access; the agent re-runs the ci recipe after."}}
    doc["recipe"]["x-mypub-buildPlan"] = build_plan
    return doc


def build_plan(f: dict[str, Any], platform: str, procs: Sequence[ks.Procedure]) -> list[str]:
    plan = [
        f"Read the product spec and keep it the source of truth for {f['name']}.",
        "Build or update the pipeline that produces the tables in the data contract.",
    ]
    plan += [f"{p.name} (procedure #{p.procedure_id})" for p in procs[:6]]
    plan += [
        f"Publish through the product's access method ({f['port'] or 'see dataAccess'}).",
        "Wire the SLA and data quality checks named in the product and contract.",
        "Run the ci recipe; fix every validation error before asking for review.",
    ]
    return plan


def handoff(f: dict[str, Any], platform: str, product_file: str, contract_file: Optional[str],
            graph_file: Optional[str], procs: Sequence[ks.Procedure], plan: Sequence[str]) -> str:
    proc_lines = "\n".join(
        f"- **{p.name}.** {ks.first_sentence(p.steps, 240)}"
        + (f" _({p.source.label})_" if p.source else "") for p in procs) or \
        "_The knowledge base has no procedures for this platform yet._"
    plan_lines = "\n".join(f"{i}. {s}" for i, s in enumerate(plan, 1))
    return f"""\
# Builder handoff: {f['name']}

<!-- Sections follow the ODPR 1.0 DataProductRecipe draft. The SDK can't
validate that kind yet, so the handoff is Markdown next to two valid recipes. -->

## Recipe readme
A builder (a developer or an AI agent) delivers **{f['name']}** on {platform}.
Done means: `open-data-products recipe run recipes/{og.slug(f['id'])}-ci.recipe.yaml --execute`
passes, and a person has approved the delivery plan.

## Source product spec
`{product_file}` (ODPS 4.2). Don't restate it here; read it with
`open-data-products explain {product_file}`.

## Product summary
{f['summary']}

## Delivery plan
{plan_lines}

### Procedures from the library
{proc_lines}

## Relationship context
{f"`{graph_file}` (ODPG): read the use cases and objectives this product serves before changing it." if graph_file else "No ODPG graph given. Generate one with `/kb-dp-portfolio` to record which use cases depend on this product."}

## Contract plan
{f"`{contract_file}` (ODCS)." if contract_file else f"Contract: `{f['contract_url'] or 'none referenced'}`. Create one with `/kb-data-contract`."}

## AI agent brief
- Work from the files above only; record assumptions in Open questions.
- Never change access, licensing or personal-data handling without the review step.
- Keep every file valid; run the ci recipe after each change.
- Use ODPV terms for names (`open-data-products odpv-resolve "<term>"`).

## Open questions
- [ ] Who approves the delivery plan?
- [ ] Which environment does the agent deploy to first?
"""


CI_WORKFLOW = """\
name: odp-recipe
on: [push, pull_request]
jobs:
  recipe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "open-data-products>=0.7"
      - run: open-data-products recipe validate {recipe}
      - run: open-data-products recipe run {recipe} --execute
"""


class RecipeDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        doc = read_product(kw.get("odps_path"))
        f = product_facts(doc, query)
        port_platform = og.detect_platform(f" {f.get('port') or ''} ")
        platform = (kw.get("platform")
                    or (port_platform if port_platform != "generic" else og.detect_platform(query))
                    ).lower()
        procs: list[ks.Procedure] = []
        seen: set[int] = set()
        for topic in PLATFORM_TOPICS.get(platform, ()) + ("Data Product",):
            tm = ks.topic_material(conn, resolver, topic, max_procedures=4)
            for p in (tm.procedures if tm else []):
                if p.procedure_id not in seen:
                    seen.add(p.procedure_id)
                    procs.append(p)
        cites = ks.doc_citations(conn, og.ODPR_SOURCES,
                                 keywords=("recipe", "step", "gate", "agent", "review"),
                                 per_source=4)
        return {"query": query, "facts": f, "doc": doc, "odps_path": kw.get("odps_path"),
                "platform": platform, "procedures": procs[:8], "citations": cites,
                "product_file": kw.get("product_file"), "contract_file": kw.get("contract_file"),
                "graph_file": kw.get("graph_file"), "mode": (kw.get("mode") or "both").lower()}


class RecipePlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        f = d["facts"]
        pslug = og.slug(f["id"])
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or f"{pslug}-recipes",
                       domain="data product recipes", source_query=d["query"])
        if d["odps_path"] and d["doc"] is None:
            plan.notes.append(f"couldn't read an ODPS product at {d['odps_path']}; "
                              "used the description instead")
        product_file = d["product_file"] or (
            f"products/{Path(d['odps_path']).name}" if d["doc"] is not None
            else f"products/{pslug}.odps.yaml")
        contract_file, graph_file = d["contract_file"], d["graph_file"]
        steps = build_plan(f, d["platform"], d["procedures"])
        files: dict[str, str] = {}
        ci_path = f"recipes/{pslug}-ci.recipe.yaml"
        agent_path = f"recipes/{pslug}-agent.recipe.yaml"
        if d["mode"] in ("ci", "both"):
            files[ci_path] = og.dump_yaml(ci_recipe(f, product_file, graph_file),
                                          "ODPR recipe (ci). Run from the product repo root.")
            files[".github/workflows/odp-recipe.yml"] = CI_WORKFLOW.format(recipe=ci_path)
        if d["mode"] in ("agent", "both"):
            files[agent_path] = og.dump_yaml(
                agent_recipe(f, product_file, contract_file, graph_file, steps),
                "ODPR recipe (agent). x-mypub-buildPlan lists what the builder does.")
        files["handoff/README.md"] = handoff(f, d["platform"], product_file, contract_file,
                                             graph_file, d["procedures"], steps)

        # Check the recipes against the SDK with the real (or a stand-in) product in place.
        fixture = dict(files)
        if d["doc"] is not None:
            fixture[product_file] = Path(d["odps_path"]).expanduser().read_text()
        else:
            ctx = SimpleNamespace(project_name=f["name"], slug=pslug, pkg=og.snake(pslug),
                                  purpose="", ctx_summary="", request=d["query"],
                                  data={"platform": d["platform"]})
            fixture[product_file] = odp_profile.odps_product(ctx)
            files[product_file] = fixture[product_file]
            plan.notes.append(f"no ODPS file given; wrote a draft at {product_file}")
        if graph_file:
            fixture.setdefault(graph_file, _stub_graph(pslug))
        cmds: list[tuple[str, Sequence[str]]] = []
        for path in (ci_path, agent_path):
            if path in files:
                cmds += [(path, ["recipe", "validate", path]),
                         (f"{path} (dry run)", ["recipe", "dry-run", path])]
        checks = og.sdk_check(fixture, cmds)

        files["_provenance.md"] = (
            f"# Provenance: {f['name']} recipes\n\n"
            + ks.render_citations(d["citations"], "ODPR spec sections")
            + "\n" + ks.render_citations([p.source for p in d["procedures"] if p.source],
                                         "Procedures behind the delivery plan"))
        files["_validation.md"] = og.render_checks(
            f"Recipe checks: {f['name']}", checks,
            "The DataProductRecipe handoff (handoff/README.md) is Markdown: ODPR 1.0 is a "
            "draft and SDK 0.7 doesn't validate that kind.")
        plan.package_metadata = {
            "product_id": f["id"], "platform": d["platform"], "mode": d["mode"],
            "product_file": product_file, "n_procedures": len(d["procedures"]),
            "check_errors": og.check_errors(checks),
            "checks_run": sum(1 for c in checks if c.ok is not None),
        }
        plan.units.append(GenUnit(
            unit_type="dp_recipe", name=f["name"], logical_key=pslug,
            content_markdown=files["handoff/README.md"],
            sources=[(c.source_type, c.source_id, 1.0, 1.0, None) for c in d["citations"]]
            + [("procedure", p.procedure_id, 1.0, 1.0, None) for p in d["procedures"]]))
        for path, content in files.items():
            plan.files.append(GenFile(filename=path, content=content,
                                      purpose=path.split("/")[0]))
        return plan


def _stub_graph(pslug: str) -> str:
    """A minimal valid graph so ``recipe dry-run`` can check a graph path we can't read."""
    uid = og.upper_id(pslug)
    return og.dump_yaml({"schema": odp_profile.ODPG_SCHEMA, "version": "1.0", "kind": "Graph",
                         "graph": {"metadata": {"id": f"GRAPH-{uid}", "name": {"en": pslug}},
                                   "nodes": [{"id": f"DP-{uid}", "type": "DataProduct"}],
                                   "edges": []}})


class RecipeValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        out = [ValidationIssue("", "error", m) for m in meta.get("check_errors", [])]
        if not meta.get("checks_run"):
            out.append(ValidationIssue("", "warning", "SDK not installed; recipes not checked"))
        if not meta.get("n_procedures"):
            out.append(ValidationIssue("", "warning",
                                       "no KB procedures for this platform; delivery plan is generic"))
        return out


def make_dp_recipe_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=RecipeDecomposer(),
                     planner=RecipePlanner(), ranking_mode="generation",
                     validator=RecipeValidator(), materializer=ks.TreeMaterializer())
