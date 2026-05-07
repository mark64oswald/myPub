"""Tests for curriculum.py — Phase 16 Curriculum Generator (composite)."""
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

import curriculum as cu  # noqa: E402


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
def chain_corpus(catalog):
    """Linear prereq chain so the Learning Path decomposer produces stages."""
    sql = _seed_concept(catalog, "SQL")
    db = _seed_concept(catalog, "Database Design")
    schema = _seed_concept(catalog, "Schema Modeling")
    pipe = _seed_concept(catalog, "Data Pipeline")
    cdc = _seed_concept(catalog, "Change Data Capture")
    _add_rel(catalog, cdc, pipe, "REQUIRES")
    _add_rel(catalog, pipe, schema, "REQUIRES")
    _add_rel(catalog, schema, db, "REQUIRES")
    _add_rel(catalog, db, sql, "REQUIRES")
    return {"sql": sql, "db": db, "schema": schema, "pipe": pipe, "cdc": cdc}


# ---------------------------------------------------------------------------


def test_unknown_topic_returns_negative(catalog):
    res = _resolver({})
    g = cu.make_curriculum_generator()
    pid, _, _ = g.run_deterministic(catalog, res, "ghost", output_root="/tmp")
    assert pid == -1


def test_decomposer_produces_n_weeks(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=8,
    )
    assert len(d.weeks) == 8


def test_decomposer_marks_tutorial_starting_week_2(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=6,
    )
    assert not d.weeks[0].has_tutorial
    assert d.weeks[1].has_tutorial


def test_decomposer_marks_patterns_in_last_third(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=12,
    )
    # Last third = weeks 9, 10, 11, 12 (after threshold 12*2//3 = 8)
    assert all(not w.has_patterns for w in d.weeks[:8])
    assert all(w.has_patterns for w in d.weeks[8:])


def test_decomposer_emits_note_when_stages_repeat(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=20,
    )
    assert any("repeat" in n for n in d.notes)


def test_render_curriculum_lists_all_weeks(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=4,
    )
    text = cu._render_curriculum(d)
    for w in d.weeks:
        assert f"`weeks/week-{w.ordinal}/`" in text


def test_render_week_includes_anchor_and_concepts(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=3,
    )
    text = cu._render_week(d.weeks[0])
    assert d.weeks[0].stage.name in text
    assert "## Concepts" in text


def test_planner_one_unit_per_week(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=5,
    )
    plan = cu.CurriculumPlanner().plan(catalog, d)
    assert len(plan.units) == 5
    fnames = {f.filename for f in plan.files}
    assert "_curriculum.md" in fnames
    assert sum(1 for n in fnames if n.startswith("weeks/week-")) == 5


def test_validator_passes_for_valid_plan(catalog, chain_corpus):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    d = cu.CurriculumDecomposer().decompose(
        catalog, res, "Change Data Capture", n_weeks=4,
    )
    plan = cu.CurriculumPlanner().plan(catalog, d)
    issues = cu.CurriculumValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_unresolved_topic_errors(catalog):
    from generator import GenPlan
    plan = GenPlan(generator_type="curriculum", package_name="x", domain="x",
                    package_metadata={"topic_concept_id": -1})
    issues = cu.CurriculumValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "topic" in i.message for i in issues)


def test_run_deterministic_writes_curriculum_and_weeks(catalog, chain_corpus, tmp_path):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    g = cu.make_curriculum_generator()
    pid, _, _ = g.run_deterministic(
        catalog, res, "Change Data Capture",
        n_weeks=6, output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "change-data-capture"
    assert (pkg_dir / "_curriculum.md").exists()
    assert (pkg_dir / "weeks" / "week-1" / "_week.md").exists()
    assert (pkg_dir / "weeks" / "week-6" / "_week.md").exists()


def test_run_deterministic_idempotent(catalog, chain_corpus, tmp_path):
    res = _resolver({"change data capture": chain_corpus["cdc"]})
    g = cu.make_curriculum_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       n_weeks=4, output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       n_weeks=4, output_root=str(tmp_path))
    assert pid1 == pid2
