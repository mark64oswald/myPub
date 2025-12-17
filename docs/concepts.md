# Concept Graph Guide

## Overview

The concept graph represents knowledge relationships across your ePub collection. It enables discovery, learning path generation, and multi-perspective exploration.

## Concept Structure

### Concept Record

```sql
concepts
├── concept_id     -- Unique identifier (slugified)
├── name           -- Display name
├── description    -- Brief explanation
├── domain         -- Category (data_engineering, healthcare, etc.)
└── aliases        -- Alternative names/spellings
```

### Example

```sql
INSERT INTO concepts VALUES (
    'change_data_capture',
    'Change Data Capture',
    'A technique for identifying and tracking changes in source data',
    'data_engineering',
    ['CDC', 'incremental extraction', 'delta detection']
);
```

## Relationship Types

### REQUIRES (Prerequisites)

Indicates concept A requires understanding of concept B first.

```
dimensional_modeling ──REQUIRES──► sql_fundamentals
dimensional_modeling ──REQUIRES──► data_warehouse_concepts
star_schema ──REQUIRES──► dimensional_modeling
```

Use for: Learning path generation, prerequisite checking

### RELATED_TO (Association)

Concepts that are often discussed together or share context.

```
cdc ──RELATED_TO──► event_sourcing
cdc ──RELATED_TO──► streaming_pipelines
cdc ──RELATED_TO──► database_replication
```

Use for: Topic exploration, "see also" suggestions

### EXTENDS (Builds Upon)

Concept A is a specialized version or extension of concept B.

```
scd_type_2 ──EXTENDS──► slowly_changing_dimension
accumulating_snapshot ──EXTENDS──► fact_table
```

Use for: Understanding specializations, drilling down

### CONTRASTS_WITH (Alternative Approaches)

Concepts that represent different approaches to the same problem.

```
kimball_methodology ──CONTRASTS_WITH──► inmon_methodology
star_schema ──CONTRASTS_WITH──► snowflake_schema
batch_processing ──CONTRASTS_WITH──► stream_processing
```

Use for: Comparison requests, methodology discussions

## Graph Queries

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
    
    -- Recursive case: prerequisites of prerequisites
    SELECT 
        cr.target_id,
        pc.depth + 1,
        array_append(pc.path, cr.source_id)
    FROM concept_relationships cr
    JOIN prereq_chain pc ON cr.source_id = pc.concept_id
    WHERE cr.relationship = 'REQUIRES'
      AND pc.depth < 3  -- Limit depth
      AND NOT array_contains(pc.path, cr.target_id)  -- Prevent cycles
)
SELECT DISTINCT c.name, MIN(pc.depth) AS depth
FROM prereq_chain pc
JOIN concepts c ON pc.concept_id = c.concept_id
GROUP BY c.name
ORDER BY depth;
```

### Find Related Concepts

```sql
SELECT 
    related_name,
    relationship,
    strength
FROM v_concept_related
WHERE concept_id = 'cdc'
ORDER BY strength DESC;
```

### Find Contrasting Approaches

```sql
SELECT 
    c2.name AS alternative,
    cr.notes
FROM concept_relationships cr
JOIN concepts c2 ON cr.target_id = c2.concept_id
WHERE cr.source_id = 'kimball_methodology'
  AND cr.relationship = 'CONTRASTS_WITH';
```

### Learning Path Generation

```sql
-- Generate ordered reading list for a target concept
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
    lp.level,
    (SELECT chapter_title || ' (' || book_title || ')'
     FROM v_concept_chapters vcc
     WHERE vcc.concept_id = lp.concept_id
       AND vcc.treatment = 'deep_dive'
     LIMIT 1) AS recommended_reading
FROM learning_path lp
JOIN concepts c ON lp.concept_id = c.concept_id
ORDER BY lp.level DESC;  -- Start with fundamentals
```

## Chapter-Concept Mapping

### Treatment Levels

| Treatment | Meaning | Token Threshold |
|-----------|---------|-----------------|
| `mention` | Brief reference | < 200 tokens about concept |
| `explain` | Substantial coverage | 200-1000 tokens |
| `deep_dive` | Primary focus | > 1000 tokens or dedicated section |

### Mapping Example

```sql
INSERT INTO chapter_concepts VALUES (
    'data-warehouse-toolkit:7',  -- chapter_id
    'dimensional_modeling',       -- concept_id
    'deep_dive',                  -- treatment
    0.95,                         -- relevance (0-1)
    'This chapter introduces the dimensional modeling technique...'
);
```

## Building the Graph

### Phase 1: Seed Concepts

Start with well-known concepts from your domain:

```sql
-- Data Engineering concepts
INSERT INTO concepts (concept_id, name, domain) VALUES
('dimensional_modeling', 'Dimensional Modeling', 'data_engineering'),
('star_schema', 'Star Schema', 'data_engineering'),
('snowflake_schema', 'Snowflake Schema', 'data_engineering'),
('fact_table', 'Fact Table', 'data_engineering'),
('dimension_table', 'Dimension Table', 'data_engineering'),
('slowly_changing_dimension', 'Slowly Changing Dimension', 'data_engineering'),
('cdc', 'Change Data Capture', 'data_engineering'),
('etl', 'ETL', 'data_engineering'),
('elt', 'ELT', 'data_engineering');
```

### Phase 2: Add Relationships

Connect concepts based on book content:

```sql
INSERT INTO concept_relationships (source_id, target_id, relationship) VALUES
('dimensional_modeling', 'sql_fundamentals', 'REQUIRES'),
('star_schema', 'dimensional_modeling', 'REQUIRES'),
('star_schema', 'snowflake_schema', 'CONTRASTS_WITH'),
('scd_type_2', 'slowly_changing_dimension', 'EXTENDS');
```

### Phase 3: Map Chapters

Link chapters to concepts they cover:

```sql
INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, relevance) VALUES
('data-warehouse-toolkit:3', 'dimensional_modeling', 'deep_dive', 0.95),
('data-warehouse-toolkit:4', 'fact_table', 'deep_dive', 0.90),
('data-warehouse-toolkit:5', 'dimension_table', 'deep_dive', 0.90);
```

### Phase 4: Claude-Assisted Enrichment

Use Claude to analyze chapters and suggest:
- New concepts to add
- Relationships between concepts
- Chapter-concept mappings

See: `scripts/extract_concepts.py`

## Maintenance

### Adding New Books

1. Index the book: `python scripts/index_books.py --book new-book.epub`
2. Extract concepts: Work with Claude to identify concepts in new chapters
3. Add mappings: Link new chapters to existing concepts
4. Add new concepts: Create any concepts unique to this book

### Handling Conflicts

When sources disagree:
1. Note both perspectives in concept description
2. Use `CONTRASTS_WITH` relationship
3. Document in pattern variations if applicable

### Quality Checks

```sql
-- Concepts without chapters
SELECT c.name FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
WHERE cc.chapter_id IS NULL;

-- Orphan chapters (no concepts)
SELECT ch.title, b.title 
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
LEFT JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
WHERE cc.concept_id IS NULL
  AND ch.token_count > 1000;

-- Isolated concepts (no relationships)
SELECT c.name FROM concepts c
LEFT JOIN concept_relationships cr1 ON c.concept_id = cr1.source_id
LEFT JOIN concept_relationships cr2 ON c.concept_id = cr2.target_id
WHERE cr1.source_id IS NULL AND cr2.target_id IS NULL;
```
