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

---

# Phase 2.4 — Session 4 (2026-04-19, session-p24-04)

Fourth full-corpus push — another 1,000 content blocks. One rate-limit
interruption mid-wave-8, clean recovery after the 11am reset.

## Dispatch

10 waves of 10 concurrent sub-agents, alphabetical continuation from
"Crucial Conversations" through "Data Modeling with Snowflake".

| phase     | waves                            | chapters written | notes                            |
|-----------|----------------------------------|------------------|----------------------------------|
| initial   | waves 1–7                        | 700              | straight through, no incidents   |
| stall     | wave 8 (partial)                 | +75              | rate-limit hit mid-wave          |
| resume    | wave 8b (3 consolidated agents)  | +25              | stragglers after 11am reset      |
| continued | wave 9 (dispatched with 8b)      | +100             | 13 agents concurrent             |
| tail      | wave 10                          | +100             | final batches 91–100             |

No phantom-success events this session — every agent that reported
`written: N` actually wrote N files. Post-wave file counts all matched
cumulative expectations at 100 / 200 / 300 … / 900 / 1,000.

## Results — this session only

```
entities extracted  16,881
relations written   12,458
resolution counts:
  exact            7,972   (cross-book reuse)
  new              7,663
  borderline       1,190
  embedding_high      56
```

Exact-match ratio 47% (7,972 / 16,881) — between session 2's 60% and
session 3's 41%. This session crossed into "Data*" territory (Data
Architecture, Data Contracts, Data Engineering, Data Governance, Data
Mesh), which is dense with domain-specific vocabulary but also shares
heavy overlap with the earlier corpus on patterns and infra concepts.

## Cumulative corpus state

```
chapters with relations  2,750   (prev 1,822)  +928
chapters attempted       3,099   (prev 2,099)  +1,000
concepts                25,386   (prev 16,533) +8,853
relations               32,553   (prev 20,095) +12,458
review queue (pend)      3,100   (prev 1,910)  +1,190
```

### Entity types (cumulative)

| type      |  count | share |
|-----------|-------:|------:|
| Concept   | 10,416 | 41.0% |
| Tool      |  4,591 | 18.1% |
| Technique |  4,469 | 17.6% |
| Pattern   |  2,977 | 11.7% |
| Framework |  1,530 |  6.0% |
| Algorithm |  1,403 |  5.5% |

### Relation types (cumulative)

| type           | count |
|----------------|-----:|
| REQUIRES       | 9,979 |
| IMPLEMENTS     | 9,409 |
| CITES          | 4,864 |
| EXTENDS        | 4,324 |
| CONTRASTS_WITH | 3,977 |

All five types well-balanced; REQUIRES still leads, consistent with
sessions 2–3.

## Books covered so far (top 10)

| chapters | book |
|---------:|------|
| 193 | A Common-Sense Guide to DSA in JavaScript |
| 183 | A Common-Sense Guide to DSA in Python, Vol. 1 |
| 139 | A Common-Sense Guide to DSA in Python, Vol. 2 |
| 123 | Beautiful Data |
| 122 | Business Metadata - Capturing Enterprise Knowledge |
|  60 | AI Agents and Applications |
|  53 | Basic Applied Bioinformatics |
|  43 | Data Architecture |
|  40 | Bioinformatics and Functional Genomics |
|  37 | Clean Architecture |

## Full-corpus progress

```
unique content blocks at threshold=2000:  10,203
  attempted so far (sessions 1–4):         2,607  (25.6%)
  remaining:                               7,596
```

About **7,600 blocks to go**, ~7–8 more sessions at this pace.

## Observations

- Rate-limit recovery is smooth: `extraction_attempted_at` means a
  partial wave's successful writes stay written. We just re-identify
  which chapter_ids in the session pool lack result files and
  re-dispatch exactly those. In this session that was 25 stragglers
  from wave 8 plus the 200 blocks from waves 9–10.
- The "consolidated stragglers" pattern (3 agents handling 8–9
  chapters each) works well — no need to keep a one-to-one
  batch-to-agent mapping during recovery.
