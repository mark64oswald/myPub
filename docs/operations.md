# Operations

Day-to-day running of myPub: refresh, eval, deferred work, disaster recovery.

[← back to top-level README](../README.md) · [Ingestion & indexing ↗](ingestion-and-indexing.md) · [Concept graph ↗](concept-graph.md)

---

## Refresh

### Live-doc refresh

```bash
# Refresh anything past its TTL (default 30 days)
.venv/bin/python3 scripts/refresh_docs.py refresh --all

# Refresh one source
.venv/bin/python3 scripts/refresh_docs.py refresh --source "Apache Kafka"

# Skip extraction (snapshot only — useful when only re-snapshotting)
.venv/bin/python3 scripts/refresh_docs.py refresh --all --no-extract

# Status — what's stale, pinned, recently refreshed
.venv/bin/python3 scripts/refresh_docs.py status
```

A doc is re-snapshotted when:

- Its TTL elapses, *and*
- Its content hash differs from the last snapshot

So a TTL-elapsed-but-unchanged doc only updates `last_refresh_at` — no new `doc_snapshot` row, no re-embedding, no re-indexing.

### Pinning a source

To freeze a source against automatic refresh (useful when shipping):

```bash
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb')
c.execute(\"UPDATE doc_source SET pinned = true WHERE name = 'Apache Kafka'\")
c.close()
"
```

A pinned source is exempt from `refresh --all`. Unpin by setting `pinned = false`.

### Scheduled refresh

The `launchd/` directory has macOS LaunchAgent plists for periodic refresh. Install with `launchctl load`. Linux equivalents (systemd timers) are not currently shipped.

---

## Retrieval eval

The retrieval-quality eval lives at [`tests/eval/retrieval_eval.py`](../tests/eval/retrieval_eval.py). It is **not** part of `pytest`'s default run — it's slow, runs against the live catalog, and would interfere with the unit tests' speed.

Run deliberately when iterating on ranking or retrieval:

```bash
.venv/bin/python3 tests/eval/retrieval_eval.py
```

Output: per-query rank of the expected answer, per-modality breakdown, and aggregate recall@1 / recall@3 / MRR.

The eval set is curated — short, real queries with expected primary chapters. To extend, edit the constants at the top of the file.

### Skills Factory trigger-routing eval

```bash
.venv/bin/python3 -m mcp-servers.kb-mcp.skills_eval
```

Reports recall@1 / recall@3 / MRR for the trigger-routing decision (which skill should be invoked for this query?).

### Extraction eval (Phase 2.6)

```bash
.venv/bin/python3 scripts/extraction_eval.py
.venv/bin/python3 scripts/extraction_eval.py --baseline logs/extraction_eval_baseline.md
```

Reports precision / recall / F1 on the extraction golden set ([`tests/eval/golden_extractions.json`](../tests/eval/golden_extractions.json)) and same-vs-different accuracy on the resolution golden pairs. Use as the quality gate when iterating on extraction prompts.

---

## Deferred work

The substrate, ranking engine, concept graph, and 17 generators have all shipped. Most of what was historically deferred has been resolved through targeted cleanup rounds. This section is the honest current ledger — what's done, what's still open, and why the open items haven't been closed yet.

The full per-cleanup-round detail lives in the auto-memory at `~/.claude/projects/-Users-markoswald-Developer-projects-myPub/memory/` (notably `project_post_resplit_state_*`, `project_doc_source_expansion_*`, `project_cleanup_closeout_*`).

### Recently closed

