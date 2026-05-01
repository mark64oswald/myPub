# Dimensional Modeling Patterns Skill

## Overview

This skill guides Claude in using dimensional modeling patterns from the myPub pattern library.

## Available Patterns

### Fact Table Patterns

| Pattern | Description |
|---------|-------------|
| `dimensional.facts.transaction_fact` | Event-level facts |
| `dimensional.facts.periodic_snapshot` | Point-in-time snapshots |
| `dimensional.facts.accumulating_snapshot` | Process milestone tracking |
| `dimensional.facts.factless_fact` | Event tracking without measures |

### Dimension Patterns

| Pattern | Description |
|---------|-------------|
| `dimensional.dimensions.scd_type_1` | Overwrite changes |
| `dimensional.dimensions.scd_type_2` | Track history with versioning |
| `dimensional.dimensions.scd_type_3` | Previous value column |
| `dimensional.dimensions.role_playing` | Single dimension, multiple roles |
| `dimensional.dimensions.junk` | Low-cardinality flags/indicators |
| `dimensional.dimensions.degenerate` | Transaction identifiers in fact |
| `dimensional.dimensions.bridge` | Many-to-many relationships |

### Common Patterns

| Pattern | Description |
|---------|-------------|
| `dimensional.common.date_dimension` | Calendar dimension |
| `dimensional.common.time_dimension` | Time-of-day dimension |
| `dimensional.common.surrogate_key` | Key generation strategies |
| `dimensional.common.conformed_dimension` | Cross-process dimensions |

## Fact Table Selection Guide

### Transaction Fact

- **Grain**: One row per event/transaction
- **When**: Individual transactions, line items, clicks
- **Measures**: Additive (sum, count)
- **Example**: Sales, claims, orders

### Periodic Snapshot

- **Grain**: One row per entity per time period
- **When**: Track state at regular intervals
- **Measures**: Semi-additive (can't sum across time)
- **Example**: Account balances, inventory levels

### Accumulating Snapshot

- **Grain**: One row per process instance
- **When**: Track pipeline/workflow milestones
- **Measures**: Durations between stages
- **Example**: Order fulfillment, claims processing

### Factless Fact

- **Grain**: One row per event occurrence
- **When**: Track events without measures
- **Measures**: None (or just count)
- **Example**: Attendance, eligibility, coverage

## SCD Selection Guide

### Type 1 (Overwrite)

- No history needed
- Current value only
- Simple implementation
- Example: Correcting data errors

### Type 2 (Versioning)

- Full history required
- Temporal analysis needed
- Most common for analysis
- Example: Customer address history

### Type 3 (Previous Value)

- Limited history (current + previous)
- Specific comparison needed
- Simple history tracking
- Example: Last year's region assignment

## Using Patterns

```sql
-- Find dimensional patterns
SELECT pattern_id, name, description
FROM patterns
WHERE domain = 'dimensional_modeling'
ORDER BY category, name;

-- Get pattern with full definition
SELECT canonical_yaml
FROM patterns
WHERE pattern_id = 'dimensional.dimensions.scd_type_2';
```

## Pattern Location

```text
patterns/dimensional-modeling/
├── facts/
│   ├── transaction_fact.yaml
│   ├── periodic_snapshot.yaml
│   └── accumulating_snapshot.yaml
├── dimensions/
│   ├── scd_type_1.yaml
│   ├── scd_type_2.yaml
│   └── bridge_table.yaml
└── common/
    ├── date_dimension.yaml
    └── surrogate_key.yaml
```
