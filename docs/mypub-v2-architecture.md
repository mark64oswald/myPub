# myPub v2: Architecture and Design

**Status:** Design proposal (revised)
**Scope:** Evolution of the myPub knowledge base system to add automated concept and procedure extraction, a Skills Factory for generating Claude Skills packages on demand, and currency-aware retrieval that merges book content with live official documentation. Designed as a single-user system running locally with Claude Code as the agent harness.

---

## 1. Purpose

myPub transforms a collection of ~345 technical ePub books into an intelligent, queryable knowledge base for Claude. The v1 design preserves document structure, captures concept relationships manually, and loads full chapters into Claude's context rather than chunking them into embeddings.

v2 extends this foundation in four directions:

1. **Automated concept and procedure extraction** so the knowledge graph scales beyond manual curation.
2. **A Skills Factory** that generates coherent Claude Skills packages on demand (e.g., "create Skills for data engineering on Databricks").
3. **Currency-aware retrieval** that combines book content with live documentation from multiple sources — vendor and well-documented OSS docs via Context7, AI-generated architecture docs via DeepWiki for any public GitHub repo, and raw GitHub file fetching as a long-tail fallback.
4. **Multi-criteria ranking with two distinct modes** — interactive (surface conflicts) for Q&A, silent (resolve conflicts) for Skills generation.

The guiding principle is evolution, not revolution: keep what works in v1, close the automation gaps, add capabilities without introducing dependencies that could become liabilities. And critically: **single-user, local-first**. No cloud infrastructure unless and until multi-user or remote access becomes a real requirement.

---

## 2. Starting Point: Current myPub

### 2.1 What v1 Does Today

The current system indexes ePub books into a DuckDB catalog, curates a concept graph with prerequisite and cross-reference relationships, maintains a YAML-based pattern library, and exposes everything to Claude through Skills files and custom commands (`/kb-search`, `/kb-compare`, `/kb-prereqs`, `/kb-pattern`, `/kb-generate-skill`, `/kb-learning-path`). ePub access happens through an MCP server, keeping full chapters available for loading into context.

The philosophy is **native-first retrieval**: rather than chunking books into vector embeddings, the system preserves author structure (books → chapters → sections) and retrieves at the chapter level. Most chapters fit comfortably into Claude's context window.

### 2.2 Strengths Worth Preserving

- **Native-first retrieval.** Loading full chapters preserves narrative context that chunking destroys.
- **DuckDB as substrate.** Embedded, MIT-licensed, fast, increasingly capable with extensions.
- **Chapter-level granularity.** Matches how authors organize knowledge and how Skills want to be scoped.
- **Concept graph with typed relationships.** `REQUIRES`, `EXTENDS`, `CONTRASTS_WITH` capture intellectual structure.
- **Pattern library.** Reusable specs referenced rather than re-explained.
- **Skills + Commands + MCP integration.** Works with Claude Desktop and Claude Code.

### 2.3 Gaps Motivating v2

- **Manual concept curation doesn't scale** to 345 books.
- **No semantic or graph query capability.** The catalog is relational; "find chapters discussing X conceptually" requires keyword matches; "find prerequisite chains" requires recursive CTEs.
- **No procedure extraction.** Books teach concepts; Skills need actions.
- **No currency layer.** Books go stale. A 2022 Databricks book doesn't cover Lakeflow Connect.
- **Skills generated one at a time.** `/kb-generate-skill` produces individual Skills; no decomposition logic or package coherence.
- **No ranking or conflict surfacing.** When multiple sources disagree, v1 can't weight them or choose between them intelligently.

---

## 3. Design Principles

Seven commitments that shape every decision in v2:

1. **Single-user, local-first.** No cloud services unless and until they earn their cost. Claude Code with a Max subscription is the agent harness; everything runs on the user's Mac.
2. **Native-first retrieval preserved.** Full chapters into context whenever window size allows.
3. **Single substrate, multiple retrieval modes.** Keyword, semantic, and graph queries all run against the same DuckDB instance. No separate vector DB, no separate graph DB.
4. **Standards over proprietary.** SQL:2023 SQL/PGQ for graph, HNSW for vectors, BM25 for keyword. Open formats, no vendor lock.
5. **Provenance-first.** Every derived artifact (extracted concept, generated Skill, merged answer) traces to its sources including chapter, book, publication date, and doc snapshot.
6. **Multiple perspectives with explicit ranking — surfaced or silent depending on context.** Interactive Q&A exposes conflicts; Skills generation resolves them behind the scenes.
7. **Evolution, not revolution.** Keep v1 strengths. Add capabilities as new layers. Existing Skills and commands continue to work.

---

## 4. Architecture Overview

Five layers, all running locally. Claude Code orchestrates everything through its Skills, commands, and MCP support.

| Layer | Components |
|---|---|
| **5. Applications** | Claude Code (agent harness), Skills Factory, Pattern library |
| **4. Retrieval & Ranking** | Hybrid retriever, two-mode ranking engine, source merge |
| **3. DuckDB Substrate** | Local DuckDB file with FTS + VSS + DuckPGQ extensions |
| **2. Extraction** | ePub parser, entity extractor, procedure extractor, doc snapshot cache |
| **1. Sources** | Local ePub collection, Context7 + DeepWiki + GitHub MCPs for live docs |

The essential shape: Claude Code at the top, MCP servers (KB + three doc sources) in the middle, local DuckDB and ePub files at the bottom. Almost everything on your Mac, everything in your control, zero incremental API cost. DeepWiki is the one exception — it's a hosted free HTTPS service, no auth required for public repos.

---

## 5. Component Specifications

### 5.1 Sources

#### ePub Collection *(existing, unchanged)*

The corpus in `~/Documents/eBooks`. Each book has metadata (title, author, publication date, publisher), a table of contents, and structured XHTML content for each chapter.

**Purpose:** Depth, author perspective, pedagogical sequencing. Books organize topics into curricula that decades of author experience have refined.

#### Live Documentation Sources *(new)*

Three complementary services, all reachable via MCP servers. They answer different questions and degrade gracefully as you move down the list.

**Context7 MCP** *(primary source)* — Upstash's documentation index. Supports four source types: Git repositories, websites, OpenAPI specs, and `llms.txt` files. For Git repos, it parses Markdown files and documentation folders using an optional `context7.json` config for fine-grained control. Covers vendor docs (Databricks, PostgreSQL, AWS) and well-documented OSS libraries (LangChain, FastMCP, Prisma). Runs as a local stdio MCP via `npx`. Public libraries can be self-submitted via context7.com/add-library if not already indexed. Returns content already pre-chunked; each chunk becomes a `doc_section` on ingestion.

**DeepWiki MCP** *(complementary source)* — Cognition's AI-generated documentation layer for any public GitHub repo. 30,000+ repos indexed. Hosted HTTPS MCP at `mcp.deepwiki.com/mcp`, free for public repos, no auth required. Exposes `read_wiki_structure`, `read_wiki_contents`, and `ask_question` tools. Distinct from Context7 in what it answers: Context7 gives you *what the library's own docs say*; DeepWiki gives you *what the library actually does*, synthesized from source code. For OSS with thin or no README, DeepWiki often has more to offer. Each wiki page becomes a `doc_snapshot`; its internal subsections become `doc_section` rows using the structure returned by `read_wiki_structure`.

**GitHub MCP** *(long-tail fallback)* — official GitHub MCP server. Fetches raw file contents, lists directories, searches code. For the rare repo neither Context7 nor DeepWiki has indexed, GitHub MCP can pull the README and `/docs/*.md` directly. Less polished (no chunking, no synthesis), but universal. Each Markdown file is parsed on ingestion — the heading tree (`#`, `##`, `###`) produces a section hierarchy that matches the structure books already have via chapters and sections. When a README has no headings at all (shapeless blob), it falls back to a single `doc_section` covering the whole file.

**Unified retrieval granularity.** Across all three sources, the retrievable unit is a `doc_section` — a coherent piece of text with its own embedding, FTS entry, and typed concept references. This mirrors the chapter-level granularity books already have, so the hybrid retriever treats book chapters and doc sections symmetrically. A section's provenance traces to its parent snapshot and source, so citations remain specific (e.g., *"Zippy docs, Schema Evolution section"* rather than *"the Zippy README"*).

**Purpose:** Addresses the currency gap at breadth and depth. Books teach foundational concepts; the three doc sources together cover the current state of nearly every technology in active use — vendor products, well-documented OSS, and obscure libraries alike.

**Division of labor:**

- Most vendor and popular OSS queries → Context7.
- OSS queries where Context7 isn't indexed, or where architectural/intent grounding matters → DeepWiki.
- Everything else → GitHub MCP direct fetch.

**Integration:**

- All three run as MCP servers reachable from Claude Code. Context7 and GitHub stdio locally, DeepWiki via its hosted HTTPS endpoint.
- Invoked on-demand when a query touches a concept linked to a doc source, and proactively pre-refreshed on a schedule (§6.6) to minimize first-query latency.
- Snapshots from all three cached in DuckDB with timestamps and source-type tags to enable deterministic retrieval and source-change tracking.
- Per-concept configuration via `concept_doc_link` specifies which doc sources to consult; a concept can link to multiple sources and the ranking engine merges across them.

### 5.2 Extraction

#### ePub Parser *(existing, unchanged)*

Parses ePub files into the relational catalog. Captures book metadata, chapter hierarchy, section structure, and full chapter text.

#### Entity/Concept Extractor *(new)*

LLM-based schema-guided extraction applied to any text-bearing content — both ePub chapters **and** doc snapshots (Context7, DeepWiki, GitHub). The extractor is given a domain ontology (allowed entity types and relationship types) and returns structured entities and relations written into the graph tables.

**Purpose:** Automate what v1 did manually. Extracts `Concept`, `Pattern`, `Tool`, `Author` and other domain entities, plus typed relationships (`REQUIRES`, `EXTENDS`, `CONTRASTS_WITH`, `IMPLEMENTS`, `CITES`).

**Design notes:**

