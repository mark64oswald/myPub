"""odp_profile.py — file renderers for the Bootstrap ``data-product`` profile (2026-11).

A data-product project is a set of Open Data Product (ODP) spec files,
not code: an ODPS 4.2 product, ODPC catalog fragments (use case, business
objective with its KPI, product reference), an ODPG graph that connects
them, an ODPR recipe that validates and builds them, an ODCS 3.1 data
contract, and a handoff folder modelled on the ODPR DataProductRecipe
sections. Every YAML file ships valid against the schemas bundled with
``open-data-products`` 0.7.0 (checked when this was written), with draft
values a sub-agent then fills from the request.

The renderers take bootstrap_profiles.ProfileContext; this module only
reads its attributes, so it doesn't import bootstrap_profiles.

Platform: ``prepare`` looks for Databricks, Snowflake or a REST API in the
request and shapes the ODPS ``dataAccess`` block for it.
"""
from __future__ import annotations

import re
from typing import Any

MARKER = "TODO: populate from stack context"

ODPS_SCHEMA = "https://opendataproducts.org/v4.2/schema/odps.json"
ODPC_SCHEMA = "https://opendataproducts.org/odpc-v1.0/schema/odpc.yaml"
ODPG_SCHEMA = "https://opendataproducts.org/odpg-v1.0/schema/odpg.yaml"
ODPR_SCHEMA = "https://opendataproducts.org/odpr-v1.0/schema/odpr.yaml"
ODCS_VERSION = "v3.1.0"
SDK_REQUIREMENT = "open-data-products>=0.7"

PLATFORMS: dict[str, dict[str, str]] = {
    "databricks": {
        "label": "Databricks (Unity Catalog table, shared with Delta Sharing)",
        "port": "Delta Sharing",
        "auth": "Token",
        "server_type": "databricks",
        "docs": "https://docs.databricks.com/en/delta-sharing/index.html",
    },
    "snowflake": {
        "label": "Snowflake (secure share)",
        "port": "Snowflake secure share",
        "auth": "OAuth",
        "server_type": "snowflake",
        "docs": "https://docs.snowflake.com/en/user-guide/data-sharing-intro",
    },
    "api": {
        "label": "REST API",
        "port": "API",
        "auth": "OAuth",
        "server_type": "api",
        "docs": "",
    },
    "generic": {
        "label": "SQL table or file export (platform not named in the request)",
        "port": "file",
        "auth": "Token",
        "server_type": "local",
        "docs": "",
    },
}

_PLATFORM_KEYWORDS = [
    ("databricks", ("databricks", "unity catalog", "delta sharing", "lakeflow", "delta lake")),
    ("snowflake", ("snowflake",)),
    ("api", (" rest api", " api ", "endpoint", "graphql")),
]


def detect_platform(request: str) -> str:
    padded = f" {(request or '').lower()} "
    for name, words in _PLATFORM_KEYWORDS:
        if any(w in padded for w in words):
            return name
    return "generic"


def prepare(request: str, options: dict) -> tuple[dict, list[str]]:
    platform = (options or {}).get("platform") or detect_platform(request)
    if platform not in PLATFORMS:
        platform = "generic"
    notes = [f"data-product: dataAccess shaped for {PLATFORMS[platform]['label']}"]
    data = {"platform": platform,
            "metadata": {"platform": platform, "odps": "4.2", "odpc": "1.0",
                         "odpg": "1.0", "odpr": "1.0", "odcs": ODCS_VERSION}}
    return data, notes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def title(project_name: str) -> str:
    words = re.split(r"[-_\s]+", project_name or "data product")
    return " ".join(w if w.isupper() else w.capitalize() for w in words if w)


