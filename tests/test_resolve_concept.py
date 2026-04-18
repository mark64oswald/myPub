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


# ----------------------------------------------------------------------------
# HNSW-present path — uses conftest.realistic_conn so the DML against
# concept_embedding actually hits the HNSW index. This is the path that
# silently broke in production and went uncaught by the plain-schema
# tests above. Regression against today's bug.
# ----------------------------------------------------------------------------

def _seed_pair_against_realistic(conn, embedder, target_name: str, prov_name: str):
    """Insert a target + provisional pair on top of the realistic fixture.

    Returns (target_id, provisional_id, queue_id). The realistic fixture
    pre-seeds two scratch concepts plus HNSW indexes; these additions
    don't conflict with that.
    """
    t_id = conn.execute(
        "INSERT INTO concept (name, concept_type, description, pending_review) "
        "VALUES (?, 'Concept', ?, FALSE) RETURNING concept_id",
        [target_name, f"desc of {target_name}"],
    ).fetchone()[0]
    p_id = conn.execute(
        "INSERT INTO concept (name, concept_type, description, pending_review) "
        "VALUES (?, 'Concept', ?, TRUE) RETURNING concept_id",
        [prov_name, f"desc of {prov_name}"],
    ).fetchone()[0]
    # Real embeddings so distances aren't all zero.
    vec_t = embedder.encode([target_name], convert_to_numpy=True)[0].astype("float32").tolist()
    vec_p = embedder.encode([prov_name], convert_to_numpy=True)[0].astype("float32").tolist()
    conn.execute(
        "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
        [t_id, vec_t],
    )
    conn.execute(
        "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
        [p_id, vec_p],
    )
    qid = conn.execute(
        """
        INSERT INTO concept_resolution_queue
            (candidate_name, candidate_context, source_type, source_id,
             nearest_concept_id, provisional_concept_id,
             similarity_score, resolution_action)
        VALUES (?, 'test context', 'chapter', 1, ?, ?, 0.80, 'pending')
        RETURNING queue_id
        """,
        [prov_name, t_id, p_id],
    ).fetchone()[0]
    return t_id, p_id, qid


def test_merge_works_when_concept_embedding_has_hnsw_index(realistic_conn, embedder):
    """This is the regression test for today's bug — merge deletes a
    concept_embedding row that's covered by an HNSW index. Before the
    `LOAD vss` fix in resolve_concept.main, this raised:
       'Cannot bind index concept_embedding, unknown index type HNSW'
    even though VSS was installed on the catalog. Now the CLI loads vss
    on connection, so the DELETE succeeds. This test pins that fix."""
    t_id, p_id, qid = _seed_pair_against_realistic(
        realistic_conn, embedder, "X-axis Scaling", "Horizontal Scaling"
    )
    rc.do_merge(realistic_conn, qid, register_alias=True)
    # Provisional is gone, target kept, alias registered.
    assert realistic_conn.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [p_id]
    ).fetchone()[0] == 0
    assert realistic_conn.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?", [p_id]
    ).fetchone()[0] == 0
    assert realistic_conn.execute(
        "SELECT alias FROM concept_alias WHERE concept_id = ?", [t_id]
    ).fetchone()[0] == "Horizontal Scaling"
    assert realistic_conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0] == "alias"


def test_rename_works_when_concept_embedding_has_hnsw_index(realistic_conn, embedder):
    """rename goes through the park-and-reinsert workaround for the
    UNIQUE-column UPDATE bug. Verify it doesn't regress when the
    concept_embedding table carries an HNSW index."""
    _t_id, p_id, qid = _seed_pair_against_realistic(
        realistic_conn, embedder, "Change Data Capture", "CDC stream"
    )
    rc.do_rename(realistic_conn, qid, "Change Stream", merge_into=None)
    row = realistic_conn.execute(
        "SELECT name, pending_review FROM concept WHERE concept_id = ?", [p_id]
    ).fetchone()
    assert row == ("Change Stream", False)
    # Embedding is still present (reinserted by the workaround).
    assert realistic_conn.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?", [p_id]
    ).fetchone()[0] == 1


def test_keep_separate_works_when_concept_embedding_has_hnsw_index(
    realistic_conn, embedder,
):
    """keep-separate only UPDATEs concept.pending_review — a plain scalar
    column, not FK-touching and not UNIQUE. Should work with or without
    the HNSW index present. Pinned here anyway so we'd catch a regression
    if DuckDB broadens the bug to touch scalars."""
    _, p_id, qid = _seed_pair_against_realistic(
        realistic_conn, embedder, "Serializability", "Weak Isolation"
    )
    rc.do_keep_separate(realistic_conn, qid)
    assert realistic_conn.execute(
        "SELECT pending_review FROM concept WHERE concept_id = ?", [p_id]
    ).fetchone()[0] is False


def test_alias_shorthand_works_when_hnsw_indexed(realistic_conn, embedder):
    """The `alias` command is `merge --register-alias` by another name;
    exercise it against the HNSW-indexed fixture to confirm both paths
    work end-to-end."""
    t_id, p_id, qid = _seed_pair_against_realistic(
        realistic_conn, embedder, "Logstash", "Heka"
    )
    rc.do_merge(realistic_conn, qid, register_alias=True)
    assert realistic_conn.execute(
        "SELECT alias FROM concept_alias WHERE concept_id = ? "
        "ORDER BY alias_id DESC LIMIT 1",
        [t_id],
    ).fetchone()[0] == "Heka"
    assert realistic_conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0] == "alias"
    assert realistic_conn.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [p_id]
    ).fetchone()[0] == 0
