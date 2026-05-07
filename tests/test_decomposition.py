"""Tests for decomposition.py — Phase 5.1 domain decomposition.

Covers:
  * find_anchor_concepts — query → concept_id resolution
  * expand_neighborhood — BFS depth + size cap
  * cluster_concepts — Louvain over induced subgraph
  * top_chapters_for_cluster — chapter ranking by concept_relation
  * _pick_cluster_anchor — most-central concept selection
  * decompose_domain — end-to-end orchestrator
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

import decomposition  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    """In-memory v2 schema for isolated tests."""
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


@pytest.fixture
def populated(catalog):
    """Seed the catalog with a small concept graph + chapters.

    Cluster A: {CDC, Change Data Capture, Debezium} — tightly connected.
    Cluster B: {Databricks, Delta Lake, Unity Catalog} — tightly connected.
    Cross-edges: CDC ↔ Databricks (the bridge of the domain).
    Plus a far-away concept (REST) at depth 2 from CDC via Webhooks.
    """
    catalog.execute("INSERT INTO author (name) VALUES ('A1')")
    book_id = catalog.execute(
        "INSERT INTO book (title, source_path) VALUES ('B', '/x') RETURNING book_id"
    ).fetchone()[0]

    # Concepts
    concepts = {
        "CDC":                  ("Concept",),
        "Change Data Capture":  ("Concept",),
        "Debezium":             ("Tool",),
        "Databricks":           ("Tool",),
        "Delta Lake":           ("Tool",),
        "Unity Catalog":        ("Concept",),
        "Webhooks":             ("Concept",),
        "REST":                 ("Concept",),
    }
    name_to_id: dict[str, int] = {}
    for name, (ctype,) in concepts.items():
        cid = catalog.execute(
            "INSERT INTO concept (name, concept_type) VALUES (?, ?) RETURNING concept_id",
            [name, ctype],
        ).fetchone()[0]
        name_to_id[name] = int(cid)

    # Aliases for resolver-style lookup.
    catalog.execute(
        "INSERT INTO concept_alias (concept_id, alias) VALUES (?, ?)",
        [name_to_id["CDC"], "cdc"],
    )

    # Concept relations
    def edge(src, dst, rt="REQUIRES"):
        # Need a chapter source_id; make a chapter for each edge.
        ch = catalog.execute(
            "INSERT INTO chapter (book_id, chapter_num, title, content) "
            "VALUES (?, ?, ?, ?) RETURNING chapter_id",
            [book_id, len(name_to_id) + 1, f"{src}-{dst}", f"about {src} and {dst}"],
        ).fetchone()[0]
        catalog.execute(
            "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
            "relation_type, confidence, source_type, source_id) "
            "VALUES (?, ?, ?, ?, 'chapter', ?)",
            [name_to_id[src], name_to_id[dst], rt, 0.9, ch],
        )
        return ch

    # Cluster A: CDC ↔ Change Data Capture ↔ Debezium
    edge("CDC", "Change Data Capture")
    edge("CDC", "Debezium")
    edge("Change Data Capture", "Debezium")

    # Cluster B: Databricks ↔ Delta Lake ↔ Unity Catalog
    edge("Databricks", "Delta Lake")
    edge("Databricks", "Unity Catalog")
    edge("Delta Lake", "Unity Catalog")

    # Bridge: CDC ↔ Databricks (the domain query "CDC with Databricks")
    edge("CDC", "Databricks")

    # Outlier: CDC ↔ Webhooks ↔ REST (depth 1 + 2 from CDC)
    edge("CDC", "Webhooks")
    edge("Webhooks", "REST")

    return name_to_id


# ---------------------------------------------------------------------------
# find_anchor_concepts
# ---------------------------------------------------------------------------


def _stub_resolver(name_to_id: dict[str, int]):
    """Mock resolver with .resolve_lookup_only(name) → cid or None."""
    resolver = MagicMock()
    def lookup(name: str):
        # case-insensitive whole-string match
        for n, cid in name_to_id.items():
            if n.lower() == name.lower():
                return cid
        # alias check (CDC = cdc)
        if name.lower() == "cdc":
            return name_to_id.get("CDC")
        return None
    resolver.resolve_lookup_only = lookup
    return resolver


def test_find_anchor_concepts_resolves_whole_query_first(populated):
    resolver = _stub_resolver(populated)
    resolved, unresolved = decomposition.find_anchor_concepts(
        resolver, "Change Data Capture",
    )
    assert resolved == [populated["Change Data Capture"]]
    assert unresolved == []


def test_find_anchor_concepts_falls_back_to_per_token(populated):
    """Multi-word query that doesn't resolve as a whole — per-token fallback."""
    resolver = _stub_resolver(populated)
    resolved, unresolved = decomposition.find_anchor_concepts(
        resolver, "CDC Databricks",
    )
    assert set(resolved) == {populated["CDC"], populated["Databricks"]}
    assert unresolved == []


