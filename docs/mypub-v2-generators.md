# myPub v2: Generators — Architecture and Execution Plan

**Status:** Design proposal (future phases, post-Phase 6)
**Prerequisite:** Skills Factory (Phase 5) fully operational
**Companion documents:** `mypub-v2-architecture.md`, `mypub-v2-execution-plan.md`

> **Revision note (2026-04-30):** Expanded from eight generators to seventeen. The eight original generators (§2.1–§2.7, with anti-pattern now bundled into Pattern Catalog) are unchanged. Nine new generators are appended in §2.8–§2.16, and §6 is restructured into Phases 7 through 17. The framework, ranking engine, and substrate are unchanged — every new generator is a new lens on the same graph. Rationale: most additions reuse one of the existing decomposers (rhetorical, prerequisite, conversational) with a different template; the genuinely-new infrastructure is concentrated in the currency-aware family (Migration + Currency Report, Phase 13) which depends on Phase 4's CONTRADICTS edges.

---

## 1. Architectural Assessment

### What already exists (from the Skills Factory)

The Skills Factory established a seven-stage generative pipeline:

1. Parse request
2. Decompose domain
3. Plan structure
4. Pre-refresh doc snapshots
5. Select strategy + rank sources
6. Generate output per unit
7. Validate + materialize

The new generators share **stages 1, 4, 5, and 7** almost unchanged. What varies per generator is the **decomposition logic** (stage 2), the **planning/structural model** (stage 3), the **ranking mode** (generation-silent vs. interactive-surfaced), and the **output template** (stage 6).

A small number of generators (Project Bootstrap, Refactoring Playbook, Migration Guide) extend the pipeline with an additional **stage 6.5: code-stack reconciliation** — given selected sources, also pull live doc snapshots for the target stack at generation time and reconcile any drift between book content and current API. This is an additive stage, not a rewrite of the framework.

### What needs to change in the architecture

**Minimal structural changes.** The core pipeline, ranking engine, entity resolution, source merge, and provenance tracking are unchanged. The changes are:

#### 1. Generalized generator framework

The Skills Factory's `skills_factory.py` becomes one instance of a general `generator.py` framework with pluggable components:

```python
class Generator:
    decomposer: Decomposer        # how to break down the domain
    planner: Planner              # how to structure the output
    ranking_mode: str             # 'generation' (silent) or 'interactive' (surface conflicts)
    output_template: Template     # what shape the output takes
    validator: Validator          # what correctness means for this output type
    materializer: Materializer    # how to write to disk
```

Each generator type provides its own implementations of these components. The pipeline orchestration — retrieve, rank, merge, generate, track provenance — is shared infrastructure.

#### 2. New decomposer implementations

| Generator | Decomposer | Primary graph operation |
|---|---|---|
| Skills Factory | Community detection | DuckPGQ clustering by concept co-occurrence |
| Learning Path | Prerequisite traversal | DuckPGQ `ANY SHORTEST PATH` following REQUIRES edges |
| Content Generator | Rhetorical structure | LLM-driven outline given topic + audience + retrieved context |
| Tutorial Generator | Exercise sequencing | Prerequisite traversal + procedure availability filtering |
| Pattern + Anti-Pattern Catalog | Pattern discovery (positive + inverse) | DuckPGQ subgraph matching for IMPLEMENTS clusters; CONTRASTS_WITH/anti-relations for inverse |
| ADR Generator | Decision framing | Options from CONTRASTS_WITH edges + criteria from concept attributes |
| Tech Assessment | Evaluation matrix | Multi-concept comparison via graph neighborhood + doc currency |
| Dialog Generator | Conversational arc | Topic decomposition into debate points from ranked source conflicts |
| Concept Neighborhood Map | k-hop expansion | DuckPGQ traversal of REQUIRES/EXTENDS/CONTRASTS_WITH/IMPLEMENTS edges with depth limit |
| Cheatsheet | Topical condensation | Procedure aggregation by concept cluster, distilled to one-page reference |
| Slide-deck Outline | Talk skeleton | Reuse of rhetorical decomposer with bullet-condensation template |
| Migration Guide | Era diff | CONTRADICTS edges between book-era content and doc-era content |
| Currency Report | Volatility audit | Doc snapshot diff history + book-era/doc-era source delta |
| Author Panel | Multi-character arc (N>2) | Tension points across N character view-functions over the ranking engine |
| Project Bootstrap | Stack scaffolding | Concept→Pattern→Procedure composition reconciled with live doc snapshots |
| Refactoring Playbook | Anti-pattern → pattern transformation | Anti-pattern detection + procedure substitution path |
| Curriculum Generator | Composite (paths + tutorials + assessment + bibliography) | Combined prerequisite + procedure + dialog decomposers |

#### 3. New output tables

The existing `skill_package` / `skill` / `skill_source` / `skill_file` tables are Skills-specific. The generators need a parallel but generalized output model. Two approaches:

**Option A — Generalized tables.** One set of tables (`generated_package`, `generated_unit`, `generated_source`, `generated_file`) with a `generator_type` discriminator. Simpler schema, single provenance query path.

**Option B — Per-generator tables.** `learning_path` / `learning_stage`, `content_project` / `content_section`, etc. More explicit, but multiplies table count.

**Recommendation: Option A.** The provenance pattern is identical across generators — every generated unit traces back to sources via the same score/weight/drop_reason structure. A single generalized table set avoids duplicating this. The `generator_type` field distinguishes output types for queries.

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR,       -- 'skills', 'learning_path', 'content', 'tutorial',
                                        -- 'pattern_catalog', 'adr', 'tech_assessment', 'dialog',
                                        -- 'concept_map', 'cheatsheet', 'slide_deck',
                                        -- 'migration_guide', 'currency_report', 'author_panel',
                                        -- 'project_bootstrap', 'refactoring_playbook', 'curriculum'
    name            VARCHAR,
    domain          VARCHAR,
    target_audience VARCHAR,
    created_at      TIMESTAMP,
    source_query    TEXT           -- the original user request
);

CREATE TABLE generated_unit (
    unit_id          BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES generated_package(package_id),
    unit_type        VARCHAR,      -- 'skill', 'stage', 'section', 'exercise', 'pattern',
                                        -- 'anti_pattern', 'concept_node', 'cheatsheet_block',
                                        -- 'slide', 'migration_step', 'currency_finding',
                                        -- 'character_line', 'project_file', 'refactor_step',
                                        -- 'curriculum_week'
    name             VARCHAR,
    ordinal          INTEGER,      -- position within package
    parent_unit_id   BIGINT,       -- for nested structures (stages containing exercises)
    content_markdown TEXT,
    metadata_json    TEXT,         -- generator-specific fields (strategy, currency, checkpoint questions, etc.)
    generation_notes TEXT
);

CREATE TABLE generated_source (
    unit_id      BIGINT REFERENCES generated_unit(unit_id),
    source_type  VARCHAR,          -- 'chapter', 'doc_section', 'procedure', 'pattern'
    source_id    BIGINT,
    score        DOUBLE,
    weight       DOUBLE,
    drop_reason  VARCHAR,
    PRIMARY KEY (unit_id, source_type, source_id)
);

CREATE TABLE generated_file (
    file_id   BIGINT PRIMARY KEY,
    unit_id   BIGINT REFERENCES generated_unit(unit_id),
    filename  VARCHAR,
    purpose   VARCHAR,
    content   TEXT
);
```

**Migration note:** The existing `skill_package`, `skill`, `skill_source`, and `skill_file` tables can either be migrated into these generalized tables (with `generator_type='skills'`) or kept as-is alongside them. Keeping both avoids a migration during active use; merging simplifies queries. Decide during implementation.

#### 4. Ranking mode per generator

This is the most important architectural distinction:

| Generator | Ranking mode | Why |
|---|---|---|
| Skills Factory | Generation (silent) | Agent-consumed output must be confident, no hedging |
| Learning Path | Generation (silent) | Reading assignments should be decisive, not "maybe read this" |
| Content Generator | **Interactive (surface conflicts)** | Human author benefits from seeing debates, multiple perspectives |
| Tutorial Generator | Generation (silent) | Exercise steps must be concrete and current |
| Pattern + Anti-Pattern Catalog | **Interactive (surface conflicts)** | Pattern trade-offs ARE the content; surfacing disagreement is the point |
| ADR Generator | **Interactive (surface conflicts)** | Pros/cons of each option require showing real tensions |
| Tech Assessment | **Interactive (surface conflicts)** | Honest evaluation requires surfacing strengths AND weaknesses |
| Dialog Generator | **Interactive (surface conflicts)** | Character disagreements are driven by real source conflicts |
| Concept Neighborhood Map | Generation (silent) | Visualization is structural, not editorial — show the graph as-is |
| Cheatsheet | Generation (silent) | One-page reference must be decisive — no "maybe" entries |
| Slide-deck Outline | Generation (silent) | Bullet points need to be assertions, not surveys of debate |
| Migration Guide | **Interactive (surface conflicts)** | The whole point is surfacing book-era vs. doc-era differences |
| Currency Report | **Interactive (surface conflicts)** | Conflict surfacing IS the report — quantifies the volatility |
| Author Panel | **Interactive (surface conflicts)** | Like Dialog but with N>2 view functions; conflicts → character tensions |
| Project Bootstrap | Generation (silent) | Generated code must run; no hedging in scaffolds |
| Refactoring Playbook | **Interactive (surface conflicts)** | Trade-offs of refactor approaches require showing tensions |
| Curriculum Generator | Mixed (per sub-output) | Inherits modes from constituent generators (paths silent, dialog interactive, etc.) |

The ranking engine already supports both modes. The generator framework just needs to pass the mode through.

---

## 2. Generator Specifications

### 2.1 Learning Path Generator

**Purpose:** Produce a sequenced curriculum from current knowledge to target knowledge, using the user's own library as the primary source, with gaps filled by live docs.

**Input:** Starting knowledge ("I understand SQL and basic Python") + target knowledge ("I want to design distributed CDC pipelines") + optional constraints (time budget, depth preference).

**Decomposition — prerequisite traversal:**

1. Identify start concepts in the graph (SQL, Python — match via entity resolution).
2. Identify target concepts (CDC, distributed systems, pipeline design).
3. Run DuckPGQ `ANY SHORTEST PATH` from each start concept to each target concept following REQUIRES and EXTENDS edges. Merge the paths.
4. The union of paths is the raw concept sequence — the intellectual dependencies.
5. LLM refinement pass: group adjacent concepts into coherent learning stages (3–7 concepts per stage), name each stage, identify natural checkpoints.

**Gap analysis:**
For each concept in the path, check coverage:

- Book chapters that DISCUSS this concept → reading assignments
- Procedures that link to this concept → practice exercises
- Doc sections covering this concept → supplementary/current material
- Concepts with no book coverage → flag as gap, suggest doc sources or note "consider acquiring a book on this topic"

**Output shape:**

```text
learning-paths/<path-name>/
├── _path.md              # Overview, prerequisites, estimated time, gap report
├── stage-1-<name>/
│   ├── reading-list.md   # Specific chapters, ordered, with context for why
│   ├── exercises.md      # Procedures adapted as practice exercises
│   ├── supplements.md    # Doc sections for current API details
│   └── checkpoint.md     # Self-assessment: "you should now be able to..."
├── stage-2-<name>/
│   └── ...
└── stage-N-<name>/
    └── ...
```

**Selection strategy:** Authority pick for foundational concepts (Kleppmann for distributed systems, Kimball for data modeling). Recent-doc anchored for technology-specific stages. Consensus synthesis for design patterns and architectural topics.

**Validation:**

- Prerequisite completeness: every concept in stage N has its prerequisites covered in stages 1..N-1
- No circular dependencies between stages
- Every stage has at least one reading assignment (not just doc references)
- Checkpoint questions are answerable from the stage's reading material
- Gap report flags concepts with thin coverage

**Slash command:** `/kb-generate learning-path "<from> to <to>"`

---

### 2.2 Content Generator

**Purpose:** Produce a research-grounded first draft for technical articles, blog posts, conference talks, or design documents. The system provides the structured research foundation; the author provides voice and editorial judgment.

**Input:** Topic + audience + format (blog post, conference talk, design doc, book chapter) + angle/thesis (optional).

**Decomposition — rhetorical structure:**

1. Retrieve broadly across the topic. Cast wide — pull from books, doc sections, procedures. The goal is to see the full landscape before structuring.
2. LLM-driven outline generation given: topic, audience, format conventions, and the retrieved source material. The LLM proposes a narrative arc appropriate to the format:
   - Blog post: hook → context → problem → approaches → comparison → recommendation → conclusion
   - Conference talk: opening story → problem framing → three key insights → demo walkthrough → takeaways
   - Design doc: context → requirements → options considered → analysis → decision → consequences
   - Book chapter: concept introduction → theory → worked examples → edge cases → summary
3. User reviews and adjusts the outline before generation proceeds.

**Ranking mode: interactive.** This is the critical difference from Skills. The author *wants* to see where sources disagree. "Two books recommend trigger-based CDC for simplicity; current docs and one recent book strongly favor log-based. The shift happened because..." — that's not noise, that's the substance of good technical writing.

**Output shape:**

```text
content/<project-name>/
├── _brief.md             # Topic, audience, angle, format, source summary
├── outline.md            # Rhetorical structure with section goals
├── draft.md              # First draft with inline provenance annotations
│                         # [source: Kleppmann Ch.11, score: 0.92]
│                         # [conflict: book says X, docs say Y — author decision needed]
├── sources.md            # Full bibliography with relevance scores
├── notes.md              # All conflicts, currency flags, decisions needed
└── assets/
    ├── comparison-table.md  # Auto-generated from concept attributes
    └── code-examples.md     # Pulled from procedures, verified current
```

**Selection strategy:** Consensus synthesis as default (breadth matters for articles). But conflicts are surfaced in `notes.md` rather than silently resolved. The author decides how to handle them — that's editorial judgment, not something the system should automate.

**Validation:**

- Every outline section has at least 2 source references
- Source coverage spans the topic (not all from one book)
- Currency flags present for any source >2 years old on a fast-moving topic
- Code examples verified against current doc snapshots
- Comparison tables have data for all compared items (no empty cells)

**Slash command:** `/kb-generate content "<topic>" --format blog|talk|design-doc|chapter`

---

### 2.3 Tutorial / Workshop Generator

**Purpose:** Produce hands-on exercises with explanatory context, sequenced by prerequisite dependency, using current APIs and configurations.

**Input:** Topic + skill level (beginner/intermediate/advanced) + tools/technologies to cover + optional time budget.

**Decomposition — exercise sequencing:**

1. Prerequisite traversal (like learning paths) to establish concept ordering.
2. Filter to concepts that have associated procedures — a concept without a procedure can't become a hands-on exercise.
3. Group into workshop modules: each module teaches 1–2 concepts through 1–3 exercises.
4. Order modules by prerequisite dependency, with each module building on the previous one's outputs where possible (exercise 3 uses the data produced by exercise 2).

**Key distinction from learning paths:** Learning paths point you to *existing content to read*. Tutorials *generate new content* — exercise instructions, expected outputs, troubleshooting guides. The procedures from the procedure table are the raw material, but they need to be adapted into a teaching context with setup steps, expected outputs, and "what if it doesn't work" sections.

**Ranking mode: generation (silent).** Tutorial steps must be concrete and current. "Some sources recommend X" is death in a tutorial — the student needs one unambiguous instruction. Recent-doc anchored is the dominant strategy because API calls and configuration steps must match current reality.

**Output shape:**

```text
tutorials/<tutorial-name>/
├── _workshop.md          # Overview, prerequisites, setup, estimated time
├── module-1-<name>/
│   ├── lesson.md         # Concept explanation (why before how)
│   ├── exercise-1.md     # Step-by-step with expected output
│   ├── exercise-2.md     # Builds on exercise-1
│   └── troubleshooting.md  # Common errors and fixes
├── module-2-<name>/
│   └── ...
├── solutions/            # Complete solutions for all exercises
│   ├── module-1/
│   └── module-2/
└── setup/
    └── prerequisites.md  # Tools to install, accounts to create, data to download
