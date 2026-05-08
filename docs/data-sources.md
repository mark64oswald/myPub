# Data sources

myPub combines two kinds of inputs: a personal ePub library that doesn't change much, and live vendor documentation that changes weekly. This document explains what each is, how it's modeled, and how the two combine.

[← back to top-level README](../README.md) · [Hello, myPub! ↗](getting-started.md) · [Architecture ↗](architecture.md)

---

## Why two sources, not one

A book is a deliberate, edited explanation of an idea — at the moment it was published. A vendor doc is the current truth about an API. Most knowledge bases pick one and lose the other half:

- A book-only KB tells you what consistent hashing *is*, but doesn't know that DynamoDB now uses adaptive capacity.
- A doc-only KB tells you the current API surface, but never explains why the API is shaped that way.

myPub keeps both. The `alignment_edge` table records where they corroborate or contradict — and the ranker's `corroboration` and `doc_alignment` factors fold that into search.

---

## ePub library

### Source

| | |
|---|---|
| Location | `~/Documents/eBooks/` (configurable per command) |
| Format | EPUB 2 / EPUB 3 |
| Books indexed | 572 |
| Authors | 820 unique |
| Publishers | O'Reilly (283), Manning (52), Packt (71 across imprints), Addison-Wesley (12), Elsevier (12), Apress (9), Wiley (9), … |
| Total chapters | 118,447 |
| Chapters with content + embeddings | 118,110 |
| Total tokens | several hundred million (rough estimate from chapter content) |

Books are personal copies; the catalog stores metadata, structure, derived embeddings, and chapter content. The original ePub files are not redistributed via this repository.

### What gets extracted from each ePub

`scripts/index_books.py` walks the ePub, stores rows in `book` and `book_author`, sectionizes the spine into a `chapter` tree, and recovers metadata from OPF / NCX / TOC sources with fallback heuristics:

```text
book                  ← title, publisher, publication_date, language, isbn,
                        cover hash, file path, content hash
book_author           ← author display names with disambiguation
chapter               ← parent_chapter_id, ordinal, title, content,
                        token_count, content_hash
```

Special cases the indexer handles:

