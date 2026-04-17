# myPub v2: Execution Plan

**Purpose:** Step-by-step instructions for building myPub v2 in Claude Code, organized as build prompts modeled after healthsim-workspace/build-prompts. Each step includes the prompt, expected validation, and commit checkpoint.

**Companion document:** `mypub-v2-architecture.md` (the design reference)

**Working conventions:**
- Every prompt runs in Claude Code from the project root (`~/Developer/projects/myPub`)
- Test and validate at every step; do not proceed with broken state
- Fix issues when you find them, even from prior sessions
- Commit and push after every successful checkpoint (marked with 🔀)
- README.md is updated as features land, not at the end
- Use Context7 MCP to verify DuckDB extension APIs, FastMCP patterns, and any library docs before writing code

---

## Pre-flight: Repository Setup

### Prompt 0.1 — Initialize v2 branch and project structure

```
Create a new branch `v2-substrate` from main. Set up the v2 project structure
per the architecture doc at docs/mypub-v2-architecture.md (which I'll add to the
repo). Create the directory skeleton:

  .claude/skills/kb-usage/
  .claude/skills/skills-factory/
  .claude/commands/
  mcp-servers/kb-mcp/
  scripts/
  schemas/
  patterns/
  tests/
  logs/
  launchd/
  data/generated-packages/

Create a CLAUDE.md at the project root per the spec in the architecture doc §9.3.
Create a pyproject.toml with project metadata and dependencies:
  duckdb, sentence-transformers, fastmcp, markdown-it-py, pydantic, pytest, httpx

Set up a Python venv. Verify `duckdb` imports and prints its version.

Do NOT modify the existing v1 catalog.ddb — we'll evolve it in Phase 1.

README.md: Add a "v2 Development" section explaining the branch, the architecture
doc location, and how to set up the dev environment.
```

**Validate:**
- `python -c "import duckdb; print(duckdb.__version__)"` succeeds
- Directory structure matches the architecture doc §9.2
- CLAUDE.md is present and readable
- README.md has the new section

🔀 Commit: `chore: initialize v2 project structure and dependencies`

### Prompt 0.2 — Copy and commit the architecture doc

```
Copy the architecture doc into docs/mypub-v2-architecture.md.
Also create docs/EXECUTION-PLAN.md (this document).
```

🔀 Commit: `docs: add v2 architecture and execution plan`

---

## Phase 1: Substrate Upgrade (week 1–2)

**Goal:** DuckDB with FTS, VSS, and DuckPGQ extensions working against the existing catalog. Existing commands still work; new retrieval modalities are available.

### Prompt 1.1 — Schema migration: add v2 tables

```
Read the existing catalog.ddb schema. Compare it to the v2 target schema in
docs/mypub-v2-architecture.md §7.1.

Write a migration script at scripts/migrate_v2_schema.py that:
1. Makes a backup copy of data/catalog.ddb → data/catalog_v1_backup.ddb
2. Adds all new v2 tables that don't exist yet (doc_source, doc_snapshot,
   doc_section, concept_alias, concept_resolution_queue, concept_query_log,
   procedure, skill_package, skill, skill_source, skill_file, skill_relation)
3. ALTERs existing tables to add new columns (embedding on chapter, embedding
   on concept, query_count and last_queried_at on concept)
4. Does NOT drop or modify existing data
5. Prints a before/after summary of tables and row counts

Use Context7 to verify DuckDB ALTER TABLE syntax for adding columns.

Run the migration. Verify all new tables exist.
Write the target DDL to schemas/catalog.sql for reference.

Tests: write tests/test_schema.py that verifies every expected table and column
exists in the migrated database.
```

**Validate:**
- `pytest tests/test_schema.py -v` passes
- `duckdb data/catalog.ddb "SELECT table_name FROM information_schema.tables ORDER BY 1"` shows all expected tables
- Existing data (books, chapters, concepts) is intact

🔀 Commit: `feat(phase1): migrate schema to v2 with new tables`

### Prompt 1.2 — Install and verify DuckDB extensions

