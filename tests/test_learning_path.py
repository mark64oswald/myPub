"""Tests for learning_path.py — Phase 8 Learning Path generator (v1).

Uses a tiny seeded prerequisite chain:

    SQL → Database Design → Schema Modeling → Data Pipeline → CDC

with REQUIRES edges between adjacent concepts. Two book chapters
provide coverage for different stage subsets.
"""
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

import learning_path as lp  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Concept", description=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES (?, ?, ?) RETURNING concept_id",
        [name, concept_type, description],
    ).fetchone()[0]


_REL_COUNTER = [0]


def _add_rel(conn, src, tgt, rel_type, source_id=None):
    _REL_COUNTER[0] += 1
    sid = source_id if source_id is not None else _REL_COUNTER[0]
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, sid],
    )


def _seed_book_chapter(conn, book_title, chapter_title, chapter_num=1):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) "
        "VALUES (?, ?) RETURNING book_id",
        [book_title, f"/tmp/{book_title.replace(' ', '_')}.epub"],
    ).fetchone()[0]
    cid = conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, ?, ?) RETURNING chapter_id",
        [bid, chapter_title, chapter_num],
    ).fetchone()[0]
    return bid, cid


@pytest.fixture
def chain_graph(catalog):
    """Linear chain SQL → DB Design → Schema → Pipeline → CDC."""
    sql = _seed_concept(catalog, "SQL", description="basic SQL")
    db = _seed_concept(catalog, "Database Design")
    schema = _seed_concept(catalog, "Schema Modeling")
    pipe = _seed_concept(catalog, "Data Pipeline")
    cdc = _seed_concept(catalog, "Change Data Capture")
    # REQUIRES chain: cdc → pipe → schema → db → sql
    # i.e. CDC requires Pipeline, Pipeline requires Schema, etc.
    _add_rel(catalog, cdc, pipe, "REQUIRES")
    _add_rel(catalog, pipe, schema, "REQUIRES")
    _add_rel(catalog, schema, db, "REQUIRES")
    _add_rel(catalog, db, sql, "REQUIRES")

    # Two book chapters covering different parts of the chain.
    # source_ids in concept_relation MUST point at real chapter_ids
    # for the count joins (defensive against orphans) to find them.
    _, ch_db = _seed_book_chapter(catalog, "DB Fundamentals", "Schemas and Models", 1)
    _, ch_pipe = _seed_book_chapter(catalog, "Streaming Systems", "Pipelines & CDC", 2)
    # Chapter ch_db covers SQL + DB Design + Schema Modeling
    _add_rel(catalog, sql, db, "CITES", source_id=ch_db)
    _add_rel(catalog, db, schema, "CITES", source_id=ch_db)
    # Chapter ch_pipe covers Pipeline + CDC
    _add_rel(catalog, pipe, cdc, "CITES", source_id=ch_pipe)

    return {
        "sql": sql, "db": db, "schema": schema, "pipe": pipe, "cdc": cdc,
        "ch_db": ch_db, "ch_pipe": ch_pipe,
    }


def _resolver_for(name_to_id: dict[str, int]):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


# ---------------------------------------------------------------------------
# PrerequisiteDecomposer
# ---------------------------------------------------------------------------


