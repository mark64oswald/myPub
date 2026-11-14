# Bootstrap profiles and Guided Conversation

Two generators added in November 2026. This guide starts with what each is
for, then how it works, then worked examples with the commands to run and
what they write.

## 1. The big picture

Both generators turn knowledge into something you **run**, not something you
read. They differ in what gets run.

| | Bootstrap profiles | Guided Conversation |
|---|---|---|
| Question it answers | "Give me a working project of this kind" | "Walk me from this goal to a finished output" |
| What you get | A project folder: code, config, tests, Claude Code settings | One Agent Skill (plus `conversation.json` for other hosts) |
| Who runs it next | You or sub-agents fill the `TODO` files; `pytest` / `databricks bundle validate` check it | Claude, in a chat, one question at a time |
| What the KB adds | Concepts and doc sections that ground the stack | Prerequisites, alternatives and procedures that become the steps |
| Command | `/mypub:kb-bootstrap` | `/mypub:kb-guided-conversation` |

How they relate to the rest of the generator family:

```
                    myPub catalog (books + live docs)
         concepts ── REQUIRES ── CONTRASTS_WITH ── procedures
                 │                                     │
     ┌───────────┴───────────┐             ┌───────────┴───────────┐
     │   Project Bootstrap   │             │  Guided Conversation  │
     │  stack  (python, …)   │             │  frame → assess →     │
     │  + profile (category) │             │  decide → work →      │
     │                       │             │  produce → review     │
     └───────────┬───────────┘             └───────────┬───────────┘
                 │                                     │
        project folder                         Agent Skill folder
     (code, config, tests,                (SKILL.md + references/,
      .claude/ settings,                    conversation.json)
      sub-agent prompts)
                 │                                     │
     fill TODOs, run tests                   /guide-… in Claude Code
```

## 2. Bootstrap profiles

### Why

Plain Bootstrap picks a **language** (python, rust, …) and writes a generic
skeleton. Most real projects are a **kind** of project first: a Databricks
bundle, an app that needs synthetic healthcare data, an agent on Lexa. Each
kind has a fixed shape that a language skeleton doesn't know. A profile is
that shape.

### What a profile is

A profile is a layer on top of a stack:

```
  request ──► detect profile ──► detect / take stack ──► compose files
   "Databricks pipeline …"   databricks            python?      profile files win
                                                                over stack files
                                    │
                                    ▼
             profile.prepare()  (Lexa only: read the installed plugin)
                                    │
                                    ▼
      render files ──► write package ──► _sub_agent_prompts/ for every TODO file
```

| Profile | Built on | What it writes | Detected from |
|---|---|---|---|
| `databricks` | its own layout | Declarative Automation Bundle: `databricks.yml`, Lakeflow pipeline + job, wheel package, offline unit tests | Databricks, Lakeflow, DLT, asset bundle, Unity Catalog |
| `healthsim` | python | Project that pulls synthetic test data from HealthSim: plugin settings, a `/<name>-test-data` skill, a cohort reader for `~/.healthsim/cohorts.duckdb` | HealthSim, synthetic patients/members/claims |
| `lexa` | none, or the `stack` you pass | Custom Claude Code agent on the Lexa plugin: settings, `CLAUDE.md` with Lexa's skills, tools and guard rails, a workflow skill, eval prompts | Lexa |
| `claude-plugin`, `mcp-server`, `mcp-app` | routed | Handed to `/kb-plugin-scaffold` or `/kb-mcp-server` | plugin, MCP server, MCP App |

Pass `profile=<name>` to choose one, or `profile=none` to switch detection off.

Every package also gets `_build_plan.md` and one prompt per file in
`_sub_agent_prompts/`. Files that still need request-specific code carry the
marker `TODO: populate from stack context`; `bootstrap_runtime` lists them for
sub-agent dispatch and then runs the stack's checks.

### Example 1: Databricks bundle

```
/mypub:kb-bootstrap Databricks pipeline that loads member claims into Unity Catalog, project name claims-etl
```

```
claims-etl/
├── databricks.yml                         bundle: dev + prod targets, catalog/schema vars
├── resources/
│   ├── claims_etl_etl.pipeline.yml        serverless Lakeflow pipeline
│   └── claims_etl_job.job.yml             refresh pipeline, then run the wheel
├── src/
│   ├── claims_etl/{__init__,main,logic}.py
│   └── claims_etl_etl/transformations/
│       ├── bronze.py                      raw ingest
│       └── silver.py                      cleaned + expectations
├── tests/test_logic.py                    runs offline: no Spark, no workspace
├── pyproject.toml, README.md, .gitignore
└── _build_plan.md, _sub_agent_prompts/   12 prompts
```

`silver.py`, as generated:

```python
from pyspark import pipelines as dp
from pyspark.sql import functions as F


@dp.table(comment="Cleaned records for claims-etl")
@dp.expect_or_drop("valid_key", "pickup_zip IS NOT NULL")
def claims_etl_silver():
    # TODO: populate from stack context: apply the request's cleaning rules (see src/claims_etl/logic.py).
    return spark.read.table("claims_etl_bronze").withColumn("loaded_at", F.current_timestamp())
```

The bronze table reads `samples.nyctaxi.trips`, as Databricks' own
`default-python` template does, so the bundle deploys before you've written
anything. The `TODO`s say where your claims source and rules go.

To check it: `uv sync && uv run pytest -q`, then `databricks bundle validate`
if the CLI is installed (`bootstrap_runtime` skips that step if it isn't).

### Example 2: HealthSim test data

```
/mypub:kb-bootstrap Claims adjudication service tested with HealthSim cohorts, project name claims-svc
```

```
claims-svc/
├── .claude/
│   ├── settings.json                      marketplace mark64oswald/healthsim-workspace,
│   │                                      enabledPlugins: healthsim@healthsim
│   └── skills/claims-svc-test-data/SKILL.md
├── CLAUDE.md                              synthetic-only rules, HealthSim products
├── src/{main,healthsim_cohorts}.py        cohort reader (DuckDB)
├── tests/{test_smoke,test_healthsim_cohorts}.py
└── pyproject.toml, README.md, …
```

The generated skill (excerpt):

```markdown
---
name: claims-svc-test-data
description: "Build or refresh claims-svc's synthetic test cohorts with HealthSim."
disable-model-invocation: true
argument-hint: "[which cohorts]"
---
## Steps
1. Read `tests/` and `src/` to see which entity types and formats the tests expect.
2. For each cohort, ask HealthSim (its `healthsim` router skill) to generate it…
3. Save each one with the `save_cohort` tool as `claims_svc-<purpose>`.
4. Check with `list_cohorts` and `get_summary`, then run the tests.
```

In the new project you type `/claims-svc-test-data`, HealthSim builds the
cohorts, and `src/healthsim_cohorts.py` loads them in tests.

### Example 3: Lexa agent

```
/mypub:kb-bootstrap Variant interpretation tutor built on Lexa that looks up ClinVar, project name variant-tutor
```

```
variant-tutor/
├── .claude/
│   ├── settings.json                      enabledPlugins: lexa-bioscience@lexa
│   ├── lexa-capabilities.json             all 37 skills + 124 tools, as read from the plugin
│   └── skills/variant-tutor-workflow/SKILL.md
├── CLAUDE.md                              chosen Lexa skills + tools, guard rails
├── evals/prompts.md                       behaviour + guard-rail checks
├── DISCLAIMER.md, README.md, .gitignore
```

**Where the Lexa knowledge comes from.** Lexa isn't indexed in the catalog
and doesn't need to be. When the package is generated, the profile reads the
**installed** `lexa-bioscience` plugin: its skills (`skills/*/SKILL.md`) and
every MCP tool with its server and parameters
(`runtime/operation-contract.json`). Nothing is started. It looks in
`plugin_path` if you pass one, then `$LEXA_PLUGIN_ROOT`, then Claude Code's
`~/.claude/plugins`. It then picks the skills that fit the request and the
servers those skills own:

| Request | Skills picked | MCP servers |
|---|---|---|
| Variant interpretation tutor … ClinVar | education, education-routing, genomics (+ education-domain-rules, provenance) | learner, clinvar, ensembl, ncbi |
| Lexa agent that screens drug targets … ChEMBL | drug-discovery (+ the two always-on skills) | chembl |
| Single-cell RNA-seq analysis assistant | single-cell (+ the two) | none named; see the inventory |

`CLAUDE.md` (excerpt):

```markdown
### Lexa skills for this agent
| Skill | Use it for |
|---|---|
| `/lexa-bioscience:education` | Teach bioscience concepts using layered explanations. |
| `/lexa-bioscience:genomics` | Variant annotation, pathogenicity classification, and genome-level analysis. |
| `/lexa-bioscience:provenance` | Track and record the full provenance of bioinformatics analyses… |

### Lexa MCP tools for this agent
| MCP server | Tools |
|---|---|
| `clinvar-mcp` | `batch_classify`, `get_clinical_significance`, `get_condition_variants`, `search_variant` |
| `learner-mcp` | `check_question`, `consent_status`, … `suggest_next` |
```

**How the MCP servers get wired in.** The project doesn't copy Lexa's
servers. `.claude/settings.json` enables `lexa-bioscience@lexa`, and an
enabled plugin brings its `.mcp.json` with it, so Claude Code starts all 15
Lexa servers in the new project. Run `/mcp` there to confirm. Lexa's repo is
private for now, so the README explains how to point the marketplace at a
local checkout instead.

If no plugin is installed where the generator runs, it uses a built-in list
of skill and server names, says so in the notes and in `CLAUDE.md`, and
leaves the tool choice to a sub-agent.

## 3. Guided Conversation

### Why

Many goals aren't "write me X" but "help me get to X": a design doc, a
migration plan, an ADR, a runbook. A good colleague would check what you
already know, put the real alternatives in front of you, walk through the
known procedures, and then help assemble the result. The KB already holds
those parts as edges: **REQUIRES** (what you need to know first),
**CONTRASTS_WITH** (alternatives) and **procedures**. Guided Conversation
turns them into a scripted chat.

### How it works

```
goal + output ("a design doc")
   │
   ├─ output type ── document | plan | decision | code | checklist
   ├─ topics ─────── concepts named in the goal (longest match, generic words dropped)
   │
   ├─ REQUIRES walk (topic-gated) ─────► assess steps   "Check: Domain Events"
   ├─ CONTRASTS_WITH (same kind only) ─► decide step    "Choose an approach"
   ├─ procedures (ranked by goal words) ► work steps    "Design an event store"
   │
   ▼
frame → assess… → decide → work… → produce → review     (trimmed to max_steps:
                                                         assess first, then work)
```

The shape changes with the goal:

- **Comparison** ("choose between Kafka and Pulsar"): one *Compare A and B* step.
- **Migration** ("migrate CRUD to event sourcing"): no decide step; migration procedures are ranked first.
- **Decision output** (ADR): no work steps.
- **Hands-on output** (code, checklist): work steps walk the procedure one step at a time. For documents and plans they're drafting input instead.

Output package:

```
guide-<goal>/
├── guide-<goal>/                    the Agent Skill (copy to .claude/skills/)
│   ├── SKILL.md                     steps with Ask / Do / Done when / Skip if
│   └── references/
│       ├── background.md            prerequisite explanations
│       ├── options.md               alternatives, when each fits
│       ├── procedures.md            the source procedures
│       ├── output-template.md       skeleton of the final output
│       └── sources.md               chapters + doc sections used
├── conversation.json                the same flow as data, for other hosts
├── _prompts/refine-conversation.md  optional prose polish by a sub-agent
└── README.md, _provenance.md, _validation.md
```

### Example: design doc

```
/mypub:kb-guided-conversation Design an order service with event sourcing --output "a design doc"
```

`SKILL.md` (excerpt, from a small test catalog):

```markdown
---
name: guide-design-an-order-service-with-event-sourcing
description: "Guided conversation that takes the user from a goal to a finished design doc…"
disable-model-invocation: true
argument-hint: "[where you are starting from]"
---
## State
- `goal`, `audience`, `constraints`, `success_test`
- `knows_domain_events`, `knows_aggregates`
- `decision_event_sourcing`
- `result_design_an_event_store`, `result_replay_events_into_a_projection`
- `output_location`

## Steps
### 1. Frame the goal
**Ask:** Restate the goal… Who will use the design doc? Constraints? What is good enough?
**Done when:** The user has confirmed the goal and given a success test.

### 2. Check: Domain Events
**Ask:** How comfortable are you with Domain Events: yes, roughly, or no?
**Do:** If "roughly" or "no", explain from references/background.md#domain-events, then ask one check question.
**Skip if:** the user says they already know it.

### 4. Choose an approach: Event Sourcing
**Do:** Give each option in one line from references/options.md… Leave out any option
that is not a real alternative… Recommend one against the user's constraints.

### 5. Design an event store
**Do:** Summarise the steps in references/procedures.md#design-an-event-store that matter;
draft the matching section of the design doc.

### 7. Produce the design doc
### 8. Review against the success test
```

To use it, copy the inner folder into a project's `.claude/skills/` and type
`/guide-design-an-order-service-with-event-sourcing`. With `invocation=auto`
Claude can start it on its own when the conversation matches.

### More goals to try

| Goal | `--output` | Shape you'll get |
|---|---|---|
| Choose between Apache Kafka and Apache Pulsar for our event backbone | an ADR | frame → assess → one compare step → produce → review |
| Migrate a CRUD orders table to event sourcing | a migration plan | no decide step; migration procedures first |
| Set up a Lakeflow pipeline for claims ingestion | a working prototype | procedures walked one step at a time |
| Prepare a production deployment of Kafka Streams | a deployment runbook | checklist-shaped output template |

## 4. Known limits

- The catalog's CONTRASTS_WITH edges can be loose (Kafka's neighbours include
  KSQL and HDFS). The skill tells Claude to drop options that aren't real
  alternatives, but the list starts noisy.
- The validator checks structure, not whether a procedure is on-topic.
  Read the work steps before sharing a guide.
- Lexa skill picking is word overlap with skill descriptions. For a vague
  request ("Lexa agent") it picks only the always-on skills and leaves the
  domain choice to a sub-agent; name the domain to get a sharper agent.
- Databricks: `databricks bundle validate` needs the CLI and a workspace
  login; the offline tests don't.