```
Write scripts/install_extensions.py that:
1. Opens the catalog.ddb
2. Installs and loads: fts, vss, duckpgq (community extensions)
3. Runs a smoke test for each:
   - FTS: create a temp table, build a full-text index, run a match query
   - VSS: create a temp table with FLOAT[384] column, build an HNSW index,
     run a similarity search
   - DuckPGQ: create temp vertex/edge tables, define a property graph,
     run a simple path query
4. Reports pass/fail for each extension

Use Context7 to check the current DuckDB docs for the exact extension install
and usage syntax — these APIs have changed recently.

Run it. All three should pass. If any fail, debug before proceeding.
```

**Validate:**
- All three extensions pass smoke tests
- Script is idempotent (safe to re-run)

🔀 Commit: `feat(phase1): install and verify FTS, VSS, DuckPGQ extensions`

### Prompt 1.3 — Generate embeddings for existing chapters

```
Write scripts/generate_embeddings.py that:
1. Loads sentence-transformers/all-MiniLM-L6-v2
2. Reads all chapters from catalog.ddb that have NULL embedding
3. Generates embeddings for chapter.content (truncated to model max if needed)
4. Writes embeddings back to chapter.embedding
5. Also generates embeddings for concept.description where NULL
6. Reports: N chapters embedded, N concepts embedded, elapsed time

Start with a batch of 10 chapters first as a timing test. Then run the full
corpus. Log progress every 50 chapters.

This will take a while for 345 books. That's fine.
```

**Validate:**
- `SELECT COUNT(*) FROM chapter WHERE embedding IS NOT NULL` matches total chapters
- `SELECT COUNT(*) FROM concept WHERE embedding IS NOT NULL` matches total concepts
- Spot-check: query a few embeddings, verify they're 384-dim float arrays

🔀 Commit: `feat(phase1): generate embeddings for chapters and concepts`

### Prompt 1.4 — Build FTS indexes

```
Write scripts/build_fts_index.py that:
1. Creates a full-text index on chapter.content using the FTS extension
2. Runs test queries:
   - "change data capture" → should return chapters discussing CDC
   - "dimensional modeling" → should return Kimball-related chapters
   - A known exact phrase from a specific book → should return that chapter
3. Reports hit counts for each test query

Use Context7 to verify the current DuckDB FTS API (pragma_create_fts_index or
CREATE INDEX ... USING fts — the syntax has changed across versions).
```

**Validate:**
- Test queries return sensible results
- FTS index persists across database close/reopen

🔀 Commit: `feat(phase1): build FTS indexes on chapter content`

### Prompt 1.5 — Build VSS indexes

```
Write scripts/build_vss_index.py that:
1. Creates an HNSW index on chapter.embedding using the VSS extension
2. Creates an HNSW index on concept.embedding
3. Runs test queries:
   - Generate an embedding for "how to implement change data capture"
   - Find the 5 nearest chapters by cosine similarity
   - Find the 5 nearest concepts by cosine similarity
4. Reports results with similarity scores

Note whether the HNSW index persists across close/reopen. If not, document this
as a known limitation — we'll rebuild on MCP server startup.
```

**Validate:**
- Semantic search returns topically relevant results
- Cross-check: FTS results for "change data capture" and VSS results for the same query should have meaningful overlap (not identical, but correlated)

🔀 Commit: `feat(phase1): build VSS/HNSW indexes on embeddings`

### Prompt 1.6 — Define the property graph

```
Write schemas/property_graph.sql with the DuckPGQ CREATE PROPERTY GRAPH statement
from the architecture doc §7.2.

Write scripts/build_property_graph.py that:
1. Runs the property graph DDL
2. Tests basic graph queries:
   - Find all concepts DISCUSSED in a specific chapter
   - Find REQUIRES chains from a concept
   - Cross-author comparison: find concepts discussed by 2+ different books
3. Reports results

Use Context7 to verify DuckPGQ syntax — specifically the SQL/PGQ MATCH clause
and ANY SHORTEST PATH.
```

**Validate:**
- Graph queries return results
- Prerequisite chains make sense (spot-check 3-4 manually)

