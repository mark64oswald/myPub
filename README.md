# myPub — A Personal Knowledge Substrate for Technical Books and Live Docs

**Turn a personal ePub library and current vendor documentation into one conversational substrate — searchable by concept, traversable as a graph, and capable of generating skills, tutorials, slide decks, ADRs, migration guides, and runnable project scaffolds on demand.**

A book is too coherent to chunk. A vendor doc is too current to ignore. Most knowledge bases pick one and lose the other half — myPub keeps both, in one DuckDB file, with edges between them.

> **577 books · 109 live doc sources · 337K concepts · 5.9K alignment edges · 24 generators · one DuckDB file · zero cloud.**

---

## The pitch in three sentences

1. **Books and live docs answer different questions.** Kleppmann teaches you what an idempotent producer *is*. The current Kafka docs tell you `enable.idempotence=true` is the default since 3.0. myPub indexes both, alignment-edges them where they overlap, and lets the ranker pick when to lean which way.
2. **The unit of retrieval is the author's chapter, not a 512-token chunk.** Most chapters fit Claude's context. Author structure is preserved end-to-end.
3. **The interesting outputs aren't search results — they're generated artifacts.** Twenty-four generators sit on a single Decompose → Plan → Validate → Materialize framework. Ask for an ADR, a tutorial, a refactoring playbook, a runnable CQRS+Kafka scaffold, a domain-level library landscape, a one-page quickstart, an HL7v2→FHIR transformer, or a HIPAA-Safe-Harbor de-identification pipeline; get a versioned, citation-backed package on disk.

---

## Two kinds of input — and why both

myPub combines two fundamentally different kinds of source material into one substrate. Understanding the distinction is the single most important thing about how the system thinks.

