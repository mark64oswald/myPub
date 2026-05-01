# Phase 3.1 — Session 5 (2026-05-01, full)

Fifth procedure-extraction session. The plan's only mid-session
complication: Max plan rate limit hit at the start of wave 2; reset
arrived ~4h later and we resumed the same conversation. All 250
chapters landed in the end via two stretches separated by the
limit window.

## Scope

250 chapters via `extract_procedures.py prep --limit 250` (auto-resumed
nothing — session 4 was clean).

| book | chapters in session |
|---|---:|
| Advanced Programming in the UNIX® Environment, 3rd Ed | 32 |
| Analysis Patterns: Reusable Object Models (Fowler) | 24 |
| Advanced Web Metrics with Google Analytics | 23 |
| Advanced Web Metrics with Google Analytics, 2nd Ed | 21 |
| Agentic Architectural Patterns for Building Multi-Agent Systems | 20 |
| Analysis of Observational Health Care Data Using SAS | 18 |
| Agentic Mesh | 17 |
| Administrative Healthcare Data | 15 |
| All About Bioinformatics | 12 |
| Analytical Skills for AI and Data Science | 10 |
| (10 other books, total) | 78 |

## Dispatch

5 waves of 10 sub-agents × 5 chapters each. Wave 2's first dispatch
hit a Max plan rate-limit cap and all 10 sub-agents returned the limit
message instantly. After the 12:20 PT reset (arrived during this
conversation), wave 2 was redispatched and landed cleanly. Waves 3–5
then ran without further interruption.

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     50 |    56 | 1.12 |
|  2a  |         50 |      0 | (limit hit; deferred to 2b) | — |
|  2b  |         50 |     50 |    62 | 1.24 |
|    3 |         50 |     50 |    35 | 0.70 |
|    4 |         50 |     50 |    26 | 0.52 |
|    5 |         50 |     50 |    50 | 1.00 |

## Results — this session

```text
chapters dispatched   250
chapters landed       250 (100%)
procedures written    229
chapters w/ ≥1 proc   116 (46%)
chapters w/ 0 procs   134 (54%)
concept links written 950
pattern links written 111
```

Density 0.92 procs/chapter — slightly below sessions 3 and 4 (1.30,
1.24). The hit comes from heavily conceptual content this round:
Fowler's "Analysis Patterns" book is descriptive (24 chapters, mostly
0-proc), and Agentic Mesh / Agentic Architectural Patterns books are
similarly more about *describing* patterns than *executing* procedures.
The session's procedural counterweight came from UNIX systems
programming (Stevens, Ch 8 IPC was a standout) and SAS healthcare data
analysis (Ch 2 SAS Enterprise Guide had 5 procs).

Resolution mix on procedure-concept references:

| outcome | count | share |
|---|---:|---:|
| exact | 777 | 79.5% |
| embedding_high | 27 | 2.8% |
| alias | 13 | 1.3% |
| borderline (queued) | 61 | 6.2% |
| new concepts | 98 | 10.0% |
| pattern_link | 111 | — |

83.6% merged onto existing nodes. The 98 new concepts are primarily
UNIX system calls (`fork`, `exec`, `wait`, `sigaction`, `pthread_*`),
SAS/healthcare-specific names (HCPCS, CPT, REV, NDC codes), and
Snowflake/Snowpark API surface. Causal-inference methodology terms
(MSM, RMLPS, IPTW, structural nested models) also appeared as new.

## Cumulative across Phase 3.1 (sessions 1+2+3+4+5)

```text
total procedures              1,195
total procedure→concept links 4,705
procs with implements_pattern 616 (52%)
unique patterns referenced    428
chapters attempted            1,290
chapters w/ procedures        614
```

## Throughput

The mid-session rate-limit recovery worked exactly as designed:
- The `procedure_attempted_at` resumable-session column meant the
  failed wave-2 chapters were not lost; they simply weren't marked.
- After the limit reset, redispatching the same 10 batch prompts to
  fresh sub-agents produced clean results.
- The conversation persisted across the wait window, so no manifest
  regeneration or session-rebuild work was needed.

This validates the resumable design under in-session disruption (vs.
the cross-session recovery shown in sessions 2 → 3 and 3 → 4). Both
modes are now exercised.

## Observations

What worked:

- All 250 chapters landed across the disrupted dispatch.
- Resolver handled cross-edition merges of "Advanced Web Metrics with
  Google Analytics" 1st and 2nd editions: most chapter content shares
  vocabulary, and the resolver merged them onto common nodes.
- Healthcare-data chapters (Administrative Healthcare Data + SAS
  observational analysis) produced concrete procedures aligned with
  the user's day-job domain — these will be valuable for Skills
  Factory generation later.

To watch:

- Density variance is widening: session 4's wave 1 was 2.7
  procs/chapter (TF/Keras/AWS-AI hands-on); session 5's wave 4 was
  0.52 (Bioinformatics + Analysis Patterns). The Skills Factory will
  need a way to express "this concept has lots of procedures" vs.
  "this concept exists in the graph but has no executable procedures
  yet" — current data structure already supports it via the
  procedure_concept link table count, but ranking will need to weight
  this signal.
- Fowler's "Analysis Patterns" is essentially a catalog of named
  abstractions without walkthroughs. The procedure extractor produced
  near-zero output on those chapters, which is correct — but the
  *concept* content there is rich. A future pass might benefit from
  a "concept-only" extraction prompt for books like this where
  procedure extraction is a near-no-op.

## Artifacts

- `/tmp/mypub-procedures/session-5/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s5.ddb`
- No deferred chapters from this session.
