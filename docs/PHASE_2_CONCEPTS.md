# Phase 2: Concept Extraction

## Objective

Build the concept graph by extracting concepts from indexed chapters and establishing relationships.

## Prerequisites

- Phase 1 complete (catalog database with indexed books)
- Claude access with KB skill loaded

## Steps

### 1. Seed Core Concepts

Start with well-known concepts to anchor the graph:

```sql
-- Connect to catalog
-- duckdb data/catalog.ddb

-- Data Engineering concepts
INSERT INTO concepts (concept_id, name, domain, description) VALUES
('dimensional_modeling', 'Dimensional Modeling', 'data_engineering',
 'Data modeling technique for analytical databases using facts and dimensions'),
('star_schema', 'Star Schema', 'data_engineering',
 'Dimensional model with central fact table surrounded by dimension tables'),
('snowflake_schema', 'Snowflake Schema', 'data_engineering',
 'Normalized variation of star schema with sub-dimensions'),
('fact_table', 'Fact Table', 'data_engineering',
 'Table containing quantitative measures at a specific grain'),
('dimension_table', 'Dimension Table', 'data_engineering',
 'Table containing descriptive attributes for filtering and grouping'),
('slowly_changing_dimension', 'Slowly Changing Dimension', 'data_engineering',
 'Dimension that tracks historical changes over time'),
('scd_type_2', 'SCD Type 2', 'data_engineering',
 'Historical tracking with row versioning'),
('cdc', 'Change Data Capture', 'data_engineering',
 'Technique for identifying and tracking data changes'),
('etl', 'ETL', 'data_engineering',
 'Extract, Transform, Load - traditional data integration pattern'),
('elt', 'ELT', 'data_engineering',
 'Extract, Load, Transform - modern pattern with transformation in warehouse');

-- Healthcare concepts (if applicable)
INSERT INTO concepts (concept_id, name, domain, description) VALUES
('healthcare_claims', 'Healthcare Claims', 'healthcare',
 'Medical billing records for services provided'),
('claim_line', 'Claim Line', 'healthcare',
 'Individual service/procedure on a healthcare claim'),
('diagnosis_code', 'Diagnosis Code', 'healthcare',
 'ICD-10 code representing a medical diagnosis'),
('procedure_code', 'Procedure Code', 'healthcare',
 'CPT/HCPCS code representing a medical procedure'),
('member_eligibility', 'Member Eligibility', 'healthcare',
 'Health plan enrollment and coverage status'),
('hcc', 'HCC Risk Adjustment', 'healthcare',
 'Hierarchical Condition Category for Medicare risk adjustment');
```

### 2. Add Core Relationships

```sql
-- Prerequisites (REQUIRES)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('dimensional_modeling', 'sql_fundamentals', 'REQUIRES', 0.9),
('star_schema', 'dimensional_modeling', 'REQUIRES', 0.95),
('star_schema', 'fact_table', 'REQUIRES', 0.9),
('star_schema', 'dimension_table', 'REQUIRES', 0.9),
('snowflake_schema', 'star_schema', 'REQUIRES', 0.8),
('scd_type_2', 'slowly_changing_dimension', 'REQUIRES', 0.95),
('hcc', 'healthcare_claims', 'REQUIRES', 0.9),
('hcc', 'diagnosis_code', 'REQUIRES', 0.95);

-- Related concepts (RELATED_TO)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('cdc', 'streaming', 'RELATED_TO', 0.7),
('cdc', 'etl', 'RELATED_TO', 0.8),
('dimensional_modeling', 'data_warehouse', 'RELATED_TO', 0.9);

-- Extensions (EXTENDS)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('scd_type_2', 'dimension_table', 'EXTENDS', 0.9),
('star_schema', 'dimensional_modeling', 'EXTENDS', 0.8);

-- Contrasts (CONTRASTS_WITH)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('star_schema', 'snowflake_schema', 'CONTRASTS_WITH', 0.9),
('etl', 'elt', 'CONTRASTS_WITH', 0.95);
```

