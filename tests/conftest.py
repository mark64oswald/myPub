"""
Shared pytest fixtures, including a "substrate-realistic" catalog
that mirrors production state — v2 schema + VSS/FTS extensions
loaded + HNSW index on concept_embedding. Tests that target code
paths which touch indexes, extensions, or DDL-level behavior should
use `realistic_conn`; unit tests that only need schema can keep
using a plain `:memory:` connection.

Having a fixture that matches production caught today's bug
(resolve_concept failing because VSS wasn't loaded when the live
HNSW-indexed concept_embedding needed to be modified).
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"

# Make project modules importable from tests.
for _p in (SCRIPTS_DIR, MCP_DIR):
    _str = str(_p)
    if _str not in sys.path:
        sys.path.insert(0, _str)


@pytest.fixture(scope="module")
def embedder():
    """Load sentence-transformers/all-MiniLM-L6-v2 once per module.

    Session-scope would be faster, but module-scope gives callers
    flexibility to parametrize per-module if needed.
    """
    # pylint: disable=import-outside-toplevel
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


@pytest.fixture
def schema_only_conn():
    """Fresh in-memory DuckDB with the v2 schema but no extensions.

    Use this for pure unit tests — schema checks, resolver logic that
    doesn't touch indexed tables, etc. If your code path calls LOAD or
    reads/writes an HNSW-indexed table, prefer `realistic_conn` instead.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


@pytest.fixture
def realistic_conn():
    """In-memory DuckDB matching the production substrate state.

      * v2 schema applied
      * vss and fts extensions loaded
      * HNSW index on concept_embedding(embedding) with cosine metric
      * HNSW index on chapter_embedding(embedding) with cosine metric
      * (No FTS PRAGMA index built — that requires chapters with content
        and is slow; tests that need FTS should set it up themselves.)

    Any test that goes through a code path which DELETEs or UPDATEs
    concept_embedding / chapter_embedding belongs here — the HNSW
    binding makes those DML statements fail unless VSS is loaded, which
    is exactly the production failure mode.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_FILE.read_text())
    conn.execute("LOAD vss")
    conn.execute("SET hnsw_enable_experimental_persistence = true")
    try:
        conn.execute("LOAD fts")
    except duckdb.Error:
        # FTS may not be available in some DuckDB builds; VSS is the
        # critical one for the HNSW-blocking-DML bug.
        pass
    # Seed two embeddings so the HNSW indexes have vectors to index.
    # (DuckDB's HNSW can be created on an empty table, but some versions
    # refuse; seeding a couple of rows is harmless.)
    conn.execute(
        "INSERT INTO concept (name, concept_type, description, pending_review) "
        "VALUES ('__seed_a', 'Concept', 'seed row a', FALSE) "
        "RETURNING concept_id"
    ).fetchone()
    conn.execute(
        "INSERT INTO concept (name, concept_type, description, pending_review) "
        "VALUES ('__seed_b', 'Concept', 'seed row b', FALSE) "
        "RETURNING concept_id"
    ).fetchone()
    zero_vec = [0.0] * 384
    seed_ids = [
        r[0] for r in conn.execute(
            "SELECT concept_id FROM concept WHERE name LIKE '__seed_%'"
        ).fetchall()
    ]
    for cid in seed_ids:
        conn.execute(
            "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
            [cid, zero_vec],
        )
    conn.execute(
        "CREATE INDEX concept_embedding_hnsw ON concept_embedding "
        "USING HNSW (embedding) WITH (metric = 'cosine')"
    )
    # chapter/chapter_embedding get seeded too so test code has a realistic
    # chapter row to point at as `source_id`.
    conn.execute(
        "INSERT INTO author (name) VALUES ('__seed_author') RETURNING author_id"
    )
    conn.execute(
        "INSERT INTO book (title, source_path, content_hash, last_indexed_at, status) "
        "VALUES ('__seed_book', '/tmp/seed.epub', 'deadbeef', "
        "        CURRENT_TIMESTAMP, 'active')"
    )
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content, content_hash) "
        "SELECT book_id, 1, '__seed_chapter', 'seed content', 'cafe' "
        "FROM book WHERE title = '__seed_book'"
    )
    seed_chapter_id = conn.execute(
        "SELECT chapter_id FROM chapter WHERE title = '__seed_chapter'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chapter_embedding (chapter_id, embedding) VALUES (?, ?)",
        [seed_chapter_id, zero_vec],
    )
    conn.execute(
        "CREATE INDEX chapter_embedding_hnsw ON chapter_embedding "
        "USING HNSW (embedding) WITH (metric = 'cosine')"
    )
    yield conn
    conn.close()


@pytest.fixture
def seed_ids(realistic_conn):
    """Convenience accessor for the IDs of rows the realistic fixture seeds."""
    row = realistic_conn.execute(
        "SELECT (SELECT book_id FROM book WHERE title = '__seed_book'),"
        "       (SELECT chapter_id FROM chapter WHERE title = '__seed_chapter')"
    ).fetchone()
    return {"book_id": row[0], "chapter_id": row[1]}