def test_decomposer_resolves_target_at_depth_zero(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    assert d.target_concept_id == chain_graph["cdc"]
    target_node = next(c for c in d.concepts if c.concept_id == chain_graph["cdc"])
    assert target_node.depth == 0


def test_decomposer_walks_full_prereq_chain(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    ids = {c.concept_id for c in d.concepts}
    # All five concepts in the chain should be present
    assert ids == set(v for k, v in chain_graph.items()
                       if k in {"sql", "db", "schema", "pipe", "cdc"})


def test_decomposer_orders_deepest_first(catalog, chain_graph):
    """SQL is at depth 4 from CDC; it should appear in d.concepts before CDC."""
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    sql_idx = next(i for i, c in enumerate(d.concepts) if c.concept_id == chain_graph["sql"])
    cdc_idx = next(i for i, c in enumerate(d.concepts) if c.concept_id == chain_graph["cdc"])
    assert sql_idx < cdc_idx


def test_decomposer_max_depth_clips_chain(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(
        catalog, resolver, "Change Data Capture", max_depth=2,
    )
    ids = {c.concept_id for c in d.concepts}
    # depth 0..2 = cdc, pipe, schema. db (depth 3) and sql (depth 4) excluded.
    assert chain_graph["sql"] not in ids
    assert chain_graph["db"] not in ids
    assert chain_graph["schema"] in ids


def test_decomposer_start_clips_at_known_concept(catalog, chain_graph):
    """If user already knows DB Design, the path should not include SQL
    (which is a prerequisite of DB Design)."""
    resolver = _resolver_for({
        "change data capture": chain_graph["cdc"],
        "database design": chain_graph["db"],
    })
    d = lp.PrerequisiteDecomposer().decompose(
        catalog, resolver, "Change Data Capture", start="Database Design",
    )
    ids = {c.concept_id for c in d.concepts}
    # SQL is at depth 4; DB Design at depth 3. Concepts at depths > 3
    # are clipped, so SQL is excluded.
    assert chain_graph["sql"] not in ids
    assert chain_graph["db"] in ids
    assert chain_graph["cdc"] in ids


def test_decomposer_unknown_target_returns_empty(catalog, chain_graph):
    resolver = _resolver_for({})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Nonexistent")
    assert d.target_concept_id == -1
    assert d.concepts == []
    assert any("not found" in n for n in d.notes)


def test_decomposer_max_concepts_caps_dense_path(catalog):
    """A target with dozens of prereqs should get pruned to max_concepts,
    keeping the target itself and the highest-chapter-coverage prereqs."""
    target = _seed_concept(catalog, "Dense Target")
    bid, ch = _seed_book_chapter(catalog, "B", "ch1", 1)
    # Create 50 direct prereqs of target. Half with chapter coverage,
    # half without.
    well_covered = []
    no_coverage = []
    for i in range(25):
        c = _seed_concept(catalog, f"Covered{i}")
        well_covered.append(c)
        _add_rel(catalog, target, c, "REQUIRES")
        # Citation from real chapter -> contributes to chapter_count
        _add_rel(catalog, c, target, "CITES", source_id=ch)
    for i in range(25):
        c = _seed_concept(catalog, f"Bare{i}")
        no_coverage.append(c)
        _add_rel(catalog, target, c, "REQUIRES")

    resolver = _resolver_for({"dense target": target})
    d = lp.PrerequisiteDecomposer().decompose(
        catalog, resolver, "Dense Target", max_concepts=10,
    )
    assert len(d.concepts) == 10
    assert any("pruned" in n for n in d.notes)
    # Target itself preserved
    assert any(c.concept_id == target for c in d.concepts)
    # Bare-coverage prereqs should be pruned in favor of well-covered ones
    bare_kept = sum(1 for c in d.concepts if c.concept_id in no_coverage)
    covered_kept = sum(1 for c in d.concepts if c.concept_id in well_covered)
    assert covered_kept > bare_kept


def test_decomposer_unknown_start_emits_note(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(
        catalog, resolver, "Change Data Capture", start="UnknownStart",
    )
    assert d.target_concept_id == chain_graph["cdc"]
    assert d.start_concept_id is None
    assert any("start concept" in n.lower() and "not found" in n.lower() for n in d.notes)


# ---------------------------------------------------------------------------
# Stage grouping
# ---------------------------------------------------------------------------


def test_stage_grouping_buckets_by_depth(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    stages = lp.group_into_stages(d.concepts)
    # 5 concepts at 5 depths → tiny-bucket merging will collapse some.
    assert 1 <= len(stages) <= 5
    # First stage should be the deepest (SQL at depth 4)
    assert any(c.concept_id == chain_graph["sql"] for c in stages[0].concepts)


def test_stage_grouping_merges_tiny_buckets(catalog):
    """Five depths with 1 concept each should not produce 5 single-concept
    stages — they should merge."""
    concepts = [
        lp._PathConcept(
            concept_id=i, name=f"c{i}", concept_type="Concept",
            description=None, depth=4 - i,
            chapter_count=0, doc_section_count=0,
        )
        for i in range(5)
    ]
    stages = lp.group_into_stages(concepts, target_size=5)
    # No stage should have fewer than 3 concepts (the merge threshold)
    # except possibly the last if it picked up trailing pending.
    sizes = [len(s.concepts) for s in stages]
    assert max(sizes) >= 3
    assert sum(sizes) == 5


def test_stage_grouping_splits_huge_bucket(catalog):
    """A single depth with 15 concepts should split into ~3 stages of 5."""
    concepts = [
        lp._PathConcept(
            concept_id=i, name=f"c{i}", concept_type="Concept",
            description=None, depth=2,
            chapter_count=i, doc_section_count=0,
        )
        for i in range(15)
    ]
    stages = lp.group_into_stages(concepts, target_size=5)
    sizes = [len(s.concepts) for s in stages]
    assert all(s <= 7 for s in sizes), f"oversized stage: {sizes}"
    assert sum(sizes) == 15


def test_stage_grouping_anchor_picked_by_chapter_count(catalog):
    """Within a stage, the anchor (stage name source) is the
    highest-chapter-count concept."""
    concepts = [
        lp._PathConcept(concept_id=1, name="Low", concept_type=None,
                        description=None, depth=0, chapter_count=2,
                        doc_section_count=0),
        lp._PathConcept(concept_id=2, name="High Authority", concept_type=None,
                        description=None, depth=0, chapter_count=20,
                        doc_section_count=0),
        lp._PathConcept(concept_id=3, name="Mid", concept_type=None,
                        description=None, depth=0, chapter_count=5,
                        doc_section_count=0),
    ]
    stages = lp.group_into_stages(concepts)
    assert stages[0].name == "High Authority"


# ---------------------------------------------------------------------------
# Reading list (chapter assignment)
# ---------------------------------------------------------------------------


def test_assign_chapters_picks_chapters_covering_stage_concepts(catalog, chain_graph):
    """Stage covering DB Design + Schema should pick the DB Fundamentals
    chapter (which covers both)."""
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    # Manually pick a stage with DB Design + Schema
    db_concept = next(c for c in d.concepts if c.concept_id == chain_graph["db"])
    schema_concept = next(c for c in d.concepts if c.concept_id == chain_graph["schema"])
    stage = lp._Stage(ordinal=1, name="DB", slug="db", concepts=[db_concept, schema_concept])
    chapters = lp.assign_chapters_to_stage(catalog, stage)
    chap_ids = {ch.chapter_id for ch in chapters}
    assert chain_graph["ch_db"] in chap_ids


def test_assign_chapters_empty_stage(catalog):
    stage = lp._Stage(ordinal=1, name="x", slug="x", concepts=[])
    chapters = lp.assign_chapters_to_stage(catalog, stage)
    assert chapters == []


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_stage_reading_list_lists_concepts_and_chapters(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    plan = lp.LearningPathPlanner().plan(catalog, d)
    # Find stages with chapters and inspect the file body.
    stage_files = [f for f in plan.files if f.purpose == "reading_list"]
    assert stage_files
    body = stage_files[0].content
    assert body.startswith("# Stage 1")
    assert "## Concepts in this stage" in body
    assert "## Recommended chapters" in body


def test_render_path_overview_includes_stage_count(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    plan = lp.LearningPathPlanner().plan(catalog, d)
    overview = next(f for f in plan.files if f.filename == "_path.md")
    assert "Learning path: Change Data Capture" in overview.content
    assert "Stages:" in overview.content


def test_render_flags_gaps_when_concepts_lack_chapters(catalog):
    """A concept with chapter_count=0 should appear in the gap section."""
    seed = _seed_concept(catalog, "Lonely Topic")
    nb = _seed_concept(catalog, "Prereq")
    _add_rel(catalog, seed, nb, "REQUIRES")  # Lonely → Prereq prereq
    resolver = _resolver_for({"lonely topic": seed})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Lonely Topic")
    plan = lp.LearningPathPlanner().plan(catalog, d)
    stage_files = [f for f in plan.files if f.purpose == "reading_list"]
    assert any("Coverage gaps in this stage" in f.content for f in stage_files)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_validator_passes_for_valid_plan(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    plan = lp.LearningPathPlanner().plan(catalog, d)
    issues = lp.LearningPathValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_unresolved_target(catalog):
    """If the decomposer set target_concept_id=-1, validator emits error."""
    from generator import GenPlan
    plan = GenPlan(
        generator_type="learning_path", package_name="x", domain="x",
        package_metadata={"target_concept_id": -1},
    )
    issues = lp.LearningPathValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "target" in i.message for i in issues)


def test_validator_flags_phantom_chapter_id(catalog, chain_graph):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    d = lp.PrerequisiteDecomposer().decompose(catalog, resolver, "Change Data Capture")
    plan = lp.LearningPathPlanner().plan(catalog, d)
    plan.units[0].metadata["chapter_ids"] = list(plan.units[0].metadata.get("chapter_ids", [])) + [999_999]
    issues = lp.LearningPathValidator().validate(catalog, plan)
    assert any(
        i.severity == "error" and "chapter_id" in i.message and "999999" in i.message
        for i in issues
    )


# ---------------------------------------------------------------------------
# End-to-end Generator.run_deterministic
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_path_and_stages(catalog, chain_graph, tmp_path):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    g = lp.make_learning_path_generator()
    pid, report, issues = g.run_deterministic(
        catalog, resolver, "Change Data Capture",
        output_root=str(tmp_path),
    )
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    assert pid > 0

    pkg_dir = tmp_path / "change-data-capture"
    assert (pkg_dir / "_path.md").exists()
    # At least one stage folder + reading-list.md
    stage_files = list(pkg_dir.glob("stage-*/reading-list.md"))
    assert stage_files, "no per-stage reading-list.md files written"


def test_run_deterministic_idempotent(catalog, chain_graph, tmp_path):
    resolver = _resolver_for({"change data capture": chain_graph["cdc"]})
    g = lp.make_learning_path_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, resolver, "Change Data Capture",
        output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, resolver, "Change Data Capture",
        output_root=str(tmp_path),
    )
    assert pid1 == pid2


def test_run_deterministic_unknown_target_returns_negative(catalog, chain_graph, tmp_path):
    resolver = _resolver_for({})
    g = lp.make_learning_path_generator()
    pid, _, issues = g.run_deterministic(
        catalog, resolver, "ghost", output_root=str(tmp_path),
    )
    assert pid == -1
    assert any(i.severity == "error" for i in issues)