- Adopts the pattern used by LlamaIndex's `SchemaLLMPathExtractor` but implemented directly against DuckDB rather than through the LlamaIndex framework.
- **Runs via Claude Code sub-agents (Task tool), not as a standalone API-calling script.** A coordinator script handles I/O, DB reads/writes, and entity resolution; sub-agents handle the LLM reasoning. This keeps all LLM costs covered by the Max subscription. The coordinator dispatches chapters or doc sections in batches to sub-agents, which return structured JSON.
- Applies uniformly to chapters and snapshots — the same schema and prompts. Differences in document style (book prose vs. reference docs vs. README) are handled by the LLM, not by separate pipelines.
- **Every extracted candidate entity passes through Entity Resolution (below) before being written.** This is what keeps the graph cohesive across heterogeneous sources.
- Produces provenance: every extracted entity or relation links back to the chapter-and-paragraph or snapshot it was derived from, via a polymorphic `(source_type, source_id)` reference.
- Critical consequence: concepts can enter the graph from live docs even when no book mentions them. A new library on DeepWiki or a new vendor feature on Context7 becomes a first-class, traversable, Skills-Factory-eligible concept on next refresh.

#### Procedure Extractor *(new)*

A specialized extractor that pulls step-by-step procedures, decision rules, and command examples out of text. Applies to both ePub chapters (which mix procedures with explanatory prose) and doc snapshots (which often contain procedures explicitly).

**Purpose:** Supplies the action-oriented content Skills need. A `Procedure` entity captures pre-conditions, ordered steps, expected outcomes, and failure modes.

**Design notes:**

- Separate prompt template from the entity extractor because the extraction target is different.
- **Uses the same sub-agent pattern as the entity extractor** — coordinator script handles I/O; sub-agents handle LLM reasoning. Subscription-covered.
- Procedures link to the concepts they operate on and the patterns they implement — those concept references pass through Entity Resolution, same as entity extraction, so a Procedure extracted from Zippy's docs links to the same `CDC` concept that book chapters discuss.
- Works well against Context7 and DeepWiki content (often rich in worked examples). Works best-effort against raw GitHub READMEs — some are procedural, many aren't.
- Not every chapter or snapshot has procedures; the extractor no-ops gracefully.
- Like the entity extractor, uses `(source_type, source_id)` provenance so a procedure can trace back to a chapter or a snapshot.

#### Entity Resolution *(new)*

The load-bearing mechanism that makes books and live docs converge on a single concept graph rather than fragment into parallel silos. Called by both the Entity/Concept Extractor and the Procedure Extractor before any concept reference is written.

**Problem.** Every extractor pass produces candidate concept names. "Change Data Capture" from a Kleppmann chapter, "change data capture" from a Databricks doc, "CDC" from the Zippy README — these are the same concept and need to resolve to the same node. If the extractor writes them as separate nodes, the graph fragments, retrieval misses cross-cutting evidence, and Skills Factory synthesis falls apart. Entity resolution is the step that prevents this.

**Mechanism.** Three-stage match against the existing concept set:

1. **Exact name match.** Case-insensitive lookup on `concept.name`. If the candidate matches an existing name exactly, resolve to that concept.
2. **Alias match.** Look up the candidate in the `concept_alias` table (schema below). Handles common abbreviations (CDC → Change Data Capture), casing variants, and explicit synonyms (microservices ↔ microservice architecture).
3. **Embedding similarity match.** Compute embedding of the candidate name plus its extraction context, find nearest existing concept by cosine similarity:
   - `≥ 0.90` — auto-match, resolve to the existing concept; optionally register the candidate as a new alias.
   - `0.75 – 0.89` — borderline; enqueue to `concept_resolution_queue` for human review. Provisionally create a new concept node but mark it `pending_review` so downstream code can treat it cautiously.
   - `< 0.75` — genuinely new concept; create new node.

Thresholds are starting points to tune against the evaluation set.

**Schema additions:**

```sql
CREATE TABLE concept_alias (
    alias_id   BIGINT PRIMARY KEY,
    concept_id BIGINT REFERENCES concept(concept_id),
    alias      VARCHAR NOT NULL,
    alias_type VARCHAR,         -- 'abbreviation', 'synonym', 'casing', 'plural'
    UNIQUE (concept_id, alias)
);

CREATE TABLE concept_resolution_queue (
    queue_id           BIGINT PRIMARY KEY,
    candidate_name     VARCHAR,
    candidate_context  TEXT,          -- surrounding text for disambiguation
    source_type        VARCHAR,       -- 'chapter' | 'doc_snapshot'
    source_id          BIGINT,
    nearest_concept_id BIGINT REFERENCES concept(concept_id),
    similarity_score   DOUBLE,
    resolution_action  VARCHAR,       -- 'merge', 'keep_separate', 'alias', 'rename', 'pending'
    reviewed_at        TIMESTAMP,
    created_at         TIMESTAMP
);

-- Optional index:
-- CREATE INDEX idx_concept_alias_alias ON concept_alias(alias);
```

**Human review workflow.** The `concept_resolution_queue` is surfaced via `/kb-review-concepts`. For each borderline item the user sees the candidate name, its extraction context, the nearest existing concept, and the similarity score. Actions:

- **Merge** — treat the candidate as the existing concept; delete the provisional node; rewrite any edges to point at the existing concept; optionally register the candidate surface form as an alias.
- **Alias** — keep the existing concept; register the candidate as an alias for future matching without re-routing existing edges.
- **Keep separate** — confirm it's a genuinely distinct concept; clear the `pending_review` flag on the provisional node.
- **Rename** — the extractor produced a poor name (e.g., "CDC stream" when the real concept is "change stream"); rename and resolve against the corrected target.

**Polysemy handling.** Same term, different meanings in different domains — "transaction" means very different things in database chapters vs. blockchain chapters. Context embedding catches most of this because the surrounding vectors differ, but for known cases the resolution can be scoped by `concept_type` (e.g., "Transaction (Pattern)" vs. "Transaction (Protocol)"). Polysemy edge cases get flagged to the review queue automatically when similarity is high but the candidate types differ.

**Why this enables synthesis.** This is the step that makes the Zippy scenario work. When Zippy's README mentions "change data capture" and the extractor proposes a new concept, embedding similarity against the existing `CDC` node comes back at ~0.94, the resolution auto-matches, and the Zippy snapshot's `DISCUSSES` edge points at the *same* CDC node that five books already discuss. The graph stays cohesive; the Skills Factory has one unified view; Q&A can pull book-based CDC fundamentals and Zippy-specific current details from the same traversal.

Without this step — or with it tuned too loose — the graph fragments. Tuned too tight, distinct concepts get collapsed. Getting it right is an evaluation-driven iteration, not a one-shot design decision, which is why the review queue exists.

#### Doc Snapshot Cache *(new)*

Stores point-in-time captures of documentation from all live doc sources (Context7, DeepWiki, GitHub) with content hashes, retrieval timestamps, source type, and source URLs. Written by the doc-source integrations, queried by the ranking engine **and by the extractors**.

**Purpose:** Enables deterministic retrieval during a session across mixed source types, detects when upstream docs change, and provides the artifact against which book content is compared for currency scoring. Snapshot source type is preserved so the ranking engine can weight them differently (e.g., an official Context7 snapshot ranks higher on authority than a DeepWiki AI-generated summary for the same concept).

**Section parsing.** The raw snapshot content is retained as the authoritative artifact, but retrieval and extraction happen at the **section** level — a new `doc_section` table mirrors the role that book `section` plays for ePubs. Ingestion walks the snapshot's native structure (Context7's pre-chunked content, DeepWiki's page outline via `read_wiki_structure`, or a Markdown heading tree for raw GitHub files) and creates one `doc_section` per coherent unit. Each section gets its own embedding and its own FTS index entry. This gives docs the same retrieval granularity books have — the hybrid retriever sees no structural difference between a book chapter and a doc section.

**Extraction trigger:** When a snapshot refresh results in a new `content_hash`, the extractors are invoked against its sections. Unchanged snapshots skip re-extraction. This is how the concept and procedure graphs stay current without wholesale re-extraction — only what actually changed gets processed.

### 5.3 DuckDB Substrate

A single local DuckDB file — `data/catalog.ddb` — handling all three retrieval modalities plus the relational catalog. No separate vector DB, no separate graph DB.

#### Relational Catalog *(existing, evolved)*

Tables for `book`, `chapter`, `section`, `author`, `concept`, `pattern`, plus new v2 tables: `procedure`, `doc_source`, `doc_snapshot`, `doc_section`, `concept_alias`, `concept_resolution_queue`, `concept_query_log`, `skill_package`, `skill`, `skill_file`, `skill_source`, `skill_relation`.

#### FTS Extension *(new)*

DuckDB's full-text search extension. Inverted indexes with BM25 scoring over chapter text, Porter stemming, English stopword handling.

**Purpose:** Fast keyword search. Best for exact-term queries.

#### VSS Extension *(new)*

Vector Similarity Search with HNSW indexes over embedding vectors stored in `ARRAY` columns.

**Purpose:** Semantic search. Finds conceptually related content without keyword overlap.

**Design notes:**

- Embeddings generated via sentence-transformers, stored alongside chapter and concept tables.
- HNSW persistence flagged experimental in DuckDB; if unstable, fall back to in-memory indexes rebuilt on KB MCP server startup.

#### DuckPGQ Extension *(new)*

Property graph queries via SQL/PGQ (SQL:2023 standard). Vertex and edge tables defined over existing relational tables; no data duplication.

**Purpose:** Native graph traversal. Makes the concept graph a first-class citizen with standard graph algorithms (shortest paths, pattern matching, community detection).

**Examples enabled:**

- `ANY SHORTEST PATH` from a beginner concept to an advanced one → natural prerequisite ordering for learning paths.
- Pattern match for `(chapter)-[DISCUSSES]->(concept)<-[DISCUSSES]-(chapter)` → cross-author comparisons.
- Community detection over the concept graph → domain clustering for Skills Factory decomposition.

### 5.4 Retrieval & Ranking

#### Hybrid Retriever *(new)*

Fans a query out to all three retrieval modalities in parallel and merges candidate sets. Different query shapes use different modality weightings.

**Purpose:** No single retrieval modality is best for all queries. The hybrid retriever is modality-agnostic from the caller's perspective.

#### Ranking Engine — Two Modes *(new)*

Multi-criteria scoring with two distinct operating modes for different consumers. Detailed in §8.

- **Interactive mode** (KB assistant): surface conflicts with scores, let the agent reason about them with the user.
- **Generation mode** (Skills Factory): rank silently, apply a selection strategy, emit clean consolidated output.

