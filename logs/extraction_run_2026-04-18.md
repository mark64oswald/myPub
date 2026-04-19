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

---

# Phase 2.4 — Session 2 (2026-04-18, session-p24-02)

1,000 content blocks via the same prep → sub-agent → process pipeline.

## Scope

1,000 unique content blocks at `LENGTH(content) >= 2000`, dedup'd by
`content_hash`, skipping any hash already extracted in earlier sessions,
ordered alphabetically by book title. Produced 100 batches of 10
chapters each.

## Dispatch

Target throughput was waves of 10 concurrent sub-agents. Rate-limit
interruption split the run into three phases:

| phase   | waves (10 agents ea.)              | chapters     | notes                      |
|---------|------------------------------------|--------------|----------------------------|
| initial | waves 1–5 + partial wave 6         | ~577 written | rate-limit hit mid wave 6  |
| resume  | waves 7–11 (after 11am reset)      | +210         | straight-through           |
| tail    | waves 12–14 + a 3-chapter finisher | +213         | covers batches 80–100      |

Two quirks worth naming:

- **Phantom success:** one wave-4 agent and one rebatch-43 agent
  reported `processed: 10, written: 10` (or `3/3`) but did not actually
  write the result files. Verified by file-count after each wave. The
  missing result paths got re-dispatched and written cleanly. Lesson:
  trust file count, not sub-agent self-report.
- **VSS load missing on process path:** the sub-agent driver's
  `process` step hit `_duckdb.Error: ... unknown index type 'HNSW'` on
  the first INSERT into `concept_embedding`, the same extension-scope
  bug commit ba4857a fixed in `resolve_concept`. Patched
  `scripts/extract_batch.py` to `LOAD vss` immediately after connect;
  re-ran `process` cleanly.

Wave wall-clock: slowest agent 60–450s depending on chapter density.
Total agent wall time for the full session was dominated by the
rate-limit gap; useful agent-time was ~40 min.

## Results — this session only

```
entities extracted  12,164
relations written    8,274
resolution counts:
  exact            6,011   (cross-book concept reuse)
  new              5,269
  borderline         813   (added to review queue)
  embedding_high      71
```

60% of extraction candidates (6,011 / ~11,351 resolved entities)
matched an existing concept by exact name. That's a sharp jump from
session 1's 38% — the corpus is now broad enough that each new chapter
mostly sees concepts it's seen before.

## Cumulative corpus state

```
chapters extracted    992   (prev  126)   +866
concepts             7,278  (prev 1,195)  +6,083
relations            9,395  (prev 1,121)  +8,274
review queue (pend)    939  (prev   131)  +808
```

### Entity types

| type      |  count | share |
|-----------|-------:|------:|
| Concept   |  2,936 | 40.3% |
| Tool      |  1,541 | 21.2% |
| Technique |  1,104 | 15.2% |
| Pattern   |    687 |  9.4% |
| Framework |    528 |  7.3% |
| Algorithm |    482 |  6.6% |

Distribution looks healthy. Tool/Framework share climbed as we pulled
in a lot of cloud-certification and AI-engineering books in the
alphabetical traversal (AWS, APIs, AI Agents).

### Relation types

| type           | count |
|----------------|-----:|
| REQUIRES       | 2,715 |
| IMPLEMENTS     | 2,656 |
| CITES          | 1,499 |
| CONTRASTS_WITH | 1,274 |
| EXTENDS        | 1,251 |

All 5 types well-represented; REQUIRES overtook IMPLEMENTS at the
corpus scale, which tracks — once a chapter has named its specific
tools and patterns, the cross-chapter structure is prerequisite
chains.

## Books covered so far (top 10)

| chapters | book |
|---------:|------|
| 193 | A Common-Sense Guide to DSA in JavaScript |
| 183 | A Common-Sense Guide to DSA in Python, Vol. 1 |
| 139 | A Common-Sense Guide to DSA in Python, Vol. 2 |
|  60 | AI Agents and Applications |
|  30 | API Design Patterns |
|  23 | Advanced Programming in the UNIX Environment |
|  20 | AI and Machine Learning for Coders |
|  20 | AI and ML for Coders in PyTorch |
|  20 | AI Systems Performance Engineering |
|  19 | AWS Certified Cloud Practitioner Study Guide |

Duplicate DSA titles (JavaScript/Python v1/v2) correctly passed the
content-hash dedup as distinct books — they share structure but the
prose is different per language edition.

## Full-corpus progress

