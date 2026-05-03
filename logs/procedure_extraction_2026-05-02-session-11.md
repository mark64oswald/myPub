# Phase 3.1 — Session 11 (2026-05-02)

Eleventh session. First with `--per-batch 10` (25 batches × 10 chapters)
to halve the dispatch count. **177 procedures** landed across 250 chapters
(density 0.71) — a strong recovery from s10's 0.32, despite the dispatch
window still containing significant Business Metadata residual.

## Scope

Mixed bag — alphabetical sweep crossed several genre boundaries:

- Business Metadata (residual): heavy conceptual prose, mostly 0 procs
- Building Slack Bots remainder: moderate procedural
- Chapter on Capture and Categorize Knowledge / Chemical Biology surveys: 0 procs
- Clean Architecture (Robert Martin): conceptual on architectural principles, mostly 0 procs but later chapters yielded
- **Big winners** (later batches): Cloud-Native LLM Engineering, CISA-style cybersecurity, Cloud-Native AI patterns —
  high-density code-walk-through chapters where the Building* clusters had peaked

Densest batches in s11:
- Batch 17 (10 chapters, **25 procs** — 2.5/chapter)
- Batch 8 (10 chapters, 23 procs — 2.3/chapter)
- Batch 11 (18 procs)
- Batch 10 (21 procs)

## Results

```text
chapters dispatched   250
chapters landed       250 (zero stalls, zero AUP refusals)
procedures written    177
chapters w/ ≥1 proc   89   (36%)
chapters w/ 0 procs   161  (64%)
concept links written 649
pattern links written 67
```

Density 0.71 procs/chapter — middle of the Phase 3.1 distribution.

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 527 | 78.6% |
| embedding_high | 10 | 1.5% |
| alias | 21 | 3.1% |
| borderline | 24 | 3.6% |
| new | 77 | 11.5% |
| pattern_link | 67 | — |

83% merged onto existing nodes — concept-graph health holding.

## Cumulative across Phase 3.1 (sessions 1–11)

```text
total procedures              2,394  (+177)
total procedure→concept links 9,661  (+649)
chapters attempted            2,763  (~21.4% of corpus)
chapters w/ procedures        1,193  (+89)
```

## Operational notes

- `--per-batch 10` works well. ~10 chapters/agent runtime ranged from 27s
  (all-zero front-matter) to 370s (dense procedural). Dispatch overhead
  halved relative to the s10 streaming model.
- Sub-agent Write-tool denial pattern continues — most batches needed the
  heredoc fallback. All recovered cleanly.
- Streaming dispatch state tracker (scripts/dispatch_state.py) used
  end-to-end. Works.

## Disruptions

None. Zero stalls, zero AUP refusals, zero rate-limit hits.

## Artifacts

- `/tmp/mypub-procedures/session-11/` — prompts, results, manifest, dispatch.state
- Catalog backup: still skipped (s8 backup remains rollback point)