🔀 Commit: `feat(phase1): define property graph over catalog tables`

### Prompt 1.7 — Phase 1 integration test

```
Write tests/test_phase1_integration.py that exercises all three retrieval
modalities together:

1. Pick a topic that should appear in books (e.g., "star schema")
2. Run FTS search → collect chapter IDs
3. Run VSS search → collect chapter IDs
4. Run DuckPGQ traversal from the concept → collect chapter IDs via DISCUSSES edges
5. Assert: there is non-trivial overlap between the three result sets
6. Assert: VSS finds at least one chapter that FTS missed (semantic > keyword)
7. Assert: DuckPGQ finds at least one chapter via a graph traversal that neither
   FTS nor VSS returned directly

Repeat for 3 different topics. This is the eval set seed — we'll grow it.

Write tests/eval/phase1_eval_set.json with the test cases so we can re-run
as a regression suite.
```

**Validate:**
- `pytest tests/test_phase1_integration.py -v` passes
- Results are plausible (human review of a few)

🔀 Commit: `feat(phase1): integration tests across all three retrieval modalities`

### Prompt 1.8 — Update README for Phase 1

```
Update README.md:
- Document the three DuckDB extensions and what they enable
- Add a "Quick start" section showing how to run a semantic search query
- Add a "Development" section with instructions for running tests
- Add a "Phase 1 status" badge or note
```

🔀 Commit: `docs: update README for Phase 1 substrate upgrade`

---

## Phase 2: Automated Extraction + Entity Resolution (week 3–5)

**Goal:** Entity/concept extractor with entity resolution. Full-corpus graph population. Review queue for borderline matches.

### Prompt 2.1 — Entity resolution module

```
Build the entity resolution module at mcp-servers/kb-mcp/resolution.py.

This is the foundation that ALL extraction depends on, so build it first.

Implement the three-stage resolution from the architecture doc §5.2:
1. Exact name match (case-insensitive on concept.name)
2. Alias match (lookup in concept_alias table)
3. Embedding similarity match:
   - ≥ 0.90 → auto-match, optionally register alias
   - 0.75–0.89 → borderline, enqueue to concept_resolution_queue
   - < 0.75 → new concept

Write the resolution as a class EntityResolver with a resolve(candidate_name,
candidate_context) → (concept_id, is_new, resolution_type) method.

Seed the concept_alias table with obvious abbreviations from the existing
concept table (e.g., CDC ↔ Change Data Capture, ETL ↔ Extract Transform Load).
Write a script scripts/seed_aliases.py that scans existing concepts and
proposes aliases using an LLM call.

Tests: write tests/test_resolution.py with cases for:
- Exact match (known concept name)
- Alias match (known abbreviation)
- High-similarity match (paraphrase of existing concept)
- Borderline match (related but distinct concept)
- Genuinely new concept
```

**Validate:**
- `pytest tests/test_resolution.py -v` passes
- Alias seed script produces sensible results (human review)

🔀 Commit: `feat(phase2): entity resolution module with alias table and review queue`

### Prompt 2.2 — Entity/concept extractor

```
Build scripts/extract_entities.py — the LLM-based entity extractor.

Design:
- Takes a chapter (or later, a doc_section) as input
- Uses a structured prompt with the domain ontology (allowed entity types:
  Concept, Pattern, Tool, Framework, Algorithm, Technique; allowed relation
  types: REQUIRES, EXTENDS, CONTRASTS_WITH, IMPLEMENTS, CITES)
- Returns structured JSON with entities and relations
- Each entity passes through EntityResolver before writing to DB
- Records provenance via (source_type='chapter', source_id=chapter_id)

Start with a SINGLE CHAPTER as a test. Pick a meaty one (e.g., a chapter on
data modeling patterns). Run the extractor. Inspect the output:
- Are the entity types correct?
- Are the relation types sensible?
- Did resolution correctly merge with existing concepts?
- Did it create new concepts where appropriate?

Print a summary: N entities extracted, N matched existing, N new, N borderline.

Do NOT run the full corpus yet. Tune the prompt based on this one chapter.
```

