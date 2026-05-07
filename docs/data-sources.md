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
| Books at this writing | 541 |
| Authors | ~600+ unique |
| Publishers | O'Reilly (270), Manning (48), Packt (28), Addison-Wesley (12), Apress (9), Wiley (9), Elsevier (12), … |
| Total chapters | 113,165 |
| Chapters with content | 112,968 |
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
- **Author placeholder.** A historical "AUTHOR NAMES HERE" placeholder author was deleted in commit `ecc74f4`; three books currently have NO author rather than a misleading placeholder. These are tracked for future re-extraction.
- **Content-hash duplicates.** A book's chapter is identified by hash; re-running ingestion is idempotent.

### Top of the library by author and publisher

Top authors by book count: Martin Fowler (4), Joe Celko (4), Brian W. Kernighan (4), Denis Rothman (3), Tom Taulli (3), Jay Wengrow (3), Bruce Schneier (3), Roberto Infante (3), Addy Osmani (3), Ole Olesen-Bagneux (3).

Top publishers (by book count, with their authority tier used in ranking):

| Publisher | Books | Authority tier (default) |
|---|---|---|
| O'Reilly Media, Inc. | 270 | high |
| Manning Publications | 48 | high |
| Packt | 28 | medium |
| Addison-Wesley Professional | 12 | high |
| Apress | 9 | medium |
| John Wiley & Sons | 9 | medium |
| Elsevier | 12 | medium |

Authority tiers feed the `authority` factor in the ranker — see [docs/architecture.md](architecture.md#five-factor-re-scoring).

---

## Live documentation

The live-doc layer ingests current vendor documentation through MCP servers, snapshots the sections into the same DuckDB catalog, and re-indexes them on a TTL schedule.

### The ten currently-indexed sources

| Source | Provider | Sections | Snapshot age |
|---|---|---|---|
| PostgreSQL | Context7 | 22 | snapshot |
| Apache Kafka | Context7 | 22 | snapshot |
| Apache Spark | Context7 | 22 | snapshot |
| LangChain | Context7 | 22 | snapshot |
| MLflow | Context7 | 26 | snapshot |
| Databricks | Context7 | 28 | snapshot |
| Delta Lake | Context7 | 21 | snapshot |
| DuckDB | Context7 | 20 | snapshot |
| DuckPGQ | DeepWiki | 282 | snapshot |
| FastMCP | DeepWiki | 437 | snapshot |

902 doc sections total at this writing.

### Why three providers (Context7, DeepWiki, GitHub raw)

The probe order and authority defaults match how trustworthy each source typically is for a *technical* lookup:

| Provider | Authority default | Best at | Worst at |
|---|---|---|---|
| **Context7** | 0.60 | Vendor docs for popular OSS (Postgres, Kafka, Spark, Databricks, MLflow, …) | Long-tail libraries it hasn't indexed |
| **DeepWiki** | 0.50 | AI-generated docs for any public GitHub repo (DuckPGQ, FastMCP, smaller libs) | Authoritativeness — it's *generated*, so we trust it less than vendor docs |
| **GitHub raw** | 0.40 | Last-resort fetch of README / docs/ directory | No structure, manual section parsing |

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

Today's catalog: **120 CORROBORATES edges** across 7 of 10 sources:

| Source | Alignment edges |
|---|---|
| LangChain | 24 |
| Apache Kafka | 22 |
| Delta Lake | 20 |
| Apache Spark | 17 |
| DuckDB | 14 |
| PostgreSQL | 12 |
| Databricks | 11 |
| MLflow | (pending — alignment run not yet executed) |
| DuckPGQ | (pending — narrow vendor surface; deferred) |
| FastMCP | (pending — narrow vendor surface; deferred) |

CONTRADICTS edges are 0 today: narrow vendor docs tend to corroborate or be unrelated to book content, not contradict it. The `Migration Guide` and `Currency Report` generators are designed to surface contradictions when they appear — see [docs/generators.md](generators.md#migration-guide).

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

A few topics return tangential content because the corpus simply doesn't cover them well:

- **TLS certificate pinning.** No chapter title in the corpus matches all three tokens; PostgreSQL SSL doc wins as a defensible-but-not-canonical answer. A security-focused book would close this.
- **FHIR / HL7 / EHR integration patterns.** Healthcare data exchange topics return tangential content; FHIR and HL7 are real registered libraries on Context7 / DeepWiki and a `/kb-discover` workflow during dogfooding would probe them.

These are known gaps — see [docs/operations.md](operations.md#corpus-gaps) for the running list.

---

## See also

- [docs/architecture.md](architecture.md) — substrate and ranking details
- [docs/ingestion-and-indexing.md](ingestion-and-indexing.md) — the indexing pipeline end-to-end
- [docs/concept-graph.md](concept-graph.md) — entity extraction and alignment
- [docs/operations.md](operations.md#refresh) — refresh policy and TTLs
