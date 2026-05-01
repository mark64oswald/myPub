# Graph connectivity snapshot — 2026-04-30

Second look at the concept graph's topology, run after Phase 2.4 session 12
completion. Question: did the corpus-completion sweep collapse silos, grow
the giant component, and produce a graph dense enough for cross-book
reasoning?

Snapshot taken after session 12 + s11 review pass. **Phase 2.4 is
functionally complete** — all 12,096 unique content blocks ≥500 chars
across 541 books processed. Compares directly against the post-s6
snapshot at 44.8% coverage (`graph_connectivity_2026-04-24.md`).

## Graph size

```text
concepts                81,859     (was 41,260 — +98%)
undirected pairs       109,173     (was 52,277 — +109%)
relation rows          126,401     (was 58,780)
books touched (≥1 rel):    541     (was 284 — +90%, every book in catalog)
```

Every book in the catalog now contributes ≥1 relation. Edge count grew
slightly faster than node count, so density improved (mean degree
2.53 → 2.67).

## Weakly connected components

```text
total components       12,431     (was 6,990)
largest component      63,054     (77.0% of all concepts; was 74.4%)
2nd largest                30
3rd-10th largest           25, 25, 25, 25, 25, 20, 20, 19

size buckets:
  1 (singleton)        9,312     (was 5,269)
  2-5                  2,882     (was 1,586)
  6-20                   230     (was 129)
  21-100                   6     (was 5)
  101-1000                 0
  1000+                    1     ← the giant
```

**Healthier than the s6 snapshot.** The giant absorbed an additional
2.6 percentage points despite the corpus doubling in size. The 6
clusters in the 21-100 size band are local sub-graphs that haven't
been bridged by any sibling-book edge yet — small enough to address
in Phase 2.5 if needed, not silo-shaped.

Singletons grew proportionally with the corpus (12.7% → 11.4%), so
the orphan share *improved* even as absolute count rose.

## Degree distribution

```text
mean      2.67     (was 2.53)
median    1
max       270     (was 169)

degree buckets:
  0 (orphan)     9,312   11.4%   (was 12.7%)
  1             36,002   44.0%   (was 43.4%)
  2-3           22,330   27.3%   (was 26.9%)
  4-10          11,437   14.0%   (was 13.7%)
  11-50          2,571    3.1%   (was 3.0%)
  51-200           200    0.2%   (was 0.2%)
  200+               7    0.0%   (was 0)
```

Distribution shape barely moved — power-law tail held as the corpus
grew. The notable change is the appearance of the 200+ bucket (7
concepts now have ≥200 distinct relations); the s6 snapshot had zero.
Densification is concentrated at the head, not the tail.

## Top 20 hubs (highest-degree concepts)

```text
530  Apache Spark                   (was 169 — +361)
506  Large Language Model           (was 154)
496  Machine Learning               (was 142)
443  Deep Learning                  (was 157)
388  Retrieval-Augmented Generation (was 143)
378  Logistic Regression            (was 122)
346  LangChain                      (was 145)
346  PyTorch                        (was 113)
321  Pandas                         (was 105)
314  Data Warehouse                 (was 152)
288  scikit-learn                   (new entrant)
269  Supervised Learning            (new entrant)
268  Linear Regression              (was 110)
247  Decision Tree                  (new entrant)
247  Apache Kafka                   (was 103)
237  Classification                 (new entrant)
235  Artificial Intelligence        (was  92)
224  Amazon S3                      (was 108)
223  Fine-Tuning                    (was 102)
223  Aggregate                      (new entrant)
```

The hub composition is stable: same anchors (Spark, LLM, ML, Deep
Learning, RAG, LangChain, PyTorch, S3, Kafka), and the new entrants
(scikit-learn, Supervised Learning, Decision Tree, Classification,
Aggregate) are exactly the foundations you'd expect from late-stage
healthcare-analytics + ML book pulls in s10–s12. **No surprises.**

The dense core grew: 1,300 concepts with degree ≥11 in s6, now 2,778
(2,571 + 200 + 7) — about 3.4% of total, slightly larger share than
the 3.2% in s6. This is the substrate the Skills Factory will anchor
on.

## Per-book novelty

How many books cite each concept? (Higher = more cross-book reuse.)

```text
1 book (book-unique)   59,653   82.2%   (was 82.2%)
2-3 books               9,134   12.6%   (was 13.2%)
4-10 books              3,028    4.2%   (was 3.9%)
11-30 books               651    0.9%   (was 0.6%)
30+ books                 108    0.1%   (was 0.1%)
```

Novelty distribution is *exactly* stationary. 82% book-unique held
across the doubling of the corpus. The cross-book "common core" (≥2
books) grew from ~6,400 to ~12,900 concepts — still the useful
substrate, now twice the size.

The 30+ tier swelled from 31 to 108 concepts — these are the
foundation concepts that essentially every book in their domain
references (Apache Spark, ML, RAG, Deep Learning, etc.).

### Top 20 book contributors

