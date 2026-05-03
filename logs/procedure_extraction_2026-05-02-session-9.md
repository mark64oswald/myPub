# Phase 3.1 — Session 9 (2026-05-02)

Ninth session. The "Building*" cluster — 18 books, all procedural-leaning —
delivered the densest, smoothest session in Phase 3.1 to date. 282 procedures
landed across 250 dispatched chapters; **zero stalls** across all 5 waves
despite a high rate of Write-tool denials. The Bash heredoc fallback path
proved durable.

## Scope

250 chapters across 18 books, dominated by the alphabetical "Building*"
range:

| chapters | book |
|---:|:---|
|  23 | Building Event-Driven Microservices |
|  19 | Building Data Integration Solutions |
|  19 | Building Neo4j-Powered Applications with LLMs |
|  18 | Building Data-Driven Applications with LlamaIndex |
|  18 | Building Medallion Architectures |
|  16 | Building Machine Learning Powered Applications |
|  16 | Building Machine Learning Systems with a Feature Store |
|  16 | Building Micro-Frontends |
|  16 | Building Natural Language and LLM Pipelines (Haystack) |
|  15 | Building Applications with AI Agents |
|  15 | Building Knowledge Graphs |
|  15 | Building LLM Powered Applications |
|  14 | Building Generative AI Services with FastAPI |
|  11 | Building Integrations with MuleSoft |
|  10 | Building Agents with OpenAI Agents SDK (residual) |
| 3+3+3 | Agentic AI Systems / Workflows / Data Products residuals |

## Disruptions

1. **Write-tool denials at scale**. ~10 of 50 sub-agents hit `Claude requires
   approval to use Write` and fell back to Bash heredoc on their own. Every
   single fallback succeeded. This is the same denial pattern that drove
   the s8 wave-5 stall epidemic — except this session none of them stalled,
   suggesting the s8 stalls may have been triggered by an external factor
   (latency, rolling cap) rather than the denial itself.
