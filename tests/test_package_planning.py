"""Tests for package_planning.py — Phase 5.2 Skills Factory: package planning.

Covers:
  * _slugify — filesystem-safe naming
  * build_dependency_edges — REQUIRES edges between Skill clusters
  * topo_order — dependency-respecting ordering, with cycle handling
  * _has_doc_source_match — anchor concept ↔ registered doc_source check
  * _book_authority_stats — top-authority + book-diversity stats per Skill
  * select_strategy — three-strategy decision logic
  * find_cross_references — Skill A references Skill B when B's anchor is
    in A's concept set
  * plan_package — end-to-end orchestrator
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import package_planning as pp  # noqa: E402
from decomposition import ProposedSkill, DecompositionResult  # noqa: E402


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
    """Seed with concepts, REQUIRES edges between clusters, books with publishers, and doc_sources."""
    catalog.execute("INSERT INTO author (name) VALUES ('A1')")
    book_oreilly = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('OReillyBook', '/x', 'O''Reilly Media') RETURNING book_id"
    ).fetchone()[0]
    book_packt = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('PacktBook', '/y', 'Packt') RETURNING book_id"
    ).fetchone()[0]
    book_unknown = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('UnknownBook', '/z', 'Unknown Press') RETURNING book_id"
    ).fetchone()[0]

    # Concepts in two clusters; cluster A is "Foundation", cluster B "Application"
    concepts = ["Foundation", "Theorem", "Proof",
                "Application", "WorkedExample", "CaseStudy"]
    cmap = {}
    for n in concepts:
        cid = catalog.execute(
            "INSERT INTO concept (name, concept_type) VALUES (?, 'Concept') RETURNING concept_id",
            [n],
        ).fetchone()[0]
        cmap[n] = int(cid)

    # Cluster A internal edges (CITES — non-REQUIRES)
    def chap(book_id, num, title):
        return catalog.execute(
            "INSERT INTO chapter (book_id, chapter_num, title, content) "
            "VALUES (?, ?, ?, '...') RETURNING chapter_id",
            [book_id, num, title],
        ).fetchone()[0]

    def edge(src, dst, rt, source_chap):
        catalog.execute(
            "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
            "relation_type, confidence, source_type, source_id) "
            "VALUES (?, ?, ?, 0.9, 'chapter', ?)",
            [cmap[src], cmap[dst], rt, source_chap],
        )

    # Build chapters touching cluster A and cluster B
    ch_oreilly = chap(book_oreilly, 1, "Foundations")
    ch_packt   = chap(book_packt, 1, "Applications")
    ch_unknown = chap(book_unknown, 1, "Other")

    # Cluster A internal edges
    edge("Foundation", "Theorem", "CITES", ch_oreilly)
    edge("Theorem", "Proof", "EXTENDS", ch_oreilly)
    # Cluster B internal edges
    edge("Application", "WorkedExample", "CITES", ch_packt)
    edge("WorkedExample", "CaseStudy", "EXTENDS", ch_packt)
    # Cross-cluster REQUIRES edge: Application requires Foundation
    edge("Application", "Foundation", "REQUIRES", ch_packt)

    # Register a doc_source for Application (so cluster B → recent_doc_anchored)
    catalog.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
        "authority_score, refresh_ttl_days) "
        "VALUES ('Application', 'context7', 'context7', '/example/app', 0.85, 30)"
    )

    return {
        "cmap": cmap,
        "ch_oreilly": ch_oreilly, "ch_packt": ch_packt, "ch_unknown": ch_unknown,
        "book_oreilly": book_oreilly, "book_packt": book_packt, "book_unknown": book_unknown,
    }


def _proposed_a(cmap):
    return ProposedSkill(
        cluster_id=0,
        concept_ids=sorted([cmap["Foundation"], cmap["Theorem"], cmap["Proof"]]),
        anchor_concept_id=cmap["Foundation"],
        anchor_concept_name="Foundation",
        suggested_name="Foundation",
        top_chapters=[],
    )


def _proposed_b(cmap):
    return ProposedSkill(
        cluster_id=1,
        concept_ids=sorted([cmap["Application"], cmap["WorkedExample"], cmap["CaseStudy"]]),
        anchor_concept_id=cmap["Application"],
        anchor_concept_name="Application",
        suggested_name="Application",
        top_chapters=[],
    )


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert pp._slugify("Hello World") == "hello-world"
    assert pp._slugify("CDC with Databricks") == "cdc-with-databricks"


def test_slugify_special_chars():
    assert pp._slugify("Don't @#$ panic!") == "don-t-panic"


def test_slugify_empty_or_alnum_only():
    assert pp._slugify("") == "skill"
    assert pp._slugify("@#$%") == "skill"


def test_slugify_caps_length():
    long = "a" * 100
    assert len(pp._slugify(long, max_len=20)) <= 20


# ---------------------------------------------------------------------------
# build_dependency_edges + topo_order
# ---------------------------------------------------------------------------


def test_build_dependency_edges_finds_cross_cluster_requires(catalog, populated):
    skills = [_proposed_a(populated["cmap"]), _proposed_b(populated["cmap"])]
    edges = pp.build_dependency_edges(catalog, skills)
    # B requires A (Application requires Foundation, cluster B → cluster A)
    assert (1, 0) in edges
    # No reverse edge
    assert (0, 1) not in edges


def test_build_dependency_edges_ignores_internal_requires(catalog, populated):
    """A REQUIRES edge between two concepts in the same cluster doesn't produce
    a cluster-level edge (a cluster doesn't require itself)."""
    cmap = populated["cmap"]
    # Add a REQUIRES inside cluster A
    catalog.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, confidence, source_type, source_id) "
        "VALUES (?, ?, 'REQUIRES', 0.9, 'chapter', ?)",
        [cmap["Theorem"], cmap["Proof"], populated["ch_oreilly"]],
    )
    skills = [_proposed_a(cmap), _proposed_b(cmap)]
    edges = pp.build_dependency_edges(catalog, skills)
    # No self-edges
    assert (0, 0) not in edges
    assert (1, 1) not in edges


