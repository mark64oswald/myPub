-- ============================================================================
-- myPub Catalog Database Schema
-- DuckDB schema for the ePub knowledge base
-- 
-- Usage: duckdb data/catalog.ddb < schemas/catalog.sql
-- ============================================================================

-- ============================================================================
-- CORE CATALOG TABLES
-- ============================================================================

-- Books table - metadata about each ePub
CREATE TABLE IF NOT EXISTS books (
    book_id         VARCHAR PRIMARY KEY,  -- slug from filename
    title           VARCHAR NOT NULL,
    authors         VARCHAR[],            -- DuckDB array of authors
    publisher       VARCHAR,
    pub_date        DATE,
    filepath        VARCHAR NOT NULL,     -- path to ePub file
    description     TEXT,
    subjects        VARCHAR[],            -- subject tags
    total_tokens    INTEGER,              -- estimated token count
    chapter_count   INTEGER,
    indexed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP
);

-- Chapters table - table of contents with metadata
CREATE TABLE IF NOT EXISTS chapters (
    chapter_id      VARCHAR PRIMARY KEY,  -- book_id:sequence
    book_id         VARCHAR NOT NULL REFERENCES books(book_id),
    title           VARCHAR NOT NULL,
    sequence        INTEGER NOT NULL,     -- order in book
    href            VARCHAR,              -- internal ePub reference
    parent_id       VARCHAR,              -- for nested chapters
    token_count     INTEGER,
    summary         TEXT,                 -- AI-generated 2-3 sentences
    key_concepts    VARCHAR[],            -- extracted concept names
    content_type    VARCHAR,              -- 'tutorial', 'reference', 'conceptual'
    difficulty      VARCHAR,              -- 'beginner', 'intermediate', 'advanced'
    indexed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id);
CREATE INDEX IF NOT EXISTS idx_chapters_parent ON chapters(parent_id);


-- ============================================================================
-- CONCEPT GRAPH TABLES
-- ============================================================================

