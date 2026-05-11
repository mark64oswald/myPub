# myPub Architecture

A start-with-why walkthrough of the substrate, the retrieval engine, and the generator framework.

[← back to top-level README](../README.md) · [Hello, myPub! ↗](getting-started.md) · [Canonical spec ↗](mypub-v2-architecture.md)

---

## Why myPub looks the way it does

Three observations drive every decision in the system:

1. **Books are too good to chunk.** Authors structure books deliberately. Chapters are coherent units of explanation; a Kleppmann chapter on consistency is more valuable to Claude than 200 disjoint chunks of the same chapter. So the unit of retrieval is the *author's chapter*, not a 512-token window.
2. **Books and live docs answer different questions.** A 2018 Kafka book teaches you what an idempotent producer *is*. The current Kafka docs tell you that `enable.idempotence=true` is the default since 3.0. Both are necessary. So the substrate ingests *both*, and the ranker has a doc-alignment factor that knows which queries should lean recent.
3. **The interesting outputs aren't search results.** A search result tells you where to read. An ADR, a tutorial, a runnable scaffold tells you what to *do*. So on top of retrieval sits a generator framework — seventeen of them, all sharing the same Decompose → Plan → Validate → Materialize shape.

The rest of this document explains how each layer falls out of those three premises.

---

## The substrate (DuckDB)

One file: `data/catalog.ddb`. DuckDB 1.5.0 with three community extensions:

| Extension | Role |
|---|---|
| `fts` | BM25 full-text index on `chapter.content` and `doc_section.content`, Porter-stemmed |
| `vss` | HNSW (cosine) over 384-dim float arrays for chapters, doc sections, concepts, snapshots |
| `duckpgq` | Property graph queries (`MATCH … -[r:requires]-> …`) over the concept and bibliographic graph |

### Schema layers

```text
Bibliographic        Concept graph         Procedures        Live docs            Generators
─────────────        ─────────────         ──────────        ─────────            ──────────
author               concept               procedure         doc_source           skill_package
book                 concept_alias         procedure_concept doc_snapshot         skill
book_author          concept_relation                        doc_section          skill_file
chapter              concept_doc_link                        alignment_edge       skill_source
chapter_embedding    concept_embedding                                             skill_relation
                     concept_resolution                                            generated_package
                     concept_query_log                                             generated_unit
                                                                                   generated_file
                                                                                   generated_source
```

### Three rules the schema enforces

1. **Singular table names with BIGINT surrogate PKs.** No `users` plural; tables are `author`, `book`, `chapter`. Every PK is a BIGINT named `<table>_id`.
2. **Embeddings live in side tables.** A DuckDB 1.5.0 bug raises a spurious FK violation on `UPDATE chapter SET embedding = …` when the row has inbound FKs. So `chapter`, `concept`, `doc_section`, `doc_snapshot` each have a sibling `*_embedding` table with `(<id>, vector FLOAT[384])`. The ingestion scripts treat this as load-bearing.
3. **Self-referential FKs are application-enforced.** The same 1.5.0 bug mis-blocks UPDATE/DELETE on parents of self-referential FK chains. So `chapter.parent_chapter_id` and `doc_section.parent_id` are plain BIGINT columns; integrity is enforced in code, not by the database.

The full schema lives in [`schemas/catalog.sql`](../schemas/catalog.sql) and the property graph in [`schemas/property_graph.sql`](../schemas/property_graph.sql).

---

## The retrieval engine

### Three modalities, one fused score

Every search query fans out to three modalities in parallel, then merges with reciprocal rank fusion.

```text
                         ┌────────────────────────────────────┐
   query ────────────────►   FTS over chapter.content (BM25)   │──┐
                         └────────────────────────────────────┘  │
                         ┌────────────────────────────────────┐  │   RRF
                         │   VSS over chapter_embedding       │──┼─► fuse ──► top-K candidates
                         │   (cosine, HNSW, k=20)             │  │
                         └────────────────────────────────────┘  │
                         ┌────────────────────────────────────┐  │
                         │   Graph proximity via DuckPGQ /    │──┘
                         │   concept_relation                 │
                         └────────────────────────────────────┘
```

Why three? Each picks up something the others miss:

- FTS catches exact-phrase wins ("BM25", "Lambda Architecture") that semantic search dilutes.
- VSS catches paraphrases and conceptual neighbors that don't share tokens.
- Graph catches "this chapter discusses X *because* X requires Y, and Y is the query".

