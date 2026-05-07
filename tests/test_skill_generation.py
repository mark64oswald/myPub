"""Tests for skill_generation.py — Phase 5.3 Skills Factory: per-Skill generation.

Covers:
  * build_retrieval_query — anchor + supplementary concepts
  * build_sibling_summaries — sibling list construction
  * _format_source_excerpts / _format_sibling_summary — prompt fragments
  * build_skill_prompt — full prompt assembly
  * _scored_to_source_record / _validate_skill_payload — data normalization
  * prep_skill_generation — manifest + prompt files emitted per Skill
  * process_skill_generation — JSON ingestion + idempotent re-run +
    full §8.6 provenance (selected and dropped both written)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import skill_generation as sg  # noqa: E402
from decomposition import ProposedSkill  # noqa: E402
from package_planning import PackagePlan, PlannedSkill  # noqa: E402


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
def populated(catalog):
    """Seed: one book + two chapters + one doc_section, plus 4 concepts and
    a few concept_relation rows so build_retrieval_query has data."""
    catalog.execute("INSERT INTO author (name) VALUES ('A1')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('Book1', '/x', 'O''Reilly Media') RETURNING book_id"
    ).fetchone()[0]
    chap_id = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'Intro', 'A long discussion of foundations and theorems.') "
        "RETURNING chapter_id",
        [book_id],
    ).fetchone()[0]
    chap_dropped_id = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 2, 'Dropped Chapter', 'Tangentially related content.') "
        "RETURNING chapter_id",
        [book_id],
    ).fetchone()[0]
    src_id = catalog.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
        "authority_score, refresh_ttl_days) "
        "VALUES ('TestDoc', 'context7', 'context7', '/t/x', 0.85, 30) "
        "RETURNING doc_source_id"
    ).fetchone()[0]
    snap_id = catalog.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content) "
        "VALUES (?, 'context7', 'http://x', 'abc', 'snap content') "
        "RETURNING snapshot_id",
        [src_id],
    ).fetchone()[0]
    section_id = catalog.execute(
        "INSERT INTO doc_section (snapshot_id, ordinal, heading_text, content) "
        "VALUES (?, 0, 'Reference', 'Reference content with deep details.') "
        "RETURNING doc_section_id",
        [snap_id],
    ).fetchone()[0]
    cmap = {}
    for n in ["Foundation", "Theorem", "Proof", "Application"]:
        cid = catalog.execute(
            "INSERT INTO concept (name, concept_type) VALUES (?, 'Concept') RETURNING concept_id",
            [n],
        ).fetchone()[0]
        cmap[n] = int(cid)
    # Edges so mention counts > 0
    for src, dst in [("Foundation", "Theorem"), ("Theorem", "Proof"),
                      ("Foundation", "Proof"), ("Foundation", "Application")]:
        catalog.execute(
            "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
            "relation_type, confidence, source_type, source_id) "
            "VALUES (?, ?, 'CITES', 0.9, 'chapter', ?)",
            [cmap[src], cmap[dst], chap_id],
        )
    return {"cmap": cmap, "chap_id": chap_id,
            "chap_dropped_id": chap_dropped_id, "section_id": section_id}


def _make_plan(populated, *, package_name="testpkg", domain="test domain"):
    """Build a PackagePlan with two PlannedSkills for testing."""
    cmap = populated["cmap"]
    sk_a = PlannedSkill(
        proposed=ProposedSkill(
            cluster_id=0,
            concept_ids=[cmap["Foundation"], cmap["Theorem"], cmap["Proof"]],
            anchor_concept_id=cmap["Foundation"],
            anchor_concept_name="Foundation",
            suggested_name="Foundation",
        ),
        order=0,
        strategy="consensus_synthesis",
        strategy_rationale="3 distinct books contribute",
        folder_name="foundation",
    )
    sk_b = PlannedSkill(
        proposed=ProposedSkill(
            cluster_id=1,
            concept_ids=[cmap["Application"]],
            anchor_concept_id=cmap["Application"],
            anchor_concept_name="Application",
            suggested_name="Application",
        ),
        order=1,
        strategy="recent_doc_anchored",
        strategy_rationale="anchor matches doc_source 'TestDoc'",
        folder_name="application",
        requires_cluster_ids=[0],
        references_cluster_ids=[0],
    )
    return PackagePlan(
        package_name=package_name,
        domain=domain,
        folder_root=f"data/generated-packages/{package_name}",
        planned_skills=[sk_a, sk_b],
    )


def _stub_search_fn(populated, *, results=None, dropped=None):
    """Build a mock search_chapters that returns canned generation-mode results."""
    chap_id = populated["chap_id"]
    section_id = populated["section_id"]
    if results is None:
        results = [
            {"kind": "chapter", "result_id": chap_id, "combined_score": 0.81},
            {"kind": "doc_section", "result_id": section_id, "combined_score": 0.74},
        ]
    if dropped is None:
        dropped = [
            {"kind": "chapter", "result_id": populated["chap_dropped_id"],
             "combined_score": 0.30,
             "drop_reason": "single-source (no corroboration)"},
        ]

    def fn(**kwargs):
        return {
            "query": kwargs.get("query"),
            "mode": "generation",
            "selection_strategy": kwargs.get("selection_strategy"),
            "results": list(results),
            "dropped": list(dropped),
        }
    return fn


# ---------------------------------------------------------------------------
# build_retrieval_query
# ---------------------------------------------------------------------------


def test_build_retrieval_query_includes_anchor_and_supplementary(catalog, populated):
    cmap = populated["cmap"]
    out = sg.build_retrieval_query(
        catalog, "Foundation",
        [cmap["Foundation"], cmap["Theorem"], cmap["Proof"], cmap["Application"]],
        max_supplementary=2,
    )
    parts = out.split()
    assert parts[0] == "Foundation"
    assert len(parts) >= 2  # anchor + at least 1 supplementary


def test_build_retrieval_query_no_concepts_returns_anchor(catalog):
    out = sg.build_retrieval_query(catalog, "Solo", [])
    assert out == "Solo"


def test_build_retrieval_query_anchor_none_uses_concepts(catalog, populated):
    cmap = populated["cmap"]
    out = sg.build_retrieval_query(catalog, None, [cmap["Foundation"]])
    assert "Foundation" in out


def test_build_retrieval_query_empty_input(catalog):
    assert sg.build_retrieval_query(catalog, None, []) == ""


# ---------------------------------------------------------------------------
# build_sibling_summaries
# ---------------------------------------------------------------------------


def test_build_sibling_summaries_compact_shape(populated):
    plan = _make_plan(populated)
    siblings = sg.build_sibling_summaries(plan)
    assert len(siblings) == 2
    for s in siblings:
        assert "cluster_id" in s
        assert "name" in s
        assert "anchor" in s
        assert "strategy" in s
        assert "folder_name" in s


# ---------------------------------------------------------------------------
# Prompt fragments + _scored_to_source_record + _validate_skill_payload
# ---------------------------------------------------------------------------


def test_format_source_excerpts_renders_chapter_and_doc_section(catalog, populated):
    sources = [
        sg.SkillSourceRecord(
            source_type="chapter", source_id=populated["chap_id"], score=0.8,
        ),
        sg.SkillSourceRecord(
            source_type="doc_section", source_id=populated["section_id"], score=0.7,
        ),
    ]
    out = sg._format_source_excerpts(sources, conn=catalog, excerpt_chars=200)
    assert "source 1" in out
    assert "source 2" in out
    assert "BOOK:" in out
    assert "DOC SOURCE:" in out


def test_format_source_excerpts_empty_input(catalog):
    out = sg._format_source_excerpts([], conn=catalog)
    assert "no source material" in out


def test_format_sibling_summary_excludes_self():
    siblings = [
        {"cluster_id": 0, "name": "A", "anchor": "A", "strategy": "consensus_synthesis", "folder_name": "a"},
        {"cluster_id": 1, "name": "B", "anchor": "B", "strategy": "recent_doc_anchored", "folder_name": "b"},
        {"cluster_id": 2, "name": "C", "anchor": "C", "strategy": "authority_pick", "folder_name": "c"},
    ]
    out = sg._format_sibling_summary(siblings, current_cluster_id=1)
    assert "a" in out
    assert "c" in out
    # Skill B's own folder name shouldn't appear
    assert "folder_name" not in out  # we don't print the literal field name
    # Listing line for B should be excluded
    lines = out.split("\n")
    assert not any("anchor: 'B'" in line for line in lines)


def test_format_sibling_summary_lone_skill():
    out = sg._format_sibling_summary([{"cluster_id": 0, "name": "A",
                                        "anchor": "A", "strategy": "x",
                                        "folder_name": "a"}],
                                       current_cluster_id=0)
    assert "only Skill" in out


def test_scored_to_source_record_handles_chapter_and_doc_section():
    rec = sg._scored_to_source_record(
        {"kind": "chapter", "result_id": 5, "combined_score": 0.7},
    )
    assert rec is not None and rec.source_type == "chapter"
    assert rec.source_id == 5

    rec2 = sg._scored_to_source_record(
        {"kind": "doc_section", "result_id": 9, "combined_score": 0.6},
        drop_reason="contradicted",
    )
    assert rec2 is not None and rec2.drop_reason == "contradicted"


def test_scored_to_source_record_skips_unknown_kind():
    assert sg._scored_to_source_record({"kind": "weird", "result_id": 1}) is None
    assert sg._scored_to_source_record({"result_id": 1}) is None
    assert sg._scored_to_source_record({"kind": "chapter"}) is None  # missing result_id


def test_validate_skill_payload_accepts_well_formed():
    out = sg._validate_skill_payload(
        {"trigger_description": "When user asks X", "skill_md": "# Body"}
    )
    assert out == {"trigger_description": "When user asks X", "skill_md": "# Body"}


def test_validate_skill_payload_rejects_missing_fields():
    assert sg._validate_skill_payload({}) is None
    assert sg._validate_skill_payload({"trigger_description": "x"}) is None
    assert sg._validate_skill_payload(
        {"trigger_description": "  ", "skill_md": "x"}
    ) is None
    assert sg._validate_skill_payload("not a dict") is None


# ---------------------------------------------------------------------------
# _strip_json_fences + _parse_skill_result — robust to model formatting
# ---------------------------------------------------------------------------


def test_strip_json_fences_handles_lowercase_fence():
    src = '```json\n{"k": 1}\n```'
    assert sg._strip_json_fences(src) == '{"k": 1}'


def test_strip_json_fences_handles_uppercase_fence():
    src = '```JSON\n{"k": 1}\n```'
    assert sg._strip_json_fences(src) == '{"k": 1}'


def test_strip_json_fences_handles_unlabeled_fence():
    src = '```\n{"k": 1}\n```'
    assert sg._strip_json_fences(src) == '{"k": 1}'


def test_strip_json_fences_passthrough_when_no_fence():
    src = '{"k": 1}'
    assert sg._strip_json_fences(src) == '{"k": 1}'


def test_strip_json_fences_keeps_inner_fences():
    """A skill_md body legitimately contains a fenced code sample; we
    only strip the outermost fence, not nested ones inside string values."""
    src = '```json\n{"skill_md": "```python\\nx = 1\\n```"}\n```'
    out = sg._strip_json_fences(src)
    # The outer fence is gone; the inner ```python ... ``` survives
    # inside the JSON string.
    assert out.startswith("{") and out.endswith("}")
    assert "```python" in out


def test_parse_skill_result_clean_json():
    out = sg._parse_skill_result('{"trigger_description": "x", "skill_md": "y"}')
    assert out == {"trigger_description": "x", "skill_md": "y"}


def test_parse_skill_result_fenced_json():
    out = sg._parse_skill_result('```json\n{"trigger_description": "x", "skill_md": "y"}\n```')
    assert out == {"trigger_description": "x", "skill_md": "y"}


def test_parse_skill_result_with_leading_prose():
    """Some sub-agents prepend a sentence before the JSON despite
    instructions; the parser should still extract the object."""
    src = (
        'Here is the JSON for this skill:\n'
        '{"trigger_description": "x", "skill_md": "y"}'
    )
    out = sg._parse_skill_result(src)
    assert out == {"trigger_description": "x", "skill_md": "y"}


def test_parse_skill_result_unrecoverable_raises():
    import json as _json
    with pytest.raises(_json.JSONDecodeError):
        sg._parse_skill_result("this is not json at all and has no braces")


# ---------------------------------------------------------------------------
# build_skill_prompt
# ---------------------------------------------------------------------------


def test_build_skill_prompt_includes_all_sections(catalog, populated):
    cmap = populated["cmap"]
    entry = sg.SkillManifestEntry(
        cluster_id=0,
        skill_name="Foundation",
        anchor_concept_id=cmap["Foundation"],
        anchor_concept_name="Foundation",
        concept_ids=[cmap["Foundation"]],
        strategy="consensus_synthesis",
        strategy_rationale="3 distinct books",
        folder_name="foundation",
        selected_sources=[
            sg.SkillSourceRecord(
                source_type="chapter", source_id=populated["chap_id"], score=0.8,
            ),
        ],
    )
    siblings = [
        {"cluster_id": 0, "name": "Foundation", "anchor": "Foundation",
         "strategy": "consensus_synthesis", "folder_name": "foundation"},
        {"cluster_id": 1, "name": "Application", "anchor": "Application",
         "strategy": "recent_doc_anchored", "folder_name": "application"},
    ]
    prompt = sg.build_skill_prompt(
        package_name="pkg", domain="domain",
        entry=entry, siblings=siblings, conn=catalog, excerpt_chars=200,
    )
    assert "PACKAGE: pkg" in prompt
    assert "DOMAIN: domain" in prompt
    assert "ANCHOR CONCEPT: Foundation" in prompt
    assert "STRATEGY: consensus_synthesis" in prompt
    assert "SIBLING SKILLS" in prompt
    assert "application" in prompt   # the sibling
    assert "BOOK:" in prompt          # the source excerpt is included
    assert "trigger_description" in prompt   # output schema present


# ---------------------------------------------------------------------------
# prep_skill_generation
# ---------------------------------------------------------------------------


def test_prep_skill_generation_writes_manifest_and_prompts(catalog, populated, tmp_path):
    plan = _make_plan(populated)
    search_fn = _stub_search_fn(populated)
    manifest = sg.prep_skill_generation(
        plan, catalog, tmp_path, search_fn=search_fn, retrieval_limit=5,
    )
    assert manifest.package_name == "testpkg"
    assert len(manifest.skills) == 2
    # Each entry has the expected on-disk artifacts
    for entry in manifest.skills:
        assert Path(entry.prompt_path).exists()
        # result file is NOT created at prep time
        assert not Path(entry.result_path).exists()
        assert entry.selected_sources  # stub search fn returned 2
        # Paths must be absolute so dispatched sub-agents (which may
        # have a different CWD) can reach them unambiguously.
        assert Path(entry.prompt_path).is_absolute()
        assert Path(entry.result_path).is_absolute()
    # Manifest JSON is round-trippable
    on_disk = json.loads((tmp_path / "manifest.json").read_text())
    assert on_disk["package_name"] == "testpkg"
    assert len(on_disk["skills"]) == 2


def test_prep_skill_generation_resolves_relative_output_dir(
    catalog, populated, tmp_path, monkeypatch,
):
    """A relative output_dir should be resolved against CWD so the
    manifest carries absolute paths regardless of how it was called."""
    monkeypatch.chdir(tmp_path)
    plan = _make_plan(populated)
    search_fn = _stub_search_fn(populated)
    manifest = sg.prep_skill_generation(
        plan, catalog, Path("relative/dir"),
        search_fn=search_fn, retrieval_limit=2,
    )
    for entry in manifest.skills:
        assert Path(entry.prompt_path).is_absolute()
        assert Path(entry.result_path).is_absolute()
        assert str(tmp_path.resolve()) in entry.prompt_path


def test_prep_skill_generation_strategy_to_profile_mapping(catalog, populated, tmp_path):
    """Each strategy should request the corresponding skill_* weight profile."""
    plan = _make_plan(populated)
    captured: list[dict] = []

    def fn(**kwargs):
        captured.append(kwargs)
        return {"results": [], "dropped": []}

    sg.prep_skill_generation(plan, catalog, tmp_path, search_fn=fn)
    profiles = [c["weight_profile"] for c in captured]
    # Skill 0 is consensus_synthesis → skill_consensus
    # Skill 1 is recent_doc_anchored → skill_recent_doc
    assert "skill_consensus" in profiles
    assert "skill_recent_doc" in profiles


def test_prep_skill_generation_handles_search_failure(catalog, populated, tmp_path):
    """If search_fn raises, the skill still ends up in the manifest with an
    empty source list — it just gets a sub-agent prompt with no excerpts."""
    plan = _make_plan(populated)

    def failing_fn(**kwargs):
        raise RuntimeError("simulated retrieval failure")

    manifest = sg.prep_skill_generation(
        plan, catalog, tmp_path, search_fn=failing_fn,
    )
    assert len(manifest.skills) == 2
    for entry in manifest.skills:
        assert entry.selected_sources == []
        assert entry.dropped_sources == []
        assert Path(entry.prompt_path).exists()


# ---------------------------------------------------------------------------
# process_skill_generation
# ---------------------------------------------------------------------------


def _write_prep_then_results(catalog, populated, tmp_path, *, results_for: dict):
    """Helper: prep the manifest, then write canned result JSONs.

    ``results_for`` is ``{cluster_id: {trigger_description, skill_md} | None}``.
    None means "don't write the result file" (simulates missing).
    """
    plan = _make_plan(populated)
    search_fn = _stub_search_fn(populated)
    manifest = sg.prep_skill_generation(
        plan, catalog, tmp_path, search_fn=search_fn, retrieval_limit=5,
    )
    for entry in manifest.skills:
        payload = results_for.get(entry.cluster_id)
        if payload is None:
            continue
        Path(entry.result_path).write_text(json.dumps(payload))
    return manifest


def test_process_skill_generation_happy_path(catalog, populated, tmp_path):
    """Both skills have valid result JSON → both are inserted with provenance."""
    _write_prep_then_results(catalog, populated, tmp_path, results_for={
        0: {"trigger_description": "When user asks about Foundation",
            "skill_md": "# Foundation\nBody."},
        1: {"trigger_description": "When user asks about Application",
            "skill_md": "# Application\nBody."},
    })
    summary = sg.process_skill_generation(catalog, tmp_path)
    assert summary.processed == 2
    assert summary.missing == 0
    assert summary.unparseable == 0
    assert summary.package_id is not None
    assert len(summary.skill_ids) == 2

    # Skill rows persisted
    rows = catalog.execute(
        "SELECT name, description, content_markdown, strategy, source_currency "
        "FROM skill WHERE package_id = ? ORDER BY name", [summary.package_id],
    ).fetchall()
    assert len(rows) == 2
    by_name = {r[0]: r for r in rows}
    assert "When user asks about Foundation" in by_name["Foundation"][1]
    assert by_name["Application"][3] == "recent_doc_anchored"
    assert by_name["Application"][4] == "current"   # currency mapping
    assert by_name["Foundation"][4] == "consensus"

    # Provenance: every selected + dropped source got a skill_source row
    src_rows = catalog.execute(
        "SELECT skill_id, source_type, drop_reason FROM skill_source "
        "WHERE skill_id IN (SELECT skill_id FROM skill WHERE package_id = ?)",
        [summary.package_id],
    ).fetchall()
    selected_count = sum(1 for r in src_rows if r[2] is None)
    dropped_count = sum(1 for r in src_rows if r[2] is not None)
    assert selected_count >= 2   # 2 selected per skill (chap + doc) × 2 skills
    assert dropped_count >= 1    # 1 dropped per skill × 2 skills

    # Skill relations: REQUIRES + REFERENCES from cluster 1 → cluster 0
    rel_rows = catalog.execute(
        "SELECT relation_type FROM skill_relation",
    ).fetchall()
    rel_types = {r[0] for r in rel_rows}
    assert "REQUIRES" in rel_types
    assert "REFERENCES" in rel_types


def test_process_skill_generation_missing_result_counts_as_missing(catalog, populated, tmp_path):
    _write_prep_then_results(catalog, populated, tmp_path, results_for={
        0: {"trigger_description": "Trigger", "skill_md": "Body"},
        # cluster 1 result absent
    })
    summary = sg.process_skill_generation(catalog, tmp_path)
    assert summary.processed == 1
    assert summary.missing == 1
    # Only skill 0 was inserted; no skill_relation rows since the
    # REQUIRES target (cluster 1) failed to ingest
    rel_rows = catalog.execute("SELECT * FROM skill_relation").fetchall()
    assert rel_rows == []


def test_process_skill_generation_unparseable_result_counts(catalog, populated, tmp_path):
    """Result file with bad JSON → counted unparseable, not crashing."""
    plan = _make_plan(populated)
    search_fn = _stub_search_fn(populated)
    manifest = sg.prep_skill_generation(
        plan, catalog, tmp_path, search_fn=search_fn, retrieval_limit=5,
    )
    # First skill: garbage JSON
    Path(manifest.skills[0].result_path).write_text("{not valid")
    # Second skill: missing required fields
    Path(manifest.skills[1].result_path).write_text(json.dumps({"foo": "bar"}))

    summary = sg.process_skill_generation(catalog, tmp_path)
    assert summary.processed == 0
    assert summary.unparseable == 2


def test_process_skill_generation_idempotent_on_repeat(catalog, populated, tmp_path):
    """Re-running process clears prior package skills + re-ingests fresh.
    Ensures idempotent runs don't double-up rows or violate constraints."""
    _write_prep_then_results(catalog, populated, tmp_path, results_for={
        0: {"trigger_description": "T0", "skill_md": "Body0"},
        1: {"trigger_description": "T1", "skill_md": "Body1"},
    })
    s1 = sg.process_skill_generation(catalog, tmp_path)
    s2 = sg.process_skill_generation(catalog, tmp_path)
    assert s1.processed == 2
    assert s2.processed == 2
    # Same package id should be reused (UNIQUE on name)
    assert s1.package_id == s2.package_id
    # Each run produces 2 skills — the second run cleared the first's, so
    # final count is 2, not 4
    count = catalog.execute(
        "SELECT COUNT(*) FROM skill WHERE package_id = ?", [s2.package_id],
    ).fetchone()[0]
    assert count == 2