**Validate:**
- Single-chapter extraction produces reasonable entities (human review)
- Resolution correctly matches known concepts
- No crashes, no schema violations

🔀 Commit: `feat(phase2): entity extractor with single-chapter test`

### Prompt 2.3 — Tune extraction on 10-book sample

```
Run the entity extractor against 10 diverse books (mix of topics: data modeling,
distributed systems, cloud, programming, DevOps). For each book, extract all
chapters.

After extraction, analyze quality:
1. SELECT concept_type, COUNT(*) FROM concept GROUP BY 1 — type distribution
2. SELECT relation_type, COUNT(*) FROM concept_relation GROUP BY 1 — relation dist
3. SELECT resolution_action, COUNT(*) FROM concept_resolution_queue GROUP BY 1
4. Spot-check 20 random concept_relation rows — are the relations correct?
5. Spot-check 10 resolution queue entries — are the similarity scores reasonable?

If extraction quality is poor (>30% nonsensical relations, or >50% wrong entity
types), adjust the prompt and re-run on the same 10 books.

This is the tuning loop. Run it until the quality is acceptable.
```

**Validate:**
- Entity type distribution is reasonable (not all one type)
- Relations make semantic sense (spot-check)
- Resolution queue contains genuinely borderline cases, not obvious matches or misses

🔀 Commit: `feat(phase2): tuned extraction across 10-book sample`

### Prompt 2.4 — Full corpus extraction

```
Run the entity extractor against the full corpus (~345 books). This will take
a while. Log progress every book.

Batch settings:
- Process books in alphabetical order
- Commit to DB every 10 books (not every chapter — too many small transactions)
- Write extraction stats to logs/extraction_run_YYYYMMDD.log
- If a chapter fails extraction (LLM error, timeout), log it and continue

After completion, run the quality analysis from Prompt 2.3 on the full corpus.
Report the same metrics.
```

**Validate:**
- All books processed (check for skipped/failed chapters)
- Graph is substantially larger than before
- Resolution queue has items to review

🔀 Commit: `feat(phase2): full corpus entity extraction complete`

### Prompt 2.5 — Review queue command

```
Build the /kb-review-concepts slash command at .claude/commands/kb-review-concepts.md.

The command should call the MCP server's list_pending_resolutions tool, display
each borderline item with context, and let me choose: merge, alias, keep_separate,
or rename. Process items one at a time.

Also build the resolve_concept tool in the MCP server that handles each action.

Review the first 25 items in the queue now. Resolve them. This teaches the system
(aliases registered, thresholds calibrated by example).
```

**Validate:**
- Command works interactively
- Resolved items are removed from the queue
- Merges correctly rewrite edges
- Aliases are registered and used by subsequent resolution calls

🔀 Commit: `feat(phase2): concept review queue command and resolution workflow`

### Prompt 2.6 — Phase 2 eval set and autoresearch

```
Create an autoresearch eval for extraction quality.

Write tests/eval/extraction_eval.py that:
1. Loads a golden set of 50 manually-verified (concept, chapter) pairs from
   tests/eval/golden_extractions.json
2. For each pair, checks whether the extractor found the concept in that chapter
3. Reports precision, recall, and F1
4. Checks entity resolution quality: for 20 known-same-concept pairs, did
   resolution merge them? For 20 known-different-concept pairs, did resolution
   keep them separate?

Write tests/eval/golden_extractions.json by sampling from the extraction results
and manually verifying. This is the holdout set.

The autoresearch loop for extraction tuning:
1. Run eval → get baseline F1 and resolution accuracy
2. Modify the extraction prompt (one change at a time)
3. Re-run eval on the golden set
4. If F1 improved → keep the prompt change, commit
5. If F1 declined → revert
6. Repeat

Run 3-5 iterations now to establish a baseline.
```

**Validate:**
- Golden set is manually verified
- Baseline metrics are recorded in logs/extraction_eval_baseline.md
- At least one prompt improvement iteration completed

🔀 Commit: `feat(phase2): extraction eval set and autoresearch baseline`

---