#### Source Merge Layer *(new)*

Combines ranked results from books and doc snapshots. Behavior differs by mode:

- **Interactive:** presents multiple perspectives with explicit scores and conflict flags.
- **Generation:** applies the selected strategy (§8.3) and produces consolidated content with silent provenance.

#### Auto-Discovery *(new)*

When the hybrid retriever encounters a query term that doesn't resolve to any existing concept or doc source, auto-discovery probes the live doc source stack to find and ingest relevant content inline. This is how the knowledge base grows organically from actual use — you don't have to know in advance which technologies you'll ask about.

**Mechanism:**

1. **Concept gap detection.** After hybrid retrieval, identify query terms with no FTS match, no VSS match above threshold, and no resolution match. These are candidate unknown terms.
2. **Live source probe.** For each candidate, try the doc source stack in priority order:
   - Context7 `resolve-library-id` — covers both documentation sites (Databricks, AWS, PostgreSQL) and indexed OSS repos. Context7's library index spans thousands of technologies, so this catches vendor docs and popular OSS alike.
   - DeepWiki `read_wiki_structure` — covers any public GitHub repo, even those Context7 hasn't indexed. For OSS with thin or absent Context7 coverage.
   - GitHub MCP file search — last resort for repos neither service has processed.
3. **Confidence gate.** Only proceed if the probe returns a clear, unambiguous match. If multiple candidates match (e.g., "spark" matches Apache Spark, spark-nlp, and three other repos), the system does **not** guess — it asks the user: *"I found several possible sources for 'spark' — did you mean Apache Spark (data processing), spark-nlp (NLP library), or something else?"* The default for ambiguity is to ask, not to ingest. This keeps the knowledge base clean.
4. **Auto-register and ingest.** For a confident match:
   - Create `doc_source` with conservative defaults: `authority_score` = 0.60 (Context7) / 0.50 (DeepWiki) / 0.40 (GitHub raw) — lower than explicitly registered sources until the content proves its value.
   - Run the full snapshot ingestion pipeline (§6.2): fetch → persist → sectionize → embed → index → extract with resolution → alignment.
   - Notify the user: *"I just indexed Zippy's documentation. Let me search again with this new content..."*
5. **Re-run retrieval.** With the freshly-ingested content now in the corpus, re-run hybrid retrieval. The new doc sections participate in ranking alongside book chapters, weighted by their conservative authority score.

**What doesn't get ingested:** If the confidence gate fails (ambiguous match), or the probe returns nothing, or the probed content is too thin to extract meaningful concepts from (<100 words of actual content after sectionizing), the system tells the user what happened and moves on with book-only results. The knowledge base stays clean — no junk entries from speculative ingestion.

**Latency.** First-time discovery adds 10–30 seconds (dominated by extraction). The user sees a status message explaining what's happening. Every subsequent query about the same technology is instant. Auto-discovered sources participate in proactive refresh (§6.6) from that point forward, so they stay current.

**Growth trajectory.** Over weeks of use, the knowledge base grows to cover every technology you actually work with, without you ever having to configure it. Explicitly registered sources (your top 10–20, seeded during setup) provide high-authority anchors; auto-discovered sources fill in around them at lower authority, earning higher scores as you query them and the system validates their content.

### 5.5 Applications

#### Claude Code *(new — replaces v1 Claude Desktop setup)*

The agent harness. Runs locally on the user's Mac, authenticated with their Claude Max subscription so API token costs do not apply. Loads skills from the project's `.claude/skills/` directory, commands from `.claude/commands/`, project instructions from `CLAUDE.md`, and connects to local MCP servers.

**Purpose:** Interactive and agentic interface to the knowledge base and Skills Factory. Every v2 capability runs through Claude Code.

#### Skills Factory *(new)*

Generates coherent Claude Skills packages from the corpus on demand. Detailed in §6.3.

#### Pattern Library *(existing, unchanged)*

YAML-based reusable patterns with canonical implementations and variations. In v2, patterns are first-class graph citizens — Skills reference them rather than inlining content, and the entity extractor can identify when chapter content matches a pattern.

---

## 6. Key Workflows

### 6.1 Book Ingestion and Extraction

Invoked from Claude Code: `/kb-index [path]`. Scans the ePub collection (or a specific path), detects new, updated, and unchanged books, and only processes what's needed.

**Detection phase — for each ePub file in the target path:**

1. **Compute file content_hash** (SHA-256 of the ePub file).
2. **Look up by source_path** in the `book` table.
3. **Not found → new book.** Full pipeline (below).
4. **Found, hash matches → unchanged.** Skip entirely. This is the fast path for the vast majority of your corpus.
5. **Found, hash differs → updated book.** Typically an early release with new chapters, a revised edition, or a MEAP with content changes. Re-process with chapter-level diffing (below).

**Full pipeline for new books:**

1. **Parse ePub** — extract metadata, ToC, chapter XHTML into the catalog. Write `book.content_hash` and `book.last_indexed_at`.
2. **Compute chapter hashes** — store `chapter.content_hash` per chapter.
3. **Generate embeddings** for chapters; write to VSS-indexed columns.
4. **Build FTS index** over chapter text.
5. **Run entity/concept extractor** on each chapter via sub-agents. Each candidate entity passes through Entity Resolution (§5.2) — matched candidates attach to existing concepts; unmatched candidates become new nodes or go to the review queue. Write entities and relations with `source_type='chapter'`.
6. **Run procedure extractor** on each chapter via sub-agents; concept references resolve via the same mechanism. Write extracted procedures with `source_type='chapter'`.
7. **Link to doc sources** — for each concept (new or existing), identify applicable live doc sources (Context7 for vendor/indexed OSS, DeepWiki for any repo-backed concept, GitHub raw for long tail) and record linkages in `concept_doc_link`. Multiple sources per concept are expected.

**Incremental pipeline for updated books:**

1. **Re-parse ePub** — extract updated ToC and chapter XHTML. Compute `chapter.content_hash` for each chapter.
2. **Diff chapters** against stored hashes. Three outcomes per chapter:
   - **Unchanged** (hash matches) — skip. Existing embeddings, entities, procedures, and graph edges remain valid.
   - **New** (chapter number or title not previously present) — run the full chapter pipeline: embed → FTS → entity extraction → procedure extraction.
   - **Changed** (same chapter, different hash) — re-process:
     a. Re-generate the embedding for the updated content.
     b. Update the FTS index entry.
     c. **Delete stale extraction edges** — remove `concept_relation` and `procedure` rows where `source_type='chapter'` and `source_id=<this chapter>`. Don't delete the concepts themselves — other chapters or doc sources may also reference them.
     d. Re-run entity extraction via sub-agent. Entity Resolution re-links to existing concepts.
     e. Re-run procedure extraction via sub-agent.
   - **Deleted** (existed before, gone in updated ePub) — rare, but possible when early releases reorganize. Mark the chapter as superseded. Remove its extraction edges. Don't delete referenced concepts.
3. **Update `book.content_hash`** and `book.last_indexed_at`.
4. **Log summary**: N chapters unchanged, N new, N changed, N deleted.

**Example — early release update:**
You have *"Databricks Cookbook, Early Release"* indexed in March with 8 chapters. In June, you download an update with 12 chapters and revisions to chapters 3 and 7.

- Chapters 1, 2, 4, 5, 6, 8: hash unchanged → **skip** (zero work)
- Chapters 3, 7: hash changed → **re-extract** (~4 minutes)
- Chapters 9, 10, 11, 12: new → **full pipeline** (~8 minutes)
- Total: ~12 minutes, vs. ~25 minutes for a full re-index of all 12 chapters

**Edition replacement:**
When you replace a 1st edition with a 2nd edition (different ePub file, different ISBN), the system sees it as a new book since the `source_path` differs. Both editions coexist in the catalog. To retire the old edition, use `/kb-retire-book <path>` — this marks the book as superseded and drops its weight in ranking without deleting its graph contributions. Concepts introduced by the retired edition remain in the graph if other sources also reference them.

### 6.2 Snapshot Ingestion and Extraction

Parallel to book ingestion, but triggered by doc source refreshes rather than corpus changes. Runs per-snapshot when a refresh produces a new `content_hash`:

1. **Fetch snapshot** — call the appropriate MCP server (Context7, DeepWiki, or GitHub) for a configured `doc_source` + `identifier`, compute `content_hash`.
2. **Skip if unchanged** — if `content_hash` matches the latest stored snapshot, update `last_refresh_at` on the `doc_source` and return. No re-extraction, no re-embedding. This is the common path (~90% of refreshes) and it's nearly free.
3. **Persist snapshot** — write a new `doc_snapshot` row with full content, URL, timestamp, and source type. Mark the previous snapshot as superseded.
4. **Parse into sections** — split the snapshot into `doc_section` rows:
   - **Context7** content is already pre-chunked; each chunk becomes a section.
   - **DeepWiki** pages use the structure returned by `read_wiki_structure`; each subsection becomes a row.
   - **GitHub Markdown** files are parsed with a Markdown tree walker (e.g., `markdown-it-py`); the heading hierarchy produces nested `doc_section` rows — by default `H2` and above start new sections, `H3+` content folds into its parent.
   - **Shapeless fallback** — documents with no headings below the title produce a single `doc_section` covering the whole snapshot. Retrieval still works; it's just coarser.
5. **Generate embeddings per section** — each `doc_section.content` gets its own embedding written to the VSS index. The parent snapshot gets an embedding too (useful for snapshot-level queries), but section embeddings are the primary retrieval target.
6. **Add sections to FTS index** — each section indexes independently, so keyword matches point at the smallest meaningful unit.
7. **Run entity/concept extractor** on each section with the same domain schema. **Entity Resolution runs here** — candidate concepts from the section get matched against the existing graph (seeded by books) via exact name, alias, and embedding similarity. Zippy's "change data capture" resolves to the same `CDC` node that books already discuss. Truly new concepts (like `Zippy` itself) create new nodes. Write entities and relations with `source_type='doc_section'`.
8. **Run procedure extractor** on each section; concept references resolve via the same mechanism. Write extracted procedures with `source_type='doc_section'`.
9. **Compute alignment** — for each concept a section discusses (now unified via resolution), compare against existing book chapters on the same concept. Record `CORROBORATES` or `CONTRADICTS` edges at section granularity.