### Five-factor re-scoring

The fused candidate pool is then re-scored. The five factors:

| Factor | Sources | Mechanic |
|---|---|---|
| **relevance** | RRF score from FTS × VSS × graph | Already in [0, 1] post-fusion; treated as the base signal |
| **recency** | `book.publication_date`, `doc_snapshot.retrieved_at` | Exponential decay; half-life set per profile |
| **authority** | Publisher tier for books; doc-source tier (Context7=0.85, DeepWiki=0.70, GitHub=0.65) | Hand-set tiers; books inherit publisher rank |
| **corroboration** | `alignment_edge` table — CORROBORATES boosts, CONTRADICTS penalizes | 9,809 CORROBORATES + 150 CONTRADICTS edges live across all 150 doc sources |
| **doc_alignment** | Whether the query domain has live-doc coverage at all | 1.00 for the 150 aligned sources, 0.50 neutral elsewhere — prevents penalizing a query that *can't* be corroborated because no live doc exists |

The final score is a weighted combination determined by `weight_profile`. Profiles are tuned per use case — see [docs/customization.md](customization.md#weight-profiles).

### Two retrieval modes

```text
mode = "interactive"                       mode = "generation"
─────────────────────                       ───────────────────
{ primary,            }                     [
  corroborations,                              { source, score, citation, … },
  conflicts,                                   …  (curated, deduplicated)
  all_scored,                                ]
  by_modality,
  discovery
}
```

Interactive mode surfaces conflicts as first-class output — the assistant can say "Kleppmann says X, but the current Kafka doc says Y." Generation mode is silent: it's used by generators that need a clean, ranked list of sources to feed into a Decomposer.

The selection inside generation mode is one of three strategies (§8.3 of the canonical spec):

- `recent_doc_anchored` — pin to current vendor docs, supplement with book context
- `consensus_synthesis` — restrict to where book + live doc agree
- `book_authoritative` — prefer book sources for foundational concepts

---

## The auto-discovery loop

When a query mentions a library or framework that isn't in the catalog, the search detects "novel-library" patterns and probes external MCP servers in tier order:

```text
Context7   (authority 0.60)  ─┐
DeepWiki   (authority 0.50)  ─┼─► first hit registers a new doc_source
GitHub raw (authority 0.40)  ─┘
```

If multiple candidates score similarly, the search returns `outcome="asked_user"` with a candidate list. The user chooses; `disambiguate_discovery` registers the choice; the search re-runs and the discovery is logged.

Probe order, novel-library detection, and authority defaults live in [`mcp-servers/kb-mcp/discovery.py`](../mcp-servers/kb-mcp/discovery.py).

---

## The concept graph

Five edge types, all extracted from chapter and doc-section text:

| Type | Meaning | Count today |
|---|---|---|
| `CITES` | A's discussion references B | 409,808 |
| `IMPLEMENTS` | A is an implementation pattern for B | 83,031 |
| `REQUIRES` | A is a prerequisite for B | 58,105 |
| `CONTRASTS_WITH` | A and B are commonly compared / contrasted | 45,419 |
| `EXTENDS` | A is a refinement / specialization of B | 16,244 |

Plus the side `alignment_edge` table for cross-source signals (`CORROBORATES`, `CONTRADICTS`).

Concept extraction runs through `scripts/extract_batch.py prep` (writes prompts) → `scripts/batch_dispatch.py submit --task concepts` (Anthropic Batch API with Haiku 4.5 + prompt caching) → fetch → `scripts/extract_batch.py process` (resolver-driven idempotent ingest). Same shape for procedures (`scripts/extract_procedures.py`), doc-section extraction (`scripts/refresh_docs.py prep` + `batch_dispatch.py submit-doc-extraction`), and alignment (`scripts/refresh_docs.py align-prep` + `batch_dispatch.py submit-alignment` with Sonnet 4.6).

For full details on extraction, resolution, and alignment: [docs/concept-graph.md](concept-graph.md).

---

## The generator framework (Phase 7)

Every generator implements the same four protocols:

```text
                ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
inputs ────────►│ Decomposer   │───►│ Planner      │───►│ Validator    │───►│ Materializer │────► output
                └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                concept clusters    file layout +       resolve targets,    write files,
                + retrieval pool    sub-agent prompts   match procedures,   record provenance
                                                        confidence floor
```

The protocols are defined in [`mcp-servers/kb-mcp/generator.py`](../mcp-servers/kb-mcp/generator.py). Each generator subclasses or implements the protocols and is wired to a `/kb-*` slash command in [`.claude/commands/`](../.claude/commands/).

### Why one framework

A `/kb-cheatsheet` and a `/kb-bootstrap` look totally different to the user, but their internals are 80% identical:

- Both decompose a topic into concept clusters using `find_prerequisites` + concept-graph walks
- Both plan an output structure (cheatsheet has 1 file; bootstrap has 23)
- Both validate that every claim has a source and every step has a procedure
- Both materialize to `data/generated-packages/<name>_<timestamp>/` with a manifest

The framework owns this shared shape. Each generator only writes what makes it different.

### Shared persistence

| Table | Owner | Purpose |
|---|---|---|
| `generated_package` | Phase 7 framework | One row per generator run |
| `generated_unit` | Phase 7 framework | Concept clusters identified during decomposition |
| `generated_file` | Phase 7 framework | Files materialized to disk |
| `generated_source` | Phase 7 framework | Provenance: which chapter / doc_section / procedure backed each file |
| `skill_package`, `skill`, `skill_file`, `skill_source`, `skill_relation` | Skills Factory (frozen) | Phase 5 Skills Factory uses its own tables; predates the generic framework |

### The seventeen generators

See [docs/generators.md](generators.md) for the full catalog. By category:

- **Skills & curriculum**: Skills Factory, Concept Map, Learning Path, Curriculum
- **Reference & teaching**: Cheatsheet, Slide-Deck Outline, Tutorial, Content Brief, Pattern Catalog
- **Decisions & strategy**: ADR, Tech Assessment, Migration Guide, Currency Report
- **Voice & character**: Dialog, Author Panel
- **Bootstrap & refactor**: Project Bootstrap, Refactoring Playbook

Project Bootstrap is the user's #1 — see [docs/generators.md#project-bootstrap](generators.md#project-bootstrap) for the canonical CQRS+Kafka+HL7 walkthrough.

---

## Concurrency model

DuckDB takes a single-writer file lock that **excludes all other processes**, including read-only ones. So the rule is: writers and readers cannot coexist at the file level.

The MCP server's `db.py` enforces this with one knob — `read_only=True` is the default. Writers (`refresh_docs`, `index_books`, `extract_*`, `migrate_*`, `build_*`) must pass `read_only=False` explicitly, which makes the "I am about to mutate" intent visible at every write site. Multiple readers can coexist; one writer excludes everyone.

Practical consequences:

| Scenario | What happens |
|---|---|
| MCP server running, you start a writer script | Writer fails to acquire the lock; close the MCP session first (or kill the process) |
| Writer running, you query from another shell | Reader fails to acquire the lock; wait for the writer or open `read_only=True` after it finishes |
| MCP server's own `find_prerequisites` walk | Uses recursive SQL CTEs — no `CREATE PROPERTY GRAPH` write, so RO is enough |

`open_catalog()` in `db.py` is the single place that knows the full incantation: open with the right mode, `LOAD vss / fts / duckpgq`, set `hnsw_enable_experimental_persistence`, and (in RW mode only) re-execute the property-graph DDL since DuckPGQ doesn't persist graph definitions across reopens.

Reference: see the global `~/Developer/notes/duckdb-concurrent-access.md`.

---

## What's *not* in the system (deliberately)

- **No chunking.** Author structure is preserved.
- **No re-ranking model.** The five-factor ranker is interpretable. Adding a learned re-ranker would obscure why a result won; the project optimizes for "I can explain this rank."
- **No cloud database.** Single-file DuckDB. Embeddings, FTS, graph all local.
- **No live agent loops inside generators.** The generators emit deterministic v1 outputs (skeleton + sub-agent prompts). The user dispatches sub-agents; a v2 dispatch loop is on the roadmap.

---

## See also

- [docs/data-sources.md](data-sources.md) — what feeds the substrate
- [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) — how the substrate gets built
- [docs/concept-graph.md](concept-graph.md) — extraction, resolver, alignment
- [docs/generators.md](generators.md) — the seventeen generators
- [docs/customization.md](customization.md) — weight profiles, character profiles, adding generators
- [docs/operations.md](operations.md) — refresh, eval, diagnostics
- [docs/mypub-v2-architecture.md](mypub-v2-architecture.md) — canonical design spec (deeper)
