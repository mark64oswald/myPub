# Phase 3.1 — Session 8 (2026-05-01, evening)

Eighth session. Hit two distinct failure modes: persistent AUP refusals
on a small set of chapters that needed manual marking, and a wave-5
sub-agent stall epidemic. 186 procedures landed across 223 processed
chapters; 27 deferred (5 AUP-blocked + 22 stalled).

## Scope

250 chapters across 16 books. Auto-resumed the 5 AUP-blocked chapters
from session 7 (they re-failed on retry, then were manually marked as
attempted to break the perpetual-deferral loop).

| book | chapters in session |
|---:|:---|
|  56 | Bioinformatics and Functional Genomics (Pevsner) |
|  28 | Bioinformatics and Medical Applications |
|  23 | Bioinformatics with Python Cookbook |
|  19 | Building AI Agents with LLMs, RAG, and Knowledge Graphs |
|  19 | Building Agentic AI Systems |
|  18 | Bioinformatics Managing Scientific Data |
|  18 | Bioinformatics Tools for Pharmaceutical Drug Product Development |
|  15 | Building Agents with OpenAI Agents SDK |
|  14 | Blueprints for Text Analytics Using Python |
|  12 | Build a Large Language Model (From Scratch) — Raschka |
|  +6 | other smaller groupings |

## Disruptions

1. **AUP refusal on the 5 deferred chapters from session 7** (15341,
   15352, 15364, 15372, 15384). Retry produced the same refusal, so
   the chapters' content itself triggers the safety filter. Marked
   `procedure_attempted_at = NOW()` directly to remove them from
   future selections — they'll show up as 0-procedure attempted but
   won't keep getting selected and re-failing.
2. **Wave 5 sub-agent stall epidemic**. Six batches stalled out (41,
   44, 47, 48, 49, 50). The Write tool was being denied for some
   sub-agents and they sat for 10–35 minutes before the watchdog
   killed them. Pattern matched what we saw earlier in session 4 but
   at much higher rate (60% of wave 5 vs single-batch then). Stalls
   appeared concentrated near the end of the dispatch window;
   possibly a backend latency issue.
3. **Disk-space pressure** (carried over from session 7): catalog
   backups had grown to 700G+. Cleared old backups, kept only the
   pre-s8 snapshot (110G).

## Dispatch

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     45 |    12 | 0.27 |
|    2 |         50 |     50 |    22 | 0.44 |
|    3 |         50 |     50 |    59 | 1.18 |
|    4 |         50 |     50 |    74 | 1.48 |
|    5 |         50 |     26 |    14 | 0.54 |

Wave-by-wave density tracked content type:

- Wave 1: AUP-blocked + Bioinformatics Managing Data (mostly conceptual)
- Wave 2: Pevsner Functional Genomics textbook (conceptual)
- Wave 3: Bioinformatics with Python Cookbook chapters 1–10 (very procedural)
- Wave 4: Cookbook ch 11–18 + Blueprints + Raschka (very procedural — 26 procs in Raschka's 5 chapters)
- Wave 5: degraded by stalls; landed batches were mostly Building AI Agents (conceptual)

## Results — this session

```text
chapters dispatched   250
chapters landed       223 (5 AUP-blocked + 22 stalled deferred)
procedures written    186
chapters w/ ≥1 proc   91 (41%)
chapters w/ 0 procs   132 (59%)
concept links written 817
pattern links written 96
```

Resolution mix:

| outcome | count | share |
|---|---:|---:|
| exact | 808 | 81.4% |
| embedding_high | 11 | 1.1% |
| alias | 3 | 0.3% |
| borderline | ~57 | ~5.7% |
| new concepts | ~93 | ~9.4% |
| pattern_link | 96 | — |

83% merged onto existing nodes (counts approximated — process re-ran twice
so the running tallies above reflect a partial picture). The 94 new concepts are a healthy mix
of bioinformatics tools (IGV, samtools/bcftools commands, Bioconductor),
agent frameworks (LangGraph state machines, OpenAI Agents SDK
primitives), and LLM-from-scratch coding concepts (attention heads,
positional embeddings, byte-pair tokenizer).

## Cumulative across Phase 3.1 (sessions 1–8)

```text
total procedures              1,856
total procedure→concept links ~7,510
procs with implements_pattern ~947 (51%)
unique patterns referenced    ~688
chapters attempted            2,013
chapters w/ procedures        914
```

Crossed the **2,000 chapters attempted** milestone (~16% of corpus).

## Standout procedural books

Three highlights this session:

1. **Bioinformatics with Python Cookbook** — 19 chapters, ~40 procs.
   Each chapter is a workflow recipe (setting up environment, working
   with NCBI/Ensembl APIs, aligning sequences, calling variants,
   single-cell analysis). Will be a strong source for Skills Factory
   bioinformatics packages.
2. **Build a Large Language Model (From Scratch)** by Raschka — 5 chapters
   produced 26 procedures (5–6 per chapter). Each chapter walks through
   building a substantial component (tokenizer, attention, positional
   embedding, training loop) with literal code throughout. Among the
   most procedure-dense chapters extracted in any session.
3. **Blueprints for Text Analytics Using Python** — 14 chapters, ~24 procs.
   Concrete pipelines for common NLP tasks (sentiment, topic modeling,
   embeddings, knowledge graph construction) with verbatim code.

## Known issues for Phase 4 prep

1. **Sub-agent Write-tool denial pattern** is causing meaningful loss
   when concentrated. Worth investigating whether granting Write to
   the procedure-extraction sub-agent permission preset would help —
   they only need to write to `/tmp/mypub-procedures/` paths.
2. **Catalog file size** is still ~110GB (DuckDB free-space accumulation).
   Phase 4 should plan a dump-drop-reload cycle before serious work.
3. **AUP-blocked chapter handling**: now have a small list of chapters
   the sub-agent path won't process. May want to add a flag or label
   distinct from `procedure_attempted_at` so we can identify them later
   for alternative-path processing.

## Artifacts

- `/tmp/mypub-procedures/session-8/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s8.ddb`
- Deferred to next session: 24 chapters from stalled wave-5 batches
  (will auto-resume because `procedure_attempted_at IS NULL`)
- Marked-as-attempted (skip permanently): 15341, 15352, 15364, 15372,
  15384 (AUP-blocked content)