- Review queue now at 3,100 pending (+1,190). Crossing the threshold
  where `/kb-review-concepts` should run before session 5, else the
  queue starts drowning out real borderline cases.
- Coverage distribution shifted toward data-centric books this
  session — tracks the alphabetical traversal. Next session will
  start at "Data Modeling" and walk through the rest of the "D" books.

## Next session

- Same 1,000-block target.
- **Run `/kb-review-concepts` first.** Collapse 3,100 borderline
  matches into alias registrations or keep-separate decisions before
  adding another ~1,200 items to the queue. At the current growth
  rate, deferring once more would land session 6 at ~4,300 pending —
  well past manageable.
- Pool picks up alphabetically from "Data Modeling with Snowflake"
  onward.

---

# Phase 2.4 — Review pass + Session 5 (2026-04-19 / 04-20)

## Review pass (kb-review-concepts, 380 items)

Partial drain of the 3,100-pending borderline queue before session 5.
Interactive review pass from highest-similarity items downward, stopped
at sim≈0.858 after 380 decisions.

```
queue:  3,100 pending → 2,720 pending (-380, 12% cleared)
  159 aliases registered
  226 kept separate
  0 renames (not used without explicit direction)
graph: 25,386 concepts → 25,228 concepts (-158 from merges)
```

**Alias patterns (dominant):**
- Abbreviation expansions (CI/CD, PCI DSS, PHI, TDD, OLTP, TF-IDF, RAG, CMMI)
- Singular/plural (Connectors, Diffusion Models, Description Logics, Vector stores)
- Hyphen/case variants (Dead-Letter Queue, 3D-QSAR, Gray-Level Run Length Matrix)
- Parent-brand synonyms (AWS IAM ↔ AWS Identity and Access Management, Amazon
  Kinesis ↔ AWS Kinesis, Amazon MSK ↔ Amazon Managed Streaming for Apache Kafka)
- Suffix variants ("Data Ingestion Pipeline" ↔ "Ingestion Pipeline")

**Keep-separate patterns (dominant):**
- Paired opposites (Input/Output Port, grid-row/grid-column, Selection/Insertion
  Sort, SOCK_STREAM/TCP where one is the POSIX constant and one the protocol)
- Sibling variants (EBS gp2/gp3, Graviton2/3, SIS/SIR Model, FP4/FP8 Quantization,
  Smith-Waterman/Needleman-Wunsch, PaLM/LaMDA)
- Competitor tools (Puppet/Chef, Superset/Tableau, Fortify/Checkmarx, seaborn/matplotlib)

**Biggest graph impacts (edge migrations on merge):**
- `RAG` ← `Retrieval-Augmented Generation (RAG)`: 25 edges
- `Lakehouse` ← `Lakehouse Architecture`: 20 edges
- `Diffusion Models` ← `Diffusion Model`: 20 edges
- `Data processing pipeline` ← `Data Pipeline`: 20 edges
- `Vector Database` ← `VectorDB`: 15 edges

## Session 5 dispatch (session-p24-05)

1,000 blocks covering "Data Modeling with Snowflake" → "Facilitating
Software Architecture" (alphabetical continuation).

| phase    | waves | chapters written | notes                                      |
|----------|-------|------------------|--------------------------------------------|
| wave 1   | 1     | 93 → then 7 redo | **dispatch bug**: fabricated IDs for 7/10  |
| waves 2–9 | 8    | 800              | clean, no incidents                        |
| wave 10  | partial | 50             | rate-limit hit late evening                 |
| recovery | 5 consolidated agents | +50 | resumed after reset, session completes    |

**Dispatch bug caught in wave 1.** I printed the first 3 and last 3
batches from the manifest but made up the IDs for batches 4–10 by
extrapolation. Five of those agents reported `written: 0/1/2` because
the prompts didn't exist on disk. Fixed by reading the manifest
directly for every wave thereafter. Two stray writes from the bad
dispatches (31790, 32715) landed in chapters that were in later
batches — harmless overwrites when those waves ran.

## Results — this session only

```
entities extracted  17,243
relations written   12,412
resolution counts:
  exact            9,328   (cross-book reuse)
  new              6,804
  borderline       1,019
  embedding_high      52
  alias               40   (auto-resolved from review pass aliases)
```

