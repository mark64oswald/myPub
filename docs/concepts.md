# Concept Graph Guide

## Overview

The concept graph represents relationships between ideas, technologies, and methodologies across your ePub collection. Unlike a vector database that finds similar text, the concept graph captures semantic relationships.

## Concept Types

Concepts can represent:

- **Technologies**: Spark, Kafka, DuckDB, dbt
- **Methodologies**: Dimensional Modeling, Data Vault, Medallion Architecture
- **Techniques**: SCD Type 2, CDC, Deduplication
- **Domains**: Healthcare Analytics, Claims Processing, Risk Adjustment
- **Patterns**: Fact Tables, Bridge Tables, Slowly Changing Dimensions

## Relationship Types

### REQUIRES (Prerequisites)

One concept requires understanding another first.

```text
Dimensional Modeling → REQUIRES → SQL Fundamentals
SCD Type 2 → REQUIRES → Dimensional Modeling
HCC Risk Adjustment → REQUIRES → Healthcare Claims
```

### RELATED_TO (Associated Concepts)

Concepts frequently discussed together or in similar contexts.

```text
CDC → RELATED_TO → Event Sourcing
Kafka → RELATED_TO → Streaming
Data Warehouse → RELATED_TO → Data Lake
```

### EXTENDS (Builds Upon)

One concept extends or specializes another.

```text
SCD Type 2 → EXTENDS → Slowly Changing Dimensions
Data Lakehouse → EXTENDS → Data Lake
```

### CONTRASTS_WITH (Alternative Approaches)

Concepts that represent different approaches to similar problems.

```text
Kimball → CONTRASTS_WITH → Inmon
Batch Processing → CONTRASTS_WITH → Stream Processing
Star Schema → CONTRASTS_WITH → Snowflake Schema
```

### IMPLEMENTS (Realization)

A technology implements a concept or pattern.

```text
Delta Lake → IMPLEMENTS → ACID Transactions
dbt → IMPLEMENTS → ELT Pattern
```

## Chapter-Concept Mapping

Each chapter can discuss multiple concepts with different levels of treatment:

### Treatment Levels

| Level | Description | Token Hint |
|-------|-------------|------------|
| `deep_dive` | Primary focus, extensive coverage | Load first |
| `explain` | Explains the concept with context | Good secondary source |
| `mention` | References but doesn't explain | Skip unless needed |

### Relevance Score

0.0 to 1.0 indicating how central the concept is to the chapter:

- 1.0: Chapter is primarily about this concept
- 0.5: Significant discussion
- 0.2: Brief mention in context

## SQL Queries for Concepts

### Find a Concept

```sql
SELECT concept_id, name, description, domain, aliases
FROM concepts
WHERE name ILIKE '%dimensional%'
   OR '%dimensional%' = ANY(aliases);
```

### Find Prerequisites (Direct)

```sql
SELECT prereq_name, strength
FROM v_concept_prerequisites
WHERE concept_id = 'dimensional_modeling';
```

### Find Prerequisites (Recursive)

```sql
WITH RECURSIVE prereq_chain AS (
    -- Base case: direct prerequisites
    SELECT
        target_id AS concept_id,
        1 AS depth,
        ARRAY[source_id] AS path
    FROM concept_relationships
    WHERE source_id = 'dimensional_modeling'
      AND relationship = 'REQUIRES'

    UNION ALL

    -- Recursive case
    SELECT
        cr.target_id,
        pc.depth + 1,
        array_append(pc.path, cr.source_id)
    FROM concept_relationships cr
    JOIN prereq_chain pc ON cr.source_id = pc.concept_id
    WHERE cr.relationship = 'REQUIRES'
      AND pc.depth < 5
      AND NOT array_contains(pc.path, cr.target_id)  -- Prevent cycles
)
SELECT DISTINCT
    c.name,
    MIN(pc.depth) AS depth
FROM prereq_chain pc
JOIN concepts c ON pc.concept_id = c.concept_id
GROUP BY c.name
ORDER BY depth;
```

### Find Related Concepts (Co-occurrence)

```sql
WITH my_chapters AS (
    SELECT chapter_id
    FROM chapter_concepts
    WHERE concept_id = 'cdc'
)
SELECT
    c.name,
    COUNT(*) AS shared_chapters,
    array_agg(DISTINCT cc.treatment) AS treatments
FROM chapter_concepts cc
JOIN concepts c ON cc.concept_id = c.concept_id
WHERE cc.chapter_id IN (SELECT chapter_id FROM my_chapters)
  AND cc.concept_id != 'cdc'
GROUP BY c.concept_id, c.name
ORDER BY shared_chapters DESC
LIMIT 10;
```

### Generate Learning Path

```sql
WITH RECURSIVE learning_path AS (
    SELECT
        'target_concept' AS concept_id,
        0 AS level

    UNION ALL

    SELECT
        cr.target_id,
        lp.level + 1
    FROM concept_relationships cr
    JOIN learning_path lp ON cr.source_id = lp.concept_id
    WHERE cr.relationship = 'REQUIRES'
      AND lp.level < 5
)
SELECT
    c.name,
    lp.level AS learn_order,
    (SELECT vcc.book_title || ': ' || vcc.chapter_title
     FROM v_concept_chapters vcc
     WHERE vcc.concept_id = lp.concept_id
       AND vcc.treatment = 'deep_dive'
     LIMIT 1) AS recommended_reading
FROM learning_path lp
JOIN concepts c ON lp.concept_id = c.concept_id
GROUP BY c.concept_id, c.name, lp.level
ORDER BY lp.level DESC;  -- Start with fundamentals
```

## Building the Concept Graph

### Phase 1: Seed Concepts

Start with core concepts from your domains:

```sql
INSERT INTO concepts (concept_id, name, domain, description) VALUES
('dimensional_modeling', 'Dimensional Modeling', 'data_engineering',
 'Technique for organizing data warehouses around business processes'),
('kimball', 'Kimball Methodology', 'data_engineering',
 'Bottom-up approach to data warehouse design using conformed dimensions'),
('cdc', 'Change Data Capture', 'data_engineering',
 'Pattern for capturing incremental changes from source systems'),
('healthcare_claims', 'Healthcare Claims', 'healthcare',
 'Insurance claim records for healthcare services');
```

### Phase 2: Extract from Chapters

Use Claude to analyze chapters and identify concepts:

1. Load a chapter via ebook-mcp
2. Ask: "What concepts does this chapter explain?"
3. For each concept, determine treatment level
4. Save to chapter_concepts table

### Phase 3: Build Relationships

Relationships emerge from:

- Explicit statements ("Before learning X, you should know Y")
- Chapter structure (concepts in prerequisites section)
- Co-occurrence analysis (concepts discussed together)
- Domain knowledge (CDC is related to streaming)

### Phase 4: Validate and Refine

- Check for cycles in REQUIRES relationships
- Verify relationship types make sense
- Merge duplicate concepts via aliases
- Adjust strengths based on usage

## Using the Memory MCP

For dynamic exploration during conversations, use the Memory MCP:

```text
1. Create entities for concepts being discussed
2. Create relations as you discover connections
3. Search to find previously discussed concepts
4. Build session-specific concept maps
```

This complements the persistent DuckDB graph with ephemeral exploration.