```

**Selection strategy:** Recent-doc anchored exclusively for any step involving an API call, CLI command, or configuration. Book content provides the "why" context in lesson.md (consensus synthesis). Procedures are the primary source for exercise steps.

**Validation:**

- Every exercise references a procedure from the procedure table
- API calls and configurations verified against current doc snapshots
- Prerequisites are complete (every tool mentioned in exercises is listed in setup)
- Solutions match exercises step-for-step
- Modules are ordered so no exercise requires knowledge from a later module
- Code/commands are syntactically valid (basic lint check)

**Slash command:** `/kb-generate tutorial "<topic>" --level beginner|intermediate|advanced`

---

### 2.4 Pattern + Anti-Pattern Catalog Generator

**Purpose:** Discover and document reusable patterns from the concept graph — automated identification of recurring architectural approaches, with trade-offs surfaced from multiple sources. The same generator also produces **anti-pattern catalogs** as the inverse output: cluster-detected approaches that sources explicitly warn against, paired with the patterns they should be replaced by (via CONTRASTS_WITH and "do this instead" linkage).

**Why bundle them:** Patterns and anti-patterns share the same backend (graph clustering, multi-perspective ranking, evidence filtering). The only differences are the seed query (concepts tagged or discussed as "anti-pattern", "smell", "avoid", "deprecated", "considered harmful") and the output template (anti-pattern entries include a "replace with" pointer to a positive pattern). Bundling avoids a parallel generator with 90% shared code.

**Input:** Domain scope (e.g., "data integration patterns", "stream processing patterns") or discovery mode ("find patterns in my library related to `<topic>`"). For inverse mode: `--inverse` flag, or `/kb-discover-anti-patterns "<topic>"`.

**Decomposition — pattern discovery:**

1. Query the concept graph for clusters of concepts connected by IMPLEMENTS edges. Each cluster is a candidate pattern: a concept (the pattern) linked to multiple procedures (implementations) and discussed across multiple chapters/doc sections (evidence).
2. Filter to clusters with sufficient evidence — at least 2 independent sources discussing the pattern (not just one author's invention).
3. For each candidate pattern, check against the existing YAML pattern library to avoid duplicates.
4. LLM refinement: name the pattern, draft the problem statement, identify the key trade-offs based on how different sources discuss it.

**Ranking mode: interactive (surface conflicts).** Pattern trade-offs *are* the content. When Kleppmann describes event sourcing differently than a Databricks architecture guide, that's not noise — it's the essential information about when and why to choose different approaches. The Pattern Catalog Generator explicitly surfaces these perspectives.

**Output shape (positive patterns):**

```text
patterns/<catalog-name>/
├── _catalog.md           # Overview, scope, how patterns relate
├── <pattern-name>/
│   ├── pattern.yaml      # Structured: name, problem, solution, trade-offs, aliases
│   ├── discussion.md     # Multi-perspective analysis with provenance
│   │                     # "Kleppmann emphasizes X; the Databricks guide recommends Y
│   │                     #  for lakehouse contexts; these approaches differ because..."
│   ├── implementations/  # Known implementations with links to procedures
│   │   ├── debezium.md
│   │   └── delta-dlt.md
│   └── related.md        # Links to other patterns (CONTRASTS_WITH, EXTENDS)
└── <pattern-name>/
    └── ...
```

**Output shape (anti-patterns, when `--inverse` mode):**

```text
patterns/<catalog-name>-anti/
├── _catalog.md             # Overview of anti-patterns and why they recur
├── <anti-pattern-name>/
│   ├── anti-pattern.yaml   # name, smell, why-it-happens, harm, replace-with
│   ├── discussion.md       # Source quotes warning against it (provenance preserved)
│   ├── replace-with.md     # Pointer(s) to positive patterns; rationale per source
│   └── examples.md         # Sourced examples (with sanitized provenance) of the anti-pattern in the wild
└── <anti-pattern-name>/
    └── ...
```

**Selection strategy:** Consensus synthesis for problem statements and solutions (multiple authors should agree on what the pattern *is*). Interactive surfacing for trade-offs (where authors disagree is where the interesting design wisdom lives). Authority pick for canonical formulations when one source is definitional.

**Validation:**

- Every pattern has at least 2 independent source references
- No duplicate patterns (check against existing pattern library)
- Trade-offs section contains at least one genuine tension or design choice
- Implementation links point to valid procedures
- Related-pattern links use valid edge types from the concept graph
- pattern.yaml validates against a JSON schema

**Validation (anti-patterns):**

- Every anti-pattern has at least 2 independent sources warning against it (no straw-men)
- Every anti-pattern has a `replace_with` pointer to a positive pattern in the catalog (or an explicit "no canonical replacement" note with rationale)
- The "harm" claim is sourced, not asserted
- Anti-patterns aren't merely "older patterns" — they're approaches sources actively warn against, distinguished from "deprecated by recency" (which belongs in the Currency Report, §2.12)

**Slash commands:**

- `/kb-generate patterns "<domain>"` — positive patterns
- `/kb-generate patterns "<domain>" --inverse` — anti-patterns
- `/kb-discover-patterns "<topic>"` — exploratory mode (positive)
- `/kb-discover-anti-patterns "<topic>"` — exploratory mode (inverse)

---

### 2.5 Architecture Decision Record (ADR) Generator

**Purpose:** Produce a structured ADR grounded in the knowledge graph — options identified from the concept graph's CONTRASTS_WITH and IMPLEMENTS edges, pros/cons populated from ranked sources with currency flags, and a recommendation traced to evidence.

**Input:** Decision context ("We need a CDC solution for our Databricks lakehouse") + optional constraints (must support schema evolution, must integrate with existing Kafka cluster, team has no JVM experience).

**Decomposition — decision framing:**

1. Identify the core decision concept(s) from the input (CDC, lakehouse integration).
2. Traverse CONTRASTS_WITH and IMPLEMENTS edges to find candidate options. For CDC: Debezium, Delta Live Tables, Kafka Connect, Zippy (if auto-discovered). The graph tells you what the real alternatives are — not a speculative list but technologies that your sources actually discuss as alternatives to each other.
3. For each option, collect the concept neighborhood: what it REQUIRES, what it EXTENDS, what patterns it IMPLEMENTS, what technologies it integrates with.
4. Derive evaluation criteria from the constraints and from the concept attributes themselves. Some criteria come from the user ("must support schema evolution"); others emerge from the sources ("operational complexity is a recurring theme across all options").
5. LLM refinement: structure as a standard ADR (context → options → criteria → analysis → recommendation).

**Ranking mode: interactive.** The pros and cons must surface real tensions. A pro is a claim supported by a source; a con is either a gap, a conflict, or a currency concern. "Debezium has excellent schema evolution support [Kleppmann, Ch.11; Debezium docs v2.6] but requires JVM operations expertise that your team lacks [constraint from user]."

**Output shape:**

```text
decisions/<decision-name>/
├── _adr.md               # The complete ADR document
│                         # Status, context, options, criteria, analysis, decision
├── options-matrix.md     # Options × criteria scoring with provenance
├── sources.md            # All sources consulted with relevance scores
├── risks.md              # Risk factors: currency gaps, thin coverage, unresolved conflicts
└── notes.md              # Raw conflict data and author's decision points
```

The `_adr.md` follows a standard ADR template:

- **Status:** Proposed
- **Context:** The decision scenario (from user input + graph context)
- **Options considered:** Each with a sourced description (not invented by the LLM)
- **Evaluation criteria:** From constraints + source-derived dimensions
- **Analysis:** Options × criteria with pros/cons from ranked sources, currency flags visible
- **Recommendation:** The top-ranked option with rationale, or "no clear winner — here's why" when sources genuinely conflict
- **Consequences:** What this decision implies (from REQUIRES edges — "choosing Debezium means you'll also need Kafka, ZooKeeper or KRaft, and a schema registry")

**Selection strategy:** Interactive surfacing for pros/cons (the whole point is showing the tensions). Consensus synthesis for context and option descriptions. Recent-doc anchored for any claims about current features or performance.

**What the graph gives you that ad-hoc research can't:** The options come from the graph's actual CONTRASTS_WITH relationships, not from Claude guessing alternatives. The consequences come from REQUIRES edges — real dependency chains. The currency flags are computed, not estimated. An ADR generated from the graph is grounded in your accumulated knowledge in a way that "ask Claude to write an ADR" never is.

**Validation:**

- Every option is a real concept in the graph (not invented)
- Every pro/con traces to a source with a score
- Evaluation criteria are comprehensive (not just the user's constraints)
- Consequences follow real REQUIRES edges
- Currency flags present for options with dated source coverage
- At least 2 independent sources per option

**Slash command:** `/kb-generate adr "<decision context>" [--constraints "..."]`

---

### 2.6 Technical Assessment / Due Diligence Generator

**Purpose:** Produce a comprehensive evaluation of a technology, library, or platform — maturity, ecosystem, risks, comparison to alternatives — grounded in the knowledge graph with explicit coverage analysis.

**How it differs from ADRs:** An ADR is decision-oriented (which option should we choose?). A Technical Assessment is evaluation-oriented (how good is this technology, period?). An ADR compares options side by side. An assessment deep-dives one subject. They share the interactive ranking mode and the practice of surfacing tensions, but the decomposition and output structure are different.

**Input:** Technology to assess ("FastMCP" or "DuckDB" or "LangGraph") + optional evaluation dimensions (maturity, performance, ecosystem, learning curve, community health).

**Decomposition — evaluation matrix:**

1. Identify the target concept in the graph. If not present, trigger auto-discovery.
2. Map its graph neighborhood:
   - What does it REQUIRE? (dependencies, prerequisites)
   - What EXTENDS it? (ecosystem, plugins, integrations)
   - What IMPLEMENTS it? (use cases, patterns)
   - What CONTRASTS_WITH it? (alternatives, competitors)
   - How many sources DISCUSS it? (evidence depth — 8 book chapters + 3 doc sources = well-covered; 1 doc section only = thin)
3. Derive assessment dimensions from the neighborhood + user input:
   - **Maturity:** how long has the graph known about it? How many books (vs. only docs)? Is there a pattern it IMPLEMENTS that's well-established?
   - **Ecosystem:** count and quality of EXTENDS edges — what integrates with it?
   - **Risk factors:** thin coverage areas, currency concerns, unresolved conflicts between sources
   - **Learning curve:** REQUIRES chain depth — how much prerequisite knowledge is needed?
   - **Community health:** doc source volatility (actively changing = active community), coverage breadth across source types
4. For each dimension, retrieve relevant sources and rank in interactive mode.

**Ranking mode: interactive.** An honest assessment requires showing both strengths and weaknesses with their sources. "DuckDB has excellent analytical performance [7 sources, strong consensus] but its HNSW persistence is still experimental [DuckDB docs, confirmed; 2 books don't mention this limitation because they predate it]."

**Output shape:**

```text
assessments/<technology>/
├── _assessment.md        # Executive summary + detailed evaluation
├── dimensions/
│   ├── maturity.md       # Evidence-based maturity analysis
│   ├── ecosystem.md      # Integration landscape from EXTENDS edges
│   ├── risks.md          # Thin coverage, currency gaps, conflicts
│   ├── learning-curve.md # Prerequisite depth analysis
│   └── alternatives.md   # Comparison from CONTRASTS_WITH edges
├── coverage-report.md    # Graph coverage analysis: what's well-sourced vs. thin
├── sources.md            # Full bibliography with relevance scores
└── graph-neighborhood.md # Visual map of the concept's graph context
```

**Selection strategy:** Consensus synthesis for established assessments (what multiple sources agree on). Interactive surfacing for tensions and risks. Recent-doc anchored for any claims about current state (performance benchmarks, feature availability, known limitations).

**Unique validation:**

- Graph coverage report is accurate (spot-check source counts)
- Maturity assessment correlates with real-world signals (not just graph metrics)
- Risk section includes at least one non-obvious risk (not just "it's new")
- Alternatives come from actual CONTRASTS_WITH edges, not LLM suggestions
- Learning curve depth matches real prerequisite chains

**Slash command:** `/kb-generate assessment "<technology>" [--dimensions "maturity,ecosystem,risks"]`

---

### 2.7 Dialog / Script Generator

**Purpose:** Produce conversational scripts where two or three characters discuss a technical topic, debate approaches, and explore trade-offs — suitable for podcast episodes, video content, educational audio (via ElevenLabs or similar TTS), or animated explainers.

**Why this is architecturally interesting:** Every other generator produces a document where the author's voice is singular. The Dialog Generator produces content where **source conflicts become character disagreements.** The interactive ranking mode doesn't just surface tensions for a human author to resolve — it *distributes tensions across characters* who each advocate for a different perspective, grounded in different sources.

When Kleppmann and a Databricks guide disagree about CDC approaches, that's not a conflict to resolve — it's the substance of an interesting conversation between two characters who each have good reasons for their position.

**Input:** Topic + character definitions (optional — defaults provided) + format (podcast, video script, debate, panel discussion) + target length (minutes).

**Character system:**

Default characters (can be overridden per generation):

- **The Architect** — favors foundational principles, long-term thinking, theory-first. Draws primarily from books, especially canonical references. Tends toward consensus synthesis and authority pick. Voice: measured, considers trade-offs, references historical context.
- **The Practitioner** — favors current best practices, hands-on experience, "what actually works today." Draws primarily from current documentation and procedures. Tends toward recent-doc anchored. Voice: pragmatic, specific, focuses on operational reality.
- **The Explorer** (optional third character) — curious, asks the questions the audience would ask, bridges between the other two. Synthesizes and challenges both perspectives. Voice: inquisitive, identifies tensions, pushes for clarity.

Characters aren't personas pasted onto generic dialogue — they're **view functions over the ranking engine.** The Architect sees the same ranked results but weights authority and corroboration higher. The Practitioner weights recency and doc alignment higher. When they disagree in dialogue, the disagreement traces to actual source-ranking differences.

**Decomposition — conversational arc:**

1. Retrieve broadly across the topic. Identify the key concepts, debates, and practical concerns.
2. Identify the **natural tension points** — concepts where sources with high authority-weight disagree with sources that have high recency-weight. These become the dialogue's dramatic structure.
3. Structure the conversation as scenes:
   - **Opening:** one character introduces the topic, the other reacts with a different framing. Ground the audience in why this matters.
   - **Exploration scenes (2-4):** each scene centers on a tension point. Characters present their perspective (sourced), debate, and reach either agreement or productive disagreement.
   - **Synthesis:** characters find common ground or explicitly agree to disagree, with practical takeaways for the audience.
   - **Closing:** each character gives their one-sentence recommendation. Provenance visible in show notes.
4. Target length calibration: ~150 words per minute of spoken audio. A 20-minute podcast episode ≈ 3,000 words of dialogue.

**Output shape:**

```text
dialogs/<topic>/
├── _script.md            # The complete script with character labels
│                         # ARCHITECT: "The fundamental issue with trigger-based CDC..."
│                         # PRACTITIONER: "Sure, but in practice with Databricks..."
│                         # EXPLORER: "Wait — when would you still choose triggers?"
├── show-notes.md         # Source references for every claim made in dialogue
│                         # Organized by scene, with concept links
├── characters.md         # Character profiles with their ranking weight biases
├── topics-covered.md     # Concepts discussed, mapped to graph nodes
├── tts-ready/            # Split into per-character text files for TTS
│   ├── architect.txt     # Just the Architect's lines, in order
│   ├── practitioner.txt  # Just the Practitioner's lines
│   └── explorer.txt      # Just the Explorer's lines (if 3-character)
└── metadata.json         # Timestamps, scene breaks, character assignments
                          # (for video generation tools)