|   | **Local ePub library** | **Live documentation** |
|---|---|---|
| **What it is** | Technical books I've bought (.epub files) | Current vendor documentation pulled from MCP servers |
| **Where it lives** | `~/Documents/eBooks/` (~577 books, ~470 GB total) | `doc_section` rows in the catalog, refreshed on TTL |
| **How it gets there** | `scripts/index_books.py` walks the directory, extracts chapter structure, generates embeddings | `scripts/refresh_docs.py` calls Context7 / DeepWiki / GitHub raw via MCP, snapshots the markdown, sectionizes by heading |
| **Granularity** | Chapter (preserves the author's structural intent) | Heading-aligned section (preserves the doc author's structure) |
| **What it's good at** | Explaining *why* something is shaped the way it is. Foundational concepts. Tradeoffs. The long view. | Telling you what the *current* API surface looks like — what flag is the new default, what changed in 3.0 |
| **What it's bad at** | Currency. A 2018 book about Kafka still says exactly-once needs manual setup. | Explaining itself. The current Kafka doc tells you `enable.idempotence=true` is default; it won't tell you why exactly-once was hard for a decade. |
| **Authority** | Publisher-tier (O'Reilly, Manning, Addison-Wesley graded high; Packt mid; defaults below) | Provider-tier: Context7 = 0.85 (curated/vendor), DeepWiki = 0.70 (AI-generated), GitHub raw = 0.65 |
| **Typical age** | Years (book publication date) | Days to weeks (`refresh_ttl_days`, default 30) |
| **Schema** | `book` → `chapter` → `chapter_embedding` | `doc_source` → `doc_snapshot` → `doc_section` → `doc_section_embedding` |
| **Today's count** | 577 books, 119K chapters | 109 sources, 9.5K sections |

### Why one without the other isn't enough

A book-only KB tells you what consistent hashing *is*, but doesn't know that DynamoDB now uses adaptive capacity. A doc-only KB tells you the current API, but never explains why the API is shaped that way. Most knowledge bases pick one. myPub keeps both.

The ranker has two factors that make this concrete:

- **`corroboration`** — when a query hits a topic where book and live doc both exist, it gets a boost. The signal comes from the `alignment_edge` table (1,296 CORROBORATES edges across all 75 sources today, plus 24 CONTRADICTS — see [docs/concept-graph.md → alignment edges](docs/concept-graph.md#alignment-edges)).
- **`doc_alignment`** — *whether the query domain has any live-doc coverage at all*. 1.00 if yes, 0.50 (neutral) if no. This stops the ranker from penalizing queries that *can't* be corroborated because no live doc exists for that topic.

### The book-vs-doc tension, made visible

```text
Query: "kafka exactly-once configuration"

Book candidate                       Live-doc candidate
─────────────────────                ───────────────────
"Kafka: The Definitive Guide"        Apache Kafka — Context7
ch. 6, Confluent · 2017              snapshot 2026-04-22
                                     "Producer Configuration"
relevance       0.81                 relevance       0.79
recency         0.10  (9 yrs)        recency         0.99  (3 wks)
authority       0.85  (O'Reilly)     authority       0.60  (Context7)
corroboration   0.62  (linked)       corroboration   0.62  (linked)
doc_alignment   1.00                 doc_alignment   1.00

balanced_interactive  → book wins (0.65 vs 0.62) by relevance + authority
currency_critical_int → doc wins  (0.60 vs 0.43) — recency leads at 0.40

So the *same* query against the *same* substrate routes differently
depending on the profile. Want to learn what exactly-once *is*? Use the
book route. Want to ship Kafka 3.x today? Use the doc route. Want both?
The interactive mode shows them side by side and flags any conflicts.
```

The catalog tracks **109 live doc sources** today (mixed Context7 / DeepWiki / GitHub-raw — DeepWiki increasingly preferred for OSS libraries after the PDF + Rust expansion surfaced that Context7 returns dense-but-thin API summaries while DeepWiki returns full architectural docs) covering 9.5K sections. The breakdown spans the working stacks the corpus actually has books about — data platforms, ML frameworks, web frameworks, cloud services, databases, message queues, dev tooling. A representative sample by alignment density:

| Source | Provider | Sections | Alignment edges |
|---|---|---|---|
| LangChain | Context7 | 22 | 27 |
| React | Context7 | 23 | 34 |
| spaCy | Context7 | 24 | 31 |
| FastAPI | Context7 | 23 | 29 |
| OpenAPI | Context7 | 24 | 27 |
| scikit-learn | Context7 | 22 | 27 |
| GitHub | Context7 | 22 | 26 |
| Stable Diffusion | Context7 | 23 | 24 |
| Haystack | Context7 | 23 | 23 |
| LangGraph | Context7 | 21 | 23 |
| PySpark | Context7 | 34 | 23 |
| Apache Kafka | Context7 | 22 | 22 |
| Delta Lake | Context7 | 21 | 20 |
| OpenLineage / dbt / Terraform / Apache Airflow / … | Context7 | … | … |
| FastMCP | DeepWiki | 437 | 245 |
| DuckPGQ | DeepWiki | 282 | 170 |

Full per-source numbers in [docs/data-sources.md](docs/data-sources.md#registered-sources).

When a query mentions a library that's in *neither* the local books *nor* any registered live-doc source, the auto-discovery loop probes Context7 → DeepWiki → GitHub raw in tier order. First confident hit registers a new `doc_source` row; if multiple candidates score similarly, you're asked to pick. The catalog grew from 10 → 75 doc sources via this same path, sometimes in batched expansions and sometimes one source at a time during real queries. Full mechanics: [docs/data-sources.md](docs/data-sources.md) and [docs/ingestion-and-indexing.md → discovery](docs/ingestion-and-indexing.md#refresh-and-discovery).

---

## Documentation

Start here. Everything is cross-linked.

| Doc | What's in it |
|---|---|
| [**Hello, myPub!**](docs/getting-started.md) | Zero to first generator output in 15 minutes |
| [Architecture](docs/architecture.md) | Substrate, ranking engine, Phase 7 framework, concurrency |
| [Data sources](docs/data-sources.md) | What goes in: ePubs, Context7, DeepWiki, GitHub raw |
| [Ingestion & indexing](docs/ingestion-and-indexing.md) | Structural parse, FTS, vector embeddings, DuckPGQ, alignment |
| [Concept graph](docs/concept-graph.md) | Entity extraction, EntityResolver, alignment edges, procedures |
| [Generators](docs/generators.md) | Catalog of all 24 generators with sample outputs |
| [Customization](docs/customization.md) | Weight profiles, character profiles, adding your own generator |
| [Operations](docs/operations.md) | Refresh, eval, deferred work, disaster recovery, diagnostics |

Canonical specs (deeper, design-doc level):

- [`docs/mypub-v2-architecture.md`](docs/mypub-v2-architecture.md) — full system design
- [`docs/mypub-v2-execution-plan.md`](docs/mypub-v2-execution-plan.md) — phased roadmap
- [`docs/mypub-v2-generators.md`](docs/mypub-v2-generators.md) — generator specifications

---

## What you can do with it

Three tasks, three actual conversations. None of these need a slash command — natural language routes through the `mypub-kb` MCP server.

### 1. Search across books and live docs at once

```text
You: search the kb for change data capture

mypub-kb returns:

  Primary
    "Capturing All Database Changes" — Designing Data-Intensive
    Applications, Martin Kleppmann · O'Reilly · 2017 · ch. 11

  Corroborations
    "Kafka Connect" — Apache Kafka live doc · Context7 · refreshed
    2026-04-22
    "Event Sourcing" — Implementing Domain-Driven Design, Vaughn
    Vernon · ch. 8

  Conflicts
    (none — book and live-doc descriptions agree)

  Score breakdown for primary:
    relevance      0.78   recency       0.40
    authority      0.85   corroboration 0.62   doc_alignment 1.00
```

Five factors, weighted by `weight_profile`. Conflicts are first-class output — the assistant can say "Kleppmann says X but the current Kafka doc says Y." See [Architecture → ranking engine](docs/architecture.md#the-retrieval-engine).

### 2. Compare how your authors handle a concept

```text
You: who in my library discusses CQRS, and how do they differ?

mypub-kb returns (via compare_concept_across_authors):

  Vaughn Vernon — Implementing Domain-Driven Design (Addison-Wesley)
    ch. 4: "Architecture" — CQRS as a tactical pattern
    ch. 8: "Domain Events" — pairing CQRS with event sourcing

  Greg Young — Versioning in an Event-Sourced System
    ch. 2: "Read Models" — projection patterns
    ch. 5: "Polyglot Persistence" — when CQRS earns its complexity

  Sam Newman — Building Microservices, 2nd ed.
    ch. 5: "Implementing Microservice Communication" — read/write
    separation as a service-boundary tool

  Martin Fowler — Patterns of Enterprise Application Architecture
    Reference: brief mention; deeper treatment on bliki

  …continued for 6 authors total
```

This is `compare_concept_across_authors` — one of four MCP tools the server exposes. See [Architecture → MCP tools](docs/architecture.md).

### 3. Generate a runnable project scaffold

This is the headline. The user's #1 motivating example:

```text
You: /kb-bootstrap Python CQRS event-sourced order service with Kafka and HL7

[stack]       inferred from request: python
              (override with the `stack` parameter — also accepts
               rust / node / typescript / java / go / csharp / ruby)

[decompose]   12 concept clusters identified
              (CQRS, Event Sourcing, Aggregate, CommandHandler,
               Projection, Kafka producer, Kafka consumer,
               Kafka Connect, HL7 v2 parser, docker-compose,
               pytest fixtures, OpenAPI spec)

[plan]        23 files projected (python stack template)

[validate]    unresolved targets:    0
              unmatched procedures:  0
              WARNING: 0 procedures in catalog for HL7 — the HL7
              layer will be doc-only. Acquire HL7 books or accept
              that the scaffold won't be runtime-validated for HL7.

[materialize]
              data/generated-packages/cqrs-kafka-hl7-bootstrap_<ts>/
              ├── README.md
              ├── docker-compose.yml
              ├── kafka/topic-config.yml
              ├── services/order-command/
              │   ├── pyproject.toml
              │   ├── src/handlers.py
              │   └── tests/test_handlers.py
              ├── services/order-query/
              ├── hl7/v2_parser.py
              ├── docs/architecture.md
              └── _prompts/                — sub-agent prompts
                                              (one per file)
```

The v1 generator emits skeleton + per-file sub-agent prompts in the right shape for the target stack (Python, Rust, Node, TypeScript, Java, Go, C#, Ruby, or a minimal generic skeleton). You dispatch Task agents to fill them in from the prompts. v2 wraps the dispatch loop and adds runtime validation — running the stack's build + test command (`cargo build && cargo test` for Rust, `pytest` for Python, `mvn verify` for Java, `npm test` for Node, etc.) plus a smoke check on the data flow. See [Generators → Project Bootstrap](docs/generators.md#project-bootstrap).

---

## Hello, myPub! — three commands to first output

```bash
git clone <this-repo> myPub && cd myPub
git checkout v2-substrate
python3 -m venv .venv
.venv/bin/python3 -m pip install -e ".[dev]"

# 1. Build the substrate (5–70 min depending on library size)
.venv/bin/python3 scripts/migrate_v2_schema.py
.venv/bin/python3 scripts/install_extensions.py
.venv/bin/python3 scripts/index_books.py --source ~/Documents/eBooks
.venv/bin/python3 scripts/generate_embeddings.py
.venv/bin/python3 scripts/build_fts_index.py
.venv/bin/python3 scripts/build_vss_index.py
.venv/bin/python3 scripts/build_property_graph.py

# 2. Start Claude Code with the project's .mcp.json
claude .

# 3. Ask anything
#    "search the kb for event sourcing"
#    "compare how my authors discuss CQRS"
#    "/kb-concept-map event sourcing"
```

For the full walkthrough — including extraction, alignment, and the first generator — see [**docs/getting-started.md**](docs/getting-started.md).

---

## Architecture at a glance

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
│              Phase 7 Generator Framework (24 generators)             │
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
│   Profiles: balanced · currency_critical · foundational · skill_*    │
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
│   author · book · chapter · concept · concept_relation · procedure   │
│   doc_source · doc_snapshot · doc_section · alignment_edge           │
│   generated_package · skill_package · *_embedding side tables        │
└──────────────────────────────────────────────────────────────────────┘
                  │                                      │
                  ▼                                      ▼
        ┌────────────────────┐               ┌─────────────────────────┐
        │  ePub library      │               │  Live doc MCP servers   │
        │  ~/Documents/      │               │  Context7 · DeepWiki    │
        │  eBooks/ (572)     │               │  · GitHub raw           │
        └────────────────────┘               └─────────────────────────┘
```

Detailed walkthrough: [docs/architecture.md](docs/architecture.md).

---

## The substrate, today

| | Count |
|---|---|
| Books indexed | **572** |
| Chapters | **118,447** (118,110 with content + embeddings) |
| Authors | **820** |
| Concepts | **312,396** (every concept has a 384-dim embedding) |
| Concept-graph edges (CITES, IMPLEMENTS, REQUIRES, CONTRASTS_WITH, EXTENDS) | **612,607** |
| Procedures (precondition / steps / postcondition / failure modes / concept links) | **47,874** with **175,106** concept links |
| Live doc sources | **54** (52 Context7, 2 DeepWiki) |
| Doc snapshots / sections | 54 / **1,909** |
| Alignment edges (book chapter ↔ live-doc section) | **5,946** (5,872 CORROBORATES + 74 CONTRADICTS) |
| Generators on the Phase 7 framework | **17** |
| Tests | **37 test modules** (unit + live API) |

**Concept-relation breakdown:** 410K CITES (66%), 83K IMPLEMENTS, 58K REQUIRES, 45K CONTRASTS_WITH, 16K EXTENDS — the shape of a graph that's been earning its edges for a year.

**Top publishers:** O'Reilly (283), Manning (52), Packt (71 across imprints), Addison-Wesley + Wiley + Apress + Elsevier filling out the long tail.
**Top authors by book count:** Mark Richards & Valliappa Lakshmanan (6 each), Joe Celko & Neal Ford (5 each), Brian Kernighan, Khaled El Emam, Martin Fowler, Ralph Kimball (4 each).

The full live-doc-source breakdown is in the [Two kinds of input](#two-kinds-of-input--and-why-both) section above; per-source details are in [docs/data-sources.md](docs/data-sources.md).

---

## The indexing & retrieval engine

The substrate is more than a database — it's a hybrid retrieval engine designed around one premise: *no single signal is sufficient*. A keyword index misses paraphrases; a vector index misses exact-phrase wins; a graph misses anything not yet linked; recency alone overweights stale-but-current docs; authority alone overweights big publishers writing about yesterday's API. So myPub indexes four ways, fuses them, then re-scores against five factors that capture the dimensions that actually matter for a technical query.

### Four indexing layers, each pulling its weight

| Layer | What it indexes | What it solves | Why it matters |
|---|---|---|---|
| **FTS (BM25, Porter-stemmed)** | `chapter.content` and `doc_section.content` | Exact-phrase wins. "BM25", "Lambda Architecture", `enable.idempotence` — terms that semantic search dilutes by averaging | Without FTS, querying for a specific config flag returns chapters that *talk about* config flags but don't mention this one |
| **VSS (HNSW, cosine)** | 384-dim `sentence-transformers/all-MiniLM-L6-v2` embeddings, in side tables | Paraphrases and conceptual neighbors. "consistent hashing" finds chapters about Rendezvous, Maglev, and DHT — even when those words don't appear in the query | Without VSS, the system can only find what you asked for verbatim |
| **DuckPGQ property graph** | `mypub` graph: `author`/`book`/`chapter`/`concept` vertex tables, `wrote`/`book_contains`/`concept_relates_to` edges | "X relates to Y" queries that pure search can't express — author roll-ups, prerequisite walks | Without the graph, comparing how three authors handle CQRS becomes manual collation |
| **Concept graph + alignment edges** | 337K concepts, 657K relations (CITES / IMPLEMENTS / REQUIRES / CONTRASTS_WITH / EXTENDS), 5,946 cross-source alignment edges | Cross-document reasoning: prerequisites, contrasts, book↔doc agreement and disagreement | Without this, the system is a pile of independent text files; with it, it's a knowledge structure |

Embeddings live in side tables (`chapter_embedding`, `concept_embedding`, etc.) instead of as columns on the parent. This isn't aesthetic — DuckDB 1.5.0 raises a spurious FK violation on `UPDATE chapter SET embedding = …` when the row has inbound FKs. The pinned regression test `tests/test_duckdb_fk_bugs.py` exercises three such bugs we work around. Full schema rationale: [docs/architecture.md → substrate](docs/architecture.md#the-substrate-duckdb).

### How a query flows through

```text
                         ┌────────────────────────────────────┐
   query ────────────────►   FTS over chapter.content (BM25)   │──┐
                         └────────────────────────────────────┘  │
                         ┌────────────────────────────────────┐  │   RRF
                         │   VSS over chapter_embedding       │──┼─► fuse ──► top-K
                         │   (cosine, HNSW, k=20)             │  │           candidates
                         └────────────────────────────────────┘  │
                         ┌────────────────────────────────────┐  │
                         │   Graph proximity via concept      │──┘
                         │   relations + DuckPGQ              │
                         └────────────────────────────────────┘
                                                                ▼
                       Top-K candidates re-scored against five factors:
                       relevance × recency × authority × corroboration × doc_alignment
                                                                ▼
                                         Final ranked list  +  conflicts list
```

The fusion step uses reciprocal rank fusion (RRF, k=60) — each modality contributes a rank, the candidate's score is `Σ 1/(k + rank)`. Each modality picks up something the others miss, and RRF lets a candidate that lands top-5 in two of three modalities beat one that lands top-1 in only one.

### Five factors — what each one is for

This is the part that distinguishes myPub from a vector-only RAG. After fusion, the top candidates are re-scored on five dimensions; the `weight_profile` parameter chooses the linear combination.

| Factor | What it measures | What it solves |
|---|---|---|
| **`relevance`** | The fused FTS+VSS+graph score | Base signal — does this candidate *match* the query at all |
| **`recency`** | Age of source — `book.publication_date` or `doc_snapshot.retrieved_at`, exponential decay | Stops a 2018 Kafka book from drowning out the current Kafka 3.x doc when the user asked about *now* |
| **`authority`** | Publisher tier for books (O'Reilly / Manning graded high; defaults below); doc-source tier (Context7=0.60, DeepWiki=0.50, GitHub=0.40) for live docs | Lets the ranker prefer a deeply-edited O'Reilly chapter over an AI-generated DeepWiki section when both are relevant |
| **`corroboration`** | Boost when an `alignment_edge` exists between the candidate and another source | Rewards "book and current doc agree on this" — the highest-confidence signal myPub produces. 1,296 CORROBORATES edges live, and 24 CONTRADICTS edges that penalize symmetrically when book and doc disagree |
| **`doc_alignment`** | Whether the query domain has live-doc coverage *at all* — 1.00 for the 75 aligned sources, 0.50 (neutral) elsewhere | Stops queries about un-aligned topics from being penalized for an absence of corroboration that's not their fault |

Six built-in profiles cover the common axes:

| Profile | rec | doc | rel | corr | auth | When to use |
|---|---|---|---|---|---|---|
| `balanced_interactive` | 0.10 | 0.10 | 0.45 | 0.15 | 0.20 | General Q&A |
| `currency_critical_interactive` | 0.40 | 0.25 | 0.20 | 0.10 | 0.05 | "What does Kafka do *now*?" — recency leads |
| `foundational_interactive` | 0.05 | 0.10 | 0.35 | 0.30 | 0.20 | Algorithms, classical CS, design pattern theory |
| `skill_recent_doc` | 0.30 | 0.30 | 0.25 | 0.05 | 0.10 | Skills Factory: anchor on current vendor docs |
| `skill_consensus` | 0.05 | 0.10 | 0.30 | 0.35 | 0.20 | Skills Factory: synthesize where book + docs agree |
| `skill_authority` | 0.05 | 0.10 | 0.25 | 0.10 | 0.50 | Skills Factory: defer to the most authoritative source |

The `Weights` dataclass enforces that the five values sum to ~1.0 in `__post_init__` — typo'd profiles fail loudly at import rather than silently producing out-of-range scores. Definitions live in [`mcp-servers/kb-mcp/ranking.py`](mcp-servers/kb-mcp/ranking.py); add your own per [docs/customization.md → weight profiles](docs/customization.md#weight-profiles).

### Two retrieval modes — interactive vs. generation

Same retrieval pool, two output shapes:

| Mode | Returns | Used by |
|---|---|---|
| **`interactive`** | `{primary, corroborations, conflicts, all_scored, by_modality, discovery}` | Natural-language Q&A; the assistant can say "Kleppmann says X but the current Kafka doc says Y" because conflicts are first-class output |
| **`generation`** | A curated, deduplicated list under one of three §8.3 selection strategies (`recent_doc_anchored`, `consensus_synthesis`, `book_authoritative`) | Generators that need a clean source list to feed into a Decomposer — silent, no conflict-surfacing |

That conflict-surfacing in interactive mode isn't a UI feature, it's a retrieval feature. The system is *designed* to find disagreements between sources and put them in front of you.

### Auto-discovery — the catalog grows when it needs to

When a query mentions a library that's in *neither* the book corpus *nor* any registered live-doc source, `search_chapters` doesn't return zero results — it triggers a discovery probe. The MCP server walks the providers in tier order:

```text
Context7  (authority 0.60)  ─┐
DeepWiki  (authority 0.50)  ─┼─►  first confident hit registers a new doc_source
GitHub    (authority 0.40)  ─┘
```

If multiple candidates score similarly, the search returns `outcome="asked_user"` with a candidate list; you pick; `disambiguate_discovery` registers the choice; the original query re-runs. Every probe is logged to `discovery_log` so you can audit how the catalog grew. Full mechanics: [docs/ingestion-and-indexing.md → refresh and discovery](docs/ingestion-and-indexing.md#refresh-and-discovery).

### What you talk to

Four MCP tools the `mypub-kb` server exposes, all auto-invoked from natural language:

| Tool | What it does |
|---|---|
| `search_chapters(query, mode, limit, weight_profile, auto_discover, selection_strategy)` | The full pipeline above — fan-out, fuse, re-score, return |
| `compare_concept_across_authors(concept_name, limit_per_author)` | Per-author roll-up of chapters that discuss a concept |
| `find_prerequisites(concept_name, max_depth)` | Recursive walk over `REQUIRES` edges; returns each prerequisite at its shortest depth |
| `disambiguate_discovery(source, identifier, display_name, query_term)` | Closes the auto-discovery loop on user choice |

For a search, comparison, or prerequisite walk, just ask Claude in natural language — the routing happens automatically. Slash commands are reserved for multi-step workflows (`/kb-discover` for an explicit discovery loop, `/kb-review-concepts` for the EntityResolver queue, and the 24 generator commands).

---

## The generator framework — where retrieval becomes artifacts

A search result tells you where to read. An ADR, a tutorial, a runnable scaffold tells you what to *do*. The generator framework is the layer that turns retrieved sources into reproducible, citation-backed, deterministic artifacts on disk.

### Why one framework, not seventeen tools

A `/kb-cheatsheet` and a `/kb-bootstrap` look completely different to the user — one produces a one-page reference, one produces a 23-file project scaffold. But internally they share 80% of the work:

- Both decompose a topic into concept clusters using the concept graph
- Both plan an output structure (cheatsheet has 1 file; bootstrap has 23)
- Both validate that every claim has a source and every step has a procedure
- Both materialize to disk with provenance
- Both record the run in `generated_package` / `generated_unit` / `generated_file` / `generated_source`

The framework owns this shared shape. Each generator only writes what makes it different. Adding the seventeenth generator (Curriculum) was ~150 lines of code on top of the framework, plus its slash command and tests.

### The four protocols every generator implements

```text
                ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
inputs ────────►│ Decomposer   │───►│ Planner      │───►│ Validator    │───►│ Materializer │────► output
                └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                concept clusters    file layout +       resolve targets,    write files,
                + retrieval pool    sub-agent prompts   match procedures,   record provenance
                                                        confidence floor
```

| Protocol | Job | Reuses from substrate |
|---|---|---|
| **Decomposer** | Turn "topic + stack" into concept clusters and a candidate retrieval pool | `search_chapters(mode="generation")`, `find_prerequisites`, `compare_concept_across_authors`, the concept graph |
| **Planner** | Turn clusters into a file layout (path, template, sources to cite per file) and per-file sub-agent prompts | The selection strategy chosen by the generator |
| **Validator** | Refuse to materialize unless every concept-target resolves, every cited chapter exists, every procedure-backed step matches a real `procedure` row, and the per-file confidence floor is met | `EntityResolver`, `concept_relation`, `procedure` |
| **Materializer** | Write files to disk with a `manifest.json`, INSERT into `generated_*` tables for full provenance | `data/generated-packages/<name>_<timestamp>/` and the Phase 7 framework persistence layer |

### What you get for free

This is the value prop — when you write a new generator, you don't write any of:

| | What the framework gives you |
|---|---|
| **Citation provenance** | Every materialized file gets rows in `generated_source` linking back to the chapter / doc_section / procedure that backed it. You can audit the source list with one SQL query |
| **Reproducibility** | Same inputs + same catalog state → same output files (modulo timestamp). No sneaky LLM nondeterminism in the structural pipeline |
| **Validation guarantees** | Unresolved concepts, unmatched procedures, missing sources all *block* materialization. The Validator is the safety net that keeps the generator from emitting "step 3: …" without a real source |
| **Sub-agent dispatch pattern** | Generators that need prose (Skills Factory, Project Bootstrap) emit per-file `*.prompt.md` files. The dispatch loop is shared infrastructure (Skills Factory's prep/process pattern) |
| **Idempotent re-runs** | Re-running a generator produces a new timestamped directory; old runs aren't clobbered. Good for diff-vs-prior-run audits |
| **Persistence schema** | `generated_package` (one row per run), `generated_unit` (concept clusters), `generated_file` (every materialized file), `generated_source` (provenance per file). All managed by the framework base class |

### The nineteen generators today

Outputs land under `data/generated-packages/<name>_<timestamp>/`. Each entry below links to the full walkthrough in [docs/generators.md](docs/generators.md).

| Category | Generator | Slash command | One-line value |
|---|---|---|---|
| Skills & curriculum | [Skills Factory](docs/generators.md#skills-factory) | `/kb-generate-skills` | Generate a complete Claude Skills package |
| | [Concept Neighborhood Map](docs/generators.md#concept-neighborhood-map) | `/kb-concept-map` | Visualize a concept's local graph |
| | [Learning Path](docs/generators.md#learning-path) | `/kb-learning-path` | Topologically-sorted reading order to a target |
| | [Curriculum](docs/generators.md#curriculum-composite) | `/kb-curriculum` | Multi-week course composing Learning Paths + Tutorials |
| Reference & teaching | [Quickstart](docs/generators.md#quickstart) | `/kb-quickstart` | First-contact for any library: install + hello-world + verify (language inferred from corpus content; no hardcoded ecosystem assumptions) |
| | [Cheatsheet](docs/generators.md#cheatsheet) | `/kb-cheatsheet` | One-page quick reference with citations |
| | [Slide-Deck Outline](docs/generators.md#slide-deck-outline) | `/kb-slides` | 30–60 minute talk skeleton |
| | [Tutorial](docs/generators.md#tutorial) | `/kb-tutorial` | Step-by-step walkthrough backed by procedures |
| | [Content Brief](docs/generators.md#content-brief) | `/kb-content-brief` | Article / blog skeleton with sources |
| | [Pattern + Anti-Pattern Catalog](docs/generators.md#pattern--anti-pattern-catalog) | `/kb-pattern-catalog` | Patterns paired with the anti-patterns to avoid |
| Decisions & strategy | [ADR](docs/generators.md#adr-architecture-decision-record) | `/kb-adr` | Architecture Decision Record with options + rationale |
| | [Tech Assessment](docs/generators.md#tech-assessment) | `/kb-tech-assessment` | Maturity, fit, risk, alternatives — one decision, N candidates |
| | [Library Landscape](docs/generators.md#library-landscape) | `/kb-landscape` | M jobs-to-be-done × N candidates orientation for any domain — candidates auto-discovered by concept-overlap; rarity-weighted scoring + vector job seeding |
| | [Migration Guide](docs/generators.md#migration-guide) | `/kb-migration-guide` | CONTRADICTS-edge-driven version migration |
| | [Currency Report](docs/generators.md#currency-report) | `/kb-currency-report` | Where books and live docs disagree |
| Voice & character | [Dialog](docs/generators.md#dialog) | `/kb-dialog` | Two-character conversation (Architect vs Practitioner) |
| | [Author Panel](docs/generators.md#author-panel) | `/kb-author-panel` | Multi-author roundtable |
| Bootstrap & refactor | [**Project Bootstrap**](docs/generators.md#project-bootstrap) ★ | `/kb-bootstrap` | Runnable project scaffold for any of 8 language stacks (python / rust / node / typescript / java / go / csharp / ruby) — detected from request keywords or set via the `stack` parameter |
| | [Refactoring Playbook](docs/generators.md#refactoring-playbook) | `/kb-refactoring` | Targeted refactor with before/after |
| Healthcare interop | [EDI Round-Trip](docs/generators.md#edi-round-trip-test) | `/kb-edi-roundtrip` | X12 transaction-set fixtures + parse/round-trip tests (270/271, 834, 835, 837, 997, 999) |
| | [De-identification Bundle](docs/generators.md#de-identification-procedure-bundle) | `/kb-deid-bundle` | HIPAA Safe-Harbor de-id pipeline scaffold + audit trail (FHIR / DICOM / HL7v2 / clinical trial) |
| | [Standards Translator](docs/generators.md#standards-translator) | `/kb-standards-translator` | Cross-standard mapping package (HL7v2↔FHIR, X12↔FHIR, DICOM→FHIR) — table + transformer + tests |
| | [FHIR IG Scaffold](docs/generators.md#fhir-implementation-guide-scaffold) | `/kb-fhir-ig` | Buildable SUSHI/FSH IG repo (bulk export, IPS, tumor board, prior auth, AE reporting) |
| | [Integration Channel](docs/generators.md#integration-channel-generator) | `/kb-integration-channel` | Mirth Connect channel.xml + JS transformer for healthcare data flows |

★ Project Bootstrap is the canonical substrate-validation case. CQRS+Kafka+HL7 is the test that proves *books-plus-docs synthesis is necessary, not nice-to-have* — a 2020 Kafka book alone produces scaffolds that don't run on current Kafka. Currency-aware ranking is load-bearing for Bootstrap. The other generators could run on books alone; Bootstrap can't.

The five **healthcare interop** generators are a recent addition built on the new healthcare doc sources (FHIR, HL7v2, X12, DICOM, EHR specs). They show the framework's portability — same Decomposer/Planner/Validator/Materializer protocols, completely different domain. Two highlights: the Standards Translator builds field-by-field cross-standard mappings (HL7v2 ADT → FHIR Patient/Encounter, X12 837 → FHIR Claim) directly from documented industry transforms; the De-identification Bundle generates a HIPAA-Safe-Harbor-mapped pipeline + audit trail for any healthcare dataset shape. Same framework, different domain.

Each healthcare package is a thin consumer of the `healthcare_libs` reference implementations (`x12`, `hl7v2`, `fhir`, `dicom`, `deid`, `cross_standards`, `integration_channel_xml`) — generated `transformer.py` files import the lib rather than carrying inline X12/FHIR templates. The packages also ship a `_sub_agent_prompts/` directory mirroring [Project Bootstrap's](docs/generators.md#project-bootstrap) pattern: per-customization-point prompts (trading partner, payer IG deviations, channel topology, deployment runbook, …) the user can dispatch via the Task tool to fill in deployment-specific details on top of the deterministic base. 487 tests cover the libs (320), the generators (138), and the dispatch layer (29).

### Adding your own

The framework makes new generators cheap. The full procedure is in [docs/customization.md → adding a generator](docs/customization.md#adding-a-generator), but the shape is:

1. Implement `Decomposer.decompose(inputs) -> [GeneratedUnit]`
2. Implement `Planner.plan(units) -> [PlannedFile]` (path + template + sources per file)
3. Implement `Validator.validate(plan)` (the framework's base class handles the common checks; override for generator-specific rules)
4. Implement `Materializer.materialize(plan, output_dir)` (most generators inherit from a markdown / multi-file base)
5. Wire a slash command in `.claude/commands/kb-<your-generator>.md`
6. Add 6–12 unit tests + 1 live integration test

Most existing generators are 200–600 lines. The framework absorbs the rest.

---

## Repository layout

```text
myPub/
├── README.md                          # this file
├── CLAUDE.md                          # project instructions for Claude Code
├── docs/                              # the eight guides linked above
│   ├── getting-started.md             # Hello, myPub!
│   ├── architecture.md                # substrate + framework
│   ├── data-sources.md                # ePubs + live docs
│   ├── ingestion-and-indexing.md      # FTS, VSS, DuckPGQ pipeline
│   ├── concept-graph.md               # extraction, alignment, procedures
│   ├── generators.md                  # all 24 generators
│   ├── customization.md               # weight profiles, characters, new generators
│   ├── operations.md                  # re-ingestion, refresh, eval, recovery
│   └── mypub-v2-*.md                  # canonical specs
├── mcp-servers/kb-mcp/                # FastMCP server + 24 generators
│   ├── server.py · ranking.py · discovery.py · resolution.py
│   ├── generator.py                   # Phase 7 framework protocols
│   ├── concept_map.py · learning_path.py · cheatsheet.py · …
│   └── skills_factory.py · skill_generation.py · skills_eval.py
├── scripts/                           # build, ingest, refresh, migrate
├── schemas/                           # catalog.sql + property_graph.sql
├── tests/                             # 37 test modules
├── .claude/
│   ├── commands/                      # 19 /kb-* slash commands
│   └── skills/                        # kb-usage, skills-factory
├── data/                              # catalog.ddb (gitignored)
└── pyproject.toml
```

---

## Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Substrate | DuckDB 1.5.0 (pinned) | Single-file catalog; embeddings via VSS; FTS via BM25; graph via DuckPGQ — no external services |
| Embeddings | sentence-transformers/all-MiniLM-L6-v2, 384-dim | Local, fast, free; embeddings live in side tables to dodge a 1.5.0 FK bug |
| Granularity | Chapter / heading-aligned section | Preserve author structure; most chapters fit Claude's context (4K–17K tokens) |
| Ranking | 5-factor weighted score, two modes | `generation` mode is silent; `interactive` mode surfaces conflicts as first-class output |
| Live docs | Context7 → DeepWiki → GitHub probe order | Authority-tiered fallback (0.60 / 0.50 / 0.40) when a library isn't on a higher source |
| Alignment | Separate `alignment_edge` table | Corroboration / contradiction is a distinct relation kind; 1,354 CORROBORATES + 24 CONTRADICTS edges live, computed via Sonnet 4.6 alignment passes per snapshot |
| Generators | One framework, 17 implementations | Decomposer + Planner + Validator + Materializer protocols — every generator is the same shape |
| Personality | Character profiles | View functions over the ranking engine; same substrate, different voices |
| Concurrency | RO-by-default; writers pass `read_only=False` explicitly | DuckDB's exclusive file lock excludes RO too — explicit intent at every write site |

---

## Lineage

myPub inherits design patterns from sibling projects:

- **[healthsim-workspace](https://github.com/mark64oswald/healthsim-workspace)** — the Claude Code skill-driven development model, hierarchical seed manager, close-before-write DuckDB pattern, idempotent migration scripts
- **[vantage](https://github.com/mark64oswald/vantage-docs)** — the conversational-strategist surface (slash commands as multi-step workflow shortcuts; natural language as the default), and the "comparison + report generator" output pattern
- **[BioScienceAgent](https://github.com/mark64oswald/BioScienceAgent-docs)** — the sub-agent dispatch pattern used by the Skills Factory and Project Bootstrap (prep → dispatch → process → materialize)

The domain content (technical books, vendor live docs, code generators) is myPub-specific.

---

## Engineering depth — the things that didn't go in the spec

A year of building this surfaced real problems, none of which appeared in the v2 design doc. They're worth flagging because they describe what "production-quality" actually means for a system like this:

- **DuckDB FK bug pinned in regression tests.** DuckDB 1.5.0 raises a spurious foreign-key violation on `UPDATE chapter SET embedding = …` when the row has inbound FKs. The fix wasn't to wait for upstream — it was to put embeddings in side tables (`chapter_embedding`, `concept_embedding`, etc.). Three FK-bug variants are pinned in [`tests/test_duckdb_fk_bugs.py`](tests/test_duckdb_fk_bugs.py) so a patched DuckDB doesn't silently regress us.
- **The HNSW-vs-DELETE pathology.** DuckDB's HNSW (vector) index makes individual-row deletion catastrophically slow because it rebuilds the graph per delete. A 479-row delete sat for 22+ minutes before we killed it. The pattern: drop index → delete → rebuild index. 5 seconds total. Worth knowing if any catalog operation deletes rows from `chapter_embedding`.
- **Phase 1 splitter bug + in-place migration.** ePub TOCs frequently point multiple TOC entries at the same xhtml file via fragment anchors. The original splitter handed every TOC entry the *full file* content, producing 88.5% chapter-content duplication. The fix sliced at fragment boundaries; the migration was in-place (preserved `chapter_id`s) so 154 alignment edges, embeddings, and skill-source FKs survived.
- **Empty-OPF ePubs.** Some Packt ePubs ship with a 0-byte `OEBPS/content.opf`. ebooklib refuses to open them, even though the actual chapter xhtml is intact. The fallback path ([`/tmp/recover_broken_epub.py`](.) — not committed; a hand-rolled recovery indexer) parses the TOC xhtml directly and synthesizes the spine.
- **Author-smush in `<dc:creator>`.** Some ePubs encode all co-authors in a single `<dc:creator>` element separated by ", " or " and ". The naive parser captured "Stephen Buxton, Lowell Fryman, Ralf Hartmut Güting, …" as one author. A robust splitter (with credential-suffix re-merging for "M.D.", "Ph.D.", "Jr.") fixed 161 rows.
- **Concept-name collisions across `concept_type`.** "Event Sourcing" exists as both `Concept` and `Pattern`. The schema's `UNIQUE(name, concept_type)` allows this by design, but it broke the resolver early on: the empty twin won the lookup. Fix: order resolver matches by edge count (`ecc74f4`); the richest twin wins. The `dedupe_concepts.py` script collapses strict orphans (no edges, no aliases, no procedure links) — it's removed 8,326 such concepts to date.
- **Polymorphic source IDs.** `concept_relation.source_id` and `procedure.source_id` are typed by a `source_type` discriminator (`chapter` vs `doc_section`). No FK enforcement — it's a polymorphic association — but it lets one schema serve both ingestion paths without duplication.
- **Alignment is non-deterministic.** Re-running alignment on the same prompts produces different edges. We learned this the hard way: an earlier run had 9 high-confidence CONTRADICTS edges (FastMCP allowing breaking changes in minor versions, contradicting SemVer textbooks); the re-run rolled the dice differently and lost most of them. For high-stakes edges, multi-sample voting is the right answer.

The pattern across all of these: trust internal code and framework guarantees; only validate at system boundaries (user input, external APIs); but pin every nontrivial bug as a regression test so the lesson survives.

---

## Status

The v2 substrate, ranking engine, concept graph, all 24 generators, and the doc-augmentation layer have shipped. The catalog is in continuous-improvement mode rather than feature-build mode.

Recent rounds of work:

- **Live-doc expansion 10 → 75 sources** via batched Context7 discovery, with a Batch-API-driven extraction + alignment pipeline (Haiku 4.5 with prompt caching for extraction, Sonnet 4.6 for alignment).
- **Catalog cleanup.** All 20 books missing authors resolved via OpenLibrary + Google Books ISBN lookup; 161 author-smush rows split; 8,326 strict-orphan duplicate concepts removed; 3 stranded doc sources recovered (FastMCP, DuckPGQ, MLflow) adding 448 alignment edges.
- **New-book ingestion path validated.** 32 new ePubs identified by hash diff, 30 indexed cleanly, 2 broken (empty OPF) recovered via custom indexer, 1 Safari-rename duplicate caught at chapter-content-hash time.
- **Re-alignment against expanded chapter pool.** All 75 doc sources re-aligned; 53 new edges from new-book chapters surfaced (e.g. React Compiler currency drift between docs and book).

Honest open work — none blocking real use:

- **CONTRADICTS quality.** The alignment edges are valid CORROBORATES at avg conf 0.72; CONTRADICTS edges are mostly degenerate (avg conf 0.16). A contradiction-tuned alignment prompt + multi-sample voting would meaningfully sharpen Migration Guide and Currency Report output.
- **Generator-output validation.** The 24 generators all ship and run, but real eval data — running `/kb-currency-report`, `/kb-migration-guide`, `/kb-bootstrap` against the now-richer substrate and grading the output — is the open question. That's how you know if the substrate actually delivers, not just that it ingested cleanly.
- **Domain gaps.** The corpus has decent biology/genomics books now (Biology for Engineers, NGS Data Analysis, Zero to Genetic Engineering Hero) but zero PubMed Central / clinical-trial papers / HL7-FHIR specs. Different ingestion paths than ePubs (JATS XML, FHIR resources). Would matter for life-sciences use cases.
- **Project Bootstrap v2.** The v1 generator emits skeletons + sub-agent prompts, with stack-aware scaffolding for python / rust / node / typescript / java / go / csharp / ruby. v2 wraps the dispatch loop and adds stack-specific runtime validation (the right `build + test` command for each stack: `cargo build && cargo test`, `pytest`, `mvn verify`, `npm test`, `go test ./...`, `dotnet test`, etc.) plus a smoke check on the data flow.

Full deferred-work tracker: [docs/operations.md](docs/operations.md#deferred-work).

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

The catalog database, embeddings, and ePub originals are *not* distributed. Only the substrate code, schema, generator framework, and tests are in this repository. Build your own catalog from your own ePub library following [docs/getting-started.md](docs/getting-started.md).

---

*myPub indexes a personal technical library and live vendor documentation for individual learning and authoring. No proprietary content from publishers is redistributed; only metadata, structure, and locally-derived embeddings are stored in the catalog.*
