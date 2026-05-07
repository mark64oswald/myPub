"""Tests for character.py + dialog.py + author_panel.py — Phase 14."""
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

import character as char  # noqa: E402
import dialog as dlg  # noqa: E402
import author_panel as ap  # noqa: E402


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


def _seed_chapter(conn, book_title, chapter_title):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
        [book_title, f"/tmp/{book_title}.epub"],
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) VALUES (?, ?, 1) "
        "RETURNING chapter_id", [bid, chapter_title],
    ).fetchone()[0]


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


# ---------------------------------------------------------------------------
# Character scoring
# ---------------------------------------------------------------------------


def test_score_higher_for_preferred_relation_match(catalog):
    cid = _seed_concept(catalog, "Saga", "Pattern")
    other = _seed_concept(catalog, "Other")
    _add_rel(catalog, cid, other, "IMPLEMENTS")
    _add_rel(catalog, cid, other, "IMPLEMENTS", source_id=2)
    arch_score = char.score_concept_for_character(catalog, cid, char.ARCHITECT)
    pract_score = char.score_concept_for_character(catalog, cid, char.PRACTITIONER)
    assert arch_score > pract_score


def test_score_higher_for_preferred_concept_type(catalog):
    pat = _seed_concept(catalog, "Pat", "Pattern")
    tool = _seed_concept(catalog, "Tool", "Tool")
    arch_pat = char.score_concept_for_character(catalog, pat, char.ARCHITECT)
    arch_tool = char.score_concept_for_character(catalog, tool, char.ARCHITECT)
    assert arch_pat > arch_tool


def test_chapter_count_baseline(catalog):
    cid = _seed_concept(catalog, "Concept")
    ch = _seed_chapter(catalog, "Book", "Chapter")
    _add_rel(catalog, cid, cid, "CITES", source_id=ch)
    score = char.score_concept_for_character(catalog, cid, char.ARCHITECT)
    assert score >= 1.0  # at least the chapter baseline


def test_parse_character_json_round_trips():
    spec = [{
        "name": "Theorist",
        "bio": "Loves theorems",
        "preferred_relations": ["EXTENDS"],
        "preferred_concept_types": ["Concept"],
        "preferred_era": "classical",
    }]
    chars = char.parse_character_json(spec)
    assert len(chars) == 1
    assert chars[0].name == "Theorist"
    assert chars[0].preferred_relations == ["EXTENDS"]


# ---------------------------------------------------------------------------
# Dialog Generator
# ---------------------------------------------------------------------------


@pytest.fixture
def dialog_corpus(catalog):
    """Topic with neighbors that score divergently across the default
    Architect / Practitioner profiles."""
    sm = _seed_concept(catalog, "Service Mesh", "Pattern")
    # Pattern-y neighbor (Architect favors)
    cb = _seed_concept(catalog, "Circuit Breaker", "Pattern")
    _add_rel(catalog, cb, cb, "IMPLEMENTS")
    _add_rel(catalog, cb, cb, "IMPLEMENTS", source_id=2)
    # Tool-y neighbor (Practitioner favors)
    istio = _seed_concept(catalog, "Istio", "Tool")
    ch = _seed_chapter(catalog, "Service Mesh Book", "Istio chapter")
    _add_rel(catalog, istio, istio, "CITES", source_id=ch)
    # Wire all to topic
    _add_rel(catalog, sm, cb, "CITES")
    _add_rel(catalog, sm, istio, "CITES")
    return {"sm": sm, "cb": cb, "istio": istio}


def test_dialog_resolves_topic_and_finds_beats(catalog, dialog_corpus):
    res = _resolver({"service mesh": dialog_corpus["sm"]})
    d = dlg.DivergenceDecomposer().decompose(catalog, res, "Service Mesh")
    assert d.topic_concept_id == dialog_corpus["sm"]
    assert len(d.beats) > 0


def test_dialog_unknown_topic_returns_negative(catalog):
    res = _resolver({})
    g = dlg.make_dialog_generator()
    pid, _, _ = g.run_deterministic(catalog, res, "ghost", output_root="/tmp")
    assert pid == -1


def test_dialog_beats_have_a_favorite(catalog, dialog_corpus):
    res = _resolver({"service mesh": dialog_corpus["sm"]})
    d = dlg.DivergenceDecomposer().decompose(catalog, res, "Service Mesh")
    favors = {b.favors for b in d.beats}
    # Should produce at least one of A or B favored
    assert favors & {"A", "B"}