```

**Ranking mode: interactive — but consumed differently.** Instead of surfacing conflicts in a notes file for the author, conflicts are *distributed across characters*. The ranking engine identifies tension points; the decomposer assigns each side of the tension to a character based on their weight profile; the generator produces dialogue where the tension plays out naturally.

**Selection strategy per character:**

- The Architect uses authority pick + consensus synthesis (books and established patterns)
- The Practitioner uses recent-doc anchored (current docs, procedures, live APIs)
- The Explorer uses no strategy bias — draws from the full ranked set and asks about gaps

**What makes this genuinely novel:** Most AI-generated "discussions" are fake — both sides are written by the same model with the same knowledge. myPub's dialog generator produces discussions where character disagreements are **grounded in actual source disagreements.** The Architect's defense of event sourcing comes from Kleppmann; the Practitioner's skepticism comes from a Databricks operations guide that documents the operational complexity. The argument is real because the sources are real.

**Validation:**

- Every factual claim in the dialogue traces to a source in show-notes.md
- Character voices are consistent (the Architect doesn't suddenly cite current docs without narrative reason)
- Tension points are genuine source disagreements, not manufactured conflict
- Dialogue reads naturally (not like alternating monologues)
- TTS-ready files are clean text (no markdown formatting, no stage directions)
- Target length is within 10% of specified duration

**Slash command:** `/kb-generate dialog "<topic>" --characters 2|3 --format podcast|video|debate --minutes 15`

**N>2 extension:** When `--characters` exceeds 3, the generator dispatches to the **Author Panel** generator (§2.13) which extends this same character/view-function model to arbitrary numbers of participants with custom weight profiles. Dialog and Author Panel share `decomposers/conversational.py` and `templates/dialog_scene.py`; Author Panel adds the panel-orchestration and Q&A scaffolding.

---

### 2.8 Concept Neighborhood Map Generator

**Purpose:** Produce a Mermaid (or DOT/SVG) visualization of a concept's k-hop graph neighborhood — REQUIRES, EXTENDS, CONTRASTS_WITH, IMPLEMENTS, and CITES edges, with node coloring by source type and edge styling by relation. Useful for orientation when starting on an unfamiliar topic, for embedding in design docs, and for debugging the graph itself.

**Why this generator goes first (Phase 7):** It exercises the framework's core moves (entity resolution, graph traversal, materialization) without needing a sophisticated decomposer or template — which makes it the smallest credible validation that the refactored generator framework actually works for non-Skills outputs. If Concept Neighborhood Map can't be built cleanly on top of the framework, the framework needs work before tackling more complex generators.

**Input:** Concept name (resolved via EntityResolver) + optional depth (default: 2 hops) + optional edge filter (e.g., only REQUIRES + EXTENDS for a learning-prerequisites view).

**Decomposition — k-hop expansion:**

1. Resolve the seed concept via EntityResolver.
2. Expand outward via DuckPGQ traversal up to `depth` hops, collecting nodes and edges.
3. Cap node count at a sensible limit (e.g., 60 nodes) — if the neighborhood is larger, prune by edge weight (most-connected first) and add an explicit "N additional nodes pruned" annotation.
4. Group nodes by source type (book chapter coverage vs. doc section coverage vs. procedure-only) for color coding.

**Ranking mode: generation (silent).** Visualizations don't surface conflict — they show the graph as-is. If a concept is contested, that surfaces via Migration Guide or Currency Report, not here.

**Output shape:**

```text
concept-maps/<concept-name>/
├── _map.md           # Concept summary + interpretation guide
├── neighborhood.mmd  # Mermaid source (renders in markdown viewers)
├── neighborhood.dot  # Graphviz DOT source (for higher-fidelity rendering)
├── neighborhood.svg  # Pre-rendered SVG (committed for browser viewing)
└── nodes.csv         # Node list with source counts (for debugging / analysis)
```

**Selection strategy:** Not applicable in the usual sense — no source ranking. The "selection" is which edges/nodes to include, governed by depth and edge-filter parameters.

**Validation:**

- Every node corresponds to a real graph concept (no orphan IDs)
- Every edge corresponds to a real `concept_relation` row
- Mermaid syntax parses (validate via `mmdc` dry-run)
- Pruned-node count is explicit when truncation occurs
- Coloring legend matches the actual node types present

**Slash command:** `/kb-generate concept-map "<concept>" [--depth N] [--edges REQUIRES,EXTENDS,...]`

---

### 2.9 Cheatsheet Generator

**Purpose:** Produce a single-page distilled reference for a library, tool, or technology — most-used commands, configuration knobs, gotchas, and "quick reference" tables. Output is one self-contained markdown page (printable, pinnable in a tab) optimized for fast lookup.

**Input:** Target subject ("DuckDB", "kubectl", "Polars DataFrames") + optional audience-level (cheatsheet for newcomers vs. cheatsheet for power users emphasizes different content).

**Decomposition — topical condensation:**

1. Resolve subject to a graph concept.
2. Pull all procedures linked to this concept (and its EXTENDS descendants) from the procedure table.
3. Cluster procedures by topic (CRUD, configuration, performance, error handling, integration).
4. For each cluster, distill to one-liner: the canonical command/snippet + one-line "when to use it." Drop preconditions, postconditions, and prose explanations — they belong in the Tutorial generator, not here.
5. Add a "gotchas" section sourced from `failure_modes` across the procedures.

**Ranking mode: generation (silent).** A cheatsheet must be decisive — "the canonical command for X is Y." If sources disagree on what's canonical, prefer the most-recent doc-anchored procedure (cheatsheets degrade fast when sourced from old books). Surface no conflicts; the reader wants the answer, not the debate.

**Output shape:**

```text
cheatsheets/<subject>/
├── cheatsheet.md      # One self-contained page — the deliverable
├── _provenance.md     # Per-line source pointer (so the reader can drill in)
└── _gotchas.md        # Failure-mode notes that didn't fit on the page
```

The `cheatsheet.md` is the *only* file the user typically reads. The other two exist for traceability and for the "I want to know more" follow-up.

**Selection strategy:** Strictly recent-doc anchored for command syntax, configuration flags, and API names. Books contribute only to the gotchas section — failure modes that recur across versions are more book-derived than doc-derived.

**Validation:**

- Output fits on one page when rendered (heuristic: <= 1,200 words / <= 8 visible sections)
- Every command snippet verified against current doc snapshot (parse-check + flag-existence-check)
- No conflicting recommendations within the same cluster
- Each section has at most one "canonical" choice; alternatives mentioned only when commonly conflated

**Slash command:** `/kb-generate cheatsheet "<subject>" [--level newcomer|power-user]`

---

### 2.10 Slide-deck Outline Generator

**Purpose:** Produce a talk skeleton — slide-by-slide bullet outline with citations, presenter notes, and suggested visuals — suitable for a 20–60 minute conference talk or internal brown-bag. Reuses the rhetorical decomposer from the Content Generator (§2.2) but with a bullet-condensation template instead of prose generation.

**Why distinct from Content Generator's "talk" format:** The Content Generator produces a prose narrative with the structure of a talk (opening → 3 insights → close). The Slide-deck Outline Generator produces actual slide-level structure: title slide, agenda slide, per-section content slides with bullets that fit on a slide, code-snippet slides with file pointers, and a closing/Q&A slide. Different deliverable, different optimal density.

**Input:** Topic + audience (engineers, executives, mixed) + duration (minutes) + optional thesis/angle.

**Decomposition — talk skeleton:**

1. Reuse `decomposers/rhetorical.py` to produce a narrative arc.
2. Map arc beats to slide groups: opening (1 slide), agenda (1 slide), each insight (3–6 slides per insight), demo/code (variable), takeaways (1–2 slides), Q&A (1 slide).
3. For each slide group, retrieve sources and condense to bullet form — 3–5 bullets per slide, < 10 words per bullet.
4. Generate per-slide presenter notes (1–3 sentences) that flesh out what the speaker says.
5. Suggest visuals where the bullets are abstract (diagram, code listing, comparison table).

**Ranking mode: generation (silent).** Bullets are assertions, not surveys. If sources disagree, the talk should pick a position (or explicitly call out the disagreement as a discussion slide) rather than waffle.

**Output shape:**

```text
slides/<topic>/
├── _outline.md       # Slide-by-slide outline with bullets and presenter notes
├── _abstract.md      # Talk abstract (suitable for CFP submissions)
├── visuals.md        # Per-slide visual suggestions (diagram requests, code refs)
├── speaker-notes.md  # Standalone speaker notes (one section per slide)
└── sources.md        # Bibliography
```

**Selection strategy:** Same as Content Generator — consensus synthesis as default, recent-doc anchored for any technology specifics. Bullets cite by superscript with full citations in `sources.md`.

**Validation:**

- Slide count matches duration (heuristic: 1 minute per content slide, +20% for opening/closing/Q&A)
- No slide has more than 5 bullets or any bullet > 10 words
- Every code-snippet slide names the file it's drawn from (verifiable in the corpus)
- Presenter notes are < 3 sentences per slide (talks fail when notes overflow)
- The narrative arc is intact when bullets are read in order (not just a list of facts)

**Slash command:** `/kb-generate slides "<topic>" --minutes N [--audience engineers|executives|mixed]`

---

### 2.11 Migration / "What Changed" Guide Generator

**Purpose:** Produce a guide that explains what has changed between an "older" understanding (typical in books >2 years old) and the current state (in live docs and recent procedures). Targets readers who learned a technology from a specific book or era and need to update their mental model — "I learned Kafka from the 2017 book, what's different in 2026?"

**Critical dependency:** Phase 4 — specifically the **CONTRADICTS** edge type between book content and current doc content. Without that edge, this generator has no signal to operate on. Cannot be prototyped before Phase 4 ships.

**Input:** Subject + optional from-era anchor ("from the perspective of *Kafka — The Definitive Guide* (2017)") + optional to-era anchor ("as of current Kafka docs").

**Decomposition — era diff:**

1. Resolve the subject concept.
2. Collect book-era sources for the subject (filtered by `book.publication_year` ≤ from-era cutoff).
3. Collect doc-era sources (current snapshots + recent books, filtered by `>= to-era cutoff`).
4. Find CONTRADICTS edges where one endpoint is in the book-era set and the other in the doc-era set. Each edge becomes a candidate migration item.
5. For each migration item, resolve: the book-era statement, the current-era statement, and the dated reasoning for the change (often inferable from `concept_relation.evidence_quote` or by re-querying the relevant doc section).
6. Group migration items by sub-topic (config-model changes, API renames, deprecated features, new defaults, security model changes).

**Ranking mode: interactive (surface conflicts) — and conflict surfacing IS the entire output**, not a side-channel. Every section is a conflict.

**Output shape:**

```text
migrations/<subject>-<from-era>-to-<to-era>/
├── _migration.md       # Reader's guide: how to use this document
├── changes/
│   ├── <topic-1>.md    # "Was: X (Book Ch.4); Now: Y (current docs); Why: Z"
│   ├── <topic-2>.md
│   └── ...
├── deprecated.md       # Features the from-era described that no longer exist
├── new-since.md        # Features that didn't exist in the from-era
├── breaking.md         # Behavior changes that would silently break legacy code
└── sources.md          # Both eras' sources side by side
```

**Selection strategy:** Per migration item: book-era source for the "was" statement, current doc snapshot for the "now" statement, and the most-recent corroborating book or doc for the "why" rationale.

**Validation:**

- Every change has both a "was" and a "now" source (no one-sided claims)
- "Was" sources predate "now" sources by at least 2 years
- Breaking-change section has a reproducer or example for each entry (not just a description)
- No claims of change without a CONTRADICTS edge underneath (the report can't invent changes)
- The deprecated/new-since lists are populated from existence diffs (concepts present in one era's source set but not the other), not from change descriptions

**Slash command:** `/kb-generate migration "<subject>" --from "<from-era anchor>" --to "<to-era anchor>"`

---

### 2.12 Currency Report Generator

**Purpose:** Produce a quantitative report on how stale a given concept's coverage is — how recent the most-recent source is, how much the doc snapshot has changed in the last N refreshes, how many CONTRADICTS edges exist between book-era and doc-era sources, and which procedures linked to this concept are still consistent with current docs vs. drifted. Targets technical leads doing portfolio reviews ("which technologies in our stack have stale internal documentation?") and authors deciding whether to refresh a piece of internal content.

**Critical dependency:** Phase 4 — same CONTRADICTS edges as Migration Guide, plus doc-snapshot version history (which Phase 4 also introduces).

**Input:** Concept name (or domain — multiple concepts) + optional time window for the volatility analysis (default: last 12 months of doc snapshots).

**Decomposition — volatility audit:**

1. Resolve concept(s).
2. For each concept, compute:
   - **Most-recent source date** (highest `MAX(publication_year, doc_snapshot_date)`)
   - **Source-age distribution** (how many sources by year)
   - **CONTRADICTS edge count** between this concept's pre-N-years sources and post-N-years sources
   - **Doc snapshot delta count** in the last `time_window` — how many times this section changed
   - **Procedure drift ratio** — what fraction of procedures linked to this concept have at least one step that no longer matches current docs (verified via parse-check or signature check)
3. Score each concept on a 0–100 currency index; lower = more stale.

**Ranking mode: interactive.** The whole report is conflict surfacing — quantified.

**Output shape:**

```text
currency-reports/<scope>/
├── _report.md            # Executive summary with currency-index distribution
├── concepts.csv          # One row per concept: index, age stats, drift counts
├── by-concept/
│   ├── <concept-1>.md    # Per-concept detail: timeline, contradictions, drifted procedures
│   └── ...
├── high-priority.md      # Top-N concepts most-stale × most-used (combine importance + drift)
└── refresh-suggestions.md # Suggested refresh actions: "re-snapshot doc X", "consider acquiring book Y"
```

**Selection strategy:** Not strategy-driven in the usual sense — the report includes everything in scope. Ranking is by currency index (computed metric), not by source authority.

**Validation:**

- Currency index calculation is reproducible (golden-file test)
- Procedure drift counts can be re-verified by re-running the parse-check
- High-priority list combines staleness with usage signal (high staleness + low usage = low priority)
- CONTRADICTS counts match the underlying graph (spot-check)
- Time window honored consistently (no off-by-one errors in date math)

**Slash command:** `/kb-generate currency-report "<concept-or-domain>" [--window 12mo]`

---

### 2.13 Author Panel Generator

**Purpose:** Extension of the Dialog Generator (§2.7) to N>2 characters, each with their own weight profile over the ranking engine — suitable for panel-discussion content (3–6 panelists), mock-author roundtables (e.g., "what would Kleppmann, Martin Fowler, and the Databricks docs team say about CDC?"), and educational debate formats with a moderator.

**Why this is structurally interesting (and cheap):** The Dialog Generator already models characters as view functions over the ranking engine. Author Panel doesn't change that — it just allows N>2 of them, with custom weight profiles per character. Almost all the implementation is shared with Dialog. The novelty is in panel-orchestration: turn-taking logic, moderator dynamics, Q&A scaffolding.

**Input:** Topic + character definitions (3–6 characters, each with name + voice + weight profile) + format (panel, roundtable, debate-with-moderator) + duration.

**Decomposition — multi-character arc:**

1. Reuse the Dialog generator's tension-point identifier — but now run pairwise across all N characters' weight profiles (instead of just Architect vs. Practitioner). This produces an N×N matrix of where pairs disagree.
2. Cluster tension points by sub-topic.
3. For each cluster, identify which 2–3 characters have the most-divergent rankings → those characters lead the segment, others react.
4. Add moderator scaffolding (introductions, transitions between segments, Q&A solicitation, wrap-up).
5. Length calibration: ~150 words per spoken minute, with N speakers reducing per-speaker airtime proportionally.

**Ranking mode: interactive.** Same as Dialog — conflicts become character tensions.

**Output shape:**

```text
panels/<topic>/
├── _script.md          # Full script with character labels and stage directions
├── show-notes.md       # Per-claim source provenance
├── characters.md       # Character profiles with weight profiles
├── tension-matrix.md   # Where pairs of panelists diverge — drives the discussion arc
├── tts-ready/          # Per-character text files for TTS
└── metadata.json       # Scene breaks, character assignments, timing
```

**Custom character authoring:** Users can define a character with a custom weight profile derived from a specific corpus author's style — e.g., "the Kleppmann character" weights the Kleppmann book sources at 1.5×, other authority sources at 1.0×, recency at 0.5×, doc alignment at 0.3×. This isn't impersonation (the character doesn't claim to be Kleppmann); it's a view function that draws from Kleppmann-favoring sources.

**Selection strategy:** Per character — each character has their own strategy bias. The framework runs the ranking engine N times (once per character's weight profile) and the orchestrator composes lines from each character's top-ranked sources for the relevant scene.

**Validation:**

- Every panelist has a distinct weight profile (no two panelists with identical profiles — that's a misconfiguration)
- Tension matrix shows real divergences (no panel where all characters agree on everything — that's a sign the topic is too settled for this format)
- Moderator turns are present at expected cadence (no monologues > 2 minutes, no character holding the floor for > 30% of total airtime)
- All Dialog validation rules apply per character

**Slash command:** `/kb-generate author-panel "<topic>" --characters "Kleppmann,Fowler,Databricks" --minutes 30`

---

### 2.14 Project Bootstrap Generator

**Purpose:** Produce a working project scaffold — folder structure, `README.md`, setup scripts, `.claude/CLAUDE.md`, sample code, configuration files — for a stated design and stack. Targets the "I just learned about pattern X and want a working example using stack Y" workflow. The motivating example: "I learned about CQRS and event-driven systems — create a working example using Kafka for HL7 messaging."

**Why this generator is the strongest test of the substrate:** Most generators are useful with books alone (Skills, Tutorial, Cheatsheet can all run on book-derived content). Bootstrap can't. A book-derived Kafka project from 2020 produces scaffolds that don't run on current Kafka (config keys renamed, defaults changed, deprecated APIs removed). So Bootstrap *requires* Phase 4's currency-aware ranking and live doc snapshots — it's load-bearing, not optional. Bootstrap also stress-tests procedure quality: if procedures aren't specific enough (e.g., "configure exactly-once" vs `enable.idempotence=true` and `transactional.id`), the generator can't compose them into runnable code. That's useful signal for prompt iteration.

**Input:** Two-axis: **design** (a concept-graph anchor — CQRS, event sourcing, hexagonal architecture, multi-tenant SaaS) + **stack** (a technology set — Kafka + Postgres, Databricks Delta Live Tables, FastAPI + DuckDB). Optional domain framing ("HL7 messaging in healthcare", "ride-hailing dispatcher").

**Decomposition — stack scaffolding:**

1. Resolve design and stack to graph concepts.
2. Walk REQUIRES + IMPLEMENTS edges from design concepts to find the patterns and procedures that realize the design.
3. Walk EXTENDS + integrations edges from stack concepts to find the procedures specific to that stack.
4. Compose: each design pattern needs a stack-specific implementation; the ranking engine picks the most-recent procedure that combines (pattern, stack).
5. **Stage 6.5 — code-stack reconciliation:** for every selected procedure, pull the live doc snapshot for the relevant stack version. Reconcile any drift (renamed config keys, deprecated APIs, default changes) before code generation. Surface unresolvable drift as scaffold-level TODOs rather than emitting code that won't run.
6. Generate scaffold: README, setup script, source files, config files, tests, `.claude/CLAUDE.md` for downstream Claude Code work in the project.

**Ranking mode: generation (silent).** Generated code must run; no hedging in scaffolds. Where sources disagree on the canonical approach, prefer the most-recent doc-anchored procedure. Conflicts are surfaced in `_design-notes.md` for user review, not in the generated code.

**Output shape:**

```text
project-scaffolds/<project-name>/
├── _design-notes.md        # Design decisions made by the generator + conflicts surfaced
├── _provenance.md          # Per-file source mapping (which procedure(s) produced this file)
├── README.md               # Project README (what it does, how to run it)
├── .claude/
│   └── CLAUDE.md           # Project-specific instructions for downstream Claude Code work
├── setup/
│   ├── install.sh          # Setup script — verified against current doc snapshots
│   └── prerequisites.md    # What needs to be installed before running setup
├── src/
│   └── ...                 # Source files implementing the design on the stack
├── tests/
│   └── ...                 # Smoke tests + sample integration tests
└── docs/
    └── architecture.md     # ADR-style design rationale (links to Pattern Catalog entries)