Triggered four ways (see §6.6 for the proactive scheduling layer):

- **On-demand** from Q&A flow when a query touches a concept whose snapshot is beyond its `refresh_ttl_days`.
- **Proactively scheduled** via a LaunchAgent running nightly, refreshing sources in priority tiers (Hot daily, Warm weekly, Cool monthly).
- **Before Skills Factory runs**, for all concepts in scope of the target package.
- **Manually** via `/kb-refresh-docs [domain]` slash command.

The key property: **books enter the graph once; snapshots enter the graph repeatedly as they change.** Both use the same extractors, same schema, same graph tables, same section-level retrieval granularity.

### 6.3 Interactive Query Flow (Interactive Mode)

The flow for KB assistant queries — surfaces conflicts, lets Claude reason with the user. **Both ePub chapters and doc snapshots are first-class retrieval targets** — live docs don't only feed the Skills Factory, they answer real-time user questions too:

1. **Accept query + current context** — user question plus any prior conversation context.
2. **Hybrid retrieval** — parallel fan-out to FTS, VSS, DuckPGQ across the unified corpus of chapters and snapshots. A VSS hit on a recent Context7 snapshot is weighted the same, in principle, as a VSS hit on a chapter — the ranking stage handles source-type weighting.
3. **Auto-discovery check** — if the query contains terms that produced no matches across any modality, trigger the auto-discovery probe (§5.4). If a confident match is found, ingest inline and re-run retrieval. If ambiguous, ask the user to disambiguate. If nothing found, proceed with book-only results. The user sees a status message during discovery: *"I don't have Zippy in my knowledge base yet — let me pull its docs and index them..."*
4. **Opportunistic refresh** — for concept hits with doc sources whose latest snapshot is beyond TTL, trigger an async refresh (§6.2). Use the currently-cached snapshot for this query; the refresh benefits the next one.
5. **Score candidates** — ranking engine runs in interactive mode (§8.1).
6. **Merge with conflict surfacing** — return a structured result object with `primary`, `corroborations`, and `conflicts` fields.
7. **Return context package** — ranked, cited, annotated context ready for Claude to synthesize into a nuanced response.

### 6.4 Skills Factory Pipeline (Generation Mode)

Generates a complete Skills package from a domain request. Ranking runs silently throughout; the output is clean.

1. **Parse request** — extract domain, scope hints, target audience.
2. **Decompose domain** — use DuckPGQ community detection over the concept graph (populated from both books and snapshots, so OSS-only concepts are first-class), cross-reference with book ToCs, refine with an LLM pass. Output: proposed list of Skill topics.
3. **Plan package** — determine Skill boundaries, prerequisite relationships, shared patterns to reference, folder structure.
4. **Pre-refresh doc snapshots** — proactively refresh any stale snapshots for concepts in scope of the package, so generation uses current content.
5. **Select strategy per Skill** — choose source-selection strategy (recent-doc anchored, consensus synthesis, or authority pick) based on the Skill's domain characteristics.
6. **Generate per Skill:**
   - Retrieve candidate sources via hybrid retriever scoped to Skill concepts.
   - Rank candidates silently (generation mode).
   - Apply selection strategy (§8.3).
   - Resolve or drop conflicts per §8.4.
   - Generate SKILL.md content using package-aware prompts (sees all sibling Skills).
   - Generate trigger descriptions with discrimination against siblings.
   - Generate supporting files (examples, reference tables, cheatsheets).
   - Record provenance in `skill_source` (including dropped sources with reasons).
7. **Validate** — coherence checks across Skills, trigger-accuracy tests, source currency flags, human-review flags for unresolvable conflicts.
8. **Materialize** — write folder structure to disk with SKILL.md files, supporting files, and `_package.md`.

### 6.5 Currency and Source Merge

When a query touches a technology with both book and doc sources:

1. **Identify affected concepts** from query + initial retrieval.
2. **Resolve doc sources** — for each concept, look up all linked doc sources via `concept_doc_link`. A single concept may be linked to Context7, DeepWiki, and/or GitHub.
3. **Fetch or reuse snapshots** — per source, cached if within TTL, otherwise refresh. Sources are queried in priority order (Context7 → DeepWiki → GitHub) but all available results feed into ranking.
4. **Compute alignment scores** — for each book passage, compare to the doc snapshots. High alignment = corroboration, low alignment = potential currency concern. Cross-source corroboration (e.g., Context7 and DeepWiki agree) is a strong confidence signal.
5. **Annotate** — passages with low alignment get a `source_currency` flag; passages with conflicts between doc sources get a `doc_disagreement` flag.
6. **Handle per mode:**
   - **Interactive:** surface alignment and flags to Claude for discussion with user, including when doc sources themselves disagree.
   - **Generation:** if `recent-doc anchored`, the highest-authority doc source wins on factual conflict; book content with low alignment is dropped from the Skill silently.

### 6.6 Proactive Refresh and Priority Tiers

On-demand snapshot refresh is correct for cold misses but makes first queries slow. A 5000-word README fetched and extracted from scratch takes 10–30 seconds, dominated by the LLM-driven extraction step. To keep interactive latency low, doc sources are refreshed proactively on a schedule, with priority driven by how likely the user is to actually need the content.

**Priority signals.** Each `doc_source` gets a `priority_tier` assigned automatically from these inputs:

- **Query frequency** — how often the concepts linked to this source have been queried recently (30-day window).
- **Graph centrality** — how many `DISCUSSES` and `REQUIRES` edges touch the linked concepts; foundational concepts with many incoming edges score higher.
- **Project focus** — concepts in the user's currently-focused domain, set via `/kb-focus <domain>`, get boosted.
- **Source volatility** — tracked over time as the fraction of refreshes that actually changed `content_hash`. Slow-changing sources don't need aggressive refresh.
- **User pin** — `doc_source.pinned = TRUE` forces Hot tier regardless of other signals.

**Refresh tiers.**

| Tier | Cadence | Typical members |
|---|---|---|
| **Hot** | Daily | Currently-focused project; most-queried last 30d; user-pinned sources |
| **Warm** | Weekly | Frequently-referenced concepts; high graph centrality; vendor docs for regular use |
| **Cool** | Monthly | Everything else with a registered doc source |
| **Cold** | On-demand only | Sources linked to concepts that have never been queried |

Sources move between tiers automatically based on rolling query frequency. The user can manually lock a source to Hot via `/kb-pin-source <name>`, or elevate an entire domain via `/kb-focus <domain>` for the duration of a project.

**Execution mechanism.** Claude Code doesn't run continuously, so proactive refresh lives outside it. On macOS, a user-level **LaunchAgent** runs `scripts/refresh_docs.py` nightly at 3 AM:

```xml
<!-- ~/Library/LaunchAgents/com.mypub.refresh.plist -->
<plist version="1.0">
<dict>
    <key>Label</key><string>com.mypub.refresh</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/markoswald/projects/mypub/.venv/bin/python</string>
        <string>/Users/markoswald/projects/mypub/scripts/refresh_docs.py</string>
        <string>--tier=auto</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>/Users/markoswald/projects/mypub/logs/refresh.log</string>
</dict>
</plist>
```

The script:

1. Queries `doc_source` with priority data to determine which sources are due (Hot every run, Warm on Wednesdays, Cool on the 1st of the month).
2. For each scheduled source: fetches snapshot → computes `content_hash` → compares to stored.
3. **Unchanged → no-op.** This is the critical efficiency: typical churn is ~10%, so most refreshes finish in milliseconds and cost nothing.
4. **Changed → runs the non-LLM ingestion steps**: parse into sections → generate embeddings → add to FTS index. These are pure Python operations that run without Claude Code. The LLM-dependent steps (entity extraction, procedure extraction, alignment computation) are **deferred** — flagged as pending and run on the next interactive Claude Code session when the user queries a concept linked to the changed source. This keeps the LaunchAgent self-contained (no API keys, no subscription dependency) while still pre-warming the most expensive steps (fetch + embed + index).
5. Writes a summary to `logs/refresh.log`: sources checked, sources changed, sections added/updated, extraction pending flags.

**Cost profile.** With ~50 registered doc sources across the tiers, realistic workload is roughly:

- Hot tier (~5 sources, daily): 1–2 content-changed refreshes per night, a few seconds each for the non-LLM steps.
- Warm tier (~15 sources, weekly): 2–5 content-changed refreshes per week.
- Cool tier (~30 sources, monthly): ~5 per month.

Total: under a minute of compute per night for the LaunchAgent. Deferred extraction adds 10–30 seconds per changed source on first interactive use — but only for sources that actually changed, which is a small fraction.

**User-facing commands** (see §9.3 for full list):

- `/kb-focus <domain>` — elevate a domain's sources to Hot for the duration of a project.
- `/kb-pin-source <name>` / `/kb-unpin-source <name>` — lock or release individual sources.
- `/kb-refresh-status` — show what's fresh, what's stale, when each tier refreshes next.
- `/kb-refresh-docs [domain]` — manual trigger, scoped by domain if specified.

---

## 7. Data Model

Core DuckDB tables plus a DuckPGQ property graph layered over them.

### 7.1 Core Tables (simplified)

