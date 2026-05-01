# Phase 3.1 — Session 2 (2026-04-30)

First multi-wave procedure-extraction session at scale, modeled on the
Phase 2.4 multi-session approach. Validates that the pipeline scales beyond
the small batch and exercises the resumable-session design under a real
rate-limit interruption.

## Scope

500 chapters selected by [scripts/extract_procedures.py](../scripts/extract_procedures.py)
prep with default parameters (alphabetical by book title, dedup by
content_hash, skip chapters already attempted, ≥500 chars). Selection
landed on three editions of "A Common-Sense Guide to Data Structures and
Algorithms":

- Java edition (Vol 1) — ~182 chapters
- Python edition (Vol 1) — ~199 chapters
- Python edition (Vol 2) — ~119 chapters

Heavy clustering on a single textbook trilogy by design — the alphabetical
sort starts with "A Common-Sense Guide…" three times. Future sessions
will work through the rest of the catalog the same way.

## Dispatch

5 waves of 20 parallel sub-agents × 5 chapters per agent = 100 chapters
per wave. Per-batch payloads ran ~17K chars each; sub-agents ran ~30 s
to ~3 min apiece.

| wave | dispatched | landed | procs |
|-----:|-----------:|-------:|------:|
|    1 |        100 |    100 |    58 |
|    2 |        100 |    100 |    73 |
|    3 |        100 |    100 |    51 |
|    4 |        100 |    100 |    73 |
|    5 |        100 |     90 |    54 |

Total wall clock: ~3.5 hours from prep through process. The Max plan
hit a 5-hour rolling limit at the end of wave 5: 2 of 20 batches (10
chapters: 523–527 and 558–562) returned the rate-limit error and wrote
no result files. By design these chapters keep `procedure_attempted_at
IS NULL` and have no procedure rows, so the next session's prep will
re-select them automatically.

## Results — this session

```text
chapters dispatched   500
chapters landed       490
chapters w/ ≥1 proc   222
chapters w/ 0 procs   268
procedures written    309
concept links written 1,143
pattern links written 211
```

Resolution mix on procedure-concept references:

| outcome | count | share |
|---|---:|---:|
| exact | 1,025 | 89.3% |
| embedding_high | 9 | 0.8% |
| alias | 11 | 1.0% |
| borderline (queued) | 34 | 3.0% |
| new concepts | 69 | 6.0% |
| pattern_link | 211 | — |

90.1% of concept references resolved to pre-existing Phase-2 nodes
(exact + embedding_high + alias). Pattern resolution is high because
algorithms textbooks invoke a small fixed catalog of named techniques
repeatedly — most procedures in this session implement a Greedy
Algorithm, Divide-and-Conquer, Memoization, etc. that already exist as
concepts.

## Cumulative (after Phase 3.1 session 1 + session 2)

```text
total procedures              339
total procedure→concept links 1,261
procs with implements_pattern 224 (66%)
unique patterns referenced    121
chapters attempted            545
chapters w/ procedures        236
```

## Top 12 patterns implemented

| pattern | procs |
|---|---:|
| Top-Down Recursion | 15 |
| Greedy Algorithm | 8 |
| Divide and Conquer | 7 |
| Magical Lookups | 5 |
| Adjacency List | 5 |
| Breadth-First Search | 5 |
| Memoization | 5 |
| Separate chaining | 5 |
| Sort-Then-Scan | 5 |
| Hash-Based Lookup | 4 |
| Change the Data Structure | 4 |
| Selection Sort | 4 |

The recurrence count for "Top-Down Recursion" (15) is partly an artifact
of the textbook trilogy — three editions of the same book each include a
chapter that walks through a top-down recursion procedure on different
problems. This is exactly the cross-edition merge story the EntityResolver
is designed for: 15 distinct procedure rows from 3 books all link to one
canonical Pattern concept.

## Observations

What worked:

- 20-agent parallelism was the sustained throughput limit before hitting
  the Max plan rolling cap. Future sessions should plan ≤ ~75 sub-agent
  invocations per session at this density to leave headroom.
- The resumable-session design caught the rate-limit interruption
  cleanly — no special handling, no manual recovery; the 10 unprocessed
  chapters will simply be re-prepped next session.
- Quality remained consistent across the textbook trilogy: each Java
  procedure has Python counterparts in the other two volumes, and the
  EntityResolver merged the concept references across editions (visible
  in the 89% exact-match rate).
- The high pattern-link density (68% of procedures) is appropriate for
  an algorithms textbook. Future sessions on operationally-flavored
  books (Hadoop ops, Kafka admin, etc.) will likely show lower pattern
  density and higher concept-link density per procedure.

To watch:

- One sub-agent had its `Write` tool denied mid-batch (w4-77, ch=444)
  and fell back to Bash heredoc successfully. The output landed and
  validated; the fallback is fine, but if it becomes common we may want
  to pre-permit the Write path for these tasks.
- A handful of chapters returned 5–6 procedures (against the prompt's
  "more than 5 is suspicious" caution). On inspection these are
  exercise-solutions chapters that walk through 6 distinct algorithms
  without a natural way to merge — the LLM correctly chose not to
  collapse them. May tune the cap upward in a future prompt iteration.

## Throughput notes for the full corpus

At this rate (~150 procedures + 4–5 hours / 500 chapters), the remaining
~12,400 unique-content-hash chapters extrapolate to roughly **25 more
sessions** of comparable size, or ~125 hours of agent wall time. Sessions
4 onward will hit more domain diversity (databases, web, ML, security,
etc.) so the procedure-density-per-chapter and pattern density-per-
procedure will both shift; the no-op rate may go up (more conceptual
content) or the procedures-per-chapter may go up (more ops/cookbook
content). Re-baseline after session 4.

## Artifacts

- `/tmp/mypub-procedures/session-2/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s2.ddb` (pre-session-2)
- Pending in next session: chapter_ids 523, 524, 525, 526, 527, 558,
  559, 560, 561, 562