```

**Selection strategy:** Strictly recent-doc anchored for any code, config, or command. Books provide architectural rationale (in `_design-notes.md` and `docs/architecture.md`). Procedures are the primary source for executable steps.

**Validation (highest bar of any generator):**

- Setup script runs end-to-end on a clean machine (smoke-tested in CI before the package is sealed)
- Every file's commands/APIs verified against current doc snapshots
- Tests pass (basic smoke tests at minimum)
- `.claude/CLAUDE.md` references the actual files and conventions used
- Scaffold reflects the design (a CQRS scaffold actually has separate command and query paths, not just a name)
- Design choices are explained and sourced — no "magic" code without rationale

**Slash command:** `/kb-generate project "<design> using <stack>" [--domain "<framing>"]`

---

### 2.15 Refactoring Playbook Generator

**Purpose:** Produce a step-by-step migration playbook from an anti-pattern (or older pattern) to a target pattern, in a specified codebase context. Targets the "we have ad-hoc REST endpoints scattered across services and want to migrate to event-driven communication, what are the steps?" workflow.

**Input:** Source state (what's in the codebase now — anti-pattern or older pattern from the catalog) + target state (target pattern from the catalog) + stack (current stack and any constraints) + optional risk tolerance.

**Decomposition — anti-pattern → pattern transformation:**

1. Resolve source and target to catalog entries (Pattern Catalog § 2.4 — positive or anti-).
2. Find the transformation path: what intermediate states make the migration safe? E.g., REST → event-driven might go through "REST + outbox table" → "REST deprecated, events authoritative" rather than a big-bang switch.
3. For each step, identify the procedures that implement it (from source state, intermediate, target).
4. Reconcile each step against current docs (stage 6.5 — same as Bootstrap).
5. Generate per-step content: what to do, why, what tests verify the step, rollback procedure if it fails.

**Ranking mode: interactive.** Refactoring trade-offs are real — there are usually 2–3 viable paths from the source to the target, with different risk/effort/value profiles. The playbook surfaces these as discussion in `_path-choices.md` and lets the reader pick before drilling into the chosen path.

**Output shape:**

```text
refactor-playbooks/<source-to-target>/
├── _playbook.md           # Overview: source state, target state, chosen path
├── _path-choices.md       # Alternative paths considered + why this one was chosen
├── steps/
│   ├── 01-<step-name>.md  # Per-step: actions, rationale, tests, rollback
│   ├── 02-<step-name>.md
│   └── ...
├── checkpoints/
│   ├── after-step-2.md    # Recoverable state checkpoint definitions
│   └── after-step-N.md
├── risk-register.md       # Known risks, mitigations, sources for each
└── sources.md             # Bibliography
```

**Selection strategy:** Recent-doc anchored for the per-step actions (the code changes must run). Books and Pattern Catalog entries provide rationale (the "why" for each step). Past incidents and CONTRADICTS edges flag known-risky transitions.

**Validation:**

- Each step has a concrete rollback procedure (no "you can't go back" steps without explicit warnings)
- Checkpoints are well-defined: a tested-able state, not a midpoint
- Step ordering respects dependencies (no step requires the output of a later step)
- Risk register is sourced — risks aren't invented
- The playbook starts at a state matching "source" and ends at "target" (no missed steps)

**Slash command:** `/kb-generate refactor "<source-pattern> to <target-pattern>" --stack "<stack>"`

---

### 2.16 Curriculum Generator

**Purpose:** Produce a multi-week curriculum — readings, exercises, assessments, dialog/panel sessions, and a bibliography — for self-directed or instructor-led learning of a substantial topic. The composite generator: it orchestrates Learning Path (§2.1), Tutorial (§2.3), Dialog/Author Panel (§2.7/§2.13), and Cheatsheet (§2.9) to assemble a structured course.

**Why this depends on multiple prior phases:** Each weekly module typically pairs a reading list (Learning Path), exercises (Tutorial), an optional discussion/dialog session, an end-of-week assessment, and an optional cheatsheet handout. Curriculum can't be built before the constituent generators exist, which means Phase 16 — after Learning Path (Phase 8), Tutorial (Phase 10), Dialog (Phase 14), and Cheatsheet (Phase 9) all ship.

**Input:** Subject + audience + duration (weeks) + cadence (hours per week) + format (self-paced / instructor-led / cohort).

**Decomposition — composite:**

1. Define learning outcomes (LLM-driven from subject + audience).
2. Decompose into weekly modules (3–12 weeks typical).
3. Per module: dispatch to constituent generators with module-scoped queries:
   - Learning Path generator → reading list for the week
   - Tutorial generator → 1–3 hands-on exercises
   - Dialog/Author Panel → optional discussion-prompt script (e.g., for cohort kickoff or instructor use)
   - Cheatsheet → handout if the module covers a tool with cheatsheet-worthy content
4. Generate weekly assessments (2–4 questions per module, answerable from the assigned material).
5. Generate a final project brief that integrates outcomes across modules.
6. Compile a unified bibliography.

**Ranking mode: mixed (per sub-output).** Inherits the mode from each constituent generator. Learning Path is silent, Tutorial is silent, Dialog is interactive, Cheatsheet is silent. The Curriculum Generator doesn't introduce a new mode — it composes existing ones.

**Output shape:**

```text
curricula/<subject>-<audience>/
├── _curriculum.md         # Overview: outcomes, structure, prerequisites
├── _syllabus.md           # Week-by-week summary table
├── week-01-<theme>/
│   ├── reading-list.md    # From Learning Path
│   ├── exercises/         # From Tutorial
│   ├── discussion.md      # From Dialog (optional)
│   ├── cheatsheet.md      # From Cheatsheet (optional)
│   ├── assessment.md      # End-of-week assessment
│   └── checkpoint.md      # Self-check for self-paced learners
├── week-02-<theme>/
│   └── ...
├── final-project.md       # Capstone brief
├── bibliography.md        # Unified across modules
└── instructor-guide.md    # Optional: per-module facilitation notes (cohort/instructor-led)
```

**Selection strategy:** Inherits per sub-output. The composite layer adds: cross-module deduplication (don't re-assign the same chapter twice unless intentional), and prerequisite-respect verification across modules.

**Validation:**

- Every constituent sub-output passes its own validator
- No circular prerequisites across modules (week N's prerequisites are covered by weeks 1..N-1)
- Cadence holds: estimated hours per module match the stated weekly hours
- Final project covers all stated learning outcomes
- Assessment questions are answerable from assigned material (verifiable against the reading list and exercises)
- Bibliography is the union of constituent bibliographies, deduplicated

**Slash command:** `/kb-generate curriculum "<subject>" --audience "<audience>" --weeks N [--cadence "Nh/wk"] [--format self-paced|cohort|instructor-led]`

---

## 3. Schema Changes

### New tables (generalized output model)

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR NOT NULL,   -- 'skills', 'learning_path', 'content', 'tutorial',
                                        -- 'pattern_catalog', 'adr', 'tech_assessment', 'dialog',
                                        -- 'concept_map', 'cheatsheet', 'slide_deck',
                                        -- 'migration_guide', 'currency_report', 'author_panel',
                                        -- 'project_bootstrap', 'refactoring_playbook', 'curriculum'
    name            VARCHAR,
    domain          VARCHAR,
    target_audience VARCHAR,
    format          VARCHAR,            -- content: 'blog'|'talk'|'design_doc'|'chapter'
                                        -- dialog: 'podcast'|'video'|'debate'|'panel'
                                        -- pattern_catalog: 'positive'|'inverse'
                                        -- slide_deck, project_bootstrap: stack/audience variants
    created_at      TIMESTAMP,
    source_query    TEXT,
    parent_package_id BIGINT REFERENCES generated_package(package_id)
                                        -- for composite generators (curriculum) that orchestrate
                                        -- sub-packages: a curriculum's reading list is a
                                        -- learning_path package whose parent is the curriculum
);

CREATE TABLE generated_unit (
    unit_id          BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES generated_package(package_id),
    unit_type        VARCHAR NOT NULL,  -- 'skill', 'stage', 'section', 'module',
                                        -- 'exercise', 'pattern', 'anti_pattern',
                                        -- 'option', 'criterion', 'scene', 'character',
                                        -- 'assessment_dimension', 'concept_node',
                                        -- 'cheatsheet_block', 'slide', 'migration_step',
                                        -- 'currency_finding', 'character_line', 'project_file',
                                        -- 'refactor_step', 'curriculum_week'
    name             VARCHAR,
    ordinal          INTEGER,
    parent_unit_id   BIGINT REFERENCES generated_unit(unit_id),
    content_markdown TEXT,
    metadata_json    TEXT,              -- generator-specific structured data
    generation_notes TEXT
);

CREATE TABLE generated_source (
    unit_id      BIGINT REFERENCES generated_unit(unit_id),
    source_type  VARCHAR,
    source_id    BIGINT,
    score        DOUBLE,
    weight       DOUBLE,
    drop_reason  VARCHAR,
    PRIMARY KEY (unit_id, source_type, source_id)
);

CREATE TABLE generated_file (
    file_id   BIGINT PRIMARY KEY,
    unit_id   BIGINT REFERENCES generated_unit(unit_id),
    filename  VARCHAR,
    purpose   VARCHAR,
    content   TEXT
);
```

### Migration path for existing Skills tables

Option 1 (recommended for initial implementation): Keep existing `skill_*` tables as-is. New generators use `generated_*` tables. Skills Factory continues writing to `skill_*` tables. Merge later when/if the duplication becomes annoying.

Option 2 (clean but disruptive): Migrate `skill_*` data into `generated_*` tables with `generator_type='skills'`. Drop `skill_*` tables. Single provenance query path. Do this only during a natural break when no active Skills packages are in use.

### New schema needs introduced by the appended generators

Most appended generators reuse the existing `generated_*` tables with new `generator_type` and `unit_type` values — no schema changes required. The exceptions:

1. **Curriculum** introduces composite-package semantics. The `parent_package_id` column on `generated_package` (added above) lets a curriculum's weekly modules reference their constituent sub-packages (the learning path that produced the reading list, the tutorial that produced the exercises, etc.). Without it, provenance queries on a curriculum couldn't drill into "which sources backed week 3's exercises" — they'd dead-end at the curriculum's top-level package.

2. **Currency Report** and **Migration Guide** depend on Phase 4 schema additions: doc snapshot version history (`doc_snapshot.captured_at` time series, not just current state) and CONTRADICTS edges in `concept_relation`. Those land with Phase 4; the generators only consume them.

3. **Project Bootstrap** and **Refactoring Playbook** introduce per-file source mapping at finer grain than `generated_source` natively supports (each generated source file may compose 3–5 procedures). Two options: (a) generate one `generated_unit` per file with `unit_type='project_file'` and link sources via `generated_source` on that unit (recommended — fits the existing model); (b) add a `generated_file_source` link table for file-level provenance directly. Option (a) is the default unless we hit query-performance issues at scale.

---

## 4. Project Layout Changes

