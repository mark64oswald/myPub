# Phase 3.1 — Procedure Extraction (2026-04-30)

First procedure-extraction run. Validates the sub-agent coordinator
([scripts/extract_procedures.py](../scripts/extract_procedures.py)) at small
scale and establishes a sane no-op rate.

## Schema additions

Migration: [scripts/migrate_phase3_procedures.py](../scripts/migrate_phase3_procedures.py)

- `chapter.procedure_attempted_at TIMESTAMP` — set after a chapter has been
  dispatched for procedure extraction; lets resumable sessions skip chapters
  that returned zero procedures (front-matter, conceptual prose).
- `procedure_concept(procedure_id, concept_id)` — link table for the
  "procedure operates on these concepts" edge. Concept references on the
  extractor output pass through `EntityResolver` then land here. The
  IMPLEMENTS edge (Procedure → Pattern) continues to use the existing
  `procedure.implements_pattern` column.

Schema drift fixed in passing: [schemas/catalog.sql](../schemas/catalog.sql)
now declares `chapter.extraction_attempted_at` (Phase 2 added it via
ALTER TABLE without updating the canonical schema).

## Validation pass — 5 hand-picked chapters

Selected for diversity: 4 procedural domains and 1 conceptual negative case.

| ch | book | title | procs |
|---:|---|---|---:|
| 112711 | Version Control with Git | B. Installing Git | 5 |
| 55165 | Hadoop: The Definitive Guide | A. Installing Apache Hadoop | 4 |
| 60214 | Implementing Domain-Driven Design | Creating CalendarEntry Instances | 1 |
| 78826 | Mastering Kafka Streams | A. Kafka Streams Configuration | 0 |
| 99822 | Software Architecture Patterns | 1. Introduction | 0 |

Notable behaviors:

- The Kafka config appendix is a reference table of parameters, not a
  step-by-step how-to. The sub-agent correctly returned `{"procedures": []}`
  with explicit reasoning ("this appendix is a reference, not a how-to").
- The Software Architecture Patterns intro is conceptual prose. Zero, correct.
- The DDD chapter produced one procedure with 8 detailed steps and resolved
  `implements_pattern: "Factory"` to the existing `Factory` Pattern concept.

Persistence: 10 procedures, 43 concept links, 1 pattern link. No invented
commands; literal code transcribed from the source where shown.

## Small batch — 50 chapters (default selection)

Run via `extract_procedures.py prep --limit 50` (dedup by content_hash,
skip-attempted, alphabetical by book title). Selection landed on two books:

- `"Looks Good to Me"` (Code Review) — 24 chapters
- `A Common-Sense Guide to Data Structures and Algorithms in Java` — 26 chapters

Dispatched as 10 sub-agents × 5 chapters each, running in parallel.

| metric | value |
|---|---:|
| chapters processed | 50 |
| chapters with ≥1 procedure | 14 (28%) |
| chapters with 0 procedures | 36 (72%) |
| procedures written | 20 |
| concept links written | 75 |
| pattern links written | 12 |

Resolution mix on procedure-concept references:

| outcome | count |
|---|---:|
| exact | 59 |
| embedding_high | 1 |
| borderline (queued) | 5 |
| new | 11 |
| pattern_link | 12 |

87% of concept references (60 / 70 — exact + embedding_high) merged onto
existing nodes from the Phase 2 graph. This is the same merge dynamic as
Phase 2 cross-book extraction: most procedure concepts already exist as
discussion-level nodes.

## Patterns extracted

12 IMPLEMENTS edges across 11 unique patterns. The code-review book
("Looks Good to Me") explicitly names patterns in its source material;
those got picked up cleanly:

- Atomic Pull Request (×2)
- Linear Search (×2 — `linear search` casing variant; will resolve via review queue or alias seeding)
- Team Working Agreement
- 5P Process
- MMG Exchange
- Comment Categorization
- Defined Code Review Process
- Break-Glass Procedure
- Automated Style Enforcement
- Binary Search
- Factory (from validation pass)

## Quality observations

What worked:

- Step-by-step procedures correctly distinguished from conceptual prose.
  Front-matter and intros consistently produce zero, even when the chapter
  title sounds procedural ("Why Data Structures Matter" → 0 procs, correct).
- Reference appendices (Kafka config) recognized as non-procedural.
- Named patterns extracted reliably when the source uses them explicitly.
- Code in steps is verbatim where the chapter shows it; absent otherwise.
  No fabricated commands seen.

To watch in future sessions:

- The `linear search` casing duplicate is a borderline-resolution case. The
  resolver scopes Pattern matching by `concept_type`, so when the LLM
  returns a pattern name that already exists as a non-Pattern concept, a
  new Pattern node is created. May want to revisit alias seeding for
  cross-type matches.

## Throughput

Wall clock: ~3.5 minutes for 10 sub-agents to process 50 chapters in
parallel; ~10 seconds Python-side processing. At this rate, the 12,981
unique-content-hash chapter set in the corpus would extract in roughly
10–12 hours of agent wall time across many sessions. Many will be no-ops
(matching this run's 72% no-op rate), which is the architectural
expectation: not every chapter has procedures.

## Artifacts

- `/tmp/mypub-procedures/validate-5/` — 5-chapter validation session
- `/tmp/mypub-procedures/batch-50/` — 50-chapter run

Catalog backup: `data/catalog_pre-phase3.ddb` (taken before migration).
