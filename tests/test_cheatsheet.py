"""Tests for cheatsheet.py — Phase 9.4 Cheatsheet generator (v1).

Uses a tiny seeded subject (Delta Lake) with a handful of procedures
spanning the category taxonomy, plus an EXTENDS descendant whose
procedures should be pulled in.
"""
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

import cheatsheet as cs  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Tool"):
    return conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES (?, ?) RETURNING concept_id",
        [name, concept_type],
    ).fetchone()[0]


def _seed_procedure(conn, name, steps, *, preconditions=None, failure_modes=None,
                    source_type="chapter", source_id=1):
    return conn.execute(
        """
        INSERT INTO procedure (name, preconditions, steps, failure_modes,
                                 source_type, source_id)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING procedure_id
        """,
        [name, preconditions, steps, failure_modes, source_type, source_id],
    ).fetchone()[0]


def _link(conn, procedure_id, concept_id):
    conn.execute(
        "INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
        [procedure_id, concept_id],
    )


_REL = [0]


def _add_rel(conn, src, tgt, rel_type):
    _REL[0] += 1
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, _REL[0]],
    )


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


# ---------------------------------------------------------------------------
# Category classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,steps,expected",
    [
        ("Create a Delta table", "CREATE TABLE x (...)", "CRUD"),
        ("Configure write mode", "Set spark.databricks...", "Configuration"),
        ("Optimize file layout", "Run OPTIMIZE table", "Performance"),
        ("Recover from a failed write", "When the write fails, retry...", "Errors"),
        ("Connect Spark to Delta", "Use spark.read.format('delta').load(...)", "Integration"),
        ("Install Delta Lake", "pip install delta-spark", "Install"),
        ("Start a streaming pipeline", "spark.readStream...", "Operations"),
        ("Use Delta with Iceberg", "Some general usage notes", "General"),
    ],
)
def test_classifier_buckets_by_keyword(name, steps, expected):
    assert cs._classify_procedure(name, steps) == expected


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


@pytest.fixture
def delta_lake_corpus(catalog):
    delta = _seed_concept(catalog, "Delta Lake")
    streaming = _seed_concept(catalog, "Delta Streaming")
    _add_rel(catalog, streaming, delta, "EXTENDS")  # Streaming EXTENDS Delta

    p_create = _seed_procedure(catalog, "Create a Delta table",
                                "CREATE TABLE foo USING DELTA",
                                failure_modes="Path collision if not OVERWRITE")
    p_optim = _seed_procedure(catalog, "Optimize Delta partitions",
                               "OPTIMIZE foo ZORDER BY id")
    p_install = _seed_procedure(catalog, "Install Delta Lake on Spark",
                                 "pip install delta-spark")
    p_stream = _seed_procedure(catalog, "Start a Delta streaming write",
                                "spark.readStream.format('delta').load('/foo')",
                                failure_modes="Watermarks must be set on event time")
    for p in (p_create, p_optim, p_install):
        _link(catalog, p, delta)
    _link(catalog, p_stream, streaming)

    return {
        "delta": delta, "streaming": streaming,
        "p_create": p_create, "p_optim": p_optim,
        "p_install": p_install, "p_stream": p_stream,
    }


