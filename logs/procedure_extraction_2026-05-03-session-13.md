# Phase 3.1 — Session 13 (2026-05-02 → 2026-05-03)

Thirteenth session. **237 procedures** across 250 chapters (density 0.95).
Notable for a transient backend slowdown that caused 3 batch timeouts; all
were retried successfully after a wait period.

## Scope

Wide alphabetical sweep through D-titled books:
- Cloud-Native intro / scaling / observability books (variable density)
- Data Algorithms book (high-density: batch 12 yielded 23 procs, batch 23 yielded 22)
- Data architecture/governance/mesh books (mostly conceptual, 0 procs each)
- Data Algorithms with Spark / Apache Beam infra setup (batch 9: 23 procs)
- Data Mesh by Dehghani (160+ chapters, all conceptual — 0 procs throughout)

## Results

```text
chapters dispatched   250
chapters landed       250 (after probe-then-resume)
procedures written    237
chapters w/ ≥1 proc   89   (36%)
chapters w/ 0 procs   161  (64%)
concept links written 824
pattern links written 106
```

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 665 | 79.3% |
| embedding_high | 7 | 0.8% |
| alias | 6 | 0.7% |
| borderline | 42 | 5.0% |
| new | 111 | 13.2% |
| pattern_link | 106 | — |

## Disruption: backend slowdown mid-session

Roughly mid-session, dispatch performance degraded sharply. Symptoms:
- 3 sub-agent batches (11, 12, 11-retry) hit "Stream idle timeout" or
  "Agent stalled, no progress for 600s" with 13 tokens of output
- Other batches that were already in flight kept finishing but at 35–54
  minutes per batch (vs the normal 1–4 min)
- After a wait period, dispatched a probe batch which completed in 218s
  (3.6 min) — backend recovery confirmed
- Re-ran the failed 3 batches plus continued with 12-25 dispatch; all
  completed cleanly

Pattern looks similar to s7's Max-plan rolling-cap behavior, but the
failure mode was different — instead of immediate "limit hit" responses,
agents stalled mid-generation with no output. Worth noting in case it
recurs: the probe-batch approach correctly diagnosed recovery.

## Cumulative across Phase 3.1 (sessions 1–13)

```text
total procedures              2,839  (+237)
chapters attempted            3,263  (~25.2% of corpus)
chapters w/ procedures        1,402  (+89)
```

Crossed the **25% of corpus attempted** milestone. Concept-graph integrity
remains strong: 80%+ of resolutions are exact matches.

## Artifacts

- `/tmp/mypub-procedures/session-13/` — prompts, results, manifest, dispatch.state
