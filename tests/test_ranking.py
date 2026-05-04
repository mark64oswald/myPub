"""
test_ranking.py — Phase 4.5 ranking engine tests.

Layered:

    Pure factor functions   — recency, doc_alignment, relevance, corroboration,
                              authority, combined. Deterministic, no DB.
    Weights + profiles      — sum-to-1 invariant + the 5 starting profiles
                              from arch §8.5.
    DB-backed lookups       — chapter/doc_section recency, alignment stats,
                              authority. Use a small in-memory catalog.
    Component scoring       — compute_components_for_result over both kinds.
    InteractiveRanker       — {primary, corroborations, conflicts} with real
                              alignment_edge rows driving the conflict split.
    GenerationRanker        — each of the three §8.3 strategies.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"

if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import ranking  # noqa: E402


NOW = datetime(2026, 5, 3, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure factor functions
# ---------------------------------------------------------------------------


def test_recency_score_at_zero_age_is_one():
    assert ranking.recency_score(age_days=0) == pytest.approx(1.0)


def test_recency_score_at_half_life_is_one_half():
    assert ranking.recency_score(age_days=ranking.DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.5)


def test_recency_score_decays_monotonically():
    s_new = ranking.recency_score(age_days=30)
    s_mid = ranking.recency_score(age_days=365)
    s_old = ranking.recency_score(age_days=365 * 5)
    assert s_new > s_mid > s_old


def test_recency_score_unknown_age_is_neutral():
    assert ranking.recency_score(age_days=None) == pytest.approx(ranking.DEFAULT_NEUTRAL_FACTOR)


def test_recency_score_negative_age_clamps_to_one():
    """A snapshot retrieved a few seconds in the future (clock skew) shouldn't
    crash or score below 1.0."""
    assert ranking.recency_score(age_days=-5) == 1.0


def test_doc_alignment_score_no_edges_is_neutral():
    assert ranking.doc_alignment_score(corroborates=0, contradicts=0) == 0.5


def test_doc_alignment_score_pure_corroborates_is_one():
    assert ranking.doc_alignment_score(corroborates=5, contradicts=0) == 1.0


def test_doc_alignment_score_pure_contradicts_is_zero():
    assert ranking.doc_alignment_score(corroborates=0, contradicts=5) == 0.0


def test_doc_alignment_score_mixed_returns_fraction():
    assert ranking.doc_alignment_score(corroborates=3, contradicts=1) == pytest.approx(0.75)


def test_relevance_score_at_max_rrf_is_one():
    assert ranking.relevance_score(rrf_score=2.0, max_rrf_score=2.0) == 1.0


def test_relevance_score_proportional_to_rrf():
    assert ranking.relevance_score(rrf_score=1.0, max_rrf_score=2.0) == 0.5


def test_relevance_score_zero_max_returns_zero():
    """Empty / all-zero result set must not divide-by-zero — return 0."""
    assert ranking.relevance_score(rrf_score=0.0, max_rrf_score=0.0) == 0.0


def test_corroboration_score_is_zero_at_zero():
    assert ranking.corroboration_score(corroborator_count=0) == 0.0


def test_corroboration_score_saturates():
    """Curve should approach but not exceed 1.0 even at high counts."""
    s = ranking.corroboration_score(corroborator_count=100)
    assert 0.95 < s <= 1.0


def test_corroboration_score_monotonically_increasing():
    counts = [0, 1, 3, 5, 10]
    scores = [ranking.corroboration_score(corroborator_count=c) for c in counts]
    assert scores == sorted(scores)


def test_authority_score_clamps_out_of_range_input():
    assert ranking.authority_score_from_raw(1.5) == 1.0
    assert ranking.authority_score_from_raw(-0.5) == 0.0


def test_authority_score_unknown_is_neutral():
    assert ranking.authority_score_from_raw(None) == 0.5


def test_publisher_authority_known_imprints():
    """Established tech imprints get above-baseline authority."""
    assert ranking.authority_score_from_publisher("O'Reilly") > ranking.DEFAULT_AUTHORITY_BOOK
    assert ranking.authority_score_from_publisher("Manning") > ranking.DEFAULT_AUTHORITY_BOOK
    assert ranking.authority_score_from_publisher("MIT Press") > ranking.DEFAULT_AUTHORITY_BOOK


def test_publisher_authority_case_and_whitespace_tolerant():
    """Lookup must work regardless of casing or surrounding whitespace."""
    a1 = ranking.authority_score_from_publisher("o'reilly")
    a2 = ranking.authority_score_from_publisher("  O'Reilly  ")
    a3 = ranking.authority_score_from_publisher("O'REILLY")
    assert a1 == a2 == a3


def test_publisher_authority_unknown_falls_back_to_default():
    assert ranking.authority_score_from_publisher("Some Niche Press") == \
           ranking.DEFAULT_AUTHORITY_BOOK


def test_publisher_authority_none_or_empty_falls_back_to_default():
    assert ranking.authority_score_from_publisher(None) == ranking.DEFAULT_AUTHORITY_BOOK
    assert ranking.authority_score_from_publisher("") == ranking.DEFAULT_AUTHORITY_BOOK
    assert ranking.authority_score_from_publisher("   ") == ranking.DEFAULT_AUTHORITY_BOOK


def test_publisher_authority_no_fuzzy_false_positives():
    """A publisher containing a known substring (e.g., 'Wiley-VCH') must NOT
    accidentally match the known 'Wiley' score — exact strings only."""
    assert ranking.authority_score_from_publisher("Wiley-VCH") == \
           ranking.DEFAULT_AUTHORITY_BOOK


def test_combined_score_zero_inputs_zero_output():
    w = ranking.WEIGHT_PROFILES["currency_critical_interactive"]
    assert ranking.combined_score(
        w, recency=0, doc_alignment=0, relevance=0, corroboration=0, authority=0,
    ) == 0.0


def test_combined_score_unit_inputs_yield_unit_output():
    """Every weight profile's components, all at 1.0, should sum to ~1.0."""
    for name, w in ranking.WEIGHT_PROFILES.items():
        s = ranking.combined_score(
            w, recency=1, doc_alignment=1, relevance=1, corroboration=1, authority=1,
        )
        assert s == pytest.approx(1.0, abs=1e-6), f"profile {name} doesn't sum to 1.0"


