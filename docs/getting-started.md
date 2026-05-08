# Hello, myPub!

A guided walkthrough — zero to first generated artifact in about fifteen minutes.

By the end of this you'll have:

1. A DuckDB catalog populated from a small ePub library
2. The `mypub-kb` MCP server running under Claude Code
3. A first natural-language query routed through hybrid retrieval
4. A first slash-command-driven generator output saved to disk

[← back to top-level README](../README.md)

---

## What you'll need

| | Required | Notes |
|---|---|---|
| Python | 3.11+ | Anaconda fine; project uses a `.venv` |
| Disk | ~2 GB | Catalog + embeddings; ePubs live elsewhere |
| ePubs | At least 5 | Drop into `~/Documents/eBooks/` (or your own path) |
| Claude Code | Latest | The MCP server is launched as a stdio server |
| (Optional) Context7 / DeepWiki MCP | Already configured in your Claude Code setup | If absent, doc-ranking factors fall back to neutral |

> **Tip.** myPub is designed for personal libraries. Five books is enough to feel everything; the production catalog at the time of writing carries 572 books.

---

## 1. Install

```bash
git clone <your-fork-or-this-repo> myPub
cd myPub
git checkout v2-substrate

python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install -e ".[dev]"
```

Verify the DuckDB version pin (the project requires exactly 1.5.0 because DuckPGQ is broken on 1.5.1 and missing on 1.5.2):

```bash
.venv/bin/python3 -c "import duckdb; print(duckdb.__version__)"
# 1.5.0
```

---

## 2. Build the substrate

The build is broken into idempotent, resumable scripts. A full cold-start on the production library takes ~70 minutes (mostly embeddings on Apple Silicon MPS); a 10-book starter library finishes in 2–3 minutes.

```bash
# 1. Create the v2 schema (writes a backup first if a catalog exists).
.venv/bin/python3 scripts/migrate_v2_schema.py

# 2. Install DuckDB extensions (FTS, VSS, DuckPGQ).
.venv/bin/python3 scripts/install_extensions.py

# 3. Index the ePub library — drop your books into ~/Documents/eBooks/ first.
.venv/bin/python3 scripts/index_books.py --source ~/Documents/eBooks --limit 10

# 4. Generate 384-dim chapter embeddings.
.venv/bin/python3 scripts/generate_embeddings.py

# 5. Build FTS (BM25) and VSS (HNSW, cosine) indexes.
.venv/bin/python3 scripts/build_fts_index.py
.venv/bin/python3 scripts/build_vss_index.py

# 6. Declare the DuckPGQ property graph.
.venv/bin/python3 scripts/build_property_graph.py
```

The catalog now lives at `data/catalog.ddb` (gitignored). To verify:

```bash
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb', read_only=True)
print('books:', c.execute('SELECT COUNT(*) FROM book').fetchone()[0])
print('chapters:', c.execute('SELECT COUNT(*) FROM chapter').fetchone()[0])
c.close()
"
```

What you've built so far covers the *retrieval* substrate. The *concept graph* (entity extraction, alignment, procedures) is layered on top in [section 6](#6-extract-concepts-optional-on-first-pass) — you don't need it to make the first query work.

---

## 3. Wire up the MCP server

The project ships a working `.mcp.json` at the repo root. Confirm it points at your venv's Python:

```json
{
  "mcpServers": {
    "mypub-kb": {
      "command": ".venv/bin/python3",
      "args": ["mcp-servers/kb-mcp/server.py"]
    }
  }
}
```

Open the project in Claude Code:

```bash
claude .
```

When prompted, allow the `mypub-kb` server to start. The first launch loads the sentence-transformers model (~90 MB, cached afterward).

---

## 4. First query — natural language

In Claude Code:

> `search the kb for change data capture`

Claude will route this to `search_chapters` automatically. Expected output (your titles will vary based on which books you indexed):

```text
Primary
  "Capturing All Database Changes" — Designing Data-Intensive Applications,
  Martin Kleppmann · O'Reilly · 2017 · ch. 11

Corroborations
  "Kafka Connect" — Apache Kafka live doc · Context7 snapshot · 2026-04-22
  "Event Sourcing" — Implementing Domain-Driven Design, Vaughn Vernon · ch. 8

Conflicts
  (none surfaced — book and live-doc descriptions agree)

Discovery
  No new doc-source candidates required.
```

Behind the scenes the ranker fused FTS hits (BM25 over the Porter-stemmed `chapter.content`), VSS hits (cosine over 384-dim embeddings), and graph-proximity hits (concepts linked via the concept graph), then re-scored the top pool through the five factors.

---

## 5. First comparison — multi-author

> `compare how my authors discuss CQRS`

This routes to `compare_concept_across_authors`. Expected:

```text
Vaughn Vernon — Implementing Domain-Driven Design (Addison-Wesley)
  ch. 4: "Architecture" — CQRS as a tactical pattern
  ch. 8: "Domain Events" — pairing CQRS with event sourcing

Martin Fowler — Patterns of Enterprise Application Architecture
  Reference: brief mention in Service Layer chapter, deeper treatment
  on bliki cross-reference

Greg Young — Versioning in an Event-Sourced System
  ch. 2: "Read Models" — projection patterns
  ch. 5: "Polyglot Persistence" — when CQRS earns its complexity

… (each author at most 2 best-matching chapters)
```

