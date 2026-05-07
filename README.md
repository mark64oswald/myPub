# myPub — Personal Knowledge Substrate for Technical Books and Live Docs

**A Claude-native knowledge base that turns a personal ePub library plus live vendor documentation into a single conversational substrate — searchable by concept, traversable as a graph, and capable of generating skills, tutorials, slide decks, ADRs, migration guides, and runnable project scaffolds on demand.**

myPub indexes a working library of ~540 technical books alongside live documentation snapshots from Context7, DeepWiki, and GitHub. Two surfaces sit on top of one DuckDB substrate: an MCP server that Claude calls during normal conversation, and a generator framework that composes retrieval + concept graph + procedures into deterministic, reproducible artifacts.

One catalog. One conversation. Books *and* current docs. Seventeen generators.

> **This is a personal, working repository.** The catalog (`data/catalog.ddb`), embeddings, and ePub originals are gitignored — the substrate is rebuildable from the scripts, but the source ePubs live at `~/Documents/eBooks/` on the author's machine. The architecture, schema, generators, and tests are all here in the open.

---

## What makes myPub different

| | Most KB tools | **myPub** |
|---|---|---|
| Source | Books *or* docs | Books *and* live docs, with alignment edges between them |
| Granularity | Chunks | Author-structured chapters + heading-aligned doc sections |
| Retrieval | Vector similarity | 5-factor ranking: relevance × recency × authority × corroboration × doc-alignment |
| Output | Search results | 17 generators that produce ADRs, tutorials, slide decks, runnable project scaffolds |
| Currency | Static | Doc snapshots refresh with TTLs; CONTRADICTS edges flag book/live drift |
| Personality | One voice | Character profiles (Architect, Practitioner) view the same substrate differently |

The most valuable queries cross all of these — a single conversation that pulls a foundational concept from a 2018 book, corroborates it against current Kafka docs, surfaces a contradiction where the book says "exactly-once requires this manual setup" but current docs ship `enable.idempotence=true` by default, and feeds the result into a Project Bootstrap generator that emits a runnable scaffold.

---

## The substrate (current state)

| | Count |
|---|---|
| Books indexed | **541** |
| Chapters | **113,165** (112,968 with content) |
| Concepts | **85,328** |
| Concept-graph edges (REQUIRES, EXTENDS, IMPLEMENTS, CONTRASTS_WITH, CITES) | **127,490** |
| Procedures (precondition / steps / postcondition / failure modes) | **4,341** |
| Live doc sources (Context7 + DeepWiki) | **10** |
| Doc sections indexed | **902** |
| Alignment edges (book ↔ live-doc CORROBORATES) | **120** across 7 sources |
| Generators on Phase 7 framework | **17** |
| Tests | **830** (825 unit + 5 live) |

Top publishers in the library: O'Reilly (270), Manning (48), Packt (28), Addison-Wesley (12). Top authors by book count: Martin Fowler, Joe Celko, Brian Kernighan (4 each).

Live doc sources (snapshot count × section count):

| Source | Provider | Sections |
|---|---|---|
| PostgreSQL | Context7 | 22 |
| Apache Kafka | Context7 | 22 |
| Apache Spark | Context7 | 22 |
| LangChain | Context7 | 22 |
| MLflow | Context7 | 26 |
| Databricks | Context7 | 28 |
| Delta Lake | Context7 | 21 |
| DuckDB | Context7 | 20 |
| DuckPGQ | DeepWiki | 282 |
| FastMCP | DeepWiki | 437 |

---

## Getting started

| Resource | Description |
|---|---|
| [Hello, myPub!](docs/getting-started.md) | Zero-to-first-query in 15 minutes — install, build the catalog, run the MCP server, ask the first question |
| [Architecture](docs/architecture.md) | Substrate layers, ranking engine, Phase 7 generator framework |
| [Data sources](docs/data-sources.md) | What goes in: ePubs, Context7, DeepWiki, GitHub raw |
| [Ingestion & indexing](docs/ingestion-and-indexing.md) | Structural parse, FTS, vector embeddings, DuckPGQ — every stage of the pipeline |
| [Concept graph](docs/concept-graph.md) | Entity extraction, resolver, alignment edges, procedures |
| [Generators](docs/generators.md) | Catalog of all 17 generators with prompts and example outputs |
| [Customization](docs/customization.md) | Weight profiles, character profiles, adding a new generator |
| [Operations](docs/operations.md) | Re-ingestion, doc refresh, alignment runs, retrieval eval |