## Phase 3: Procedure Extraction (week 6–7)

### Prompt 3.1 — Procedure extractor

```
Build scripts/extract_procedures.py, similar to the entity extractor but with
a different prompt targeting step-by-step procedures.

The procedure prompt should extract:
- procedure.name
- procedure.preconditions
- procedure.steps (JSON array)
- procedure.postconditions
- procedure.failure_modes
- Linked concepts (via EntityResolver)

Test on 5 chapters known to be procedural (e.g., a tutorial chapter, an
operations guide chapter). Inspect output quality.

Then run on the full corpus. Not every chapter has procedures — expect many
no-ops. Report: N chapters processed, N procedures extracted, N chapters with
zero procedures.
```

**Validate:**
- Procedure steps are actual steps (not summaries or descriptions)
- Linked concepts resolve correctly
- Procedural chapters produce procedures; non-procedural chapters produce nothing

🔀 Commit: `feat(phase3): procedure extraction with full corpus run`

---

## Phase 4: Live Docs + Ranking (week 8–10)

### Prompt 4.1 — MCP server skeleton

```
Build the KB MCP server at mcp-servers/kb-mcp/server.py using FastMCP.

Use Context7 to check the current FastMCP API for tool registration and stdio
transport setup.

Start with three tools:
- search_chapters(query, mode='interactive') — fans out to FTS + VSS + DuckPGQ
- compare_concept_across_authors(concept_name) — graph query
- find_prerequisites(concept_name, max_depth=5) — graph traversal

Test each tool end-to-end from Claude Code. The MCP config should match the
architecture doc §9.3.
```

**Validate:**
- MCP server starts without errors
- Each tool returns results from Claude Code
- `search_chapters` combines all three modalities

🔀 Commit: `feat(phase4): KB MCP server with hybrid retrieval tools`

### Prompt 4.2 — Context7 + DeepWiki + GitHub MCP integration

```
Configure all three doc-source MCP servers in Claude Code settings per the
architecture doc §9.3 (Context7 stdio, DeepWiki HTTPS, GitHub stdio).

Test each:
- Context7: resolve-library-id for "databricks", then query-docs for a topic
- DeepWiki: read_wiki_structure for a known repo (e.g., "jlowin/fastmcp")
- GitHub: get_file_contents for a README

Build the doc_source registry: write scripts/seed_doc_sources.py that populates
doc_source with initial entries for 10 key technologies from the corpus
(Databricks, PostgreSQL, DuckDB, FastMCP, etc.) with appropriate source_type,
authority_score, and refresh_ttl_days.
```

**Validate:**
- All three MCP servers respond
- doc_source table has 10+ entries

🔀 Commit: `feat(phase4): integrate Context7, DeepWiki, and GitHub MCP servers`

### Prompt 4.3 — Sectionizer

```
Build mcp-servers/kb-mcp/sectionizer.py — the module that parses doc content
into doc_section trees.

Implement three parsers:
1. Markdown heading tree (for GitHub README — use markdown-it-py)
2. Context7 chunk handler (content is pre-chunked, just persist sections)
3. DeepWiki page handler (use read_wiki_structure for the heading tree)

Plus the shapeless fallback: if no headings found, one section covers the whole doc.

Default split level: H2. Content from H3+ folds into its parent section.

Tests: write tests/test_sectionizer.py with:
- A well-structured README (multiple H2s with H3 subsections)
- A flat README (no headings below title)
- A deeply nested doc (H1/H2/H3/H4)
- An empty doc
```

**Validate:**
- `pytest tests/test_sectionizer.py -v` passes
- Section trees match expected heading hierarchy

🔀 Commit: `feat(phase4): sectionizer for Markdown, Context7, and DeepWiki content`

### Prompt 4.4 — Snapshot ingestion pipeline

