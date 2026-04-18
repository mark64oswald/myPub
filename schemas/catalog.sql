-- ============================================================================
-- myPub v2 Catalog Schema
-- DuckDB schema for the knowledge-base substrate.
-- Source of truth: docs/mypub-v2-architecture.md §7.1 and §5.2.
--
-- Usage: duckdb data/catalog.ddb < schemas/catalog.sql
--
-- Conventions:
--   * Singular table names (book, chapter, concept, …).
--   * BIGINT primary keys backed by dedicated SEQUENCE objects
--     (DuckDB 1.5 does not support GENERATED ... AS IDENTITY).
--   * Embeddings live in side tables (chapter_embedding, concept_embedding,
--     doc_snapshot_embedding, doc_section_embedding) keyed 1:1 by the
--     entity's PK. This departs from arch doc §7.1 which inlines the
--     embedding column; the reason is a DuckDB 1.5.0 bug — UPDATE on a
--     FLOAT[N] column fails with a spurious FK violation if the row is
--     referenced by any inbound FK. Side tables sidestep it (we INSERT
--     rather than UPDATE) and are arguably cleaner: embeddings are
--     derived data, separable from source metadata, and the model
--     backing them can change without touching the primary tables.
--   * Polymorphic provenance uses (source_type, source_id) pairs; these
--     cannot be enforced as DuckDB FKs and are validated in application code.
-- ============================================================================


-- ============================================================================
-- SEQUENCES (one per table with a surrogate PK)
-- ============================================================================

CREATE SEQUENCE seq_author_id                    START 1;
CREATE SEQUENCE seq_book_id                      START 1;
CREATE SEQUENCE seq_chapter_id                   START 1;
CREATE SEQUENCE seq_concept_id                   START 1;
CREATE SEQUENCE seq_concept_alias_id             START 1;
CREATE SEQUENCE seq_concept_resolution_queue_id  START 1;
CREATE SEQUENCE seq_concept_query_log_id         START 1;
CREATE SEQUENCE seq_doc_source_id                START 1;
CREATE SEQUENCE seq_doc_snapshot_id              START 1;
CREATE SEQUENCE seq_doc_section_id               START 1;
CREATE SEQUENCE seq_procedure_id                 START 1;
CREATE SEQUENCE seq_skill_package_id             START 1;
CREATE SEQUENCE seq_skill_id                     START 1;
CREATE SEQUENCE seq_skill_file_id                START 1;
CREATE SEQUENCE seq_discovery_log_id             START 1;


-- ============================================================================
-- AUTHOR / BOOK / CHAPTER
-- ============================================================================

CREATE TABLE author (
    author_id  BIGINT   PRIMARY KEY DEFAULT nextval('seq_author_id'),
    name       VARCHAR  NOT NULL,
    UNIQUE (name)
);

CREATE TABLE book (
    book_id          BIGINT     PRIMARY KEY DEFAULT nextval('seq_book_id'),
    title            VARCHAR    NOT NULL,
    publisher        VARCHAR,
    publication_date DATE,
    source_path      VARCHAR    NOT NULL,
    description      TEXT,
    subjects         VARCHAR[],
    total_tokens     INTEGER,
    chapter_count    INTEGER,
    content_hash     VARCHAR,                      -- SHA-256 of the ePub file, for /kb-index change detection
    last_indexed_at  TIMESTAMP,                    -- when this book was last fully processed
    status           VARCHAR    DEFAULT 'active',  -- 'active' | 'superseded' (retired editions)
    indexed_at       TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP,
    UNIQUE (source_path)
);

-- Many-to-many book ↔ author (most technical books have multiple authors).
CREATE TABLE book_author (
    book_id   BIGINT  NOT NULL REFERENCES book(book_id),
    author_id BIGINT  NOT NULL REFERENCES author(author_id),
    position  INTEGER,
    PRIMARY KEY (book_id, author_id)
);

