# Phase 2.4 — Session 1 (2026-04-18, session-p24-01)

First full-corpus extraction session. Validates the sub-agent driver at
scale and establishes a throughput baseline.

## Scope

100 chapters, selected as:

- Unique content blocks (dedup by `chapter.content_hash`)
- Not previously extracted (no sibling chapter has `concept_relation` rows)
- `LENGTH(content) >= 2000` to skip front-matter/sub-sub-sections
- Ordered alphabetically by book title

Coverage for this session:

- `"Looks Good to Me"` — 24 chapters
- `A Common-Sense Guide to Data Structures and Algorithms in JavaScript, Vol. 1` — 76 chapters

## Pre-session cleanup

Three books had empty DC:title metadata (sentinel "(blank)"):

- book_id=112 → Business Metadata - Capturing Enterprise Knowledge
- book_id=309 → Joe Celko's Analytics and OLAP in SQL
- book_id=497 → The Data Model Resource Book, Volume 3

Fixed by UPDATE to the catalog; indexer patched with filename fallback so
future ingests of "(blank)"-DC:title books get a real title.

## Dispatch

4 waves of sub-agents via Claude Code Task tool:

| wave | agents | chapters | wall clock |
|-----:|-------:|---------:|-----------:|
|    1 |      5 |       25 | ~175 s (slowest) |
|    2 |      5 |       25 |  ~66 s |
|    3 |      5 |       25 |  ~68 s |
|    4 |      5 |       25 |  ~90 s |

Total: **~5 min agent wall time** for 100 chapters. Average ~3s/chapter
at 10-agent parallelism (waves 3+4 ran concurrent); ~7s/chapter at
5-agent parallelism (waves 1+2). Wave 1 paid a cold-start penalty; 2+
showed the steady-state rate.

Python-side processing (resolver + DB writes) for all 100 results: ~30 s.

## Results — this session only

```
entities extracted   754
relations written    504
borderline queue    +46
embedding_high       +3
exact (cross-book) +285
new concepts      +420
```

38% of extraction candidates (285 / 754) matched pre-existing concepts via
exact name — the "common-sense DSA" book shares SWE vocabulary heavily
with the earlier SQL/ML/microservices sample.

## Cumulative corpus state

```
chapters extracted    126   (prev 35)
concepts             1197   (prev 731)
relations            1121   (prev 617)
review queue (pend)   131   (prev 85)
```

### Entity types

| type      | count |
|-----------|-----:|
| Concept   |  485 |
| Tool      |  217 |
| Technique |  199 |
| Pattern   |  144 |
| Algorithm |   86 |
| Framework |   66 |

Algorithm count roughly doubled (38 → 86) after pulling in a DSA book —
distribution is now more balanced than it was after session-2.

### Relation types

| type           | count |
|----------------|-----:|
| IMPLEMENTS     |   366 |
| REQUIRES       |   286 |
| CONTRASTS_WITH |   198 |
| CITES          |   152 |
| EXTENDS        |   119 |

All 5 types still well-represented.

## Books covered so far

| chapters | book |
|---------:|------|
| 74 | A Common-Sense Guide to Data Structures and Algorithms in JavaScript |
| 17 | "Looks Good to Me" |
| 10 | Learning SQL |
| 10 | Machine Learning Production Systems |
| 10 | Node.js Design Patterns |
|  1 | Kubernetes: Up and Running |
|  1 | Kafka: The Definitive Guide |
|  1 | Designing Data-Intensive Applications |
|  1 | Building Event-Driven Microservices |
|  1 | The Data Warehouse Toolkit |

## Full-corpus progress

```
unique content blocks at threshold=2000:  10,348 total
  already extracted (direct or sibling):     100 (this session)
                                              +35 (earlier sample work)
  remaining:                                10,213
```

Call it roughly **10,250 blocks to go**. At this session's 3s/chapter
rate with 10-agent parallelism, that's ~9 hours of agent wall clock —
doable in ~10 sessions of 1,000 blocks each, or fewer larger sessions.

## Observations

- Content-hash dedup is working as designed: 1,197 concepts spread across
  126 chapters ≈ 9.5 concepts/chapter, up from 117 across 35 (3.3).
  Books with more unique content yield more concepts per chapter.
- Review queue grew from 85 → 131 (+46). Once Phase 2.5 is built,
  running `/kb-review-concepts` will collapse many of these into alias
  registrations.
- No sub-agent failures. No schema violations. Zero API charges.

## Next session

Likely target: 500–1,000 blocks. At wave-of-10 throughput that's ~30–60
min of agent time, plus 2–5 min Python post-processing. If rate limits
hold, a single session could probably do 1,000 blocks; better to pause
and reassess at 500 to confirm behavior stays clean.
