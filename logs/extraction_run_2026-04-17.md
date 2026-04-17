# Phase 2.3 — Extraction Sample Run (2026-04-17)

## Scope

Tight sample via Claude Code sub-agents (user is on Max subscription without API
credits; sub-agents use Max, API calls do not). Five chapters drawn from four
diverse books, extracted via the Agent tool and processed through
`scripts/extract_entities.py --json-file`.

| chapter_id | book                                           | title                              |
|-----------:|------------------------------------------------|------------------------------------|
|     106072 | The Data Warehouse Toolkit, 3rd Edition        | Chapter 3: Retail Sales            |
|      38732 | Designing Data-Intensive Applications          | What Exactly Is a Transaction?     |
|      64401 | Kafka: The Definitive Guide                    | Kafka Consumer Concepts            |
|      65515 | Kubernetes: Up and Running                     | 7. Service Discovery               |
|      19559 | Building Event-Driven Microservices            | 21. Deploying Event-Driven Microservices |

Original 2.3 spec calls for 10 books / ~2,000 chapters. Sub-agent throughput
(~3–6 min wall-clock per chapter, and each invocation sits in the main
session's context budget) makes that infeasible as-run — see "Scope question
for 2.4" below.

## Results

```
concepts           117
concept_embeddings 117
relations          111
review queue       14 (all 'pending')
```

### Entity type distribution

| type      | count | share  |
|-----------|-----:|-------:|
| Concept   |   64 |  54.7% |
| Technique |   18 |  15.4% |
| Tool      |   18 |  15.4% |
| Pattern   |   13 |  11.1% |
| Framework |    3 |   2.6% |
| Algorithm |    1 |   0.9% |

### Relation type distribution

| type            | count |
|-----------------|-----:|
| IMPLEMENTS      |    38 |
| REQUIRES        |    29 |
| EXTENDS         |    18 |
| CONTRASTS_WITH  |    18 |
| CITES           |     8 |

All five relation types represented. IMPLEMENTS-heavy is expected (specific
instances of general concepts), CITES is thin (structural relations dominate
technical writing).

## Quality assessment (20-relation spot-check)

- **Strong**: 14/20 (70%) — e.g., "Write-Ahead Log IMPLEMENTS Durability",
  "CockroachDB IMPLEMENTS NewSQL", "Service Object REQUIRES Cluster IP",
  "Snapshot Isolation IMPLEMENTS Weak Isolation Levels".
- **Passable but imprecise**: 4/20 (20%) — e.g., "ACID EXTENDS Transaction"
  (ACID describes *properties of* transactions, not an extension of them),
  "Linearizability IMPLEMENTS CAP Theorem" (Linearizability is an option
  *discussed within* CAP, not an implementation of it).
- **Wrong**: 2/20 (10%) — "Service Discovery REQUIRES Kubernetes" (direction
  reversed — K8s provides service discovery), "KafkaConsumer CONTRASTS_WITH
  KafkaProducer" (they're complementary, not contrasting — should be CITES
  or untyped).

Within the prompt 2.3 thresholds (>30% nonsensical relations triggers a
re-tune) — no prompt adjustment required before 2.4.

## Review queue (14 items, all legitimately borderline)

Every item is a genuinely confusable pair the resolver correctly punted to
human review rather than auto-merging:

```
sim=0.895  'CAP Theorem'                 ↔ 'Linearizability'
sim=0.892  'KafkaProducer'               ↔ 'KafkaConsumer'
sim=0.839  'Weak Isolation Levels'       ↔ 'Serializability'
sim=0.825  'TiDB'                        ↔ 'CockroachDB'
sim=0.795  'Serializability'             ↔ 'Isolation'
sim=0.794  'FoundationDB'                ↔ 'TiDB'
sim=0.788  'Continuous Deployment'       ↔ 'Continuous Delivery'
sim=0.782  'Label Selector'              ↔ 'Service Object'
sim=0.781  'Rolling Update Pattern'      ↔ 'Basic Full-Stop Deployment Pattern'
sim=0.773  'Snapshot Isolation'          ↔ 'Serializability'
sim=0.769  'Read Committed'              ↔ 'Snapshot Isolation'
sim=0.758  'Endpoints Object'            ↔ 'Service Object'
sim=0.751  'Deployment'                  ↔ 'Service Object'
sim=0.750  'SQL Server'                  ↔ 'MySQL'
```

Resolution thresholds (0.90 / 0.75) look well-calibrated for this embedding
model. 0 false auto-merges observed in the sample.

## Scope question for Phase 2.4

The original plan calls for full-corpus extraction (~113K chapters) as Phase
2.4. Three constraints we now know:

1. Max subscription doesn't cover programmatic API. No API budget.
2. Sub-agents take ~3–6 min wall clock each (single-agent) or longer
   in parallel-of-3 (throughput-limited), and each consumes a slice of
   the driving session's context budget.
3. At the observed rates, even a single book (~200 chapters) runs into
   many hours and a lot of context. Full corpus via sub-agents is not
   a realistic path.

Options to discuss:

- (a) Redefine 2.4 scope: extract only the 5–20 "most important" chapters
  per book (filter by token_count and title patterns to skip front-matter).
- (b) Stay with the current 5-chapter sample and defer full extraction
  until API credits are available.
- (c) Iterate on a different dimension: scale the *eval set* (Phase 2.6)
  and the *review queue workflow* (Phase 2.5) first, which don't require
  more extraction.