If a concept name is ambiguous, the resolver falls back to the review queue (`/kb-review-concepts`).

---

## 6. Extract concepts (optional on first pass)

Concept extraction is what populates the 312K-concept graph. The seed dataset works without it — graph factors degrade gracefully when the graph is sparse — but the *interesting* generators (Concept Map, Learning Path, Migration Guide, Project Bootstrap) need a populated graph.

```bash
# Prep extraction prompts for un-extracted chapters.
.venv/bin/python3 scripts/extract_batch.py prep --limit 50

# Inside Claude Code, dispatch the sub-agent batch:
#   /kb-review-concepts        # interactive review
# or extraction sub-agents process the prompts directly.

# Process the JSON outputs back into the catalog.
.venv/bin/python3 scripts/extract_batch.py process
```

For procedures (precondition / steps / postcondition / failure modes), see [docs/concept-graph.md](concept-graph.md#procedures).

For book ↔ live-doc alignment edges (the "are book and current docs saying the same thing?" signal), see [docs/concept-graph.md](concept-graph.md#alignment-edges).

---

## 7. First generator — Concept Map

In Claude Code:

> `/kb-concept-map event sourcing`

The Concept Map generator walks the concept graph N hops out from "Event Sourcing" along REQUIRES, EXTENDS, IMPLEMENTS, and CONTRASTS_WITH edges, then materializes a markdown map under `data/generated-packages/`.

Expected file layout:

```text
data/generated-packages/concept-map_event-sourcing_<timestamp>/
├── manifest.json                  # what was generated, from where
├── concept-map.md                 # the rendered map
└── sources.md                     # citations per node
```

Open `concept-map.md` — you'll see the central concept, its prerequisites (REQUIRES), things that extend it (EXTENDS), and things it's commonly contrasted with (CONTRASTS_WITH), each with chapter citations.

---

## 8. The headline generator — Project Bootstrap

This is the user's **#1** generator and the canonical substrate-validation case. Try:

> `/kb-bootstrap CQRS event-sourced order service with Kafka and HL7`

Expected pipeline:

```text
[decompose] 12 concept clusters identified
            (CQRS, Event Sourcing, Kafka producers/consumers, HL7 v2,
             docker-compose, pytest fixtures, …)
[plan]      23 files projected
[validate]  unresolved targets: 0
            unmatched procedures: 0
            HL7 procedure gap WARNING: 0 procedures in catalog for HL7
            (acquire HL7 books or accept doc-only HL7 layer)
[materialize]
            data/generated-packages/cqrs-kafka-hl7-bootstrap_<timestamp>/
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
```

The v1 generator emits *placeholder files plus per-file sub-agent prompts* — the user dispatches Task agents to fill in the implementation from the prompts. v2 (planned) wraps the dispatch loop and adds runtime validation (`pip install + pytest + docker-compose up`).

See [docs/generators.md](generators.md#project-bootstrap) for the full Project Bootstrap walkthrough.

---

## 9. Run the tests

```bash
./scripts/test.sh
```

Expected: a clean run across 37 test modules (unit + a handful of live API tests). Live tests embed real queries against the catalog; the first run downloads the embedding model.

Single-file:

```bash
./scripts/test.sh tests/test_phase1_integration.py
```

Filtered:

```bash
./scripts/test.sh -k resolve
```

---

## What's next

| If you want to… | Read |
|---|---|
| Understand the substrate end-to-end | [docs/architecture.md](architecture.md) |
| Add new books or refresh live docs | [docs/data-sources.md](data-sources.md) |
| Re-ingest from scratch / debug indexing | [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) |
| Run alignment between books and live docs | [docs/concept-graph.md](concept-graph.md#alignment-edges) |
| Try every generator | [docs/generators.md](generators.md) |
| Add your own generator | [docs/customization.md](customization.md#adding-a-generator) |
| Tune ranking for a specific use case | [docs/customization.md](customization.md#weight-profiles) |
| Run retrieval-quality eval | [docs/operations.md](operations.md#retrieval-eval) |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `_duckdb.IOException: GLIBCXX_…` | Wrong DuckDB version | `pip install duckdb==1.5.0` |
| `DuckPGQ extension not found` | Pre-1.5.0 catalog | Re-run `scripts/install_extensions.py` |
| HNSW build hangs | Missing pragma | Set `hnsw_enable_experimental_persistence = true` (already in `build_vss_index.py`) |
| `FK constraint violation on UPDATE chapter SET embedding=…` | DuckDB 1.5.0 FK bug | Embeddings live in side tables (`chapter_embedding`); never UPDATE a `FLOAT[N]` column on a row with inbound FKs |
| MCP server fails to start | Sentence-transformers cold-start timeout | First boot downloads ~90 MB; rerun |
| First query returns "0 results" | Graph empty / FTS not built | Re-run `build_fts_index.py` and the extraction step |

For deeper diagnostic trails, see [docs/operations.md](operations.md#diagnostics).