### 3. Map Chapters to Concepts

Find chapters that discuss each concept:

```sql
-- Example: Find chapters about dimensional modeling
SELECT
    ch.chapter_id,
    ch.title,
    b.title as book,
    ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%dimension%'
   OR ch.title ILIKE '%star schema%'
   OR ch.title ILIKE '%fact%table%';
```

Then create mappings:

```sql
-- Map chapters to concepts
INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, relevance) VALUES
('data-warehouse-toolkit:3', 'dimensional_modeling', 'deep_dive', 0.95),
('data-warehouse-toolkit:4', 'fact_table', 'deep_dive', 0.90),
('data-warehouse-toolkit:5', 'dimension_table', 'deep_dive', 0.90),
('data-warehouse-toolkit:6', 'slowly_changing_dimension', 'deep_dive', 0.92);
```

### 4. Claude-Assisted Extraction

For chapters without obvious concept mappings, ask Claude:

**Prompt:**

```text
I have a chapter from [Book Title] called "[Chapter Title]".
Here's the content: [paste chapter content or chapter_id]

Please identify:
1. Main concepts discussed (with treatment level: mention/explain/deep_dive)
2. New concepts not in our catalog
3. Relationships between concepts mentioned

Our existing concepts include:
[query: SELECT concept_id, name FROM concepts WHERE domain = 'data_engineering']
```

Claude will analyze and suggest:

- New concepts to add
- Chapter-concept mappings
- Relationships to create

### 5. Validate the Graph

```sql
-- Concepts without chapters
SELECT c.name
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
WHERE cc.chapter_id IS NULL;

-- Orphan chapters (no concepts mapped)
SELECT ch.title, b.title as book
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
LEFT JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
WHERE cc.concept_id IS NULL
  AND ch.token_count > 1000;

-- Isolated concepts (no relationships)
SELECT c.name
FROM concepts c
LEFT JOIN concept_relationships cr1 ON c.concept_id = cr1.source_id
LEFT JOIN concept_relationships cr2 ON c.concept_id = cr2.target_id
WHERE cr1.source_id IS NULL AND cr2.target_id IS NULL;
```

### 6. Test Concept Queries

```sql
-- Find prerequisites for a concept
SELECT prereq_name, strength
FROM v_concept_prerequisites
WHERE concept_id = 'dimensional_modeling';

-- Find chapters covering a concept
SELECT book_title, chapter_title, treatment
FROM v_concept_chapters
WHERE concept_id = 'dimensional_modeling'
  AND treatment IN ('explain', 'deep_dive')
ORDER BY relevance DESC;
```

## Validation Checklist

- [ ] 20+ concepts seeded with descriptions
- [ ] 30+ relationships established
- [ ] Key chapters mapped to concepts
- [ ] Prerequisite queries return results
- [ ] No isolated concepts (all have relationships)
- [ ] Claude can answer "What are prerequisites for X?"

## Tips

### Treatment Levels

- **mention**: Brief reference (< 200 tokens)
- **explain**: Substantial coverage (200-1000 tokens)
- **deep_dive**: Primary focus (> 1000 tokens or dedicated section)

### Relationship Strength

- 0.9-1.0: Strong/definite relationship
- 0.7-0.9: Moderate relationship
- 0.5-0.7: Weak/tangential relationship

## Next Phase

Phase 3 and beyond are documented in the canonical specs:

- [mypub-v2-architecture.md](mypub-v2-architecture.md) — full system design including Skills Generation, generators, and ranking
- [mypub-v2-execution-plan.md](mypub-v2-execution-plan.md) — phased roadmap (Phases 3-17)
- [mypub-v2-generators.md](mypub-v2-generators.md) — per-generator specifications

The PHASE_*.md docs in this directory are historical phase notes from the v1 era; the v2 design supersedes them.
