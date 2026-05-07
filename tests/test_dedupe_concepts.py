"""Tests for scripts/dedupe_concepts.py — orphan-duplicate cleanup.

Covers the safety filter that protects loaded concepts from accidental
deletion. The script's premise is "delete duplicates that have zero
references everywhere"; these tests pin the boundary cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
SCRIPTS = PROJECT_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dedupe_concepts as dc  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type, description=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES (?, ?, ?) RETURNING concept_id",
        [name, concept_type, description],
    ).fetchone()[0]


_relation_counter = 0


def _add_relation(conn, src_id, tgt_id):
    """Self-loop counts as 1 incoming + 1 outgoing for the source.

    PK is (from, to, relation_type, source_type, source_id), so vary
    source_id per call to avoid constraint violations when the test
    needs to add multiple edges between the same endpoints.
    """
    global _relation_counter
    _relation_counter += 1
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, 'CITES', 'chapter', ?, 0.9)",
        [src_id, tgt_id, _relation_counter],
    )


# ---------------------------------------------------------------------------
# Safety filter: orphan vs loaded
# ---------------------------------------------------------------------------


def test_finds_orphan_duplicate_in_asymmetric_group(catalog):
    """Two concepts share a name; one has edges, the other has none.
    The empty one is an orphan; the rich one is the canonical."""
    rich = _seed_concept(catalog, "Event Sourcing", "Pattern", "rich body")
    orphan = _seed_concept(catalog, "Event Sourcing", "Concept", "orphan body")
    _add_relation(catalog, rich, rich)
    _add_relation(catalog, rich, rich)

    found = dc.find_orphan_duplicates(catalog)
    assert len(found) == 1
    assert found[0]["concept_id"] == orphan
    assert found[0]["canonical_id"] == rich
    assert found[0]["rel_count"] == 0


def test_skips_dup_with_alias(catalog):
    """An empty duplicate that has an alias attached is NOT an orphan —
    deleting it would silently break the alias contract."""
    rich = _seed_concept(catalog, "Bulkhead", "Pattern", "rich")
    other = _seed_concept(catalog, "Bulkhead", "Concept", "other")
    _add_relation(catalog, rich, rich)
    catalog.execute(
        "INSERT INTO concept_alias (concept_id, alias) VALUES (?, 'BH')",
        [other],
    )

    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_skips_dup_referenced_by_alignment_edge(catalog):
    """alignment_edge.concept_id is an FK; deleting the row would
    orphan the edge. Filter skips."""
    rich = _seed_concept(catalog, "Circuit Breaker", "Pattern", "rich")
    other = _seed_concept(catalog, "Circuit Breaker", "Concept", "other")
    _add_relation(catalog, rich, rich)

    # Need a doc_section + chapter for the alignment edge to point at.
    bid = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('B', '/tmp/b.epub') "
        "RETURNING book_id"
    ).fetchone()[0]
    cid = catalog.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, 'C', 1) RETURNING chapter_id", [bid],
    ).fetchone()[0]
    dsid = catalog.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
        "VALUES ('s', 'context7', 'context7', '/x') RETURNING doc_source_id"
    ).fetchone()[0]
    snid = catalog.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, content_hash, content) "
        "VALUES (?, 'context7', 'h', '...') RETURNING snapshot_id", [dsid],
    ).fetchone()[0]
    secid = catalog.execute(
        "INSERT INTO doc_section (snapshot_id, ordinal, content) "
        "VALUES (?, 0, '...') RETURNING doc_section_id", [snid],
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO alignment_edge "
        "(from_doc_section_id, to_chapter_id, concept_id, relation_type) "
        "VALUES (?, ?, ?, 'CORROBORATES')",
        [secid, cid, other],
    )

    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_skips_dup_referenced_by_procedure(catalog):
    rich = _seed_concept(catalog, "Quorum", "Pattern", "rich")
    other = _seed_concept(catalog, "Quorum", "Concept", "other")
    _add_relation(catalog, rich, rich)

    bid = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('B', '/tmp/b.epub') "
        "RETURNING book_id"
    ).fetchone()[0]
    chid = catalog.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, 'C', 1) RETURNING chapter_id", [bid],
    ).fetchone()[0]
    pid = catalog.execute(
        "INSERT INTO procedure (source_type, source_id, name, steps) "
        "VALUES ('chapter', ?, 'P', '...') RETURNING procedure_id", [chid],
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
        [pid, other],
    )

    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_skips_dup_with_query_log(catalog):
    rich = _seed_concept(catalog, "Saga", "Pattern", "rich")
    other = _seed_concept(catalog, "Saga", "Concept", "other")
    _add_relation(catalog, rich, rich)
    catalog.execute(
        "INSERT INTO concept_query_log (concept_id) VALUES (?)",
        [other],
    )

    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_skips_dup_referenced_by_resolution_queue(catalog):
    """concept_resolution_queue.nearest_concept_id is an FK; an empty
    duplicate that's the nearest match for some pending-review queue
    item must NOT be deleted — that would orphan the queue row."""
    rich = _seed_concept(catalog, "Quorum Read", "Pattern", "rich")
    other = _seed_concept(catalog, "Quorum Read", "Concept", "other")
    _add_relation(catalog, rich, rich)
    catalog.execute(
        "INSERT INTO concept_resolution_queue "
        "(candidate_name, nearest_concept_id, similarity_score) "
        "VALUES ('Quorum Reading', ?, 0.85)",
        [other],
    )
    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_skips_dup_with_doc_link(catalog):
    rich = _seed_concept(catalog, "FHIR", "Pattern", "rich")
    other = _seed_concept(catalog, "FHIR", "Concept", "other")
    _add_relation(catalog, rich, rich)

    dsid = catalog.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
        "VALUES ('h', 'context7', 'context7', '/y') RETURNING doc_source_id"
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO concept_doc_link (concept_id, doc_source_id) VALUES (?, ?)",
        [other, dsid],
    )

    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_no_duplicates_yields_empty(catalog):
    _seed_concept(catalog, "Lonely", "Pattern", "single concept")
    found = dc.find_orphan_duplicates(catalog)
    assert found == []


def test_canonical_picked_by_relation_count(catalog):
    """Three concepts share a name. The richest (most edges) is the
    canonical; the orphan(s) point at it."""
    sparse = _seed_concept(catalog, "Three Way", "Concept", "")
    medium = _seed_concept(catalog, "Three Way", "Pattern", "")
    rich = _seed_concept(catalog, "Three Way", "Tool", "")
    # rich gets 5 edges, medium gets 2, sparse gets 0
    for _ in range(5):
        _add_relation(catalog, rich, rich)
    for _ in range(2):
        _add_relation(catalog, medium, medium)

    found = dc.find_orphan_duplicates(catalog)
    # Only sparse is an orphan (medium has 2 edges, so it's loaded).
    assert len(found) == 1
    assert found[0]["concept_id"] == sparse
    assert found[0]["canonical_id"] == rich  # richest, not medium


def test_canonical_tiebreak_by_lowest_id(catalog):
    """When two duplicates have equal edge counts, lowest concept_id
    wins canonical so the result is deterministic."""
    a = _seed_concept(catalog, "Equal", "Pattern", "")
    b = _seed_concept(catalog, "Equal", "Concept", "")
    c = _seed_concept(catalog, "Equal", "Tool", "")
    # Both a and b get 1 edge; c gets 0.
    _add_relation(catalog, a, a)
    _add_relation(catalog, b, b)

    found = dc.find_orphan_duplicates(catalog)
    assert len(found) == 1
    assert found[0]["concept_id"] == c
    # a < b, both rank 1 in tie; canonical should be a.
    assert found[0]["canonical_id"] == a


def test_all_zero_group_keeps_one_canonical(catalog):
    """A group where every duplicate has 0 edges still picks one as
    canonical (lowest concept_id) and treats the rest as orphans."""
    a = _seed_concept(catalog, "All Empty", "Pattern", "a")
    b = _seed_concept(catalog, "All Empty", "Concept", "b")
    c = _seed_concept(catalog, "All Empty", "Tool", "c")

    found = dc.find_orphan_duplicates(catalog)
    # 2 orphans (b and c), canonical = a (lowest id)
    assert {o["concept_id"] for o in found} == {b, c}
    assert all(o["canonical_id"] == a for o in found)


# ---------------------------------------------------------------------------
# Deletion path
# ---------------------------------------------------------------------------


def test_delete_orphans_removes_concepts_and_embeddings(catalog):
    rich = _seed_concept(catalog, "Sample", "Pattern", "rich")
    orphan = _seed_concept(catalog, "Sample", "Concept", "orphan")
    _add_relation(catalog, rich, rich)
    # Embeddings on both
    for cid in (rich, orphan):
        catalog.execute(
            "INSERT INTO concept_embedding (concept_id, embedding) "
            "VALUES (?, ?)",
            [cid, [0.0] * 384],
        )

    found = dc.find_orphan_duplicates(catalog)
    deleted = dc.delete_orphans(catalog, found)
    assert deleted == 1

    # Orphan + its embedding gone; canonical untouched.
    assert catalog.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [orphan]
    ).fetchone()[0] == 0
    assert catalog.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?", [orphan]
    ).fetchone()[0] == 0
    assert catalog.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [rich]
    ).fetchone()[0] == 1


def test_delete_orphans_empty_input_is_noop(catalog):
    assert dc.delete_orphans(catalog, []) == 0


def test_idempotent_after_first_run(catalog):
    """Re-running find_orphan_duplicates after deletion returns nothing."""
    rich = _seed_concept(catalog, "Re-run", "Pattern", "")
    orphan = _seed_concept(catalog, "Re-run", "Concept", "")
    _add_relation(catalog, rich, rich)
    for cid in (rich, orphan):
        catalog.execute(
            "INSERT INTO concept_embedding (concept_id, embedding) "
            "VALUES (?, ?)", [cid, [0.0] * 384],
        )

    first = dc.find_orphan_duplicates(catalog)
    assert len(first) == 1
    dc.delete_orphans(catalog, first)
    second = dc.find_orphan_duplicates(catalog)
    assert second == []
