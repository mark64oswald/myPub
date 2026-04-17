-- ============================================================================
-- myPub Property Graph (DuckPGQ)
-- Per arch doc §7.2, with Phase 1 scope: only the vertex/edge tables backed
-- by populated base tables are included. Concept-, doc-, skill-, and
-- procedure-related edges become meaningful in Phase 2+ and should be added
-- then (stubs noted in comments).
--
-- DuckPGQ conventions learned the hard way:
--   * Labels must be lowercase; Book/Chapter etc. collide with reserved
--     tokens in the parser.
--   * Every edge pattern in a MATCH needs a bound variable, even when you
--     don't reference it.
-- ============================================================================

DROP PROPERTY GRAPH IF EXISTS mypub;

CREATE PROPERTY GRAPH mypub

VERTEX TABLES (
    author        LABEL author,
    book          LABEL book,
    chapter       LABEL chapter,
    concept       LABEL concept,           -- 0 rows until Phase 2
    procedure     LABEL procedure,         -- 0 rows until Phase 2
    doc_snapshot  LABEL doc_snapshot,      -- 0 rows until Phase 3
    doc_section   LABEL doc_section,       -- 0 rows until Phase 3
    skill         LABEL skill,             -- 0 rows until Phase 5
    skill_package LABEL skill_package      -- 0 rows until Phase 5
)

EDGE TABLES (
    -- Author WROTE Book: author ← book_author → book
    book_author
        SOURCE KEY (author_id) REFERENCES author (author_id)
        DESTINATION KEY (book_id) REFERENCES book (book_id)
        LABEL wrote,

    -- Book CONTAINS Chapter (chapter.book_id acts as the edge).
    -- DuckPGQ needs every LABEL unique across the graph, so each
    -- "contains" variant gets a subject-prefixed name.
    chapter
        SOURCE KEY (book_id) REFERENCES book (book_id)
        DESTINATION KEY (chapter_id) REFERENCES chapter (chapter_id)
        LABEL book_contains,

    -- DocSnapshot CONTAINS DocSection.
    doc_section
        SOURCE KEY (snapshot_id) REFERENCES doc_snapshot (snapshot_id)
        DESTINATION KEY (doc_section_id) REFERENCES doc_section (doc_section_id)
        LABEL snapshot_contains,

    -- SkillPackage CONTAINS Skill.
    skill
        SOURCE KEY (package_id) REFERENCES skill_package (package_id)
        DESTINATION KEY (skill_id) REFERENCES skill (skill_id)
        LABEL package_contains,

    -- Concept RELATES_TO Concept (REQUIRES / EXTENDS / CONTRASTS_WITH / …).
    -- DuckPGQ requires edge-table LABELs to be globally unique, so we
    -- distinguish from skill_relates_to below.
    concept_relation
        SOURCE KEY (from_concept_id) REFERENCES concept (concept_id)
        DESTINATION KEY (to_concept_id) REFERENCES concept (concept_id)
        LABEL concept_relates_to,

    -- Skill RELATES_TO Skill (REQUIRES / REFERENCES / EXTENDS).
    skill_relation
        SOURCE KEY (from_skill_id) REFERENCES skill (skill_id)
        DESTINATION KEY (to_skill_id) REFERENCES skill (skill_id)
        LABEL skill_relates_to

    -- Phase 2+ stubs (add when the backing tables are populated):
    --   chapter_concept   Chapter   → Concept    LABEL discusses
    --   doc_section_concept DocSection → Concept LABEL discusses
    --   chapter_procedure Chapter   → Procedure  LABEL explains
    --   doc_cross_ref     DocSection → {Chapter, DocSection}
    --                                           LABEL corroborates|contradicts
    --   skill_source      Skill     → {Chapter, Procedure, DocSection}
    --                                           LABEL derived_from
);

-- ============================================================================
-- END OF GRAPH DEFINITION
-- ============================================================================
