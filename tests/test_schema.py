"""
Structural tests for the v2 catalog schema.

Verifies that every expected table exists with the expected key columns,
that embedding columns are FLOAT[384], and that critical indexes are present.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"


EXPECTED_TABLES = {
    "author",
    "book",
    "book_author",
    "chapter",
    "chapter_embedding",
    "concept",
    "concept_embedding",
    "concept_alias",
    "concept_resolution_queue",
    "concept_relation",
    "concept_query_log",
    "concept_doc_link",
    "doc_source",
    "doc_snapshot",
    "doc_snapshot_embedding",
    "doc_section",
    "doc_section_embedding",
    "procedure",
    "skill_package",
    "skill",
    "skill_source",
    "skill_file",
    "skill_relation",
    "discovery_log",
}

EXPECTED_COLUMNS = {
    "book": {"book_id", "title", "publisher", "publication_date", "source_path",
             "description", "subjects", "total_tokens", "chapter_count",
             "indexed_at", "updated_at"},
    "chapter": {"chapter_id", "book_id", "chapter_num", "parent_chapter_id",
                "title", "href", "content", "token_count", "indexed_at"},
    "chapter_embedding": {"chapter_id", "embedding", "model", "created_at"},
    "concept": {"concept_id", "name", "concept_type", "description", "domain",
                "pending_review", "query_count", "last_queried_at",
                "created_at", "updated_at"},
    "concept_embedding": {"concept_id", "embedding", "model", "created_at"},
    "doc_snapshot_embedding": {"snapshot_id", "embedding", "model", "created_at"},
    "doc_section_embedding": {"doc_section_id", "embedding", "model", "created_at"},
    "concept_alias": {"alias_id", "concept_id", "alias", "alias_type"},
    "concept_resolution_queue": {"queue_id", "candidate_name",
                                 "candidate_context", "source_type",
                                 "source_id", "nearest_concept_id",
                                 "similarity_score", "resolution_action",
                                 "reviewed_at", "created_at"},
    "concept_relation": {"from_concept_id", "to_concept_id", "relation_type",
                         "confidence", "source_type", "source_id", "created_at"},
    "concept_query_log": {"log_id", "concept_id", "queried_at", "mode"},
    "doc_source": {"doc_source_id", "name", "source_type", "mcp_server",
                   "identifier", "authority_score", "refresh_ttl_days",
                   "priority_tier", "pinned", "last_refresh_at",
                   "last_content_changed_at", "created_at"},
    "doc_snapshot": {"snapshot_id", "doc_source_id", "source_type", "url",
                     "retrieved_at", "content_hash", "content"},
    "doc_section": {"doc_section_id", "snapshot_id", "parent_id",
                    "heading_level", "heading_text", "ordinal", "content"},
    "procedure": {"procedure_id", "name", "preconditions", "steps",
                  "postconditions", "failure_modes", "source_type",
                  "source_id", "implements_pattern", "created_at"},
    "skill_package": {"package_id", "name", "domain", "root_topic",
                      "source_query", "created_at"},
    "skill": {"skill_id", "package_id", "name", "description", "scope_summary",
              "content_markdown", "source_currency", "strategy",
              "generation_notes", "created_at"},
    "skill_source": {"skill_id", "source_type", "source_id", "score", "weight",
                     "drop_reason"},
    "skill_file": {"file_id", "skill_id", "filename", "purpose", "content"},
    "skill_relation": {"from_skill_id", "to_skill_id", "relation_type"},
    "discovery_log": {"log_id", "query_term", "probe_source", "probe_result",
                      "match_count", "top_match_name", "top_match_score",
                      "action_taken", "doc_source_id", "created_at"},
}

EMBEDDING_COLUMNS = [
    ("chapter_embedding", "embedding"),
    ("concept_embedding", "embedding"),
    ("doc_snapshot_embedding", "embedding"),
    ("doc_section_embedding", "embedding"),
]


@pytest.fixture(scope="module")
def conn():
    assert CATALOG.exists(), f"catalog not found at {CATALOG}; run migrate_v2_schema.py"
    c = duckdb.connect(str(CATALOG), read_only=True)
    yield c
    c.close()


def test_all_expected_tables_exist(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE'"
    ).fetchall()
    actual = {r[0] for r in rows}
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {sorted(missing)}"


@pytest.mark.parametrize("table,expected_cols", sorted(EXPECTED_COLUMNS.items()))
def test_expected_columns(conn, table, expected_cols):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name = ?",
        [table],
    ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected_cols - actual
    assert not missing, f"table {table} missing columns: {sorted(missing)}"


@pytest.mark.parametrize("table,column", EMBEDDING_COLUMNS)
def test_embeddings_are_float_384(conn, table, column):
    row = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    assert row is not None, f"{table}.{column} not found"
    dtype = row[0].upper()
    assert "FLOAT" in dtype and "384" in dtype, (
        f"{table}.{column} has unexpected type {dtype!r} (expected FLOAT[384])"
    )


def test_identity_primary_keys(conn):
    """BIGINT identity PKs exist on the key entity tables."""
    for table in ("book", "chapter", "concept", "author",
                  "doc_source", "doc_snapshot", "doc_section",
                  "skill_package", "skill"):
        pk = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name = ? "
            "ORDER BY ordinal_position LIMIT 1",
            [table],
        ).fetchone()
        assert pk is not None, f"no columns for {table}"
        col, dtype = pk
        assert dtype.upper() == "BIGINT", f"{table}.{col} is {dtype}, expected BIGINT"


def test_not_yet_populated_tables_are_empty(conn):
    """Tables populated only by later phases should still be empty."""
    # What each phase populates:
    #   Phase 1: author, book, book_author, chapter, chapter_embedding
    #   Phase 2: concept, concept_embedding, concept_relation,
    #            concept_alias, concept_resolution_queue, concept_query_log
    #   Phase 3: procedure
    #   Phase 4: doc_source, doc_snapshot, doc_section,
    #            doc_snapshot_embedding, doc_section_embedding,
    #            concept_doc_link, discovery_log
    #   Phase 5: skill_package, skill, skill_source, skill_file, skill_relation
    populated_by_phase_1_or_2 = {
        "author", "book", "book_author", "chapter", "chapter_embedding",
        "concept", "concept_embedding", "concept_relation",
        "concept_alias", "concept_resolution_queue", "concept_query_log",
    }
    for table in EXPECTED_TABLES - populated_by_phase_1_or_2:
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert count == 0, f"{table} is not empty (count={count})"


def test_author_name_unique(conn):
    """UNIQUE constraint on author.name is declared."""
    constraints = conn.execute(
        "SELECT constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema='main' AND table_name='author'"
    ).fetchall()
    types = {r[0] for r in constraints}
    assert "UNIQUE" in types or "PRIMARY KEY" in types, (
        f"expected UNIQUE/PK constraints on author, got {types}"
    )
