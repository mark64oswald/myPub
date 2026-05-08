# Generators

Seventeen generators on a single Phase 7 framework. Each takes a topic, a stack, or a question — and produces a deterministic, reproducible artifact backed by chapter citations, doc snapshots, and procedures.

[← back to top-level README](../README.md) · [Architecture ↗](architecture.md) · [Concept graph ↗](concept-graph.md) · [Customization ↗](customization.md)

---

## The framework shape

Every generator is a four-stage pipeline:

```text
                ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
inputs ────────►│ Decomposer   │───►│ Planner      │───►│ Validator    │───►│ Materializer │────► output
                └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                concept clusters    file layout +       resolve targets,    write files,
                + retrieval pool    sub-agent prompts   match procedures,   record provenance
                                                        confidence floor
```

Defined in [`mcp-servers/kb-mcp/generator.py`](../mcp-servers/kb-mcp/generator.py). Each generator implements or composes these protocols, persists run state in `generated_package` / `generated_unit` / `generated_file` / `generated_source`, and writes output under `data/generated-packages/<name>_<timestamp>/`.

### What `validate` actually checks

| Check | Why |
|---|---|
| Every concept-target resolves to a `concept_id` | Catches typos and unresolved aliases before materialization |
| Every cited chapter / doc section exists | The retrieval pool stays consistent with the catalog |
| Every step in a procedure-backed file matches a row in `procedure` | Tutorial / Bootstrap won't emit "step 3: …" without a real source |
| Per-file confidence floor met | Low-confidence concepts get explicit `[needs review]` markers rather than silent omission |
| No unmatched targets | The materializer doesn't run if anything is unresolved |

---

## The seventeen, by category

### Skills & curriculum

| Generator | Slash command | Source file | What it does |
|---|---|---|---|
| Skills Factory | `/kb-generate-skills <domain>` | [`skills_factory.py`](../mcp-servers/kb-mcp/skills_factory.py) | Generates a complete Claude Skills package |
| Concept Neighborhood Map | `/kb-concept-map <concept>` | [`concept_map.py`](../mcp-servers/kb-mcp/concept_map.py) | Markdown map of concepts within N hops |
| Learning Path | `/kb-learning-path <target>` | [`learning_path.py`](../mcp-servers/kb-mcp/learning_path.py) | Topologically sorted curriculum with chapter citations |
| Curriculum (composite) | `/kb-curriculum <topic>` | [`curriculum.py`](../mcp-servers/kb-mcp/curriculum.py) | Multi-week course built from learning paths + tutorials |

### Reference & teaching

| Generator | Slash command | Source file | What it does |
|---|---|---|---|
| Cheatsheet | `/kb-cheatsheet <topic>` | [`cheatsheet.py`](../mcp-servers/kb-mcp/cheatsheet.py) | One-page quick reference: definitions, gotchas, code patterns |
| Slide-Deck Outline | `/kb-slides <topic>` | [`slide_deck.py`](../mcp-servers/kb-mcp/slide_deck.py) | Title + bullets per slide with citations |
| Tutorial | `/kb-tutorial <topic>` | [`tutorial.py`](../mcp-servers/kb-mcp/tutorial.py) | Step-by-step walkthrough backed by procedures |
| Content Brief | `/kb-content-brief <topic>` | [`content_brief.py`](../mcp-servers/kb-mcp/content_brief.py) | Article / blog skeleton with key points and sources |
| Pattern + Anti-Pattern Catalog | `/kb-pattern-catalog <topic>` | [`pattern_catalog.py`](../mcp-servers/kb-mcp/pattern_catalog.py) | Patterns with canonical implementations + anti-patterns to avoid |

### Decisions & strategy

| Generator | Slash command | Source file | What it does |
|---|---|---|---|
| ADR | `/kb-adr <decision>` | [`adr.py`](../mcp-servers/kb-mcp/adr.py) | Architecture Decision Record with options + rationale |
| Tech Assessment | `/kb-tech-assessment <tech>` | [`tech_assessment.py`](../mcp-servers/kb-mcp/tech_assessment.py) | Maturity, fit, risk, alternatives |
| Migration Guide | `/kb-migration-guide <from> <to>` | [`migration_guide.py`](../mcp-servers/kb-mcp/migration_guide.py) | CONTRADICTS-edge-driven version migration |
| Currency Report | `/kb-currency-report <topic>` | [`currency_report.py`](../mcp-servers/kb-mcp/currency_report.py) | Where books and live docs disagree |

### Voice & character