def test_topo_order_emits_prerequisites_first():
    """Cluster B requires cluster A → A comes first."""
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1, 2], anchor_concept_id=1,
                       anchor_concept_name="A", suggested_name="A"),
        ProposedSkill(cluster_id=1, concept_ids=[3, 4], anchor_concept_id=3,
                       anchor_concept_name="B", suggested_name="B"),
    ]
    edges = [(1, 0)]  # B requires A
    order = pp.topo_order(skills, edges)
    assert order == [0, 1]


def test_topo_order_no_edges_preserves_input_order():
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1], anchor_concept_id=1,
                       anchor_concept_name="A", suggested_name="A"),
        ProposedSkill(cluster_id=1, concept_ids=[2], anchor_concept_id=2,
                       anchor_concept_name="B", suggested_name="B"),
        ProposedSkill(cluster_id=2, concept_ids=[3], anchor_concept_id=3,
                       anchor_concept_name="C", suggested_name="C"),
    ]
    order = pp.topo_order(skills, [])
    assert order == [0, 1, 2]


def test_topo_order_breaks_cycles_gracefully(caplog):
    """Cycle: A requires B, B requires A. Both should still appear in output."""
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1, 2], anchor_concept_id=1,
                       anchor_concept_name="A", suggested_name="A"),
        ProposedSkill(cluster_id=1, concept_ids=[3], anchor_concept_id=3,
                       anchor_concept_name="B", suggested_name="B"),
    ]
    edges = [(0, 1), (1, 0)]
    with caplog.at_level("WARNING"):
        order = pp.topo_order(skills, edges)
    assert sorted(order) == [0, 1]
    assert any("cycle" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# _has_doc_source_match
# ---------------------------------------------------------------------------


def test_has_doc_source_match_returns_doc_name(catalog, populated):
    sk = ProposedSkill(cluster_id=0, concept_ids=[],
                       anchor_concept_id=1, anchor_concept_name="Application")
    assert pp._has_doc_source_match(catalog, sk) == "Application"


def test_has_doc_source_match_no_anchor(catalog, populated):
    sk = ProposedSkill(cluster_id=0, concept_ids=[],
                       anchor_concept_id=None, anchor_concept_name=None)
    assert pp._has_doc_source_match(catalog, sk) is None


def test_has_doc_source_match_unrelated(catalog, populated):
    sk = ProposedSkill(cluster_id=0, concept_ids=[],
                       anchor_concept_id=1, anchor_concept_name="Foundation")
    assert pp._has_doc_source_match(catalog, sk) is None


# ---------------------------------------------------------------------------
# _book_authority_stats
# ---------------------------------------------------------------------------


def test_book_authority_stats_distinct_books(catalog, populated):
    """Skill with 3 distinct books — distinct_books == 3."""
    cmap = populated["cmap"]
    sk = ProposedSkill(
        cluster_id=0, concept_ids=[cmap["Foundation"]],
        anchor_concept_id=cmap["Foundation"], anchor_concept_name="Foundation",
        top_chapters=[
            {"chapter_id": populated["ch_oreilly"], "concept_hits": 3, "mention_count": 5,
             "book_title": "OReillyBook", "chapter_title": "X"},
            {"chapter_id": populated["ch_packt"], "concept_hits": 2, "mention_count": 3,
             "book_title": "PacktBook", "chapter_title": "Y"},
            {"chapter_id": populated["ch_unknown"], "concept_hits": 1, "mention_count": 1,
             "book_title": "UnknownBook", "chapter_title": "Z"},
        ],
    )
    stats = pp._book_authority_stats(catalog, sk)
    assert stats["distinct_books"] == 3
    # O'Reilly authority is 0.85 (top), then Packt 0.65, then unknown 0.6
    assert stats["top_authority"] == pytest.approx(0.85, abs=0.01)
    assert stats["top_book_title"] == "OReillyBook"


def test_book_authority_stats_no_chapters(catalog):
    sk = ProposedSkill(cluster_id=0, concept_ids=[], anchor_concept_id=1,
                       anchor_concept_name="X", top_chapters=[])
    stats = pp._book_authority_stats(catalog, sk)
    assert stats["distinct_books"] == 0
    assert stats["top_authority"] is None


# ---------------------------------------------------------------------------
# select_strategy
# ---------------------------------------------------------------------------


def test_select_strategy_recent_doc_anchored_when_doc_source_matches(catalog, populated):
    cmap = populated["cmap"]
    sk = _proposed_b(cmap)  # Application matches doc_source "Application"
    strategy, rationale = pp.select_strategy(catalog, sk)
    assert strategy == pp.STRATEGY_RECENT_DOC
    assert "doc_source" in rationale


def test_select_strategy_authority_pick_for_single_dominant_book(catalog, populated):
    """One O'Reilly chapter, no other books → authority_pick."""
    cmap = populated["cmap"]
    sk = ProposedSkill(
        cluster_id=0, concept_ids=[cmap["Foundation"]],
        anchor_concept_id=cmap["Foundation"], anchor_concept_name="Foundation",
        top_chapters=[
            {"chapter_id": populated["ch_oreilly"], "concept_hits": 3, "mention_count": 5,
             "book_title": "OReillyBook", "chapter_title": "X"},
        ],
    )
    strategy, rationale = pp.select_strategy(catalog, sk)
    assert strategy == pp.STRATEGY_AUTHORITY


def test_select_strategy_consensus_for_multiple_books(catalog, populated):
    """3 distinct books, no doc_source match, no single dominant → consensus."""
    cmap = populated["cmap"]
    # Add one more book to clear the threshold
    book_extra = catalog.execute(
        "INSERT INTO book (title, source_path, publisher) "
        "VALUES ('ExtraBook', '/x4', 'Manning Publications') RETURNING book_id"
    ).fetchone()[0]
    ch_extra = catalog.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'Extra', '...') RETURNING chapter_id",
        [book_extra],
    ).fetchone()[0]
    sk = ProposedSkill(
        cluster_id=0, concept_ids=[cmap["Foundation"]],
        anchor_concept_id=cmap["Foundation"], anchor_concept_name="Foundation",
        top_chapters=[
            {"chapter_id": populated["ch_oreilly"], "concept_hits": 3, "mention_count": 5,
             "book_title": "OReillyBook", "chapter_title": "X"},
            {"chapter_id": populated["ch_packt"], "concept_hits": 2, "mention_count": 3,
             "book_title": "PacktBook", "chapter_title": "Y"},
            {"chapter_id": ch_extra, "concept_hits": 2, "mention_count": 4,
             "book_title": "ExtraBook", "chapter_title": "Z"},
        ],
    )
    strategy, rationale = pp.select_strategy(catalog, sk)
    assert strategy == pp.STRATEGY_CONSENSUS
    assert "distinct" in rationale


