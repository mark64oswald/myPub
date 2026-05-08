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

Known debt as of the last push. See `~/.claude/projects/-Users-markoswald-Developer-projects-myPub/memory/project_deferred_retrieval_work.md` for the running log.

### Alignment

| Item | State |
|---|---|
| Apache Kafka, Apache Spark, PostgreSQL, DuckDB, Delta Lake, Databricks, LangChain | ✅ aligned (107 sections + 35 → 120 edges) |
| MLflow (26 sections) | Pending — medium effort, ~50 sub-agent runs |
| FastMCP (437 sections) | Deferred indefinitely — narrow vendor API, low expected book overlap |
| DuckPGQ (282 sections) | Deferred indefinitely — same reasoning |
| CONTRADICTS-tuned alignment prompts | Pending — current prompt produces only CORROBORATES; Migration Guide and Currency Report are data-starved until this lands |

### Procedure extraction on doc sections

`procedure` table currently has 4,341 chapter-sourced rows and zero doc-section-sourced rows. The Phase 4.4b alignment prep generated `prompt_section_<id>_proc.txt` files for each aligned source's sections, but these were never dispatched to sub-agents. To activate:

```bash
# Re-prep against the same output dir already used per source
.venv/bin/python3 scripts/extract_procedures.py prep --source kafka
# Dispatch sub-agents to process the procedure prompts
# Re-run process — idempotent if rows already exist
.venv/bin/python3 scripts/extract_procedures.py process
```

~20–25 sub-agent runs per source.

### Phase 1 splitter bug

94.8% of chapters share content with siblings due to a Phase 1 sectionizer issue. This is mitigated at retrieval time (the ranker dedupes near-identical chapters), but a proper fix needs a re-ingestion pass with a corrected splitter. Tracked in `project_phase1_splitter_bug.md`. Not blocking dogfooding — the retrieval mitigations are working — but should be fixed before the next major version.

### Concept-name duplicate hygiene

5,597 concept-name groups have duplicates across `concept_type` variants (e.g., "Event Sourcing" as both `Concept` and `Pattern`). The resolver bug that picked the empty twin was fixed in `ecc74f4` — the duplicates themselves are now harmless, but consolidating them would tighten the graph. `scripts/dedupe_concepts.py` is the script for this; its flags (`--catalog`, `--dry-run`, `--limit`, `--report-file`) let you preview a merge run and capture a report before applying. Start with `--dry-run` to see what would change.

### Ranking weight tuning

Current `WEIGHT_PROFILES` were calibrated to dogfood data, not eval-driven. Watch for:

- `rec=0.10` too low for the default? (currency-critical queries getting wrong answers under `balanced_interactive`)
- `TITLE_COVERAGE_BOOST=0.8` too aggressive? (chapters with metaphorical titles winning)
- `skill_*` profiles correctly calibrated for current Skills Factory? (untested since May 2026)

Real eval data from sustained dogfooding should drive the next iteration.

### Author placeholder

3 books currently have NO author after the "AUTHOR NAMES HERE" placeholder cleanup in `ecc74f4`:

- `book_id=42` — Agentic AI Data Architectures (O'Reilly)
- `book_id=151` — Data Mesh (O'Reilly) — by Zhamak Dehghani
- `book_id=484` — TensorFlow 2 Pocket Reference (O'Reilly) — by KC Tung

To restore: re-extract author metadata from the source ePubs (`book.source_path`).

### Generator v2 work

The generator program shipped v1 (deterministic skeleton + sub-agent prompts). v2 items:

- **Bootstrap dispatch loop.** Wrap the Task agent dispatch (mirror Skills Factory's prep/process pattern).
- **Bootstrap runtime validation.** Add `pip install + pytest + docker-compose up + data flows` smoke pass.
- **Content Generator prose layer** (Phase 9.1–9.3). Sub-agent dispatch for actual prose, not just the brief skeleton.
- **Tutorial prose layer** (Phase 10). Rewrite each procedure step as pedagogical prose via sub-agent.

---

## Corpus gaps

Topics where the system returned tangential content despite working correctly (corpus simply doesn't cover them):

- **TLS certificate pinning** — no chapter title in the corpus matches all three tokens; PostgreSQL SSL doc wins as defensible-but-not-canonical. A security-focused book would close this.
- **FHIR / HL7 / EHR integration patterns** — healthcare data exchange returns tangential content. FHIR and HL7 are real registered libraries on Context7 / DeepWiki; running `/kb-discover` for these terms would probe them and seed the doc layer. The HL7 layer of Project Bootstrap is doc-only today (0 procedures) for the same reason.

---

## Disaster recovery

The catalog (`data/catalog.ddb`) and the run artifacts (`data/extraction-runs/`, `data/alignment-runs/`) are gitignored. Recovery scenarios:

### Scenario 1 — catalog wiped, run artifacts intact

```bash
# Re-run Phase 1-3 ingestion to rebuild books / chapters / concepts
.venv/bin/python3 scripts/migrate_v2_schema.py
.venv/bin/python3 scripts/install_extensions.py
.venv/bin/python3 scripts/index_books.py
.venv/bin/python3 scripts/generate_embeddings.py
.venv/bin/python3 scripts/build_fts_index.py
.venv/bin/python3 scripts/build_vss_index.py
.venv/bin/python3 scripts/build_property_graph.py

# Re-populate doc_sections from existing snapshots
.venv/bin/python3 scripts/refresh_docs.py refresh --all --no-extract

# For each aligned source, replay extraction + alignment from existing
# JSON results in data/extraction-runs/ and data/alignment-runs/
for src in postgres kafka spark duckdb delta databricks langchain; do
  .venv/bin/python3 scripts/migrate_phase4_4b_alignment.py process --source $src
  .venv/bin/python3 scripts/migrate_phase4_4b_alignment.py align-process --source $src
done
```

The expensive sub-agent extraction is recoverable as long as the run-artifact JSONs survive.

### Scenario 2 — both catalog and run artifacts lost

Only re-running ~145 sub-agent extractions would reconstruct the alignment data. Worth backing up `data/extraction-runs/` and `data/alignment-runs/` before any catalog migration or schema change.

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

~55 minutes for 113K chapter embeddings on Apple Silicon MPS.

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
./scripts/test.sh                       # full suite (830 tests)
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
