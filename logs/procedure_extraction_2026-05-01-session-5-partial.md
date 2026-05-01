# Phase 3.1 — Session 5 (partial, 2026-05-01)

Partial session — Max plan rate limit hit immediately at the start of
wave 2. Wave 1 (50 chapters) landed; waves 2–5 (200 chapters) deferred
to next session.

## Scope

250 chapters across 20 books. New territory: Advanced Programming in
the UNIX Environment (32 ch), Analysis Patterns: Reusable Object Models
(24 ch), Advanced Web Metrics with Google Analytics (44 ch across two
editions), Agentic Architectural Patterns (20 ch), Administrative
Healthcare Data + SAS analysis (33 ch combined), Bioinformatics (12 ch),
Synthetic Data, Analytical Skills.

## Dispatch

| wave | dispatched | landed | procs |
|-----:|-----------:|-------:|------:|
|    1 |         50 |     50 |    56 |
|    2 |         50 |      0 |     0 |
|  3–5 |        150 |      — |     — |

Wave 1 throughput was normal (~25 min wall clock). Wave 2 dispatched
into a Max plan rolling rate-limit cap, and all 10 sub-agents returned
the limit message instantly (combined 0–10 tokens, sub-second
durations). Reset at 12:20 PT, ~4 hours away — too long to wait in
this conversation. Waves 3–5 not dispatched. The 200 unprocessed
chapters retain `procedure_attempted_at IS NULL` and will auto-resume
next session via the same mechanism that handled prior interruptions.

## Results — wave 1 only

```text
chapters dispatched   50
chapters landed       50
procedures written    56
chapters w/ ≥1 proc   28 (56%)
chapters w/ 0 procs   22 (44%)
concept links written 226
pattern links written 10
```

Resolution mix (lower exact-match rate than session 4 — UNIX systems
programming and SAS healthcare data analysis bring substantial new
vocabulary):

| outcome | count | share |
|---|---:|---:|
| exact | 158 | 70% |
| embedding_high | 9 | 4% |
| alias | 2 | 1% |
| borderline (queued) | 21 | 9% |
| new concepts | 46 | 20% |
| pattern_link | 10 | — |

74% merged onto existing nodes. The 46 new concepts (highest new-rate
in any wave so far) reflect domains that hadn't yet appeared in the
corpus — UNIX system calls (`fork`, `exec`, `signals`, `pty`), SAS
healthcare claim codes (HCPCS, CPT, REV codes), and Google Analytics
metrics.

## Cumulative across Phase 3.1 (sessions 1+2+3+4 + partial 5)

```text
total procedures              1,022   ← crossed 1k milestone
total procedure→concept links 3,981
procs with implements_pattern 515 (50%)
unique patterns referenced    337
chapters attempted            1,090
chapters w/ procedures        526
```

## Throughput observation

The rolling 5-hour rate-limit budget fills faster when sessions are
back-to-back. Sessions 4 (clean, 0 failures) and 5 (immediate limit)
were 16 hours apart; the reset window covers most of a workday but not
much overnight. Adjusting strategy: leave more wall time between
sessions, or run sessions with substantially smaller waves (e.g., 5
sub-agents × 5 chapters = 25 per wave) when the limit is tight. The
resumable design absorbed this interruption without data loss as
expected.

## Deferred to next session

Chapters from wave 2–5 manifests, all with `procedure_attempted_at
IS NULL`:

- Wave 2: 50 chapters (Administrative Healthcare cont'd, UNIX threads/IPC,
  Analytical Skills, Web Metrics)
- Wave 3: 50 chapters
- Wave 4: 50 chapters
- Wave 5: 50 chapters

Total deferred: 200 chapters. They'll be re-prepped at the front of
session 6.

## Artifacts

- `/tmp/mypub-procedures/session-5/` — prompts, results (50/250),
  manifest
- Catalog backup: `data/catalog_pre-phase3-s5.ddb`