def test_dialog_renders_with_two_speaker_lines(catalog, dialog_corpus):
    res = _resolver({"service mesh": dialog_corpus["sm"]})
    d = dlg.DivergenceDecomposer().decompose(catalog, res, "Service Mesh")
    text = dlg._render_dialog(d)
    assert "**Architect:**" in text and "**Practitioner:**" in text


def test_dialog_run_deterministic_writes_files(catalog, dialog_corpus, tmp_path):
    res = _resolver({"service mesh": dialog_corpus["sm"]})
    g = dlg.make_dialog_generator()
    pid, _, _ = g.run_deterministic(
        catalog, res, "Service Mesh", output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "service-mesh"
    assert (pkg_dir / "dialogue.md").exists()
    assert (pkg_dir / "_stage_directions.md").exists()


def test_dialog_handles_no_divergence(catalog, tmp_path):
    """A topic whose neighbors all score equally should warn but ship."""
    sm = _seed_concept(catalog, "Lonely Topic")
    res = _resolver({"lonely topic": sm})
    g = dlg.make_dialog_generator()
    pid, _, issues = g.run_deterministic(
        catalog, res, "Lonely Topic", output_root=str(tmp_path),
    )
    assert pid > 0
    assert any("no divergent beats" in i.message for i in issues
                if i.severity == "warning")


# ---------------------------------------------------------------------------
# Author Panel Generator
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_corpus(catalog):
    cqrs = _seed_concept(catalog, "CQRS", "Pattern")
    es = _seed_concept(catalog, "Event Sourcing", "Pattern")
    _add_rel(catalog, cqrs, es, "IMPLEMENTS")
    return {"cqrs": cqrs, "es": es}


def test_panel_decomposer_resolves_topics(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"], "event sourcing": panel_corpus["es"]})
    d = ap.PanelDecomposer().decompose(
        catalog, res, "DDD",
        topics=["CQRS", "Event Sourcing"],
    )
    names = {p.concept_name for p in d.topics}
    assert "CQRS" in names and "Event Sourcing" in names


def test_panel_skips_unresolved_topic(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"]})
    d = ap.PanelDecomposer().decompose(
        catalog, res, "panel",
        topics=["CQRS", "ghost-topic"],
    )
    assert any("ghost-topic" in n for n in d.notes)


def test_panel_uses_default_2_characters_when_none_supplied(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"]})
    d = ap.PanelDecomposer().decompose(
        catalog, res, "panel", topics=["CQRS"],
    )
    assert len(d.characters) == 2


def test_panel_supports_n_more_than_2(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"]})
    extra = char.Character(
        name="Theorist", bio="b",
        preferred_relations=["EXTENDS"],
        preferred_concept_types=["Concept"],
    )
    d = ap.PanelDecomposer().decompose(
        catalog, res, "panel", topics=["CQRS"],
        characters=[char.ARCHITECT, char.PRACTITIONER, extra],
    )
    assert len(d.characters) == 3


def test_panel_renders_grid(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"], "event sourcing": panel_corpus["es"]})
    d = ap.PanelDecomposer().decompose(
        catalog, res, "DDD panel", topics=["CQRS", "Event Sourcing"],
    )
    text = ap._render_panel(d)
    assert "| Topic |" in text
    assert "CQRS" in text and "Event Sourcing" in text


def test_panel_renders_per_author(catalog, panel_corpus):
    res = _resolver({"cqrs": panel_corpus["cqrs"]})
    d = ap.PanelDecomposer().decompose(
        catalog, res, "p", topics=["CQRS"],
    )
    text = ap._render_author(d, char.ARCHITECT)
    assert "Architect" in text


def test_panel_run_deterministic_writes_files(catalog, panel_corpus, tmp_path):
    res = _resolver({"cqrs": panel_corpus["cqrs"], "event sourcing": panel_corpus["es"]})
    g = ap.make_author_panel_generator()
    pid, _, _ = g.run_deterministic(
        catalog, res, "DDD",
        topics=["CQRS", "Event Sourcing"],
        panel_name="DDD",
        output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "ddd"
    assert (pkg_dir / "_panel.md").exists()
    assert (pkg_dir / "authors" / "architect.md").exists()
    assert (pkg_dir / "authors" / "practitioner.md").exists()