```
Build the full snapshot ingestion pipeline from the architecture doc §6.2:

1. Fetch snapshot from MCP server
2. Compute content_hash, skip if unchanged
3. Persist doc_snapshot
4. Parse into doc_sections via sectionizer
5. Generate embeddings per section
6. Add sections to FTS index
7. Run entity extractor (with resolution) at section level
8. Run procedure extractor at section level
9. Compute alignment (CORROBORATES/CONTRADICTS edges)

Package as scripts/refresh_docs.py with CLI:
  python scripts/refresh_docs.py --source-id 1
  python scripts/refresh_docs.py --all
  python scripts/refresh_docs.py --tier hot

Test with a single doc_source (e.g., DuckDB docs via Context7). Inspect:
- Are sections created?
- Are embeddings generated?
- Did entity extraction run with resolution?
- Did new concepts appear that weren't in any book?
```

**Validate:**
- End-to-end pipeline works for one source
- doc_section rows created with correct hierarchy
- Entity resolution correctly links to existing concepts
- FTS and VSS queries return doc sections alongside book chapters

🔀 Commit: `feat(phase4): snapshot ingestion pipeline with section-level extraction`

### Prompt 4.5 — Ranking engine

```
Build mcp-servers/kb-mcp/ranking.py — the two-mode ranking engine from
the architecture doc §8.

Implement:
- score(passage, query) with the five-factor formula
- InteractiveRanker that returns {primary, corroborations, conflicts}
- GenerationRanker that applies selection strategy and returns consolidated set
- Weight profiles (the 5 profiles from §8.5)

Test with the search_chapters MCP tool — switch it to use the ranking engine
instead of raw result lists. In interactive mode, a query like "how to do CDC
in Databricks" should return:
- A primary result (highest scored)
- Corroborations (other sources that agree)
- Conflicts (if any book content contradicts current docs)

The ranking engine needs to be the place where book content and doc content
come together — verify that both appear in results.
```

**Validate:**
- Ranking produces sensible orderings
- Interactive mode surfaces conflicts when they exist
- Generation mode produces clean consolidated output

🔀 Commit: `feat(phase4): two-mode ranking engine with weight profiles`

### Prompt 4.6 — Phase 4 eval set

```
Create tests/eval/retrieval_eval.py that:
1. Loads test queries from tests/eval/retrieval_queries.json — 20 queries
   spanning topics with and without doc sources
2. For each query, runs hybrid retrieval + ranking in interactive mode
3. Checks:
   - Does the top result come from the most current source?
   - When books and docs disagree, is the conflict flagged?
   - For queries with no doc source, does the system degrade gracefully?
4. Reports a retrieval quality score

Build the test query set manually — include at least 5 queries where you know
books are outdated and docs should win.

Run the eval. Establish baseline. Adjust ranking weights if needed using the
autoresearch keep/revert loop.
```

**Validate:**
- Eval runs successfully
- Baseline metrics recorded
- At least one weight adjustment iteration completed

🔀 Commit: `feat(phase4): retrieval eval set with ranking weight baseline`

---

## Phase 4b: Proactive Refresh + Priority Tiers (week 10–11)

### Prompt 4b.1 — Fixed-TTL scheduled refresh

```
Extend scripts/refresh_docs.py to support --tier=auto mode:
- Query doc_source for all sources where last_refresh_at is beyond refresh_ttl_days
- Refresh those sources (most will no-op on unchanged content_hash)

Create launchd/com.mypub.refresh.plist per the architecture doc §6.6.
Write a setup script scripts/install_launchagent.sh that:
- Copies the plist to ~/Library/LaunchAgents/
- Loads it
- Verifies it's scheduled

Test: manually run the refresh script. Verify it logs to logs/refresh.log.
```

**Validate:**
- `launchctl list | grep mypub` shows the agent
- Manual run produces log output
- No-op sources complete quickly

🔀 Commit: `feat(phase4b): scheduled refresh with LaunchAgent`

### Prompt 4b.2 — Adaptive tiering

```
Build scripts/assign_tiers.py and mcp-servers/kb-mcp/tiering.py.

Add query logging: every call to search_chapters logs touched concepts to
concept_query_log. Nightly, assign_tiers.py computes priority_tier for each
doc_source from:
- Concept query frequency (30-day rolling)
- Graph centrality (count of DISCUSSES + REQUIRES edges)
- Source volatility (content_hash change rate)
- Pinned status

Build /kb-focus, /kb-pin-source, /kb-unpin-source, /kb-refresh-status commands.

Test: simulate query patterns by inserting fake log entries, run tier assignment,
verify sources move between tiers as expected.
```

