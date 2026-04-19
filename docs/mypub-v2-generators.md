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
| ADR Generator | Decision framing | Options from CONTRASTS_WITH edges + criteria from concept attributes |
| Tech Assessment | Evaluation matrix | Multi-concept comparison via graph neighborhood + doc currency |
| Dialog Generator | Conversational arc | Topic decomposition into debate points from ranked source conflicts |

#### 3. New output tables

The existing `skill_package` / `skill` / `skill_source` / `skill_file` tables are Skills-specific. The generators need a parallel but generalized output model. Two approaches:

**Option A — Generalized tables.** One set of tables (`generated_package`, `generated_unit`, `generated_source`, `generated_file`) with a `generator_type` discriminator. Simpler schema, single provenance query path.

**Option B — Per-generator tables.** `learning_path` / `learning_stage`, `content_project` / `content_section`, etc. More explicit, but multiplies table count.

**Recommendation: Option A.** The provenance pattern is identical across generators — every generated unit traces back to sources via the same score/weight/drop_reason structure. A single generalized table set avoids duplicating this. The `generator_type` field distinguishes output types for queries.

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR,       -- 'skills', 'learning_path', 'content', 'tutorial',
                                        -- 'pattern_catalog', 'adr', 'tech_assessment', 'dialog'
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
| ADR Generator | **Interactive (surface conflicts)** | Pros/cons of each option require showing real tensions |
| Tech Assessment | **Interactive (surface conflicts)** | Honest evaluation requires surfacing strengths AND weaknesses |
| Dialog Generator | **Interactive (surface conflicts)** | Character disagreements are driven by real source conflicts |

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
```
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
```
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
```
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

---

## 3. Schema Changes

### New tables (generalized output model)

```sql
CREATE TABLE generated_package (
    package_id      BIGINT PRIMARY KEY,
    generator_type  VARCHAR NOT NULL,   -- 'skills', 'learning_path', 'content', 'tutorial',
                                        -- 'pattern_catalog', 'adr', 'tech_assessment', 'dialog'
    name            VARCHAR,
    domain          VARCHAR,
    target_audience VARCHAR,
    format          VARCHAR,            -- content: 'blog'|'talk'|'design_doc'|'chapter'
                                        -- dialog: 'podcast'|'video'|'debate'|'panel'
    created_at      TIMESTAMP,
    source_query    TEXT
);

