# Ingestion and indexing

The end-to-end pipeline that turns ePub files and live-doc snapshots into a queryable substrate: structural parse, full-text search, vector embeddings, property graph, and the resumable scripts that orchestrate it all.

[← back to top-level README](../README.md) · [Architecture ↗](architecture.md) · [Data sources ↗](data-sources.md) · [Concept graph ↗](concept-graph.md)

---

## The pipeline at a glance

```text
                                                ┌─────────────────────────────┐
ePub file ─► sectionizer ─► chapter rows ───────► chapter_embedding (VSS)     │
   │                                            │                             │
   │                            book / book_author / chapter (structural)     │
   │                                            │                             │
   │                                            └─► FTS index (BM25, Porter)  │
   │                                                                          │
   ▼                                                                          │
hash + dedup                                                                  │
                                                                              │
live doc URL ─► MCP fetch ─► doc_snapshot ─► doc_section ─► doc_section_embedding (VSS)
                                            │                                 │
                                            └─► FTS index                     │
                                                                              ▼
                                                  ┌────────────────────────────────┐
                                                  │  DuckPGQ property graph        │
                                                  │  (vertex + edge tables;        │
                                                  │   declared on every connect)   │
                                                  └────────────────────────────────┘
                                                                              ▲
chapter content ─► extraction sub-agent prompts ─► concept rows + ────────────┘
                                                   concept_relation edges +
                                                   procedures
                                                                              ▲
chapter ↔ doc_section ─► alignment sub-agent prompts ─► alignment_edge rows ──┘
```

Every stage is implemented as a separate, idempotent script under `scripts/`. The pipeline is resumable — re-running any stage skips already-processed inputs by content hash.

---

## Stage 1 — structural ingestion

### Books (`scripts/index_books.py`)

```bash
.venv/bin/python3 scripts/index_books.py --source ~/Documents/eBooks
```

What it does:

1. Walks `~/Documents/eBooks/` for `.epub` files
2. For each file:
   - Computes a content hash; skips if the hash already matches a row in `book`
   - Reads OPF / NCX / TOC to extract metadata (title, authors, publisher, publication date, ISBN, language)
   - Falls back to a small set of heuristics (filename parsing, embedded HTML metadata) when ePub metadata is missing
   - Sectionizes the spine into a chapter tree
3. Inserts rows into `book`, `book_author`, `chapter` (with `parent_chapter_id` for nested headings)
4. Records `chapter.token_count` for downstream context-budget calculations

Idempotency is by content hash. Re-running with the same library is a no-op except for new or changed files. A test that builds an ePub programmatically and exercises the full path lives at [`tests/test_index_books.py`](../tests/test_index_books.py).

### Live docs (`scripts/refresh_docs.py`)

```bash
.venv/bin/python3 scripts/refresh_docs.py refresh --all
```

What it does:

1. Lists rows in `doc_source` whose `last_refresh_at` is older than `refresh_ttl_days`, or whose `pinned = false` and content has changed since last fetch
2. For each due source:
   - Calls the appropriate MCP server (Context7, DeepWiki, or GitHub raw)
   - Computes `content_hash`; if unchanged, only updates `last_refresh_at`; if changed, writes a new `doc_snapshot` row
   - Sectionizes the markdown into `doc_section` rows under that snapshot

The `--no-extract` flag skips the concept-extraction stage on the new sections (useful when only re-snapshotting, not running alignment).

A small diagnostic command:

```bash
.venv/bin/python3 scripts/refresh_docs.py status
```

prints which sources are stale, which are pinned, and which were most recently refreshed.

---

## Stage 2 — embeddings

### Model and dimensions

| | |
|---|---|
| Model | `sentence-transformers/all-MiniLM-L6-v2` |
| Dimensions | 384 |
| Device | Apple MPS (Apple Silicon) / CUDA / CPU fallback |
| Cold-start | ~90 MB downloaded into HuggingFace cache on first run |