# ---------------------------------------------------------------------------
# Weights + profiles
# ---------------------------------------------------------------------------


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="must sum to 1.0"):
        ranking.Weights(0.5, 0.5, 0.5, 0.5, 0.5)


def test_weight_profiles_are_all_valid():
    """Every profile in WEIGHT_PROFILES must construct without raising."""
    assert set(ranking.WEIGHT_PROFILES) >= {
        "currency_critical_interactive",
        "foundational_interactive",
        "skill_recent_doc",
        "skill_consensus",
        "skill_authority",
    }
    for name, w in ranking.WEIGHT_PROFILES.items():
        # Construction enforces sum-to-1; an invalid profile would raise.
        assert isinstance(w, ranking.Weights), name


# ---------------------------------------------------------------------------
# DB-backed factor lookups (small in-memory catalog)
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog():
    """Fresh in-memory catalog with v2 schema."""
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


@pytest.fixture
def populated(catalog):
    """Seed: two chapters (one with publication_date), one doc_source +
    snapshot + section. Returns ids the tests use."""
    catalog.execute("INSERT INTO author (name) VALUES ('Author')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path, publication_date) "
        "VALUES ('Old Book', '/x', '2018-01-01') RETURNING book_id"
    ).fetchone()[0]
    book2_id = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('No Date Book', '/y') "
        "RETURNING book_id"
    ).fetchone()[0]
    ch1 = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'C1', 'old content') RETURNING chapter_id", [book_id]
    ).fetchone()[0]
    ch2 = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'C2', 'no-date content') RETURNING chapter_id", [book2_id]
    ).fetchone()[0]

    src_id = catalog.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
        "                        authority_score) "
        "VALUES ('Test Docs', 'context7', 'context7', '/x/y', 0.85) "
        "RETURNING doc_source_id"
    ).fetchone()[0]
    snap_id = catalog.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content) "
        "VALUES (?, 'context7', 'http://x', 'h', 'c') RETURNING snapshot_id",
        [src_id],
    ).fetchone()[0]
    sec_id = catalog.execute(
        "INSERT INTO doc_section (snapshot_id, heading_text, ordinal, content) "
        "VALUES (?, 'Heading', 0, 'doc body') RETURNING doc_section_id",
        [snap_id],
    ).fetchone()[0]
    cid = catalog.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Concept', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]
    return {
        "ch_with_date": int(ch1), "ch_no_date": int(ch2),
        "doc_section_id": int(sec_id), "concept_id": int(cid),
        "doc_source_id": int(src_id),
    }