| Generator | Slash command | Source file | What it does |
|---|---|---|---|
| Dialog | `/kb-dialog <topic>` | [`dialog.py`](../mcp-servers/kb-mcp/dialog.py) | Two-character conversation (Architect vs Practitioner) |
| Author Panel | `/kb-author-panel <topic>` | [`author_panel.py`](../mcp-servers/kb-mcp/author_panel.py) | Multi-author roundtable using `compare_concept_across_authors` |

### Bootstrap & refactor

| Generator | Slash command | Source file | What it does |
|---|---|---|---|
| **Project Bootstrap** ★ | `/kb-bootstrap <stack>` | [`project_bootstrap.py`](../mcp-servers/kb-mcp/project_bootstrap.py) | Runnable scaffold — code, configs, docker-compose, tests |
| Refactoring Playbook | `/kb-refactoring <pattern>` | [`refactoring_playbook.py`](../mcp-servers/kb-mcp/refactoring_playbook.py) | Targeted refactor with before/after snippets |

★ User's #1 motivating example. See [Project Bootstrap](#project-bootstrap) below for the canonical CQRS+Kafka+HL7 walkthrough.

---

## Concept Neighborhood Map

**Purpose**: visualize a concept's local graph — what it requires, what extends it, what implements it, what it's contrasted with.

**Inputs**: a concept name (resolved via the EntityResolver), and an optional hop depth (default 2).

**Output**:

```text
data/generated-packages/concept-map_<concept>_<timestamp>/
├── manifest.json           — generation provenance
├── concept-map.md          — rendered map
└── sources.md              — chapter citations per node
```

**Example call**:

```text
/kb-concept-map event sourcing
```

The Decomposer walks `REQUIRES`, `EXTENDS`, `IMPLEMENTS`, `CONTRASTS_WITH` edges out from "Event Sourcing" to depth 2. The Planner groups results into four sections (Prerequisites, Refinements, Implementations, Contrasts). The Validator confirms every node resolves and has at least one chapter citation. The Materializer renders markdown.

A small map example (excerpted):

```markdown
# Event Sourcing — Concept Neighborhood Map

## Prerequisites (REQUIRES)
- **Domain Event** — Vernon, *Implementing Domain-Driven Design*, ch. 8
- **Aggregate** — Evans, *Domain-Driven Design*, ch. 6

## Refinements (EXTENDS)
- **Event-Sourced Aggregate** — Young, *Versioning in an Event-Sourced System*, ch. 1

## Implementations (IMPLEMENTS)
- **Event Store** — Vernon ch. 8; EventStore docs (Context7)
- **Outbox Pattern** — Microservices Patterns, Richardson, ch. 3

## Contrasts (CONTRASTS_WITH)
- **CRUD with Audit Log** — Newman, *Building Microservices*, 2nd ed., ch. 5
```

---

## Learning Path

**Purpose**: produce a topologically sorted reading order that ends at a target concept.

**Inputs**: target concept name; optional starting concept; optional max-depth.

**Algorithm**:

1. Run `find_prerequisites(target, max_depth)` to collect the prerequisite tree
2. Topologically sort by depth (ties broken by chapter authority + recency)
3. For each concept in order, pick the best chapter via `search_chapters(generation, balanced)` weight profile
4. Emit a numbered reading list with chapter title, book, and an explanation of *why* this concept appears at this point

**Output** (excerpted):

```markdown
# Learning Path: Event Sourcing

## 1. Domain Modeling Basics
   *Read: Evans, ch. 1, "Crunching Knowledge into Software"*
   You need a feel for entities, value objects, and aggregates before
   events make sense.

## 2. Domain Events
   *Read: Vernon, ch. 8, "Domain Events"*
   What an event is, why it's published, who subscribes.

## 3. Aggregate Design
   *Read: Vernon, ch. 10, "Aggregates"*
   Aggregates are the unit of consistency that emit events.

## 4. Event Sourcing Mechanics
   *Read: Young, ch. 1, "Why Event Sourcing"*
   The pivot: store events instead of state.

…
```

---

## Cheatsheet

**Purpose**: one-page reference. Definitions, gotchas, common code shapes.

**Inputs**: a topic.

**Algorithm**: `search_chapters(generation, balanced)` for top-scored chapters, then a Decomposer that pulls headings, definitions, and code-shaped excerpts. Validator drops anything that lacks an explicit citation.

**Output**: single-file `cheatsheet.md` with sections — *Definitions*, *Common gotchas*, *Code patterns*, *Anti-patterns*, *Further reading*.

---