def _q(text: str) -> str:
    """A YAML double-quoted scalar."""
    return '"' + " ".join(str(text).split()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def ids(c: Any) -> dict[str, str]:
    up = re.sub(r"[^A-Z0-9]+", "-", c.slug.upper()).strip("-")
    return {
        "product": c.slug,
        "use_case": f"{c.slug}-use-case",
        "objective": f"{c.slug}-objective",
        "kpi": f"{c.slug}-kpi",
        "graph": f"GRAPH-{up}",
        "node_dp": f"DP-{up}",
        "node_uc": f"UC-{up}",
        "node_obj": f"OBJ-{up}",
        "node_kpi": f"KPI-{up}",
        "recipe": f"RCP-{up}-VALIDATE",
        "catalog": f"CAT-{up}",
    }


def _platform(c: Any) -> dict[str, str]:
    return PLATFORMS[(c.data or {}).get("platform") or "generic"]


def paths(c: Any) -> dict[str, str]:
    s = c.slug
    return {
        "odps": f"products/{s}.odps.yaml",
        "odcs": f"contracts/{s}.odcs.yaml",
        "use_case": f"catalog/fragments/use_case_{s}.yaml",
        "objective": f"catalog/fragments/business_objective_{s}.yaml",
        "product_ref": f"catalog/fragments/product_reference_{s}.yaml",
        "catalog": f"catalog/{s}.odpc.yaml",
        "graph": f"graph/{s}.odpg.yaml",
        "recipe": "recipes/validate.recipe.yaml",
    }


# The project's files: (path template, purpose). {slug} is expanded by Bootstrap.
FILES: list[tuple[str, str]] = [
    ("README.md", "project overview: the files, how they connect, how to validate"),
    ("CLAUDE.md", "rules Claude follows when editing the spec files"),
    ("products/{slug}.odps.yaml", "ODPS 4.2 product: details, strategy, contract, SLA, "
                                  "data quality, access, license, data holder"),
    ("contracts/{slug}.odcs.yaml", "ODCS 3.1 data contract the ODPS contract points at"),
    ("catalog/fragments/use_case_{slug}.yaml", "ODPC use case: who needs the data and why"),
    ("catalog/fragments/business_objective_{slug}.yaml",
     "ODPC business objective with its KPI"),
    ("catalog/fragments/product_reference_{slug}.yaml",
     "ODPC product reference pointing at the ODPS file"),
    ("graph/{slug}.odpg.yaml", "ODPG graph: use case, objective, KPI and product, connected"),
    ("recipes/validate.recipe.yaml", "ODPR recipe: validate every file, then build the catalog"),
    ("handoff/README.md", "builder handoff modelled on the ODPR DataProductRecipe sections"),
    ("handoff/open-questions.md", "decisions still open before the product can ship"),
    ("requirements.txt", "the open-data-products SDK"),
    ("Makefile", "validate, catalog and recipe targets"),
    (".github/workflows/validate.yml", "CI: validate the spec files on every push"),
    (".gitignore", "ignore the build output and venvs"),
]

REQUIRED_FILES = ["products/{slug}.odps.yaml", "catalog/fragments/use_case_{slug}.yaml",
                  "graph/{slug}.odpg.yaml", "recipes/validate.recipe.yaml"]

GITIGNORE = "build/\n.venv/\n__pycache__/\n.DS_Store\n"


# ---------------------------------------------------------------------------
# ODPS
# ---------------------------------------------------------------------------

def odps_product(c: Any) -> str:
    i, p, pf = ids(c), paths(c), _platform(c)
    name = title(c.project_name)
    docs = f"\n      documentationURL: {pf['docs']}" if pf["docs"] else ""
    return f"""\
# ODPS 4.2 product for {name}.
# {MARKER}: replace the draft values (KPI targets, SLA and quality objectives,
# access URL) with real ones; keep every key the schema needs.
# Check: open-data-products validate {p['odps']}
schema: {ODPS_SCHEMA}
version: "4.2"
product:
  details:
    en:
      productID: {i['product']}
      name: {_q(name)}
      visibility: organisation
      status: draft
      type: dataset
      productVersion: 0.1.0
      governanceProfile: structured
      portfolioPriority: medium
      valueProposition: {_q(_first_sentence(c.request))}
      description: {_q(c.request)}
      tags: [{c.slug}]
  productStrategy:
    status: Planned
    objectives:
      - en: {_q("Serve the use case in " + p['use_case'])}
    contributesToKPI:
      id: {i['kpi']}
      name: {_q(name + " objective KPI")}
      description: The business KPI this product moves.
      unit: percentage
      target: 0
      direction: increase
      timeframe: 12 months
      frequency: monthly
      owner: Business owner
      calculation: Defined by the business objective KPI in the ODPC fragment.
    productKPIs:
      - id: {i['product']}-active-consumers
        name: Active consuming teams
        description: Teams that use the product, a sign it is adopted.
        unit: count
        target: 3
        direction: increase
        timeframe: 6 months
        frequency: monthly
        owner: Data product owner
        calculation: Distinct teams that queried the product in the last 30 days.
  contract:
    default:
      id: {i['product']}-contract
      type: ODCS
      contractVersion: 0.1.0
      contractURL: ../{p['odcs']}
  SLA:
    declarative:
      default:
        name:
          en: Standard
        dimensions:
          - dimension: uptime
            objective: 99
            unit: percent
          - dimension: updateFrequency
            objective: 1
            unit: days
          - dimension: timeToRepair
            objective: 2
            unit: days
  dataQuality:
    declarative:
      default:
        displaytitle:
          en: Baseline quality targets
        dimensions:
          - dimension: completeness
            objective: 95
            unit: percentage
          - dimension: timeliness
            objective: 95
            unit: percentage
          - dimension: uniqueness
            objective: 100
            unit: percentage
  dataAccess:
    default:
      name:
        en: {_q(pf['label'])}
      outputPortType: {pf['port']}
      authenticationMethod: {pf['auth']}{docs}
  license:
    scope:
      definition: Internal use within the organisation; no redistribution outside it.
      permanent: false
      exclusive: false
      rights:
        - Reproduction
        - Display
    governance:
      ownership: The data holder owns the product and its data.
      audit: Access is logged and can be audited by the data holder.
      confidentiality: Consumers keep the data inside the organisation.
    termination:
      noticePeriod: 30
      terminationConditions: Either side may end access with the notice period.
  # dataHolder is left out on purpose: the 4.2 schema wants it keyed by
  # language (dataHolder.en.email), but SDK 0.7's checker reads a flat
  # dataHolder.email, so no shape passes both. Name the holder in
  # handoff/README.md until the SDK is fixed.
"""


def odcs_contract(c: Any) -> str:
    i, pf = ids(c), _platform(c)
    table = c.pkg
    return f"""\
# ODCS {ODCS_VERSION} data contract for {title(c.project_name)}.
# {MARKER}: define the real schema, quality rules and server details.
# The ODPS product's contract.default points here.
apiVersion: {ODCS_VERSION}
kind: DataContract
id: {i['product']}-contract
name: {c.slug}
version: 0.1.0
status: draft
description:
  purpose: {_q(_first_sentence(c.request))}
  usage: Read-only analytical use by the teams named in the ODPC use case.
schema:
  - name: {table}
    physicalType: table
    description: Main table of the data product.
    properties:
      - name: id
        logicalType: string
        primaryKey: true
        required: true
        description: Unique record identifier.
      - name: updated_at
        logicalType: timestamp
        required: true
        description: When the record last changed.
servers:
  - server: production
    type: {pf['server_type']}
"""


# ---------------------------------------------------------------------------
# ODPC fragments
# ---------------------------------------------------------------------------

def odpc_use_case(c: Any) -> str:
    i = ids(c)
    return f"""\
# ODPC use case. {MARKER}: name the real stakeholders and decision.
useCase:
  id: {i['use_case']}
  name:
    en: {_q(title(c.project_name) + " use case")}
  description:
    en: {_q(c.request)}
  stakeholders:
    - data product owner
    - consuming analysts
  decision:
    en: Which decision this data supports.
  expectedOutcome:
    en: The measurable change the use case should produce.
  dataNeeds:
    summary:
      en: {_q("The " + title(c.project_name) + " data product.")}
    items:
      - {i['product']}
  status: draft
  priority: medium
"""


def odpc_objective(c: Any) -> str:
    i = ids(c)
    return f"""\
# ODPC business objective with its KPI. {MARKER}: real target and dates.
businessObjective:
  id: {i['objective']}
  name:
    en: {_q(title(c.project_name) + " objective")}
  description:
    en: The business outcome the use case contributes to.
  owner:
    role: Business owner
  kpis:
    - id: {i['kpi']}
      name:
        en: {_q(title(c.project_name) + " objective KPI")}
      unit: percent
      target:
        value: 0
  status: draft
  priority: medium
"""


def odpc_product_ref(c: Any) -> str:
    i, p = ids(c), paths(c)
    return f"""\
# ODPC product reference: points at the ODPS file instead of copying it.
productReference:
  id: {i['product']}
  productID: {i['product']}
  productVersion: 0.1.0
  name:
    en: {_q(title(c.project_name))}
  description:
    en: {_q(_first_sentence(c.request))}
  status: draft
  type: dataset
  portfolioPriority: medium
  productModel:
    standard: ODPS
    version: "4.2"
    format: yaml
    $ref: {p['odps']}
"""


# ---------------------------------------------------------------------------
# ODPG, ODPR
# ---------------------------------------------------------------------------

def odpg_graph(c: Any) -> str:
    i, p = ids(c), paths(c)
    name = title(c.project_name)
    return f"""\
# ODPG graph: how the product creates value. Edge types are ODPV
# relationship terms (check with: open-data-products odpv-relationship).
# {MARKER}: add the other products, APIs, policies or agents it touches.
schema: {ODPG_SCHEMA}
version: "1.0"
kind: Graph
graph:
  metadata:
    id: {i['graph']}
    name:
      en: {_q(name + " value graph")}
    description:
      en: Use case, objective, KPI and data product for {name}.
  nodes:
    - id: {i['node_uc']}
      type: UseCase
      $ref: ../{p['use_case']}
    - id: {i['node_obj']}
      type: BusinessObjective
      $ref: ../{p['objective']}
    - id: {i['node_kpi']}
      type: KPI
      $ref: ../{p['objective']}#{i['kpi']}
    - id: {i['node_dp']}
      type: DataProduct
      $ref: ../{p['odps']}
  edges:
    - from: {i['node_uc']}
      to: {i['node_dp']}
      type: uses
      confidence: high
    - from: {i['node_uc']}
      to: {i['node_obj']}
      type: supports
      confidence: high
    - from: {i['node_dp']}
      to: {i['node_obj']}
      type: contributesTo
      confidence: medium
    - from: {i['node_kpi']}
      to: {i['node_obj']}
      type: measures
      confidence: high
"""


def odpr_recipe(c: Any) -> str:
    i, p = ids(c), paths(c)
    steps = "\n".join(
        f"""    - id: validate-{key.replace('_', '-')}
      command: validate
      with:
        document: {p[key]}"""
        for key in ("odps", "graph"))
    return f"""\
# ODPR recipe: validate the spec files, then build the ODPC catalog.
# Run: open-data-products recipe validate {p['recipe']}
#      open-data-products recipe dry-run {p['recipe']}
schema: {ODPR_SCHEMA}
version: "1.0"
kind: Recipe
recipe:
  metadata:
    id: {i['recipe']}
    name:
      en: {_q("Validate " + title(c.project_name))}
    description:
      en: Validate the ODPS product and ODPG graph, then build the ODPC catalog.
    tags:
      - validation
  version: "1.0.0"
  type: ci
  steps:
{steps}
    - id: build-catalog
      command: odpc.build
      with:
        input: catalog/fragments
        output: {p['catalog']}
        id: {i['catalog']}
        validate: true
  gates:
    - id: schemas-valid
      type: validation
      required: true
      description: Every file validates against its schema.
  review:
    required: true
    mode: human
    instructions: The data product owner reviews SLA, quality and access before release.
  runPolicy:
    stopOnFailure: true
"""


# ---------------------------------------------------------------------------
# Handoff, docs, tooling
# ---------------------------------------------------------------------------

def handoff_readme(c: Any) -> str:
    p = paths(c)
    return f"""\
# Builder handoff: {title(c.project_name)}

<!-- {MARKER}: fill each section from the spec files and the request. -->

This folder follows the sections of an ODPR `DataProductRecipe` handoff
(ODPR 1.0 is a draft, and the SDK 0.7 schema doesn't validate that kind
yet, so it is kept as Markdown).

## Recipe readme
What the builder (a developer or an AI agent) is asked to deliver, and
how to check it: `make validate` must pass.

## Source product spec
`{p['odps']}` (ODPS 4.2) is the source of truth. Don't restate it here.

## Product summary
{c.request}

## Delivery plan
1. Build the pipeline that produces the table in `{p['odcs']}`.
2. Publish it through the access method in the ODPS `dataAccess` block.
3. Wire the SLA and data quality checks named in the ODPS file.
4. Update the KPI target in `{p['objective']}` once a baseline exists.

## Relationship context
`{p['graph']}` (ODPG): the use case uses this product, and the product
contributes to the business objective that the KPI measures.

## Contract plan
`{p['odcs']}` (ODCS {ODCS_VERSION}).

## AI agent brief
Work only from the files listed above. Keep every file valid
(`make validate`), use ODPV terms for names (`open-data-products
odpv-resolve "<term>"`), and record anything you had to assume in
`open-questions.md`.

## Open questions
See `open-questions.md`.
"""


def open_questions(c: Any) -> str:
    return f"""\
# Open questions: {title(c.project_name)}

<!-- {MARKER}: add the questions the request leaves open. -->

- [ ] Who is the data product owner, and who are the first consumers?
- [ ] What is the KPI baseline, and what target and date should the objective carry?
- [ ] Which SLA and data quality objectives are realistic for the first release?
- [ ] Which access method and authentication will consumers use?
- [ ] Which license terms apply, and is the product ever shared outside the organisation?
- [ ] Is the data sensitive (personal, health, financial)? If so, what policies govern it?
"""


def claude_md(c: Any) -> str:
    p = paths(c)
    return f"""\
# {title(c.project_name)}: data product specification

This repository defines one data product with the Open Data Product (ODP)
standards. The files are the product's contract with its consumers, so
keep them valid and consistent with each other.

## Files

| File | Standard | Role |
|---|---|---|
| `{p['odps']}` | ODPS 4.2 | The product: details, strategy, contract, SLA, quality, access, license |
| `{p['odcs']}` | ODCS {ODCS_VERSION} | The data contract the ODPS contract points at |
| `catalog/fragments/*.yaml` | ODPC 1.0 | Use case, business objective + KPI, product reference |
| `{p['catalog']}` | ODPC 1.0 | Built from the fragments by `make catalog`; don't edit by hand |
| `{p['graph']}` | ODPG 1.0 | How use case, objective, KPI and product connect |
| `{p['recipe']}` | ODPR 1.0 | Validate + build steps |
| `handoff/` | ODPR DataProductRecipe sections | What a builder needs |

## Rules

- Run `make validate` after every change; never leave a file invalid.
- Change a fact in one place: the ODPS file is the source of truth for the
  product, the fragments for the portfolio objects, the graph for the links.
- IDs are stable: renaming `productID` or a node id means updating every
  `$ref` and edge that uses it.
- Use ODPV terms (`open-data-products odpv-resolve "<term>"`) for names and
  ODPG edge types (`open-data-products odpv-relationship <from> <type> <to>`).
- Don't invent SLA, quality or KPI numbers: leave the draft value and add a
  question to `handoff/open-questions.md`.
- No real records in examples; describe columns, not rows.

## Context from the myPub knowledge base

{c.ctx_summary}
"""


def readme(c: Any) -> str:
    p, pf = paths(c), _platform(c)
    return f"""\
# {title(c.project_name)}

{c.request}

A data product described with the Open Data Product standards
(opendataproducts.org): ODPS 4.2 for the product, ODPC 1.0 for its use case
and objective, ODPG 1.0 for how they connect, ODPR 1.0 for the repeatable
validate-and-build steps, and ODCS {ODCS_VERSION} for the data contract.
Access is shaped for {pf['label']}.

```
use case ──uses──▶ data product ──contributesTo──▶ business objective ◀──measures── KPI
     └──────────────────────supports──────────────────────┘
```

| Path | What it is |
|---|---|
| `{p['odps']}` | ODPS product (source of truth) |
| `{p['odcs']}` | ODCS data contract |
| `catalog/fragments/` | ODPC use case, objective + KPI, product reference |
| `{p['graph']}` | ODPG value graph |
| `{p['recipe']}` | ODPR validate + build recipe |
| `handoff/` | What a builder needs, and the open questions |

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
make validate        # every file against its schema
make catalog         # build {p['catalog']} from the fragments
make recipe          # validate and dry-run the ODPR recipe
```

`open-data-products serve` runs the SDK's MCP server, so an agent can
validate and explain these files too.

## Context from the myPub knowledge base

{c.ctx_summary}
"""


def requirements(c: Any) -> str:
    return f"{SDK_REQUIREMENT}\n"


def makefile(c: Any) -> str:
    p = paths(c)
    odp = "open-data-products"
    return f"""\
ODP ?= {odp}

.PHONY: validate catalog recipe all

# Catalog fragments aren't documents on their own; `catalog` checks them by
# building and validating the ODPC catalog.
validate:
\t$(ODP) validate {p['odps']}
\t$(ODP) validate {p['graph']}
\t$(ODP) recipe validate {p['recipe']}

catalog:
\t$(ODP) odpc-build catalog/fragments -o {p['catalog']} --id {ids(c)['catalog']}
\t$(ODP) validate {p['catalog']}

recipe:
\t$(ODP) recipe dry-run {p['recipe']}

all: validate catalog recipe
"""


def ci_workflow(c: Any) -> str:
    return """\
name: validate
on: [push, pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: make validate catalog
"""


RENDERERS = {
    "README.md": readme,
    "CLAUDE.md": claude_md,
    "products/{slug}.odps.yaml": odps_product,
    "contracts/{slug}.odcs.yaml": odcs_contract,
    "catalog/fragments/use_case_{slug}.yaml": odpc_use_case,
    "catalog/fragments/business_objective_{slug}.yaml": odpc_objective,
    "catalog/fragments/product_reference_{slug}.yaml": odpc_product_ref,
    "graph/{slug}.odpg.yaml": odpg_graph,
    "recipes/validate.recipe.yaml": odpr_recipe,
    "handoff/README.md": handoff_readme,
    "handoff/open-questions.md": open_questions,
    "requirements.txt": requirements,
    "Makefile": makefile,
    ".github/workflows/validate.yml": ci_workflow,
}