def test_chapter_age_days_uses_publication_date(catalog, populated):
    age = ranking.chapter_age_days(catalog, populated["ch_with_date"], now=NOW)
    # 2018-01-01 to 2026-05-03 ≈ 8 years and 4 months ≈ 3045 days.
    assert age is not None
    assert 3000 < age < 3100


def test_chapter_age_days_returns_none_when_no_pub_date(catalog, populated):
    assert ranking.chapter_age_days(catalog, populated["ch_no_date"], now=NOW) is None


def test_doc_section_age_days_uses_snapshot_retrieved_at(catalog, populated):
    """retrieved_at defaults to CURRENT_TIMESTAMP — age should be ~0 here."""
    age = ranking.doc_section_age_days(catalog, populated["doc_section_id"], now=NOW)
    assert age is not None
    # CURRENT_TIMESTAMP at insert time is "now-ish"; age should be tiny relative
    # to anything meaningful for ranking. Allow a generous window.
    assert age < 365


def test_doc_section_raw_authority_pulls_from_doc_source(catalog, populated):
    auth = ranking.doc_section_raw_authority(catalog, populated["doc_section_id"])
    assert auth == 0.85


def test_chapter_raw_authority_uses_publisher_when_known(catalog):
    """A book with a known publisher should yield a publisher-derived score,
    not the flat 0.6 default."""
    catalog.execute("INSERT INTO author (name) VALUES ('A')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('B', '/x', 'O''Reilly') RETURNING book_id"
    ).fetchone()[0]
    ch_id = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'C', 'x') RETURNING chapter_id", [book_id]
    ).fetchone()[0]
    auth = ranking.chapter_raw_authority(catalog, int(ch_id))
    assert auth == ranking.PUBLISHER_AUTHORITY["o'reilly"]


def test_chapter_raw_authority_falls_back_to_default_when_publisher_missing(catalog):
    catalog.execute("INSERT INTO author (name) VALUES ('A')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('No Publisher', '/y') "
        "RETURNING book_id"
    ).fetchone()[0]
    ch_id = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, content) "
        "VALUES (?, 1, 'x') RETURNING chapter_id", [book_id]
    ).fetchone()[0]
    auth = ranking.chapter_raw_authority(catalog, int(ch_id))
    assert auth == ranking.DEFAULT_AUTHORITY_BOOK


def test_doc_section_raw_authority_returns_none_when_missing(catalog):
    """Section that doesn't exist → None (not zero — "unknown" is distinct)."""
    assert ranking.doc_section_raw_authority(catalog, 999999) is None


def test_chapter_alignment_stats_zero_when_no_edges(catalog, populated):
    assert ranking.chapter_alignment_stats(catalog, populated["ch_with_date"]) == (0, 0)


def test_chapter_alignment_stats_counts_by_relation_type(catalog, populated):
    cid = populated["concept_id"]
    sid = populated["doc_section_id"]
    chid = populated["ch_with_date"]
    catalog.execute(
        "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
        "                            relation_type, confidence) "
        "VALUES (?, ?, ?, 'CORROBORATES', 0.9), "
        "       (?, ?, ?, 'CORROBORATES', 0.7), "
        "       (?, ?, ?, 'CONTRADICTS', 0.8)",
        [sid, chid, cid, sid, chid, cid, sid, chid, cid],
    )
    assert ranking.chapter_alignment_stats(catalog, chid) == (2, 1)


def test_doc_section_alignment_stats_counts_outbound_edges(catalog, populated):
    cid = populated["concept_id"]
    sid = populated["doc_section_id"]
    chid = populated["ch_with_date"]
    catalog.execute(
        "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
        "                            relation_type, confidence) "
        "VALUES (?, ?, ?, 'CORROBORATES', 0.9)",
        [sid, chid, cid],
    )
    assert ranking.doc_section_alignment_stats(catalog, sid) == (1, 0)


# ---------------------------------------------------------------------------
# Component scoring
# ---------------------------------------------------------------------------