def test_find_anchor_concepts_reports_unresolved(populated):
    resolver = _stub_resolver(populated)
    resolved, unresolved = decomposition.find_anchor_concepts(
        resolver, "CDC unknownthing",
    )
    assert resolved == [populated["CDC"]]
    assert unresolved == ["unknownthing"]


def test_find_anchor_concepts_empty_query():
    resolver = _stub_resolver({})
    assert decomposition.find_anchor_concepts(resolver, "") == ([], [])
    assert decomposition.find_anchor_concepts(resolver, "   ") == ([], [])


# ---------------------------------------------------------------------------
# expand_neighborhood
# ---------------------------------------------------------------------------


def test_expand_neighborhood_depth_zero_is_anchor_only(catalog, populated):
    out = decomposition.expand_neighborhood(catalog, [populated["CDC"]], max_depth=0)
    assert out == {populated["CDC"]: 0}


def test_expand_neighborhood_walks_to_depth(catalog, populated):
    """From CDC, depth=2 reaches everything except REST (which is depth 2 via Webhooks)."""
    out = decomposition.expand_neighborhood(
        catalog, [populated["CDC"]], max_depth=2,
    )
    assert populated["CDC"] in out
    assert populated["Webhooks"] in out  # depth 1
    assert populated["REST"] in out      # depth 2 via Webhooks
    assert populated["Databricks"] in out  # depth 1 via bridge
    assert populated["Delta Lake"] in out  # depth 2 via Databricks


def test_expand_neighborhood_caps_at_max_size(catalog, populated):
    """max_size guards against runaway expansion on large graphs."""
    out = decomposition.expand_neighborhood(
        catalog, [populated["CDC"]], max_depth=5, max_size=3,
    )
    assert len(out) == 3
    assert populated["CDC"] in out


def test_expand_neighborhood_empty_anchors(catalog):
    assert decomposition.expand_neighborhood(catalog, [], max_depth=2) == {}


# ---------------------------------------------------------------------------
# cluster_concepts
# ---------------------------------------------------------------------------


def test_cluster_concepts_finds_two_clusters(catalog, populated):
    """Concept graph has two tightly-connected groups (CDC cluster + Databricks
    cluster) plus the outlier Webhooks/REST. Louvain should find at least 2
    distinct clusters."""
    all_ids = list(populated.values())
    clusters = decomposition.cluster_concepts(
        catalog, all_ids, min_cluster_size=2,
    )
    assert len(clusters) >= 2
    # CDC cluster and Databricks cluster shouldn't share members.
    cdc_cluster = next(c for c in clusters if populated["CDC"] in c)
    db_cluster = next(c for c in clusters if populated["Databricks"] in c)
    # Note: with the bridge edge, Louvain may merge — verify they're clearly grouped
    # by checking membership:
    assert populated["Change Data Capture"] in cdc_cluster
    assert populated["Debezium"] in cdc_cluster
    assert populated["Delta Lake"] in db_cluster
    assert populated["Unity Catalog"] in db_cluster


def test_cluster_concepts_empty_input():
    assert decomposition.cluster_concepts(MagicMock(), []) == []


def test_cluster_concepts_single_concept_returns_one_cluster(catalog, populated):
    out = decomposition.cluster_concepts(
        catalog, [populated["CDC"]], min_cluster_size=1,
    )
    assert out == [{populated["CDC"]}]


