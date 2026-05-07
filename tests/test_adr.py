"""Tests for adr.py — Phase 12 ADR Generator."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import adr  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Pattern", description=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES (?, ?, ?) RETURNING concept_id",
        [name, concept_type, description],
    ).fetchone()[0]


_REL = [0]


def _add_rel(conn, src, tgt, rel_type, source_id=None):
    _REL[0] += 1
    sid = source_id if source_id is not None else _REL[0]
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, sid],
    )


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


@pytest.fixture
def es_corpus(catalog):
    es = _seed_concept(catalog, "Event Sourcing",
                        description="Persist state as append-only event log.")
    crud = _seed_concept(catalog, "CRUD Storage",
                          description="Mutable rows updated in place.")
    snapshot = _seed_concept(catalog, "Snapshot Storage",
                              description="Periodic full-state snapshots.")
    _add_rel(catalog, es, crud, "CONTRASTS_WITH")
    _add_rel(catalog, es, snapshot, "CONTRASTS_WITH")
    return {"es": es, "crud": crud, "snapshot": snapshot}


# ---------------------------------------------------------------------------


def test_slugify():
    assert adr._slugify("Adopt event sourcing?") == "adopt-event-sourcing"


def test_short_caps_length():
    assert len(adr._short("a" * 500, limit=50)) <= 51


def test_decomposer_unknown_anchor_returns_empty(catalog, es_corpus):
    res = _resolver({})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "ghost")
    assert d.anchor_concept_id == -1


def test_decomposer_finds_options_via_contrasts(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    names = {o.name for o in d.options}
    assert "Event Sourcing" in names  # anchor included
    assert "CRUD Storage" in names
    assert "Snapshot Storage" in names


def test_decomposer_handles_no_contrasts(catalog):
    seed = _seed_concept(catalog, "Lonely Pattern")
    res = _resolver({"lonely pattern": seed})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Lonely Pattern")
    # Anchor still becomes option 1
    assert len(d.options) == 1


def test_decomposer_max_options_caps(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(
        catalog, res, "Event Sourcing", max_options=2,
    )
    assert len(d.options) == 2


def test_render_adr_includes_status_context_decision_sections(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    text = adr._render_adr(d)
    for sec in ("# ADR:", "## Status", "## Context", "## Options",
                "## Pros & Cons", "## Decision", "## Consequences"):
        assert sec in text


def test_render_options_lists_each_option(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    text = adr._render_options(d)
    for o in d.options:
        assert o.name in text


def test_planner_one_unit_per_option(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    plan = adr.ADRPlanner().plan(catalog, d)
    assert len(plan.units) == len(d.options)
    fnames = {f.filename for f in plan.files}
    assert fnames == {"adr.md", "_options.md", "_references.md"}


def test_validator_passes_for_valid_plan(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    plan = adr.ADRPlanner().plan(catalog, d)
    issues = adr.ADRValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_warns_when_only_one_option(catalog):
    seed = _seed_concept(catalog, "Lonely")
    res = _resolver({"lonely": seed})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Lonely")
    plan = adr.ADRPlanner().plan(catalog, d)
    issues = adr.ADRValidator().validate(catalog, plan)
    assert any("only" in i.message.lower() and i.severity == "warning"
                for i in issues)


def test_validator_flags_phantom_concept_id(catalog, es_corpus):
    res = _resolver({"event sourcing": es_corpus["es"]})
    d = adr.ContrastsDecomposer().decompose(catalog, res, "Event Sourcing")
    plan = adr.ADRPlanner().plan(catalog, d)
    plan.units[0].metadata["concept_id"] = 999_999
    issues = adr.ADRValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


def test_run_deterministic_writes_three_files(catalog, es_corpus, tmp_path):
    res = _resolver({"event sourcing": es_corpus["es"]})
    g = adr.make_adr_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Event Sourcing", output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "event-sourcing"
    for fname in ("adr.md", "_options.md", "_references.md"):
        assert (pkg_dir / fname).exists()


def test_run_deterministic_idempotent(catalog, es_corpus, tmp_path):
    res = _resolver({"event sourcing": es_corpus["es"]})
    g = adr.make_adr_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Event Sourcing",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Event Sourcing",
                                       output_root=str(tmp_path))
    assert pid1 == pid2


def test_run_deterministic_unknown_returns_negative(catalog, tmp_path):
    res = _resolver({})
    g = adr.make_adr_generator()
    pid, _, issues = g.run_deterministic(catalog, res, "ghost",
                                           output_root=str(tmp_path))
    assert pid == -1