Why this model: small enough to load locally, fast enough on MPS to embed 113K chapters in under an hour, and the 384-dim output keeps the VSS index file under a few hundred megabytes.

### Generating embeddings (`scripts/generate_embeddings.py`)

```bash
.venv/bin/python3 scripts/generate_embeddings.py
```

Targets every chapter where `chapter.token_count > 0` and no row exists in `chapter_embedding` for that chapter. Idempotent — re-running picks up only new chapters.

The same script supports doc sections via flags; under the hood, the production catalog has separate calls for chapters, doc sections, doc snapshots, and concepts.

### Why embeddings live in side tables

DuckDB 1.5.0 raises a spurious FK violation on:

```sql
UPDATE chapter SET embedding = ? WHERE chapter_id = ?
```

…whenever the row has any inbound foreign-key reference. So instead, every embedded entity has a sibling table:

```text
chapter           ←  chapter_embedding         (chapter_id, vector FLOAT[384])
concept           ←  concept_embedding         (concept_id, vector FLOAT[384])
doc_section       ←  doc_section_embedding     (doc_section_id, vector FLOAT[384])
doc_snapshot      ←  doc_snapshot_embedding    (snapshot_id, vector FLOAT[384])
```

INSERT-on-side-table is unaffected. The pinned regression test at [`tests/test_duckdb_fk_bugs.py`](../tests/test_duckdb_fk_bugs.py) exercises all three known FK bugs we work around.

---

## Stage 3 — FTS (BM25)

`scripts/build_fts_index.py` builds a Porter-stemmed full-text index over `chapter.content` and `doc_section.content`.

```bash
.venv/bin/python3 scripts/build_fts_index.py
```

Implementation:

```sql
PRAGMA create_fts_index(
  'chapter', 'chapter_id', 'content',
  stemmer = 'porter',
  ignore = '(\\.|[^a-z\\s])+',
  strip_accents = 1,
  lower = 1
);
```

The same is run against `doc_section`. FTS scoring uses BM25 directly; the ranker reads BM25 scores and folds them through reciprocal rank fusion alongside VSS and graph scores.

---

## Stage 4 — VSS (HNSW, cosine)

`scripts/build_vss_index.py` builds an HNSW index per embedding side table.

```bash
.venv/bin/python3 scripts/build_vss_index.py
```

Implementation:

```sql
SET hnsw_enable_experimental_persistence = true;

CREATE INDEX IF NOT EXISTS chapter_embedding_hnsw
ON chapter_embedding USING HNSW (vector)
WITH (metric = 'cosine');
```

The pragma must be set on every connection that creates or queries an HNSW index against a file-backed catalog (DuckDB documents this as experimental for 1.5.x).

Side-table indexes built by `build_vss_index.py`:

- `chapter_embedding_hnsw`
- `concept_embedding_hnsw`

`doc_section_embedding` is populated by `refresh_docs.py` and indexed via the same VSS pragma there. `doc_snapshot_embedding` is populated for whole-doc relevance but isn't HNSW-indexed today (the section-level embeddings are the practical retrieval surface).

VSS queries return `vector <=> :query_vector` cosine distance; the ranker subtracts from 1 to convert to a similarity score before fusion.

---

## Stage 5 — DuckPGQ property graph

`scripts/build_property_graph.py` declares the `mypub` property graph against the existing `book`, `chapter`, `concept`, etc. tables.

```bash
.venv/bin/python3 scripts/build_property_graph.py
```

Vertex tables (entities):

```text
author                ← author_id
book                  ← book_id
chapter               ← chapter_id
concept               ← concept_id
doc_section           ← doc_section_id
```

Edge tables (relations) — see [`schemas/property_graph.sql`](../schemas/property_graph.sql):

```text
wrote                 author        →  book           (book_author edge table)
book_contains         book          →  chapter        (chapter.book_id)
snapshot_contains     doc_snapshot  →  doc_section    (doc_section.snapshot_id)
package_contains      skill_package →  skill          (skill.package_id)
concept_relates_to    concept       →  concept        (concept_relation rows)
skill_relates_to      skill         →  skill          (skill_relation rows)
```