```sql
-- Existing in v1 (with v2 additions)
CREATE TABLE book (
    book_id          BIGINT PRIMARY KEY,
    title            VARCHAR NOT NULL,
    author_id        BIGINT,
    publisher        VARCHAR,
    publication_date DATE,
    source_path      VARCHAR,
    content_hash     VARCHAR,       -- NEW: SHA-256 of ePub file, for change detection
    last_indexed_at  TIMESTAMP,     -- NEW: when this book was last fully processed
    status           VARCHAR DEFAULT 'active'  -- NEW: 'active' | 'superseded' (for retired editions)
);

CREATE TABLE chapter (
    chapter_id   BIGINT PRIMARY KEY,
    book_id      BIGINT REFERENCES book(book_id),
    chapter_num  INTEGER,
    title        VARCHAR,
    content      TEXT,
    content_hash VARCHAR,          -- NEW: hash of chapter content, for incremental re-indexing
    embedding    FLOAT[384]        -- NEW: for VSS
);

CREATE TABLE concept (
    concept_id   BIGINT PRIMARY KEY,
    name         VARCHAR NOT NULL,
    concept_type VARCHAR,     -- Pattern, Technique, Tool, etc.
    description  TEXT,
    embedding    FLOAT[384]   -- NEW
);

CREATE TABLE concept_relation (
    from_concept_id BIGINT REFERENCES concept(concept_id),
    to_concept_id   BIGINT REFERENCES concept(concept_id),
    relation_type   VARCHAR,
    confidence      DOUBLE,
    source_type     VARCHAR,       -- 'chapter' | 'doc_section' | 'doc_snapshot'
    source_id       BIGINT,        -- chapter_id, doc_section_id, or snapshot_id
    PRIMARY KEY (from_concept_id, to_concept_id, relation_type, source_type, source_id)
);

-- NEW in v2: procedures and currency
CREATE TABLE procedure (
    procedure_id       BIGINT PRIMARY KEY,
    name               VARCHAR,
    preconditions      TEXT,
    steps              TEXT,          -- JSON array of step objects
    postconditions     TEXT,
    failure_modes      TEXT,
    source_type        VARCHAR,       -- 'chapter' | 'doc_section' | 'doc_snapshot'
    source_id          BIGINT,        -- chapter_id, doc_section_id, or snapshot_id
    implements_pattern BIGINT
);

-- Optional index to keep the polymorphic lookups fast:
-- CREATE INDEX idx_procedure_source   ON procedure(source_type, source_id);
-- CREATE INDEX idx_conceptrel_source  ON concept_relation(source_type, source_id);

CREATE TABLE doc_source (
    doc_source_id           BIGINT PRIMARY KEY,
    name                    VARCHAR,    -- e.g., "Databricks docs (Context7)", "FastMCP (DeepWiki)"
    source_type             VARCHAR,    -- 'context7', 'deepwiki', 'github_raw'
    mcp_server              VARCHAR,    -- which MCP server to invoke
    identifier              VARCHAR,    -- library ID (Context7), owner/repo (DeepWiki, GitHub), etc.
    authority_score         DOUBLE,     -- 0-1, used in ranking (Context7 > DeepWiki > github_raw)
    refresh_ttl_days        INTEGER,    -- baseline TTL; priority tier may refresh sooner
    priority_tier           VARCHAR,    -- 'hot' | 'warm' | 'cool' | 'cold' (assigned by tiering job)
    pinned                  BOOLEAN DEFAULT FALSE,   -- user-locked to Hot
    last_refresh_at         TIMESTAMP,
    last_content_changed_at TIMESTAMP   -- tracks volatility; used to tune tier assignment
);

-- Example:
-- INSERT INTO doc_source VALUES
--   (1, 'Databricks docs',    'context7', 'context7',  '/databricks/docs',   0.95, 30, 'hot',  true,  ...),
--   (2, 'FastMCP repo',       'context7', 'context7',  '/prefecthq/fastmcp', 0.90, 14, 'warm', false, ...),
--   (3, 'FastMCP architecture','deepwiki','deepwiki',  'PrefectHQ/fastmcp',  0.75, 30, 'warm', false, ...),
--   (4, 'LangGraph repo',     'github',   'github-mcp','langchain-ai/langgraph', 0.65, 7, 'cool', false, ...);

CREATE TABLE doc_snapshot (
    snapshot_id      BIGINT PRIMARY KEY,
    doc_source_id    BIGINT REFERENCES doc_source(doc_source_id),
    source_type      VARCHAR,   -- denormalized for fast filtering
    url              VARCHAR,
    retrieved_at     TIMESTAMP,
    content_hash     VARCHAR,
    content          TEXT,      -- authoritative raw text; retrieval uses sections below
    embedding        FLOAT[384] -- snapshot-level embedding (coarse queries); section embeddings are primary
);

-- NEW in v2: section-level structure for doc snapshots, analogous to book sections
CREATE TABLE doc_section (
    doc_section_id  BIGINT PRIMARY KEY,
    snapshot_id     BIGINT REFERENCES doc_snapshot(snapshot_id),
    parent_id       BIGINT REFERENCES doc_section(doc_section_id), -- nested headings
    heading_level   INTEGER,       -- 1 for H1, 2 for H2, etc.
    heading_text    VARCHAR,       -- e.g., "Schema Evolution"
    ordinal         INTEGER,       -- position within snapshot
    content         TEXT,          -- the section's own text (not including descendant sections)
    embedding       FLOAT[384]     -- primary retrieval embedding
);

-- Optional indexes:
-- CREATE INDEX idx_doc_section_snapshot ON doc_section(snapshot_id);
-- CREATE INDEX idx_doc_section_parent   ON doc_section(parent_id);

CREATE TABLE concept_doc_link (
    concept_id    BIGINT REFERENCES concept(concept_id),
    doc_source_id BIGINT REFERENCES doc_source(doc_source_id),
    PRIMARY KEY (concept_id, doc_source_id)
);

-- NEW in v2: query logging for tier assignment (supports proactive refresh §6.6)
CREATE TABLE concept_query_log (
    log_id      BIGINT PRIMARY KEY,
    concept_id  BIGINT REFERENCES concept(concept_id),
    queried_at  TIMESTAMP,
    mode        VARCHAR     -- 'interactive' | 'generation'
);

-- Concept gains rolling frequency counters (nightly-updated from the log)
-- ALTER TABLE concept ADD COLUMN query_count     BIGINT DEFAULT 0;
-- ALTER TABLE concept ADD COLUMN last_queried_at TIMESTAMP;

-- NEW in v2: auto-discovery event tracking (supports confidence gate tuning)
CREATE TABLE discovery_log (
    log_id          BIGINT PRIMARY KEY,
    query_term      VARCHAR,       -- the unresolved term that triggered discovery
    probe_source    VARCHAR,       -- 'context7', 'deepwiki', 'github'
    probe_result    VARCHAR,       -- 'match', 'ambiguous', 'not_found'
    match_count     INTEGER,       -- number of candidates returned
    top_match_name  VARCHAR,       -- best candidate name (if any)
    top_match_score DOUBLE,        -- confidence score from the probe
    action_taken    VARCHAR,       -- 'ingested', 'asked_user', 'discarded'
    doc_source_id   BIGINT,        -- FK to doc_source if ingested, NULL otherwise
    created_at      TIMESTAMP
);

-- NEW in v2: Skills Factory
CREATE TABLE skill_package (
    package_id    BIGINT PRIMARY KEY,
    name          VARCHAR,
    domain        VARCHAR,
    root_topic    VARCHAR,
    created_at    TIMESTAMP,
    source_query  TEXT
);

CREATE TABLE skill (
    skill_id         BIGINT PRIMARY KEY,
    package_id       BIGINT REFERENCES skill_package(package_id),
    name             VARCHAR,
    description      TEXT,     -- the critical trigger description
    scope_summary    TEXT,
    content_markdown TEXT,
    source_currency  VARCHAR,  -- 'current', 'recent', 'dated', 'stale'
    strategy         VARCHAR,  -- 'recent-doc', 'consensus', 'authority'
    generation_notes TEXT      -- human-review flags, conflict notes
);

-- Provenance: every source considered, including dropped ones
CREATE TABLE skill_source (
    skill_id     BIGINT REFERENCES skill(skill_id),
    source_type  VARCHAR,      -- 'chapter', 'procedure', 'pattern', 'doc_snapshot'
    source_id    BIGINT,
    score        DOUBLE,       -- ranking score at generation time
    weight       DOUBLE,       -- 0 if dropped, else selection weight
    drop_reason  VARCHAR,      -- NULL if selected, reason string if dropped
    PRIMARY KEY (skill_id, source_type, source_id)
);

CREATE TABLE skill_file (
    file_id  BIGINT PRIMARY KEY,
    skill_id BIGINT REFERENCES skill(skill_id),
    filename VARCHAR,
    purpose  VARCHAR,          -- 'example', 'reference', 'template'
    content  TEXT
);

CREATE TABLE skill_relation (
    from_skill_id BIGINT REFERENCES skill(skill_id),
    to_skill_id   BIGINT REFERENCES skill(skill_id),
    relation_type VARCHAR,     -- 'REQUIRES', 'REFERENCES', 'EXTENDS'
    PRIMARY KEY (from_skill_id, to_skill_id, relation_type)
);
```

### 7.2 Property Graph Definition

```sql
CREATE PROPERTY GRAPH mypub
VERTEX TABLES (
    book          LABEL Book,
    chapter       LABEL Chapter,
    concept       LABEL Concept,
    procedure     LABEL Procedure,
    doc_snapshot  LABEL DocSnapshot,
    doc_section   LABEL DocSection,
    skill         LABEL Skill,
    skill_package LABEL SkillPackage
)
EDGE TABLES (
    concept_relation
        SOURCE KEY (from_concept_id) REFERENCES concept (concept_id)
        DESTINATION KEY (to_concept_id) REFERENCES concept (concept_id)
        LABEL relates_to,
    skill_relation
        SOURCE KEY (from_skill_id) REFERENCES skill (skill_id)
        DESTINATION KEY (to_skill_id) REFERENCES skill (skill_id)
        LABEL relates_to
    -- plus edge tables for CONTAINS (Book→Chapter, SkillPackage→Skill, DocSnapshot→DocSection),
    -- DISCUSSES (Chapter→Concept, DocSection→Concept — section-level extraction source),
    -- EXPLAINS (Chapter→Procedure, DocSection→Procedure — section-level extraction source),
    -- CORROBORATES / CONTRADICTS (DocSection→Chapter, DocSection→DocSection across sources),
    -- DERIVED_FROM (Skill→{Chapter, Procedure, Pattern, DocSection}).
);
```

### 7.3 Key Edge Types

