"""
Tests for mcp-servers/kb-mcp/resolution.py — the EntityResolver three-stage
resolver (exact → alias → embedding → borderline → new).

Uses an in-memory DuckDB with the v2 schema so each test starts from a
clean state. The sentence-transformers model is loaded once per module
(slow — ~2 s cold) and shared across tests via a fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
sys.path.insert(0, str(MCP_DIR))

from resolution import (  # noqa: E402  # pylint: disable=wrong-import-position
    EntityResolver, ResolveResult,
)


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def embedder():
    """Load sentence-transformers/all-MiniLM-L6-v2 once per module."""
    # pylint: disable=import-outside-toplevel
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


@pytest.fixture
def conn():
    """Fresh in-memory DuckDB with the v2 schema, rolled back per test."""
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA_FILE.read_text())
    yield c
    c.close()


@pytest.fixture
def resolver(conn, embedder):
    """Resolver bound to the per-test connection, with the shared model."""
    return EntityResolver(conn, model=embedder)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _seed_concept(
    conn: duckdb.DuckDBPyConnection,
    embedder,
    name: str,
    description: str,
    concept_type: str | None = None,
) -> int:
    """Insert a concept and its embedding. Returns concept_id."""
    row = conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES (?, ?, ?) RETURNING concept_id",
        [name, concept_type, description],
    ).fetchone()
    concept_id = row[0]
    text = f"{name}\n\n{description}" if description else name
    vec = embedder.encode([text], convert_to_numpy=True)[0].astype("float32").tolist()
    conn.execute(
        "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
        [concept_id, vec],
    )
    return concept_id


def _seed_alias(conn, concept_id: int, alias: str, alias_type: str = "abbreviation") -> None:
    conn.execute(
        "INSERT INTO concept_alias (concept_id, alias, alias_type) VALUES (?, ?, ?)",
        [concept_id, alias, alias_type],
    )


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

def test_exact_match_case_insensitive(conn, embedder, resolver):
    cid = _seed_concept(conn, embedder, "Change Data Capture",
                        "row-level change streaming from an OLTP database")
    result = resolver.resolve("change data capture")
    assert result == ResolveResult(
        concept_id=cid, is_new=False, resolution_type="exact",
        similarity=None, nearest_concept_id=None,
    )


def test_alias_match(conn, embedder, resolver):
    cid = _seed_concept(conn, embedder, "Change Data Capture",
                        "row-level change streaming from an OLTP database")
    _seed_alias(conn, cid, "CDC")
    result = resolver.resolve("CDC")
    assert result.concept_id == cid
    assert result.is_new is False
    assert result.resolution_type == "alias"


def test_alias_case_insensitive(conn, embedder, resolver):
    cid = _seed_concept(conn, embedder, "Extract Transform Load",
                        "classic batch data integration pattern")
    _seed_alias(conn, cid, "ETL")
    result = resolver.resolve("etl")
    assert result.concept_id == cid
    assert result.resolution_type == "alias"


def test_embedding_high_similarity_matches_existing(conn, embedder):
    """A paraphrase that's semantically the same concept should auto-match.

    Uses a lowered high_threshold (0.80) because MiniLM-L6-v2 puts close
    paraphrases around 0.83 rather than the arch doc's default 0.90. The
    0.90 default is a Phase-2.6-tuning question; this test is about the
    *embedding_high path*, not the specific threshold value.
    """
    resolver = EntityResolver(conn, model=embedder, high_threshold=0.80)
    cid = _seed_concept(
        conn, embedder, "Star Schema",
        "A dimensional modeling pattern with a central fact table and "
        "surrounding dimension tables used in data warehousing.",
    )
    result = resolver.resolve(
        "star-schema model",
        candidate_context=(
            "The star schema organizes a warehouse around a central fact "
            "table joined to denormalized dimensions."
        ),
    )
    assert result.concept_id == cid, "paraphrase should resolve to same concept"
    assert result.is_new is False
    assert result.resolution_type == "embedding_high"
    assert result.similarity >= resolver.high_threshold


def test_new_concept_when_corpus_empty(conn, resolver):
    """With no existing concepts, every candidate is a fresh new concept."""
    result = resolver.resolve(
        "Quantum Entanglement",
        candidate_context="A physics phenomenon unrelated to our data KB.",
    )
    assert result.is_new is True
    assert result.resolution_type == "new"
    assert result.concept_id is not None
    assert result.similarity is None
    assert result.nearest_concept_id is None

    # The new concept exists and has an embedding.
    row = conn.execute(
        "SELECT name, pending_review FROM concept WHERE concept_id = ?",
        [result.concept_id],
    ).fetchone()
    assert row == ("Quantum Entanglement", False)
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?",
        [result.concept_id],
    ).fetchone()[0] == 1


def test_new_concept_when_unrelated_to_existing(conn, embedder, resolver):
    """A candidate far from every existing concept should still land as new."""
    _seed_concept(conn, embedder, "Star Schema",
                  "dimensional modeling for warehouses")
    _seed_concept(conn, embedder, "Change Data Capture",
                  "streaming row-level changes from OLTP")

    result = resolver.resolve(
        "Amphibian Biology",
        candidate_context="A completely unrelated topic about frogs.",
    )
    assert result.is_new is True
    assert result.resolution_type == "new"
    # Nearest should still be populated as audit info.
    assert result.nearest_concept_id is not None
    assert result.similarity is not None
    assert result.similarity < resolver.low_threshold


def test_borderline_creates_pending_concept_and_enqueues(conn, embedder):
    """A related-but-distinct candidate should land in the review queue."""
    # Use tighter thresholds so the borderline band is easier to hit
    # without engineering exotic embeddings.
    resolver = EntityResolver(
        conn, model=embedder, high_threshold=0.98, low_threshold=0.50,
    )
    parent = _seed_concept(
        conn, embedder, "Star Schema",
        "central fact table plus denormalized dimensions",
    )

    result = resolver.resolve(
        "Snowflake Schema",
        candidate_context=(
            "dimensional model with normalized dimension tables — related to "
            "but distinct from star schema"
        ),
    )
    assert result.resolution_type == "borderline", (
        f"expected borderline, got {result.resolution_type} "
        f"(sim={result.similarity})"
    )
    assert result.is_new is True
    assert result.nearest_concept_id == parent

    # Provisional concept is flagged pending_review.
    row = conn.execute(
        "SELECT pending_review FROM concept WHERE concept_id = ?",
        [result.concept_id],
    ).fetchone()
    assert row[0] is True

    # Queue has the entry.
    queued = conn.execute(
        "SELECT candidate_name, nearest_concept_id, resolution_action "
        "FROM concept_resolution_queue"
    ).fetchall()
    assert len(queued) == 1
    assert queued[0] == ("Snowflake Schema", parent, "pending")


def test_source_provenance_on_borderline(conn, embedder):
    """Borderline candidates record their source on concept_resolution_queue.

    Uses the same wide threshold band and long context as the
    borderline-enqueue test so the candidate reliably lands in the band;
    this test is about provenance propagation, not band calibration.
    """
    resolver = EntityResolver(
        conn, model=embedder, high_threshold=0.98, low_threshold=0.50,
    )
    _seed_concept(conn, embedder, "Star Schema",
                  "central fact table plus denormalized dimensions")

    resolver.resolve(
        "Snowflake Schema",
        candidate_context=(
            "dimensional model with normalized dimension tables — related "
            "to but distinct from star schema"
        ),
        source_type="chapter",
        source_id=42,
    )
    row = conn.execute(
        "SELECT source_type, source_id FROM concept_resolution_queue"
    ).fetchone()
    assert row == ("chapter", 42)


def test_concept_type_scopes_exact_match(conn, embedder, resolver):
    """Same name under different types should not exact-match across types."""
    pattern_id = _seed_concept(conn, embedder, "Transaction", "DB transaction",
                               concept_type="Pattern")
    protocol_id = _seed_concept(conn, embedder, "Transaction",
                                "blockchain transaction record",
                                concept_type="Protocol")
    # Unscoped lookup picks one (we don't care which; either is a concept
    # with that name). Scoped lookup must pick the right one.
    r_pattern = resolver.resolve("Transaction", concept_type="Pattern")
    assert r_pattern.concept_id == pattern_id
    r_protocol = resolver.resolve("Transaction", concept_type="Protocol")
    assert r_protocol.concept_id == protocol_id


def test_register_alias_is_idempotent(conn, embedder, resolver):
    cid = _seed_concept(conn, embedder, "Change Data Capture",
                        "row-level change streaming")
    first = resolver.register_alias(cid, "CDC", alias_type="abbreviation")
    second = resolver.register_alias(cid, "CDC", alias_type="abbreviation")
    assert first is not None
    assert second is None  # silent no-op on duplicate
    rows = conn.execute(
        "SELECT COUNT(*) FROM concept_alias WHERE concept_id = ?", [cid]
    ).fetchone()[0]
    assert rows == 1


def test_bad_thresholds_rejected(conn, embedder):
    with pytest.raises(ValueError):
        EntityResolver(conn, model=embedder,
                       high_threshold=0.50, low_threshold=0.80)
    with pytest.raises(ValueError):
        EntityResolver(conn, model=embedder,
                       high_threshold=1.5, low_threshold=0.5)


def test_empty_candidate_name_rejected(resolver):
    with pytest.raises(ValueError):
        resolver.resolve("")
    with pytest.raises(ValueError):
        resolver.resolve("   ")