**Validate:**
- Tier assignment produces sensible results
- Pin/unpin commands work
- `/kb-refresh-status` shows tier inventory

🔀 Commit: `feat(phase4b): adaptive priority tiers with user controls`

---

## Phase 5: Skills Factory (week 12–15)

### Prompt 5.1 — Decomposition via DuckPGQ community detection

```
Build the first stage of the Skills Factory: domain decomposition.

Given a domain string like "CDC with Databricks", use DuckPGQ to:
1. Find the anchor concepts (CDC, Databricks, etc.)
2. Traverse the graph to find related concepts (up to depth 3)
3. Use community detection (or clustering by edge density) to propose
   Skill groupings
4. Cross-reference with book ToCs for chapter-level topic boundaries
5. Refine with an LLM pass that names and scopes each proposed Skill

Test: decompose "data engineering on Databricks" and "dimensional modeling
fundamentals". Review the proposed Skill lists — are they reasonable? Would
you use them?
```

**Validate:**
- Decomposition produces 5-15 Skills for a mid-sized domain
- Skill boundaries don't overlap significantly
- Concepts that should be together are together

🔀 Commit: `feat(phase5): domain decomposition via graph community detection`

### Prompt 5.2 — Package planning and strategy selection

```
Build the second stage: given a proposed Skill list, plan the package.

1. Determine Skill ordering (prerequisites first, using REQUIRES edges)
2. Select source strategy per Skill (recent-doc anchored for tech with
   doc sources, consensus synthesis for foundational, authority pick when
   a single canonical source dominates)
3. Identify shared patterns to reference
4. Generate folder structure

Test: plan a package for "CDC with Databricks". Review the strategy assignments.
```

**Validate:**
- Prerequisite ordering is logical
- Strategy assignments match domain characteristics
- No circular dependencies

🔀 Commit: `feat(phase5): package planning with strategy selection`

### Prompt 5.3 — Per-Skill generation

```
Build the third stage: generate a single Skill.

1. Retrieve candidates via hybrid retriever scoped to Skill concepts
2. Rank silently in generation mode
3. Apply selection strategy
4. Resolve/drop conflicts per the architecture doc §8.4
5. Generate SKILL.md content with package-aware prompt (sees sibling Skills)
6. Generate trigger description with discrimination against siblings
7. Record provenance in skill_source (including dropped sources with reasons)

Test: generate one Skill from the "CDC with Databricks" package. Review:
- Is the content actionable and confident (no hedging)?
- Does the trigger description discriminate from siblings?
- Is provenance recorded (check skill_source)?
- Were any sources dropped? Are the drop reasons sensible?
```

**Validate:**
- Generated SKILL.md reads like a real, usable Skill
- Trigger description is specific and accurate
- Provenance is complete

🔀 Commit: `feat(phase5): per-Skill generation with provenance tracking`

### Prompt 5.4 — Full package generation and /kb-generate-skills command

```
Wire it all together:
1. Build the /kb-generate-skills command
2. Build mcp-servers/kb-mcp/skills_factory.py that orchestrates the full pipeline
3. Generate a complete package: "data engineering on Databricks"
4. Materialize to data/generated-packages/databricks-de/

Review the full package:
- Do the Skills cover the domain?
- Are there gaps?
- Do trigger descriptions work together?
- Are any Skills flagged for human review?
```

**Validate:**
- Package generates end-to-end
- SKILL.md files are well-formatted
- _package.md provides a useful overview

🔀 Commit: `feat(phase5): Skills Factory end-to-end with /kb-generate-skills command`

### Prompt 5.5 — Skills Factory eval