| Item | Resolution |
|---|---|
| Phase 1 splitter bug (88.5% chapter content duplicated across TOC siblings) | Fixed via fragment-anchor slicing + in-place migration that preserved `chapter_id`s. Post-fix: 3.3% duplication. |
| Doc-source coverage (10 → 54 sources) | Batched Context7 discovery + Anthropic Batch API extraction (Haiku 4.5) + alignment (Sonnet 4.6). Final: 54 sources, 1,909 sections. |
| 3 stranded doc sources (MLflow, FastMCP, DuckPGQ — snapshots without extraction) | Re-prepped + extracted + aligned. Added 448 alignment edges. |
| 20 books missing authors (incl. 3 "AUTHOR NAMES HERE" placeholders, 17 with no `dc:creator`) | Resolved via OpenLibrary + Google Books ISBN lookup. 1 unresolvable book remains (Platform Enterprise — source ePub has no author metadata at all). |
| Author smush bug (161 rows containing comma-separated co-authors as one row) | Fixed via robust splitter with credential-suffix re-merging (M.D., Ph.D., Jr., II, III). |
| Concept-name duplicate hygiene (8,326 strict-orphan duplicates across `concept_type` variants) | Removed via `scripts/dedupe_concepts.py`. ~26K multi-type groups remain but all have edges (resolver-fix in `ecc74f4` routes lookups to the richest twin). |
| Procedure extraction on doc sections | 970 doc-section procedures + 175K procedure-concept links extracted as part of the doc-source expansion. |
| New-book ingestion path validation | 32 new ePubs ingested; 30 indexed cleanly; 2 with empty `OEBPS/content.opf` recovered via custom indexer that parses TOC xhtml directly; 1 Safari-rename duplicate caught at chapter-content-hash time. |

### Truly open

| Item | Why it's still open |
|---|---|
| **CONTRADICTS quality** | Avg confidence on the 24 CONTRADICTS edges is 0.16 — most are degenerate. The 9 high-conf CONTRADICTS we surfaced from the FastMCP/DuckPGQ recovery (FastMCP allowing breaking changes in minor versions, contradicting SemVer textbooks) didn't reproduce on a later re-run because alignment is non-deterministic. Real fix: contradiction-tuned prompt + multi-sample voting (N=3, accept any conf-≥0.7). Migration Guide and Currency Report quality is gated on this. |
| **Generator-output validation** | The 17 generators all ship and pass unit + integration tests. What's missing is real-eval grading: run `/kb-currency-report`, `/kb-migration-guide`, `/kb-bootstrap` against the now-richer substrate and inspect the output. That's the only way to know if the substrate actually delivers, not just that it ingested cleanly. |
| **Domain gaps for healthcare / life sciences** | Catalog now has decent biology/genomics books (Biology for Engineers, NGS Data Analysis, Zero to Genetic Engineering Hero) but zero PubMed Central / clinical-trial papers / HL7-FHIR specs. These need different ingestion paths (JATS XML, FHIR resources). New `source_type` column values would map them in cleanly without overloading `chapter` or `doc_section`. |
| **Project Bootstrap v2** | v1 emits skeletons + sub-agent prompts; v2 wraps the Task-agent dispatch loop and adds runtime validation (`pip install + pytest + docker-compose up + smoke-test`). Mirrors the Skills Factory's prep→dispatch→process pattern. |
| **Tutorial / Content Brief prose layer (Phase 9 + 10 v2)** | v1 generators emit deterministic skeletons. v2 dispatches a sub-agent per file to fill in pedagogical prose. Architecture is the same as Bootstrap v2. |
| **Ranking weight tuning from real eval data** | Current `WEIGHT_PROFILES` were calibrated to dogfooding observation, not eval-driven. The retrieval eval set should grow and drive the next round of weight tuning. |
| **1 book with no author** | `book_id=558` Platform Enterprise (ISBN 9798341643444). The source ePub ships an O'Reilly template OPF with no creator field; OpenLibrary, Google Books, and ISBN search all return nothing. Genuinely unknown without a different source. |

---

## Corpus gaps