- **Front-matter filtering.** "Cover", "Copyright", "About the Author" chapters are detected and indexed but flagged so retrieval can deprioritize them.
- **Heading-only chapters.** Some ePubs have heading-only entries; these get `token_count = 0` and are skipped by FTS / VSS but kept for structural completeness.
- **Author recovery via ISBN lookup.** When `<dc:creator>` is missing or contains a placeholder ("AUTHOR NAMES HERE"), the indexing flow falls back to OpenLibrary + Google Books ISBN lookup. 19 of 20 historically-unauthored books were recovered this way; one residual case (Platform Enterprise, ISBN 9798341643444) is genuinely unknown — no public catalog has author info for that ISBN.
- **Author smush.** Some ePubs encode all co-authors in a single `<dc:creator>` element separated by ", " or " and ". The post-ingest splitter handles this with credential-suffix re-merging (M.D., Ph.D., Jr., II) so "Stephen Buxton, Lowell Fryman, Ralf Hartmut Güting, …" becomes individual rows.
- **Empty-OPF recovery.** Some Packt ePubs ship with a 0-byte `OEBPS/content.opf`. ebooklib refuses to open them. The recovery indexer parses the TOC xhtml directly and synthesizes the spine — see [README → Engineering depth](../README.md#engineering-depth--the-things-that-didnt-go-in-the-spec).
- **Content-hash duplicates.** Each chapter's content has a SHA256; re-running ingestion is idempotent. Macro-level duplicates (a Safari "(1).epub" auto-rename of an already-indexed book) are caught by chapter-content-hash overlap during extraction prep — the duplicate book is dropped from the catalog before extraction runs.

### Top of the library by author and publisher

Top authors by book count: Mark Richards (6), Valliappa Lakshmanan (6), Joe Celko (5), Neal Ford (5), Brian W. Kernighan (4), Khaled El Emam (4), Martin Fowler (4), Ralph Kimball (4).

Top publishers (by book count, with their authority tier used in ranking):

| Publisher | Books | Authority tier (default) |
|---|---|---|
| O'Reilly Media, Inc. | 283 | high |
| Manning Publications | 52 | high |
| Packt (all imprints) | 71 | medium |
| Addison-Wesley Professional | 12 | high |
| Elsevier | 12 | medium |
| John Wiley & Sons | 9 | medium |
| Apress | 9 | medium |

Authority tiers feed the `authority` factor in the ranker — see [docs/architecture.md](architecture.md#five-factor-re-scoring).

---

## Live documentation

The live-doc layer ingests current vendor documentation through MCP servers, snapshots the sections into the same DuckDB catalog, and re-indexes them on a TTL schedule.

### Registered sources

**54 sources, 1,909 sections.** 52 served via Context7, 2 via DeepWiki. Below is the high-density subset by alignment-edge count; the full list spans data platforms (Spark / Kafka / Snowflake / BigQuery / Iceberg / Delta Lake / Hive / Trino), ML frameworks (PyTorch / TensorFlow / Keras / scikit-learn / spaCy), web frameworks (React / Next.js / FastAPI), AI/LLM tooling (LangChain / LangGraph / LlamaIndex / Haystack / MLflow / Stable Diffusion), cloud services (AWS Glue / S3 / Redshift / SageMaker / Lambda, GCP BigQuery, Databricks), databases (PostgreSQL / MySQL / SQLite / Redis / Neo4j / DuckDB / DuckPGQ), and dev tooling (Docker / Kubernetes / Terraform / Airflow / dbt / OpenAPI / gRPC / Jupyter / GitHub / GitHub Copilot / FastMCP).

| Source | Provider | Sections | Alignment edges |
|---|---|---|---|
| FastMCP | DeepWiki | 437 | 221 |
| DuckPGQ | DeepWiki | 282 | 151 |
| MLflow | Context7 | 26 | 36 |
| React | Context7 | 23 | 31 |
| FastAPI | Context7 | 23 | 32 |
| scikit-learn | Context7 | 22 | 31 |
| Apache Kafka | Context7 | 22 | 29 |
| OpenAPI | Context7 | 24 | 28 |
| spaCy | Context7 | 24 | 28 |
| LangChain | Context7 | 22 | 28 |
| SQLite | Context7 | 21 | 28 |
| Stable Diffusion | Context7 | 23 | 25 |
| GitHub | Context7 | 22 | 25 |
| Haystack | Context7 | 23 | 24 |
| Delta Lake | Context7 | 21 | 24 |
| PostgreSQL | Context7 | 22 | 23 |
| Apache Spark | Context7 | 22 | 22 |
| dbt | Context7 | 22 | 22 |
| Docker | Context7 | 21 | 22 |
| Databricks | Context7 | 28 | 20 |
| (34 more sources, 5–20 edges each) | | ~840 | ~480 |

The DeepWiki sources (FastMCP, DuckPGQ) have anomalously high section counts because DeepWiki returns full repo trees rather than the 20-25 top topics that Context7 returns by default. The two stranded-then-recovered sources (Phase 1: snapshot only; Phase 2: extraction; Phase 3: alignment) contributed 391 of today's 1,320 alignment edges.

### Why three providers (Context7, DeepWiki, GitHub raw)

The probe order and authority defaults match how trustworthy each source typically is for a *technical* lookup:

| Provider | Authority default | Best at | Worst at |
|---|---|---|---|
| **Context7** | 0.85 | Vendor docs for popular OSS, indexed and curated (Postgres, Kafka, Spark, Databricks, MLflow, FastAPI, React, …) | Long-tail libraries it hasn't indexed |
| **DeepWiki** | 0.70 | AI-generated docs for any public GitHub repo (FastMCP, DuckPGQ, niche libs not on Context7) | Authoritativeness — it's *generated*, so we trust it less than vendor docs |
| **GitHub raw** | 0.65 | Last-resort fetch of README / docs/ directory | No structure, manual section parsing |

Probe order is Context7 → DeepWiki → GitHub raw. First confident hit wins. If multiple candidates score similarly, the discovery loop returns `asked_user` and waits for the user to pick.

### Snapshot model

```text
doc_source              ← name, mcp_server, identifier, authority_score,
                          refresh_ttl_days, priority_tier, pinned
doc_snapshot            ← per-fetch row: source_id, retrieved_at,
                          content_hash, full content blob
doc_section             ← heading-aligned section: snapshot_id, heading,
                          ordinal, content, embedding (side table)
doc_section_embedding   ← 384-dim float[] (embeds the section text)
doc_snapshot_embedding  ← 384-dim float[] (embeds the whole snapshot,
                          for whole-doc relevance)
```

A doc is re-snapshotted when:

- Its TTL elapses (default 30 days; can be set per source with `refresh_ttl_days`)
- Its content hash differs from the last snapshot (so a TTL-elapsed-but-unchanged doc doesn't generate noise)
- The user runs `/kb-refresh-docs <source>` (planned slash command)

Pinned sources (`doc_source.pinned = true`) are exempt from automatic refresh — useful for "I'm shipping this week, freeze the doc snapshot until I'm done."

### Sectionizer

ePubs and live docs both run through the same sectionizer, but with different heuristics:

- For ePubs, the chapter tree is already explicit (NCX / TOC).
- For Context7 / DeepWiki / GitHub markdown, the sectionizer walks H1 / H2 / H3 headings and produces a tree.

Edge cases the live-doc sectionizer handles:

- **URL-shaped headings.** A doc whose top heading was a raw URL (e.g., `https://example.com/docs/api`) used to break title-coverage scoring. The sectionizer now detects URL-shaped headings, derives a body-internal heading (preferring embedded H3 subheads, falling back to a heading-shaped first line), and sets `heading = NULL` if no reasonable substitute exists. Fixed in commit `ecc74f4`.
- **Acronym tokens.** The retrieval ranker requires acronym tokens (e.g., "FHIR", "HL7") to appear *literally* in candidate sections to prevent semantic-only matches drowning out exact-phrase ones. Fixed in commit `e82810b`.

---

## How books and live docs combine

### Alignment edges

After both modalities are indexed, an alignment pass extracts pairwise edges between book chapters and live doc sections that discuss the same concept. The output is a row in `alignment_edge`:

```text
alignment_edge
  from_doc_section_id   → which live-doc section
  to_chapter_id         → which book chapter (or to_doc_section_id for doc-doc)
  concept_id            → what concept they're both about
  relation_type         → 'CORROBORATES' | 'CONTRADICTS'
  confidence            → 0..1
  explanation           → human-readable snippet
```

Today's catalog: **1,320 alignment edges** across all 54 sources — 1,296 CORROBORATES (avg confidence 0.72) and 24 CONTRADICTS (avg confidence 0.16). The full per-source breakdown is the table at the top of [Live documentation → Registered sources](#registered-sources).

CORROBORATES dominates because narrow vendor docs tend to agree with or be unrelated to book content; high-confidence CONTRADICTS edges (vendor doc explicitly supersedes book guidance) are rare but valuable when they appear. Examples live in the catalog: FastMCP allowing breaking changes in minor versions vs. SemVer textbooks; React Compiler being installable now vs. "experimental" in older books; DuckPGQ's logical-graph-over-SQL approach vs. native graph databases' index-free adjacency.

The `Migration Guide` and `Currency Report` generators consume CONTRADICTS edges — see [docs/generators.md → Migration Guide](generators.md#migration-guide). Quality of those generators improves as alignment is re-run with contradiction-tuned prompts; the alignment-rerun-after-new-books loop is described in [docs/operations.md → Deferred work](operations.md#deferred-work).

### Where alignment shows up in retrieval

For a query against an aligned domain:

```text
┌─ ranker computes raw 5-factor score ──────────────────────────────┐
│                                                                   │
│   relevance      0.78   (RRF-fused FTS+VSS+graph)                 │
│   recency        0.40   (mixed — book 2017 + live 2026)           │
│   authority      0.85   (O'Reilly book + Context7 doc)            │
│   corroboration  0.62   ← +0.62 boost from CORROBORATES edge      │
│   doc_alignment  1.00   ← domain has live-doc coverage            │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

For the same kind of query against an unaligned domain (e.g., a topic where the live-doc match is FastMCP, which has no extracted alignment edges):

```text
   corroboration  0.00   ← no CORROBORATES edge (signal dormant)
   doc_alignment  0.50   ← neutral; domain doesn't have alignment yet
```

The `doc_alignment` factor exists specifically to prevent penalizing queries that *can't* be corroborated because no live-doc-to-book alignment has been extracted for that domain — a 0.50 neutral pad rather than a 0.00 zero.

### When the system runs `/kb-discover` instead

If a user asks about a library that's not in any source — books or live docs — the search returns `outcome="needs_discovery"`. The `/kb-discover` slash command:

1. Probes Context7, then DeepWiki, then GitHub raw.
2. Presents candidate sources to the user with their authority scores and section counts.
3. On user approval, runs `disambiguate_discovery`, which adds the source to `doc_source`, fetches a first snapshot, and re-runs the original search.

See [docs/ingestion-and-indexing.md](ingestion-and-indexing.md#refresh-and-discovery) for the snapshot lifecycle.

---

## Adding more sources

### Adding a book

```bash
# Copy the ePub into your library
cp some-new-book.epub ~/Documents/eBooks/

# Re-run the indexer (idempotent; only new files are processed)
.venv/bin/python3 scripts/index_books.py --source ~/Documents/eBooks

# Generate embeddings for the new chapters
.venv/bin/python3 scripts/generate_embeddings.py

# Indexes refresh automatically on next FTS/VSS query
```

### Adding a live doc source

Use `/kb-discover <library-name>` interactively, or seed manually:

```bash
.venv/bin/python3 scripts/seed_doc_sources.py --name "Apache Pulsar" \
    --source-type context7 --identifier "/apache/pulsar"

.venv/bin/python3 scripts/refresh_docs.py refresh --source "Apache Pulsar"
```

### Removing or repairing a source

`scripts/fix_broken_doc_sources.py` is the safety net for sources that got corrupted during a snapshot fetch. The Databricks and LangChain repairs in commit `f9a0f61` used this script.

---

## Corpus gaps (acknowledged)

A few topics still return tangential content because the corpus doesn't cover them deeply yet:

- **TLS certificate pinning + low-level network security.** No chapter title in the corpus matches all three tokens; PostgreSQL SSL doc wins as a defensible-but-not-canonical answer. A security-focused book would close this.
- **HL7-FHIR / clinical-trial / EHR integration patterns.** Healthcare data exchange topics return tangential content. FHIR ships its specs as JSON/XML resource definitions (one per `Patient`, `Observation`, etc.); HL7 v2 ships as schema documents. Both fit the `doc_section` model better than the `chapter` model and would be a natural extension of the live-doc layer once a `source_type='fhir_resource'` ingestion path is added. Recently-ingested life-sciences books (Biology for Engineers, NGS Data Analysis, Zero to Genetic Engineering Hero, Biophysics) cover the underlying biology; the integration / interop layer is the gap.
- **Peer-reviewed life-sciences research (PubMed Central JATS XML).** A different ingestion path than ePub — JATS XML is structured and consistent, would map cleanly to a `paper` table that mirrors `book`. Worthwhile when the corpus needs to support life-sciences research workflows.

Tracking: [docs/operations.md → Deferred work](operations.md#deferred-work).

---

## See also

- [docs/architecture.md](architecture.md) — substrate and ranking details
- [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) — the indexing pipeline end-to-end
- [docs/concept-graph.md](concept-graph.md) — entity extraction and alignment
- [docs/operations.md](operations.md#refresh) — refresh policy and TTLs
