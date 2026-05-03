# Phase 3.1 — Session 12 (2026-05-02)

Twelfth session. **208 procedures** across 250 chapters (density 0.83) —
solid recovery and a strong showing for the `--per-batch 10` model.

## Scope

Mixed-genre dispatch as the alphabetical sweep moved through:
- Chemical Biology / pharmacology survey (mostly conceptual, 0 procs)
- Choosing... / Cloud-Native software / Cloud computing books (variable)
- Code review and refactoring books (high-density patterns)
- Data architecture (Inmon-style governance: conceptual)
- Some highly procedural cookbook chapters mid-session

Densest batches:
- Batch 5 (18 procs)
- Batch 17 (18 procs)
- Batch 3 (19 procs)
- Batch 18 (13 procs)

Some single-proc-per-chapter patterns (batch 4, batch 6) — workflow-style
short cookbook chapters.

## Results

```text
chapters dispatched   250
chapters landed       250 (zero stalls, zero AUP refusals)
procedures written    208
chapters w/ ≥1 proc   120  (48%)
chapters w/ 0 procs   130  (52%)
concept links written 819
pattern links written 86
```

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 657 | 80.2% |
| embedding_high | 7 | 0.9% |
| alias | 8 | 1.0% |
| borderline | 47 | 5.7% |
| new | 107 | 13.1% |
| pattern_link | 86 | — |

82% merged onto existing nodes — concept-graph health holding.

## Cumulative across Phase 3.1 (sessions 1–12)

```text
total procedures              2,602  (+208)
chapters attempted            3,013  (~23.3% of corpus)
chapters w/ procedures        1,313  (+120)
```

## Disruptions

None. Zero stalls, zero AUP refusals, zero rate-limit hits.

## Artifacts

- `/tmp/mypub-procedures/session-12/` — prompts, results, manifest, dispatch.state