```text
mcp-servers/kb-mcp/
├── server.py
├── retrievers.py
├── ranking.py
├── resolution.py
├── discovery.py
├── sectionizer.py
├── tiering.py
├── generator.py              # NEW: generalized pipeline framework
├── skills_factory.py         # refactored to use generator.py
├── decomposers/              # NEW: pluggable decomposition strategies
│   ├── community.py          #   Skills Factory
│   ├── prerequisite.py       #   Learning Path + Tutorial
│   ├── rhetorical.py         #   Content + Slide-deck Outline
│   ├── pattern_cluster.py    #   Pattern + Anti-Pattern Catalog
│   ├── decision_frame.py     #   ADR Generator
│   ├── eval_matrix.py        #   Tech Assessment
│   ├── conversational.py     #   Dialog + Author Panel
│   ├── khop_expansion.py     #   Concept Neighborhood Map
│   ├── topical_condense.py   #   Cheatsheet
│   ├── era_diff.py           #   Migration Guide
│   ├── volatility_audit.py   #   Currency Report
│   ├── stack_scaffold.py     #   Project Bootstrap
│   ├── transformation_path.py#   Refactoring Playbook
│   └── composite.py          #   Curriculum (orchestrates other decomposers)
├── reconcilers/              # NEW: stage 6.5 — code-stack reconciliation
│   └── live_doc_check.py     #   Used by Bootstrap, Refactoring, Migration
├── templates/                # NEW: output templates per generator
│   ├── skill.py
│   ├── learning_stage.py
│   ├── content_section.py
│   ├── tutorial_module.py
│   ├── pattern_entry.py
│   ├── anti_pattern_entry.py
│   ├── adr_section.py
│   ├── assessment_dim.py
│   ├── dialog_scene.py       # also used by author_panel
│   ├── concept_map.py
│   ├── cheatsheet_block.py
│   ├── slide.py
│   ├── migration_step.py
│   ├── currency_finding.py
│   ├── project_file.py
│   ├── refactor_step.py
│   └── curriculum_week.py
└── validators/               # NEW: validation logic per generator
    ├── skill_validator.py
    ├── path_validator.py
    ├── content_validator.py
    ├── tutorial_validator.py
    ├── pattern_validator.py  # validates both positive and inverse catalogs
    ├── adr_validator.py
    ├── assessment_validator.py
    ├── dialog_validator.py   # also used by author_panel with N-character extensions
    ├── concept_map_validator.py
    ├── cheatsheet_validator.py
    ├── slide_deck_validator.py
    ├── migration_validator.py
    ├── currency_validator.py
    ├── project_validator.py  # includes "scaffold actually runs" smoke test
    ├── refactor_validator.py
    └── curriculum_validator.py  # orchestrator: invokes constituent validators + cross-module checks

.claude/commands/
├── ...existing commands...
├── kb-generate.md            # NEW: unified generate command
├── kb-discover-patterns.md   # NEW: pattern discovery mode
└── kb-discover-anti-patterns.md  # NEW: anti-pattern discovery mode

data/
├── catalog.ddb
├── generated-packages/       # existing Skills output
├── learning-paths/           # Phase 8
├── content/                  # Phase 9
├── cheatsheets/              # Phase 9
├── slides/                   # Phase 9
├── tutorials/                # Phase 10
├── patterns/                 # Phase 11 (positive + inverse, sibling dirs)
├── decisions/                # Phase 12 (ADRs)
├── assessments/              # Phase 12 (Tech Assessments)
├── migrations/               # Phase 13
├── currency-reports/         # Phase 13
├── dialogs/                  # Phase 14 (Scripts)
├── panels/                   # Phase 14 (Author Panel)
├── project-scaffolds/        # Phase 15 (Project Bootstrap)
├── refactor-playbooks/       # Phase 15
├── curricula/                # Phase 16
└── concept-maps/             # Phase 7 (visualization)
```

---

## 5. MCP Server Tool Additions

```python
# Generalized generation entry point
generate_package(
    generator_type,  # 'skills' | 'learning_path' | 'content' | 'tutorial' |
                     # 'pattern_catalog' | 'adr' | 'tech_assessment' | 'dialog' |
                     # 'concept_map' | 'cheatsheet' | 'slide_deck' |
                     # 'migration_guide' | 'currency_report' | 'author_panel' |
                     # 'project_bootstrap' | 'refactoring_playbook' | 'curriculum'
    domain,
    target_audience=None,
    format=None,           # content: 'blog'|'talk'|'design_doc'; dialog: 'podcast'|'video'|'debate'
                           # pattern_catalog: 'positive'|'inverse'
    start_knowledge=None,  # for learning paths + curriculum
    target_knowledge=None, # for learning paths + curriculum
    skill_level=None,      # for tutorials
    constraints=None,      # for ADRs: list of decision constraints
    characters=None,       # for dialog: 2 or 3; or custom; for author_panel: 3-6 with profiles
    target_minutes=None,   # for dialog/panel/slides: target length
    depth=None,            # for concept_map: hop depth
    edge_filter=None,      # for concept_map: edge types to include
    from_era=None,         # for migration_guide
    to_era=None,           # for migration_guide
    time_window=None,      # for currency_report
    design=None,           # for project_bootstrap: graph-anchored design concept(s)
    stack=None,            # for project_bootstrap, refactoring_playbook
    source_pattern=None,   # for refactoring_playbook
    target_pattern=None,   # for refactoring_playbook
    weeks=None,            # for curriculum
    cadence=None,          # for curriculum
    course_format=None,    # for curriculum: 'self-paced'|'cohort'|'instructor-led'
    strategy_hint=None
)

# Learning path specific
analyze_knowledge_gaps(start_concepts, target_concepts)

# Pattern catalog specific
discover_patterns(domain, min_evidence=2, inverse=False)
# Returns: candidate patterns (or anti-patterns when inverse=True) with evidence counts

# Tech assessment specific
assess_graph_coverage(concept_name)
# Returns: source count by type, coverage depth, currency status, graph neighborhood size

# Dialog + Author Panel specific
identify_tension_points(domain, min_sources_per_side=2, character_profiles=None)
# Returns: concept pairs where ranking diverges across the supplied character profiles
# (default profiles: Architect/Practitioner; or N>2 custom profiles for Author Panel)

# Concept Neighborhood Map specific
expand_concept_neighborhood(concept_name, depth=2, edges=None, max_nodes=60)
# Returns: nodes + edges within `depth` hops, pruned to `max_nodes` if needed

# Migration Guide + Currency Report specific (Phase 4-dependent)
find_era_contradictions(concept_name, from_year, to_year)
# Returns: CONTRADICTS edges where one endpoint is pre-from_year and the other post-to_year
compute_currency_index(concept_name, time_window_months=12)
# Returns: composite staleness score 0-100 + breakdown (age, drift, contradictions)

# Project Bootstrap + Refactoring Playbook specific
reconcile_with_live_docs(procedure_ids, stack)
# For each procedure, fetch the relevant live doc snapshot and verify commands/APIs/configs
# still match. Returns drift report per procedure: clean | needs-update | unresolvable.

# Curriculum specific
plan_curriculum_modules(subject, audience, weeks, cadence)
# Composite planner: returns a week-by-week module breakdown with dispatch hints
# for each constituent generator (learning_path, tutorial, dialog, cheatsheet).
```

### Slash command summary (§5.1)

| Command | Generator | Phase |
|---|---|---|
| `/kb-generate-skills <domain>` | Skills Factory | (existing, Phase 5) |
| `/kb-generate concept-map <concept>` | Concept Neighborhood Map | 7 |
| `/kb-generate learning-path "<from> to <to>"` | Learning Path | 8 |
| `/kb-generate content <topic> --format ...` | Content | 9 |
| `/kb-generate cheatsheet <subject>` | Cheatsheet | 9 |
| `/kb-generate slides <topic> --minutes N` | Slide-deck Outline | 9 |
| `/kb-generate tutorial <topic> --level ...` | Tutorial | 10 |
| `/kb-generate patterns <domain> [--inverse]` | Pattern + Anti-Pattern Catalog | 11 |
| `/kb-discover-patterns <topic>` | Pattern Catalog (exploratory) | 11 |
| `/kb-discover-anti-patterns <topic>` | Anti-Pattern Catalog (exploratory) | 11 |
| `/kb-generate adr <context> --constraints ...` | ADR | 12 |
| `/kb-generate assessment <technology>` | Tech Assessment | 12 |
| `/kb-generate migration <subject> --from ... --to ...` | Migration Guide | 13 |
| `/kb-generate currency-report <concept-or-domain>` | Currency Report | 13 |
| `/kb-generate dialog <topic> --characters 2 or 3 ...` | Dialog | 14 |
| `/kb-generate author-panel <topic> --characters ...` | Author Panel | 14 |
| `/kb-generate project "<design> using <stack>"` | Project Bootstrap | 15 |
| `/kb-generate refactor "<source> to <target>"` | Refactoring Playbook | 15 |
| `/kb-generate curriculum <subject> --weeks N ...` | Curriculum | 16 |

---

## 6. Execution Plan — Generator Phases

**Prerequisite:** Phase 5 (Skills Factory) complete and validated. The generator framework refactoring depends on having a working reference implementation.

### Phase summary

| Phase | Generators | Approx. weeks | Hard prerequisites |
|---|---|---:|---|
| 7 | Framework refactor + Concept Neighborhood Map | 2 | Phase 5 |
| 8 | Learning Path | 3 | Phase 7 |
| 9 | Content + Cheatsheet + Slide-deck Outline | 3 | Phase 7 |
| 10 | Tutorial / Workshop | 2 | Phase 8 (prerequisite decomposer) |
| 11 | Pattern + Anti-Pattern Catalog | 3 | Phase 7 |
| 12 | ADR + Tech Assessment | 3 | Phase 11 (CONTRASTS_WITH usage) |
| 13 | Migration Guide + Currency Report | 2 | Phase 4 (CONTRADICTS edges + snapshot history) |
| 14 | Dialog + Author Panel | 3 | Phase 7 |
| 15 | Project Bootstrap + Refactoring Playbook | 3 | Phase 11 + Phase 13 (currency-aware ranking) |
| 16 | Curriculum Generator | 2 | Phases 8, 9, 10, 14 (composite) |
| 17 | Integration + Final Regression | 1 | All prior |

**Total: ~27 weeks after Phase 6.** Each phase is independently useful — you can stop after any phase and have working generators for everything built so far.

### Phase 7: Generator Framework + Concept Neighborhood Map (week 16–18)

The framework refactor is the spine; the Concept Neighborhood Map is its first non-Skills concrete generator and serves as the "does the framework actually work for new generator types?" smoke test before we invest in larger generators.

#### Prompt 7.1 — Refactor Skills Factory into generator framework

```text
Refactor mcp-servers/kb-mcp/skills_factory.py into a generalized
generator framework.

Extract the seven-stage pipeline into generator.py as a Generator base
class with pluggable components:
  - Decomposer (stage 2)
  - Planner (stage 3)
  - ranking_mode selection (stage 5)
  - OutputTemplate (stage 6)
  - Validator (stage 7)
  - Materializer (stage 7)

The Skills Factory becomes the first concrete implementation:
  - decomposers/community.py (extracted from skills_factory.py)
  - templates/skill.py
  - validators/skill_validator.py

After refactoring, the Skills Factory should work EXACTLY as before.
Run the Phase 5 Skills eval to verify zero regression.

Also add the generalized output tables (generated_package, generated_unit,
generated_source, generated_file) to the schema. The Skills Factory
continues using the existing skill_* tables for now — migration comes later.
```

**Validate:**

- Skills Factory eval passes with identical scores to pre-refactoring baseline
- `/kb-generate-skills` still works end-to-end
- Generator base class is clean and extensible
- New tables exist in the schema

🔀 Commit: `refactor(phase7): extract generator framework from Skills Factory`

#### Prompt 7.2 — Concept Neighborhood Map generator (framework smoke test)

```text
Build the Concept Neighborhood Map generator on top of the refactored
framework. This is a deliberately small generator chosen to validate
that the framework supports non-Skills generators with minimal friction.

1. Build decomposers/khop_expansion.py:
   - Resolve seed concept via EntityResolver
   - Expand outward via DuckPGQ traversal up to `depth` hops
   - Collect nodes and edges; group nodes by source-type coverage
   - Cap node count (default 60); prune by edge weight; annotate truncation
2. Build templates/concept_map.py:
   - Render Mermaid (.mmd), Graphviz DOT (.dot), and pre-rendered SVG
   - Color nodes by coverage type, style edges by relation type
   - Emit nodes.csv for analysis
3. Build validators/concept_map_validator.py:
   - All node IDs resolve to real graph concepts
   - All edges correspond to real concept_relation rows
   - Mermaid parses (mmdc dry-run)
   - Pruned-node count is explicit when truncation occurs
4. Register as generator_type='concept_map'
5. Build /kb-generate concept-map slash command
6. Generate maps for 3 test concepts spanning depth-1, depth-2, and a
   highly-connected concept that triggers pruning

Use Context7 to verify the current DuckPGQ traversal syntax.
```

**Validate:**

- All three test maps render correctly (commit the SVG and visually inspect)
- Pruning logic kicks in cleanly for the high-fan-out concept
- The generator was straightforward to add — if it required hacking the framework, the framework needs more work before Phase 8

🔀 Commit: `feat(phase7): concept neighborhood map generator + framework validation`

---

### Phase 8: Learning Path Generator (week 18–21)

#### Prompt 8.1 — Prerequisite decomposer

```text
Build decomposers/prerequisite.py — the decomposer for learning paths.

Given start concepts and target concepts:
1. Match both to existing graph nodes via entity resolution
2. Run DuckPGQ ANY SHORTEST PATH from each start to each target
   following REQUIRES and EXTENDS edges
3. Merge paths into a unified concept sequence
4. LLM refinement: group into learning stages (3-7 concepts per stage),
   name each stage, identify checkpoint boundaries

Use Context7 to verify the current DuckPGQ shortest path syntax.

Test with three scenarios:
1. "SQL basics" → "distributed CDC pipeline design" (long path, multi-stage)
2. "Python" → "data modeling with dbt" (medium path)
3. "Kubernetes basics" → "Kubernetes operator patterns" (short, focused)

Verify: paths follow logical prerequisite order, stages group coherently,
no circular dependencies.
```

**Validate:**

- Prerequisite paths make pedagogical sense (human review)
- Stages are coherent groupings (not random splits)
- Different scope requests produce appropriately-sized paths

🔀 Commit: `feat(phase8): prerequisite decomposer for learning paths`

#### Prompt 8.2 — Gap analysis and reading list assembly

```text
Build the gap analysis and reading list components for learning paths.

For each concept in the prerequisite chain:
1. Find book chapters with DISCUSSES edges → reading assignments
2. Find procedures linked to this concept → practice exercises
3. Find doc sections covering this concept → supplements
4. Flag concepts with NO book coverage → gap report entries
5. Rank sources using generation mode (silent, authority pick for
   foundations, recent-doc for tech-specific)

Assemble into per-stage reading lists with:
- Specific chapter references ("Kleppmann, Ch.11, pp. 451-468")
- Context for WHY this chapter is assigned (not just "it covers CDC")
- Exercise suggestions adapted from procedures
- Checkpoint questions generated from the stage's concepts

Test: generate a complete learning path for "SQL → CDC pipeline design".
Review every reading assignment — is it the RIGHT chapter from the RIGHT
book for this stage?
```

**Validate:**

- Reading assignments match the stage's concepts (not generic suggestions)
- Gaps are correctly identified
- Checkpoint questions are answerable from the assigned reading
- No reading assignment appears in a stage before its prerequisites are covered

🔀 Commit: `feat(phase8): gap analysis and reading list assembly`

#### Prompt 8.3 — Full learning path generation and command

```text
Wire the learning path generator end-to-end:
1. Build templates/learning_stage.py
2. Build validators/path_validator.py (prerequisite completeness,
   no circular deps, every stage has reading assignments)
3. Register as generator_type='learning_path' in the framework
4. Build /kb-generate command (unified) that dispatches to the right
   generator based on type argument
5. Generate a complete learning path:
   /kb-generate learning-path "SQL and Python to distributed CDC pipelines"
6. Materialize to data/learning-paths/

Review the full output package. Is this a curriculum you'd actually follow?
```

**Validate:**

- End-to-end generation works
- Output structure matches the spec (_path.md, stages, reading lists, checkpoints)
- Provenance recorded in generated_source
- Path validator catches intentionally-broken test cases

