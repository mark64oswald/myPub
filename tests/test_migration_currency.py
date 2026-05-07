"""Tests for migration_guide.py + currency_report.py — Phase 13."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import migration_guide as mg  # noqa: E402
import currency_report as cr  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name):
    return conn.execute(
        "INSERT INTO concept (name) VALUES (?) RETURNING concept_id", [name],
    ).fetchone()[0]


def _seed_doc_source(conn, name, src_type="context7"):
    return conn.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
        "VALUES (?, ?, ?, ?) RETURNING doc_source_id",
        [name, src_type, src_type, f"/{name.lower()}/{name.lower()}"],
    ).fetchone()[0]


def _seed_snapshot(conn, ds_id, content_hash, retrieved_at=None):
    if retrieved_at is None:
        retrieved_at = datetime.now()
    return conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, content_hash, "
        "content, retrieved_at) "
        "VALUES (?, 'context7', ?, '...', ?) RETURNING snapshot_id",
        [ds_id, content_hash, retrieved_at],
    ).fetchone()[0]


def _seed_chapter(conn, book_title, chapter_title):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
        [book_title, f"/tmp/{book_title}.epub"],
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) VALUES (?, ?, 1) "
        "RETURNING chapter_id", [bid, chapter_title],
    ).fetchone()[0]


def _seed_section(conn, ds_id, snap_id, heading="Heading"):
    return conn.execute(
        "INSERT INTO doc_section (snapshot_id, ordinal, content, heading_text) "
        "VALUES (?, 0, '...', ?) RETURNING doc_section_id",
        [snap_id, heading],
    ).fetchone()[0]


def _seed_alignment_edge(conn, *, from_sec, to_chapter=None, to_sec=None,
                         concept_id, relation):
    conn.execute(
        "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, "
        "to_doc_section_id, concept_id, relation_type, confidence) "
        "VALUES (?, ?, ?, ?, ?, 0.9)",
        [from_sec, to_chapter, to_sec, concept_id, relation],
    )


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


# ---------------------------------------------------------------------------
# Migration Guide
# ---------------------------------------------------------------------------


def test_migration_unknown_subject_returns_negative(catalog):
    g = mg.make_migration_generator()
    res = _resolver({})
    pid, _, issues = g.run_deterministic(
        catalog, res, "ghost", output_root="/tmp",
    )
    assert pid == -1


def test_migration_data_starved_warns(catalog, tmp_path):
    """When CONTRADICTS edges are absent, validator emits a warning."""
    sid = _seed_concept(catalog, "Apache Kafka")
    res = _resolver({"apache kafka": sid})
    g = mg.make_migration_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Apache Kafka", output_root=str(tmp_path),
    )
    assert pid > 0
    assert any("data-starved" in i.message for i in issues
                if i.severity == "warning")


def test_migration_renders_files_even_when_empty(catalog, tmp_path):
    sid = _seed_concept(catalog, "Apache Kafka")
    res = _resolver({"apache kafka": sid})
    g = mg.make_migration_generator()
    pid, report, _ = g.run_deterministic(
        catalog, res, "Apache Kafka", output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "apache-kafka"
    assert (pkg_dir / "_migration.md").exists()
    assert (pkg_dir / "_superseded.md").exists()
    text = (pkg_dir / "_migration.md").read_text().lower()
    # Migration text honestly explains the empty deliverable.
    assert "no contradicts edges" in text or "data-starved" in text


def test_migration_with_contradicts_edges_renders_diffs(catalog, tmp_path):
    """Seed a CONTRADICTS edge and confirm it appears in the output."""
    sid = _seed_concept(catalog, "Apache Kafka")
    other = _seed_concept(catalog, "Tied Concept")
    # Wire neighbor relation so 'other' is in the BFS frontier
    conn = catalog
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, 'CITES', 'chapter', 1, 0.9)", [sid, other],
    )
    ds_id = _seed_doc_source(catalog, "Kafka")
    snap = _seed_snapshot(catalog, ds_id, "h1")
    sec = _seed_section(catalog, ds_id, snap, heading="New API")
    chap = _seed_chapter(catalog, "Old Kafka Book", "Outdated chapter")
    _seed_alignment_edge(
        catalog, from_sec=sec, to_chapter=chap,
        concept_id=other, relation="CONTRADICTS",
    )
    res = _resolver({"apache kafka": sid})
    g = mg.make_migration_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Apache Kafka", output_root=str(tmp_path),
    )
    assert pid > 0
    text = (tmp_path / "apache-kafka" / "_migration.md").read_text()
    assert "Tied Concept" in text


def test_migration_idempotent(catalog, tmp_path):
    sid = _seed_concept(catalog, "Apache Kafka")
    res = _resolver({"apache kafka": sid})
    g = mg.make_migration_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Apache Kafka",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Apache Kafka",
                                       output_root=str(tmp_path))
    assert pid1 == pid2


# ---------------------------------------------------------------------------
# Currency Report
# ---------------------------------------------------------------------------


def test_currency_no_sources_errors(catalog, tmp_path):
    g = cr.make_currency_report_generator()
    res = _resolver({})
    pid, report, issues = g.run_deterministic(
        catalog, res, "all-sources", output_root=str(tmp_path),
    )
    # Empty corpus → -1 (validator errors before persist)
    assert pid == -1


def test_currency_audits_all_sources(catalog, tmp_path):
    ds1 = _seed_doc_source(catalog, "S1")
    ds2 = _seed_doc_source(catalog, "S2")
    _seed_snapshot(catalog, ds1, "h1")
    _seed_snapshot(catalog, ds2, "h2")
    res = _resolver({})
    g = cr.make_currency_report_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "all-sources", output_root=str(tmp_path),
    )
    assert pid > 0
    assert (tmp_path / "all-sources" / "_report.md").exists()


def test_currency_filtered_to_one_source(catalog, tmp_path):
    ds1 = _seed_doc_source(catalog, "S1")
    ds2 = _seed_doc_source(catalog, "S2")
    _seed_snapshot(catalog, ds1, "h1")
    _seed_snapshot(catalog, ds2, "h2")
    res = _resolver({})
    g = cr.make_currency_report_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "S1", source_filter="S1", output_root=str(tmp_path),
    )
    assert pid > 0
    text = (tmp_path / "s1" / "_report.md").read_text()
    assert "S1" in text and "S2" not in text


def test_currency_volatility_higher_for_hash_churn(catalog, tmp_path):
    ds1 = _seed_doc_source(catalog, "Stable")
    ds2 = _seed_doc_source(catalog, "Churning")
    # Stable: same hash repeated
    _seed_snapshot(catalog, ds1, "h_same",
                    retrieved_at=datetime(2026, 1, 1))
    _seed_snapshot(catalog, ds1, "h_same",
                    retrieved_at=datetime(2026, 2, 1))
    _seed_snapshot(catalog, ds1, "h_same",
                    retrieved_at=datetime(2026, 3, 1))
    # Churning: every snapshot has new hash
    _seed_snapshot(catalog, ds2, "ha", retrieved_at=datetime(2026, 1, 1))
    _seed_snapshot(catalog, ds2, "hb", retrieved_at=datetime(2026, 2, 1))
    _seed_snapshot(catalog, ds2, "hc", retrieved_at=datetime(2026, 3, 1))
    res = _resolver({})
    d = cr.SnapshotHistoryDecomposer().decompose(catalog, res, "all")
    by_name = {r.name: r for r in d.sources}
    assert by_name["Churning"].volatility_score > by_name["Stable"].volatility_score


def test_currency_renders_per_source_timeline(catalog, tmp_path):
    ds1 = _seed_doc_source(catalog, "S1")
    _seed_snapshot(catalog, ds1, "h1", retrieved_at=datetime(2026, 1, 1))
    _seed_snapshot(catalog, ds1, "h2", retrieved_at=datetime(2026, 2, 1))
    res = _resolver({})
    g = cr.make_currency_report_generator()
    pid, _, _ = g.run_deterministic(
        catalog, res, "all", output_root=str(tmp_path),
    )
    timeline = (tmp_path / "all" / "sources" / "s1.md").read_text()
    assert "Currency Timeline" in timeline
    assert "h1" in timeline and "h2" in timeline


def test_currency_idempotent(catalog, tmp_path):
    ds1 = _seed_doc_source(catalog, "S1")
    _seed_snapshot(catalog, ds1, "h1")
    res = _resolver({})
    g = cr.make_currency_report_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "all",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "all",
                                       output_root=str(tmp_path))
    assert pid1 == pid2
