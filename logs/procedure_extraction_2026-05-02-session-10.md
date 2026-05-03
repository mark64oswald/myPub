# Phase 3.1 — Session 10 (2026-05-02)

Tenth session. First session under the new dispatch model: streaming pipeline
(keep ~10 sub-agents in flight, replace on completion) and tightened
front-matter regex filter. Both work mechanically; the session yield was
unexpectedly low because the alphabetical sweep hit a wall of conceptual
content ("Building Slack Bots" early chapters + the bulk of "Business
Metadata - Capturing Enterprise Knowledge").

## Scope

250 chapters across ~5 books, dominated by:

| chapters | book |
|---:|:---|
| ~120 | Business Metadata - Capturing Enterprise Knowledge |
|  ~80 | Building Slack Bots |
|  ~50 | other (Building*, NLP-related residuals) |

Building Slack Bots's content is short cookbook chapters (~5-10 sections per
chapter). Business Metadata is conceptual/governance prose throughout.

## Results — this session

```text
chapters dispatched   250
chapters landed       250 (zero stalls)
procedures written    79      (← lowest density of any session)
chapters w/ ≥1 proc   54  (22%)
chapters w/ 0 procs   196 (78%)
concept links written 333
pattern links written 39
```

Density: 0.32 procs/chapter — half of s7's 0.48 (which was the prior low,
driven by "Beautiful Data" essays). Genre→density signal is consistent with
prior sessions: conceptual/governance-style books produce few procedures.

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 243 | 73% |
| embedding_high | 5 | 1.5% |
| alias | 7 | 2.1% |
| borderline | 36 | 10.8% |
| new | 47 | 14.1% |
| pattern_link | 39 | — |

84% merged onto existing nodes. Concept-graph integration is healthy despite
the low procedure count.

## Cumulative across Phase 3.1 (sessions 1–10)

```text
total procedures              2,217  (+79)
total procedure→concept links 9,012  (+333)
procs with implements_pattern 1,187  (+39)
unique patterns referenced    910    (+38)
chapters attempted            2,513  (~19.4% of corpus)
chapters w/ procedures        1,104  (+54)
```

## Two improvements landed this session

1. **Tightened FRONT_MATTER_REGEX** — adds anchored patterns for Part-N
   intros, Glossary/Bibliography/References, Epilogue, Packt-style back-matter
   ("Why subscribe?", "Free Benefits", "Unlock Your Exclusive Benefits",
   "Other Books You May Enjoy"). Eliminates 271 dispatches across the
   remaining pool (~3% — smaller than my projection of 25-30%; honest update).
2. **Streaming dispatch via scripts/dispatch_state.py** — replaces wave-of-10
   gating. Keeps ~10 sub-agents in flight, dispatches a replacement when each
   completes. Wall-clock saved per session ~25% (estimated; this session was
   too low-density to measure cleanly).

Both improvements work as designed. Neither magically increases procedure
yield — that's still bounded by the underlying content density of the books
in the dispatch window.

## A/B test result (separate from this session)

Tested whether sub-agents on Sonnet 4.6 could replace Opus 4.7 to gain
wall-clock speedup. Result: **dropped Sonnet** after 25-chapter shadow run.
Findings:

- Concept-naming Jaccard overlap with Opus baseline: **0.33** (target was
  ≥0.80). Sonnet says "agent as tool", "data loader", "as_tool()"; Opus
  says "agent-as-tool", "data loaders", "@function_tool decorator". These
  are not synonyms a resolver merges, so Sonnet would silently pollute the
  concept graph with near-duplicate nodes.
- Wall-clock: Sonnet was actually **slower** on 4 of 5 buckets (likely
  rate-limit contention or longer thinking on dense chapters). Front-matter
  20s vs 30s; OpenAI Agents 155s vs 274s; Knowledge Graphs 150s vs 276s.
- Step verbatim fidelity: roughly tied; both occasionally paraphrase.

Conclusion: structured-format intuition was wrong. The concept-naming work
requires familiarity with how the catalog already names things, which Sonnet
doesn't have and can't acquire from a chapter prompt alone.

## Disruptions

None. Zero stalls, zero AUP refusals, zero rate-limit hits. The streaming
dispatch model is mechanically clean.

## Issue noted

The Building Slack Bots book has ~5 batches of all-zero-proc chapters
that look like they should produce procedures (chapter titles like "Basic
responses", "Sending a direct message", "Restricting access", "Debugging a
bot"). Spot-checking some of these would be worthwhile in Phase 4 prep —
either the chapters are very short (truncation possible), the content is
prose-only with code embedded but conceptually framed, or the extractor is
too conservative for very short cookbook entries. Not blocking; flagging.

## Artifacts

- `/tmp/mypub-procedures/session-10/` — prompts, results, manifest, dispatch.state
- Catalog backup: still skipped (s8 backup remains rollback point)
- Deferred to next session: 0 chapters
