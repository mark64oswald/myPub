# Concept graph

The 312K-concept graph that sits across the substrate — extracted from chapter and doc-section text, deduplicated by the EntityResolver, augmented with procedures, and aligned across books and live docs.

[← back to top-level README](../README.md) · [Architecture ↗](architecture.md) · [Ingestion & indexing ↗](ingestion-and-indexing.md)

---

## What's in the graph

```text
                   ┌────────────────────────────────────────────────┐
                   │                  concept                       │
                   │ 312,396 rows: name, definition, concept_type,  │
                   │ synonyms (via concept_alias), embedding (side) │
                   └────────────────────────────────────────────────┘
                                     ▲
                                     │
   chapter ──┐                  ┌────┴────┐                   ┌──── doc_section
   (118K)    │                  │ relates │                   │    (1,909)
             │                  │   to    │                   │
             ▼                  │         │                   ▼
   ┌───────────────────┐        │         │         ┌─────────────────────┐
   │  concept_relation │◄───────┘         └────────►│   alignment_edge    │
   │  613K edges:      │                            │   1,320 edges:      │
   │   CITES (410K)    │                            │     CORROBORATES    │
   │   IMPLEMENTS (83K)│                            │       (1,296)       │
   │   REQUIRES (58K)  │                            │     CONTRADICTS     │
   │   CONTRASTS (45K) │                            │       (24)          │
   │   EXTENDS (16K)   │                            └─────────────────────┘
   └───────────────────┘                                          ▲
                                                                  │
                    ┌─────────────────────────────────────────────┴─┐
                    │                  procedure                    │
                    │  47,874 rows: precondition / steps /          │
                    │  postcondition / failure modes / concepts     │
                    │  175,106 procedure_concept links              │
                    └───────────────────────────────────────────────┘
```

Five entity-graph relations (`concept_relation`), one alignment relation (`alignment_edge`), and procedures that hang off concepts to give the graph a how-to layer.

---

## Concept rows and their counts

312,396 concept rows today, every one with a 384-dim embedding.

| Field | What it means |
|---|---|
| `concept_id` | BIGINT surrogate key |
| `name` | Canonical name (e.g., "Event Sourcing") |
| `concept_type` | One of: `Concept`, `Pattern`, `AntiPattern`, `Algorithm`, `Tool`, `Library`, `Term`, `Person`, `Org`, `Project`, … |
| `definition` | One-paragraph definition extracted by the sub-agent |
| `confidence` | 0..1 — how confident the extractor was |
| `embedding` | 384-dim vector (in `concept_embedding` side table) |

Aliases (synonyms) live in `concept_alias`. The resolver consults aliases on every lookup so "CDC" and "Change Data Capture" resolve to the same concept.

---

## Edge types

### Five concept-to-concept edges

| Edge type | Semantics | Today |
|---|---|---|
| `REQUIRES` | A is a prerequisite for B. Walked recursively for `find_prerequisites`. | 39,716 |
| `IMPLEMENTS` | A is an implementation of pattern B (e.g., "Outbox table" IMPLEMENTS "Reliable event publishing"). | 37,645 |
| `EXTENDS` | A is a refinement / specialization of B (e.g., "Idempotent producer" EXTENDS "Producer"). | 17,661 |
| `CITES` | A's discussion references B without prerequisite-ness or specialization. | 16,298 |
| `CONTRASTS_WITH` | A and B are commonly compared / contrasted (e.g., "REST" CONTRASTS_WITH "GraphQL"). | 16,170 |

The full edge schema:

```text
concept_relation
  from_concept_id   BIGINT
  to_concept_id     BIGINT
  relation_type     VARCHAR  (one of the five above)
  confidence        DOUBLE   (0..1)
  source_type       VARCHAR  ('chapter' | 'doc_section')
  source_id         BIGINT   (chapter_id or doc_section_id where the edge was extracted)
  created_at        TIMESTAMP
```

### Alignment edges (`alignment_edge`)

A separate table, because the *kind* of relation is different — it's not "X is a prerequisite for Y" but "this *book section* and this *doc section* are saying the same (or contradictory) thing about concept Z."