def test_select_strategy_default_consensus_when_no_chapters(catalog, populated):
    cmap = populated["cmap"]
    sk = ProposedSkill(
        cluster_id=0, concept_ids=[cmap["Foundation"]],
        anchor_concept_id=cmap["Foundation"], anchor_concept_name="Foundation",
        top_chapters=[],
    )
    strategy, rationale = pp.select_strategy(catalog, sk)
    assert strategy == pp.STRATEGY_CONSENSUS
    assert "default" in rationale or "no clear" in rationale


# ---------------------------------------------------------------------------
# find_cross_references
# ---------------------------------------------------------------------------


def test_find_cross_references_links_when_anchor_appears_in_other():
    """B's anchor (id=3) appears in A's concept_ids → A references B."""
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1, 2, 3],
                       anchor_concept_id=1, anchor_concept_name="A"),
        ProposedSkill(cluster_id=1, concept_ids=[3, 4, 5],
                       anchor_concept_id=3, anchor_concept_name="B"),
    ]
    refs = pp.find_cross_references(skills)
    assert refs[0] == [1]   # A references B
    assert 1 not in refs    # B doesn't reference A (A's anchor=1 not in B's concepts)


def test_find_cross_references_disjoint_clusters_have_no_refs():
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1, 2],
                       anchor_concept_id=1, anchor_concept_name="A"),
        ProposedSkill(cluster_id=1, concept_ids=[3, 4],
                       anchor_concept_id=3, anchor_concept_name="B"),
    ]
    refs = pp.find_cross_references(skills)
    assert refs == {}


