# Super Prompt: Phase 2 - Concept Extraction

## Context

You are helping build the concept graph for the myPub knowledge base. This is Phase 2 of 5, focused on:
- Extracting concepts from indexed chapters
- Building concept relationships
- Mapping chapters to concepts

**Prerequisite:** Phase 1 complete (books indexed)

## Understanding Concepts

A concept is a canonical idea, technology, methodology, or pattern that appears across multiple sources. Examples:
- Technologies: Spark, Kafka, DuckDB, Delta Lake
- Methodologies: Kimball Dimensional Modeling, Data Vault
- Techniques: SCD Type 2, CDC, Star Schema
- Domains: Healthcare Claims, Risk Adjustment

## Step 1: Seed Core Concepts

Start by adding fundamental concepts for your domains:

```sql
-- Data Engineering core concepts
INSERT INTO concepts (concept_id, name, domain, description, aliases) VALUES
('data_warehouse', 'Data Warehouse', 'data_engineering', 
 'Central repository of integrated data from multiple sources', 
 ARRAY['DW', 'DWH', 'enterprise data warehouse']),
 
('etl', 'ETL', 'data_engineering',
 'Extract, Transform, Load - traditional data integration pattern',
 ARRAY['extract transform load']),
 
('elt', 'ELT', 'data_engineering',
 'Extract, Load, Transform - modern pattern with transformation in warehouse',
 ARRAY['extract load transform']),
 
('dimensional_modeling', 'Dimensional Modeling', 'data_engineering',
 'Technique for designing data warehouses around business processes',
 ARRAY['dimensional model', 'star schema design']),

('cdc', 'Change Data Capture', 'data_engineering',
 'Pattern for capturing incremental changes from source systems',
 ARRAY['CDC', 'change capture', 'incremental capture']),

('data_lake', 'Data Lake', 'data_engineering',
 'Storage repository holding vast amounts of raw data in native format',
 ARRAY['lake']),

('medallion_architecture', 'Medallion Architecture', 'data_engineering',
 'Bronze/Silver/Gold layered data organization pattern',
 ARRAY['bronze silver gold', 'multi-hop architecture']);

-- Healthcare core concepts  
INSERT INTO concepts (concept_id, name, domain, description, aliases) VALUES
('healthcare_claims', 'Healthcare Claims', 'healthcare',
 'Insurance claims for healthcare services',
 ARRAY['medical claims', 'claims data']),
 
('hcc', 'HCC Risk Adjustment', 'healthcare',
 'Hierarchical Condition Category model for Medicare risk adjustment',
 ARRAY['HCC', 'risk adjustment', 'RAF']),

('hedis', 'HEDIS Measures', 'healthcare',
 'Healthcare Effectiveness Data and Information Set quality measures',
 ARRAY['HEDIS', 'quality measures']);

-- Dimensional Modeling concepts
INSERT INTO concepts (concept_id, name, domain, description, aliases) VALUES
('fact_table', 'Fact Table', 'dimensional_modeling',
 'Table containing measurements and metrics of business processes',
 ARRAY['facts']),
 
('dimension_table', 'Dimension Table', 'dimensional_modeling',
 'Table containing descriptive attributes for analysis context',
 ARRAY['dimensions', 'dim']),

('scd', 'Slowly Changing Dimension', 'dimensional_modeling',
 'Technique for tracking historical changes in dimension attributes',
 ARRAY['SCD', 'slowly changing dimensions']),

('scd_type_2', 'SCD Type 2', 'dimensional_modeling',
 'Track history by adding new rows with validity dates',
 ARRAY['SCD2', 'type 2 SCD']),

('surrogate_key', 'Surrogate Key', 'dimensional_modeling',
 'System-generated key replacing natural business keys',
 ARRAY['SK', 'artificial key']);
```

## Step 2: Define Core Relationships

```sql
-- Prerequisites (REQUIRES)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('dimensional_modeling', 'data_warehouse', 'REQUIRES', 0.9),
('dimensional_modeling', 'fact_table', 'REQUIRES', 1.0),
('dimensional_modeling', 'dimension_table', 'REQUIRES', 1.0),
('scd_type_2', 'scd', 'REQUIRES', 1.0),
('scd_type_2', 'dimension_table', 'REQUIRES', 0.8),
('hcc', 'healthcare_claims', 'REQUIRES', 0.9),
('medallion_architecture', 'data_lake', 'REQUIRES', 0.7),
('elt', 'data_warehouse', 'REQUIRES', 0.6);

-- Related concepts (RELATED_TO)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('cdc', 'etl', 'RELATED_TO', 0.7),
('cdc', 'elt', 'RELATED_TO', 0.7),
('fact_table', 'dimension_table', 'RELATED_TO', 1.0),
('data_warehouse', 'data_lake', 'RELATED_TO', 0.6);

-- Contrasting approaches (CONTRASTS_WITH)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('etl', 'elt', 'CONTRASTS_WITH', 0.9),
('data_warehouse', 'data_lake', 'CONTRASTS_WITH', 0.5);

-- Extensions (EXTENDS)
INSERT INTO concept_relationships (source_id, target_id, relationship, strength) VALUES
('scd_type_2', 'scd', 'EXTENDS', 1.0);
```