CREATE TABLE chapter (
    chapter_id        BIGINT     PRIMARY KEY DEFAULT nextval('seq_chapter_id'),
    book_id           BIGINT     NOT NULL REFERENCES book(book_id),
    chapter_num       INTEGER,
    parent_chapter_id BIGINT,  -- logical self-ref; FK omitted (DuckDB 1.5 per-row
                               -- checker mis-blocks UPDATE/DELETE even when the
                               -- new value is NULL). Application enforces.
    title             VARCHAR,
    href              VARCHAR,
    content           TEXT,
    content_hash      VARCHAR,  -- SHA-256 of content, for chapter-level diffing during re-index
    token_count       INTEGER,
    indexed_at        TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_chapter_book   ON chapter(book_id);
CREATE INDEX idx_chapter_parent ON chapter(parent_chapter_id);

-- Chapter embeddings (one-to-one with chapter, populated by prompt 1.3).
CREATE TABLE chapter_embedding (
    chapter_id BIGINT     PRIMARY KEY REFERENCES chapter(chapter_id),
    embedding  FLOAT[384] NOT NULL,
    model      VARCHAR    NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    created_at TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================================
-- CONCEPT + ENTITY RESOLUTION
-- ============================================================================

CREATE TABLE concept (
    concept_id       BIGINT     PRIMARY KEY DEFAULT nextval('seq_concept_id'),
    name             VARCHAR    NOT NULL,
    concept_type     VARCHAR,
    description      TEXT,
    domain           VARCHAR,
    pending_review   BOOLEAN    DEFAULT FALSE,
    query_count      BIGINT     DEFAULT 0,
    last_queried_at  TIMESTAMP,
    created_at       TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP,
    UNIQUE (name, concept_type)
);

CREATE INDEX idx_concept_domain ON concept(domain);
CREATE INDEX idx_concept_type   ON concept(concept_type);

-- Concept embeddings (one-to-one with concept).
CREATE TABLE concept_embedding (
    concept_id BIGINT     PRIMARY KEY REFERENCES concept(concept_id),
    embedding  FLOAT[384] NOT NULL,
    model      VARCHAR    NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    created_at TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE concept_alias (
    alias_id    BIGINT   PRIMARY KEY DEFAULT nextval('seq_concept_alias_id'),
    concept_id  BIGINT   NOT NULL REFERENCES concept(concept_id),
    alias       VARCHAR  NOT NULL,
    alias_type  VARCHAR,
    UNIQUE (concept_id, alias)
);

CREATE INDEX idx_concept_alias_alias ON concept_alias(alias);

CREATE TABLE concept_resolution_queue (
    queue_id                BIGINT     PRIMARY KEY DEFAULT nextval('seq_concept_resolution_queue_id'),
    candidate_name          VARCHAR    NOT NULL,
    candidate_context       TEXT,
    source_type             VARCHAR,
    source_id               BIGINT,
    nearest_concept_id      BIGINT     REFERENCES concept(concept_id),
    provisional_concept_id  BIGINT,    -- the pending_review=TRUE concept the
                                       -- resolver provisionally created; the
                                       -- review workflow updates/deletes this
                                       -- row based on the chosen action
    similarity_score        DOUBLE,
    resolution_action       VARCHAR    DEFAULT 'pending',
    reviewed_at             TIMESTAMP,
    created_at              TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_concept_resq_action ON concept_resolution_queue(resolution_action);

CREATE TABLE concept_relation (
    from_concept_id BIGINT    NOT NULL REFERENCES concept(concept_id),
    to_concept_id   BIGINT    NOT NULL REFERENCES concept(concept_id),
    relation_type   VARCHAR   NOT NULL,
    confidence      DOUBLE    DEFAULT 1.0,
    source_type     VARCHAR   NOT NULL,
    source_id       BIGINT    NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (from_concept_id, to_concept_id, relation_type, source_type, source_id)
);

CREATE INDEX idx_concept_relation_from ON concept_relation(from_concept_id);
CREATE INDEX idx_concept_relation_to   ON concept_relation(to_concept_id);
CREATE INDEX idx_concept_relation_src  ON concept_relation(source_type, source_id);

CREATE TABLE concept_query_log (
    log_id      BIGINT     PRIMARY KEY DEFAULT nextval('seq_concept_query_log_id'),
    concept_id  BIGINT     NOT NULL REFERENCES concept(concept_id),
    queried_at  TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    mode        VARCHAR
);

CREATE INDEX idx_concept_query_log_concept ON concept_query_log(concept_id, queried_at);


-- ============================================================================
-- LIVE DOC SOURCES (Context7 / DeepWiki / GitHub raw)
-- ============================================================================

CREATE TABLE doc_source (
    doc_source_id           BIGINT     PRIMARY KEY DEFAULT nextval('seq_doc_source_id'),
    name                    VARCHAR    NOT NULL,
    source_type             VARCHAR    NOT NULL,
    mcp_server              VARCHAR    NOT NULL,
    identifier              VARCHAR    NOT NULL,
    authority_score         DOUBLE,
    refresh_ttl_days        INTEGER,
    priority_tier           VARCHAR    DEFAULT 'cool',
    pinned                  BOOLEAN    DEFAULT FALSE,
    last_refresh_at         TIMESTAMP,
    last_content_changed_at TIMESTAMP,
    created_at              TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_type, identifier)
);

CREATE INDEX idx_doc_source_tier ON doc_source(priority_tier);

CREATE TABLE doc_snapshot (
    snapshot_id    BIGINT      PRIMARY KEY DEFAULT nextval('seq_doc_snapshot_id'),
    doc_source_id  BIGINT      NOT NULL REFERENCES doc_source(doc_source_id),
    source_type    VARCHAR,
    url            VARCHAR,
    retrieved_at   TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    content_hash   VARCHAR,
    content        TEXT
);

CREATE INDEX idx_doc_snapshot_source ON doc_snapshot(doc_source_id);
CREATE INDEX idx_doc_snapshot_hash   ON doc_snapshot(content_hash);

CREATE TABLE doc_snapshot_embedding (
    snapshot_id BIGINT     PRIMARY KEY REFERENCES doc_snapshot(snapshot_id),
    embedding   FLOAT[384] NOT NULL,
    model       VARCHAR    NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    created_at  TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE doc_section (
    doc_section_id BIGINT      PRIMARY KEY DEFAULT nextval('seq_doc_section_id'),
    snapshot_id    BIGINT      NOT NULL REFERENCES doc_snapshot(snapshot_id),
    parent_id      BIGINT,  -- logical self-ref; FK omitted (same DuckDB 1.5
                            -- limitation as chapter.parent_chapter_id).
    heading_level  INTEGER,
    heading_text   VARCHAR,
    ordinal        INTEGER,
    content        TEXT
);

CREATE INDEX idx_doc_section_snapshot ON doc_section(snapshot_id);
CREATE INDEX idx_doc_section_parent   ON doc_section(parent_id);

CREATE TABLE doc_section_embedding (
    doc_section_id BIGINT     PRIMARY KEY REFERENCES doc_section(doc_section_id),
    embedding      FLOAT[384] NOT NULL,
    model          VARCHAR    NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    created_at     TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE concept_doc_link (
    concept_id    BIGINT     NOT NULL REFERENCES concept(concept_id),
    doc_source_id BIGINT     NOT NULL REFERENCES doc_source(doc_source_id),
    notes         TEXT,
    created_at    TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (concept_id, doc_source_id)
);


-- ============================================================================
-- PROCEDURES (extracted from book chapters and doc sections)
-- ============================================================================

CREATE TABLE procedure (
    procedure_id       BIGINT     PRIMARY KEY DEFAULT nextval('seq_procedure_id'),
    name               VARCHAR,
    preconditions      TEXT,
    steps              TEXT,
    postconditions     TEXT,
    failure_modes      TEXT,
    source_type        VARCHAR,
    source_id          BIGINT,
    implements_pattern BIGINT,
    created_at         TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_procedure_source ON procedure(source_type, source_id);


-- ============================================================================
-- SKILLS FACTORY OUTPUT
-- ============================================================================

CREATE TABLE skill_package (
    package_id    BIGINT     PRIMARY KEY DEFAULT nextval('seq_skill_package_id'),
    name          VARCHAR    NOT NULL,
    domain        VARCHAR,
    root_topic    VARCHAR,
    source_query  TEXT,
    created_at    TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (name)
);

CREATE TABLE skill (
    skill_id         BIGINT     PRIMARY KEY DEFAULT nextval('seq_skill_id'),
    package_id       BIGINT     REFERENCES skill_package(package_id),
    name             VARCHAR    NOT NULL,
    description      TEXT,
    scope_summary    TEXT,
    content_markdown TEXT,
    source_currency  VARCHAR,
    strategy         VARCHAR,
    generation_notes TEXT,
    created_at       TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_skill_package ON skill(package_id);

CREATE TABLE skill_source (
    skill_id     BIGINT   NOT NULL REFERENCES skill(skill_id),
    source_type  VARCHAR  NOT NULL,
    source_id    BIGINT   NOT NULL,
    score        DOUBLE,
    weight       DOUBLE   DEFAULT 0,
    drop_reason  VARCHAR,
    PRIMARY KEY (skill_id, source_type, source_id)
);

CREATE TABLE skill_file (
    file_id   BIGINT     PRIMARY KEY DEFAULT nextval('seq_skill_file_id'),
    skill_id  BIGINT     NOT NULL REFERENCES skill(skill_id),
    filename  VARCHAR    NOT NULL,
    purpose   VARCHAR,
    content   TEXT
);

CREATE INDEX idx_skill_file_skill ON skill_file(skill_id);

CREATE TABLE skill_relation (
    from_skill_id BIGINT   NOT NULL REFERENCES skill(skill_id),
    to_skill_id   BIGINT   NOT NULL REFERENCES skill(skill_id),
    relation_type VARCHAR  NOT NULL,
    PRIMARY KEY (from_skill_id, to_skill_id, relation_type)
);


-- ============================================================================
-- AUTO-DISCOVERY (§5.4 — Phase 4.5b landing zone)
-- ============================================================================

-- Records every probe attempt against Context7 / DeepWiki / GitHub when a
-- query term fails to resolve to an existing concept. Used to tune the
-- confidence gate and to audit which auto-ingestions have happened.
CREATE TABLE discovery_log (
    log_id          BIGINT     PRIMARY KEY DEFAULT nextval('seq_discovery_log_id'),
    query_term      VARCHAR    NOT NULL,
    probe_source    VARCHAR,                       -- 'context7' | 'deepwiki' | 'github'
    probe_result    VARCHAR,                       -- 'match' | 'ambiguous' | 'not_found'
    match_count     INTEGER,
    top_match_name  VARCHAR,
    top_match_score DOUBLE,
    action_taken    VARCHAR,                       -- 'ingested' | 'asked_user' | 'discarded'
    doc_source_id   BIGINT     REFERENCES doc_source(doc_source_id),
    created_at      TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_discovery_log_term    ON discovery_log(query_term);
CREATE INDEX idx_discovery_log_created ON discovery_log(created_at);


-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
