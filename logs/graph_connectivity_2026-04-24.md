# Graph connectivity snapshot — 2026-04-24

First look at the concept graph's topology, run between sessions 6 and 7.
Question: is the extraction producing a *connected* graph, or a pile of
per-book subgraphs that'll need stitching later?

Snapshot taken after session 6 + 380-item review pass.
Corpus progress: 4,568 / 10,203 unique content blocks (44.8%).

## Graph size

```text
concepts   41,260
relations  52,277   (undirected node-pair count; concept_relation has 58,780 rows,
                    the delta is self-loops or duplicate from/to pairs)
books touched (≥1 relation): 284
```

## Weakly connected components

```text
total components     6,990
largest component    30,682  (74.4% of all concepts)
2nd largest                 49
3rd-10th largest      28, 28, 26, 25, 19, 19, 17, 16

size buckets:
  1 (singleton)   5,269
  2-5             1,586
  6-20              129
  21-100              5
  101-1000            0
  1000+               1   ← the giant component
```

**Healthy.** One dominant giant component covers three-quarters of the
graph. No sign of per-book silos (that would show up as a cluster of
21-100-size components; we have 5). The 5,269 singletons are concepts
with zero relations — dead weight, but not structurally harmful.

## Degree distribution

```text
mean     2.53
median   1
max      169

degree buckets:
  0 (orphan)    5,259  12.7%
  1            17,927  43.4%
  2-3          11,092  26.9%
  4-10          5,671  13.7%
  11-50         1,224   3.0%
  51-200           87   0.2%
  200+              0
```

Power-law-like long tail. 87% of concepts have degree ≤ 3; 3.2% have
> 10. The useful "dense core" for cross-book reasoning is the ~1,300
concepts with degree ≥ 11 — 3% of total.

### Top 20 hubs

```text
169  Apache Spark
157  Deep Learning
154  Large Language Model
152  Data Warehouse
145  LangChain
143  Retrieval-Augmented Generation
142  Machine Learning
122  Logistic Regression
113  PyTorch
110  Linear Regression
108  Amazon S3
105  Pandas
105  Data Mesh
103  Apache Kafka
102  Fine-Tuning
 92  Artificial Intelligence
 91  Gradient Descent
 91  Big O Notation
 90  Docker
 88  Data Product
```

These are exactly the anchor concepts you'd want the Skills Factory to
build around. The hub list is dominated by foundational platforms
(Spark, PyTorch, S3, Kafka, Docker), methods (Deep Learning, ML,
Fine-Tuning, Gradient Descent), and patterns (RAG, Data Mesh, Data
Product). No surprises — the corpus knows what matters.

## Per-book novelty

How many books cite each concept? (Higher = more cross-book reuse.)

```text
1 book (book-unique)  29,586  82.2%
2-3 books              4,766  13.2%
4-10 books             1,388   3.9%
11-30 books              230   0.6%
30+ books                 31   0.1%
```

82% of concepts appear in exactly one book. The shared
"cross-book common core" is the ~6,400 concepts cited by ≥2 books
(18% of total).

### Top 20 book contributors

| book                                          | total | novel | shared | novel% |
|-----------------------------------------------|------:|------:|-------:|-------:|
| Beautiful Data                                |   727 |   554 |    173 |  76.2% |
| Basic Applied Bioinformatics                  |   568 |   419 |    149 |  73.8% |
| Business Metadata                             |   552 |   435 |    117 |  78.8% |
| Full Stack JavaScript Strategies              |   537 |   330 |    207 |  61.5% |
| Genetics and Genomics (a)                     |   531 |   151 |    380 |  28.4% |
| Genetics and Genomics (b)                     |   530 |   155 |    375 |  29.2% |
| Chemical Biology                              |   434 |   402 |     32 |  92.6% |
| Data Architecture                             |   420 |   268 |    152 |  63.8% |
| Hadoop: The Definitive Guide                  |   408 |   261 |    147 |  64.0% |
| Fluent Python                                 |   393 |   310 |     83 |  78.9% |
| DSA in JavaScript                             |   381 |   298 |     83 |  78.2% |
| DSA in Python Vol. 2                          |   372 |   130 |    242 |  34.9% |
| Genomics at the Nexus of AI                   |   362 |   201 |    161 |  55.5% |
| Advanced Programming in the UNIX Environment  |   355 |   325 |     30 |  91.5% |
| AWS Certified Cloud Practitioner              |   349 |   153 |    196 |  43.8% |
| AI Systems Performance Engineering            |   348 |   305 |     43 |  87.6% |
| Fundamentals of Software Architecture         |   341 |   231 |    110 |  67.7% |
| Hands-On Data Science for Marketing           |   339 |   208 |    131 |  61.4% |
| DSA in Python Vol. 1                          |   337 |    96 |    241 |  28.5% |
| Foundations of Scalable Systems               |   332 |   173 |    159 |  52.1% |

Pattern: **sister books** (DSA in JS/Python x2, Genetics and Genomics
x2) show the lowest novel%, because their concepts get captured once
and then the siblings reuse them. That's the alias/resolver loop working
correctly — no wasted work.

## Novelty trend over extraction days

```text
day          books  total    novel   novel%
2026-04-18      97  17,565   9,398   53.5%
2026-04-19      56  11,070   6,782   61.3%
2026-04-20      64  11,138   6,048   54.3%
2026-04-24      61  12,518   7,058   56.4%
```

**Novelty is flat at ~55%.** Each new extraction day continues to
introduce roughly the same share of book-unique concepts.
Interpretation: the corpus is broad enough that we haven't exhausted
novel material even at 45% coverage. Sessions 7–11 will likely keep
adding at this rate.

## Verdict

The graph is topologically healthy:

- **74% in one giant component** — not silo'd per-book.
- **Hubs are right** — major platforms and methods at the top, as
  they should be.
- **Long tail is real but expected** — 82% book-unique reflects that
  each technical book covers specific niche material. The dense core
  of ~6,400 cross-book-shared concepts is the useful substrate for
  Skills Factory anchoring.
- **13% orphans** — 5,259 concepts with zero relations is notable.
  These are likely single-mention extractions from sparse chapters
  or cases where the extractor named a concept without establishing
  a relation. Worth revisiting in Phase 2.5 but not a blocker.

**No red flag that suggests pausing the grind.** Finishing sessions
7–11 is safe and will continue to expand both the common core and the
long tail at current rates.

What to watch as the grind continues:

- Does novel% start dropping below 50%? (Would suggest saturation.)
- Does the giant-component share stay ≥ 70%? (It should; new concepts
  join the giant via their shared relations.)
- Does the orphan count grow faster than extraction rate? (Would
  suggest the extractor is under-relating.)

Cheap to re-run between sessions.