## Step 3: Extract Concepts from Chapters

For each indexed book, analyze chapters to extract concepts:

### Workflow for Each Chapter

1. **Load chapter content**
   ```sql
   SELECT ch.chapter_id, ch.title, ch.href, b.filepath, b.title AS book
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.key_concepts IS NULL
   LIMIT 1;
   ```

2. **Load via ebook-mcp**
   ```
   ebook-mcp:get_epub_chapter_markdown(filepath, href)
   ```

3. **Analyze with this prompt:**
   ```
   Analyze this chapter and identify:
   1. Key concepts discussed (not just mentioned)
   2. For each concept:
      - Canonical name
      - Treatment level: deep_dive | explain | mention
      - Brief excerpt showing the treatment
   3. Any new concepts not in our existing list
   4. Relationships between concepts revealed
   
   Format as:
   CONCEPTS:
   - concept_name (treatment): "brief excerpt"
   
   NEW CONCEPTS:
   - suggested_name: description
   
   RELATIONSHIPS:
   - concept_a -> REQUIRES -> concept_b
   ```

4. **Update database**
   ```sql
   -- Add chapter-concept mappings
   INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, relevance)
   VALUES ('{chapter_id}', '{concept_id}', '{treatment}', {relevance});
   
   -- Update chapter with key concepts
   UPDATE chapters 
   SET key_concepts = ARRAY['{concept_1}', '{concept_2}', ...]
   WHERE chapter_id = '{chapter_id}';
   ```

## Step 4: Batch Processing Strategy

Process books in priority order:

```sql
-- High-value books to process first
SELECT b.book_id, b.title, b.chapter_count
FROM books b
WHERE b.title ILIKE '%warehouse%'
   OR b.title ILIKE '%dimensional%'
   OR b.title ILIKE '%kimball%'
   OR b.title ILIKE '%healthcare%'
   OR b.title ILIKE '%spark%'
   OR b.title ILIKE '%kafka%'
ORDER BY b.chapter_count DESC;
```

## Step 5: Validate Concept Graph

```sql
-- Concept coverage
SELECT 
    c.domain,
    COUNT(*) AS concept_count,
    SUM(CASE WHEN cc.concept_id IS NOT NULL THEN 1 ELSE 0 END) AS has_chapters
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
GROUP BY c.domain;

-- Orphan concepts (no chapters)
SELECT c.concept_id, c.name
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
WHERE cc.concept_id IS NULL;

-- Most covered concepts
SELECT 
    c.name,
    COUNT(*) AS chapter_count,
    array_agg(DISTINCT cc.treatment) AS treatments
FROM concepts c
JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
GROUP BY c.concept_id, c.name
ORDER BY chapter_count DESC
LIMIT 20;

-- Verify no cycles in REQUIRES
WITH RECURSIVE req_chain AS (
    SELECT source_id, target_id, ARRAY[source_id] AS path
    FROM concept_relationships
    WHERE relationship = 'REQUIRES'
    
    UNION ALL
    
    SELECT cr.source_id, cr.target_id, array_append(rc.path, cr.source_id)
    FROM concept_relationships cr
    JOIN req_chain rc ON cr.source_id = rc.target_id
    WHERE cr.relationship = 'REQUIRES'
      AND NOT array_contains(rc.path, cr.source_id)
      AND array_length(rc.path) < 10
)
SELECT * FROM req_chain WHERE array_contains(path, target_id);
-- Should return empty (no cycles)
```

## Success Criteria for Phase 2

- [ ] 20+ core concepts defined
- [ ] 30+ relationships established
- [ ] At least 50 chapters mapped to concepts
- [ ] No orphan concepts (all have at least one chapter)
- [ ] No cycles in REQUIRES relationships
- [ ] Can query prerequisites and related concepts

## Next Phase

After Phase 2 is complete, proceed to Phase 3: Skills Generation.

Load the Phase 3 super prompt: `tutorials/super-prompt-phase-3.md`
