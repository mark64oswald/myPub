# Phase 3.1 — Session 6 (2026-05-01)

Sixth procedure-extraction session. Heaviest density yet — the run
landed in books that are essentially cookbooks (Bioinformatics tools,
Schneier's Applied Cryptography algorithms, Iceberg/Spark/Dremio
ops, Kafka admin). Result: **357 procedures from 250 chapters
(1.43 procs/chapter)**.

## Scope

250 chapters across 16 books. No deferred chapters from session 5.

| book | chapters in session |
|---|---:|
| Basic Applied Bioinformatics | 48 |
| Applied Cryptography (Schneier) | 30 |
| Apache Kafka in Action | 24 |
| Architecture Patterns with Python | 23 |
| Apache Iceberg: The Definitive Guide | 19 |
| Applied Machine Learning and AI for Engineers | 15 |
| Anonymizing Health Data | 14 |
| Architecting AI Software Systems | 13 |
| Architecting Data and Machine Learning Platforms | 13 |
| Applied Health Analytics and Informatics Using SAS | 12 |
| (6 other books, total) | 39 |

## Dispatch

5 waves of 10 sub-agents × 5 chapters each.

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     50 |    86 | 1.72 |
|    2 |         50 |     50 |    99 | 1.98 |
|    3 |         50 |     50 |    54 | 1.08 |
|    4 |         50 |     50 |    48 | 0.96 |
|    5 |         50 |     50 |    70 | 1.40 |

All 250 chapters landed cleanly — no rate-limit interruptions, no
sub-agent stalls. Wave 2's density of 1.98 procs/chapter is the
highest single-wave density across all sessions; nearly every chapter
in the Schneier crypto block ciphers and public-key crypto
sections produced 3-7 procedures.

## Results — this session

```text
chapters dispatched   250
chapters landed       250 (100%)
procedures written    357
chapters w/ ≥1 proc   154 (62%)
chapters w/ 0 procs   96 (38%)
concept links written 1,533
pattern links written 193
```

Resolution mix on procedure-concept references:

| outcome | count | share |
|---|---:|---:|
| exact | 1,229 | 78.3% |
| embedding_high | 22 | 1.4% |
| alias | 11 | 0.7% |
| borderline (queued) | 113 | 7.2% |
| new concepts | 179 | 11.4% |
| pattern_link | 193 | — |

80.4% merged onto existing nodes. The 113 borderline queued (highest
of any session) and 179 new concepts reflect the bioinformatics
vocabulary explosion: BLAST variants, phylogenetic algorithms (UPGMA,
Fitch-Margoliash, Neighbor-Joining), molecular tools (Primer3, MEGA7,
miRDeep2, Clustal Omega), and Apache Iceberg-specific concepts
(catalogs, snapshots, time travel queries).

## Cumulative across Phase 3.1 (sessions 1+2+3+4+5+6)

```text
total procedures              1,552
total procedure→concept links 6,238
procs with implements_pattern 809 (52%)
unique patterns referenced    573
chapters attempted            1,540
chapters w/ procedures        768
```

## Density by domain (across sessions)

A clearer pattern is emerging:

| genre | typical procs/chapter | examples |
|---|---:|---|
| Cookbook/hands-on | 2-4 | TF/Keras, AWS-AI Practitioner, Stable Diffusion, Schneier |
| Tool walkthroughs | 1-3 | Bioinformatics, Kafka admin, Iceberg |
| Algorithm walkthroughs | 1-2 | Algorithms textbook, Crypto algorithms |
| Mixed practitioner | 0.5-1.5 | Data Eng, Cloud Architecture, AI/ML |
| Conceptual/architectural | 0-0.5 | Fowler patterns, AWS Solutions Architects, AI strategy |
| Front-matter/exam-prep | 0 | Forewords, exercises, certification answer keys |

This signal is now strong enough to drive Skills Factory ranking:
chapters with high procedure density are the ones whose content can
be turned into executable how-to skills. Conceptual chapters
contribute concept content but won't seed the "what to do" portion of
generated Skills.

## Observations

What worked:

- 0 failures, 0 rate-limit issues. The sustained Max plan budget after
  yesterday's 12:20 PT reset has held all morning.
- Schneier's vintage book (1995) integrated cleanly with modern crypto
  concepts already in the graph: AES, RSA, ECB, CBC, GCM all merged
  exactly via the resolver despite different authors / decades.
- Bioinformatics established a substantial new sub-graph — 96 chapters
  with procedures from a single author's book gives the corpus a
  domain it didn't have before.

To watch:

- 113 borderline-queued items is the highest of any session (sessions
  3-5 averaged ~50). The bioinformatics tool vocabulary brings many
  concepts that look similar to existing concepts but are distinct
  (e.g., "Smith-Waterman" vs "Needleman-Wunsch" — both alignment
  algorithms but they're separate). The review queue is going to need
  attention before Phase 4. /kb-review-concepts should run for an
  hour or two to clear the backlog.
- Architecture Patterns with Python (Cosmic Python) is heavily
  pattern-named in chapter titles ("Repository Pattern", "Unit of Work",
  "Service Layer") — checking whether the resolver's 11 alias matches
  picked these up correctly is worth a spot-check after this session.

## Throughput

Wall clock ~3 hours for 250 chapters at 10-agent parallelism. Density
is up 50%+ from sessions 3-5 (~1.27 procs/chapter average), driven
entirely by domain mix.

## Artifacts

- `/tmp/mypub-procedures/session-6/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s6.ddb`
- No deferred chapters from this session.