def test_find_cross_references_no_self_reference():
    """A Skill's anchor is in its own concept_ids; ensure self isn't listed."""
    skills = [
        ProposedSkill(cluster_id=0, concept_ids=[1, 2],
                       anchor_concept_id=1, anchor_concept_name="A"),
    ]
    refs = pp.find_cross_references(skills)
    assert refs == {}


# ---------------------------------------------------------------------------
# plan_package — end-to-end
# ---------------------------------------------------------------------------


def test_plan_package_orders_by_dependency(catalog, populated):
    cmap = populated["cmap"]
    decomp = DecompositionResult(
        query="Application engineering",
        anchor_concept_ids=[cmap["Foundation"], cmap["Application"]],
        proposed_skills=[_proposed_a(cmap), _proposed_b(cmap)],
    )
    plan = pp.plan_package(decomp, catalog, package_name="testpkg")
    assert plan.package_name == "testpkg"
    assert plan.domain == "Application engineering"
    assert len(plan.planned_skills) == 2
    # Foundation should come first (cluster B requires cluster A)
    assert plan.planned_skills[0].proposed.cluster_id == 0
    assert plan.planned_skills[1].proposed.cluster_id == 1
    assert plan.planned_skills[0].order == 0
    assert plan.planned_skills[1].order == 1
    # Cluster B requires cluster A
    assert plan.planned_skills[1].requires_cluster_ids == [0]


def test_plan_package_assigns_strategy_per_skill(catalog, populated):
    cmap = populated["cmap"]
    decomp = DecompositionResult(
        query="Application engineering",
        anchor_concept_ids=[cmap["Foundation"], cmap["Application"]],
        proposed_skills=[_proposed_a(cmap), _proposed_b(cmap)],
    )
    plan = pp.plan_package(decomp, catalog, package_name="t")
    # Cluster A (Foundation): no doc_source, no chapters → default consensus
    a_planned = next(p for p in plan.planned_skills if p.proposed.cluster_id == 0)
    assert a_planned.strategy == pp.STRATEGY_CONSENSUS
    # Cluster B (Application): doc_source matches → recent_doc_anchored
    b_planned = next(p for p in plan.planned_skills if p.proposed.cluster_id == 1)
    assert b_planned.strategy == pp.STRATEGY_RECENT_DOC


def test_plan_package_generates_unique_folder_names(catalog, populated):
    cmap = populated["cmap"]
    decomp = DecompositionResult(
        query="x",
        proposed_skills=[_proposed_a(cmap), _proposed_b(cmap)],
    )
    plan = pp.plan_package(decomp, catalog)
    folders = [p.folder_name for p in plan.planned_skills]
    assert len(folders) == len(set(folders))  # all unique
    assert all(folders)  # non-empty


def test_plan_package_empty_decomposition():
    plan = pp.plan_package(
        DecompositionResult(query="x", proposed_skills=[]),
        conn=None,  # not consulted on empty path
    )
    assert plan.planned_skills == []
    assert "empty" in plan.notes.lower()


def test_plan_package_default_name_from_query(catalog, populated):
    cmap = populated["cmap"]
    decomp = DecompositionResult(
        query="CDC with Databricks",
        proposed_skills=[_proposed_a(cmap)],
    )
    plan = pp.plan_package(decomp, catalog)
    assert plan.package_name == "cdc-with-databricks"
    assert "cdc-with-databricks" in plan.folder_root


def test_plan_package_no_skill_requires_itself(catalog, populated):
    """Sanity: a Skill must never list its own cluster_id in
    requires_cluster_ids or references_cluster_ids — that would mean
    we're telling Phase 5.3 to wait for itself."""
    cmap = populated["cmap"]
    decomp = DecompositionResult(
        query="x",
        proposed_skills=[_proposed_a(cmap), _proposed_b(cmap)],
    )
    plan = pp.plan_package(decomp, catalog)
    for ps in plan.planned_skills:
        own = ps.proposed.cluster_id
        assert own not in ps.requires_cluster_ids, (
            f"cluster {own} requires itself: {ps.requires_cluster_ids}"
        )
        assert own not in ps.references_cluster_ids, (
            f"cluster {own} references itself: {ps.references_cluster_ids}"
        )