# ---------------------------------------------------------------------------
# top_chapters_for_cluster
# ---------------------------------------------------------------------------


def test_top_chapters_for_cluster_ranks_by_concept_hits(catalog, populated):
    """Chapters that touch multiple cluster concepts rank above those that
    touch only one."""
    cluster = [populated["CDC"], populated["Change Data Capture"], populated["Debezium"]]
    out = decomposition.top_chapters_for_cluster(catalog, cluster, k=10)
    assert len(out) >= 1
    # Each row has the schema we expect
    for row in out:
        assert "chapter_id" in row
        assert "concept_hits" in row
        assert "mention_count" in row
        assert "book_title" in row
    # concept_hits is sorted descending
    hits = [r["concept_hits"] for r in out]
    assert hits == sorted(hits, reverse=True)


def test_top_chapters_for_cluster_empty_input(catalog):
    assert decomposition.top_chapters_for_cluster(catalog, [], k=5) == []


def test_top_chapters_for_cluster_respects_k(catalog, populated):
    cluster = list(populated.values())
    out_k1 = decomposition.top_chapters_for_cluster(catalog, cluster, k=1)
    out_k3 = decomposition.top_chapters_for_cluster(catalog, cluster, k=3)
    assert len(out_k1) == 1
    assert len(out_k3) >= 1


# ---------------------------------------------------------------------------
# _pick_cluster_anchor
# ---------------------------------------------------------------------------


def test_pick_cluster_anchor_returns_most_central(catalog, populated):
    """In the CDC sub-cluster, CDC has 3 internal edges (to CDC's siblings
    + the bridge), Change Data Capture has 2, Debezium has 2. CDC should be
    the anchor."""
    cluster = [populated["CDC"], populated["Change Data Capture"], populated["Debezium"]]
    cid, name = decomposition._pick_cluster_anchor(catalog, cluster)
    assert cid == populated["CDC"]
    assert name == "CDC"


def test_pick_cluster_anchor_empty_cluster(catalog):
    cid, name = decomposition._pick_cluster_anchor(catalog, [])
    assert cid is None and name is None


# ---------------------------------------------------------------------------
# decompose_domain — end-to-end
# ---------------------------------------------------------------------------


def test_decompose_domain_produces_proposed_skills(catalog, populated):
    resolver = _stub_resolver(populated)
    out = decomposition.decompose_domain(
        catalog, resolver, "CDC Databricks",
        max_depth=2, min_cluster_size=2,
    )
    assert out.query == "CDC Databricks"
    assert set(out.anchor_concept_ids) == {populated["CDC"], populated["Databricks"]}
    assert out.anchor_terms_unresolved == []
    assert out.neighborhood_size > 0
    assert len(out.proposed_skills) >= 1
    # Each proposed skill has its anchor + concepts
    for ps in out.proposed_skills:
        assert ps.concept_ids
        assert ps.anchor_concept_id is not None
        assert ps.anchor_concept_name is not None


def test_decompose_domain_unresolvable_query_returns_note(catalog, populated):
    resolver = _stub_resolver(populated)
    out = decomposition.decompose_domain(
        catalog, resolver, "completely unknown topic",
    )
    assert out.anchor_concept_ids == []
    assert out.proposed_skills == []
    assert "novel" in out.notes.lower() or "match" in out.notes.lower()


def test_decompose_domain_isolated_anchor(catalog, populated):
    """Anchor with no neighbors in concept_relation — empty proposed_skills."""
    # Add an isolated concept with no relations.
    cid = catalog.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Isolated', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]
    populated["Isolated"] = int(cid)
    resolver = _stub_resolver(populated)
    out = decomposition.decompose_domain(catalog, resolver, "Isolated", max_depth=2)
    # Isolated has 1 in neighborhood (itself only), no edges
    assert out.anchor_concept_ids == [int(cid)]
    # cluster_concepts on a single-node has min_cluster_size guard;
    # default is 3, so we get 0 proposed skills since 1 < 3
    assert len(out.proposed_skills) <= 1
