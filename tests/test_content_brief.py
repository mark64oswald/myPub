"""Tests for content_brief.py — Phase 9.1-9.3 Content Generator (deterministic v1)."""
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

import content_brief as cb  # noqa: E402


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


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


@pytest.fixture
def cdc_corpus(catalog):
    cdc = _seed_concept(catalog, "Change Data Capture")
    kafka = _seed_concept(catalog, "Apache Kafka", concept_type="Tool")
    debezium = _seed_concept(catalog, "Debezium", concept_type="Tool")
    batch_etl = _seed_concept(catalog, "Batch ETL", concept_type="Pattern")

    # Topic links
    _add_rel(catalog, cdc, kafka, "REQUIRES")
    _add_rel(catalog, cdc, debezium, "IMPLEMENTS")
    # CONTRASTS_WITH for angle hints
    _add_rel(catalog, cdc, batch_etl, "CONTRASTS_WITH")

    # Chapters mentioning CDC + Kafka
    ch1 = _seed_book_chapter(catalog, "Streaming Systems", "CDC chapter")
    ch2 = _seed_book_chapter(catalog, "Kafka Definitive Guide", "Kafka basics")
    _add_rel(catalog, cdc, kafka, "CITES", source_id=ch1)
    _add_rel(catalog, kafka, kafka, "CITES", source_id=ch2)

    return {"cdc": cdc, "kafka": kafka, "debezium": debezium, "batch_etl": batch_etl}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_slugify_handles_punctuation():
    assert cb._slugify("Hook!") == "hook"
    assert cb._slugify("Design Doc") == "design-doc"
    assert cb._slugify("") == "section"


def test_arc_is_format_specific():
    assert cb._ARCS["blog"][0][0] == "Hook"
    assert cb._ARCS["talk"][0][0] == "Opening Story"
    assert cb._ARCS["design-doc"][0][0] == "Context"
    assert cb._ARCS["chapter"][0][0] == "Introduction"


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def test_decomposer_unknown_topic_returns_empty(catalog, cdc_corpus):
    res = _resolver({})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "ghost")
    assert d.topic_concept_id == -1
    assert d.sections == []


def test_decomposer_resolves_topic_and_builds_blog_arc(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(
        catalog, res, "Change Data Capture", fmt="blog",
    )
    assert d.topic_concept_id == cdc_corpus["cdc"]
    assert d.fmt == "blog"
    titles = [s.title for s in d.sections]
    assert titles == [t for t, _ in cb._ARCS["blog"]]


def test_decomposer_unknown_format_falls_back_to_default(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(
        catalog, res, "Change Data Capture", fmt="podcast",
    )
    assert d.fmt == cb.DEFAULT_FORMAT


def test_decomposer_emits_angle_hints_from_contrasts(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(
        catalog, res, "Change Data Capture", fmt="blog",
    )
    # First section anchors on the topic itself; should pick up the
    # CONTRASTS_WITH neighbor (Batch ETL).
    hint_names = {n for s in d.sections for _cid, n in s.angle_hints}
    assert "Batch ETL" in hint_names


def test_decomposer_each_section_has_thesis(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    for s in d.sections:
        assert s.thesis  # non-empty


def test_decomposer_topic_with_no_neighbors_emits_note(catalog):
    seed = _seed_concept(catalog, "Lonely Topic")
    res = _resolver({"lonely topic": seed})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Lonely Topic")
    assert any("no graph neighbors" in n for n in d.notes)


def test_decomposer_supports_all_arcs(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    for fmt in ("blog", "talk", "design-doc", "chapter"):
        d = cb.RhetoricalContentDecomposer().decompose(
            catalog, res, "Change Data Capture", fmt=fmt,
        )
        assert len(d.sections) == len(cb._ARCS[fmt])


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_brief_includes_topic_and_format(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(
        catalog, res, "Change Data Capture", fmt="design-doc", angle="ship in Q2",
    )
    text = cb._render_brief(d)
    assert "Change Data Capture" in text
    assert "design-doc" in text
    assert "ship in Q2" in text


def test_render_outline_lists_sections_with_theses(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    text = cb._render_outline(d)
    for s in d.sections:
        assert s.title in text
        assert s.thesis in text


def test_render_section_lists_anchor_and_sources(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    s = d.sections[0]
    text = cb._render_section(s)
    assert s.title in text
    assert s.thesis in text


def test_render_sources_dedupes_by_type(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    text = cb._render_sources(d)
    assert "# Change Data Capture" in text


# ---------------------------------------------------------------------------
# Planner + Validator
# ---------------------------------------------------------------------------


def test_planner_produces_one_unit_per_section(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = cb.ContentBriefPlanner().plan(catalog, d)
    assert len(plan.units) == len(d.sections)
    fnames = {f.filename for f in plan.files}
    assert "_brief.md" in fnames
    assert "outline.md" in fnames
    assert "sources.md" in fnames


def test_validator_passes_for_valid_plan(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = cb.ContentBriefPlanner().plan(catalog, d)
    issues = cb.ContentBriefValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_unresolved_topic(catalog):
    from generator import GenPlan
    plan = GenPlan(generator_type="content_brief", package_name="x", domain="x",
                    package_metadata={"topic_concept_id": -1})
    issues = cb.ContentBriefValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "topic" in i.message for i in issues)


def test_validator_warns_on_sourceless_section(catalog, cdc_corpus):
    """Inject a section with no source citations and confirm warning."""
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = cb.ContentBriefPlanner().plan(catalog, d)
    plan.units[0].metadata["n_sources"] = 0
    issues = cb.ContentBriefValidator().validate(catalog, plan)
    assert any("no source citations" in i.message for i in issues
                if i.severity == "warning")


def test_validator_flags_phantom_chapter_id(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = cb.RhetoricalContentDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = cb.ContentBriefPlanner().plan(catalog, d)
    plan.units[0].metadata["source_ids_by_type"]["chapter"] = [999_999]
    issues = cb.ContentBriefValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_brief_outline_sources(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = cb.make_content_brief_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Change Data Capture", output_root=str(tmp_path),
    )
    assert pid > 0
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    pkg_dir = tmp_path / "change-data-capture"
    assert (pkg_dir / "_brief.md").exists()
    assert (pkg_dir / "outline.md").exists()
    assert (pkg_dir / "sources.md").exists()
    section_files = list((pkg_dir / "sections").glob("*.md"))
    assert len(section_files) == len(cb._ARCS["blog"])


def test_run_deterministic_idempotent(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = cb.make_content_brief_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Change Data Capture",
                                       output_root=str(tmp_path))
    assert pid1 == pid2


def test_run_deterministic_unknown_topic_returns_negative(catalog, tmp_path):
    res = _resolver({})
    g = cb.make_content_brief_generator()
    pid, _, issues = g.run_deterministic(
        catalog, res, "ghost", output_root=str(tmp_path),
    )
    assert pid == -1


def test_run_deterministic_supports_all_arcs(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = cb.make_content_brief_generator()
    for i, fmt in enumerate(("blog", "talk", "design-doc", "chapter")):
        pid, report, issues = g.run_deterministic(
            catalog, res, "Change Data Capture",
            fmt=fmt, package_name=f"cdc-{fmt}",
            output_root=str(tmp_path),
        )
        assert pid > 0
        errors = [iss for iss in issues if iss.severity == "error"]
        assert errors == []