**The `alias` column is new.** Those 40 extractions matched names we
registered as aliases during the review pass earlier today. Instead
of queuing as borderline or creating duplicates, the resolver collapsed
them on sight. Exactly the payoff that justifies the review work.

Exact-match ratio climbed to **54%** (9,328 / 17,243) — highest so
far. The graph has matured enough that more than half of new
extractions match existing concepts by name alone.

## Cumulative corpus state

```
chapters with relations  3,664   (prev 2,750)   +914
chapters attempted       4,099   (prev 3,099)   +1,000
concepts                33,051   (prev 25,228)  +7,823
relations               44,965   (prev 32,553)  +12,412
review queue (pend)      3,739   (prev 2,720)   +1,019
```

### Entity types

| type      |  count | share |
|-----------|-------:|------:|
| Concept   | 13,837 | 41.9% |
| Technique |  6,111 | 18.5% |
| Tool      |  5,493 | 16.6% |
| Pattern   |  3,900 | 11.8% |
| Framework |  2,034 |  6.2% |
| Algorithm |  1,676 |  5.1% |

Technique overtook Tool this session — tracks with the subject
matter (CSS, JavaScript, math, molecular biology, TypeScript — all
technique-heavy domains).

### Relation types

| type           | count |
|----------------|-----:|
| REQUIRES       | 13,921 |
| IMPLEMENTS     | 13,026 |
| CITES          |  6,342 |
| EXTENDS        |  5,998 |
| CONTRASTS_WITH |  5,678 |

## Full-corpus progress

```
unique content blocks at threshold=2000:  10,203
  attempted so far (sessions 1–5):         3,596  (35.2%)
  remaining:                               6,607
```

**About 6,600 blocks left, ~6–7 more sessions at this pace.**

## Observations

- **Alias registry pays off immediately.** 40 auto-resolutions in the
  first session after the review pass. That's 40 items that would
  otherwise have gone into the queue or duplicated existing concepts.
- **Dispatch-from-manifest is now mandatory.** The wave-1 bug wasted
  ~7 agent dispatches. The fix is trivial — always read the manifest
  for the batch IDs, never extrapolate.
- **Rate-limit recovery is now routine.** Fifth session, second
  rate-limit hit mid-wave. `extraction_attempted_at` + file-count
  gap detection make recovery ~5 minutes of setup.
- Graph neighborhood is starting to look dense around widely-shared
  concepts. Next-step intuition: start checking graph connectivity
  before session 7 (Phase 2 quality eval), not after the full corpus
  is in.

## Next session

- Same 1,000-block target.
- Pool picks up from "Facilitating Software Architecture" onward
  (starts in the "F" and "G" range).
- Review queue back at 3,739 — another `/kb-review-concepts` pass
  probably worth doing before session 6. This time the first ~380
  items are already resolved, so we'd be starting from sim≈0.858
  rather than 0.899.

---

# Phase 2.4 — Session 6 (2026-04-24, session-p24-06)

1,000 content blocks via the usual prep → sub-agent → process pipeline.
Pool starts at "Facilitating Software Architecture" and runs through
"Hands-On Entity Resolution." 100 batches of 10, dispatched as 10 waves
of 10 concurrent sub-agents.

## Dispatch

| phase   | waves (10 agents ea.)              | chapters     | notes                      |
|---------|------------------------------------|--------------|----------------------------|
| initial | waves 1–4                          | 400 written  | clean                      |
| stall   | wave 5                             | 47 written   | rate-limit hit mid-wave    |
| resume  | 6 agents covering gap (after 11:40am reset) | +53 | reads manifest IDs only    |
| tail    | waves 6–10                         | +500         | straight-through, no hiccups |

Wave 5 hit a 5-hour usage cap about 2–3 minutes into the wave. Every
sub-agent in the wave had written at least the first 4–5 chapters in
its batch before the limit triggered, so partial progress was salvaged
by recovering the 53 missing IDs from `ls` + manifest diff, then
dispatching 6 resume agents with 8–9 IDs each. No duplicate writes, no
lost chapters.

