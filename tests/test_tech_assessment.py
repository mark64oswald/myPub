"""Tests for tech_assessment.py — Phase 12 Tech Assessment Generator."""
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

import tech_assessment as ta  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Tool", description=""):
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
def streaming_corpus(catalog):
    """Three streaming engines with varying coverage."""
    kafka = _seed_concept(catalog, "Apache Kafka",
                           description="Distributed event-streaming platform.")
    flink = _seed_concept(catalog, "Apache Flink",
                           description="Stream and batch processing engine.")
    spark = _seed_concept(catalog, "Spark Streaming",
                           description="Micro-batch streaming on Spark.")

    # Kafka: 3 chapters, 2 doc sections, 5 procedures
    ch1 = _seed_book_chapter(catalog, "Kafka Definitive", "Topics")
    ch2 = _seed_book_chapter(catalog, "Streaming Systems", "Kafka chapter")
    ch3 = _seed_book_chapter(catalog, "Designing Data-Intensive", "Logs")
    for ch in (ch1, ch2, ch3):
        _add_rel(catalog, kafka, kafka, "CITES", source_id=ch)
    # Flink: 1 chapter
    ch4 = _seed_book_chapter(catalog, "Stream Processing", "Flink chapter")
    _add_rel(catalog, flink, flink, "CITES", source_id=ch4)
    # Spark Streaming: 2 chapters
    ch5 = _seed_book_chapter(catalog, "Spark Definitive", "Streaming")
    _add_rel(catalog, spark, spark, "CITES", source_id=ch5)

    return {"kafka": kafka, "flink": flink, "spark": spark}


# ---------------------------------------------------------------------------


def test_decomposer_resolves_named_candidates(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
        "spark streaming": streaming_corpus["spark"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming Engines",
        candidates=["Apache Kafka", "Apache Flink", "Spark Streaming"],
    )
    names = [c.name for c in d.candidates]
    assert names == ["Apache Kafka", "Spark Streaming", "Apache Flink"] or len(names) == 3


def test_decomposer_orders_by_score(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming Engines",
        candidates=["Apache Flink", "Apache Kafka"],  # reversed input
    )
    # Kafka has 3 chapters vs Flink's 1; Kafka should win
    assert d.candidates[0].name == "Apache Kafka"


def test_decomposer_skips_unresolved_candidates(catalog, streaming_corpus):
    res = _resolver({"apache kafka": streaming_corpus["kafka"]})
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming Engines",
        candidates=["Apache Kafka", "ghost-engine"],
    )
    assert len(d.candidates) == 1
    assert any("ghost-engine" in n for n in d.notes)


def test_decomposer_treats_query_as_csv_when_candidates_missing(catalog, streaming_corpus):
    res = _resolver({"apache kafka": streaming_corpus["kafka"]})
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Apache Kafka, Apache Flink",
    )
    assert len(d.candidates) >= 1


def test_decomposer_empty_candidates_emits_note(catalog):
    res = _resolver({})
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "ghost", candidates=["ghost"],
    )
    assert d.candidates == []
    assert any("empty" in n.lower() or "no candidates" in n.lower() for n in d.notes)


def test_render_matrix_has_table_header(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
    )
    text = ta._render_matrix(d)
    assert "| Technology |" in text
    assert "Apache Kafka" in text
    assert "Apache Flink" in text


def test_render_recommendation_picks_winner(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
    )
    text = ta._render_recommendation(d)
    assert "Recommended:" in text
    assert "Apache Kafka" in text  # higher coverage


def test_render_handles_empty_candidates():
    d = ta._Decomposition(title="x", candidates=[], notes=[])
    matrix = ta._render_matrix(d)
    rec = ta._render_recommendation(d)
    assert "No candidates" in matrix
    assert "No candidates" in rec


def test_planner_one_unit_per_candidate(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
    )
    plan = ta.TechAssessmentPlanner().plan(catalog, d)
    assert len(plan.units) == 2
    fnames = {f.filename for f in plan.files}
    assert "_matrix.md" in fnames
    assert "_recommendation.md" in fnames
    assert any(f.startswith("candidates/") for f in fnames)


def test_validator_passes_for_valid_plan(catalog, streaming_corpus):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
    )
    plan = ta.TechAssessmentPlanner().plan(catalog, d)
    issues = ta.TechAssessmentValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_errors_on_no_candidates(catalog):
    from generator import GenPlan
    plan = GenPlan(generator_type="tech_assessment", package_name="x", domain="x",
                    package_metadata={"n_candidates": 0})
    issues = ta.TechAssessmentValidator().validate(catalog, plan)
    assert any(i.severity == "error" for i in issues)


def test_validator_warns_on_single_candidate(catalog, streaming_corpus):
    res = _resolver({"apache kafka": streaming_corpus["kafka"]})
    d = ta.FeatureMatrixDecomposer().decompose(
        catalog, res, "Streaming", candidates=["Apache Kafka"],
    )
    plan = ta.TechAssessmentPlanner().plan(catalog, d)
    issues = ta.TechAssessmentValidator().validate(catalog, plan)
    assert any(i.severity == "warning" and "one candidate" in i.message
                for i in issues)


def test_run_deterministic_writes_files(catalog, streaming_corpus, tmp_path):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
        "spark streaming": streaming_corpus["spark"],
    })
    g = ta.make_tech_assessment_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Streaming Engines",
        candidates=["Apache Kafka", "Apache Flink", "Spark Streaming"],
        output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "streaming-engines"
    assert (pkg_dir / "_matrix.md").exists()
    assert (pkg_dir / "_recommendation.md").exists()
    assert (pkg_dir / "candidates" / "apache-kafka.md").exists()


def test_run_deterministic_idempotent(catalog, streaming_corpus, tmp_path):
    res = _resolver({
        "apache kafka": streaming_corpus["kafka"],
        "apache flink": streaming_corpus["flink"],
    })
    g = ta.make_tech_assessment_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
        output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, res, "Streaming",
        candidates=["Apache Kafka", "Apache Flink"],
        output_root=str(tmp_path),
    )
    assert pid1 == pid2