-- Concepts - canonical concepts across all books
CREATE TABLE IF NOT EXISTS concepts (
    concept_id      VARCHAR PRIMARY KEY,  -- slugified name
    name            VARCHAR NOT NULL,     -- display name
    description     TEXT,
    domain          VARCHAR,              -- 'data_engineering', 'healthcare', etc.
    aliases         VARCHAR[],            -- alternative names/spellings
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_concepts_domain ON concepts(domain);

-- Concept relationships - edges in the concept graph
CREATE TABLE IF NOT EXISTS concept_relationships (
    source_id       VARCHAR NOT NULL REFERENCES concepts(concept_id),
    target_id       VARCHAR NOT NULL REFERENCES concepts(concept_id),
    relationship    VARCHAR NOT NULL,     -- REQUIRES, RELATED_TO, EXTENDS, CONTRASTS_WITH
    strength        FLOAT DEFAULT 1.0,    -- 0-1 confidence/relevance
    source_ref      VARCHAR,              -- chapter_id where derived
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, target_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_concept_rel_source ON concept_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_concept_rel_target ON concept_relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_concept_rel_type ON concept_relationships(relationship);

-- Chapter-Concept mapping - which chapters discuss which concepts
CREATE TABLE IF NOT EXISTS chapter_concepts (
    chapter_id      VARCHAR NOT NULL REFERENCES chapters(chapter_id),
    concept_id      VARCHAR NOT NULL REFERENCES concepts(concept_id),
    treatment       VARCHAR,              -- 'mention', 'explain', 'deep_dive'
    relevance       FLOAT DEFAULT 1.0,    -- 0-1 how central to the chapter
    excerpt         TEXT,                 -- brief quote showing treatment
    PRIMARY KEY (chapter_id, concept_id)
);

CREATE INDEX IF NOT EXISTS idx_chapter_concepts_concept ON chapter_concepts(concept_id);
CREATE INDEX IF NOT EXISTS idx_chapter_concepts_treatment ON chapter_concepts(treatment);


-- ============================================================================
-- PATTERN LIBRARY TABLES
-- ============================================================================

-- Patterns - reusable building blocks extracted from books
CREATE TABLE IF NOT EXISTS patterns (
    pattern_id      VARCHAR PRIMARY KEY,  -- hierarchical: domain.category.name
    name            VARCHAR NOT NULL,
    description     TEXT,
    domain          VARCHAR,              -- 'healthcare', 'dimensional_modeling', etc.
    category        VARCHAR,              -- 'facts', 'dimensions', 'metrics', etc.
    problem_statement TEXT,               -- what problem does this solve
    canonical_yaml  TEXT,                 -- full pattern definition as YAML
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patterns_domain ON patterns(domain);
CREATE INDEX IF NOT EXISTS idx_patterns_category ON patterns(category);

-- Pattern sources - which chapters informed this pattern
CREATE TABLE IF NOT EXISTS pattern_sources (
    pattern_id      VARCHAR NOT NULL REFERENCES patterns(pattern_id),
    chapter_id      VARCHAR NOT NULL REFERENCES chapters(chapter_id),
    authority       VARCHAR,              -- 'high', 'medium', 'low'
    contribution    VARCHAR,              -- 'canonical', 'variation', 'extension'
    notes           TEXT,
    PRIMARY KEY (pattern_id, chapter_id)
);

-- Pattern variations - alternative approaches within a pattern
CREATE TABLE IF NOT EXISTS pattern_variations (
    variation_id    VARCHAR PRIMARY KEY,  -- pattern_id:variation_name
    pattern_id      VARCHAR NOT NULL REFERENCES patterns(pattern_id),
    name            VARCHAR NOT NULL,
    description     TEXT,
    when_to_use     TEXT,
    when_not_to_use TEXT,
    variation_yaml  TEXT,                 -- variation-specific YAML
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pattern_variations_pattern ON pattern_variations(pattern_id);

-- Pattern extensions - additive capabilities
CREATE TABLE IF NOT EXISTS pattern_extensions (
    extension_id    VARCHAR PRIMARY KEY,
    pattern_id      VARCHAR NOT NULL REFERENCES patterns(pattern_id),
    name            VARCHAR NOT NULL,
    description     TEXT,
    when_required   TEXT,
    extension_yaml  TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pattern_extensions_pattern ON pattern_extensions(pattern_id);


-- ============================================================================
-- SKILLS TRACKING
-- ============================================================================

-- Skills - generated skill files
CREATE TABLE IF NOT EXISTS skills (
    skill_id        VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    filepath        VARCHAR,              -- where the SKILL.md lives
    domain          VARCHAR,
    description     TEXT,
    source_chapters VARCHAR[],            -- chapter_ids used to generate
    source_patterns VARCHAR[],            -- pattern_ids used
    generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP,
    version         INTEGER DEFAULT 1
);

-- ============================================================================
-- USEFUL VIEWS
-- ============================================================================

-- View: Chapters with book context
CREATE OR REPLACE VIEW v_chapters_with_books AS
SELECT 
    ch.chapter_id,
    ch.title AS chapter_title,
    ch.sequence,
    ch.href,
    ch.token_count,
    ch.summary,
    ch.key_concepts,
    ch.content_type,
    ch.difficulty,
    b.book_id,
    b.title AS book_title,
    b.authors,
    b.publisher,
    b.pub_date,
    b.filepath
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id;

-- View: Concept to chapters mapping with details
CREATE OR REPLACE VIEW v_concept_chapters AS
SELECT 
    c.concept_id,
    c.name AS concept_name,
    c.domain,
    ch.chapter_id,
    ch.title AS chapter_title,
    b.title AS book_title,
    b.authors,
    b.pub_date,
    cc.treatment,
    cc.relevance,
    ch.token_count
FROM concepts c
JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
JOIN chapters ch ON cc.chapter_id = ch.chapter_id
JOIN books b ON ch.book_id = b.book_id;

-- View: Concept prerequisites (one level)
CREATE OR REPLACE VIEW v_concept_prerequisites AS
SELECT 
    c1.concept_id AS concept_id,
    c1.name AS concept_name,
    c2.concept_id AS prereq_id,
    c2.name AS prereq_name,
    cr.strength,
    cr.notes
FROM concepts c1
JOIN concept_relationships cr ON c1.concept_id = cr.source_id
JOIN concepts c2 ON cr.target_id = c2.concept_id
WHERE cr.relationship = 'REQUIRES';

-- View: Related concepts
CREATE OR REPLACE VIEW v_concept_related AS
SELECT 
    c1.concept_id AS concept_id,
    c1.name AS concept_name,
    c2.concept_id AS related_id,
    c2.name AS related_name,
    cr.relationship,
    cr.strength
FROM concepts c1
JOIN concept_relationships cr ON c1.concept_id = cr.source_id
JOIN concepts c2 ON cr.target_id = c2.concept_id;

-- View: Patterns with source info
CREATE OR REPLACE VIEW v_patterns_with_sources AS
SELECT 
    p.pattern_id,
    p.name AS pattern_name,
    p.domain,
    p.category,
    p.description,
    array_agg(DISTINCT b.title) AS source_books,
    array_agg(DISTINCT ps.authority) AS authorities
FROM patterns p
LEFT JOIN pattern_sources ps ON p.pattern_id = ps.pattern_id
LEFT JOIN chapters ch ON ps.chapter_id = ch.chapter_id
LEFT JOIN books b ON ch.book_id = b.book_id
GROUP BY p.pattern_id, p.name, p.domain, p.category, p.description;

-- View: Book coverage by domain
CREATE OR REPLACE VIEW v_domain_coverage AS
SELECT 
    c.domain,
    COUNT(DISTINCT c.concept_id) AS concept_count,
    COUNT(DISTINCT cc.chapter_id) AS chapter_count,
    COUNT(DISTINCT ch.book_id) AS book_count
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
LEFT JOIN chapters ch ON cc.chapter_id = ch.chapter_id
GROUP BY c.domain
ORDER BY concept_count DESC;


-- ============================================================================
-- COMMON QUERY PATTERNS (save as reference)
-- ============================================================================

-- These are example queries that can be used as templates.
-- They are commented out to not execute during schema creation.

/*
-- Find chapters for a concept, ranked by treatment depth
SELECT 
    book_title,
    chapter_title,
    treatment,
    token_count,
    summary
FROM v_concept_chapters
WHERE concept_id = 'dimensional_modeling'
ORDER BY 
    CASE treatment 
        WHEN 'deep_dive' THEN 1 
        WHEN 'explain' THEN 2 
        WHEN 'mention' THEN 3 
    END,
    pub_date DESC;

-- Find prerequisites (recursive, up to 3 levels)
WITH RECURSIVE prereq_chain AS (
    SELECT 
        target_id AS concept_id,
        1 AS depth,
        ARRAY[source_id] AS path
    FROM concept_relationships
    WHERE source_id = 'dimensional_modeling'
      AND relationship = 'REQUIRES'
    
    UNION ALL
    
    SELECT 
        cr.target_id,
        pc.depth + 1,
        array_append(pc.path, cr.source_id)
    FROM concept_relationships cr
    JOIN prereq_chain pc ON cr.source_id = pc.concept_id
    WHERE cr.relationship = 'REQUIRES'
      AND pc.depth < 3
      AND NOT array_contains(pc.path, cr.target_id)
)
SELECT DISTINCT c.name, pc.depth
FROM prereq_chain pc
JOIN concepts c ON pc.concept_id = c.concept_id
ORDER BY pc.depth, c.name;

-- Find different author perspectives on a topic
SELECT 
    authors[1] AS primary_author,
    book_title,
    chapter_title,
    treatment,
    pub_date,
    summary
FROM v_concept_chapters
WHERE concept_id = 'data_warehouse_architecture'
  AND treatment IN ('explain', 'deep_dive')
ORDER BY pub_date DESC;

-- Find concepts that co-occur in chapters (related topics)
WITH my_chapters AS (
    SELECT chapter_id FROM chapter_concepts WHERE concept_id = 'cdc'
)
SELECT 
    c.name,
    COUNT(*) AS co_occurrence_count
FROM chapter_concepts cc
JOIN concepts c ON cc.concept_id = c.concept_id
WHERE cc.chapter_id IN (SELECT chapter_id FROM my_chapters)
  AND cc.concept_id != 'cdc'
GROUP BY c.name
ORDER BY co_occurrence_count DESC
LIMIT 10;

-- Learning path: what to read for a concept (ordered by prerequisites)
WITH RECURSIVE learning_path AS (
    SELECT 
        'target_concept' AS concept_id,
        0 AS level,
        ARRAY['target_concept'] AS path
    
    UNION ALL
    
    SELECT 
        cr.target_id,
        lp.level + 1,
        array_append(lp.path, cr.target_id)
    FROM concept_relationships cr
    JOIN learning_path lp ON cr.source_id = lp.concept_id
    WHERE cr.relationship = 'REQUIRES'
      AND lp.level < 5
      AND NOT array_contains(lp.path, cr.target_id)
)
SELECT 
    c.name,
    lp.level AS learn_order,
    (SELECT vcc.chapter_title || ' (' || vcc.book_title || ')'
     FROM v_concept_chapters vcc
     WHERE vcc.concept_id = lp.concept_id
       AND vcc.treatment = 'deep_dive'
     LIMIT 1) AS recommended_chapter
FROM learning_path lp
JOIN concepts c ON lp.concept_id = c.concept_id
ORDER BY lp.level DESC;

-- Search across books and chapters (full text)
SELECT 
    b.title AS book_title,
    ch.title AS chapter_title,
    ch.summary
FROM books b
JOIN chapters ch ON b.book_id = ch.book_id
WHERE b.title ILIKE '%data%warehouse%'
   OR ch.title ILIKE '%dimensional%'
   OR ch.summary ILIKE '%kimball%';

-- Get pattern with all variations
SELECT 
    p.pattern_id,
    p.name,
    p.canonical_yaml,
    pv.variation_id,
    pv.name AS variation_name,
    pv.when_to_use
FROM patterns p
LEFT JOIN pattern_variations pv ON p.pattern_id = pv.pattern_id
WHERE p.pattern_id = 'healthcare.dimensional.fct_claim_line';
*/

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