def test_compute_components_for_chapter(catalog, populated):
    result = {"kind": "chapter", "result_id": populated["ch_with_date"], "rrf_score": 0.5}
    comps = ranking.compute_components_for_result(
        catalog, result, max_rrf_score=1.0, now=NOW,
    )
    assert 0.0 <= comps.recency <= 1.0
    assert comps.doc_alignment == 0.5  # no edges
    assert comps.relevance == 0.5  # 0.5 / 1.0
    assert comps.corroboration == 0.0  # no edges
    assert comps.authority == ranking.DEFAULT_AUTHORITY_BOOK


def test_compute_components_for_doc_section(catalog, populated):
    result = {"kind": "doc_section", "result_id": populated["doc_section_id"],
              "rrf_score": 1.0}
    comps = ranking.compute_components_for_result(
        catalog, result, max_rrf_score=1.0, now=NOW,
    )
    assert comps.recency > 0.99  # very recent snapshot
    assert comps.relevance == 1.0
    assert comps.authority == 0.85


def test_compute_components_unknown_kind_raises(catalog):
    with pytest.raises(ValueError, match="unknown result kind"):
        ranking.compute_components_for_result(
            catalog, {"kind": "weird", "result_id": 1, "rrf_score": 0.5},
            max_rrf_score=1.0,
        )


# ---------------------------------------------------------------------------
# InteractiveRanker
# ---------------------------------------------------------------------------


def test_interactive_ranker_empty_results_returns_none_primary(catalog):
    out = ranking.InteractiveRanker(
        catalog, ranking.WEIGHT_PROFILES["currency_critical_interactive"],
    ).rank([])
    assert out.primary is None
    assert out.corroborations == []
    assert out.conflicts == []


def test_interactive_ranker_picks_highest_combined_as_primary(catalog, populated):
    """Doc section (recent + high authority) should outrank a 2018 chapter."""
    results = [
        {"kind": "chapter", "result_id": populated["ch_with_date"], "rrf_score": 1.0},
        {"kind": "doc_section", "result_id": populated["doc_section_id"], "rrf_score": 1.0},
    ]
    out = ranking.InteractiveRanker(
        catalog, ranking.WEIGHT_PROFILES["currency_critical_interactive"],
    ).rank(results, now=NOW)
    assert out.primary.result["kind"] == "doc_section"


def test_interactive_ranker_routes_contradicts_to_conflicts(catalog, populated):
    """A doc_section with a CONTRADICTS edge to a chapter should appear under
    ``conflicts`` when that chapter is the primary.

    To make the chapter win primary, seed extra corroborating sections
    (CORROBORATES edges pointing at the chapter) so its doc_alignment +
    corroboration components push it ahead of the lone-CONTRADICTS section.
    """
    cid = populated["concept_id"]
    sid = populated["doc_section_id"]
    chid = populated["ch_with_date"]

    # Seed three sibling sections that all CORROBORATE the chapter, plus the
    # original conflict-bearing section that CONTRADICTS it.
    snap_id = catalog.execute(
        "SELECT snapshot_id FROM doc_section WHERE doc_section_id = ?", [sid]
    ).fetchone()[0]
    corr_section_ids = []
    for i in range(3):
        new_sid = catalog.execute(
            "INSERT INTO doc_section (snapshot_id, heading_text, ordinal, content) "
            "VALUES (?, ?, ?, 'corroborating body') RETURNING doc_section_id",
            [snap_id, f"corr-{i}", i + 1],
        ).fetchone()[0]
        corr_section_ids.append(int(new_sid))
        catalog.execute(
            "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
            "                            relation_type, confidence) "
            "VALUES (?, ?, ?, 'CORROBORATES', 0.9)",
            [int(new_sid), chid, cid],
        )
    # The conflict-bearing section.
    catalog.execute(
        "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
        "                            relation_type, confidence) "
        "VALUES (?, ?, ?, 'CONTRADICTS', 0.9)",
        [sid, chid, cid],
    )

    # foundational_interactive heavily weights corroboration + authority.
    # Chapter now has 3 corroborates + 1 contradicts = doc_alignment=0.75
    # and corroboration~0.63; should beat the lone doc_section.
    results = [
        {"kind": "chapter", "result_id": chid, "rrf_score": 1.0},
        {"kind": "doc_section", "result_id": sid, "rrf_score": 0.9},
    ]
    out = ranking.InteractiveRanker(
        catalog, ranking.WEIGHT_PROFILES["foundational_interactive"],
    ).rank(results, now=NOW)
    assert out.primary.result["kind"] == "chapter"
    assert any(c.result.get("result_id") == sid for c in out.conflicts)
    assert not any(c.result.get("result_id") == sid for c in out.corroborations)