Canonical specs (deeper, design-doc level):

- [docs/mypub-v2-architecture.md](docs/mypub-v2-architecture.md) — full system design
- [docs/mypub-v2-execution-plan.md](docs/mypub-v2-execution-plan.md) — phased roadmap
- [docs/mypub-v2-generators.md](docs/mypub-v2-generators.md) — generator specifications

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────────┐
│                       Claude Code / Claude Desktop                   │
│                                                                      │
│   Natural language ────────────────────►  /kb-* slash commands       │
└──────────────────────────────────────────────────────────────────────┘
                  │                                      │
                  ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      mypub-kb MCP server (FastMCP)                   │
│   search_chapters · compare_concept_across_authors                   │
│   find_prerequisites · disambiguate_discovery                        │
└──────────────────────────────────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│              Phase 7 Generator Framework (17 generators)             │
│   Decomposer ─► Planner ─► Validator ─► Materializer                 │
│                                                                      │
│   Concept Map · Learning Path · Cheatsheet · Slide Deck              │
│   Pattern Catalog · Content Brief · Tutorial · ADR                   │
│   Tech Assessment · Migration Guide · Currency Report                │
│   Dialog · Author Panel · Project Bootstrap · Refactoring · Curriculum│
│   Skills Factory                                                     │
└──────────────────────────────────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Ranking Engine (5 factors)                     │
│   relevance × recency × authority × corroboration × doc_alignment    │
│                                                                      │
│   Modes: generation (silent) │ interactive (surfaces conflicts)      │
│   Profiles: currency_critical · foundational · balanced · skill_*    │
└──────────────────────────────────────────────────────────────────────┘
                  │
        ┌─────────┴─────────────────┬─────────────────┬────────────────┐
        ▼                           ▼                 ▼                ▼
 ┌────────────┐  ┌──────────────────────┐  ┌───────────────┐  ┌──────────────┐
 │ FTS (BM25) │  │ VSS (HNSW, 384-dim   │  │ DuckPGQ graph │  │ Concept graph│
 │ Porter     │  │ all-MiniLM-L6-v2)    │  │ (vertex+edge  │  │ + alignment  │
 │ stemmer    │  │ chapter / doc / cept │  │  tables)      │  │  edges       │
 └────────────┘  └──────────────────────┘  └───────────────┘  └──────────────┘
        │                           │                 │                │
        └─────────┬─────────────────┴─────────────────┴────────────────┘
                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        DuckDB substrate (1.5.0)                      │
│                                                                      │
│   author · book · chapter · concept · concept_relation · procedure   │
│   doc_source · doc_snapshot · doc_section · alignment_edge           │
│   generated_package · skill_package · *_embedding side tables        │
└──────────────────────────────────────────────────────────────────────┘
                  │                                      │
                  ▼                                      ▼
        ┌────────────────────┐               ┌─────────────────────────┐
        │  ePub library      │               │  Live doc MCP servers   │
        │  ~/Documents/      │               │  Context7 · DeepWiki    │
        │  eBooks/ (541)     │               │  · GitHub raw           │
        └────────────────────┘               └─────────────────────────┘
