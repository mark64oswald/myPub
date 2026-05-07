"""Tests for skills_factory.py — Phase 5.4 orchestration + materialization.

Covers:
  * decompose_and_plan — Phase 5.1+5.2 chain
  * prep_package — manifest writing wrapper
  * process_package — re-export of skill_generation.process
  * materialize_package — write SKILL.md + _provenance.json + _package.md
  * run_full_package — convenience wrapper
  * _yaml_escape / _build_skill_md / _build_package_md — formatting
  * _resolve_package_id — lookup logic
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

import skills_factory as sf  # noqa: E402
import skill_generation as sg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


@pytest.fixture
def package_with_skills(catalog):
    """Insert a fully-populated skill_package + 2 skills + provenance.

    The factory's materialize_package walks these rows and writes
    SKILL.md / _provenance.json / _package.md to disk. We seed the
    database directly here so the materialization path can be tested
    independently of the prep/process pipeline.
    """
    pid = catalog.execute(
        "INSERT INTO skill_package (name, domain, root_topic, source_query) "
        "VALUES ('test-pkg', 'test domain', 'test', 'test query') RETURNING package_id"
    ).fetchone()[0]

    skill_a = catalog.execute(
        """
        INSERT INTO skill (package_id, name, description, scope_summary,
                            content_markdown, source_currency, strategy,
                            generation_notes)
        VALUES (?, 'Foundation Skill', 'When user asks about foundations',
                'anchor: Foundation', '# Foundation\n\nThe basics.',
                'consensus', 'consensus_synthesis',
                '3 distinct books contribute')
        RETURNING skill_id
        """, [pid],
    ).fetchone()[0]
    skill_b = catalog.execute(
        """
        INSERT INTO skill (package_id, name, description, scope_summary,
                            content_markdown, source_currency, strategy,
                            generation_notes)
        VALUES (?, 'Application Skill', 'When user asks about applications',
                'anchor: Application', '# Application\n\nReal usage.',
                'current', 'recent_doc_anchored',
                'doc_source matches')
        RETURNING skill_id
        """, [pid],
    ).fetchone()[0]

    # Provenance: 2 selected, 1 dropped per skill
    for sid in (skill_a, skill_b):
        catalog.execute(
            "INSERT INTO skill_source (skill_id, source_type, source_id, "
            "score, weight, drop_reason) VALUES (?, 'chapter', ?, 0.85, 1.0, NULL)",
            [sid, sid * 100 + 1],
        )
        catalog.execute(
            "INSERT INTO skill_source (skill_id, source_type, source_id, "
            "score, weight, drop_reason) VALUES (?, 'doc_section', ?, 0.78, 0.8, NULL)",
            [sid, sid * 100 + 2],
        )
        catalog.execute(
            "INSERT INTO skill_source (skill_id, source_type, source_id, "
            "score, weight, drop_reason) VALUES (?, 'chapter', ?, 0.30, 0.0, "
            "'single-source (no corroboration)')",
            [sid, sid * 100 + 3],
        )
    return {"package_id": pid, "skill_a": skill_a, "skill_b": skill_b}


# ---------------------------------------------------------------------------
# _yaml_escape + _build_skill_md
# ---------------------------------------------------------------------------


def test_yaml_escape_handles_quotes_and_backslashes():
    assert sf._yaml_escape('say "hello"') == 'say \\"hello\\"'
    assert sf._yaml_escape("a\\b") == "a\\\\b"
    assert sf._yaml_escape("") == ""
    assert sf._yaml_escape(None) == ""


def test_build_skill_md_has_frontmatter_and_body():
    out = sf._build_skill_md(
        name="My Skill", description="When user asks about X",
        body="# Content\n\nDetails here.",
    )
    assert out.startswith("---\n")
    assert 'name: "My Skill"' in out
    assert 'description: "When user asks about X"' in out
    assert "# Content\n\nDetails here.\n" in out
    # Frontmatter closes with --- on its own line
    parts = out.split("---", 2)
    assert len(parts) == 3   # opening, frontmatter content, body


def test_build_skill_md_collapses_description_whitespace():
    """Multi-line / whitespace descriptions collapse to a single line in YAML."""
    out = sf._build_skill_md(
        name="X", description="Line 1\n  Line 2\n\nLine 3", body="body",
    )
    # frontmatter description is collapsed
    assert 'description: "Line 1 Line 2 Line 3"' in out


def test_build_skill_md_handles_empty_description():
    out = sf._build_skill_md(name="X", description="", body="body")
    assert 'description: ""' in out


# ---------------------------------------------------------------------------
# _build_package_md
# ---------------------------------------------------------------------------


def test_build_package_md_has_skills_list():
    out = sf._build_package_md(
        package_name="pkg", domain="my domain",
        skills=[
            {"name": "A", "description": "trigger A"},
            {"name": "B", "description": "trigger B"},
        ],
    )
    assert "# pkg" in out
    assert "**Domain:** my domain" in out
    assert "2 Skill(s)" in out
    assert "- **A** — trigger A" in out
    assert "- **B** — trigger B" in out


def test_build_package_md_handles_empty_skills():
    out = sf._build_package_md(package_name="pkg", domain="d", skills=[])
    assert "0 Skill(s)" in out
    assert "## Skills" in out


# ---------------------------------------------------------------------------
# _resolve_package_id
# ---------------------------------------------------------------------------


def test_resolve_package_id_by_id(catalog, package_with_skills):
    pid = sf._resolve_package_id(catalog, package_with_skills["package_id"], None)
    assert pid == package_with_skills["package_id"]


def test_resolve_package_id_by_name(catalog, package_with_skills):
    pid = sf._resolve_package_id(catalog, None, "test-pkg")
    assert pid == package_with_skills["package_id"]


def test_resolve_package_id_neither_raises(catalog):
    with pytest.raises(ValueError, match="must provide"):
        sf._resolve_package_id(catalog, None, None)


def test_resolve_package_id_unknown_name_raises(catalog):
    with pytest.raises(ValueError, match="no skill_package"):
        sf._resolve_package_id(catalog, None, "nonexistent")


# ---------------------------------------------------------------------------
# materialize_package
# ---------------------------------------------------------------------------


def test_materialize_package_writes_skill_md_per_skill(catalog, package_with_skills, tmp_path):
    report = sf.materialize_package(
        catalog,
        package_id=package_with_skills["package_id"],
        output_root=str(tmp_path),
    )
    assert report.package_id == package_with_skills["package_id"]
    assert report.package_name == "test-pkg"
    assert len(report.skill_md_paths) == 2

    # Every SKILL.md exists and has frontmatter
    for path_str in report.skill_md_paths:
        path = Path(path_str)
        assert path.exists()
        content = path.read_text()
        assert content.startswith("---\n")
        assert 'name: "' in content


def test_materialize_package_writes_provenance(catalog, package_with_skills, tmp_path):
    report = sf.materialize_package(
        catalog,
        package_id=package_with_skills["package_id"],
        output_root=str(tmp_path),
    )
    assert len(report.provenance_paths) == 2
    for path_str in report.provenance_paths:
        path = Path(path_str)
        prov = json.loads(path.read_text())
        assert "skill_id" in prov
        assert "selected" in prov
        assert "dropped" in prov
        assert len(prov["selected"]) == 2  # we seeded 2 selected per skill
        assert len(prov["dropped"]) == 1   # 1 dropped per skill


def test_materialize_package_writes_package_md(catalog, package_with_skills, tmp_path):
    report = sf.materialize_package(
        catalog,
        package_name="test-pkg", output_root=str(tmp_path),
    )
    assert report.package_md_path
    pkg_md = Path(report.package_md_path).read_text()
    assert "test-pkg" in pkg_md
    assert "Foundation Skill" in pkg_md
    assert "Application Skill" in pkg_md


def test_materialize_package_overwrite_false_skips_existing(
    catalog, package_with_skills, tmp_path,
):
    """First call writes everything; second call with overwrite=False
    should write nothing new and report empty path lists for what was
    SKIPPED rather than re-writing."""
    sf.materialize_package(
        catalog,
        package_id=package_with_skills["package_id"],
        output_root=str(tmp_path),
    )
    second = sf.materialize_package(
        catalog,
        package_id=package_with_skills["package_id"],
        output_root=str(tmp_path),
        overwrite=False,
    )
    # All paths report empty because the existing files were skipped
    assert second.skill_md_paths == []
    assert second.provenance_paths == []
    assert second.package_md_path is None


def test_materialize_package_unknown_package_raises(catalog, tmp_path):
    with pytest.raises(ValueError):
        sf.materialize_package(
            catalog, package_id=99999, output_root=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# decompose_and_plan + prep_package + run_full_package
# ---------------------------------------------------------------------------
# These wrap thoroughly-tested code from decomposition / package_planning /
# skill_generation. We verify the wiring (calls are made, types flow) rather
# than re-testing every edge case.


@pytest.fixture
def planning_seed(catalog):
    """Minimal seed for decompose_and_plan: one concept + a chapter."""
    catalog.execute("INSERT INTO author (name) VALUES ('A1')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('B', '/x', 'O''Reilly Media') RETURNING book_id"
    ).fetchone()[0]
    chap = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'Intro', 'x') RETURNING chapter_id", [book_id]
    ).fetchone()[0]
    cid = catalog.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Foo', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]
    other = catalog.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Bar', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, confidence, source_type, source_id) "
        "VALUES (?, ?, 'CITES', 0.9, 'chapter', ?)",
        [int(cid), int(other), chap],
    )
    return {"foo_id": int(cid), "bar_id": int(other)}


def test_decompose_and_plan_returns_package_plan(catalog, planning_seed):
    resolver = MagicMock()
    resolver.resolve_lookup_only = lambda name: (
        planning_seed["foo_id"] if name.lower() == "foo" else None
    )
    plan = sf.decompose_and_plan(
        catalog, resolver, "Foo", min_cluster_size=1,
    )
    assert plan.domain == "Foo"
    assert plan.package_name == "foo"  # slugified
    assert plan.planned_skills  # at least one cluster


def test_prep_package_writes_manifest(catalog, planning_seed, tmp_path):
    resolver = MagicMock()
    resolver.resolve_lookup_only = lambda name: (
        planning_seed["foo_id"] if name.lower() == "foo" else None
    )
    plan = sf.decompose_and_plan(
        catalog, resolver, "Foo", min_cluster_size=1,
    )
    fake_search = MagicMock(return_value={"results": [], "dropped": []})
    report = sf.prep_package(plan, catalog, tmp_path, search_fn=fake_search)
    assert report.package_name == plan.package_name
    assert report.n_skills == len(plan.planned_skills)
    # manifest.json was written
    assert (tmp_path / "manifest.json").exists()


def test_run_full_package_chains_decompose_plan_prep(
    catalog, planning_seed, tmp_path,
):
    resolver = MagicMock()
    resolver.resolve_lookup_only = lambda name: (
        planning_seed["foo_id"] if name.lower() == "foo" else None
    )
    fake_search = MagicMock(return_value={"results": [], "dropped": []})
    plan, prep = sf.run_full_package(
        catalog, resolver, "Foo",
        search_fn=fake_search,
        output_dir=tmp_path, min_cluster_size=1,
    )
    assert plan.planned_skills
    assert prep.n_skills == len(plan.planned_skills)
    assert (tmp_path / "manifest.json").exists()


def test_run_full_package_default_output_dir(
    catalog, planning_seed, tmp_path, monkeypatch,
):
    """Default output_dir is <prompt_root>/<package_name>."""
    resolver = MagicMock()
    resolver.resolve_lookup_only = lambda name: (
        planning_seed["foo_id"] if name.lower() == "foo" else None
    )
    fake_search = MagicMock(return_value={"results": [], "dropped": []})
    monkeypatch.chdir(tmp_path)  # so default relative path lands in tmp_path
    plan, prep = sf.run_full_package(
        catalog, resolver, "Foo",
        search_fn=fake_search,
        prompt_root="prompt-runs",
        min_cluster_size=1,
    )
    expected = tmp_path / "prompt-runs" / plan.package_name / "manifest.json"
    assert expected.exists()


# ---------------------------------------------------------------------------
# process_package — verifies the re-export wires up correctly
# ---------------------------------------------------------------------------


def test_process_package_is_skill_generation_process(catalog, tmp_path, monkeypatch):
    """process_package should delegate to skill_generation.process_skill_generation."""
    called = {}

    def fake_process(conn, output_dir):
        called["conn"] = conn
        called["output_dir"] = output_dir
        return sg.SkillIngestSummary(total=0, processed=0)

    monkeypatch.setattr(sg, "process_skill_generation", fake_process)
    sf.process_package(catalog, tmp_path)
    assert called["conn"] is catalog
    assert called["output_dir"] == tmp_path
