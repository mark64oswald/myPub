# Super Prompt: Phase 5 - Full Indexing and Refinement

## Context

You are completing the myPub knowledge base setup. This is Phase 5 of 5, focused on:
- Indexing remaining books
- Refining concept extraction
- Completing documentation
- Preparing for use

**Prerequisite:** Phases 1-4 complete

## Step 1: Complete Book Indexing

Index all remaining books:

```bash
# Index all books (may take a while for 300+ books)
python scripts/index_books.py --source ~/Documents/ebooks --verbose 2>&1 | tee indexing.log
```

Review results:
```sql
-- Summary
SELECT 
    COUNT(*) AS total_books,
    SUM(chapter_count) AS total_chapters,
    SUM(total_tokens) AS total_tokens,
    AVG(chapter_count) AS avg_chapters_per_book
FROM books;

-- Books with issues (no chapters)
SELECT book_id, title, filepath
FROM books
WHERE chapter_count = 0 OR chapter_count IS NULL;

-- Token distribution
SELECT 
    CASE 
        WHEN total_tokens < 50000 THEN 'Small (<50K)'
        WHEN total_tokens < 200000 THEN 'Medium (50-200K)'
        WHEN total_tokens < 500000 THEN 'Large (200-500K)'
        ELSE 'Very Large (>500K)'
    END AS size_category,
    COUNT(*) AS book_count,
    AVG(chapter_count) AS avg_chapters
FROM books
GROUP BY 1
ORDER BY 1;
```

## Step 2: Expand Concept Coverage

Identify gaps in concept coverage:

```sql
-- Chapters without concept mappings
SELECT 
    b.title AS book,
    COUNT(*) AS unmapped_chapters
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
LEFT JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
WHERE cc.chapter_id IS NULL
GROUP BY b.book_id, b.title
ORDER BY unmapped_chapters DESC
LIMIT 20;

-- Concepts with few chapters
SELECT 
    c.name,
    c.domain,
    COUNT(cc.chapter_id) AS chapter_count
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
GROUP BY c.concept_id, c.name, c.domain
HAVING COUNT(cc.chapter_id) < 3
ORDER BY chapter_count;
```

For high-value books with unmapped chapters, run concept extraction:

```sql
-- Get chapters from high-value books needing mapping
SELECT ch.chapter_id, ch.title, b.title AS book, b.filepath, ch.href
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
LEFT JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
WHERE cc.chapter_id IS NULL
  AND (b.title ILIKE '%kimball%' 
       OR b.title ILIKE '%warehouse%'
       OR b.title ILIKE '%healthcare%'
       OR b.title ILIKE '%spark%')
ORDER BY b.title, ch.sequence;
```

## Step 3: Refine Concept Graph

### Add Missing Relationships

```sql
-- Find concepts that might need REQUIRES relationships
-- (co-occur frequently but no relationship defined)
WITH concept_pairs AS (
    SELECT 
        cc1.concept_id AS concept_a,
        cc2.concept_id AS concept_b,
        COUNT(*) AS co_occurrences
    FROM chapter_concepts cc1
    JOIN chapter_concepts cc2 ON cc1.chapter_id = cc2.chapter_id
    WHERE cc1.concept_id < cc2.concept_id
    GROUP BY cc1.concept_id, cc2.concept_id
    HAVING COUNT(*) >= 3
)
SELECT 
    c1.name AS concept_a,
    c2.name AS concept_b,
    cp.co_occurrences,
    CASE WHEN cr.relationship IS NOT NULL THEN 'Has relationship' ELSE 'No relationship' END AS status
FROM concept_pairs cp
JOIN concepts c1 ON cp.concept_a = c1.concept_id
JOIN concepts c2 ON cp.concept_b = c2.concept_id
LEFT JOIN concept_relationships cr 
    ON (cr.source_id = cp.concept_a AND cr.target_id = cp.concept_b)
    OR (cr.source_id = cp.concept_b AND cr.target_id = cp.concept_a)
WHERE cr.relationship IS NULL
ORDER BY cp.co_occurrences DESC
LIMIT 20;
```

### Merge Duplicate Concepts

```sql
-- Find potential duplicates (similar names)
SELECT c1.concept_id, c1.name, c2.concept_id AS dup_id, c2.name AS dup_name
FROM concepts c1
JOIN concepts c2 ON c1.concept_id < c2.concept_id
WHERE c1.name ILIKE '%' || c2.name || '%'
   OR c2.name ILIKE '%' || c1.name || '%';
```

For duplicates, merge by adding aliases:
```sql
UPDATE concepts 
SET aliases = array_append(aliases, 'duplicate_name')
WHERE concept_id = 'canonical_id';

-- Then reassign chapter_concepts
UPDATE chapter_concepts 
SET concept_id = 'canonical_id'
WHERE concept_id = 'duplicate_id';

-- Delete duplicate
DELETE FROM concepts WHERE concept_id = 'duplicate_id';
```

## Step 4: Generate Chapter Summaries

For chapters without summaries, generate them:

