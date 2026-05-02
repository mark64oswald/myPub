# Phase 3.1 — Session 7 (2026-05-01, late afternoon)

Seventh procedure-extraction session. Three notable disruptions, all
recovered: disk full (catalog backups had grown to >700G), mid-session
rate-limit hit at wave 1 start, and a single AUP refusal mid-wave-5.

## Scope

250 chapters across 9 books, dominated by O'Reilly's "Beautiful Data"
(146 of 250 chapters — an essay collection where data scientists
narrate how they built/used specific datasets, not how-to content):

| book | chapters in session |
|---:|:---|
| 146 | Beautiful Data (O'Reilly essay collection) |
|  21 | Becoming a Data Head |
|  15 | Bioinformatics Data Skills (Buffalo) |
|  13 | BioBuilder |
|  12 | Beyond Vibe Coding |
|  12 | Big Data Analytics and Machine Intelligence in Biomedical |
|  12 | Big Data Analytics for Intelligent Healthcare Management |
|  11 | Becoming an AI Orchestrator |
|   8 | Basic Applied Bioinformatics (residual appendices) |

## Disruptions and recoveries

1. **Disk full at catalog backup**: each catalog backup had grown to
   ~110G (DuckDB file growth not auto-reclaimed across many process
   runs). Six Phase 3 backups + Phase 2.4 backups had filled the drive.
   Cleared older Phase 3 backups (kept session 6 as the rollback
   point), freeing ~650G. Skipped the new s7 backup since s6 was
   recent enough.
2. **Rate-limit hit at start of wave 1**: 5 of 10 batches dispatched
   into a Max plan rolling cap and returned the limit message
   immediately. Reset arrived ~3h later; retried during the same
   conversation and all 5 landed clean. Same mid-session-recovery
   pattern as session 5.
3. **AUP refusal on batch s7-w5-45**: a single batch returned `API
   Error: Claude Code is unable to respond to this request, which
   appears to violate our Usage Policy`. The 5 chapters in that
   batch (15341, 15352, 15364, 15372, 15384 — likely the "Vibe Coding
   AI safety/ethics" or healthcare data privacy material) won't have
   results until they're re-prepped with a different sub-agent path.
   Defer to next session.

## Dispatch

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     50 |     3 | 0.06 |
|    2 |         50 |     50 |     8 | 0.16 |
|    3 |         50 |     50 |    23 | 0.46 |
|    4 |         50 |     50 |    32 | 0.64 |
|    5 |         50 |     45 |    52 | 1.04 |

The trajectory is clear: Beautiful Data dominates the early waves
with near-zero density (essays, not how-tos), and density climbs as
later batches reach Bioinformatics Data Skills, Beyond Vibe Coding,
and a couple of book-end appendices that were procedural.

## Results — this session

```text
chapters dispatched   250
chapters landed       245 (5 deferred — AUP refusal)
procedures written    118
chapters w/ ≥1 proc   55 (22%)
chapters w/ 0 procs   190 (78%)
concept links written 460
pattern links written 44
```

This is the lowest procedure density in any session (0.48
procs/chapter, vs sessions 3-6 averaging ~1.2). Beautiful Data alone
produced about 11 procs across its 146 chapters — 0.075 procs/chapter
— consistent with its essay format. The session's procedure count
came almost entirely from Bioinformatics Data Skills (Buffalo), which
delivered ~38 procs in 15 chapters (2.5/chapter) — its Unix shell,
Git, R, and bash scripting chapters are intensely procedural.

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 346 | 73.5% |
| embedding_high | 9 | 1.9% |
| borderline (queued) | 41 | 8.7% |
| new concepts | 73 | 15.5% |
| pattern_link | 44 | — |

75.4% merged onto existing nodes. New concepts heavily skewed toward
biology (BLAST variants, restriction enzymes, gene expression assays
not previously seen) and Buffalo's Unix-tools vocabulary
(samtools/bedtools/awk-pipelines).

## Cumulative across Phase 3.1 (sessions 1+2+3+4+5+6+7)

```text
total procedures              1,670
total procedure→concept links 6,698
procs with implements_pattern 853 (51%)
unique patterns referenced    614
chapters attempted            1,785
chapters w/ procedures        823
```

## Genre→density signal solidified further

After 7 sessions the pattern is robust:

| chapter genre | typical procs | session-7 examples |
|---|---:|---|
| Cookbook walkthroughs | 2-5 | Bioinformatics Data Skills, Vibe Coding |
| Reference appendices | 0-9 (variable) | Lab Reagents (9!), Webliography (0) |
| Essay narrative | 0 | Beautiful Data's 146 chapters |
| Conceptual/strategy | 0-1 | Becoming a Data Head |
| Healthcare survey papers | 0 | Big Data Healthcare Analytics chapters |

The "Beautiful Data" pattern is interesting and worth keeping in mind
for Phase 4: many books in the library are *insight literature* —
they teach by telling stories about real projects, not by giving
recipes. The procedure extractor correctly produces zero on those,
but the concept-graph contribution of those chapters is valuable.
This is exactly what the architecture predicted (sessions 5 log
mentioned "concept-only extraction prompt for descriptive books"
as a Phase 3.x candidate).

## Disk space concern

The catalog file is ~110GB despite holding only ~1,670 procedures and
~127K concept relations. This is DuckDB free-space accumulation from
many idempotent process runs. Worth investigating in Phase 4
preparation:
1. Try `CHECKPOINT` then file size check.
2. If that doesn't shrink: dump → drop → reload to reclaim.
3. Going forward, may want to reduce backup retention (keep only
   most-recent + one milestone backup, not every session's).

## Artifacts

- `/tmp/mypub-procedures/session-7/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s6.ddb` (s6 = our rollback
  point; we skipped the s7 snapshot due to disk pressure)
- Deferred to next session: chapter_ids 15341, 15352, 15364, 15372,
  15384 (AUP refusal — re-prep will hit a fresh sub-agent path)
