"""Tests for concept_map.py — Phase 7.2 Concept Neighborhood Map.

End-to-end tests using a tiny seeded concept graph:

  Event Sourcing → REQUIRES → Aggregate
  Event Sourcing → EXTENDS  → Domain-Driven Design
  CQRS          → REQUIRES → Event Sourcing
  Aggregate     → REQUIRES → Domain-Driven Design

Plus a "high-fan-out" graph for pruning tests.
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

import concept_map as cm  # noqa: E402
import generator as gen  # noqa: E402


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


def _add_rel(conn, src, tgt, rel_type, src_chap=1):
    """concept_relation row; bumps source_id to dodge PK collisions."""
    _REL_COUNTER[0] += 1
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, _REL_COUNTER[0]],
    )


@pytest.fixture
def small_graph(catalog):
    """Four-concept graph centered on Event Sourcing."""
    es = _seed_concept(catalog, "Event Sourcing", "Pattern", "ES desc")
    ag = _seed_concept(catalog, "Aggregate", "Pattern", "Agg desc")
    ddd = _seed_concept(catalog, "Domain-Driven Design", "Concept", "DDD desc")
    cqrs = _seed_concept(catalog, "CQRS", "Pattern", "CQRS desc")
    _add_rel(catalog, es, ag, "REQUIRES")
    _add_rel(catalog, es, ddd, "EXTENDS")
    _add_rel(catalog, cqrs, es, "REQUIRES")
    _add_rel(catalog, ag, ddd, "REQUIRES")
    return {"es": es, "ag": ag, "ddd": ddd, "cqrs": cqrs}


def _resolver_for(conn, name_to_id: dict[str, int]):
    r = MagicMock()
    def lookup(name):
        return name_to_id.get(name.strip().lower())
    r.resolve_lookup_only.side_effect = lookup
    return r


# ---------------------------------------------------------------------------
# KHopDecomposer
# ---------------------------------------------------------------------------


def test_decomposer_seed_at_depth_zero(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    assert d.seed_concept_id == small_graph["es"]
    seed = next(n for n in d.nodes if n.concept_id == small_graph["es"])
    assert seed.depth == 0


def test_decomposer_depth_1_returns_direct_neighbors(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    ids = {n.concept_id for n in d.nodes}
    # Direct neighbors of ES: ag (REQUIRES), ddd (EXTENDS), cqrs (REQUIRES from cqrs)
    assert ids == {
        small_graph["es"], small_graph["ag"],
        small_graph["ddd"], small_graph["cqrs"],
    }


def test_decomposer_depth_2_follows_transitive(catalog, small_graph):
    """ag→ddd is at depth 2 from ES through the ag bridge — but ddd is also
    at depth 1 directly. Shortest depth wins."""
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=2)
    ddd_node = next(n for n in d.nodes if n.concept_id == small_graph["ddd"])
    assert ddd_node.depth == 1


def test_decomposer_unknown_seed_returns_empty(catalog, small_graph):
    resolver = _resolver_for(catalog, {})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Nonexistent", depth=2)
    assert d.seed_concept_id == -1
    assert d.nodes == [] and d.edges == []
    assert any("not found" in n for n in d.notes)


def test_decomposer_relation_filter_restricts_traversal(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(
        catalog, resolver, "Event Sourcing",
        depth=2, relation_filter=("REQUIRES",),
    )
    # With REQUIRES-only, ddd (only reachable via EXTENDS or via ag→ddd
    # REQUIRES) should still appear via ag.
    ids = {n.concept_id for n in d.nodes}
    assert small_graph["ddd"] in ids  # via ag→ddd REQUIRES
    # But the EXTENDS-only edge between es and ddd shouldn't generate
    # a direct neighbor entry (depth would still come through ag).
    edge_types = {e.relation_type for e in d.edges}
    assert "EXTENDS" not in edge_types


def test_decomposer_prunes_high_fan_out(catalog):
    """A seed with 100 direct neighbors should prune to max_nodes."""
    seed = _seed_concept(catalog, "Hub")
    children = [_seed_concept(catalog, f"Child {i}") for i in range(100)]
    for c in children:
        _add_rel(catalog, seed, c, "CITES")
    resolver = _resolver_for(catalog, {"hub": seed})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Hub", depth=1, max_nodes=20)
    assert len(d.nodes) == 20
    assert d.pruned_node_count > 0
    assert any("pruned" in n for n in d.notes)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_mermaid_includes_seed_marker(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    mmd = cm._mermaid(d)
    assert mmd.startswith("graph LR")
    assert f"⭐ Event Sourcing" in mmd
    # Every edge appears
    assert "REQUIRES" in mmd
    assert "EXTENDS" in mmd


def test_mermaid_handles_empty_graph():
    empty = cm._Decomposition(
        seed_concept_id=-1, seed_name="x", depth=2,
        relation_filter=("REQUIRES",),
        nodes=[], edges=[], pruned_node_count=0,
    )
    mmd = cm._mermaid(empty)
    assert "(no concepts)" in mmd


def test_dot_renders_valid_digraph(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    dot = cm._dot(d)
    assert dot.startswith("digraph G {")
    assert dot.rstrip().endswith("}")
    assert "rankdir=" in dot


def test_nodes_csv_has_header_and_rows(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    csv_text = cm._nodes_csv(d)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("concept_id,name")
    assert len(lines) == 1 + len(d.nodes)


def test_overview_md_lists_pruning_when_present(catalog):
    """Pruned-node count is surfaced in _map.md."""
    seed = _seed_concept(catalog, "Hub")
    for i in range(60):
        c = _seed_concept(catalog, f"C{i}")
        _add_rel(catalog, seed, c, "CITES")
    resolver = _resolver_for(catalog, {"hub": seed})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Hub", depth=1, max_nodes=20)
    md = cm._overview_md(d)
    assert "Pruned nodes" in md


# ---------------------------------------------------------------------------
# Planner + Validator
# ---------------------------------------------------------------------------


def test_planner_produces_one_unit_per_node(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    plan = cm.ConceptMapPlanner().plan(catalog, d, package_name="test-map")
    assert plan.generator_type == "concept_map"
    assert plan.package_name == "test-map"
    assert len(plan.units) == len(d.nodes)
    # Files: mmd, dot, csv, md
    fname = {f.filename for f in plan.files}
    assert fname == {"neighborhood.mmd", "neighborhood.dot", "nodes.csv", "_map.md"}


def test_validator_flags_missing_concept_id(catalog, small_graph):
    """If a unit references a concept_id that doesn't exist, validator
    emits an error."""
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    plan = cm.ConceptMapPlanner().plan(catalog, d)
    # Inject a phantom concept reference.
    plan.units[0].metadata["concept_id"] = 999_999
    issues = cm.ConceptMapValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "999999" in i.message for i in issues)


def test_validator_passes_for_valid_plan(catalog, small_graph):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    d = cm.KHopDecomposer().decompose(catalog, resolver, "Event Sourcing", depth=1)
    plan = cm.ConceptMapPlanner().plan(catalog, d)
    issues = cm.ConceptMapValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_catches_undeclared_mermaid_node():
    plan = gen.GenPlan(
        generator_type="concept_map", package_name="x", domain="x",
        package_metadata={"seed_concept_id": 1},
        units=[gen.GenUnit(
            unit_type="concept_node", name="A",
            metadata={"concept_id": 1}, logical_key="node_1",
        )],
        files=[gen.GenFile(
            filename="neighborhood.mmd", purpose="mermaid",
            # Edge references c2 but only c1 declared.
            content='graph LR\n  c1["A"]\n  c1 ==>|REQUIRES| c2\n',
        )],
    )
    issues = cm._validate_mermaid(plan.files[0].content)
    assert any("undeclared" in i.message for i in issues)


# ---------------------------------------------------------------------------
# Materializer + end-to-end Generator.run_deterministic
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_files_and_persists(catalog, small_graph, tmp_path):
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    g = cm.make_concept_map_generator()
    pid, report, issues = g.run_deterministic(
        catalog, resolver, "Event Sourcing",
        depth=1, output_root=str(tmp_path),
    )
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    assert pid > 0

    # Files on disk
    for fname in ("neighborhood.mmd", "neighborhood.dot", "nodes.csv", "_map.md"):
        assert (tmp_path / "event-sourcing" / fname).exists()

    # Catalog rows
    n_units = catalog.execute(
        "SELECT COUNT(*) FROM generated_unit WHERE package_id = ?", [pid],
    ).fetchone()[0]
    assert n_units == 4


def test_run_deterministic_idempotent(catalog, small_graph, tmp_path):
    """Re-running with the same seed should land in the same package_id
    and produce identical files."""
    resolver = _resolver_for(catalog, {"event sourcing": small_graph["es"]})
    g = cm.make_concept_map_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, resolver, "Event Sourcing", depth=1, output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, resolver, "Event Sourcing", depth=1, output_root=str(tmp_path),
    )
    assert pid1 == pid2


def test_run_deterministic_unknown_seed_returns_negative_id(catalog, small_graph, tmp_path):
    resolver = _resolver_for(catalog, {})
    g = cm.make_concept_map_generator()
    pid, report, issues = g.run_deterministic(
        catalog, resolver, "ghost concept", output_root=str(tmp_path),
    )
    assert pid == -1
    assert any(i.severity == "error" or i.severity == "warning" for i in issues)