🔀 Commit: `feat(phase8): learning path generator end-to-end`

#### Prompt 8.4 — Learning path eval

```text
Create tests/eval/learning_path_eval.py:

1. Generate paths for 5 different start→target combinations spanning
   different domains (data engineering, distributed systems, cloud, etc.)
2. For each path, check:
   - Prerequisite completeness: every stage's concepts have prereqs
     covered in earlier stages
   - Reading assignment relevance: the assigned chapter actually covers
     the stage's concepts (verify via concept_relation edges)
   - Gap accuracy: flagged gaps don't have hidden book coverage
   - Stage sizing: 3-7 concepts per stage, no single-concept stages
   - Total path length: reasonable given the scope (not 50 stages for
     a focused topic)
3. Report a path quality score

Use the autoresearch loop to tune the stage-grouping prompt.
```

**Validate:**

- Eval runs successfully
- Path quality baseline established
- At least one tuning iteration completed

🔀 Commit: `feat(phase8): learning path eval with autoresearch baseline`

---

### Phase 9: Content + Cheatsheet + Slide-deck Outline (week 21–24)

These three generators form a "human-readable output" family. Content is the primary new generator; Cheatsheet and Slide-deck reuse the rhetorical decomposer (or, for Cheatsheet, a small new condensation decomposer) with different templates. Bundling them avoids three short phases when most of the infrastructure is shared.

#### Prompt 9.1 — Rhetorical decomposer

```text
Build decomposers/rhetorical.py — the decomposer for content generation.

Unlike prerequisite or community decomposers, this one is LLM-driven
with retrieval context. The flow:
1. Broad hybrid retrieval across the topic (cast wide)
2. LLM-driven outline proposal given: topic, audience, format, angle,
   and a summary of retrieved sources (titles, concepts covered,
   currency status)
3. Outline follows format conventions:
   - Blog post: hook → context → problem → approaches → recommendation
   - Conference talk: opening → framing → 3 insights → demo → takeaways
   - Design doc: context → requirements → options → analysis → decision
4. User reviews outline before generation proceeds

Test with three requests:
1. Blog post: "Modern CDC approaches for data engineers"
2. Conference talk: "From batch to streaming: a migration story"
3. Design doc: "CDC pipeline architecture for our data platform"

The decomposer should produce different outlines for each format even
when the topic is similar.
```

**Validate:**

- Outlines follow format conventions
- Different formats produce structurally different outlines for the same topic
- Source summary is useful (not just a list of titles)

🔀 Commit: `feat(phase9): rhetorical decomposer for content generation`

#### Prompt 9.2 — Draft generation with interactive ranking

```text
Build the content generation stage using INTERACTIVE ranking mode.

This is the key difference from Skills: the draft includes provenance
annotations and conflict notes inline:

  "Log-based CDC reads the database transaction log directly rather than
  polling tables for changes. [source: Kleppmann Ch.11, score: 0.94]
  This approach is now considered best practice, though older references
  [source: ETL Patterns 2019, score: 0.68] still recommend trigger-based
  approaches for simpler use cases. [conflict: book predates DLT; current
  Databricks docs recommend Delta Live Tables for all new CDC work]"

Build templates/content_section.py that:
1. Retrieves sources per outline section
2. Ranks in interactive mode (conflicts surfaced)
3. Generates draft prose with inline annotations
4. Collects all conflicts into a separate notes.md
5. Generates supporting assets (comparison tables, code examples)

Test: generate a full blog post draft on "Modern CDC approaches".
Review: are the annotations useful? Do the conflicts highlight genuine
editorial decisions the author needs to make?
```

**Validate:**

- Draft is readable prose (not a collection of quotes)
- Provenance annotations are specific and accurate
- Conflicts represent genuine disagreements, not noise
- notes.md is actionable (clear decisions needed from author)
- Code examples are current (verified against doc snapshots)

🔀 Commit: `feat(phase9): content generator with interactive ranking and annotations`

#### Prompt 9.3 — Full content generator and eval

```text
Wire end-to-end:
1. Build validators/content_validator.py (source coverage, currency
   flags, code example validity, comparison table completeness)
2. Register as generator_type='content'
3. Generate three complete content projects (blog, talk, design doc)
4. Materialize to data/content/

Build tests/eval/content_eval.py:
1. For each generated draft, check:
   - Source diversity (not all from one book)
   - Conflict identification accuracy (spot-check 10 conflict notes)
   - Code example currency (compare against doc snapshots)
   - Outline coverage (every section has substance, no empty stubs)
2. Report content quality score
```

**Validate:**

- Three distinct content projects generated successfully
- Eval baseline established

🔀 Commit: `feat(phase9): content generator end-to-end with eval`

#### Prompt 9.4 — Cheatsheet generator

```text
Build the Cheatsheet generator. The decomposer is small and new
(decomposers/topical_condense.py) but the framework + retrieval +
ranking infrastructure is shared with Content.

1. Build decomposers/topical_condense.py:
   - Resolve subject to a graph concept
   - Pull all procedures linked to subject + EXTENDS descendants
   - Cluster procedures by topic (CRUD, config, performance, errors, integration)
   - Distill each cluster to a one-liner: canonical command + "when to use"
   - Aggregate failure_modes across procedures into a "gotchas" section
2. Build templates/cheatsheet_block.py:
   - Render the one-page cheatsheet.md
   - Emit _provenance.md (per-line source pointer) and _gotchas.md
3. Build validators/cheatsheet_validator.py:
   - Page-fit heuristic (<= 1,200 words, <= 8 visible sections)
   - Every command verified against current doc snapshot
   - No conflicting recommendations within a cluster
4. Register as generator_type='cheatsheet'
5. Generate cheatsheets for 3 subjects (e.g., DuckDB, kubectl, Polars)
6. Materialize to data/cheatsheets/

Selection mode: STRICTLY recent-doc anchored for syntax. Books only
contribute to the gotchas section.
```

**Validate:**

- Each cheatsheet fits on one printed page
- All commands parse correctly against current docs
- Gotchas section reflects real recurring failure modes

🔀 Commit: `feat(phase9): cheatsheet generator`

#### Prompt 9.5 — Slide-deck Outline generator

```text
Build the Slide-deck Outline generator. Reuses decomposers/rhetorical.py
with a bullet-condensation template.

1. Extend decomposers/rhetorical.py with a 'slides' format mode that
   maps narrative arc beats to slide groups (opening, agenda, per-insight
   3-6 slides, demo, takeaways, Q&A)
2. Build templates/slide.py:
   - Per-slide bullets (3-5 per slide, < 10 words each)
   - Per-slide presenter notes (1-3 sentences)
   - Visual suggestions where bullets are abstract
3. Build validators/slide_deck_validator.py:
   - Slide count matches duration (~1 min per content slide + 20% buffer)
   - No slide has > 5 bullets or any bullet > 10 words
   - Presenter notes are < 3 sentences per slide
   - Code-snippet slides reference real corpus files
4. Register as generator_type='slide_deck'
5. Generate slide outlines for 3 talks of varying duration
6. Materialize to data/slides/
```

**Validate:**

- Slide-density rules respected
- Narrative arc reads correctly when bullets are read in sequence
- Presenter notes are usable on the day of the talk

🔀 Commit: `feat(phase9): slide-deck outline generator`

#### Prompt 9.6 — Phase 9 family eval

```text
Build tests/eval/phase9_family_eval.py covering content, cheatsheet,
and slide-deck:
1. Generate one of each for the same subject (CDC) and verify the
   three outputs are appropriately different (densities, structures)
2. Cross-format coherence check: do the three outputs agree on the
   factual claims? Discrepancies indicate ranking-drift bugs in
   the family.
3. Per-generator metric: see prior eval prompts (content_eval,
   cheatsheet_validator outcomes, slide_deck_validator outcomes)
```

🔀 Commit: `feat(phase9): family eval covering content, cheatsheet, slides`

---

### Phase 10: Tutorial Generator (week 24–26)

#### Prompt 10.1 — Exercise sequencing and procedure adaptation

```text
Build the tutorial decomposer and exercise generation.

The decomposer (decomposers/prerequisite.py extended) does:
1. Prerequisite traversal (like learning paths)
2. Filter to concepts with associated procedures — no procedure = no exercise
3. Group into modules (1-2 concepts, 1-3 exercises each)
4. Order so each module builds on previous outputs

Exercise generation adapts procedures into teaching format:
- Add setup steps (what to install, configure, prepare)
- Add expected output after each step
- Add "what if it doesn't work" troubleshooting
- Add "why are we doing this" context from book concepts
- Verify all commands/APIs against current doc snapshots

Selection strategy: STRICTLY recent-doc anchored for any step involving
a command, API call, or configuration. Books provide only the "why" context.

Test: generate a tutorial for "Getting started with CDC using Debezium"
(or a technology with good procedure coverage in your library).
```

**Validate:**

- Exercises have concrete, executable steps
- Expected outputs are specific
- Troubleshooting covers common failure modes
- API calls match current documentation
- Prerequisite ordering is correct

🔀 Commit: `feat(phase10): tutorial generator with exercise sequencing`

#### Prompt 10.2 — Full tutorial generator with solutions and eval

```text
Complete the tutorial generator:
1. Build solution generation (complete solutions for every exercise)
2. Build setup/prerequisites.md generation
3. Build validators/tutorial_validator.py:
   - Every exercise references a procedure
   - Commands/APIs verified against doc snapshots
   - Prerequisites listed for all tools mentioned
   - Solutions match exercise steps
   - Module ordering respects prerequisites
4. Register as generator_type='tutorial'
5. Generate a complete tutorial workshop
6. Materialize to data/tutorials/

Build tests/eval/tutorial_eval.py:
1. Verify exercise executability (commands parse correctly)
2. Verify solution completeness
3. Verify prerequisite ordering
4. Report tutorial quality score
```

**Validate:**

- Complete tutorial with solutions generated
- Eval baseline established

🔀 Commit: `feat(phase10): tutorial generator end-to-end with eval`

---

### Phase 11: Pattern + Anti-Pattern Catalog Generator (week 26–29)

Patterns and anti-patterns share the same backend. Both are built in this phase, with the inverse (anti-pattern) mode added as an additional sub-prompt rather than a separate generator.

#### Prompt 11.1 — Pattern discovery via graph clustering

```text
Build decomposers/pattern_cluster.py — discovers patterns from the
concept graph.

The discovery process:
1. Query for concept clusters connected by IMPLEMENTS edges
2. Each cluster = a candidate pattern: one concept (the pattern) linked
   to multiple procedures (implementations)
3. Filter: require at least 2 independent sources (cross-author evidence)
4. Cross-reference against existing YAML pattern library — skip duplicates
5. LLM refinement: name the pattern, draft problem statement, identify
   key trade-offs from how different sources discuss it

Test with: /kb-discover-patterns "data integration"
Review: are the discovered patterns real patterns? Or are they false
positives (unrelated concepts that happen to cluster)?
```

**Validate:**

- Discovered patterns are recognizable and real
- False positive rate < 30%
- Duplicate detection against existing library works
- At least 3 genuine patterns discovered from the test domain

🔀 Commit: `feat(phase11): pattern discovery via graph clustering`

#### Prompt 11.2 — Pattern documentation with multi-perspective analysis

```text
Build the pattern documentation generator using INTERACTIVE ranking.

For each discovered pattern:
1. Retrieve all source discussions (chapters, doc sections)
2. Rank in interactive mode — surface perspectives and trade-offs
3. Generate:
   - pattern.yaml (structured: name, problem, solution, trade-offs, aliases)
   - discussion.md (multi-perspective analysis showing how different
     authors treat the pattern — "Kleppmann emphasizes X, the Databricks
     guide recommends Y for lakehouse contexts, these differ because...")
   - implementations/ (one file per known implementation, linked to procedures)
   - related.md (CONTRASTS_WITH, EXTENDS links to other patterns)
4. Validate against a pattern YAML schema

This generator explicitly WANTS disagreements surfaced — pattern
trade-offs are the content, not noise to be resolved.

Test: generate a pattern catalog for "stream processing patterns".
```

**Validate:**

- Trade-offs section contains genuine design tensions
- Multiple author perspectives are represented
- Implementation links point to valid procedures
- pattern.yaml validates against schema
- related.md uses correct edge types

🔀 Commit: `feat(phase11): pattern catalog generator with multi-perspective analysis`

#### Prompt 11.3 — Full pattern catalog and eval

```text
Complete the pattern catalog generator:
1. Build validators/pattern_validator.py
2. Register as generator_type='pattern_catalog'
3. Generate a complete pattern catalog for a domain
4. Materialize to data/patterns/

Build tests/eval/pattern_eval.py:
1. Pattern discovery precision (what fraction are real patterns)
2. Evidence sufficiency (every pattern has 2+ independent sources)
3. Trade-off quality (genuine tensions, not restated descriptions)
4. Implementation coverage (how many patterns have linked procedures)
5. Report pattern catalog quality score

Also update the existing YAML pattern library to cross-reference
generated patterns — they should complement, not replace, the
manually-curated patterns.
```

**Validate:**

- Complete catalog generated
- Eval baseline established
- No conflicts with existing pattern library

🔀 Commit: `feat(phase11): pattern catalog generator end-to-end with eval`

#### Prompt 11.4 — Anti-pattern (inverse) catalog mode

```text
Add inverse mode to the Pattern Catalog generator. Same backend, two key changes:
1. Seed query: concepts tagged or discussed as "anti-pattern", "smell",
   "avoid", "deprecated", "considered harmful". Filter the cluster query
   accordingly.
2. Output template (templates/anti_pattern_entry.py):
   - anti-pattern.yaml: name, smell, why-it-happens, harm, replace-with
   - replace-with.md: pointer(s) to positive patterns from the same catalog
   - examples.md: sourced examples (sanitized) of the anti-pattern in the wild

Extend validators/pattern_validator.py to support inverse mode:
- Every anti-pattern has 2+ independent sources warning against it
- Every anti-pattern has a replace_with pointer (or explicit "no canonical
  replacement" with rationale)
- Harm claims are sourced
- Anti-pattern is not merely "older pattern" — it's actively warned-against,
  distinguished from "deprecated by recency" (which belongs in Currency Report)

Add /kb-discover-anti-patterns slash command.

Generate an anti-pattern catalog for "data integration" alongside the
positive pattern catalog. The two should be cross-referenced: each
positive pattern lists anti-patterns it replaces; each anti-pattern lists
its replacement patterns.
```

**Validate:**

- Anti-pattern catalog generated as sibling directory to positive catalog
- Cross-references between positive and inverse catalogs are bidirectional
- No straw-man anti-patterns (every entry has multiple sources warning against it)
- replace_with pointers resolve to real positive-pattern entries

🔀 Commit: `feat(phase11): anti-pattern (inverse) catalog mode`

---

### Phase 12: ADR and Tech Assessment Generators (week 29–32)

ADR and Technical Assessment are structurally similar — both are evaluation-oriented, both use interactive ranking, both leverage CONTRASTS_WITH edges. Building them together.

#### Prompt 12.1 — Decision framing decomposer (ADR)

```text
Build decomposers/decision_frame.py — the decomposer for ADRs.

Given a decision context and constraints:
1. Identify the core decision concept(s) via entity resolution
2. Traverse CONTRASTS_WITH and IMPLEMENTS edges to find candidate options
3. For each option, collect its graph neighborhood (REQUIRES, EXTENDS,
   integrations)
4. Derive evaluation criteria from user constraints + source-derived
   dimensions (recurring themes across sources about these options)
5. Structure as standard ADR sections

Test: generate the decision frame for "CDC solution for a Databricks
lakehouse, team has no JVM experience."
Verify: options come from the graph (not invented), criteria include
both user constraints and source-derived dimensions.
```

**Validate:**

- Options are real graph concepts with CONTRASTS_WITH relationships
- Criteria reflect both user constraints and source-derived concerns
- Consequences follow real REQUIRES edges