## Slide-Deck Outline

**Purpose**: a slide-by-slide outline (title + 3–5 bullets per slide) for a 30–60 minute talk.

**Inputs**: topic, optional duration.

**Output**: `slides.md` with one `## Slide N — Title` header per slide and bulleted content beneath. Each bullet has a citation footnote.

The deck-outline is *outline* — it doesn't generate speaker notes (that's a v2 prose-layer concern, see [docs/operations.md](operations.md#deferred-work)).

---

## Pattern + Anti-Pattern Catalog

**Purpose**: list the established patterns for a topic, paired with anti-patterns and the conditions that distinguish them.

**Inputs**: topic name.

**Algorithm**: graph walk for concepts where `concept_type IN ('Pattern', 'AntiPattern')` and within 2 hops of the topic. Cross-reference against `IMPLEMENTS` and `CONTRASTS_WITH` edges to pair patterns with their anti-patterns.

**Output**: structured markdown — pattern name, when to use, canonical implementation snippet, anti-pattern (commonly confused or commonly mis-applied), citations.

---

## Content Brief

**Purpose**: a writer's brief for an article or blog post. Not the article itself — the *brief* (key points, sources to cite, suggested structure).

**Inputs**: topic and optional target audience.

**Output**: `content-brief.md` with sections — *Audience*, *Key claims*, *Supporting evidence (with citations)*, *Suggested structure*, *Sources to cite*.

The v1 emits structural skeleton + sub-agent prompts; the prose layer is v2 work.

---

## Tutorial

**Purpose**: a step-by-step walkthrough backed by extracted procedures.

**Inputs**: topic.

**Algorithm**:

1. Find concepts within 1 hop of the topic that have linked procedures
2. Topologically sort procedures by their preconditions
3. Render each procedure as a numbered tutorial step
4. Insert prerequisite explanations between steps where needed

**Output**: `tutorial.md` with explicit prerequisites, step-by-step instructions, and "what could go wrong" callouts pulled from procedure `failure_modes`.

The Tutorial generator's v1 renders procedure JSON steps verbatim; v2 should rewrite each step into pedagogical prose via sub-agent (see [docs/operations.md](operations.md#deferred-work)).

---

## ADR (Architecture Decision Record)

**Purpose**: a decision record. Context, options considered, decision, consequences.

**Inputs**: a decision question (e.g., "Should we use event sourcing for the order service?").

**Algorithm**: identify candidate options from `CONTRASTS_WITH` edges in the concept graph, then for each option pull pros/cons from chapter citations.

**Output**: `adr.md` in the standard ADR template — *Context*, *Decision*, *Status*, *Consequences*, *Options considered* (with citations).

---

## Tech Assessment

**Purpose**: assess a technology choice. Maturity, fit, risk, alternatives.

**Inputs**: a technology or library name.

**Algorithm**: pull live-doc snapshot freshness as a signal of maintenance health; pull book coverage as a signal of community understanding; pull `CONTRASTS_WITH` edges to surface alternatives.

**Output**: `tech-assessment.md` — *Summary*, *Maturity & community*, *Strengths*, *Risks*, *Alternatives*, *Recommendation*.

---

## Migration Guide

**Purpose**: a guide for migrating from one version (or one library) to another, driven by `CONTRADICTS` edges.

**Inputs**: source version / library, target version / library.

**Algorithm**: query `alignment_edge` for CONTRADICTS edges between the two doc snapshots; for each contradiction, identify the concept and pull the migration step.

**Output**: `migration-guide.md` with a per-concept change list and migration steps.

**Today's caveat**: the `alignment_edge` table has 24 CONTRADICTS rows but most are degenerate (avg confidence 0.16). Migration Guide infrastructure ships correctly but is signal-starved until alignment is rerun with a contradiction-tuned prompt + multi-sample voting. Real high-confidence CONTRADICTS examples that *have* surfaced (FastMCP allowing breaking changes in minor versions vs. SemVer textbooks; React Compiler being installable now vs. "experimental" in older books; DuckPGQ's logical-graph-over-SQL approach vs. native graph databases' index-free adjacency) prove the substrate works — the issue is signal density. See [docs/operations.md → Deferred work](operations.md#deferred-work).

---

## Currency Report

**Purpose**: tell me where my books are out of date relative to current docs.

**Inputs**: a topic or domain (or `--all`).

**Algorithm**: same CONTRADICTS-edge walk as Migration Guide, but framed as a *report* rather than a *guide* — emphasizes diff and dates rather than migration steps.

**Output**: `currency-report.md` with one section per stale topic.

Same data dependency as Migration Guide; needs CONTRADICTS edges to be useful.

---

## Dialog

**Purpose**: two-character conversation about a topic. The default characters are Architect (high-level, decision-focused) and Practitioner (hands-on, what-can-go-wrong-focused).

**Inputs**: topic, optional character pair.

**Algorithm**: characters are *view functions* over the ranking engine. Architect filters for chapters tagged with `concept_type IN ('Pattern', 'Concept', 'Algorithm')`; Practitioner filters for chapters with linked procedures and `failure_modes`. Dialog turns alternate between the two views.

**Output**: `dialog.md` with character labels and citation footnotes.

See [docs/customization.md](customization.md#character-profiles) for adding new characters.

---

## Author Panel

**Purpose**: a roundtable where multiple authors weigh in on a topic.

**Inputs**: topic, optional `--limit-authors N`.

**Algorithm**: calls `compare_concept_across_authors` to pick the top N authors who discuss the topic, then for each author selects 1–2 chapters as their "voice." Renders alternating turns.

**Output**: `author-panel.md` — author name + book per turn, with the author's distinctive framing of the topic.

---

## Project Bootstrap

★ The user's #1 motivating example. The canonical case is:

> *"I just learned about CQRS and event-driven systems — create a working example project that demonstrates this using Kafka for HL7 messaging."*

**Inputs**: a stack description — design pattern + technologies. Examples:
- `CQRS event-sourced order service with Kafka and HL7`
- `LangChain RAG over Postgres pgvector with FastMCP server`
- `Spark Structured Streaming with Delta Lake CDC`

**Why Bootstrap stress-tests the substrate**:

| | |
|---|---|
| Design vs stack decouples cleanly | Concept graph drives the *design* (CQRS, event-driven). Live docs drive the *stack* (current Kafka, HL7). The two-input generator falls out naturally. |
| Books + docs synthesis is *necessary* | Skills, Tutorial, Cheatsheet could all run on book-derived content alone. Bootstrap can't. A 2020 Kafka book produces scaffolds that don't run on current Kafka — currency-aware ranking is load-bearing. |
| Procedure quality is exposed | If procedures aren't specific enough — e.g., "configure exactly-once" rather than `enable.idempotence=true` and `transactional.id` — the generator can't compose them into runnable code. |

**Pipeline**:

```text
[decompose]   Identify concept clusters from the design (CQRS, Event Sourcing,
              CommandHandler, Aggregate, Projection) and stack
              (Kafka producer / consumer / connect, HL7 v2 parser,
              docker-compose).

[plan]        Project a file layout. Typical CQRS+Kafka scaffold:
              ├── README.md
              ├── docker-compose.yml
              ├── kafka/topic-config.yml
              ├── services/order-command/
              │   ├── pyproject.toml
              │   ├── src/handlers.py
              │   └── tests/test_handlers.py
              ├── services/order-query/
              ├── hl7/
              └── docs/architecture.md

[validate]    Resolve every concept target. Confirm procedures exist
              for each "step" in scaffolded code (Kafka producer config,
              HL7 v2 parsing, etc.). Warn explicitly if a layer of the
              stack lacks procedure coverage:
                "WARNING: 0 procedures in catalog for HL7. The HL7 layer
                 will be doc-only. Acquire HL7 books or accept that this
                 layer of the scaffold won't be runtime-validated."

[materialize] Write skeleton + sub-agent prompts to
              data/generated-packages/<name>_<timestamp>/.
```

**Output structure**:

```text
data/generated-packages/cqrs-kafka-hl7-bootstrap_<timestamp>/
├── manifest.json
├── README.md                       — generated overview + getting-started
├── docker-compose.yml              — generated
├── services/
│   ├── order-command/
│   │   ├── pyproject.toml          — generated
│   │   ├── src/handlers.py         — placeholder + sub-agent prompt
│   │   └── tests/test_handlers.py  — placeholder + sub-agent prompt
│   └── order-query/
├── hl7/
│   └── v2_parser.py                — placeholder + sub-agent prompt
├── kafka/topic-config.yml          — generated (from procedure data)
├── docs/architecture.md            — generated (from concept-graph walk)
└── _prompts/                       — per-file sub-agent prompts
    ├── handlers.py.prompt.md
    ├── test_handlers.py.prompt.md
    └── ...
```

**v1 vs v2**:

| Stage | v1 (current) | v2 (planned) |
|---|---|---|
| Decompose | ✅ | ✅ |
| Plan | ✅ | ✅ |
| Validate | ✅ structural | + runtime: `pip install + pytest + docker-compose up + data flows` |
| Materialize | ✅ skeleton + per-file prompts | + dispatch loop: wraps the sub-agents (mirror of Skills Factory's prep/process pattern) |

The canonical CQRS+Kafka+HL7 test output is preserved as a substrate-validation fixture.

---

## Refactoring Playbook

**Purpose**: a structured refactor — the change goal, the steps to make it safe, before/after code shapes.

**Inputs**: a refactor pattern name (e.g., "extract aggregate", "introduce outbox").

**Algorithm**: pulls the refactor's preconditions, steps, postcondition from `procedure`; pulls before/after code patterns from chapters that discuss the pattern.

**Output**: `refactoring-playbook.md` — *Goal*, *Preconditions*, *Steps*, *Before/After*, *Failure modes*.

---

## Curriculum (composite)

**Purpose**: a multi-week course built by composing Learning Paths, Tutorials, and Pattern Catalogs.

**Inputs**: a high-level topic and a target depth (introductory / intermediate / advanced).

**Algorithm**: a Learning Path provides the spine; for each major concept on the path, a Tutorial fills in the hands-on layer; a Pattern Catalog supplements the implementation week.

**Output**: `curriculum/` directory with `README.md` (week-by-week overview) and per-week subdirectories.

This is the most composite generator — it calls into Learning Path, Tutorial, and Pattern Catalog under the hood.

---

## Skills Factory

**Purpose**: produce a complete Claude Skills package — manifest, files, evaluations.

**Inputs**: a domain (e.g., "kafka-streams", "delta-lake", "fastmcp-development").

**Algorithm**:

1. Decompose the domain via graph community detection (Phase 5)
2. Per-skill: select sources via the chosen strategy (`recent_doc_anchored` / `consensus_synthesis` / `book_authoritative`)
3. Generate skill files with provenance tracking
4. Run trigger-routing eval (recall@1 / recall@3 / MRR) against a golden set

**Output**: a Skills package directory under `data/generated-packages/skills_<domain>_<timestamp>/` with:

```text
.claude/
├── skills/<domain>/
│   ├── SKILL.md
│   ├── examples/
│   ├── procedures/
│   └── eval/
└── commands/                       — (if the package introduces commands)
```

The Skills Factory predates the generic Phase 7 framework and uses its own tables (`skill`, `skill_package`, `skill_file`, `skill_source`, `skill_relation`).

---

## Common patterns across generators

### How a generator picks its sources

Each generator chooses a `weight_profile` and a `selection_strategy` based on its purpose:

| Generator | Default profile | Default strategy |
|---|---|---|
| Skills Factory | `skill_recent_doc_anchored` | `recent_doc_anchored` |
| Project Bootstrap | `currency_critical_interactive` | `recent_doc_anchored` |
| Concept Map | `foundational_interactive` | `book_authoritative` |
| Tutorial | `currency_critical_interactive` | `recent_doc_anchored` (procedures favor specific configs) |
| Pattern Catalog | `foundational_interactive` | `book_authoritative` |
| Migration Guide | `currency_critical_interactive` | `consensus_synthesis` |
| Currency Report | `currency_critical_interactive` | `recent_doc_anchored` |

Override per call with `weight_profile=` and `selection_strategy=` arguments. See [docs/customization.md](customization.md#weight-profiles).

### Provenance is recorded per file

Every materialized file gets a row in `generated_source` linking it back to the chapter / doc_section / procedure that backed it. To audit a generator output:

```sql
SELECT gf.path,
       gs.source_type,    -- 'chapter' | 'doc_section' | 'procedure'
       gs.source_id,
       gs.role            -- 'primary' | 'corroboration' | 'procedure'
FROM   generated_package gp
JOIN   generated_file gf USING (generated_package_id)
JOIN   generated_source gs USING (generated_file_id)
WHERE  gp.name = 'cqrs-kafka-hl7-bootstrap'
ORDER BY gf.path, gs.role;
```

### Idempotency

Re-running a generator with the same inputs produces a new timestamped directory. To overwrite a previous run, delete the previous directory first — the generators do not auto-collapse versions.

---

## See also

- [docs/architecture.md](architecture.md) — Phase 7 framework + ranking engine
- [docs/concept-graph.md](concept-graph.md) — what's behind decomposition
- [docs/customization.md](customization.md) — adding a generator, tuning weights, character profiles
- [docs/mypub-v2-generators.md](mypub-v2-generators.md) — canonical generator specifications