```
Create an autoresearch eval for Skills quality.

Write tests/eval/skills_eval.py that:
1. Generates a Skills package for a known domain
2. For each Skill:
   - Checks that the trigger description is non-empty and > 50 chars
   - Checks that the SKILL.md content references specific tools/APIs/patterns
   - Checks that skill_source has at least 2 sources
   - Checks that no two Skills in the package have identical trigger descriptions
3. Loads each SKILL.md into Claude and asks: "Given this query, would this Skill
   trigger?" with 10 positive and 10 negative queries per Skill
4. Reports trigger accuracy (TP, FP, TN, FN rates)

Run the eval. Establish baseline. Use the autoresearch loop to improve:
1. Run eval → get baseline trigger accuracy
2. Modify the description generation prompt
3. Re-generate, re-eval
4. Keep if improved, revert if not
```

**Validate:**
- Eval framework works
- Baseline trigger accuracy > 70%
- At least one improvement iteration completed

🔀 Commit: `feat(phase5): Skills Factory eval with trigger accuracy testing`

---

## Phase 6: Refinement and Tuning (ongoing)

### Prompt 6.1 — Comprehensive README

```
Rewrite README.md to cover the full v2 system:

1. Overview and purpose
2. Architecture diagram (include as an image or describe the 5-layer model)
3. Quick start: setup, first query, first Skills package generation
4. Commands reference (all /kb-* commands with examples)
5. MCP servers (what each does, how to configure)
6. Development guide:
   - Running tests
   - Running evals
   - The autoresearch eval loop for extraction and Skills quality
   - Git workflow (branch strategy, commit conventions)
7. Data model overview (link to architecture doc for details)
8. Phase status tracker
```

🔀 Commit: `docs: comprehensive v2 README`

### Prompt 6.2 — Full regression suite

```
Create tests/test_regression.py that runs ALL evals in sequence:
1. Schema integrity (test_schema.py)
2. Extension availability (install_extensions.py --check)
3. Retrieval integration (test_phase1_integration.py)
4. Extraction quality (extraction_eval.py)
5. Entity resolution accuracy (test_resolution.py)
6. Sectionizer correctness (test_sectionizer.py)
7. Retrieval + ranking quality (retrieval_eval.py)
8. Skills Factory trigger accuracy (skills_eval.py)

Report a single pass/fail with individual scores.
This is the gatekeeper for merging v2 to main.
```

🔀 Commit: `test: comprehensive regression suite`

---

## Development Workflow Reference

### Git conventions

```
# Branch naming
v2-substrate          Phase 1
v2-extraction         Phase 2-3
v2-live-docs          Phase 4-4b
v2-skills-factory     Phase 5
v2-main               Integration branch

# Commit message format
feat(phaseN): <description>
fix(phaseN): <description>
test(phaseN): <description>
docs: <description>
chore: <description>
```

### Autoresearch eval loop

The pattern from Karpathy's autoresearch, applied to extraction and Skills quality:

```
1. Establish baseline metrics (run eval, record scores)
2. Hypothesize a change (modify ONE thing — a prompt, a threshold, a weight)
3. Implement the change
4. Run eval on the SAME golden set
5. If metric improved:
   - git commit with the change
   - Record new baseline
   - Log: "iteration N: changed X, metric Y → Z, KEPT"
6. If metric declined:
   - git revert
   - Log: "iteration N: changed X, metric Y → Z, REVERTED"
7. Repeat from step 2
```

Always log iterations to `logs/autoresearch_<component>_<date>.md`.

### Validation checklist (run before every commit)

```bash
# Quick smoke test
pytest tests/test_schema.py tests/test_resolution.py -v --tb=short

# Full regression (run before PR/merge)
pytest tests/ -v --tb=short

# Eval suite (run before prompt or weight changes)
python tests/eval/extraction_eval.py
python tests/eval/retrieval_eval.py
python tests/eval/skills_eval.py
```

### Using Context7 for API verification

Before writing code that calls DuckDB extensions, FastMCP, sentence-transformers,
or any other library:

```
use library /duckdb/duckdb — verify extension syntax
use library /jlowin/fastmcp — verify tool registration API
use library /upstash/context7 — verify MCP query patterns
```

This prevents writing code against stale training-data APIs.