```

---

## The seventeen generators

Every generator is a four-stage pipeline (decompose → plan → validate → materialize) over the same substrate. Inputs come from concept graph queries, chapter retrieval, procedures, and (for Project Bootstrap and Migration Guide) live doc sections.

### Skills & curriculum

| Slash command | Generator | Output |
|---|---|---|
| `/kb-generate-skills <domain>` | Skills Factory | A complete Claude Skills package (manifest, files, evals) |
| `/kb-concept-map <concept>` | Concept Neighborhood Map | Markdown map of concepts within N hops (REQUIRES / EXTENDS / IMPLEMENTS / CONTRASTS_WITH) |
| `/kb-learning-path <target>` | Learning Path | Topologically sorted curriculum with chapter citations |
| `/kb-curriculum <topic>` | Curriculum (composite) | Multi-week structured course built from learning paths + tutorials |

### Reference & teaching

| Slash command | Generator | Output |
|---|---|---|
| `/kb-cheatsheet <topic>` | Cheatsheet | One-page quick reference: definitions, gotchas, code patterns |
| `/kb-slides <topic>` | Slide-Deck Outline | Title + bullets per slide with citations |
| `/kb-tutorial <topic>` | Tutorial | Step-by-step procedure-backed walkthrough |
| `/kb-content-brief <topic>` | Content Brief | Article/blog skeleton with key points and sources |
| `/kb-pattern-catalog <topic>` | Pattern + Anti-Pattern Catalog | Patterns with canonical implementations + anti-patterns |

### Decisions & strategy

| Slash command | Generator | Output |
|---|---|---|
| `/kb-adr <decision>` | ADR | Architecture Decision Record with options + rationale |
| `/kb-tech-assessment <tech>` | Tech Assessment | Maturity, fit, risk, alternatives |
| `/kb-migration-guide <from> <to>` | Migration Guide | CONTRADICTS-edge-driven version migration |
| `/kb-currency-report <topic>` | Currency Report | Where books and live docs disagree |

### Voice & character

| Slash command | Generator | Output |
|---|---|---|
| `/kb-dialog <topic>` | Dialog | Two-character conversation (Architect vs Practitioner) |
| `/kb-author-panel <topic>` | Author Panel | Multi-author roundtable using `compare_concept_across_authors` |

### Bootstrap & refactor

| Slash command | Generator | Output |
|---|---|---|
| `/kb-bootstrap <stack>` | **Project Bootstrap** ★ | Runnable scaffold: code, configs, docker-compose, tests |
| `/kb-refactoring <pattern>` | Refactoring Playbook | Targeted refactor with before/after snippets |

★ = the user's #1 motivating example: *"I just learned about CQRS and event-driven systems — create a working example project that demonstrates this using Kafka for HL7 messaging."* See [docs/generators.md](docs/generators.md#project-bootstrap) for the canonical CQRS+Kafka+HL7 walkthrough.

---

## MCP tools (talk to Claude in natural language)

Most queries don't need a slash command — the `mypub-kb` MCP server is auto-invoked.

| Tool | What it does |
|---|---|
| `search_chapters(query, mode, limit, weight_profile, auto_discover, selection_strategy)` | Hybrid retrieval (FTS × VSS × graph) over chapters + doc sections; `mode="interactive"` surfaces conflicts, `mode="generation"` returns a curated list |
| `compare_concept_across_authors(concept_name, limit_per_author)` | Per-author roll-up of chapters that discuss a concept |
| `find_prerequisites(concept_name, max_depth)` | Recursive walk over `REQUIRES` edges; returns each prerequisite at its shortest depth |
| `disambiguate_discovery(source, identifier, display_name, query_term)` | Closes the auto-discovery loop when search returns multiple candidate libraries with similar scores |

**When to reach for a slash command vs. natural language**: slash commands are reserved for multi-step workflows (the explicit discovery loop, generator pipelines). For a search, a comparison, a prerequisite walk — just ask Claude.

---

## "Hello, myPub!" — first conversation

```text
You: search the kb for change data capture
Claude: [calls search_chapters → returns 8 chapters from Kleppmann, Tane,
         Confluent docs; primary = "Capturing All Database Changes"
         from Kleppmann ch. 11; corroborations from Apache Kafka doc
         section "Kafka Connect"]

