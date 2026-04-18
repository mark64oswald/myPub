"""
Tests for scripts/resolve_concept.py — the review-queue action handlers.

Each test builds a fresh in-memory DuckDB with the v2 schema, seeds a
target concept, seeds a provisional (pending_review=TRUE) concept with a
queue row, optionally wires some concept_relation edges against the
provisional, then calls one of the resolve_concept action functions and
asserts the post-state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import resolve_concept as rc  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------------

@pytest.fixture
def conn():
    """Fresh in-memory DuckDB with the v2 schema applied."""
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA_FILE.read_text())
    yield c
    c.close()


def _insert_concept(
    conn,
    name: str,
    *,
    concept_type: str | None = None,
    pending_review: bool = False,
) -> int:
    """Insert a concept and a stub embedding; return its concept_id."""
    cid = conn.execute(
        "INSERT INTO concept (name, concept_type, description, pending_review) "
        "VALUES (?, ?, ?, ?) RETURNING concept_id",
        [name, concept_type, f"desc of {name}", pending_review],
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
        [cid, [0.0] * 384],
    )
    return cid


def _enqueue(
    conn,
    candidate_name: str,
    provisional_id: int,
    nearest_id: int,
    similarity: float = 0.80,
    source_type: str = "chapter",
    source_id: int = 999,
) -> int:
    """Seed one pending queue row; return queue_id."""
    return conn.execute(
        """
        INSERT INTO concept_resolution_queue
            (candidate_name, candidate_context, source_type, source_id,
             nearest_concept_id, provisional_concept_id,
             similarity_score, resolution_action)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        RETURNING queue_id
        """,
        [candidate_name, "sample context", source_type, source_id,
         nearest_id, provisional_id, similarity],
    ).fetchone()[0]


def _add_edge(
    conn, from_id: int, to_id: int, rtype: str = "RELATED_TO",
    source_type: str = "chapter", source_id: int = 999,
) -> None:
    """Seed one concept_relation row."""
    conn.execute(
        "INSERT INTO concept_relation "
        "(from_concept_id, to_concept_id, relation_type, confidence, "
        " source_type, source_id) VALUES (?, ?, ?, 0.9, ?, ?)",
        [from_id, to_id, rtype, source_type, source_id],
    )


# ----------------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------------

def test_merge_moves_edges_and_deletes_provisional(conn):
    target = _insert_concept(conn, "Star Schema")
    provisional = _insert_concept(conn, "Star-Schema Variant", pending_review=True)
    other = _insert_concept(conn, "Kimball")
    qid = _enqueue(conn, "Star-Schema Variant", provisional, target)

    # Provisional has an outbound edge (prov → other) and an inbound (other → prov).
    _add_edge(conn, provisional, other, source_id=111)
    _add_edge(conn, other, provisional, source_id=112)

    rc.do_merge(conn, qid, register_alias=False)

    # Provisional is gone, embedding cleaned up.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [provisional]
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?", [provisional]
    ).fetchone()[0] == 0

    # Edges rewritten to target, none remain on provisional.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE from_concept_id = ? OR to_concept_id = ?",
        [provisional, provisional],
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE from_concept_id = ? OR to_concept_id = ?",
        [target, target],
    ).fetchone()[0] == 2

    # Queue row marked merged.
    action = conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0]
    assert action == "merge"

    # No alias registered.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_alias WHERE concept_id = ?", [target]
    ).fetchone()[0] == 0


def test_merge_with_register_alias(conn):
    target = _insert_concept(conn, "Change Data Capture")
    provisional = _insert_concept(conn, "CDC Stream", pending_review=True)
    qid = _enqueue(conn, "CDC Stream", provisional, target)

    rc.do_merge(conn, qid, register_alias=True)

    aliases = conn.execute(
        "SELECT alias, alias_type FROM concept_alias WHERE concept_id = ?",
        [target],
    ).fetchall()
    assert len(aliases) == 1
    assert aliases[0] == ("CDC Stream", "synonym")

    action = conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0]
    assert action == "alias"


def test_merge_drops_duplicate_edge_on_collision(conn):
    """If the same (from, to, rtype, source) edge exists on both provisional
    and target, rewriting would violate the PK. The merge should drop the
    duplicate rather than crash."""
    target = _insert_concept(conn, "Star Schema")
    provisional = _insert_concept(conn, "Star-Schema Variant", pending_review=True)
    other = _insert_concept(conn, "Fact Table")
    qid = _enqueue(conn, "Star-Schema Variant", provisional, target)

    # Both target and provisional have a REQUIRES→other edge from the same chapter.
    _add_edge(conn, target, other, rtype="REQUIRES", source_id=500)
    _add_edge(conn, provisional, other, rtype="REQUIRES", source_id=500)

    rc.do_merge(conn, qid, register_alias=False)

    # One edge remains (the target's original), duplicate was dropped.
    count = conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE from_concept_id = ? AND to_concept_id = ? "
        "AND relation_type = 'REQUIRES' AND source_id = 500",
        [target, other],
    ).fetchone()[0]
    assert count == 1


def test_merge_refuses_already_resolved(conn):
    target = _insert_concept(conn, "A")
    prov = _insert_concept(conn, "B", pending_review=True)
    qid = _enqueue(conn, "B", prov, target)
    rc.do_merge(conn, qid, register_alias=False)

    # Provisional is gone; action is recorded. Trying to merge again should
    # fail with a clear error.
    with pytest.raises(ValueError, match="already resolved"):
        rc.do_merge(conn, qid, register_alias=False)


# ----------------------------------------------------------------------------
# keep-separate
# ----------------------------------------------------------------------------

def test_keep_separate_clears_pending_and_keeps_everything(conn):
    target = _insert_concept(conn, "Snapshot Isolation")
    provisional = _insert_concept(conn, "Read Committed", pending_review=True)
    other = _insert_concept(conn, "MVCC")
    qid = _enqueue(conn, "Read Committed", provisional, target, similarity=0.77)

    _add_edge(conn, provisional, other, source_id=42)

    rc.do_keep_separate(conn, qid)

    # Provisional still exists.
    row = conn.execute(
        "SELECT pending_review FROM concept WHERE concept_id = ?", [provisional]
    ).fetchone()
    assert row is not None
    assert row[0] is False

    # Edge is untouched.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_relation WHERE from_concept_id = ?",
        [provisional],
    ).fetchone()[0] == 1

    # Queue row recorded.
    action = conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0]
    assert action == "keep_separate"


# ----------------------------------------------------------------------------
# rename
# ----------------------------------------------------------------------------

def test_rename_only(conn):
    target = _insert_concept(conn, "Change Data Capture")
    provisional = _insert_concept(conn, "CDC stream", pending_review=True)
    qid = _enqueue(conn, "CDC stream", provisional, target)

    rc.do_rename(conn, qid, "Change Stream", merge_into=None)

    # Provisional renamed, pending_review cleared, still exists.
    row = conn.execute(
        "SELECT name, pending_review FROM concept WHERE concept_id = ?",
        [provisional],
    ).fetchone()
    assert row == ("Change Stream", False)

    action = conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0]
    assert action == "rename"


def test_rename_with_merge_into(conn):
    target = _insert_concept(conn, "Change Data Capture")
    provisional = _insert_concept(conn, "CDC stream", pending_review=True)
    other = _insert_concept(conn, "Debezium")
    qid = _enqueue(conn, "CDC stream", provisional, target)

    _add_edge(conn, provisional, other, source_id=77)

    rc.do_rename(conn, qid, "Change Stream", merge_into=target)

    # Provisional deleted.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [provisional]
    ).fetchone()[0] == 0

    # Edge moved to target.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        [target, other],
    ).fetchone()[0] == 1

    # Candidate name registered as alias on target.
    assert conn.execute(
        "SELECT COUNT(*) FROM concept_alias "
        "WHERE concept_id = ? AND LOWER(alias) = 'cdc stream'",
        [target],
    ).fetchone()[0] == 1


# ----------------------------------------------------------------------------
# list / show smoke tests (mostly check SQL doesn't blow up)
# ----------------------------------------------------------------------------

def test_list_prints_only_pending(conn, capsys):
    t = _insert_concept(conn, "A")
    p1 = _insert_concept(conn, "B", pending_review=True)
    p2 = _insert_concept(conn, "C", pending_review=True)
    q1 = _enqueue(conn, "B", p1, t, similarity=0.85)
    q2 = _enqueue(conn, "C", p2, t, similarity=0.80)

    # Resolve q1 first; list should now show only q2.
    rc.do_keep_separate(conn, q1)

    rc.do_list(conn, limit=10)
    out = capsys.readouterr().out
    assert "1 of 1 pending items" in out
    assert f"q={q2}" in out
    assert f"q={q1}" not in out


def test_show_reports_resolution_action(conn, capsys):
    t = _insert_concept(conn, "Target")
    p = _insert_concept(conn, "Provisional", pending_review=True)
    qid = _enqueue(conn, "Provisional", p, t, similarity=0.81)

    rc.do_show(conn, qid)
    out = capsys.readouterr().out
    assert f"queue_id:              {qid}" in out
    assert "resolution_action:     pending" in out
    assert "candidate_name:        'Provisional'" in out
