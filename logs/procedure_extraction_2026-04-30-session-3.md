# Phase 3.1 — Session 3 (2026-04-30)

Third procedure-extraction session. First with broad domain diversity —
14 books spanning AI agents, RAG, MCP servers, Stable Diffusion, AI
engineering, healthcare analytics, and the tail of the algorithms
trilogy. Conservative wave size to leave Max plan rate-limit headroom
after session 2's late-stage limit hit.

## Scope

250 chapters via `extract_procedures.py prep --limit 250` (alphabetical
by book title, dedup by content_hash, skip-attempted). Selection picked
up the 10 deferred chapters from session 2 plus the next ~240 unique
chapter blocks alphabetically. The auto-resume worked exactly as
designed.

| book | chapters in session |
|---|---:|
| AI Agents and Applications | 73 |
| A Common-Sense Guide to Data Structures and Algorithms in Python (Vol 2) | 43 |
| AI Systems Performance Engineering | 21 |
| AI Engineering in Practice | 17 |
| AI Agents in Action | 14 |
| A Damn Fine Stable Diffusion Book | 14 |
| A Framework for Applying Analytics in Healthcare | 13 |
| A Simple Guide to Retrieval Augmented Generation | 13 |
| AI Engineering | 12 |
| AI Value Creators | 10 |
| (4 other books, total) | 20 |

## Dispatch

5 waves of 10 sub-agents × 5 chapters = 50 chapters per wave. Total 50
sub-agent invocations — well below the post-session-2 budget of ~75 to
leave rate-limit headroom.

| wave | dispatched | landed | procs |
|-----:|-----------:|-------:|------:|
|    1 |         50 |     50 |    61 |
|    2 |         50 |     50 |    70 |
|    3 |         50 |     50 |    79 |
|    4 |         50 |     50 |    73 |
|    5 |         50 |     45 |    35 |

Wave 5 had one sub-agent stall (batch s3-w5-45) where both `Write` and
the filesystem MCP `write_file` were denied for the agent's working
permission set, and a Python/Bash fallback didn't produce output before
the watchdog timed out. The 5 chapters in that batch (1259, 1277, 1294,
1304, 1341 — from "AI Engineering for Beginners") will be re-prepped
automatically next session. No data loss; same resumable mechanism that
caught the session-2 rate-limit interruption.

Wall clock ~50 min for the dispatch + ~10 s Python processing.

## Results — this session

```text
chapters dispatched   250
chapters landed       245
procedures written    318
chapters w/ ≥1 proc   131 (53%)
chapters w/ 0 procs   114 (47%)
concept links written 1,259
pattern links written 154
```

The procedure density is markedly higher than session 2 (1.30
procs/chapter vs 0.62), driven by the procedural mix: AI agent
construction, MCP server building, Stable Diffusion workflows, and
RAG indexing all walk through concrete steps. The no-op rate is
correspondingly lower (47% vs 55%).

Resolution mix on procedure-concept references:

| outcome | count | share |
|---|---:|---:|
| exact | 1,038 | 84.6% |
| embedding_high | 20 | 1.6% |
| alias | 15 | 1.2% |
| borderline (queued) | 66 | 5.4% |
| new concepts | 132 | 10.8% |
| pattern_link | 154 | — |

86.8% of concept references merged onto pre-existing Phase-2 nodes —
slightly lower than session 2's 90% because the new domains (RAG,
agents, MCP, diffusion models) bring genuinely new concepts (LangGraph
StateGraph, Chroma DB, ComfyUI, etc.). 132 new concepts is a healthy
contribution to the corpus.

## Cumulative across Phase 3.1 (sessions 1+2+3)

```text
total procedures              657
total procedure→concept links 2,520
procs with implements_pattern 378 (58%)
unique patterns referenced    220
chapters attempted            790
chapters w/ procedures        367
```

## Top 12 patterns implemented (cumulative)

| pattern | procs |
|---|---:|
| Top-Down Recursion | 15 |
| Greedy Algorithm | 8 |
| ReAct | 7 |
| Divide and Conquer | 7 |
| Bit Mask | 7 |
| Memoization | 5 |
| Magical Lookups | 5 |
| Adjacency List | 5 |
| Sort-Then-Scan | 5 |
| Separate chaining | 5 |
| Breadth-First Search | 5 |
| Selection Sort | 4 |

ReAct climbed to #3 cumulatively from this session alone — the
LangChain "Executing prompts programmatically", LangGraph "Building
tool-based agents", and AI Agents "Architectures and Patterns"
chapters all reference ReAct, and the resolver collapsed them onto a
single Pattern node. This is exactly the cross-book consolidation
EntityResolver was designed for.

## Observations

What worked:

- The 10-sub-agent wave size finished cleanly without rate-limit pressure.
- Domain diversity exposed how procedure density varies by book genre:
  - AI agents/RAG/SDXL hands-on: 2–4 procs/chapter
  - Algorithm walkthroughs: 1–2 procs/chapter
  - Strategy/conceptual books: 0 procs/chapter (correct no-op)
- The auto-resume correctly recovered the 10 deferred chapters from
  session 2 (B-Trees, Rabin-Karp) at the front of session 3.

What needs attention:

- The Write-tool denial → Python-via-Bash fallback pattern observed in
  session 2 occurred again in wave 5 here, but this time the fallback
  itself stalled. Worth investigating whether the agent permissions for
  these batches need a more direct path. For now, the resumable design
  absorbs the loss.
- 132 new concepts in one session is a notable jump — suggests the
  concept-resolution-queue may need a review pass to merge near-
  duplicates (e.g., "ReAct prompt" vs "ReAct framework" vs "ReAct").
  Defer until Phase 3.1 is complete.

## Throughput notes

- Session 2 (high parallelism, algorithm trilogy): 309 procs in 490
  chapters — 0.63 procs/chapter, ~3.5 h wall clock.
- Session 3 (medium parallelism, mixed domains): 318 procs in 245
  chapters — 1.30 procs/chapter, ~50 min wall clock.

The two sessions have similar procedure counts despite session 3 doing
half the chapters: the mixed-domain books are simply richer in
procedures. Future sessions that hit web/devops/database books will
likely behave more like session 3 than session 2.

## Artifacts

- `/tmp/mypub-procedures/session-3/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s3.ddb`
- Pending in next session: chapter_ids 1259, 1277, 1294, 1304, 1341