CREATE TABLE generated_unit (
    unit_id          BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES generated_package(package_id),
    unit_type        VARCHAR NOT NULL,  -- 'skill', 'stage', 'section', 'module',
                                        -- 'exercise', 'pattern', 'option', 'criterion',
                                        -- 'scene', 'character', 'assessment_dimension'
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
│   ├── pattern_cluster.py  #   Pattern Catalog
│   ├── decision_frame.py   #   ADR Generator
│   ├── eval_matrix.py      #   Tech Assessment
│   └── conversational.py   #   Dialog Generator
├── templates/              # NEW: output templates per generator
│   ├── skill.py
│   ├── learning_stage.py
│   ├── content_section.py
│   ├── tutorial_module.py
│   ├── pattern_entry.py
│   ├── adr_section.py
│   ├── assessment_dim.py
│   └── dialog_scene.py
└── validators/             # NEW: validation logic per generator
    ├── skill_validator.py
    ├── path_validator.py
    ├── content_validator.py
    ├── tutorial_validator.py
    ├── pattern_validator.py
    ├── adr_validator.py
    ├── assessment_validator.py
    └── dialog_validator.py

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
├── patterns/               # NEW
├── decisions/              # NEW (ADRs)
├── assessments/            # NEW (Tech Assessments)
└── dialogs/                # NEW (Scripts)
```

---

## 5. MCP Server Tool Additions

```python
# Generalized generation entry point
generate_package(
    generator_type,  # 'skills' | 'learning_path' | 'content' | 'tutorial' |
                     # 'pattern_catalog' | 'adr' | 'tech_assessment' | 'dialog'
    domain,
    target_audience=None,
    format=None,           # content: 'blog'|'talk'|'design_doc'; dialog: 'podcast'|'video'|'debate'
    start_knowledge=None,  # for learning paths
    target_knowledge=None, # for learning paths
    skill_level=None,      # for tutorials
    constraints=None,      # for ADRs: list of decision constraints
    characters=None,       # for dialog: 2 or 3; or custom character definitions
    target_minutes=None,   # for dialog: target length
    strategy_hint=None
)

# Learning path specific
analyze_knowledge_gaps(start_concepts, target_concepts)

# Pattern catalog specific
discover_patterns(domain, min_evidence=2)

# Tech assessment specific
assess_graph_coverage(concept_name)
# Returns: source count by type, coverage depth, currency status, graph neighborhood size

# Dialog specific
identify_tension_points(domain, min_sources_per_side=2)
# Returns: concept pairs where high-authority and high-recency sources disagree
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

### Phase 12: ADR and Tech Assessment Generators (week 25–27)

ADR and Technical Assessment are structurally similar — both are evaluation-oriented, both use interactive ranking, both leverage CONTRASTS_WITH edges. Building them together.

#### Prompt 12.1 — Decision framing decomposer (ADR)

```
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

```
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

```
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

```
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

### Phase 13: Dialog / Script Generator (week 27–30)

The most architecturally novel generator. Characters are view functions over the ranking engine; their disagreements are driven by actual source conflicts.

#### Prompt 13.1 — Character system and tension point identification

```
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

🔀 Commit: `feat(phase13): character system and tension point identification`

#### Prompt 13.2 — Conversational arc decomposer

```
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

🔀 Commit: `feat(phase13): conversational arc decomposer`

#### Prompt 13.3 — Dialog generation with character-specific ranking

```
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

🔀 Commit: `feat(phase13): dialog generation with character-specific ranking`

#### Prompt 13.4 — Full dialog generator with format variants and eval

```
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

🔀 Commit: `feat(phase13): dialog generator with format variants and eval`

---

### Phase 14: Generator Integration and Final Regression (week 30–31)

#### Prompt 14.1 — Unified /kb-generate command and comprehensive README

```
Finalize the unified generation interface:

1. /kb-generate dispatches to all eight generator types:
   /kb-generate skills "<domain>"
   /kb-generate learning-path "<from> to <to>"
   /kb-generate content "<topic>" --format blog|talk|design-doc
   /kb-generate tutorial "<topic>" --level beginner|intermediate|advanced
   /kb-generate patterns "<domain>"
   /kb-generate adr "<decision context>" --constraints "..."
   /kb-generate assessment "<technology>"
   /kb-generate dialog "<topic>" --format podcast|video|debate --minutes 15

2. Update README.md with documentation for all eight generators:
   - What each produces and when to use it
   - Example commands with sample output descriptions
   - The ranking mode distinction (silent vs. interactive) and why it matters
   - Character system overview for the dialog generator

3. Run ALL generator evals as a regression suite:
   - Skills Factory eval
   - Learning path eval
   - Content eval
   - Tutorial eval
   - Pattern catalog eval
   - ADR eval
   - Tech assessment eval
   - Dialog eval
   All must pass before merging.
```

**Validate:**
- Unified command works for all eight types
- Full regression suite passes
- README is comprehensive and accurate

🔀 Commit: `feat(phase14): unified eight-generator interface with full regression`

---

## 7. Summary

### Architecture changes (minimal):
- Generalized `Generator` base class extracted from Skills Factory
- Pluggable decomposers, templates, validators per generator type (8 generators)
- Generalized output tables (`generated_*`) parallel to `skill_*` tables
- Character system for dialog generation (weight-biased views over the ranking engine)
- New MCP tools: `generate_package` (generalized), `analyze_knowledge_gaps`, `discover_patterns`, `assess_graph_coverage`, `identify_tension_points`

### What stays the same (almost everything):
- DuckDB substrate with FTS + VSS + DuckPGQ
- Entity resolution
- Hybrid retrieval
- Ranking engine (both modes)
- Source merge and provenance tracking
- Sub-agent extraction pattern
- Auto-discovery
- Proactive refresh

### The eight generators at a glance:

| Generator | Decomposer | Ranking mode | Output for | Key graph operation |
|---|---|---|---|---|
| Skills Factory | Community detection | Silent | Agents | Concept clustering |
| Learning Path | Prerequisite traversal | Silent | Self-study | REQUIRES shortest paths |
| Content | Rhetorical structure | Interactive | Human readers | Broad retrieval + conflict surfacing |
| Tutorial | Exercise sequencing | Silent | Hands-on learners | Prerequisites + procedure filtering |
| Pattern Catalog | Pattern discovery | Interactive | Architects | IMPLEMENTS cluster detection |
| ADR | Decision framing | Interactive | Design reviewers | CONTRASTS_WITH option finding |
| Tech Assessment | Evaluation matrix | Interactive | Evaluators | Coverage analysis + neighborhood mapping |
| Dialog | Conversational arc | Interactive | Audiences | Tension point identification across character views |

### The key insight:
The generators don't add complexity to the core architecture — they add *value extraction surfaces* to the existing knowledge graph. Each generator is a different lens on the same underlying data: Skills are for agents, learning paths are for sequential understanding, content is for sharing knowledge, tutorials are for hands-on practice, pattern catalogs are for architectural decision-making, ADRs are for technology decisions, assessments are for due diligence, and dialogs are for making knowledge engaging and accessible.

The Dialog Generator introduces the most novel architectural concept: **characters as view functions over the ranking engine.** Instead of one author resolving conflicts, multiple characters each present the perspective that their weight profile favors, producing discussions where disagreements are grounded in actual source-ranking differences. This is structurally different from "ask an LLM to write a dialogue" — the debate is real because the underlying sources genuinely disagree.

### Timeline:
- Phase 7 (framework): 2 weeks
- Phase 8 (learning paths): 3 weeks
- Phase 9 (content): 3 weeks
- Phase 10 (tutorials): 2 weeks
- Phase 11 (pattern catalog): 3 weeks
- Phase 12 (ADR + tech assessment): 3 weeks
- Phase 13 (dialog): 3 weeks
- Phase 14 (integration): 1 week
- **Total: ~20 weeks after Phase 6**

Each phase is independently useful — you can stop after any phase and have working generators for everything built so far. The dialog generator is last because it's the most novel and benefits from all the infrastructure the earlier generators establish.