```text
alignment_edge
  from_doc_section_id  BIGINT
  to_chapter_id        BIGINT  (NULL for doc-doc alignments)
  to_doc_section_id    BIGINT  (NULL for doc-chapter alignments)
  concept_id           BIGINT  (which concept they're agreeing/disagreeing about)
  relation_type        VARCHAR ('CORROBORATES' | 'CONTRADICTS')
  confidence           DOUBLE
  explanation          VARCHAR
```

---

## Extraction pipeline

Concept extraction is the only stage that uses sub-agent dispatch rather than a single Python script.

### Phase A — `prep`

`scripts/extract_batch.py prep` writes one prompt file per chapter into a run directory. Each prompt contains:

1. The chapter content (truncated if extremely long; most chapters fit in a single sub-agent context)
2. The current concept catalog as context (so the sub-agent can re-use existing concept names rather than coining synonyms)
3. The resolver's contextual hints — recently-seen aliases, common variations
4. A schema-validated output template (the JSON shape the sub-agent must produce)

The extraction prompt asks the sub-agent for:

- A list of concepts present in the chapter (with name, type, definition)
- A list of relations between those concepts (with relation_type and a quoted snippet as evidence)
- A list of aliases (where the chapter uses an alternate name for an existing concept)

### Phase B — sub-agents process the prompts

Sub-agents run independently. Each writes a result JSON next to its prompt. The extraction stage doesn't require a centralized dispatcher — sub-agents pull work, do it, and the next phase reads what's there.

### Phase C — `process`

`scripts/extract_batch.py process` reads each result JSON, validates it against the schema, runs the EntityResolver on every concept name, and inserts:

- New `concept` rows (when the resolver determines this is a novel concept)
- New `concept_alias` rows (when the resolver finds an existing concept with a new alias)
- `concept_relation` rows (with `confidence` from the sub-agent)
- Items into `concept_resolution_queue` for borderline cases (near-duplicate names, ambiguous types)

The process phase is idempotent on the prompt-file granularity — re-running it picks up only result JSONs that haven't been ingested.

---

## EntityResolver

Resolution is a three-stage pipeline:

```text
incoming concept name ─► Stage 1: exact match (with type-aware tiebreak)
                          │
                          ├── hit → return existing concept_id
                          │
                          ▼ miss
                         Stage 2: alias lookup (concept_alias table)
                          │
                          ├── hit → return aliased concept_id
                          │
                          ▼ miss
                         Stage 3: similarity search
                          │ (embedding + edit distance combined)
                          │
                          ├── high confidence → enqueue for review
                          │   (concept_resolution_queue)
                          │
                          └── low confidence → create new concept
```

### The duplicate-concept-name bug (resolved 2026-05-06, commit `ecc74f4`)

The original Stage 1 did `LIMIT 1` with no `ORDER BY`. The catalog carries ~26K concept-name groups duplicated across `concept_type` variants — e.g., "Event Sourcing" exists as both `Concept` (cid=10998, 0 REQUIRES) and `Pattern` (cid=16520, 41 REQUIRES). The resolver could pick the empty twin and break downstream lookups.

Fix: Stage 1 now `ORDER BY (SELECT COUNT(*) FROM concept_relation WHERE from_concept_id = c.concept_id OR to_concept_id = c.concept_id) DESC` — the richest concept wins. The 26K remaining duplicate-name groups all have edges (strict orphans were removed by `scripts/dedupe_concepts.py` — 8,326 to date) and the resolver routes correctly to the richest twin, so they're harmless. Tracked as low-priority hygiene in [docs/operations.md → Deferred work](operations.md#deferred-work).

### Strict-orphan duplicate cleanup (commit `1f1d5d5`)

825 concepts were strict orphans — duplicates of an existing concept with no inbound or outbound edges. Those were collapsed into their richer twins and the orphan rows removed.

### Reviewing the queue

`/kb-review-concepts` is the slash command that drives interactive review:

- Shows borderline pairs the resolver couldn't auto-decide
- Lets the user pick: merge / alias / keep-separate / rename
- HNSW-index-present paths are special-cased (the script drops + recreates the index for safe edits, then rebuilds)

---

## Procedures

A procedure is a named, structured how-to. It's not a free-text passage — it's:

```text
procedure
  name              "Configure Kafka exactly-once producer"
  preconditions     "Kafka 3.0+; broker has transaction coordinator enabled"
  steps             "1. set enable.idempotence=true
                     2. set transactional.id=<unique>
                     3. wrap sends in beginTransaction()/commitTransaction()
                     4. ..."
  postconditions    "Producer publishes with exactly-once semantics"
  failure_modes     "If transactional.id collides with another producer,
                     fenced producer error..."
  source_type       'chapter'
  source_id         <chapter_id of the chapter where this procedure was extracted>
```

Procedures are linked to concepts via `procedure_concept` (many-to-many). A procedure can:

- *Implement* a pattern (`procedure.implements_pattern → concept_id` of the pattern)
- *Be linked to* concepts that appear in its precondition / steps / postcondition

### Why procedures matter

Most generators can run on chapter text alone. **Project Bootstrap can't.** A bootstrap that doesn't compose specific configuration steps produces scaffolds that don't run. The Tutorial generator is similar — its output is a sequence of *steps*, not a paragraph. Procedures are the input shape that lets these generators produce something deterministic and runnable.

### Procedure extraction

```bash
.venv/bin/python3 scripts/extract_procedures.py prep --limit 100
.venv/bin/python3 scripts/extract_procedures.py process
```

Today's catalog: **47,874 procedures** with **175,106 procedure-concept links**. 46,904 are chapter-sourced; 970 are doc-section-sourced (extracted as part of the doc-source expansion). The Project Bootstrap generator handles missing procedures gracefully by warning when a domain has none.

---

## Alignment edges

The alignment pass takes a doc source (e.g., Apache Kafka) and emits edges between book chapters and doc sections that discuss the same concept.

### Two-phase shape (same as extraction)

```bash
# Per source:
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py prep --source kafka
# (entity-extraction sub-agents process)
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py process --source kafka
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py align-prep --source kafka
# (alignment sub-agents process; comparison-style prompt)
.venv/bin/python3 scripts/migrate_phase4_4b_alignment.py align-process --source kafka
```

### Today's alignment results