```sql
-- Chapters needing summaries
SELECT ch.chapter_id, ch.title, b.title AS book, b.filepath, ch.href
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.summary IS NULL
  AND ch.token_count > 1000  -- Skip very short chapters
ORDER BY b.title, ch.sequence
LIMIT 50;
```

For each, load chapter and generate summary:

**Prompt:**
```
Summarize this chapter in 2-3 sentences. Focus on:
- What is the main topic?
- What will the reader learn?
- What are the key takeaways?

Keep it concise and informative.
```

Update:
```sql
UPDATE chapters 
SET summary = '{generated_summary}'
WHERE chapter_id = '{chapter_id}';
```

## Step 5: Complete Pattern Library

Ensure pattern coverage for key domains:

```sql
-- Pattern coverage by domain
SELECT 
    domain,
    category,
    COUNT(*) AS pattern_count
FROM patterns
GROUP BY domain, category
ORDER BY domain, category;
```

Minimum target patterns:

**Healthcare:**
- [ ] fct_claim_line
- [ ] fct_claim_header
- [ ] dim_member
- [ ] dim_provider
- [ ] dim_diagnosis
- [ ] dim_procedure
- [ ] metrics/pmpm
- [ ] metrics/mlr

**Dimensional Modeling:**
- [ ] facts/transaction_fact
- [ ] facts/periodic_snapshot
- [ ] facts/accumulating_snapshot
- [ ] dimensions/scd_type_2
- [ ] dimensions/role_playing
- [ ] dimensions/bridge_table
- [ ] common/date_dimension
- [ ] common/surrogate_key

**Data Engineering:**
- [ ] ingestion/cdc_pattern
- [ ] ingestion/batch_extract
- [ ] transformation/medallion
- [ ] transformation/deduplication
- [ ] quality/completeness_check
- [ ] quality/referential_check

## Step 6: Validate System

### Test Suite

Run these validation queries:

```sql
-- 1. Can find content for major topics
SELECT COUNT(*) > 0 AS dimensional_modeling_found
FROM v_concept_chapters 
WHERE concept_name = 'Dimensional Modeling';

-- 2. Prerequisites work
SELECT COUNT(*) > 0 AS prereqs_work
FROM v_concept_prerequisites
WHERE concept_name = 'SCD Type 2';

-- 3. Patterns load correctly
SELECT COUNT(*) > 0 AS patterns_exist
FROM patterns
WHERE domain = 'healthcare';

-- 4. Skills registered
SELECT COUNT(*) > 0 AS skills_registered
FROM skills;
```

### Integration Test

Test end-to-end workflow:

1. **Search:** `/kb-search dimensional modeling`
2. **Load chapter:** Use ebook-mcp to load recommended chapter
3. **Get pattern:** Query for dimensional modeling patterns
4. **Generate code:** Ask Claude to generate DDL using pattern

## Step 7: Documentation

Ensure documentation is complete:

- [x] README.md - Project overview
- [x] docs/architecture.md - System design
- [x] docs/concepts.md - Concept graph guide
- [x] docs/patterns.md - Pattern library guide
- [ ] docs/quickstart.md - Getting started guide
- [ ] docs/troubleshooting.md - Common issues
- [ ] CHANGELOG.md - Version history

## Step 8: Final Statistics

Generate final report:

```sql
-- Final statistics
SELECT 'Books' AS metric, COUNT(*) AS value FROM books
UNION ALL
SELECT 'Chapters', COUNT(*) FROM chapters
UNION ALL
SELECT 'Total Tokens', SUM(token_count) FROM chapters
UNION ALL
SELECT 'Concepts', COUNT(*) FROM concepts
UNION ALL
SELECT 'Relationships', COUNT(*) FROM concept_relationships
UNION ALL
SELECT 'Chapter-Concept Mappings', COUNT(*) FROM chapter_concepts
UNION ALL
SELECT 'Patterns', COUNT(*) FROM patterns
UNION ALL
SELECT 'Pattern Variations', COUNT(*) FROM pattern_variations
UNION ALL
SELECT 'Skills', COUNT(*) FROM skills;
```

## Success Criteria for Phase 5

- [ ] All books indexed (300+)
- [ ] 90%+ chapters have concepts mapped
- [ ] 50%+ chapters have summaries
- [ ] 20+ patterns documented
- [ ] All domain skills complete
- [ ] Documentation complete
- [ ] Test queries pass

## Ongoing Maintenance

After initial setup, maintain with:

1. **New books:** `python scripts/index_books.py --book new-book.epub`
2. **New concepts:** Add via SQL, run extraction on relevant chapters
3. **New patterns:** Extract from chapters, document variations
4. **Skill updates:** Regenerate when new content added

## Congratulations!

Your myPub knowledge base is now ready for use. Key capabilities:

- **Learning:** "Explain [topic]" → retrieves best chapters
- **Comparing:** "Compare [A] vs [B]" → multiple perspectives
- **Building:** "Build [artifact]" → patterns with variations
- **Research:** "What do experts say about [topic]" → synthesis

Enjoy your AI-augmented technical library!