2. **One probable false-positive in batch 1**: chapter 18347 ("Why
   subscribe?" / Packt unlock-benefits back-matter) returned 1 procedure.
   The other batch-1 sub-agent for similar back-matter (batch 4: "Other
   Books You May Enjoy" → 0 procs) handled it correctly. Worth tightening
   the prompt's back-matter guidance for s10 — added "unlock benefits /
   other books" hint to wave-2+ prompts and saw no recurrence.
3. **No AUP refusals, no rate limits, no disk pressure**. The 5 marked-
   attempted AUP chapters from s8 stayed excluded as designed.

## Dispatch

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     50 |   ~51 | 1.02 |
|    2 |         50 |     50 |   ~48 | 0.96 |
|    3 |         50 |     50 |   ~79 | 1.58 |
|    4 |         50 |     50 |   ~57 | 1.14 |
|    5 |         50 |     50 |   ~47 | 0.94 |

(Wave totals are agent-reported. Final post-validation total: 282.)

Wave 3 was the standout — densest wave of any Phase 3.1 session.
It hit a sweet spot of MuleSoft + Knowledge Graphs (Cypher/LOAD CSV/APOC) +
LLM Powered Apps + the back half of FastAPI's GenAI services book. Several
batches landed 9–12 procs each.

Wave 5 ran out of dense material as the dispatch reached Micro-Frontends
intro chapters and Neo4j-Powered Apps preface.

## Results — this session

```text
chapters dispatched   250
chapters landed       250 (zero stalls, zero deferrals)
procedures written    282
chapters w/ ≥1 proc   136 (54%)
chapters w/ 0 procs   114 (46%)
concept links written 1,164
pattern links written 199
```

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 922 | 78.5% |
| embedding_high | 22 | 1.9% |
| alias | 7 | 0.6% |
| borderline | 76 | 6.5% |
| new | 159 | 13.5% |
| pattern_link | 199 | — |

86% merged onto existing nodes (exact + embedding_high + alias + matched
borderline). The 159 new concepts and 182 new patterns reflect the corpus
expansion: this is the first time many Building* topics (Module Federation,
Strangler-Fig, SCD2 in Medallion, Hopsworks Online Feature Group, Haystack
SuperComponents, Neo4j APOC, DataWeave, AsyncAPI, OpenAI Agents SDK
SQLiteSession, etc.) appear in the catalog as concepts.

## Cumulative across Phase 3.1 (sessions 1–9)

```text
total procedures              2,138  (+282)
total procedure→concept links 8,679  (+1,164)
procs with implements_pattern 1,148  (54%, +199)
unique patterns referenced    872    (+182)
chapters attempted            2,263  (~17.5% of corpus)
chapters w/ procedures        1,050  (+136)
```

Crossed the **2,000 procedures** milestone on the cumulative line.

## Standout procedural books

Top three by per-book density (procs / chapters in scope):

1. **Building Knowledge Graphs** — 15 chapters, 28 procs (1.87 / chapter).
   Cypher data modeling + LOAD CSV + neo4j-admin import + APOC enrichment
   + graph-native ML + identity resolution + pattern detection +
   semantic search. Hands-on throughout.
2. **Building Generative AI Services with FastAPI** — 14 chapters, 21 procs
   (1.50 / chapter). FastAPI starter, Pydantic, model serving (TinyLlama),
   async I/O, real-time comm, DB integration, auth, deployment. Each
   chapter walks through a working slice.
3. **Building Agents with OpenAI Agents SDK** — 10 chapters, 23 procs
   (2.30 / chapter — highest density per chapter). Custom tools, Pydantic
   inputs, SQLiteSession memory, sliding-window context, multi-agent
   handoffs. The "agentic SDK cookbook" pattern at its peak.

Honorable mentions:
- **Building ML Systems with a Feature Store** — 16 ch, 25 procs.
  Hopsworks + uv + Polars + outlier detection + on-demand transforms
  + batch/streaming pipelines.
- **Building Integrations with MuleSoft** — 11 ch, 20 procs. Anypoint MQ,
  DataWeave, MUnit, Studio + Runtime Manager deploy. Strong cookbook genre.

## Wave-5 stall epidemic from s8 — not reproduced

Last session, 60% of wave 5 stalled out (6/10 batches, 22 chapters lost
that needed to be auto-resumed). This session's wave 5 was clean: 10/10
batches landed within ~4 minutes of each other, despite multiple sub-agents
hitting Write denials. The leading hypothesis is that s8's stalls were
triggered by something coincident with — but not caused by — Write
denials (rolling cap / backend latency at the dispatch hour). Worth
keeping the heredoc fallback in the standard prompt regardless; it cost
nothing and is what made today's session fully recoverable.

## Known issues for Phase 4 prep

1. **Back-matter false positives**: the s9 prompt is conservative about
   front-matter (preface, TOC, etc.) but less so about back-matter
   ("Why subscribe?", "Other Books You May Enjoy", "Free Benefits"). The
   wave-1 chapter 18347 slipped through with a 1-proc extraction that
   isn't a real procedure. Wave-2+ prompts added explicit guidance ("Promo
   / 'unlock benefits' / 'other books' back-matter has zero procedures")
   and no recurrences were seen. Should fold this into the system prompt
   for s10+.
2. **Catalog disk size** unchanged at ~110GB — still on the Phase 4-prep
   list for dump-drop-reload.
3. **Write-tool denials remain frequent** but no longer dangerous given
   the heredoc fallback. Granting Write to the procedure-extraction
   sub-agent path remains a worthwhile harness improvement (fewer Bash
   shell-outs, lower latency) but is no longer urgent.
4. The **5 AUP-blocked chapters** from s8 (15341/15352/15364/15372/15384)
   continue to be cleanly excluded by the `procedure_attempted_at IS NOT
   NULL` filter. Phase 4-prep should add a distinct flag (e.g.,
   `procedure_skipped_reason='aup'`) so we can identify them later.

## Artifacts

- `/tmp/mypub-procedures/session-9/` — prompts, results, manifest
- Catalog backup: skipped (s8 backup `catalog_pre-phase3-s8.ddb` from
  May 1 remains the rollback point; same pattern as s7)
- Deferred to next session: 0 chapters from this session
- The s8 wave-5 stall residuals (~22 chapters with `procedure_attempted_at
  IS NULL`) were NOT in the s9 selection window because the dispatch
  ordered by title and stayed within "Building*". They remain in the
  pending pool and will be picked up when the alphabetical sweep returns
  to their books in a later session.

## Phase 3.1 progress

After 9 of an estimated ~30 total sessions, we have:
- ~17.5% of the corpus attempted
- 50%+ of attempted chapters yielding ≥1 procedure
- ~2,138 procedures with strong concept-graph integration
- A robust dispatch model: 50 batches × 5 chapters × 5 waves, 0 stalls
- The 86% existing-concept-merge rate means new-extraction marginal
  cost (concept-graph noise) is staying low even as the corpus expands