| Edge | From → To | Purpose |
|---|---|---|
| `CONTAINS` | Book → Chapter | Structural (ePub hierarchy) |
| `CONTAINS` | DocSnapshot → DocSection | Structural (doc hierarchy) |
| `CONTAINS` | DocSection → DocSection | Nested headings (parent-child) |
| `DISCUSSES` | Chapter → Concept | Concept extracted from book content |
| `DISCUSSES` | DocSection → Concept | Concept extracted from a doc section |
| `EXPLAINS` | Chapter → Procedure | Procedure extracted from book content |
| `EXPLAINS` | DocSection → Procedure | Procedure extracted from a doc section |
| `REQUIRES` | Concept → Concept | Prerequisite relationship |
| `EXTENDS` | Concept → Concept | Advanced form of |
| `CONTRASTS_WITH` | Concept → Concept | Alternative approaches |
| `IMPLEMENTS` | Procedure → Pattern | Procedure realizes a pattern |
| `CORROBORATES` | DocSection → Chapter | Doc section and book chapter agree on a concept |
| `CONTRADICTS` | DocSection → Chapter | Doc section and book chapter disagree on a concept |
| `CORROBORATES` | DocSection → DocSection | Two doc sources agree (e.g., Context7 + DeepWiki) |
| `CONTRADICTS` | DocSection → DocSection | Two doc sources disagree |
| `DERIVED_FROM` | Skill → Chapter/Procedure/Pattern/DocSection | Skill provenance (any source type) |
| `REFERENCES` | Skill → Skill | Cross-Skill link |
| `CONTAINS` | SkillPackage → Skill | Package membership |

The `DISCUSSES` and `EXPLAINS` edges are the backbone of the unified extraction graph: they originate from either a book chapter or a doc section, and a single concept can accumulate dozens of such edges from both sources. This symmetric granularity — chapter on the book side, section on the doc side — is what gives retrieval and ranking their consistency: the ranking engine doesn't need to know whether a candidate came from a book or from a Zippy README, just how to score it. Books contribute depth, doc sections contribute breadth and currency, and the concept nodes they converge on are the same.

---

## 8. Ranking Engine: Two Modes

This is the piece that makes currency work in practice, and the two-mode design keeps generated Skills clean while preserving nuance for interactive queries.

### 8.1 Interactive Mode

For queries where the user is in the loop — KB assistant, `/kb-search`, `/kb-compare`. Surfaces conflicts, exposes scores, lets Claude reason openly.

Output shape:

```json
{
  "primary": {
    "source": "Databricks Lakehouse Platform Cookbook (2024)",
    "chapter": "Chapter 7: Delta Live Tables",
    "score": 0.84,
    "excerpt": "..."
  },
  "corroborations": [
    {
      "source": "Databricks docs (current)",
      "score": 0.82,
      "agreement": "high"
    }
  ],
  "conflicts": [
    {
      "source": "Spark: The Definitive Guide (2018)",
      "score": 0.71,
      "disagreement": "describes structured streaming pattern superseded by DLT",
      "currency_flag": "book is 7 years old; DLT didn't exist when written"
    }
  ]
}
```

Claude synthesizes with nuance: "Most current sources agree on X. However, this older book suggests Y, which the current Databricks docs supersede — for new work, go with X."

### 8.2 Generation Mode

For the Skills Factory. Rank silently, apply a selection strategy, produce consolidated output. No hedging, no "some sources say" — Skills loaded into agents need to be confidently actionable.

The same scoring runs underneath. What differs is consumption: instead of returning a structured object with conflicts surfaced, the generation flow gets a single consolidated source set with strategy-based selection already applied. The Skill content is generated from those selected sources only. Provenance is recorded silently.

**Stacking doc sources.** For a given Skill scope, the ranking engine pulls snapshots from all linked doc sources (Context7, DeepWiki, GitHub) and treats them as independent candidates. Authority scoring favors Context7 snapshots over DeepWiki over GitHub-raw, but the ranking engine doesn't hard-rule any out: a DeepWiki snapshot can beat a Context7 snapshot on relevance for a specific Skill scope, and the scoring formula will surface that. Cross-source corroboration (Context7 and DeepWiki agree) boosts confidence; cross-source conflict (they disagree) is flagged for human review, same as book-vs-docs conflict. This is what makes OSS-heavy Skills packages possible — a library with no Context7 entry can still anchor to DeepWiki snapshots and GitHub READMEs.

### 8.3 Selection Strategies

Three strategies, picked per-Skill based on domain characteristics:

#### Recent-doc anchored *(default for fast-moving technology)*

Current official docs win for factual content (syntax, APIs, parameters, current features). Books provide explanation, patterns, and depth. When a book contradicts current docs on a fact, docs win and book content is silently dropped from the Skill.

**Use for:** Databricks, Kubernetes, React, cloud services, anything where the truth is "what the current official docs say."

#### Consensus synthesis *(default for stable foundational domains)*

Multiple non-conflicting sources are merged into richer content. Where sources disagree, details are dropped rather than hedged. Corroboration count is a strength signal.

**Use for:** Dimensional modeling, relational algebra, algorithm families, design patterns, CAP theorem — foundational material where multiple authors agree on fundamentals.

#### Authority pick *(when one source is canonically definitive)*

Highest-ranked single source is the backbone; corroborating sources cited in provenance but don't shape content.

**Use for:** Kimball for dimensional modeling, Kleppmann for data-intensive apps, Fowler for refactoring — specific canonical references.

For a Databricks DE package, most Skills would use recent-doc anchored with consensus synthesis as fallback where doc coverage is thin. For a "Data Modeling Fundamentals" package, most Skills would use consensus synthesis or authority pick.

### 8.4 Conflict Handling

- **Factual conflict** (old book vs. current docs): docs win, book content silently dropped. Recorded in `skill_source.drop_reason = 'contradicted by current docs'`.
- **Philosophical conflict** (two authors disagree on approach): pick the top-ranked approach; either note it briefly in the Skill's "approach" paragraph or drop the contested detail entirely.
- **Unresolvable conflict**: fail loudly — flag the Skill for human review at the validation stage (`generation_notes` populated). The Factory should refuse to silently guess.

### 8.5 Scoring Formula

For a candidate passage *p* relative to a query/Skill-scope *q*:

```text
score(p, q) = w_rec  × recency(p)
            + w_doc  × doc_alignment(p)
            + w_rel  × relevance(p, q)
            + w_corr × corroboration(p)
            + w_auth × authority(p)
```

Weights `w_*` are query-type or Skill-type specific. Example profiles:

| Profile | w_rec | w_doc | w_rel | w_corr | w_auth |
|---|---|---|---|---|---|
| Currency-critical (interactive) | 0.30 | 0.35 | 0.20 | 0.10 | 0.05 |
| Foundational concept (interactive) | 0.05 | 0.05 | 0.30 | 0.30 | 0.30 |
| Skill, recent-doc anchored | 0.35 | 0.40 | 0.15 | 0.05 | 0.05 |
| Skill, consensus synthesis | 0.10 | 0.05 | 0.25 | 0.40 | 0.20 |
| Skill, authority pick | 0.05 | 0.10 | 0.20 | 0.10 | 0.55 |

Weights are starting points to be tuned against an evaluation set.

### 8.6 Provenance

Every ranking decision is recorded, whether or not it's surfaced:

- `skill_source` captures which sources were considered, their scores, their selection weights, and drop reasons for excluded sources.
- `skill.generation_notes` captures conflict flags, human-review flags, and strategy deviation notes.
- `skill.strategy` records which selection strategy was used.

This makes generated Skills **auditable and regenerable**. If a Skill later produces bad advice in production use, you can trace back which sources were selected, which were dropped, what the ranking weights were, and regenerate with refined weights or a different strategy — without re-extracting from scratch.

---

## 9. Deployment: Claude Code

Local-first, zero cloud. Claude Code is the agent harness. User authenticates with their Claude Max subscription so there are no incremental API token costs.

### 9.1 Why Claude Code

- **Subscription-covered.** Claude Max ($100/mo or $200/mo) includes Claude Code usage at no incremental cost. Managed Agents, by contrast, bills API tokens and session runtime separately. Heavy Skills Factory use on Managed Agents would easily exceed the Max monthly fee on tokens alone.
- **Full capability coverage.** Claude Code supports Skills files, slash commands, MCP servers (stdio and HTTP), subagents via the Task tool, file system access, bash, Python. Every v2 capability runs inside it.
- **Local-first architecture.** Matches the single-user, on-Mac, privacy-preserving design. ePubs stay in `~/Documents/eBooks`. Queries run at local disk speed. No deployment complexity.
- **Future-compatible.** If multi-user or headless needs ever emerge, the same codebase can be deployed to Managed Agents + Railway + DuckLake (see Appendix A). Same skills, same MCP server code, different transport and storage.

### 9.2 Project Layout

```text
mypub/
├── CLAUDE.md                   # Project instructions for Claude Code
├── README.md
├── data/
│   ├── catalog.ddb             # The local DuckDB with everything
│   └── generated-packages/     # Output of Skills Factory
├── .claude/
│   ├── skills/                 # Skills loaded into sessions
│   │   ├── kb-usage/
│   │   │   └── SKILL.md
│   │   ├── skills-factory/
│   │   │   └── SKILL.md
│   │   └── domains/            # Domain-specific Skills
│   └── commands/               # Slash commands
│       ├── kb-search.md
│       ├── kb-compare.md
│       ├── kb-prereqs.md
│       ├── kb-generate-skills.md
│       ├── kb-index.md
│       ├── kb-retire-book.md
│       ├── kb-refresh-docs.md
│       ├── kb-review-concepts.md
│       ├── kb-focus.md
│       ├── kb-pin-source.md
│       ├── kb-unpin-source.md
│       └── kb-refresh-status.md
├── mcp-servers/
│   ├── kb-mcp/                 # Local MCP server, stdio transport
│   │   ├── server.py           # FastMCP entry point
│   │   ├── retrievers.py       # Hybrid retrieval logic
│   │   ├── ranking.py          # Two-mode ranking engine
│   │   ├── resolution.py       # Entity resolution (called by extractors)
│   │   ├── discovery.py        # Auto-discovery: probe + confidence gate + inline ingest
│   │   ├── sectionizer.py      # Markdown/DeepWiki/Context7 → doc_section tree
│   │   ├── tiering.py          # Priority tier assignment from query signals
│   │   └── skills_factory.py   # Skills Factory pipeline
│   └── context7-config.json    # How to launch local Context7
├── scripts/
│   ├── index_books.py          # Book ingestion + FTS indexing
│   ├── refresh_docs.py         # Snapshot refresh + re-extraction (run by LaunchAgent)
│   ├── assign_tiers.py         # Nightly tier reassignment from concept_query_log
│   ├── extract_entities.py     # Entity/concept extraction
│   ├── extract_procedures.py   # Procedure extraction
│   └── generate_embeddings.py  # Embedding generation
├── launchd/
│   ├── com.mypub.refresh.plist # Nightly refresh LaunchAgent
│   └── com.mypub.tiering.plist # Nightly tier reassignment LaunchAgent
├── schemas/
│   ├── catalog.sql             # Table DDL
│   └── property_graph.sql      # DuckPGQ graph definition
├── patterns/                   # YAML pattern library
├── logs/                       # Refresh and tiering logs
└── tests/
```