| book                                          | total | novel | shared | novel% |
|-----------------------------------------------|------:|------:|-------:|-------:|
| Methods and Apps of Statistics in Clinical Trials | 2,432 | 2,018 |   414 |  83.0% |
| Healthcare Data Analytics                          | 1,800 | 1,367 |   433 |  75.9% |
| Genetics and Genomics                              | 1,058 |   277 |   781 |  26.2% |
| Structural Bioinformatics                          |   878 |   713 |   165 |  81.2% |
| Implementing Domain-Driven Design                  |   859 |   553 |   306 |  64.4% |
| Beautiful Data                                     |   727 |   520 |   207 |  71.5% |
| Joe Celko's SQL for Smarties                       |   725 |   476 |   249 |  65.7% |
| The Go Programming Language                        |   720 |   582 |   138 |  80.8% |
| Healthcare Analytics Made Simple                   |   706 |   428 |   278 |  60.6% |
| Business Metadata                                  |   651 |   488 |   163 |  75.0% |
| AI Agents and Applications                         |   607 |    82 |   525 |  13.5% |
| Python Data Science Handbook                       |   596 |   389 |   207 |  65.3% |
| Basic Applied Bioinformatics                       |   563 |   387 |   176 |  68.7% |
| Semantic Web for the Working Ontologist            |   557 |   425 |   132 |  76.3% |
| Learning Python                                    |   546 |   354 |   192 |  64.8% |
| Full Stack JavaScript Strategies                   |   536 |   260 |   276 |  48.5% |
| System Design on AWS                               |   530 |   248 |   282 |  46.8% |
| Bioinformatics and Functional Genomics             |   522 |   154 |   368 |  29.5% |
| Joe Celko's Analytics and OLAP in SQL              |   474 |   282 |   192 |  59.5% |
| The Data Model Resource Book, Volume 3             |   473 |   361 |   112 |  76.3% |

Two patterns stand out:

- **Sister-book / late-arrival low novel%**. Genetics and Genomics
  (26%), AI Agents and Applications (13.5%), Bioinformatics and
  Functional Genomics (29.5%) are extracted *after* sibling books in
  their domain; the shared concepts were already canonical, so most
  contribution is shared. AI Agents & Applications at 13.5% novel is
  the most extreme — its concepts are nearly all already in the
  graph from earlier RAG/agent books.
- **Specialty-domain high novel%**. Methods and Apps of Statistics in
  Clinical Trials (83%), Structural Bioinformatics (81%), The Go
  Programming Language (80.8%), Semantic Web Working Ontologist
  (76.3%) — these contribute domain-specific vocabulary that no
  other book in the catalog covers.

## Novelty trend over extraction days

```text
day          books  total    novel   novel%
2026-04-17     227     117      54   46.2%
2026-04-18     540  16,214   8,771   54.1%
2026-04-19     436   8,724   6,018   69.0%
2026-04-20     378   7,711   5,162   66.9%
2026-04-24     321  16,129  12,056   74.7%
2026-04-25     101   1,883   1,498   79.6%
2026-04-26     209  15,437  12,766   82.7%
2026-04-30     135  15,644  13,328   85.2%
```

The rising novelty% over time is expected and is *not* a
corpus-saturation signal — it's a "books arriving later have less
time to be cited by sibling books" measurement effect. New concepts
created on day N can only be referenced by extractions on day N or
later, so concepts from late days appear book-unique more often
because they haven't yet been seen by sibling extractors. This is
the inverse of what happens at extraction time, where late-stage
session 12 saw 69% `exact` resolutions vs 51% earlier — *those* are
the saturation signal.

## Verdict

The graph is structurally healthier than the s6 snapshot:

- **Giant grew from 74.4% → 77.0%** despite corpus doubling. New
  concepts joined the giant rather than forming silos.
- **Orphan share dropped from 12.7% → 11.4%.** Even as absolute
  orphan count rose, the rate per concept improved.
- **Hub composition stable.** Same canonical anchors, just bigger.
  Apache Spark went from degree 169 to 530 — the corpus is now
  hub-dominated in a way the s6 snapshot only hinted at.
- **Novelty distribution exactly stationary at 82% book-unique.**
  The doubling preserved the underlying topology shape — corpus
  growth was scale-invariant.
- **Cross-book common core grew from ~6,400 to ~12,900 concepts.**
  Doubled in step with the corpus. Skills Factory anchoring substrate
  doubled in size with no shape change.

**No structural pathologies.** The graph is ready for Phase 2.5
(orphan resolution, sub-cluster bridging) and Phase 3 (Skills Factory
generation against the 12,900-concept common core).

## Comparison summary table

| metric                          | s6 (2026-04-24) | s12 (2026-04-30) | Δ       |
|---------------------------------|----------------:|-----------------:|---------|
| concepts                        |          41,260 |           81,859 |   +98%  |
| undirected pairs                |          52,277 |          109,173 |  +109%  |
| books touched                   |             284 |              541 |   +90%  |
| total components                |           6,990 |           12,431 |   +78%  |
| giant component (% of nodes)    |          74.4% |            77.0% |  +2.6pp |
| singletons (% of nodes)         |          12.7% |            11.4% |  −1.3pp |
| 21-100 mid-clusters             |               5 |                6 |   +1    |
| mean degree                     |            2.53 |             2.67 |   +0.14 |
| max degree                      |             169 |              270 |   +60%  |
| concepts with degree ≥11        |           1,311 |            2,778 |  +112%  |
| 30+ book ubiquity tier          |              31 |              108 |  +248%  |
| common core (≥2 books)          |          ~6,400 |          ~12,900 |  +102%  |

Doubling-and-densifying. Corpus growth scaled linearly across most
metrics; densification (degree ≥11, 30+ ubiquity) outpaced
proportional growth.

## Next steps

- **s12 review pass** (500-item, post-s12) — clear the +234 borderlines
  added by s12 plus residual queue.
- **Phase 2.5 orphan analysis.** 9,312 zero-degree concepts. Two
  causes: (a) extractor named a concept but didn't establish
  relations, (b) single-mention extractions in genuinely isolated
  contexts. Worth a sweep that re-runs the extractor on the
  source-chapter context for the top-100 most-cited orphan concepts.
- **Mid-cluster bridging.** The 6 clusters in the 21-100 band are
  candidates for manual review — likely a single edge would fold
  them into the giant.
- **Skills Factory anchoring.** The 12,900-concept common core is
  the substrate. Top-20 hubs are the obvious starting anchors.