🔀 Commit: `feat(phase12): decision framing decomposer for ADRs`

#### Prompt 12.2 — ADR generator with options matrix

```text
Build the full ADR generator:
1. templates/adr_section.py — generates each ADR section with provenance
2. Options matrix with per-option pros/cons from interactive ranking
3. Recommendation logic: top-ranked option with rationale, OR explicit
   "no clear winner" when sources genuinely conflict
4. Consequences from REQUIRES edges (choosing X means you also need Y, Z)
5. validators/adr_validator.py
6. Register as generator_type='adr'

Generate: /kb-generate adr "CDC solution for Databricks lakehouse"
        --constraints "must support schema evolution, no JVM, integrate with Kafka"

Review the ADR. Is it a document you'd actually use in a design review?
```

**Validate:**

- ADR follows standard template (status, context, options, criteria, analysis, decision)
- Every pro/con traces to a source
- Consequences are real dependency chains
- Currency flags present where relevant

🔀 Commit: `feat(phase12): ADR generator end-to-end`

#### Prompt 12.3 — Evaluation matrix decomposer (Tech Assessment)

```text
Build decomposers/eval_matrix.py — the decomposer for technical assessments.

Given a technology to assess:
1. Find or auto-discover the concept in the graph
2. Map its full neighborhood (REQUIRES, EXTENDS, IMPLEMENTS, CONTRASTS_WITH)
3. Compute coverage metrics:
   - Source count by type (book chapters vs. doc sections)
   - Evidence depth (how many independent authors discuss it)
   - Currency status (oldest and newest sources)
   - Graph connectivity (how central is this concept)
4. Derive assessment dimensions from neighborhood + user input
5. For each dimension, assemble sources ranked in interactive mode

Test: assess "DuckDB" — should produce a rich assessment given your
library's coverage. Then assess a recently auto-discovered technology
with thin coverage — should honestly report the coverage gaps.
```

**Validate:**

- Coverage metrics are accurate (spot-check against manual counts)
- Assessment dimensions are comprehensive
- Thin coverage is honestly flagged, not hidden

🔀 Commit: `feat(phase12): evaluation matrix decomposer for tech assessments`

#### Prompt 12.4 — Tech Assessment generator and eval

```text
Complete the tech assessment generator:
1. templates/assessment_dim.py
2. validators/assessment_validator.py
3. Register as generator_type='tech_assessment'
4. Generate assessments for 3 technologies:
   - Well-covered (DuckDB — many books + docs)
   - Moderately covered (FastMCP — docs mainly)
   - Thinly covered (a recently auto-discovered library)

Build tests/eval/assessment_eval.py:
1. Coverage report accuracy (verify source counts)
2. Risk identification quality (non-obvious risks found?)
3. Alternatives completeness (from CONTRASTS_WITH, not invented)
4. Honest handling of thin coverage

Also build tests/eval/adr_eval.py:
1. Options from graph (not invented)
2. Criteria completeness
3. Pro/con provenance accuracy
4. Consequence chain validity
```

**Validate:**

- Both generators work end-to-end
- Eval baselines established for both

🔀 Commit: `feat(phase12): tech assessment generator with ADR and assessment evals`

---

### Phase 13: Migration Guide + Currency Report (week 32–34)

These two generators are bundled because they share the same Phase 4 dependency (CONTRADICTS edges + doc-snapshot version history) and the same volatility-audit infrastructure. Migration Guide is the per-subject narrative output; Currency Report is the portfolio-scope quantitative output. Building them together prevents redundant scaffolding around the same era-diff queries.

**Hard dependency:** Phase 4 must have shipped (CONTRADICTS edges populated, doc snapshots versioned with `captured_at` history). Without Phase 4 these generators have no signal to operate on.

#### Prompt 13.1 — Era-diff decomposer

```text
Build decomposers/era_diff.py — the core query infrastructure shared
between Migration Guide and Currency Report.

Given a subject concept and from/to era cutoffs:
1. Partition concept_relation edges and source rows into pre-from-era,
   post-to-era, and "between" buckets based on book.publication_year
   and doc_snapshot.captured_at.
2. Find CONTRADICTS edges where one endpoint is in pre-from-era set and
   the other in post-to-era set.
3. For each contradicting pair, resolve: book-era statement,
   current-era statement, evidence_quote (or re-query the relevant doc
   section if not yet cached).
4. Compute existence diffs: concepts present in one era's source set
   but not the other → "deprecated since" / "new since" entries.
5. Detect breaking-change candidates: concepts where API/config signatures
   differ between eras (parse-check on procedure steps).

Build the MCP tool find_era_contradictions wrapping this logic.
Use Phase 4's CONTRADICTS edge type — verify it's populated for at
least 50 subjects before this phase begins.
```

**Validate:**

- Spot-check 10 contradicting pairs against the underlying graph
- Existence-diff entries are correctly classified (no false positives where
  the concept exists under a different name in both eras)
- Breaking-change detection correlates with real API renames in test set

🔀 Commit: `feat(phase13): era-diff decomposer for migration + currency analysis`

#### Prompt 13.2 — Migration Guide generator

```text
Build the Migration Guide generator on top of the era-diff decomposer.

1. templates/migration_step.py:
   - Per change: "Was: X (Book Ch.4); Now: Y (current docs); Why: Z"
   - Group by sub-topic (config-model, API renames, deprecated features,
     new defaults, security changes)
2. Sub-files: deprecated.md, new-since.md, breaking.md (with reproducers)
3. Build validators/migration_validator.py:
   - Every change has both a "was" and a "now" source
   - "Was" sources predate "now" sources by at least 2 years
   - Breaking-change entries have a reproducer or example
   - All changes trace to a CONTRADICTS edge (no invented changes)
4. Register as generator_type='migration_guide'
5. Generate a migration guide for a subject with rich era coverage
   (e.g., "from Kafka 2017 book to current Kafka docs").

Selection: book-era source for the "was" statement, current doc for the
"now" statement, most-recent corroborating book or doc for the "why".
```

**Validate:**

- Migration guide is usable: a reader who learned the subject from the
  from-era source can update their mental model from the guide alone
- Breaking changes have reproducers
- Sources span both eras (no all-book or all-doc guides)

🔀 Commit: `feat(phase13): migration guide generator`

#### Prompt 13.3 — Currency Report generator

```text
Build the Currency Report generator. Reuses the era-diff decomposer +
adds quantitative scoring.

1. Build decomposers/volatility_audit.py:
   - For each concept in scope, compute:
     - Most-recent source date
     - Source-age distribution
     - CONTRADICTS edge count between pre-N-years and post-N-years sources
     - Doc snapshot delta count in time_window
     - Procedure drift ratio (procedures with steps no longer matching docs)
   - Score each concept on a 0-100 currency index
2. Build the MCP tool compute_currency_index wrapping the calculation.
3. templates/currency_finding.py:
   - Per concept: index, age stats, drift counts, suggested refresh actions
   - Top-N "high-priority" concepts: most-stale × most-used (combine
     staleness with usage signal)
4. validators/currency_validator.py:
   - Currency-index calculation is reproducible (golden-file test)
   - Drift counts are re-verifiable
   - High-priority list correctly combines staleness with usage
5. Register as generator_type='currency_report'
6. Generate a currency report for the user's primary domain.

Selection: not strategy-driven — the report includes everything in scope.
Ranking is by computed currency index, not source authority.
```

**Validate:**

- Currency index calculation matches a golden-file fixture
- High-priority list surfaces concepts the user agrees are stale
- Refresh suggestions are actionable (specific docs to re-snapshot,
  specific concepts to acquire books for)

🔀 Commit: `feat(phase13): currency report generator with quantitative scoring`

---

### Phase 14: Dialog + Author Panel Generator (week 34–37)

The most architecturally novel generator. Characters are view functions over the ranking engine; their disagreements are driven by actual source conflicts. Author Panel is the N>2 extension built on the same backend.

#### Prompt 14.1 — Character system and tension point identification

```text
Build the character system for dialog generation.

Character definition:
- Each character has a name, voice description, and a ranking weight
  profile that biases which sources they draw from
- The Architect: authority_weight=0.55, corroboration_weight=0.25,
  recency_weight=0.10 — favors books, canonical references
- The Practitioner: recency_weight=0.40, doc_alignment_weight=0.35,
  authority_weight=0.10 — favors current docs, procedures
- The Explorer: balanced weights, no bias — asks about gaps and tensions
- Custom characters can override any weight profile

Build the tension point identifier (MCP tool: identify_tension_points):
- For a given topic, find concepts where sources that would rank high
  for the Architect disagree with sources that would rank high for
  the Practitioner
- These are the natural debate points — real disagreements between
  foundational knowledge and current practice

Test: identify tension points for "CDC approaches".
Expected: log-based vs. trigger-based (books split on this), managed
vs. self-hosted (docs favor managed, older books favor self-hosted),
exactly-once semantics (theoretical vs. practical perspectives).
```

**Validate:**

- Tension points represent genuine source disagreements
- Character weight profiles produce meaningfully different rankings for the same query
- At least 3 tension points identified for a mid-complexity topic

🔀 Commit: `feat(phase14): character system and tension point identification`

#### Prompt 14.2 — Conversational arc decomposer

```text
Build decomposers/conversational.py — structures a dialog from topic
and tension points.

The conversational arc:
1. Opening scene: one character introduces the topic, the other reframes
   it from their perspective. Grounds the audience.
2. Exploration scenes (2-4): each centers on a tension point. Characters
   present their sourced perspective, debate, reach agreement or
   productive disagreement. The Explorer (if 3-character) asks
   clarifying questions the audience would ask.
3. Synthesis scene: common ground, practical takeaways, explicit
   "we agree on X but disagree on Y."
4. Closing: each character's one-sentence recommendation.

Length calibration: ~150 words per spoken minute.
20-minute podcast ≈ 3,000 words ≈ 4-5 scenes.

Test: decompose "Modern CDC for data engineers" into a conversational
arc for a 15-minute podcast with 2 characters.
Review: does the arc have natural narrative flow? Do tension points
create genuine dramatic structure (not manufactured conflict)?
```

**Validate:**

- Arc has clear narrative progression (not just alternating monologues)
- Scene count matches target length
- Tension points are distributed across scenes (not all in one)

🔀 Commit: `feat(phase14): conversational arc decomposer`

#### Prompt 14.3 — Dialog generation with character-specific ranking

```text
Build the dialog generation stage:

For each scene:
1. Retrieve sources relevant to the scene's tension point
2. Run ranking TWICE — once with the Architect's weight profile,
   once with the Practitioner's weight profile
3. The Architect's lines draw from their top-ranked sources
4. The Practitioner's lines draw from their top-ranked sources
5. Where rankings diverge → natural dialogue disagreement
6. Where rankings agree → characters find common ground
7. The Explorer reacts to both, identifies gaps, asks "but what about..."

Generate: templates/dialog_scene.py produces character-labeled dialogue
with inline source annotations (visible in show-notes.md, not in the
spoken script itself).

Generate TTS-ready output: split script into per-character text files
with clean prose (no markdown, no stage directions, no source annotations).

Test: generate a complete 15-minute podcast script on "CDC approaches"
with the Architect and Practitioner. Read it aloud. Does it sound like
a real conversation between two knowledgeable people who disagree
productively?
```

**Validate:**

- Character voices are distinct and consistent
- Disagreements trace to actual source ranking differences
- Dialogue reads naturally (not stilted or robotic)
- TTS-ready files are clean text
- Show notes accurately cite every factual claim

🔀 Commit: `feat(phase14): dialog generation with character-specific ranking`

#### Prompt 14.4 — Full dialog generator with format variants and eval

```text
Complete the dialog generator:
1. Support format variants:
   - podcast (conversational, informal, 2-3 characters)
   - video (includes visual cues: "[show diagram of CDC flow]",
     "[cut to code example]")
   - debate (more structured: opening statements, rebuttals, closing)
   - panel (3+ characters, moderated by the Explorer)
2. validators/dialog_validator.py:
   - Every claim traces to a source
   - Character voice consistency (weight profiles respected)
   - Tension points are genuine source disagreements
   - Natural dialogue flow (no alternating monologues)
   - TTS files are clean
   - Length within 10% of target
3. Register as generator_type='dialog'
4. Generate three scripts:
   - 15-minute podcast (2 characters, CDC topic)
   - 10-minute video script (3 characters, data modeling topic)
   - 20-minute debate (2 characters, monolith vs. microservices)

Build tests/eval/dialog_eval.py:
1. Source traceability (every claim → source in show notes)
2. Character consistency (Architect cites books, Practitioner cites docs)
3. Tension point authenticity (disagreements from real source conflicts)
4. Dialogue naturalness (LLM-as-judge: rate 1-5 on conversational flow)
5. Length accuracy (within 10% of target minutes)
```

**Validate:**

- Three format variants all generate successfully
- Eval baseline established across all metrics
- At least one script sounds genuinely engaging when read aloud

🔀 Commit: `feat(phase14): dialog generator with format variants and eval`

#### Prompt 14.5 — Author Panel (N>2 character extension)

```text
Extend the Dialog generator to N>2 characters as the Author Panel
generator. Most of the implementation is shared.

1. Generalize identify_tension_points to support N character profiles:
   - Run pairwise across all N profiles, producing an N×N divergence matrix
   - Cluster tension points by sub-topic
   - For each cluster, assign 2-3 lead-debate characters (those with most
     divergent rankings); other characters react
2. Add panel-orchestration logic to decomposers/conversational.py:
   - Moderator scaffolding (introductions, transitions, Q&A solicitation)
   - Turn-taking heuristics (no speaker > 30% of total airtime, no
     monologue > 2 minutes)
3. Support custom character authoring: a character with a weight profile
   derived from a specific corpus author's style (e.g., "Kleppmann
   character" = 1.5x weight on Kleppmann book sources, 0.5x recency).
   Document this clearly: it's NOT impersonation — it's a view function
   that draws from author-favoring sources.
4. Extend validators/dialog_validator.py for N-character checks:
   - All character profiles distinct (no duplicate weight profiles)
   - Tension matrix shows real pairwise divergences
   - Moderator turn cadence honored
5. Register as generator_type='author_panel'
6. Generate a 30-minute author-panel script with 4 characters on a
   rich-conflict topic (e.g., "modern data architecture: lakehouse vs
   warehouse vs mesh").
7. Materialize to data/panels/

Build tests/eval/author_panel_eval.py: same metrics as dialog_eval but
with N-character extensions (per-pair tension authenticity, character
airtime fairness, moderator scaffolding presence).
```

**Validate:**

- Panel script generates successfully with 4-6 characters
- Tension matrix shows divergences across pairs (no all-agreement panels)
- Moderator scaffolding is present and natural

🔀 Commit: `feat(phase14): author panel — N>2 character extension of dialog generator`

---

### Phase 15: Project Bootstrap + Refactoring Playbook (week 37–40)

These two generators share the **stage 6.5 — code-stack reconciliation** infrastructure (live-doc verification of every generated procedure step). Bundling them avoids duplicating that infrastructure across two phases. Both are gated on Phase 13 (currency-aware ranking is load-bearing for code generation) and Phase 11 (Pattern Catalog provides the source/target inputs for refactoring).

#### Prompt 15.1 — Stage 6.5 reconciler

```text
Build reconcilers/live_doc_check.py — the stage 6.5 infrastructure
shared by Project Bootstrap, Refactoring Playbook, and (in retrospect)
Migration Guide.

Given a list of procedure_ids and a target stack:
1. For each procedure, identify the relevant doc source(s) for the stack.
2. Pull the latest cached snapshot (or refresh if stale).
3. Reconcile each step:
   - Parse-check: does the command/API call still parse?
   - Signature check: do flag/option names still exist?
   - Default check: do default values still match?
4. Classify per procedure: clean | needs-update | unresolvable.
5. For 'needs-update', emit a patch suggestion (what to change in the step).
6. For 'unresolvable', emit a TODO marker for downstream generators to
   surface in the output.

Build the MCP tool reconcile_with_live_docs wrapping this logic.
Cache reconciliation results so repeat generators (Bootstrap, Refactoring,
Migration) don't redo the work.
```

