"""Tests for slide_deck.py — Phase 9.5 Slide-deck Outline generator (v1)."""
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

import slide_deck as sd  # noqa: E402


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


def _seed_procedure(conn, name, steps="step 1\nstep 2"):
    return conn.execute(
        "INSERT INTO procedure (name, steps) VALUES (?, ?) RETURNING procedure_id",
        [name, steps],
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
    """Topic = CDC, with 3 well-connected related concepts."""
    cdc = _seed_concept(catalog, "Change Data Capture", "Pattern")
    kafka = _seed_concept(catalog, "Apache Kafka", "Tool")
    debezium = _seed_concept(catalog, "Debezium", "Tool")
    delta = _seed_concept(catalog, "Delta Lake", "Tool")
    materialized = _seed_concept(catalog, "Materialized View", "Pattern")
    log_based = _seed_concept(catalog, "Log-based CDC", "Pattern")

    # Wire relations
    for rel in (kafka, debezium, delta):
        _add_rel(catalog, cdc, rel, "REQUIRES")
    _add_rel(catalog, kafka, materialized, "CITES")
    _add_rel(catalog, debezium, log_based, "EXTENDS")
    _add_rel(catalog, delta, materialized, "CITES")

    # Chapters that mention concepts (so chapter_count drives ranking)
    ch1 = _seed_book_chapter(catalog, "Streaming Systems", "CDC chapter")
    ch2 = _seed_book_chapter(catalog, "Kafka Definitive Guide", "Kafka basics")
    ch3 = _seed_book_chapter(catalog, "Delta Lake Definitive", "Delta basics")
    _add_rel(catalog, kafka, kafka, "CITES", source_id=ch2)
    _add_rel(catalog, debezium, kafka, "CITES", source_id=ch2)
    _add_rel(catalog, delta, delta, "CITES", source_id=ch3)

    # Procedures linked to insight anchors
    p1 = _seed_procedure(catalog, "Configure Kafka topic for CDC")
    _link_proc(catalog, p1, kafka)
    p2 = _seed_procedure(catalog, "Bootstrap a Debezium connector")
    _link_proc(catalog, p2, debezium)

    return {
        "cdc": cdc, "kafka": kafka, "debezium": debezium,
        "delta": delta, "materialized": materialized,
        "log_based": log_based,
        "p1": p1, "p2": p2,
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_short_phrase_strips_filler_and_caps_words():
    assert sd._short_phrase("The very long phrase that is too long here ok") == \
           "very long phrase that is too long here ok"
    # 14 words capped at 5; ellipsis attaches to the last token.
    # Use words that aren't in the leading-filler list ("A", "The", etc.)
    out = sd._short_phrase(
        "kafka redis postgres spark delta flink hadoop hive mongo",
        max_words=4,
    )
    assert out.endswith("…")
    assert "flink" not in out  # 6th word excluded
    assert "spark" in out      # 4th word kept


def test_short_phrase_strips_trailing_punctuation():
    assert sd._short_phrase("Hello world.") == "Hello world"
    assert sd._short_phrase("Question?") == "Question"


def test_trim_sentences_takes_first_n():
    txt = "First sentence. Second sentence. Third sentence. Fourth."
    out = sd._trim_sentences(txt, max_sentences=2)
    assert "First sentence" in out and "Second sentence" in out
    assert "Third" not in out and "Fourth" not in out


def test_trim_sentences_handles_empty():
    assert sd._trim_sentences("") == ""
    assert sd._trim_sentences(None) == ""


def test_word_count_bullet_counts_words():
    assert sd._word_count_bullet("hello world foo") == 3
    # The counter is approximate; hyphenated tokens may split. The
    # validator only cares whether words exceed MAX_WORDS_PER_BULLET,
    # so undercounting is safe and overcounting is conservative.
    assert sd._word_count_bullet("simple plain words here") == 4


def test_sentence_count_handles_punctuation():
    assert sd._sentence_count("One. Two! Three?") == 3
    assert sd._sentence_count("") == 0


def test_slide_count_from_duration_scales():
    n_15 = sd._slide_count_from_duration(15, 3)
    n_30 = sd._slide_count_from_duration(30, 3)
    n_60 = sd._slide_count_from_duration(60, 3)
    assert n_15 < n_30 < n_60


def test_slugify_handles_punctuation_and_spaces():
    assert sd._slugify("Change Data Capture") == "change-data-capture"
    assert sd._slugify("Topic? Yes!") == "topic-yes"
    assert sd._slugify("") == "topic"


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def test_decomposer_unknown_topic_returns_empty(catalog, cdc_corpus):
    res = _resolver({})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Nonexistent")
    assert d.topic_concept_id == -1
    assert d.slides == []
    assert any("not found" in n for n in d.notes)


def test_decomposer_resolves_topic_and_finds_insights(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(
        catalog, res, "Change Data Capture", duration_min=30, n_insights=3,
    )
    assert d.topic_concept_id == cdc_corpus["cdc"]
    assert len(d.insights) == 3


def test_decomposer_renders_full_slide_sequence(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(
        catalog, res, "Change Data Capture", duration_min=30,
    )
    roles = [s.role for s in d.slides]
    assert roles[0] == "title"
    assert roles[1] == "agenda"
    assert roles[-1] == "qa"
    assert roles[-2] == "takeaways"
    # Each insight contributes at least one 'insight' role slide
    assert roles.count("insight") == len(d.insights)


def test_decomposer_attaches_procedures_to_demo_slides(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(
        catalog, res, "Change Data Capture",
    )
    proc_slides = [s for s in d.slides if s.source_procedure_ids]
    # Insights that have procedures attached should produce a demo slide
    assert proc_slides, "expected at least one demo slide"


def test_decomposer_topic_with_no_neighbors_falls_back(catalog):
    lonely = _seed_concept(catalog, "Lonely Topic")
    res = _resolver({"lonely topic": lonely})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Lonely Topic")
    # Fallback: a single insight on the topic itself
    assert len(d.insights) == 1
    assert d.insights[0].anchor_concept_id == lonely


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_outline_lists_every_slide(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    text = sd._render_outline(d)
    assert text.startswith("# Change Data Capture")
    for s in d.slides:
        assert f"Slide {s.ordinal}" in text


def test_render_abstract_mentions_audience(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(
        catalog, res, "Change Data Capture", audience="executives",
    )
    text = sd._render_abstract(d)
    assert "executives" in text


def test_render_visuals_includes_per_slide_suggestion(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    text = sd._render_visuals(d)
    # Every slide with a visual_suggestion should appear
    for s in d.slides:
        if s.visual_suggestion:
            assert f"Slide {s.ordinal}" in text


def test_render_speaker_notes_one_section_per_slide(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    text = sd._render_speaker_notes(d)
    headers = [line for line in text.splitlines() if line.startswith("## Slide ")]
    assert len(headers) == len(d.slides)


def test_render_sources_lists_concepts_and_procedures(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    text = sd._render_sources(catalog, d)
    assert "## Concepts" in text
    assert "## Procedures" in text
    # CDC topic should appear
    assert "Change Data Capture" in text


# ---------------------------------------------------------------------------
# Planner + Validator
# ---------------------------------------------------------------------------


def test_planner_produces_one_unit_per_slide(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    assert len(plan.units) == len(d.slides)
    fnames = {f.filename for f in plan.files}
    assert fnames == {
        "_outline.md", "_abstract.md", "visuals.md",
        "speaker-notes.md", "sources.md",
    }


def test_validator_passes_for_valid_plan(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    issues = sd.SlideDeckValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_too_many_bullets(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    # Inject a bullet-overflow in slide 0
    plan.units[0].content_markdown = "\n".join(
        f"- bullet {i}" for i in range(7)
    )
    issues = sd.SlideDeckValidator().validate(catalog, plan)
    assert any("bullets" in i.message and i.severity == "error" for i in issues)


def test_validator_flags_too_long_bullet(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    plan.units[0].content_markdown = (
        "- " + " ".join(f"word{i}" for i in range(15))
    )
    issues = sd.SlideDeckValidator().validate(catalog, plan)
    assert any("words" in i.message and i.severity == "error" for i in issues)


def test_validator_flags_phantom_concept_id(catalog, cdc_corpus):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    plan.units[0].metadata["concept_ids"] = [999_999]
    issues = sd.SlideDeckValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


def test_validator_warns_on_slide_count_mismatch(catalog, cdc_corpus):
    """Override duration in metadata to force the count-vs-duration check
    to disagree."""
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    d = sd.RhetoricalArcDecomposer().decompose(catalog, res, "Change Data Capture")
    plan = sd.SlideDeckPlanner().plan(catalog, d)
    plan.package_metadata["duration_min"] = 5  # force mismatch
    issues = sd.SlideDeckValidator().validate(catalog, plan)
    assert any("slide count" in i.message and i.severity == "warning" for i in issues)


# ---------------------------------------------------------------------------
# End-to-end Generator.run_deterministic
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_all_files(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = sd.make_slide_deck_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Change Data Capture",
        duration_min=30, audience="engineers",
        output_root=str(tmp_path),
    )
    assert pid > 0
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    pkg_dir = tmp_path / "change-data-capture"
    for fname in ("_outline.md", "_abstract.md", "visuals.md",
                  "speaker-notes.md", "sources.md"):
        assert (pkg_dir / fname).exists()


def test_run_deterministic_idempotent(catalog, cdc_corpus, tmp_path):
    res = _resolver({"change data capture": cdc_corpus["cdc"]})
    g = sd.make_slide_deck_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, res, "Change Data Capture", output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, res, "Change Data Capture", output_root=str(tmp_path),
    )
    assert pid1 == pid2


def test_run_deterministic_unknown_topic_returns_negative(catalog, cdc_corpus, tmp_path):
    res = _resolver({})
    g = sd.make_slide_deck_generator()
    pid, _, issues = g.run_deterministic(
        catalog, res, "ghost", output_root=str(tmp_path),
    )
    assert pid == -1
    assert any(i.severity == "error" for i in issues)