```
unique content blocks at threshold=2000:  10,348 total
  already extracted (direct or sibling):    992 (cumulative)
                                             +35 (earlier sample work)
  remaining:                                9,221
```

So ~9,200 blocks to go. At session-2's observed throughput (~1,000
blocks per ~40 min of useful agent time + one rate-limit gap), full
corpus is probably 8–10 more sessions.

## Observations

- Verify written files after each wave — two agents falsely reported
  success this session. File count is authoritative.
- VSS must be loaded anywhere we INSERT/UPDATE/DELETE on
  `concept_embedding`. `extract_batch.py` now does this; other future
  writers should mirror the pattern.
- Cross-book exact matches (6,011) are the most important number: it
  means the resolver is knitting the graph together instead of each
  book creating its own island of concepts.
- Review queue at 939 pending. Running `/kb-review-concepts` after
  each session would keep it from getting unwieldy; most items resolve
  in seconds (alias registration).

## Next session

Target ~1,000 blocks again. Same pipeline. Keep the post-wave
file-count check (catch phantom-success early). Expect the exact-match
ratio to keep climbing as the graph densifies.

---

# Phase 2.4 — Session 3 (2026-04-18, session-p24-03)

Another 1,000 content blocks. Ran into two rate-limit windows; resumed
through both. Also closed a long-standing bug that was wasting quota
on front-matter.

## Fix landed this session

- Added `chapter.extraction_attempted_at` timestamp column. Prep now
  skips chapters whose content_hash has any sibling where that column
  is set OR where `concept_relation` rows exist — the old skip logic
  only checked the latter, so chapters whose extraction produced zero
  relations (front-matter, TOCs, short intros) came back to the top
  of every subsequent session's pool.
- Backfilled the new column from session-1 and session-2 manifest
  files on disk (1,099 chapters marked attempted before session 3's
  prep re-ran).
- Re-ran prep with the fix; verified 0 overlap with prior sessions
  (was 128/1000 = 12.8% on the first prep before the fix).

## Dispatch

Three rate-limit resets, three prep phases:

| phase   | resets after        | chapters written | notes                                 |
|---------|---------------------|------------------|---------------------------------------|
| initial | ~200 in first push  | 200              | waves 1–2 clean, wave 3 hit 4pm limit |
| resume  | after 4pm reset     | +700             | waves 4–9 steady, wave 10 hit 9pm     |
| tail    | after 9pm reset     | +100             | finished wave 10                      |

Two phantom-success events (agents reporting `processed: 10, written:
10` without writing files) caught by the post-wave file check.
Re-dispatched cleanly.

## Results — this session only

```
entities extracted  15,712
relations written   10,700
resolution counts:
  exact            6,357   (cross-book reuse)
  new              8,284
  borderline         971
  embedding_high     100
```

Still ~41% exact (6,357 / 15,712). Slightly lower share than session 2
(60%) — because session 3 crossed into genome/bioinformatics and text
analytics territory, which brings in a lot of domain vocabulary the
earlier corpus didn't have.

## Cumulative corpus state

```
chapters with relations  1,822   (prev 992)     +830
chapters attempted       2,099   (prev 1,099)   +1,000
concepts                16,533   (prev 7,278)   +9,255
relations               20,095   (prev 9,395)   +10,700
review queue (pend)      1,910   (prev 939)     +971
```

### Entity types (cumulative)

| type      |  count | share |
|-----------|-------:|------:|
| Concept   |  6,477 | 39.2% |
| Tool      |  3,519 | 21.3% |
| Technique |  2,639 | 16.0% |
| Pattern   |  1,680 | 10.2% |
| Algorithm |  1,171 |  7.1% |
| Framework |  1,047 |  6.3% |

### Relation types (cumulative)

| type           | count |
|----------------|-----:|
| REQUIRES       | 6,069 |
| IMPLEMENTS     | 5,639 |
| CITES          | 3,135 |
| EXTENDS        | 2,681 |
| CONTRASTS_WITH | 2,571 |

Distribution remains stable across sessions. Good sign the extraction
prompt isn't drifting as corpus breadth grows.

## Full-corpus progress

```
unique content blocks at threshold=2000:  ~10,348
  attempted so far (sessions 1–3):           2,099  (20%)
  remaining:                                  ~8,249
```

~8 more sessions at this pace.

## Next session

- Same 1,000-block target.
- With `extraction_attempted_at` landed, prep never re-queues attempted
  content. Fresh sessions just pick up alphabetically where the last
  left off.
- Review queue is past 1,900 — consider an interactive
  `/kb-review-concepts` pass soon to collapse obvious aliases before
  it grows further.