Topics where the system returned tangential content despite working correctly (corpus simply doesn't cover them):

- **TLS certificate pinning** — no chapter title in the corpus matches all three tokens; PostgreSQL SSL doc wins as defensible-but-not-canonical. A security-focused book would close this.
- **FHIR / HL7 / EHR integration patterns** — healthcare data exchange returns tangential content. FHIR and HL7 are real registered libraries on Context7 / DeepWiki; running `/kb-discover` for these terms would probe them and seed the doc layer. The HL7 layer of Project Bootstrap is doc-only today (0 procedures) for the same reason.

---

## Disaster recovery

The catalog (`data/catalog.ddb`) and the run artifacts (`data/batch-runs/`, `data/refresh/`) are gitignored. Recovery scenarios:

### Scenario 1 — catalog wiped, run artifacts intact

```bash
# Rebuild substrate (books / chapters / authors / embeddings / indexes)
.venv/bin/python3 scripts/migrate_v2_schema.py
.venv/bin/python3 scripts/install_extensions.py
.venv/bin/python3 scripts/index_books.py
.venv/bin/python3 scripts/generate_embeddings.py
.venv/bin/python3 scripts/build_fts_index.py
.venv/bin/python3 scripts/build_vss_index.py
.venv/bin/python3 scripts/build_property_graph.py

# Re-populate doc_sources + snapshots
.venv/bin/python3 scripts/seed_doc_sources.py
.venv/bin/python3 scripts/refresh_docs.py refresh --all --no-extract

# Replay concept + procedure extraction for each batch-run dir
# (the result JSON files are already on disk — no API calls needed)
for d in data/batch-runs/concepts-*/; do
  .venv/bin/python3 scripts/extract_batch.py process --output-dir "$d"
done
for d in data/batch-runs/procedures-*/; do
  .venv/bin/python3 scripts/extract_procedures.py process --output-dir "$d"
done

# Replay doc-section extraction + alignment for each refresh dir
for d in data/refresh/*/snapshot_*/; do
  .venv/bin/python3 scripts/refresh_docs.py process --output-dir "$d"
done
for d in data/refresh/*/alignment_*/; do
  .venv/bin/python3 scripts/refresh_docs.py align-process --output-dir "$d"
done
```

The expensive part — Anthropic Batch API extraction + alignment — is recoverable as long as the result JSONs in `data/batch-runs/` and `data/refresh/*/` survive.

### Scenario 2 — both catalog and run artifacts lost

The full ingestion + extraction + alignment pipeline would need to be re-run from scratch. Cost estimate: ~$10-20 in Anthropic Batch API spend (Haiku 4.5 + Sonnet 4.6 with prompt caching), ~6-8 hours wall-clock dominated by Batch API processing time. Worth backing up `data/batch-runs/` and `data/refresh/` before any catalog migration or schema change.

### Scenario 3 — embeddings need re-generation

```bash
# Drop the embedding side tables
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb')
c.execute('DELETE FROM chapter_embedding')
c.execute('DELETE FROM doc_section_embedding')
c.close()
"

# Re-generate
.venv/bin/python3 scripts/generate_embeddings.py
```

~55 minutes for 118K chapter embeddings on Apple Silicon MPS.

### Scenario 4 — DuckDB version upgrade

Pinned to **1.5.0** because:

- DuckPGQ extension is absent on 1.5.2
- DuckPGQ extension is broken on 1.5.1
- The 1.5.0 FK bugs are worked around in the schema (side tables for embeddings; application-enforced self-FKs)

Before upgrading: verify DuckPGQ availability on the target version, test the FK-bug regressions in [`tests/test_duckdb_fk_bugs.py`](../tests/test_duckdb_fk_bugs.py), and back up the catalog.

---

## Diagnostics

### Catalog health snapshot

```bash
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb', read_only=True)
print('books:                ', c.execute('SELECT COUNT(*) FROM book').fetchone()[0])
print('chapters:             ', c.execute('SELECT COUNT(*) FROM chapter').fetchone()[0])
print('chapters with embed:  ', c.execute('SELECT COUNT(*) FROM chapter_embedding').fetchone()[0])
print('concepts:             ', c.execute('SELECT COUNT(*) FROM concept').fetchone()[0])
print('graph edges:          ', c.execute('SELECT COUNT(*) FROM concept_relation').fetchone()[0])
print('alignment edges:      ', c.execute('SELECT COUNT(*) FROM alignment_edge').fetchone()[0])
print('procedures:           ', c.execute('SELECT COUNT(*) FROM procedure').fetchone()[0])
print('doc sources:          ', c.execute('SELECT COUNT(*) FROM doc_source').fetchone()[0])
print('doc sections:         ', c.execute('SELECT COUNT(*) FROM doc_section').fetchone()[0])
print('skill_packages:       ', c.execute('SELECT COUNT(*) FROM skill_package').fetchone()[0])
print('generated_packages:   ', c.execute('SELECT COUNT(*) FROM generated_package').fetchone()[0])
c.close()
"
```

### MCP server logs

The `mypub-kb` server logs to stderr by default. To redirect:

```bash
.venv/bin/python3 mcp-servers/kb-mcp/server.py 2> /tmp/mypub-kb.log
```

Look for:

- `discovery_log` rows (auto-discovery events)
- `concept_query_log` rows (every concept-resolution call)
- ranker score breakdowns when verbose logging is enabled

### Slow query investigation

```bash
.venv/bin/python3 -c "
import duckdb
c = duckdb.connect('data/catalog.ddb', read_only=True)
c.execute('PRAGMA enable_profiling = json')
c.execute('PRAGMA profiling_output = \"/tmp/duckdb_profile.json\"')
# run your query
c.close()
"
```

DuckDB's JSON profile shows the per-operator breakdown.

### Cleaning up orphaned generated packages

`data/generated-packages/` accumulates. To prune anything older than 30 days:

```bash
find data/generated-packages -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

The catalog's `generated_package` rows aren't auto-deleted; orphaned rows are harmless, but to clean those too:

```sql
DELETE FROM generated_source
WHERE generated_file_id IN (
  SELECT generated_file_id FROM generated_file
  WHERE generated_package_id IN (
    SELECT generated_package_id FROM generated_package
    WHERE created_at < now() - INTERVAL 30 DAY
  )
);
DELETE FROM generated_file WHERE generated_package_id IN (...same...);
DELETE FROM generated_unit WHERE generated_package_id IN (...same...);
DELETE FROM generated_package WHERE created_at < now() - INTERVAL 30 DAY;
```

---

## Test suite

```bash
./scripts/test.sh                       # full suite (37 test modules)
./scripts/test.sh -k resolve            # filter
./scripts/test.sh tests/test_schema.py  # single file
./scripts/test.sh -v                    # verbose
```

Layout:

| File | Tests |
|---|---|
| `test_schema.py` | v2 schema shape + backfill invariants |
| `test_resolution.py` | EntityResolver three-stage logic |
| `test_resolve_concept.py` | review-queue actions (merge / alias / keep-separate / rename) |
| `test_extract_entities.py` | validation + process_extraction_json |
| `test_extract_batch.py` | prep / process / status, dedup / skip / front-matter filter |
| `test_index_books.py` | end-to-end indexing of a programmatic ePub |
| `test_migrate_add_content_hashes.py` | content-hash migration idempotency |
| `test_duckdb_fk_bugs.py` | pinned regressions for the 1.5.0 FK-handling bugs |
| `test_phase1_integration.py` | FTS × VSS × DuckPGQ retrieval |
| `test_phase2_integration.py` | index → extract → resolve E2E |
| `test_extraction_eval.py` | extraction eval framework tests |
| `test_phase7_*` through `test_phase17_*` | per-generator unit and integration tests |

Pre-commit hook (opt-in): `./scripts/install-git-hooks.sh`. Disable with `git config --unset core.hooksPath`.

---

## See also

- [docs/architecture.md](architecture.md) — system overview
- [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) — pipeline details
- [docs/concept-graph.md](concept-graph.md) — extraction lifecycle
- [docs/customization.md](customization.md) — tuning weights and characters