Recovery was ~3 minutes of setup once the limit reset. The
dispatch-from-manifest discipline from session 5 held up: didn't need
to extrapolate IDs for the resume wave either.

Useful agent-time (minus the ~10-minute rate-limit gap and the
post-reset resume wave): ~55 min. Process step ran in ~2 minutes on
1,000 result files.

## Results — this session only

```
entities extracted  17,849
relations written   13,815
resolution counts:
  exact            9,359   (cross-book reuse)
  new              7,144
  borderline       1,231
  embedding_high      92
  alias               23
```

Exact-match ratio: **52.4%** (9,359 / 17,849). Held roughly steady
vs. session 5's 54% — still above half the extractions matching
existing concepts by name alone.

**Alias auto-resolutions fell (40 → 23).** Expected: the review pass
earlier registered aliases for names that had already shown up as
borderline, and those names mostly came from the books already
processed. New books in session 6 introduce fresh vocabulary, so
fewer hits against the existing alias registry. The 23 that did
resolve are still a pure win vs. adding them to the queue.

`embedding_high` jumped back up (52 → 92). Suggests the corpus is
broad enough now that near-duplicate concepts under different spellings
are surfacing in the embedding-high band more often than before.

## Cumulative corpus state

```
chapters with relations  4,585   (prev 3,664)   +921
chapters attempted       5,099   (prev 4,099)   +1,000
concepts                41,426   (prev 33,051)  +8,375
relations               58,780   (prev 44,965)  +13,815
review queue (pend)      4,970   (prev 3,739)   +1,231
```

79 of the 1,000 attempted chapters had empty entity/relation lists
(front-matter, indices, exercise appendices). Normal — same ratio
as earlier sessions.

### Entity types

| type      |  count | share |
|-----------|-------:|------:|
| Concept   | 17,630 | 42.6% |
| Technique |  7,613 | 18.4% |
| Tool      |  6,776 | 16.4% |
| Pattern   |  4,904 | 11.8% |
| Framework |  2,560 |  6.2% |
| Algorithm |  1,943 |  4.7% |

Shares stable across the last three sessions. The corpus has reached
a stationary type distribution — each new 1,000-block batch adds
proportionally rather than reshaping the mix.

### Relation types

| type           |  count |
|----------------|-------:|
| REQUIRES       | 17,995 |
| IMPLEMENTS     | 17,294 |
| CITES          |  8,217 |
| EXTENDS        |  7,693 |
| CONTRASTS_WITH |  7,581 |

REQUIRES and IMPLEMENTS are within 700 of each other — effectively
tied as the dominant relation pair.

## Full-corpus progress

```
unique content blocks at threshold=2000:  10,203
  attempted so far (sessions 1–6):         4,568  (44.8%)
  remaining:                               5,635
```

**Roughly 5,600 blocks remaining, ~5–6 more sessions** at the current
1,000-per-session pace. The back half of the alphabet likely contains
more dense technical books (H–Z has DDD, ML, streaming, systems
programming, etc.), so per-chapter extraction counts may climb.

## Observations

- **Rate-limit recovery is now a ~3-minute chore**, not a session
  blocker. Wave-5 stalled at 11:35am-ish, reset at 11:40am, resume
  wave dispatched by 11:50am. The persistent `data/extraction-sessions/`
  symlink meant no state reconstruction was needed.
- **Manifest-read-every-wave continues to pay off.** Wave 5's recovery
  needed missing-chapter IDs that the manifest held directly — no
  extrapolation tempt, no wasted dispatches.
- **Stationary type distribution** is a mild surprise. Expected each
  new domain to skew the mix; instead, the ratio Concept / Technique /
  Tool / Pattern / Framework / Algorithm is stable enough to guess
  next session's counts by simple multiplication.
- Review queue at 4,970 pending. Worth another `/kb-review-concepts`
  pass before session 7 — the alias payoff per review minute should
  be higher now than last pass, since the cumulative corpus is larger.

## Next session

- Same 1,000-block target.
- Pool picks up around "Hands-On" → early "H-range" books.
- Strongly consider a review-concepts pass first; 4,970 pending is
  the largest queue since sessions began.

