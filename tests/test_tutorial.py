"""Tests for tutorial.py — Phase 10 Tutorial Generator (deterministic v1)."""
from __future__ import annotations

import json
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

import tutorial as tu  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Concept"):
    return conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES (?, ?) RETURNING concept_id",
        [name, concept_type],
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


def _seed_book_chapter(conn, book_title, chapter_title):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
        [book_title, f"/tmp/{book_title.replace(' ', '_')}.epub"],
    ).fetchone()[0]
    cid = conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, ?, 1) RETURNING chapter_id", [bid, chapter_title],
    ).fetchone()[0]
    return cid


def _seed_procedure(conn, name, steps_json, *, preconditions=None,
                    postconditions=None, failure_modes=None):
    return conn.execute(
        """
        INSERT INTO procedure (name, preconditions, steps, postconditions,
                                failure_modes, source_type, source_id)
        VALUES (?, ?, ?, ?, ?, 'chapter', 1) RETURNING procedure_id
        """,
        [name, preconditions, steps_json, postconditions, failure_modes],
    ).fetchone()[0]


def _link_proc(conn, pid, cid):
    conn.execute(
        "INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
        [pid, cid],
    )


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


@pytest.fixture
def cdc_corpus(catalog):
    """Tiny linear chain: SQL → Database Design → Schema → Pipeline → CDC."""
    sql = _seed_concept(catalog, "SQL")
    db = _seed_concept(catalog, "Database Design")
    schema = _seed_concept(catalog, "Schema Modeling")
    pipe = _seed_concept(catalog, "Data Pipeline")
    cdc = _seed_concept(catalog, "Change Data Capture")
    _add_rel(catalog, cdc, pipe, "REQUIRES")
    _add_rel(catalog, pipe, schema, "REQUIRES")
    _add_rel(catalog, schema, db, "REQUIRES")
    _add_rel(catalog, db, sql, "REQUIRES")

    # Chapters for chapter_count > 0 on prereq enrichment
    ch_db = _seed_book_chapter(catalog, "DB Fundamentals", "Schemas and Models")
    ch_pipe = _seed_book_chapter(catalog, "Streaming Systems", "Pipelines & CDC")
    _add_rel(catalog, sql, db, "CITES", source_id=ch_db)
    _add_rel(catalog, db, schema, "CITES", source_id=ch_db)
    _add_rel(catalog, pipe, cdc, "CITES", source_id=ch_pipe)

    # Procedures, one per concept
    p_sql = _seed_procedure(
        catalog, "Run a SELECT query",
        json.dumps([
            {"n": 1, "action": "Open the SQL prompt",
             "command": "psql -U user db"},
            {"n": 2, "action": "Run a basic SELECT",
             "command": "SELECT * FROM users LIMIT 10;"},
        ]),
        preconditions="psql installed",
        postconditions="You can read rows from a relational table\nYou understand WHERE clauses",
    )
    _link_proc(catalog, p_sql, sql)

    p_pipe = _seed_procedure(
        catalog, "Run a Kafka consumer",
        json.dumps([
            {"n": 1, "action": "Start the Kafka broker",
             "command": "kafka-server-start.sh config/server.properties"},
        ]),
        preconditions="Java installed",
        postconditions="Pipeline reads messages from Kafka",
        failure_modes="Watermarks must be set on event time",
    )
    _link_proc(catalog, p_pipe, pipe)

    return {
        "sql": sql, "db": db, "schema": schema, "pipe": pipe, "cdc": cdc,
        "ch_db": ch_db, "ch_pipe": ch_pipe,
        "p_sql": p_sql, "p_pipe": p_pipe,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_slugify_handles_punctuation():
    assert tu._slugify("Schema Modeling") == "schema-modeling"
    assert tu._slugify("") == "stage"


def test_short_caps_length():
    out = tu._short("a" * 500, limit=50)
    assert len(out) <= 51


def test_parse_steps_returns_list_for_json():
    raw = json.dumps([{"n": 1, "action": "do X"}, {"n": 2, "action": "do Y"}])
    out = tu._parse_steps(raw)
    assert len(out) == 2
    assert out[0]["action"] == "do X"


def test_parse_steps_caps_at_max():
    raw = json.dumps([{"n": i, "action": f"step{i}"} for i in range(20)])
    out = tu._parse_steps(raw, max_steps=5)
    assert len(out) == 5


def test_parse_steps_handles_plain_text():
    out = tu._parse_steps("plain prose, not JSON")
    assert len(out) == 1
    assert "plain prose" in out[0]["action"]


def test_parse_steps_handles_empty():
    assert tu._parse_steps(None) == []
    assert tu._parse_steps("") == []


def test_render_exercise_renders_command_in_code_block():
    steps = [
        {"n": 1, "action": "Run the script",
         "command": "python script.py"},
    ]
    out = tu._render_exercise_steps(steps)
    assert "python script.py" in out
    assert "1. Run the script" in out


def test_render_exercise_handles_empty():
    out = tu._render_exercise_steps([])
    assert "conceptual" in out.lower()


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def test_decomposer_unknown_target_returns_empty(catalog, cdc_corpus):
    res = _resolver({})
    d = tu.ProcedureBackedDecomposer().decompose(catalog, res, "ghost")
    assert d.target_concept_id == -1
    assert d.stages == []


def test_decomposer_resolves_target_and_walks_chain(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    assert d.target_concept_id == cdc_corpus["cdc"]
    assert len(d.stages) > 0


def test_decomposer_attaches_procedures_when_available(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    backed = [s for s in d.stages if s.procedure is not None]
    assert backed, "expected at least one stage with a backing procedure"


def test_decomposer_max_stages_caps_count(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture", max_stages=2,
    )
    assert len(d.stages) <= 2


def test_decomposer_emits_unbacked_note_when_some_stages_lack_procedures(
    catalog, cdc_corpus,
):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    n_unbacked = sum(1 for s in d.stages if s.procedure is None)
    if n_unbacked > 0:
        assert any("backing procedure" in n for n in d.notes)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_tutorial_includes_target_and_stages(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    text = tu._render_tutorial(d)
    assert text.startswith("# Tutorial: Change Data Capture")
    for s in d.stages:
        assert f"Stage {s.ordinal}" in text


def test_render_tutorial_includes_command_for_backed_stage(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    text = tu._render_tutorial(d)
    # The seeded procedures contain SQL/Kafka commands; at least one
    # should appear if the stage's procedure backed.
    assert any(token in text for token in ("psql", "SELECT", "kafka-server-start"))


def test_render_setup_lists_required_knowledge(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    text = tu._render_setup(d)
    assert "## Required knowledge" in text


def test_render_checkpoints_creates_checkbox_items(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    text = tu._render_checkpoints(d)
    assert "- [ ]" in text


# ---------------------------------------------------------------------------
# Planner / Validator
# ---------------------------------------------------------------------------


def test_planner_one_unit_per_stage_plus_3_files(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    plan = tu.TutorialPlanner().plan(catalog, d)
    assert len(plan.units) == len(d.stages)
    fnames = {f.filename for f in plan.files}
    assert fnames == {"tutorial.md", "_setup.md", "_checkpoints.md"}


def test_validator_passes_for_valid_plan(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    plan = tu.TutorialPlanner().plan(catalog, d)
    issues = tu.TutorialValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_phantom_procedure_id(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = tu.ProcedureBackedDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    plan = tu.TutorialPlanner().plan(catalog, d)
    plan.units[0].metadata["procedure_id"] = 999_999
    issues = tu.TutorialValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


def test_validator_unresolved_target_errors(catalog):
    from generator import GenPlan
    plan = GenPlan(generator_type="tutorial", package_name="x", domain="x",
                    package_metadata={"target_concept_id": -1})
    issues = tu.TutorialValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "target" in i.message for i in issues)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_three_files(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = tu.make_tutorial_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Change Data Capture", output_root=str(tmp_path),
    )
    assert pid > 0
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    pkg_dir = tmp_path / "change-data-capture"
    for fname in ("tutorial.md", "_setup.md", "_checkpoints.md"):
        assert (pkg_dir / fname).exists()


def test_run_deterministic_idempotent(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = tu.make_tutorial_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       output_root=str(tmp_path))
    assert pid1 == pid2