Phase 2+ stubs commented in the SQL but not yet wired (because the backing tables either weren't populated when the graph was declared, or because read traversals lean on SQL CTEs instead of DuckPGQ): `chapter_concept` (chapter discusses concept), `chapter_procedure` (chapter explains procedure), `doc_cross_ref` (doc section corroborates / contradicts chapter or another section), `skill_source` (skill derived_from chapter / procedure / doc_section).

### Constraints and gotchas

- **Edge labels are case-sensitive and must be globally unique.** Reserved tokens like `Edge` or `Book` collide with the parser. Labels in this catalog are lowercase.
- **No `--` comments before `DROP/CREATE PROPERTY GRAPH`.** The DuckPGQ parser doesn't handle them — the build script uses `/* */` block comments.
- **`->{m,n}` quantifiers don't bind the edge variable.** So edge-property filters (`relation_type = 'REQUIRES'`) fail to bind in DuckPGQ in this version. The MCP server uses recursive CTEs in plain SQL for prerequisite walks; the property graph is still declared (it's part of the catalog contract) but read traversals lean on SQL.
- **The graph must be re-declared on every new connection.** It's not persisted to disk. The MCP server runs `build_property_graph.py`'s SQL on every connect.

---

## Stage 6 — concept extraction

Concept extraction populates the 85K-concept graph. It's the only stage that uses sub-agent dispatch rather than a single Python script.

### Two-phase pattern: prep + process

```bash
# Phase A — prep: write one prompt file per chapter
.venv/bin/python3 scripts/extract_batch.py prep --limit 100

# Phase B — sub-agents process the prompts (manual or via Claude Code dispatch)

# Phase C — process: ingest the JSON results back into the catalog
.venv/bin/python3 scripts/extract_batch.py process
```

`prep` writes prompts under a run directory (gitignored) — one per chapter — including the chapter content, the existing concept catalog, and the resolver's contextual hints. `process` reads each result JSON, parses it, runs the EntityResolver to dedupe against existing concepts, and inserts new `concept` and `concept_relation` rows.

