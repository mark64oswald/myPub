# Data Engineering Skill

## Overview

This skill guides Claude in data engineering topics using the myPub knowledge base.

**Status:** Placeholder - To be generated from indexed chapters

## Domain Coverage

Data engineering encompasses:

- Data pipelines and orchestration
- ETL/ELT patterns
- Data quality and validation
- Streaming and batch processing
- Data modeling (dimensional, data vault, etc.)
- Storage formats and optimization
- Change data capture (CDC)
- Data governance and lineage

## Key Concepts

*To be populated from concept graph:*

- [ ] ETL vs ELT
- [ ] Batch vs streaming
- [ ] Data quality dimensions
- [ ] Schema evolution
- [ ] CDC patterns
- [ ] Medallion architecture
- [ ] Data contracts
- [ ] Orchestration patterns

## Source Books

Key books in collection:

- Fundamentals of Data Engineering (Reis, Housley)
- The Data Warehouse Toolkit (Kimball)
- Designing Data-Intensive Applications (Kleppmann)
- Data Pipelines Pocket Reference
- Building Event-Driven Microservices
- *[Additional from your collection]*

## Common Tasks

### Pipeline Design

- Batch vs streaming decision
- Error handling patterns
- Idempotency design

### Data Quality

- Validation rules
- Anomaly detection
- Data contracts

### CDC Implementation

- Log-based CDC
- Timestamp-based CDC
- Trigger-based CDC

## Generation Instructions

To populate this skill:

```bash
python scripts/generate_skill.py --topic "data engineering" --output skills/domains/data-engineering/
```