### 9.3 Claude Code Configuration

At the project root, `CLAUDE.md` tells Claude Code how to use myPub:

```markdown
# myPub v2

This project is a knowledge base system indexing ~345 technical ePubs,
with a Skills Factory for generating Claude Skills packages.

## Key locations
- Local DuckDB: `data/catalog.ddb`
- ePub collection: `~/Documents/eBooks`
- Generated Skills packages: `data/generated-packages/`

## MCP servers (stdio + hosted)
- `mypub-kb` — hybrid retrieval, ranking, Skills Factory (local stdio)
- `context7` — primary doc source: vendor docs + well-documented OSS (local stdio)
- `deepwiki` — complementary doc source: AI-generated docs for any public GitHub repo (hosted HTTPS)
- `github` — long-tail fallback: raw file fetching from any public repo (local stdio)

## Common commands
- `/kb-search <topic>` — find chapters and concepts
- `/kb-compare <concept>` — compare author perspectives
- `/kb-prereqs <concept>` — show learning prerequisites
- `/kb-generate-skills <domain>` — run the Skills Factory
- `/kb-index <book-path>` — add new book or re-index updated books (incremental)
- `/kb-retire-book <path>` — mark a book as superseded (e.g., replaced by new edition)
- `/kb-refresh-docs [domain]` — refresh live doc snapshots and re-extract changed ones
- `/kb-review-concepts` — review borderline entity-resolution matches
- `/kb-focus <domain>` — elevate a domain's doc sources to Hot tier
- `/kb-pin-source <name>` — lock a specific doc source to Hot tier
- `/kb-unpin-source <name>` — return a pinned source to automatic tiering
- `/kb-refresh-status` — show tier assignments and next scheduled refreshes

## Conventions
When generating Skills packages, default to `recent-doc anchored`
strategy for any domain with live doc coverage. Use `consensus synthesis`
for foundational topics. Confirm strategy choice at decomposition stage.
For OSS libraries, link concepts to whichever doc sources are available —
Context7 where indexed, DeepWiki for architectural grounding, GitHub raw
as a last resort. Multiple sources per concept are encouraged; the ranking
engine merges them.
```

MCP servers are configured in Claude Code's settings to run as local stdio processes:

```json
{
  "mcpServers": {
    "mypub-kb": {
      "command": "uv",
      "args": ["run", "python", "mcp-servers/kb-mcp/server.py"],
      "cwd": "/Users/markoswald/projects/mypub"
    },
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"]
    },
    "deepwiki": {
      "url": "https://mcp.deepwiki.com/mcp",
      "transport": "http"
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

DeepWiki is the only remote server in the set — it's Cognition's hosted service, free for public repos, no auth required. The GitHub token is optional for public-repo access but raises rate limits substantially; a personal token with read-only `public_repo` scope is plenty.

### 9.4 The KB MCP Server

A single Python process using FastMCP, stdio transport. Exposes tools for the knowledge-base operations:

- `search_chapters(query, mode='interactive')`
- `compare_concept_across_authors(concept_name)`
- `find_prerequisites(concept_name, max_depth=5)`
- `generate_skills_package(domain, strategy_hint=None)`
- `query_pattern(pattern_id)`
- `refresh_doc_source(doc_source_id)` — fetch + diff + re-extract on content-hash change
- `list_pending_resolutions(limit=20)` — return borderline entity-resolution candidates
- `resolve_concept(queue_id, action, target_concept_id=None)` — merge / alias / keep_separate / rename
- `set_focus_domain(domain_name, duration_days=30)` — boost linked sources to Hot tier
- `pin_doc_source(doc_source_id)` / `unpin_doc_source(doc_source_id)` — manual tier lock
- `refresh_status()` — report per-tier inventory, next scheduled refresh, stale sources

The server opens the local DuckDB on startup, loads the FTS, VSS, and DuckPGQ extensions, and warms caches. FTS and VSS indexes may need to be rebuilt on startup if HNSW persistence is unstable — for 345 books this is a minutes-not-hours operation.

Claude Code invokes the tools through stdio. No network hop, no auth complexity, no deployment.

### 9.5 The Skills Factory in Claude Code

Invoked via `/kb-generate-skills <domain>`. The command expands into a conversation where Claude Code:

1. Calls `generate_skills_package` on the MCP server (decomposition + planning phases).
2. Reviews the proposed package shape with the user — "I propose these 12 Skills for Databricks DE, with these prerequisites. Proceed?"
3. Per Skill, calls further MCP tools to retrieve sources, rank them silently, apply strategy, generate content.
4. Writes files to `data/generated-packages/<domain>/` via the file system.
5. Runs validation, reports any human-review flags.

Because all the heavy LLM work runs inside Claude Code on your Max subscription, iterating on Skills packages is essentially free (subject to subscription rate limits). You can generate, review, tweak weights, regenerate — without worrying about API token bills.

---

## 10. Capability Comparison

| Capability | v1 | v2 |
|---|---|---|
| ePub indexing | ✓ | ✓ (incremental, content-hash-based) |
| Chapter-level retrieval | ✓ | ✓ (enhanced) |
| Section-level retrieval from docs | — | ✓ (symmetric with book chapters) |
| Concept graph | Manual | Automated extraction (books + live docs) |
| Keyword search | Basic SQL | FTS (BM25, books + live docs) |
| Semantic search | — | VSS (HNSW, books + live docs) |
| Graph traversal | Recursive CTE | DuckPGQ (SQL/PGQ) |
| Procedure extraction | — | ✓ (books + live docs) |
| Currency via live docs | — | ✓ (Context7 + DeepWiki + GitHub MCP) |
| OSS library coverage | Book-limited | Broad (any public GitHub repo) |
| Auto-discovery of new sources | — | ✓ (inline probe + ingest on first query) |
| Proactive doc refresh | — | ✓ (tiered LaunchAgent, Hot/Warm/Cool) |
| Multi-criteria ranking | — | ✓ (two-mode) |
| Conflict surfacing (interactive) | — | ✓ |
| Clean generation (silent ranking) | — | ✓ |
| Single-Skill generation | `/kb-generate-skill` | ✓ (enhanced) |
| Skills package generation | — | ✓ (Skills Factory) |
| Package-aware descriptions | — | ✓ |
| Trigger accuracy validation | — | ✓ |
| Pattern library | YAML | YAML + graph integration |
| Agent harness | Claude Desktop | Claude Code (Max-covered) |
| Deployment | Local | Local |
| Incremental API cost | $0 | $0 |

---

## 11. Implementation Roadmap

Six phases, each independently useful. Local-first throughout.

### Phase 1: Substrate upgrade (week 1–2)

Install DuckPGQ, VSS, and FTS extensions against the existing catalog. Generate embeddings for existing chapters and concepts. Build FTS indexes. Define the property graph over existing tables. Migrate existing `/kb-*` commands to use the new retrieval paths.

**Deliverable:** existing commands work with semantic and graph capabilities; no other architectural change yet.

### Phase 2: Automated extraction + resolution (week 3–5)

Build the entity/concept extractor with the domain schema. **Build the Entity Resolution module** (alias table, similarity matching, review queue). Run the extractor against the full corpus, with resolution active, populating the graph at scale. Human review of extraction samples and initial resolution queue to tune prompts, schemas, and similarity thresholds.

**Deliverable:** concept graph coverage goes from manually-curated subset to full-corpus automatic, with entity resolution established so subsequent phases (procedures, live docs) can add evidence without fragmenting the graph.

### Phase 3: Procedure extraction (week 6–7)

Build the procedure extractor with its own schema. Run against the corpus.

**Deliverable:** procedure library ready for Skills Factory consumption.

### Phase 4: Live docs + ranking (week 8–10)

Integrate Context7 as a local stdio MCP (primary doc source). Integrate DeepWiki as a hosted HTTPS MCP (complementary source). Integrate GitHub MCP as a long-tail fallback. Build unified doc snapshot cache with source-type awareness. **Implement the section parser** (`sectionizer.py`) that handles Context7's pre-chunked content, DeepWiki's page structure, and Markdown heading trees for raw GitHub files — every snapshot lands as a tree of `doc_section` rows, each with its own embedding and FTS entry. **Extend the entity and procedure extractors to run at section granularity** on content-hash change, populating the same concept and procedure graph that book content populates. Implement the ranking engine with both interactive and generation modes. Implement source merge with conflict surfacing (interactive) and selection strategies (generation). Populate `concept_doc_link` for priority concepts; a concept can and often should link to multiple doc sources. At this phase, refreshes run only on-demand or via manual `/kb-refresh-docs`.

**Deliverable:** currency-aware retrieval across `/kb-*` commands with broad OSS coverage, ranking infrastructure ready for Skills Factory, and a concept graph that grows beyond the book corpus as live docs evolve. Fixed-TTL refresh works; proactive tiering comes in Phase 4b.

### Phase 4b: Proactive refresh + priority tiers (week 10–11)

Add the tiering layer on top of Phase 4's on-demand refresh. **Staged rollout:**

1. *Fixed-TTL scheduled refresh.* Implement the LaunchAgent running `scripts/refresh_docs.py` nightly. Use the existing `doc_source.refresh_ttl_days` field; every source gets checked on a uniform schedule. Immediate UX win — first queries are already cached.
2. *Adaptive tiering.* Add `concept_query_log` wiring (log from interactive mode), implement `scripts/assign_tiers.py` to compute `priority_tier` nightly from query frequency, centrality, and source volatility. Hot/Warm/Cool cadences diverge.
3. *User controls.* Ship `/kb-focus`, `/kb-pin-source`, `/kb-unpin-source`, `/kb-refresh-status` commands plus their MCP tool backends.

**Deliverable:** near-instant cached responses for concepts the user actually works on, with overnight background extraction handling the cost. User can override automatic tiering when they know better than the system does.

### Phase 5: Skills Factory (week 12–15)

Implement decomposition (community detection + LLM refinement). Implement package planning. Implement per-Skill generation with package-aware descriptions and strategy selection. Implement validation. Implement materialization.

**Deliverable:** `/kb-generate-skills <domain>` produces coherent Skills packages on demand.

### Phase 6: Refinement and tuning (ongoing)

Evaluation set curation. Weight profile tuning against evaluation. Schema refinement based on extraction quality. Skills package quality review. Tier-assignment heuristic tuning as actual query patterns emerge. *Optional future work:* GitHub-activity-aware trending detection to surface newly-active libraries automatically.

---

## 12. Open Questions and Risks

**Extraction schema evolution.** As the domain ontology evolves, re-extraction is needed. Design the extraction pipeline to support incremental re-runs per book and per snapshot.

**Extraction quality across source styles.** The same extractor runs against book prose, Context7 reference docs, DeepWiki AI-generated summaries, and raw GitHub READMEs. These have very different styles — narrative in books, terse in docs, bulleted in READMEs, analytical in DeepWiki. The extractor prompts may need mild conditioning on source type, or quality on certain source types may be systematically weaker. Worth sampling extraction output per source type early in Phase 4 and adjusting if needed.

**Section splitting heuristic.** The default "split on H2, fold H3+ into parent" is a reasonable starting point but not universally right. Some repos use H1 for subsections within a single README (because H1 is the whole doc). Others nest deeply and meaningful content only starts at H3. Watch for two failure modes during Phase 4: (a) sections too coarse — a section spans multiple unrelated topics and extraction produces muddled concept links; (b) sections too fine — hundreds of one-sentence sections that fragment retrieval. The split threshold should be configurable per `doc_source`, with the default `section_split_level` field added to the schema if tuning pressure emerges.

**Entity resolution thresholds.** The 0.90 / 0.75 cosine thresholds are starting points. Too loose causes over-merging (distinct concepts collapsed into one); too tight causes under-merging (same concept splits into parallel nodes, defeating synthesis). The right values depend on the embedding model and the domain vocabulary. Expect several tuning iterations during Phase 2, driven by review-queue outcomes. Track merge-rate, false-merge-rate (detected later when a merged node's definition becomes inconsistent), and queue throughput as the signals.

**Tier assignment calibration.** The Hot/Warm/Cool thresholds depend on actual query patterns, which are unknown until the system is in use. Early Phase 4b will likely show that almost everything lands in Cool (because the query log is empty) and the scheduled refreshes mostly no-op. That's fine — seed the top 10–20 sources as Hot manually, let adaptive tiering take over as data accumulates. Rebalance quarterly.

**LaunchAgent reliability.** macOS is reasonably good about running scheduled LaunchAgents but not perfect — laptop closed, machine asleep, permissions revoked after OS updates. Refresh failures should log loudly and show up in `/kb-refresh-status`. Consider a missed-run catchup: if the last successful refresh is >36 hours old, run the Hot tier on next invocation regardless of schedule.

**Auto-discovery confidence tuning.** The confidence gate — "only ingest if the probe returns a clear match" — needs careful calibration. Context7's `resolve-library-id` returns multiple candidates ranked by relevance; the threshold for "clear match" (e.g., top result score >0.8, second result score <0.5) will need tuning against real queries. Track discovery events (probed, ingested, asked user, discarded) in a log table to build an eval set for the confidence gate. The bias should always be toward asking the user rather than guessing wrong.

**Auto-discovery scope management.** Over months of use, auto-discovered sources accumulate. Some will be one-off queries that never get touched again. Build a cleanup mechanism: sources with zero queries in 90 days and no concept links used by other sources can be proposed for removal via a `/kb-cleanup` command. Don't auto-delete — let the user decide.

**HNSW persistence.** Still experimental in DuckDB. If unstable, fall back to in-memory indexes rebuilt on MCP server startup. For 345 books this is minutes, acceptable for development; revisit if cold start becomes annoying.

**Embedding model choice.** `sentence-transformers/all-MiniLM-L6-v2` is a reasonable default (fast, 384-dim, decent quality). Higher-quality embeddings improve semantic retrieval but at storage cost.

**Doc source coverage.** Context7 + DeepWiki + GitHub together cover most technologies in active use — vendor products, popular OSS, and long-tail repos. For corpus topics with no book coverage and no matching repo (rare), v2 degrades gracefully. Where coverage is thin on the doc side, the ranking engine has less cross-source corroboration to work with, so Skills for those topics should be flagged for closer human review.

**DeepWiki and GitHub rate limits.** DeepWiki is free for public repos with no published rate limit, but is a single-source-of-failure if its MCP is flaky. GitHub's unauthenticated API is heavily rate-limited; using a personal access token (even read-only) raises the ceiling to 5000 requests/hour, which is more than enough for Skills Factory work. Batch fetches carefully — don't spray hundreds of file requests per Skill.

**Multi-source conflict frequency.** Unknown until implemented. If Context7 and DeepWiki frequently disagree on the same library, that's noise that needs handling. If they rarely disagree but both disagree with the books, the current architecture handles it well. Track this during Phase 4.

**Skills package evaluation.** "Does the right Skill trigger?" is measurable. "Is the Skill content correct?" needs human review, at least initially. Budget time for review of early generated packages.

**Strategy choice automation.** For now, strategy selection can be human-in-the-loop (Factory proposes, user confirms). Over time, an evaluation set could teach the system which strategies produce higher-quality Skills for which domain shapes.

**Subscription rate limits.** Claude Max has weekly usage caps. Heavy Skills Factory iteration could hit them. Worth tracking actual usage; Max 20x ($200/mo) raises the ceiling significantly compared to Max 5x.

---

## Appendix A: Cloud Deployment (Future Optionality)

The local-first architecture is the right answer today. If the requirements ever shift — multi-user access, headless scheduled runs, a polished web UI for non-technical users, collaborator sharing — the HealthSim deployment pattern provides a proven path.

### When to consider

Concrete triggers:

- You want to share myPub or specific Skills packages with collaborators who aren't comfortable with Claude Code.
- You want scheduled runs (e.g., regenerate Skills packages weekly as docs change).
- You want a web UI so non-technical users can invoke the Skills Factory.
- Workload grows enough that keeping your Mac always on becomes the bottleneck.

### Target architecture

Mirrors HealthSim's five-platform pattern:

| Platform | Role |
|---|---|
| Anthropic Managed Agents | Agent runtime, session management |
| Railway | MCP server hosting (FastMCP over HTTPS) |
| DuckLake | Analytical data (Supabase Postgres catalog + S3/R2 Parquet) |
| Supabase | Operational state (session history, saved packages) |
| GitHub | Source of truth |

Key substitution vs. HealthSim: **DuckLake** replaces MotherDuck for analytical storage because myPub needs server-side VSS and DuckPGQ, which MotherDuck doesn't support. DuckLake v1.0 (production-ready April 2026) uses a Postgres catalog plus S3 Parquet data, with DuckDB compute running in the Railway MCP server — all extensions available, all queries run where data is warm.

### Migration path

If the triggers materialize, the migration is incremental:

1. **Containerize the KB MCP server.** Already a local Python process; wrap in a Dockerfile, deploy to Railway. Keep stdio transport working for local dev; add HTTP transport for production.
2. **Provision DuckLake.** Create catalog schema in Supabase (or use MotherDuck's managed DuckLake). Migrate `catalog.ddb` contents via `ATTACH` and table copies.
3. **Adopt the Managed Agents deploy pattern.** Port from HealthSim: `agent-config.yaml` manifest, `push-skills.py`, `push-agent.py`, `push-environment.py`. Anthropic's Skills API hosts the myPub skills.
4. **Keep local mode working.** Same codebase, same skills, same MCP server code — just different transport (stdio vs HTTP) and different storage (local DuckDB file vs DuckLake). This is the dual-mode pattern HealthSim already uses.

### Cost implications

At the point of switching to cloud, API token costs for Managed Agents become real. For heavy Skills Factory use, this can easily exceed the Max subscription cost. The financial tradeoff becomes: "does the value of remote access / multi-user / headless operation justify roughly $50–200/month in Managed Agents tokens plus $20–50/month in infrastructure?" Worth revisiting when actual use patterns are clearer.

---

## 13. Summary

myPub v2 keeps the philosophical commitments that made v1 work (native-first retrieval, structure preservation, multiple author perspectives) and closes the automation and currency gaps that limited v1's scale.

The architectural bets are:

- **DuckDB with all three extensions** is a sufficient substrate for keyword, semantic, and graph retrieval in a single local process.
- **Claude Code with Max subscription** is the right agent harness for single-user workloads — zero incremental API cost, full capability coverage.
- **Unified extraction pipeline** runs against both book chapters and live doc sections, populating a single concept and procedure graph. Books contribute depth; doc sections contribute breadth and currency; they converge on the same concept nodes.
- **Symmetric section-level granularity.** A `doc_section` is the docs-side equivalent of a book chapter — coherent text with its own embedding, FTS entry, and typed concept references. Retrieval and ranking don't need to know whether evidence came from a book or from a Zippy README section.
- **Entity resolution is the load-bearing integration mechanism** — candidates from both books and docs resolve to shared concept nodes via embedding similarity, keeping the graph cohesive and enabling synthesis where new OSS libraries slot into decades of accumulated conceptual framing.
- **Context7 + DeepWiki + GitHub as a layered doc-source stack** closes the currency gap with broad OSS coverage — vendor docs, indexed OSS, AI-generated wiki docs for any public repo, and raw file fallback. All local or free-hosted, no cloud deployment required.
- **Auto-discovery** lets the knowledge base grow organically from use. Query an unknown technology and the system probes, ingests, and integrates it inline — with a confidence gate that asks the user to disambiguate rather than ingesting uncertain content. Over time, the KB converges on exactly the technologies you actually work with.
- **Proactive tiered refresh** keeps frequently-used doc content pre-warmed overnight so interactive queries don't pay the extraction latency. Hot sources refresh daily, Warm weekly, Cool monthly — automatically, based on actual query patterns, with user override.
- **Two-mode ranking** — conflicts surfaced interactively, resolved silently for generation — keeps the KB assistant nuanced and the Skills Factory clean.
- **Cloud deployment is a future option, not a current requirement.**

Evolution, not revolution. Every new capability adds a layer; nothing replaces what works.
