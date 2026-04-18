# myPub v2: Generators — Architecture and Execution Plan

**Status:** Design proposal (future phases, post-Phase 6)
**Prerequisite:** Skills Factory (Phase 5) fully operational
**Companion documents:** `mypub-v2-architecture.md`, `mypub-v2-execution-plan.md`

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

The four new generators share **stages 1, 4, 5, and 7** almost unchanged. What varies per generator is the **decomposition logic** (stage 2), the **planning/structural model** (stage 3), the **ranking mode** (generation-silent vs. interactive-surfaced), and the **output template** (stage 6).

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
| Pattern Catalog | Pattern discovery | DuckPGQ subgraph matching for IMPLEMENTS clusters |

#### 3. New output tables

The existing `skill_package` / `skill` / `skill_source` / `skill_file` tables are Skills-specific. The generators need a parallel but generalized output model. Two approaches:

**Option A — Generalized tables.** One set of tables (`generated_package`, `generated_unit`, `generated_source`, `generated_file`) with a `generator_type` discriminator. Simpler schema, single provenance query path.

**Option B — Per-generator tables.** `learning_path` / `learning_stage`, `content_project` / `content_section`, etc. More explicit, but multiplies table count.

**Recommendation: Option A.** The provenance pattern is identical across generators — every generated unit traces back to sources via the same score/weight/drop_reason structure. A single generalized table set avoids duplicating this. The `generator_type` field distinguishes output types for queries.

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR,       -- 'skills', 'learning_path', 'content', 'tutorial', 'pattern_catalog'
    name            VARCHAR,
    domain          VARCHAR,
    target_audience VARCHAR,
    created_at      TIMESTAMP,
    source_query    TEXT           -- the original user request
);

CREATE TABLE generated_unit (
    unit_id          BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES generated_package(package_id),
    unit_type        VARCHAR,      -- 'skill', 'stage', 'section', 'exercise', 'pattern'
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
| Pattern Catalog | **Interactive (surface conflicts)** | Pattern trade-offs ARE the content; surfacing disagreement is the point |

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
```
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
```
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
```
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

### 2.4 Pattern Catalog Generator

**Purpose:** Discover and document reusable patterns from the concept graph — automated identification of recurring architectural approaches, with trade-offs surfaced from multiple sources.

**Input:** Domain scope (e.g., "data integration patterns", "stream processing patterns") or discovery mode ("find patterns in my library related to <topic>").

**Decomposition — pattern discovery:**
1. Query the concept graph for clusters of concepts connected by IMPLEMENTS edges. Each cluster is a candidate pattern: a concept (the pattern) linked to multiple procedures (implementations) and discussed across multiple chapters/doc sections (evidence).
2. Filter to clusters with sufficient evidence — at least 2 independent sources discussing the pattern (not just one author's invention).
3. For each candidate pattern, check against the existing YAML pattern library to avoid duplicates.
4. LLM refinement: name the pattern, draft the problem statement, identify the key trade-offs based on how different sources discuss it.

**Ranking mode: interactive (surface conflicts).** Pattern trade-offs *are* the content. When Kleppmann describes event sourcing differently than a Databricks architecture guide, that's not noise — it's the essential information about when and why to choose different approaches. The Pattern Catalog Generator explicitly surfaces these perspectives.

**Output shape:**
```
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

**Selection strategy:** Consensus synthesis for problem statements and solutions (multiple authors should agree on what the pattern *is*). Interactive surfacing for trade-offs (where authors disagree is where the interesting design wisdom lives). Authority pick for canonical formulations when one source is definitional.

**Validation:**
- Every pattern has at least 2 independent source references
- No duplicate patterns (check against existing pattern library)
- Trade-offs section contains at least one genuine tension or design choice
- Implementation links point to valid procedures
- Related-pattern links use valid edge types from the concept graph
- pattern.yaml validates against a JSON schema

**Slash command:** `/kb-generate patterns "<domain>"` or `/kb-discover-patterns "<topic>"`

---

## 3. Schema Changes

### New tables (generalized output model)

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR NOT NULL,   -- 'skills', 'learning_path', 'content',
                                        -- 'tutorial', 'pattern_catalog'
    name            VARCHAR,
    domain          VARCHAR,
    target_audience VARCHAR,
    format          VARCHAR,            -- content-specific: 'blog', 'talk', 'design_doc'
    created_at      TIMESTAMP,
    source_query    TEXT
);