def test_interactive_ranker_routes_non_contradicts_to_corroborations(catalog, populated):
    """No CONTRADICTS edges → secondary results land in corroborations."""
    chid = populated["ch_with_date"]
    sid = populated["doc_section_id"]
    results = [
        {"kind": "chapter", "result_id": chid, "rrf_score": 1.0},
        {"kind": "doc_section", "result_id": sid, "rrf_score": 0.9},
    ]
    out = ranking.InteractiveRanker(
        catalog, ranking.WEIGHT_PROFILES["foundational_interactive"],
    ).rank(results, now=NOW)
    assert out.primary is not None
    assert len(out.corroborations) >= 1
    assert out.conflicts == []


# ---------------------------------------------------------------------------
# GenerationRanker
# ---------------------------------------------------------------------------


def test_generation_ranker_unknown_strategy_raises(catalog):
    gr = ranking.GenerationRanker(catalog, ranking.WEIGHT_PROFILES["skill_recent_doc"])
    with pytest.raises(ValueError, match="unknown strategy"):
        gr.select([{"kind": "chapter", "result_id": 1, "rrf_score": 0.5}],
                  strategy="not_a_strategy")


def test_generation_ranker_recent_doc_anchored_drops_contradicted_chapters(
    catalog, populated,
):
    """Chapters with CONTRADICTS edges pointing at them get dropped under
    recent-doc-anchored selection. Provenance is recorded in `dropped`."""
    cid = populated["concept_id"]
    sid = populated["doc_section_id"]
    chid = populated["ch_with_date"]
    catalog.execute(
        "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
        "                            relation_type, confidence) "
        "VALUES (?, ?, ?, 'CONTRADICTS', 0.9)",
        [sid, chid, cid],
    )
    out = ranking.GenerationRanker(
        catalog, ranking.WEIGHT_PROFILES["skill_recent_doc"],
    ).select(
        [{"kind": "chapter", "result_id": chid, "rrf_score": 1.0},
         {"kind": "doc_section", "result_id": sid, "rrf_score": 1.0}],
        strategy="recent_doc_anchored", now=NOW,
    )
    assert any(s.result["kind"] == "doc_section" for s in out.selected)
    assert not any(s.result["kind"] == "chapter" for s in out.selected)
    # Dropped chapter is recorded with the §8.4 reason.
    assert any(reason == "contradicted by current docs" for _, reason in out.dropped)


def test_generation_ranker_consensus_synthesis_drops_uncorroborated(catalog, populated):
    """Single-source content gets dropped under consensus-synthesis selection."""
    chid = populated["ch_with_date"]
    out = ranking.GenerationRanker(
        catalog, ranking.WEIGHT_PROFILES["skill_consensus"],
    ).select(
        [{"kind": "chapter", "result_id": chid, "rrf_score": 1.0}],
        strategy="consensus_synthesis", now=NOW,
    )
    # No alignment edges at all → corroboration = 0 → dropped.
    assert out.selected == []
    assert any(reason == "single-source (no corroboration)" for _, reason in out.dropped)


def test_generation_ranker_authority_pick_keeps_one(catalog, populated):
    """authority_pick keeps exactly one result — the highest-authority one."""
    out = ranking.GenerationRanker(
        catalog, ranking.WEIGHT_PROFILES["skill_authority"],
    ).select(
        [
            {"kind": "chapter", "result_id": populated["ch_with_date"], "rrf_score": 1.0},
            {"kind": "doc_section", "result_id": populated["doc_section_id"], "rrf_score": 1.0},
        ],
        strategy="authority_pick", now=NOW,
    )
    assert len(out.selected) == 1
    # doc_section authority (0.85) > chapter default (0.6), so doc_section wins.
    assert out.selected[0].result["kind"] == "doc_section"


def test_generation_ranker_empty_results_returns_empty_output(catalog):
    out = ranking.GenerationRanker(
        catalog, ranking.WEIGHT_PROFILES["skill_recent_doc"],
    ).select([], strategy="recent_doc_anchored")
    assert out.selected == []
    assert out.dropped == []
    assert out.strategy == "recent_doc_anchored"