The full extraction lifecycle is in [docs/concept-graph.md](concept-graph.md#extraction-pipeline). The same prep/process pattern is used for procedures (`scripts/extract_procedures.py`) and book-doc alignment (`scripts/migrate_phase4_4b_alignment.py`).

---

## Stage 7 — procedure extraction

`scripts/extract_procedures.py` extracts named procedures from chapters: precondition, steps, postcondition, failure modes.

```bash
.venv/bin/python3 scripts/extract_procedures.py prep --limit 100
.venv/bin/python3 scripts/extract_procedures.py process
```

Today's catalog: **4,341 procedures**, all chapter-sourced. Procedures from doc sections are not yet extracted — known debt in [docs/operations.md](operations.md#deferred-work).

The procedure schema:

```text
procedure
  name              VARCHAR        — "Configure Kafka exactly-once producer"
  preconditions     VARCHAR        — what must be true before
  steps             VARCHAR        — ordered list (text or JSON-encoded)
  postconditions    VARCHAR        — what's true after
  failure_modes     VARCHAR        — what can go wrong
  source_type       VARCHAR        — 'chapter' | 'doc_section'
  source_id         BIGINT         — chapter_id or doc_section_id
  implements_pattern BIGINT        — optional concept_id of the pattern this implements
```

Procedures are the backbone of the Tutorial generator and Project Bootstrap — see [docs/generators.md](generators.md#tutorial).

---

## Stage 8 — alignment extraction

The alignment pass takes a doc source (e.g., Apache Kafka), identifies sections that overlap with book chapters, and emits `alignment_edge` rows tagged `CORROBORATES` or `CONTRADICTS`.

```bash
# Per source, in sequence:
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py prep --source kafka
# (sub-agents process)
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py process --source kafka
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py align-prep --source kafka
# (alignment sub-agents process)
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py align-process --source kafka
```

Today's results across the 7 aligned sources: **120 CORROBORATES edges, 0 CONTRADICTS**. CONTRADICTS is empty because narrow vendor docs tend to corroborate or be unrelated to book content, not contradict it. The Migration Guide and Currency Report generators are designed to surface contradictions when they appear.

The 5 remaining unaligned sources (MLflow, plus the larger DeepWiki sources DuckPGQ and FastMCP) are tracked in [docs/operations.md](operations.md#deferred-work).

---

## Refresh and discovery

### Doc refresh policy

```text
For each row in doc_source:
  If pinned == true:                        skip
  If last_refresh_at + ttl_days > now:      skip
  Else:
    fetch via MCP server
    compute content_hash
    if hash matches latest snapshot:        update last_refresh_at; done
    else:                                   insert new doc_snapshot;
                                            sectionize; embed; index
```

### Auto-discovery

When a search query mentions a library that isn't in any source, the search returns `outcome="needs_discovery"` (or `asked_user` if multiple candidates score similarly). The probe order:

1. **Context7** (authority 0.60) — primary for vendor docs and well-documented OSS
2. **DeepWiki** (authority 0.50) — AI-generated docs for any public GitHub repo
3. **GitHub raw** (authority 0.40) — last-resort fetch of README and `docs/`

`disambiguate_discovery` is the MCP tool that registers a user choice. Logged to `discovery_log` for later review.

---

## Idempotency and resumability

Every script in the pipeline can be safely re-run. The mechanics:

| Stage | Dedup mechanism |
|---|---|
| Books | `book.content_hash` matches → skip |
| Chapters | `chapter.content_hash` per (book, parent, ordinal) → skip |
| Embeddings | `chapter_embedding` row already exists → skip |
| FTS / VSS | Indexes are rebuilt; idempotent at the SQL level |
| Property graph | `DROP PROPERTY GRAPH IF EXISTS mypub; CREATE PROPERTY GRAPH …` |
| Concept extraction | Prompt file already has a result JSON → skip in `process` phase |
| Procedure extraction | Same — prompt-file presence drives skip |
| Alignment | Same prep/process semantics |

If you delete `data/catalog.ddb` entirely, run the full sequence in order. Embeddings will need to regenerate (~55 minutes on Apple Silicon for 113K chapters); FTS and VSS rebuild in seconds; the concept graph rebuild requires the run-artifact JSONs (preserved locally but gitignored) — see [docs/operations.md](operations.md#disaster-recovery).

---

## Validation and health checks

After a full pipeline run, sanity checks worth running:

```bash
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb', read_only=True)
print('books:               ', c.execute('SELECT COUNT(*) FROM book').fetchone()[0])
print('chapters:            ', c.execute('SELECT COUNT(*) FROM chapter').fetchone()[0])
print('chapters with embed: ', c.execute('SELECT COUNT(*) FROM chapter_embedding').fetchone()[0])
print('concepts:            ', c.execute('SELECT COUNT(*) FROM concept').fetchone()[0])
print('graph edges:         ', c.execute('SELECT COUNT(*) FROM concept_relation').fetchone()[0])
print('procedures:          ', c.execute('SELECT COUNT(*) FROM procedure').fetchone()[0])
print('doc sections:        ', c.execute('SELECT COUNT(*) FROM doc_section').fetchone()[0])
print('alignment edges:     ', c.execute('SELECT COUNT(*) FROM alignment_edge').fetchone()[0])
c.close()
"
```

The retrieval-quality eval (`tests/eval/retrieval_eval.py`) is the deeper check — see [docs/operations.md](operations.md#retrieval-eval).

---

## See also

- [docs/architecture.md](architecture.md) — how the indexed substrate is queried
- [docs/data-sources.md](data-sources.md) — what each input source provides
- [docs/concept-graph.md](concept-graph.md) — extraction pipeline in depth
- [docs/operations.md](operations.md) — disaster recovery, refresh schedules, eval