**Validate:**

- 50-procedure smoke test against a known-stale subset returns expected
  classifications
- Patch suggestions are actionable (not just "this is wrong")
- Cache invalidation works when doc snapshots refresh

🔀 Commit: `feat(phase15): stage 6.5 code-stack reconciler`

#### Prompt 15.2 — Project Bootstrap generator

```text
Build the Project Bootstrap generator on top of the framework + stage
6.5 reconciler.

1. Build decomposers/stack_scaffold.py:
   - Resolve design + stack to graph concepts
   - Walk REQUIRES + IMPLEMENTS edges from design to find realizing
     patterns and procedures
   - Walk EXTENDS + integrations edges from stack to find stack-specific
     procedures
   - Compose: pick the most-recent procedure per (pattern, stack) pair
2. Run stage 6.5 — reconcile every selected procedure with live docs.
   Surface unresolvable drift as scaffold-level TODOs rather than
   emitting code that won't run.
3. Build templates/project_file.py:
   - Generate README, setup script, source files, config files, tests,
     and .claude/CLAUDE.md
   - Per-file provenance tracking (which procedures composed each file)
4. Build validators/project_validator.py:
   - Setup script runs end-to-end on a clean machine (CI smoke test
     before sealing the package)
   - Every command/API verified against current doc snapshots
   - Tests pass (basic smoke tests at minimum)
   - Scaffold reflects the design (not just the name — verify structural
     features like CQRS having separate command/query paths)
5. Register as generator_type='project_bootstrap'
6. Generate the motivating example: "CQRS event-driven system using
   Kafka for HL7 messaging."
7. Materialize to data/project-scaffolds/

This is the highest validation bar of any generator: the scaffold must
actually run.
```

**Validate:**

- Setup script runs cleanly in a fresh container/VM
- Smoke tests pass
- The CQRS+Kafka+HL7 scaffold is structurally a CQRS project, not just
  named one
- Design rationale in `_design-notes.md` is sourced and accurate

🔀 Commit: `feat(phase15): project bootstrap generator`

#### Prompt 15.3 — Refactoring Playbook generator

```text
Build the Refactoring Playbook generator. Reuses the Pattern Catalog
(positive + inverse) as input substrate.

1. Build decomposers/transformation_path.py:
   - Resolve source state (anti-pattern or older pattern) and target
     state (target pattern) to catalog entries
   - Find the transformation path: intermediate states that make the
     migration safe (e.g., REST → REST+outbox → events-authoritative)
   - Per step, identify procedures from source, intermediate, target states
2. Run stage 6.5 reconciliation per step.
3. Build templates/refactor_step.py:
   - Per-step content: actions, rationale, tests, rollback procedure
   - _path-choices.md surfacing alternative paths considered (interactive
     ranking — refactoring trade-offs are real)
   - Checkpoint definitions between steps
   - risk-register.md from CONTRADICTS edges and known-bad-transitions
4. Build validators/refactor_validator.py:
   - Each step has a concrete rollback procedure
   - Checkpoints define testable states
   - Step ordering respects dependencies
   - Risk register entries are sourced
   - Playbook starts at "source" and ends at "target" (no missed steps)
5. Register as generator_type='refactoring_playbook'
6. Generate a playbook for a non-trivial refactor (e.g., "ad-hoc REST
   to event-driven communication").
7. Materialize to data/refactor-playbooks/
```

**Validate:**

- Playbook is usable: a team could follow it to perform the refactor
- Rollback procedures are real, not handwaved
- Risk register surfaces non-obvious risks
- Trade-off discussion in `_path-choices.md` represents genuine
  alternative paths

🔀 Commit: `feat(phase15): refactoring playbook generator`

#### Prompt 15.4 — Phase 15 family eval

```text
Build tests/eval/phase15_family_eval.py:
1. Project Bootstrap eval:
   - Generate scaffolds for 3 distinct (design, stack) pairs
   - Each scaffold passes its smoke test
   - Each scaffold's design rationale aligns with the chosen pattern
2. Refactoring Playbook eval:
   - Generate playbooks for 3 (source-pattern, target-pattern) pairs
   - Each playbook has reachable, testable checkpoints
   - Step-count is reasonable (no 50-step playbooks for a focused refactor)
3. Cross-generator coherence:
   - A refactor playbook from anti-pattern X to pattern Y, applied to
     a Project Bootstrap scaffold of X, should produce a scaffold
     that resembles a Bootstrap of Y
```

🔀 Commit: `feat(phase15): family eval covering bootstrap + refactoring`

---

### Phase 16: Curriculum Generator (week 40–42)

The composite generator. Orchestrates Learning Path (Phase 8), Tutorial (Phase 10), Dialog/Author Panel (Phase 14), and Cheatsheet (Phase 9). Cannot be built before its constituent generators.

#### Prompt 16.1 — Composite decomposer + module planner

```text
Build decomposers/composite.py — the Curriculum decomposer.

1. Define learning outcomes (LLM-driven from subject + audience).
2. Decompose subject into weekly modules (3-12 weeks typical):
   - Each module covers a coherent sub-topic
   - Modules are ordered by prerequisite dependency
   - Cadence-aware: hours-per-week budget shapes module size
3. Per module: dispatch to constituent generators with module-scoped queries:
   - Learning Path → reading list (per-week scope)
   - Tutorial → 1-3 hands-on exercises
   - Dialog/Author Panel → optional discussion-prompt script
   - Cheatsheet → optional handout if module covers cheatsheet-worthy tool
4. Generate weekly assessments (2-4 questions per module).
5. Generate final-project brief integrating outcomes across modules.
6. Compile unified bibliography.

Build the MCP tool plan_curriculum_modules wrapping module planning.
```

**Validate:**

- Module ordering respects prerequisites across modules (no week N
  requiring concepts not yet covered)
- Cadence is honored: estimated hours per module match stated weekly hours
- Constituent dispatches succeed for each module

🔀 Commit: `feat(phase16): composite decomposer for curriculum generator`

#### Prompt 16.2 — Curriculum generator end-to-end

```text
Wire the Curriculum generator end-to-end.

1. templates/curriculum_week.py:
   - Compose constituent outputs into a per-week directory
   - Generate _curriculum.md, _syllabus.md, final-project.md,
     bibliography.md, optional instructor-guide.md
2. validators/curriculum_validator.py:
   - Every constituent sub-output passes its own validator
   - No circular prerequisites across modules
   - Final project covers all stated learning outcomes
   - Assessment questions are answerable from assigned material
   - Bibliography is the deduplicated union
3. Register as generator_type='curriculum'. Use generated_package.parent_package_id
   to link constituent sub-packages.
4. Generate a curriculum: "Distributed Systems for Backend Engineers,
   12 weeks, 4h/week, cohort format."
5. Materialize to data/curricula/

Build tests/eval/curriculum_eval.py:
1. Cross-module prerequisite respect
2. Cadence accuracy
3. Outcome coverage in final project
4. Sub-output quality (delegates to constituent evals)
```

**Validate:**

- 12-week curriculum generates successfully end-to-end
- Each week has a coherent reading-list + exercises + assessment
- Instructor guide is usable for cohort facilitation
- Eval passes including delegated sub-evals

🔀 Commit: `feat(phase16): curriculum generator end-to-end`

---

### Phase 17: Generator Integration and Final Regression (week 42–43)

#### Prompt 17.1 — Unified /kb-generate command and comprehensive README

```text
Finalize the unified generation interface:

1. /kb-generate dispatches to all seventeen generator types:
   /kb-generate skills "<domain>"
   /kb-generate concept-map "<concept>" [--depth N]
   /kb-generate learning-path "<from> to <to>"
   /kb-generate content "<topic>" --format blog|talk|design-doc
   /kb-generate cheatsheet "<subject>"
   /kb-generate slides "<topic>" --minutes N
   /kb-generate tutorial "<topic>" --level beginner|intermediate|advanced
   /kb-generate patterns "<domain>" [--inverse]
   /kb-generate adr "<decision context>" --constraints "..."
   /kb-generate assessment "<technology>"
   /kb-generate migration "<subject>" --from "..." --to "..."
   /kb-generate currency-report "<concept-or-domain>"
   /kb-generate dialog "<topic>" --format podcast|video|debate --minutes 15
   /kb-generate author-panel "<topic>" --characters "..." --minutes N
   /kb-generate project "<design> using <stack>"
   /kb-generate refactor "<source-pattern> to <target-pattern>" --stack "..."
   /kb-generate curriculum "<subject>" --weeks N --cadence "..."

2. Update README.md with documentation for all seventeen generators:
   - What each produces and when to use it
   - Example commands with sample output descriptions
   - The ranking mode distinction (silent vs. interactive) and why it matters
   - Character system overview for dialog + author panel
   - Stage 6.5 reconciliation overview for code-output generators
   - Phase 4 currency-aware infrastructure overview for migration + currency report

3. Run ALL generator evals as a regression suite:
   - Skills, Concept Map, Learning Path
   - Content, Cheatsheet, Slide-deck (Phase 9 family eval)
   - Tutorial
   - Pattern + Anti-Pattern Catalog
   - ADR, Tech Assessment
   - Migration Guide, Currency Report
   - Dialog, Author Panel
   - Project Bootstrap, Refactoring Playbook (Phase 15 family eval)
   - Curriculum
   All must pass before merging.

4. Archive eval baselines as the post-Phase-17 reference for future
   maintenance.
```

**Validate:**

- Unified command works for all seventeen types
- Full regression suite passes
- README is comprehensive and accurate
- Eval baselines archived

🔀 Commit: `feat(phase17): unified seventeen-generator interface with full regression`

---

## 7. Summary

### Architecture changes (minimal)

- Generalized `Generator` base class extracted from Skills Factory
- Pluggable decomposers, templates, validators per generator type (17 generators)
- Generalized output tables (`generated_*`) parallel to `skill_*` tables, with `parent_package_id` for composite generators (Curriculum)
- Character system for Dialog and Author Panel (weight-biased views over the ranking engine)
- Stage 6.5 — code-stack reconciliation infrastructure (`reconcilers/live_doc_check.py`) shared by Project Bootstrap, Refactoring Playbook, and Migration Guide
- Currency-aware analysis (`era_diff`, `volatility_audit` decomposers) consuming Phase 4's CONTRADICTS edges and doc-snapshot history
- New MCP tools: `generate_package` (generalized), `analyze_knowledge_gaps`, `discover_patterns` (with inverse mode), `assess_graph_coverage`, `identify_tension_points` (N-character), `expand_concept_neighborhood`, `find_era_contradictions`, `compute_currency_index`, `reconcile_with_live_docs`, `plan_curriculum_modules`

### What stays the same (almost everything)

- DuckDB substrate with FTS + VSS + DuckPGQ
- Entity resolution
- Hybrid retrieval
- Ranking engine (both modes)
- Source merge and provenance tracking
- Sub-agent extraction pattern
- Auto-discovery
- Proactive refresh

### The seventeen generators at a glance

| Generator | Decomposer | Ranking mode | Output for | Key graph operation | Phase |
|---|---|---|---|---|---:|
| Skills Factory | Community detection | Silent | Agents | Concept clustering | 5 |
| Concept Neighborhood Map | k-hop expansion | Silent | Orientation / docs / debugging | DuckPGQ traversal with depth limit | 7 |
| Learning Path | Prerequisite traversal | Silent | Self-study | REQUIRES shortest paths | 8 |
| Content | Rhetorical structure | Interactive | Human readers | Broad retrieval + conflict surfacing | 9 |
| Cheatsheet | Topical condensation | Silent | Quick lookup | Procedure aggregation | 9 |
| Slide-deck Outline | Talk skeleton | Silent | Speakers | Rhetorical + bullet condensation | 9 |
| Tutorial | Exercise sequencing | Silent | Hands-on learners | Prerequisites + procedure filtering | 10 |
| Pattern + Anti-Pattern Catalog | Pattern discovery (positive + inverse) | Interactive | Architects | IMPLEMENTS clusters / anti-relations | 11 |
| ADR | Decision framing | Interactive | Design reviewers | CONTRASTS_WITH option finding | 12 |
| Tech Assessment | Evaluation matrix | Interactive | Evaluators | Coverage analysis + neighborhood mapping | 12 |
| Migration Guide | Era diff | Interactive | Re-learners | CONTRADICTS edges across eras | 13 |
| Currency Report | Volatility audit | Interactive | Tech leads / authors | Snapshot diff + era source delta | 13 |
| Dialog / Script | Conversational arc | Interactive | Audiences | Tension points across 2-3 character views | 14 |
| Author Panel | Multi-character arc (N>2) | Interactive | Audiences (panel format) | Pairwise tension matrix across N profiles | 14 |
| Project Bootstrap | Stack scaffolding | Silent | New-project authors | Concept→Pattern→Procedure with live-doc reconcile | 15 |
| Refactoring Playbook | Anti-pattern → pattern transformation | Interactive | Engineering teams | Transformation path + per-step reconcile | 15 |
| Curriculum | Composite | Mixed | Self-paced learners / cohorts / instructors | Orchestrates Learning Path + Tutorial + Dialog + Cheatsheet | 16 |

### The key insight

The generators don't add complexity to the core architecture — they add *value extraction surfaces* to the existing knowledge graph. Each generator is a different lens on the same underlying data. The expansion from eight to seventeen generators is mostly a question of templates: 12 of the 17 generators reuse one of four core decomposers (community, prerequisite, rhetorical, conversational) with different output templates and validation rules. The genuinely-new infrastructure is concentrated in three places:

1. **Stage 6.5 — code-stack reconciliation** (Phase 15): live-doc verification of every generated procedure step, used by Project Bootstrap, Refactoring Playbook, and (in retrospect) Migration Guide.
2. **Currency-aware analysis** (Phase 13): the `era_diff` and `volatility_audit` decomposers turn Phase 4's CONTRADICTS edges and doc-snapshot history into Migration and Currency Report outputs.
3. **Composite orchestration** (Phase 16): the Curriculum generator dispatches to other generators per-module and stitches their outputs into a multi-week structure, using `parent_package_id` for provenance.

The Dialog Generator (and Author Panel, its N>2 extension) introduces the most novel architectural concept: **characters as view functions over the ranking engine.** Instead of one author resolving conflicts, multiple characters each present the perspective that their weight profile favors, producing discussions where disagreements are grounded in actual source-ranking differences. This is structurally different from "ask an LLM to write a dialogue" — the debate is real because the underlying sources genuinely disagree.

The Project Bootstrap generator is the strongest single test of the substrate: it cannot work with books alone (book-derived scaffolds don't run on current APIs), so it forces the currency-aware ranking and live-doc reconciliation into load-bearing position. If the architecture handles Bootstrap, it handles everything else.

### Timeline

- Phase 7 (framework + concept map): 2 weeks
- Phase 8 (learning paths): 3 weeks
- Phase 9 (content + cheatsheet + slide-deck): 3 weeks
- Phase 10 (tutorials): 2 weeks
- Phase 11 (pattern + anti-pattern catalog): 3 weeks
- Phase 12 (ADR + tech assessment): 3 weeks
- Phase 13 (migration + currency report): 2 weeks
- Phase 14 (dialog + author panel): 3 weeks
- Phase 15 (project bootstrap + refactoring playbook): 3 weeks
- Phase 16 (curriculum): 2 weeks
- Phase 17 (integration + final regression): 1 week
- **Total: ~27 weeks after Phase 6** (vs. ~20 in the original eight-generator plan)

Each phase is independently useful — you can stop after any phase and have working generators for everything built so far. The phase ordering minimizes prerequisite delays: framework first, then primary new decomposers (prerequisite, rhetorical, conversational, evaluation), then the currency-aware family once Phase 4 has shipped, then code-output generators on top, with the composite Curriculum last.