**1,296 CORROBORATES + 24 CONTRADICTS edges** across all 54 sources (avg confidence 0.72 / 0.16 respectively). Below is the high-density subset; the full per-source list is in [docs/data-sources.md → Registered sources](data-sources.md#registered-sources):

| Source | Alignment edges |
|---|---|
| FastMCP (DeepWiki) | 221 |
| DuckPGQ (DeepWiki) | 151 |
| MLflow | 36 |
| FastAPI | 32 |
| React | 31 |
| scikit-learn | 31 |
| Apache Kafka | 29 |
| OpenAPI | 28 |
| spaCy | 28 |
| LangChain | 28 |
| SQLite | 28 |
| (43 more sources, 2–25 edges each) | |

CORROBORATES dominates because narrow vendor docs tend to agree with or be unrelated to book content. CONTRADICTS edges are rarer but valuable when they appear — examples in the catalog include FastMCP allowing breaking changes in minor versions vs. SemVer textbooks, React Compiler being installable now vs. "experimental" in older books, and DuckPGQ's logical-graph-over-SQL approach vs. native graph databases' index-free adjacency. Avg CONTRADICTS confidence is 0.16 — most are degenerate; a contradiction-tuned alignment prompt + multi-sample voting is the path to making the Migration Guide and Currency Report generators robust.

### Why CORROBORATES boosts ranking

The ranker's `corroboration` factor reads `alignment_edge` directly. For a query against a domain with extracted alignment edges, CORROBORATES edges between book chapters and doc sections add a confidence boost. For a query against a domain without alignment, the factor returns 0 and `doc_alignment` flips to 0.50 (neutral) instead of penalizing.

### What CORROBORATES vs. CONTRADICTS *means* for output

```text
CORROBORATES
  Book 2017: "Configure exactly-once with these manual settings: …"
  Doc 2026:  "enable.idempotence is true by default since Kafka 3.0; just
              set transactional.id"
  → Both are saying "exactly-once is configurable; doc shows the modern way"

CONTRADICTS  (a hypothetical example for illustration)
  Book 2018: "Auto-commit is required for at-least-once consumer semantics"
  Doc 2026:  "Auto-commit is incompatible with exactly-once and is
              deprecated for new consumers"
  → They actively disagree on a configuration recommendation
```

The Currency Report generator surfaces CONTRADICTS edges directly; the Migration Guide generator uses them to identify what changed between versions.

---

## Querying the graph

### Recursive walks (e.g., prerequisites)

The MCP server's `find_prerequisites` tool walks `REQUIRES` edges recursively. It uses a CTE rather than DuckPGQ's `->{m,n}` quantifier because the quantifier doesn't bind the edge variable in this DuckPGQ build, so edge-property filters fail to bind.

```sql
WITH RECURSIVE prereqs(concept_id, depth, path) AS (
  SELECT to_concept_id, 1, [from_concept_id, to_concept_id]
  FROM concept_relation
  WHERE relation_type = 'REQUIRES' AND from_concept_id = :seed
  UNION ALL
  SELECT cr.to_concept_id, p.depth + 1, p.path || cr.to_concept_id
  FROM prereqs p
  JOIN concept_relation cr ON cr.from_concept_id = p.concept_id
  WHERE cr.relation_type = 'REQUIRES'
    AND p.depth < :max_depth
    AND NOT list_contains(p.path, cr.to_concept_id)
)
SELECT DISTINCT concept_id, MIN(depth) AS shortest_depth
FROM prereqs
GROUP BY concept_id
ORDER BY shortest_depth;
```

Cycle protection via `path` array. `MIN(depth)` returns each prerequisite at its shortest depth.

### Concept-by-concept author roll-ups

`compare_concept_across_authors` resolves the concept name through the EntityResolver, then groups its discussing chapters by author:

```sql
WITH discussing AS (
  SELECT chapter.chapter_id, ranking_score
  FROM /* ranker pipeline against the concept's name */
)
SELECT a.name AS author, b.title AS book, ch.title AS chapter, d.ranking_score
FROM discussing d
JOIN chapter ch USING (chapter_id)
JOIN book b USING (book_id)
JOIN book_author ba USING (book_id)
JOIN author a USING (author_id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY a.author_id ORDER BY d.ranking_score DESC) <= :limit_per_author
ORDER BY a.name, d.ranking_score DESC;
```

The `limit_per_author` cap (default 2) keeps the response from being dominated by a single voluminous author.

---

## Hygiene and known issues

| Issue | State | Reference |
|---|---|---|
| Duplicate concept names across `concept_type` variants | ~26K groups remaining; all have edges and the resolver routes correctly to the richest twin (resolver-fix in `ecc74f4`). 8,326 strict orphans removed by `dedupe_concepts.py` | low-priority hygiene; [docs/operations.md](operations.md#deferred-work) |
| Author placeholder ("AUTHOR NAMES HERE") + missing-author books | 19 of 20 historically-unauthored books recovered via OpenLibrary + Google Books ISBN lookup. 1 residual case (Platform Enterprise — source has no public author info anywhere) | resolved (modulo 1 unknown) |
| Author smush (multi-author packed into one `<dc:creator>`) | 161 rows split via post-ingest splitter with credential-suffix re-merging | resolved |
| Procedure extraction on doc sections | 970 doc-section procedures + 175K total procedure-concept links | resolved |
| Alignment for MLflow / DuckPGQ / FastMCP | All three recovered; 408 alignment edges across the trio | resolved |
| `concept_doc_link` table | Currently 0 rows; reserved for direct concept→doc-section linkage when needed beyond what `concept_relation` already provides | future |
| CONTRADICTS quality | 24 edges, avg confidence 0.16 — most degenerate. Contradiction-tuned alignment prompts + multi-sample voting needed for Migration Guide / Currency Report robustness | [docs/operations.md → Deferred work](operations.md#deferred-work) |

---

## See also

- [docs/architecture.md](architecture.md) — how the graph is queried during ranking
- [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) — the full pipeline
- [docs/data-sources.md](data-sources.md) — what sources contribute to the graph
- [docs/generators.md](generators.md) — how generators consume the graph
- [docs/operations.md](operations.md) — deferred work and corpus gaps