CREATE TABLE generated_unit (
    unit_id          BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES generated_package(package_id),
    unit_type        VARCHAR NOT NULL,  -- 'skill', 'stage', 'section', 'module',
                                        -- 'exercise', 'pattern'
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

---

## 4. Project Layout Changes

```
mcp-servers/kb-mcp/
├── server.py
├── retrievers.py
├── ranking.py
├── resolution.py
├── discovery.py
├── sectionizer.py
├── tiering.py
├── generator.py            # NEW: generalized pipeline framework
├── skills_factory.py       # refactored to use generator.py
├── decomposers/            # NEW: pluggable decomposition strategies
│   ├── community.py        #   Skills Factory (existing, extracted from skills_factory.py)
│   ├── prerequisite.py     #   Learning Path + Tutorial
│   ├── rhetorical.py       #   Content Generator
│   └── pattern_cluster.py  #   Pattern Catalog
├── templates/              # NEW: output templates per generator
│   ├── skill.py
│   ├── learning_stage.py
│   ├── content_section.py
│   ├── tutorial_module.py
│   └── pattern_entry.py
└── validators/             # NEW: validation logic per generator
    ├── skill_validator.py
    ├── path_validator.py
    ├── content_validator.py
    ├── tutorial_validator.py
    └── pattern_validator.py

.claude/commands/
├── ...existing commands...
├── kb-generate.md          # NEW: unified generate command
└── kb-discover-patterns.md # NEW: pattern discovery mode

data/
├── catalog.ddb
├── generated-packages/     # existing Skills output
├── learning-paths/         # NEW
├── content/                # NEW
├── tutorials/              # NEW
└── patterns/               # NEW
```

---

## 5. MCP Server Tool Additions

```python
# Generalized generation entry point
generate_package(
    generator_type,  # 'skills' | 'learning_path' | 'content' | 'tutorial' | 'pattern_catalog'
    domain,
    target_audience=None,
    format=None,           # for content generator: 'blog', 'talk', 'design_doc', 'chapter'
    start_knowledge=None,  # for learning paths: what the user already knows
    target_knowledge=None, # for learning paths: where they want to get to
    skill_level=None,      # for tutorials: 'beginner', 'intermediate', 'advanced'
    strategy_hint=None
)

# Learning path specific
analyze_knowledge_gaps(start_concepts, target_concepts)
# Returns: gap report showing concepts without book coverage

# Pattern catalog specific
discover_patterns(domain, min_evidence=2)
# Returns: candidate patterns with source counts before full generation
```

---

## 6. Execution Plan — Generator Phases

**Prerequisite:** Phase 5 (Skills Factory) complete and validated. The generator framework refactoring depends on having a working reference implementation.

### Phase 7: Generator Framework (week 16–17)

#### Prompt 7.1 — Refactor Skills Factory into generator framework

```
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

---

### Phase 8: Learning Path Generator (week 17–19)

#### Prompt 8.1 — Prerequisite decomposer

```
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

```
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

```
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

```
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

### Phase 9: Content Generator (week 19–21)

#### Prompt 9.1 — Rhetorical decomposer

```
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

```
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

```
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

---

### Phase 10: Tutorial Generator (week 21–23)

#### Prompt 10.1 — Exercise sequencing and procedure adaptation

```
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

```
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

### Phase 11: Pattern Catalog Generator (week 23–25)

#### Prompt 11.1 — Pattern discovery via graph clustering

```
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

```
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

```
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

---

### Phase 12: Generator Integration and Regression (week 25–26)

#### Prompt 12.1 — Unified /kb-generate command and README

```
Finalize the unified generation interface:

1. /kb-generate dispatches to the right generator:
   /kb-generate skills "<domain>"
   /kb-generate learning-path "<from> to <to>"
   /kb-generate content "<topic>" --format blog|talk|design-doc
   /kb-generate tutorial "<topic>" --level beginner|intermediate|advanced
   /kb-generate patterns "<domain>"

2. Update README.md with documentation for all five generators:
   - What each produces
   - When to use which
   - Example commands with sample output descriptions

3. Run ALL generator evals as a regression suite:
   - Skills Factory eval
   - Learning path eval
   - Content eval
   - Tutorial eval
   - Pattern catalog eval
   All must pass before merging.
```

**Validate:**
- Unified command works for all five types
- Full regression suite passes
- README covers all generators

🔀 Commit: `feat(phase12): unified generator interface with full regression`

---

## 7. Summary

### Architecture changes (minimal):
- Generalized `Generator` base class extracted from Skills Factory
- Pluggable decomposers, templates, validators per generator type
- Generalized output tables (`generated_*`) parallel to `skill_*` tables
- Three new MCP tools (generalized `generate_package`, `analyze_knowledge_gaps`, `discover_patterns`)

### What stays the same (almost everything):
- DuckDB substrate with FTS + VSS + DuckPGQ
- Entity resolution
- Hybrid retrieval
- Ranking engine (both modes)
- Source merge and provenance tracking
- Sub-agent extraction pattern
- Auto-discovery
- Proactive refresh

### The key insight:
The generators don't add complexity to the core architecture — they add *value extraction surfaces* to the existing knowledge graph. Each generator is a different lens on the same underlying data: Skills are for agents, learning paths are for sequential understanding, content is for sharing knowledge, tutorials are for hands-on practice, and pattern catalogs are for architectural decision-making.

### Timeline:
- Phase 7 (framework): 2 weeks
- Phase 8 (learning paths): 3 weeks
- Phase 9 (content): 3 weeks
- Phase 10 (tutorials): 2 weeks
- Phase 11 (pattern catalog): 3 weeks
- Phase 12 (integration): 1 week
- **Total: ~14 weeks after Phase 6**

Each phase is independently useful — you can stop after learning paths and have a working curriculum generator without needing the rest.