def test_decomposer_pulls_subject_procedures(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    assert d.subject_concept_id == delta_lake_corpus["delta"]
    # Subject's own 3 procedures + 1 from EXTENDS descendant = 4
    assert d.n_procedures_total == 4


def test_decomposer_extends_depth_zero_excludes_descendants(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(
        catalog, res, "Delta Lake", extends_depth=0,
    )
    # Without EXTENDS walk, only the 3 subject-direct procedures
    assert d.n_procedures_total == 3


def test_decomposer_clusters_by_category(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    cats = {c.category for c in d.clusters}
    # Expect CRUD (Create), Performance (Optimize), Install, Operations (Stream)
    assert "CRUD" in cats
    assert "Performance" in cats
    assert "Install" in cats
    assert "Operations" in cats


def test_decomposer_aggregates_failure_modes(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    # Two procedures have failure_modes
    proc_names = {p[0] for p in d.failure_modes}
    assert "Create a Delta table" in proc_names
    assert "Start a Delta streaming write" in proc_names


def test_decomposer_unknown_subject_returns_empty(catalog, delta_lake_corpus):
    res = _resolver({})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "ghost")
    assert d.subject_concept_id == -1
    assert any("not found" in n for n in d.notes)


def test_decomposer_subject_with_no_procedures_emits_note(catalog):
    seed = _seed_concept(catalog, "Empty Subject")
    res = _resolver({"empty subject": seed})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Empty Subject")
    assert d.n_procedures_total == 0
    assert d.clusters == []
    assert any("no procedures" in n for n in d.notes)


def test_decomposer_max_per_section_caps_each_cluster(catalog):
    delta = _seed_concept(catalog, "Delta Lake")
    # Seed 10 CRUD procedures
    for i in range(10):
        p = _seed_procedure(catalog, f"Create thing {i}", "CREATE TABLE x")
        _link(catalog, p, delta)
    res = _resolver({"delta lake": delta})
    d = cs.TopicalCondenseDecomposer().decompose(
        catalog, res, "Delta Lake", max_per_section=4,
    )
    crud = next(c for c in d.clusters if c.category == "CRUD")
    assert len(crud.procedures) == 4


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_format_steps_extracts_commands_from_json_steps():
    raw = (
        '[{"n": 1, "action": "Format HDFS", "command": "hdfs namenode -format"},'
        ' {"n": 2, "action": "Start DFS", "command": "start-dfs.sh"}]'
    )
    out = cs._format_steps_for_cheatsheet(raw)
    assert "hdfs namenode -format" in out
    assert "start-dfs.sh" in out
    # Action without command should NOT appear when command exists
    assert "Format HDFS" not in out


def test_format_steps_falls_back_to_action_when_no_command():
    raw = '[{"n": 1, "action": "Open the workspace"}]'
    out = cs._format_steps_for_cheatsheet(raw)
    assert "# Open the workspace" in out  # comment-style narration


def test_format_steps_caps_step_count():
    steps = [{"n": i, "command": f"cmd{i}"} for i in range(10)]
    raw = json.dumps(steps)
    out = cs._format_steps_for_cheatsheet(raw, max_steps=3)
    assert "cmd0" in out and "cmd1" in out and "cmd2" in out
    assert "cmd3" not in out
    assert "+7 more steps" in out


def test_format_steps_caps_per_step_chars():
    raw = '[{"n": 1, "command": "' + ("x" * 500) + '"}]'
    out = cs._format_steps_for_cheatsheet(raw, max_chars_per_step=80)
    # Output should be ≤ ~81 chars (80 + ellipsis), not 500
    assert len(out.splitlines()[0]) <= 100  # forgiving upper bound
    assert out.endswith("…")


def test_format_steps_handles_plain_text_fallback():
    raw = "This is plain prose, not JSON."
    out = cs._format_steps_for_cheatsheet(raw)
    assert "plain prose" in out


def test_format_steps_handles_malformed_json():
    raw = '[{"n": 1, "command": "broken'  # unclosed
    out = cs._format_steps_for_cheatsheet(raw)
    # Falls through to text-truncation; at minimum doesn't crash
    assert isinstance(out, str)


def test_format_steps_handles_empty_input():
    assert cs._format_steps_for_cheatsheet(None) == ""
    assert cs._format_steps_for_cheatsheet("") == ""


def test_truncate_steps_collapses_whitespace_and_caps_length():
    text = "abc   def\n\n\nghi" + " " + ("x" * 300)
    out = cs._truncate_steps(text, 50)
    assert len(out) <= 51  # +1 for trailing ellipsis
    assert out.endswith("…")
    assert "  " not in out


def test_render_cheatsheet_includes_all_clusters(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    text = cs._render_cheatsheet(d)
    assert text.startswith("# Delta Lake")
    assert "## CRUD" in text
    assert "## Performance" in text
    assert "## Gotchas" in text
    # Code-block snippets present
    assert "```" in text


def test_render_cheatsheet_handles_empty_decomposition(catalog):
    seed = _seed_concept(catalog, "Lonely Subject")
    res = _resolver({"lonely subject": seed})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Lonely Subject")
    text = cs._render_cheatsheet(d)
    assert "No procedures available" in text


def test_render_provenance_lists_each_procedure(catalog, delta_lake_corpus):
    # Add a real chapter so source_label works.
    bid = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('B', '/tmp/b.epub') RETURNING book_id"
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO chapter (chapter_id, book_id, title, chapter_num) "
        "VALUES (1, ?, 'Chapter A', 1)", [bid],
    )
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    text = cs._render_provenance(d)
    assert "Provenance" in text
    # Procedure ids surface
    assert "procedure_id=" in text


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_validator_passes_for_valid_plan(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    plan = cs.CheatsheetPlanner().plan(catalog, d)
    issues = cs.CheatsheetValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_word_count_overrun(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    plan = cs.CheatsheetPlanner().plan(catalog, d, max_words=20)
    issues = cs.CheatsheetValidator().validate(catalog, plan)
    assert any("exceeds page heuristic" in i.message for i in issues
                if i.severity == "warning")


def test_validator_flags_phantom_procedure_id(catalog, delta_lake_corpus):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Delta Lake")
    plan = cs.CheatsheetPlanner().plan(catalog, d)
    plan.units[0].metadata["procedure_ids"] = (
        list(plan.units[0].metadata.get("procedure_ids", [])) + [999_999]
    )
    issues = cs.CheatsheetValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


def test_validator_warns_when_no_procedures(catalog):
    seed = _seed_concept(catalog, "Empty Subject")
    res = _resolver({"empty subject": seed})
    d = cs.TopicalCondenseDecomposer().decompose(catalog, res, "Empty Subject")
    plan = cs.CheatsheetPlanner().plan(catalog, d)
    issues = cs.CheatsheetValidator().validate(catalog, plan)
    # No errors, but a warning about the empty deliverable
    assert all(i.severity != "error" for i in issues)
    assert any("no procedure clusters" in i.message for i in issues)


# ---------------------------------------------------------------------------
# End-to-end Generator.run_deterministic
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_three_files(catalog, delta_lake_corpus, tmp_path):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    g = cs.make_cheatsheet_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Delta Lake", output_root=str(tmp_path),
    )
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    assert pid > 0

    pkg_dir = tmp_path / "delta-lake"
    assert (pkg_dir / "cheatsheet.md").exists()
    assert (pkg_dir / "_provenance.md").exists()
    assert (pkg_dir / "_gotchas.md").exists()


def test_run_deterministic_idempotent(catalog, delta_lake_corpus, tmp_path):
    res = _resolver({"delta lake": delta_lake_corpus["delta"]})
    g = cs.make_cheatsheet_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, res, "Delta Lake", output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, res, "Delta Lake", output_root=str(tmp_path),
    )
    assert pid1 == pid2


def test_run_deterministic_unknown_subject_returns_negative(catalog, tmp_path):
    res = _resolver({})
    g = cs.make_cheatsheet_generator()
    pid, _, issues = g.run_deterministic(
        catalog, res, "ghost", output_root=str(tmp_path),
    )
    assert pid == -1
    assert any(i.severity == "error" for i in issues)
