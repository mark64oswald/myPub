# Phase 3.1 — Session 4 (2026-05-01)

Fourth procedure-extraction session. Widest book diversity yet (19 books)
across AWS certifications, ML/DL coding (TensorFlow, Keras, PyTorch),
AI-assisted programming, healthcare AI, and the start of API design /
DevOps territory. First clean session — no failed batches, no rate-limit
hits, all 250 chapters landed.

## Scope

250 chapters via `extract_procedures.py prep --limit 250`. Auto-resumed
the 5 deferred chapters from session 3 (CUDA tuning).

| book | chapters in session |
|---|---:|
| API Design Patterns | 38 |
| AWS for Solutions Architects (3rd ed) | 23 |
| AI and Machine Learning for Coders | 21 |
| AWS Certified Cloud Practitioner Study Guide | 20 |
| AWS for Solutions Architects (2nd ed) | 18 |
| AI and ML for Coders in PyTorch | 17 |
| AWS Certified AI Practitioner Study Guide | 14 |
| AI for Healthcare with Keras and Tensorflow 2.0 | 12 |
| AI-Powered Business Intelligence | 12 |
| AWS Certified Data Engineer Associate | 12 |
| (9 other books, total) | 63 |

## Dispatch

5 waves of 10 sub-agents × 5 chapters = 50 chapters per wave.
50 sub-agent invocations total.

| wave | dispatched | landed | procs | density |
|-----:|-----------:|-------:|------:|--------:|
|    1 |         50 |     50 |   136 | 2.72 |
|    2 |         50 |     50 |    70 | 1.40 |
|    3 |         50 |     50 |    40 | 0.80 |
|    4 |         50 |     50 |    45 | 0.90 |
|    5 |         50 |     50 |    18 | 0.36 |

The density variance across waves is striking — wave 1's mix of TensorFlow,
Keras, AWS Healthcare AI, and AWS Cert AI Practitioner produced 136 procs
in 50 chapters (2.7× the per-chapter rate of the previous session). Wave 5,
all AWS for Solutions Architects 2nd & 3rd editions, produced just 18 — the
"Solutions Architects" books are heavily conceptual (architecture overviews,
service descriptions, design principles) with hands-on labs concentrated in
a single chapter each.

Wall clock ~30 minutes for the dispatch + ~10 s Python processing. Faster
than session 3 because all 50 batches landed without retries.

## Results — this session

```text
chapters dispatched   250
chapters landed       250 (100% — no failures)
procedures written    309
chapters w/ ≥1 proc   131 (52%)
chapters w/ 0 procs   119 (48%)
concept links written 1,235
pattern links written 127
```

Resolution mix on procedure-concept references:

| outcome | count | share |
|---|---:|---:|
| exact | 1,001 | 80.7% |
| embedding_high | 13 | 1.0% |
| alias | 11 | 0.9% |
| borderline (queued) | 68 | 5.5% |
| new concepts | 154 | 12.4% |
| pattern_link | 127 | — |

82.6% merged onto pre-existing nodes. The 154 new concepts is the highest
of any session yet, driven by AWS-specific service vocabulary
(Bedrock/Q/Comprehend/Lex/SageMaker/Glue/Athena/Lake Formation) plus
TensorFlow/Keras/PyTorch APIs not previously seen.

## Cumulative across Phase 3.1 (sessions 1+2+3+4)

```text
total procedures              966
total procedure→concept links 3,755
procs with implements_pattern 505 (52%)
unique patterns referenced    328
chapters attempted            1,040
chapters w/ procedures        498
```

## Top 12 patterns implemented (cumulative)

| pattern | procs |
|---|---:|
| Top-Down Recursion | 15 |
| Greedy Algorithm | 8 |
| Bit Mask | 7 |
| Divide and Conquer | 7 |
| ReAct | 7 |
| Adjacency List | 5 |
| Memoization | 5 |
| Breadth-First Search | 5 |
| Sort-Then-Scan | 5 |
| Separate chaining | 5 |
| Magical Lookups | 5 |
| Binary Search | 4 |

The pattern leaderboard hasn't shifted much from session 3 — most patterns
extracted in this session were domain-specific and didn't accumulate counts
across multiple chapters. The session's procedure count came from concrete
ML/AWS walkthroughs implementing specific APIs rather than well-named
recurring design patterns. Consistent with the architecture doc's
prediction that "implements_pattern" density tracks more strongly with
classical algorithms / GoF software-design content than with platform-
specific procedural content.

## Observations

What worked:

- 0 batch failures, 0 rate-limit hits — the conservative wave size (10
  parallel sub-agents) is the right tradeoff for sustained throughput.
- Fully resumable design caught the session-3 stragglers cleanly.
- Resolver handled cross-edition merges correctly: "AWS for Solutions
  Architects" appears in 2nd and 3rd editions in this session, and the
  shared concepts (S3, EC2, IAM, Lambda) all merged via exact match
  against existing nodes.

Domain-density observations:

- AWS practitioner/study books: 2–4 procs/chapter on hands-on chapters,
  0 on conceptual/exam-prep chapters.
- TensorFlow/Keras coding books: 2–4 procs/chapter consistently.
- AWS for Solutions Architects: heavily conceptual; procedures only in
  the labs chapter (hands-on guide).
- API Design Patterns: ~0.5 procs/chapter — design principles and
  "consider this approach" without concrete walkthroughs.

This pattern (density ↔ genre) matters for Skills Factory planning:
Skills generation will draw heavily from cookbook/hands-on books and only
sparingly from architecture/conceptual books. The procedure-link table
gives Skills the "what to do" content; the concept-link table gives them
the "what this is about" content.

## Throughput across sessions

| session | chapters | procs | proc/chapter | wall |
|---|---:|---:|---:|---:|
| s1 (validation) | 50 | 30 | 0.60 | minutes |
| s2 (algorithms trilogy) | 490 | 309 | 0.63 | 3.5 h |
| s3 (mixed AI/agents/RAG) | 245 | 318 | 1.30 | 50 min |
| s4 (AWS/ML coders/AI) | 250 | 309 | 1.24 | 30 min |

Sessions 3+4 with mixed-domain content average ~1.27 procs/chapter; the
algorithm-textbook session averaged 0.63. If the remaining ~12,000
chapters track session 3+4 density, the corpus total extrapolates to
~15,000 procedures — a substantive supply for Skills Factory.

## Artifacts

- `/tmp/mypub-procedures/session-4/` — prompts, results, manifest
- Catalog backup: `data/catalog_pre-phase3-s4.ddb`
- No deferred chapters from this session.