You: who in my library discusses CQRS?
Claude: [calls compare_concept_across_authors → returns 6 authors:
         Vernon, Young, Newman, Fowler, Crain, Khononov, with the most
         detailed treatment in Vernon's "Implementing Domain-Driven Design"]

You: /kb-bootstrap CQRS event-sourced order service with Kafka and HL7
Claude: [Project Bootstrap pipeline:
         decompose → 12 concept clusters identified
         plan → 23 files projected (Python services, docker-compose,
                kafka topic config, HL7 v2 parser, pytest scaffolds)
         validate → 0 unresolved targets, all procedures matched
         materialize → writes scaffold to data/generated-packages/
                       cqrs-kafka-hl7-bootstrap/]
```

Full walkthrough with expected output: [docs/getting-started.md](docs/getting-started.md).

---

## Repository layout

```text
myPub/
├── README.md                           # this file
├── CLAUDE.md                           # project instructions for Claude Code
├── docs/
│   ├── getting-started.md              # Hello, myPub!
│   ├── architecture.md                 # substrate + framework
│   ├── data-sources.md                 # ePubs + live docs
│   ├── ingestion-and-indexing.md       # FTS, VSS, DuckPGQ pipeline
│   ├── concept-graph.md                # extraction, alignment, procedures
│   ├── generators.md                   # all 17 generators
│   ├── customization.md                # weight profiles, new generators
│   ├── operations.md                   # re-ingestion, refresh, eval
│   ├── mypub-v2-architecture.md        # canonical spec
│   ├── mypub-v2-execution-plan.md      # phased roadmap
│   └── mypub-v2-generators.md          # generator specifications
├── mcp-servers/kb-mcp/                 # FastMCP server + 17 generators
│   ├── server.py                       # MCP entrypoint, 4 tools
│   ├── ranking.py                      # 5-factor ranking engine
│   ├── discovery.py                    # auto-discovery probe order
│   ├── resolution.py                   # EntityResolver
│   ├── generator.py                    # Phase 7 framework protocols
│   ├── concept_map.py · learning_path.py · cheatsheet.py · …
│   └── skills_factory.py · skill_generation.py · skills_eval.py
├── scripts/                            # build, ingest, refresh, migrate
├── schemas/                            # catalog.sql + property_graph.sql
├── tests/                              # 830 tests
├── .claude/
│   ├── commands/                       # 19 /kb-* slash commands
│   └── skills/                         # kb-usage, skills-factory
├── data/                               # catalog.ddb (gitignored)
└── pyproject.toml
```

---

## Five-factor ranking engine

Every retrieval call combines five signals; the `weight_profile` parameter chooses how to weight them.

| Factor | What it measures | Sources |
|---|---|---|
| `relevance` | Hybrid FTS + VSS + graph score (RRF fused) | BM25 over `chapter.content`, cosine over 384-dim embeddings, graph proximity |
| `recency` | Publication / snapshot freshness | `book.publication_date`, `doc_snapshot.retrieved_at` |
| `authority` | Source credibility | Publisher tier (O'Reilly, Manning, Packt) for books; doc-source tier (Context7=0.60, DeepWiki=0.50, GitHub=0.40) |
| `corroboration` | Cross-source agreement | `alignment_edge` table; CORROBORATES boost when book + live doc agree |
| `doc_alignment` | Whether a query domain has live-doc coverage | 1.00 for the 7 aligned sources, 0.50 neutral for unaligned |

Profiles tuned for different use cases:

| Profile | rec | doc | rel | corr | auth | When to use |
|---|---|---|---|---|---|---|
| `currency_critical_interactive` | 0.30 | 0.20 | 0.30 | 0.10 | 0.10 | Default for interactive Q&A — "what does Kafka do *now*?" |
| `foundational_interactive` | 0.05 | 0.05 | 0.40 | 0.20 | 0.30 | Timeless concepts — algorithms, design pattern theory |
| `balanced_interactive` | 0.10 | 0.10 | 0.45 | 0.15 | 0.20 | General use |
| `skill_recent_doc_anchored` | 0.40 | 0.30 | 0.20 | 0.05 | 0.05 | Skills Factory: anchor on current vendor docs |
| `skill_consensus_synthesis` | 0.10 | 0.10 | 0.35 | 0.30 | 0.15 | Skills Factory: synthesize where book + docs agree |

See [docs/customization.md](docs/customization.md#weight-profiles) for tuning guidance.

---

## Slash command reference

### Knowledge base

| Command | Purpose |
|---|---|
| `/kb-discover <term>` | Hybrid search with the explicit discovery loop wired in |
| `/kb-review-concepts` | Interactive review of the EntityResolver queue |

### Generators

| Command | Generator |
|---|---|
| `/kb-generate-skills <domain>` | Skills Factory |
| `/kb-concept-map <concept>` | Concept Neighborhood Map |
| `/kb-learning-path <target>` | Learning Path |
| `/kb-cheatsheet <topic>` | Cheatsheet |
| `/kb-slides <topic>` | Slide-Deck Outline |
| `/kb-pattern-catalog <topic>` | Pattern + Anti-Pattern Catalog |
| `/kb-content-brief <topic>` | Content Brief |
| `/kb-tutorial <topic>` | Tutorial |
| `/kb-adr <decision>` | ADR |
| `/kb-tech-assessment <tech>` | Tech Assessment |
| `/kb-migration-guide <from> <to>` | Migration Guide |
| `/kb-currency-report <topic>` | Currency Report |
| `/kb-dialog <topic>` | Dialog (character pair) |
| `/kb-author-panel <topic>` | Author Panel |
| `/kb-bootstrap <stack>` | Project Bootstrap |
| `/kb-refactoring <pattern>` | Refactoring Playbook |
| `/kb-curriculum <topic>` | Curriculum (composite) |

---

## Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Substrate | DuckDB 1.5.0 (pinned) | Single-file catalog; embeddings via VSS; FTS via BM25; graph via DuckPGQ — no external services |
| Embeddings | sentence-transformers/all-MiniLM-L6-v2, 384-dim | Local, fast, free; embeddings live in side tables to dodge a 1.5.0 FK bug |
| Granularity | Chapter / heading-aligned section | Preserve author structure; most chapters fit Claude's context (4K–17K tokens) |
| Ranking | 5-factor weighted score, two modes | `generation` mode is silent; `interactive` mode surfaces conflicts as first-class output |
| Live docs | Context7 → DeepWiki → GitHub probe order | Authority-tiered fallback when a library isn't on a higher-authority source |
| Alignment | Separate `alignment_edge` table | Corroboration / contradiction is a distinct relation kind; 120 CORROBORATES live, CONTRADICTS empty |
| Generators | One framework, 17 implementations | Decomposer + Planner + Validator + Materializer protocols — every generator is the same shape |
| Personality | Character profiles | View functions over the ranking engine; same substrate, different voices |

---

## Lineage

myPub inherits design patterns from sibling projects:

- **[healthsim-workspace](https://github.com/mark64oswald/healthsim-workspace)** — the Claude Code skill-driven development model, hierarchical seed manager, close-before-write DuckDB pattern, idempotent migration scripts
- **[vantage](https://github.com/mark64oswald/vantage-docs)** — the conversational-strategist surface (slash commands as multi-step workflow shortcuts; natural language as the default), and the "comparison + report generator" output pattern
- **[BioScienceAgent](https://github.com/mark64oswald/BioScienceAgent-docs)** — the sub-agent dispatch pattern used by the Skills Factory and Project Bootstrap (prep → dispatch → process → materialize)

Reference docs that mirror their respective projects' README style: [vantage-docs/README](https://github.com/mark64oswald/vantage-docs), [BioScienceAgent-docs/README](https://github.com/mark64oswald/BioScienceAgent-docs).

The domain content (technical books, vendor live docs, code generators) is myPub-specific.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

The catalog database, embeddings, and ePub originals are *not* distributed. Only the substrate code, schema, generator framework, and tests are in this repository. Build your own catalog from your own ePub library following [docs/getting-started.md](docs/getting-started.md).

---

*myPub indexes a personal technical library and live vendor documentation for individual learning and authoring. No proprietary content from publishers is redistributed; only metadata, structure, and locally-derived embeddings are stored in the catalog.*
