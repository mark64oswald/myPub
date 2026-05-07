"""Tests for generator.py — Phase 7 generalized generator framework.

Covers the persistence path (``persist``, ``upsert_package``,
``clear_prior_units``) using an in-memory schema-loaded catalog.
The Generator class itself is exercised end-to-end via
test_concept_map.py — here we focus on the table-writing primitives.
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

import generator as gen  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _make_plan() -> gen.GenPlan:
    return gen.GenPlan(
        generator_type="concept_map",
        package_name="test-pkg",
        domain="testing",
        source_query="testing query",
        package_metadata={"depth": 2, "max_nodes": 50},
        units=[
            gen.GenUnit(
                unit_type="concept_node",
                name="Root Concept",
                ordinal=0,
                content_markdown="root description",
                metadata={"concept_id": 100, "is_seed": True},
                logical_key="node_100",
                sources=[("concept", 100, 1.0, 1.0, None)],
            ),
            gen.GenUnit(
                unit_type="concept_node",
                name="Child A",
                ordinal=1,
                parent_unit_key="node_100",
                content_markdown="child A desc",
                metadata={"concept_id": 101, "depth": 1},
                logical_key="node_101",
                sources=[("concept", 101, 1.0, 1.0, None)],
            ),
        ],
        files=[
            gen.GenFile(filename="neighborhood.mmd",
                        content="graph LR\n  c100[\"Root\"]\n  c101[\"Child\"]\n",
                        purpose="mermaid"),
            gen.GenFile(filename="_map.md", content="# Test", purpose="overview"),
        ],
    )


# ---------------------------------------------------------------------------
# upsert_package
# ---------------------------------------------------------------------------


def test_upsert_package_inserts_when_absent(catalog):
    plan = _make_plan()
    pid = gen.upsert_package(catalog, plan)
    assert pid is not None
    row = catalog.execute(
        "SELECT generator_type, name, source_query FROM generated_package "
        "WHERE package_id = ?", [pid],
    ).fetchone()
    assert row == ("concept_map", "test-pkg", "testing query")


def test_upsert_package_returns_existing_id_on_re_run(catalog):
    plan = _make_plan()
    pid1 = gen.upsert_package(catalog, plan)
    pid2 = gen.upsert_package(catalog, plan)
    assert pid1 == pid2


def test_upsert_package_distinguishes_by_generator_type(catalog):
    """Same name under different generator_type creates separate rows
    (the UNIQUE constraint is on the pair)."""
    plan_a = _make_plan()
    plan_a.generator_type = "concept_map"
    plan_b = _make_plan()
    plan_b.generator_type = "learning_path"
    pid_a = gen.upsert_package(catalog, plan_a)
    pid_b = gen.upsert_package(catalog, plan_b)
    assert pid_a != pid_b


# ---------------------------------------------------------------------------
# persist + clear_prior_units
# ---------------------------------------------------------------------------


def test_persist_writes_units_and_files(catalog):
    plan = _make_plan()
    pid = gen.persist(catalog, plan)
    n_units = catalog.execute(
        "SELECT COUNT(*) FROM generated_unit WHERE package_id = ?", [pid],
    ).fetchone()[0]
    n_files = catalog.execute(
        "SELECT COUNT(*) FROM generated_file WHERE package_id = ?", [pid],
    ).fetchone()[0]
    n_sources = catalog.execute(
        "SELECT COUNT(*) FROM generated_source gs "
        "JOIN generated_unit u ON u.unit_id = gs.unit_id WHERE u.package_id = ?",
        [pid],
    ).fetchone()[0]
    assert n_units == 2
    assert n_files == 2
    assert n_sources == 2


def test_persist_wires_parent_unit_id(catalog):
    plan = _make_plan()
    pid = gen.persist(catalog, plan)
    # Find Child A; its parent_unit_id should point to Root Concept.
    rows = catalog.execute(
        """
        SELECT u.name, p.name AS parent_name
          FROM generated_unit u
          LEFT JOIN generated_unit p ON p.unit_id = u.parent_unit_id
         WHERE u.package_id = ?
         ORDER BY u.ordinal
        """,
        [pid],
    ).fetchall()
    assert rows[0] == ("Root Concept", None)
    assert rows[1] == ("Child A", "Root Concept")


def test_persist_serializes_metadata_json(catalog):
    plan = _make_plan()
    pid = gen.persist(catalog, plan)
    pkg_meta = catalog.execute(
        "SELECT metadata_json FROM generated_package WHERE package_id = ?", [pid],
    ).fetchone()[0]
    assert pkg_meta is not None
    import json
    assert json.loads(pkg_meta) == {"depth": 2, "max_nodes": 50}
    unit_meta_rows = catalog.execute(
        "SELECT metadata_json FROM generated_unit WHERE package_id = ? "
        "ORDER BY ordinal",
        [pid],
    ).fetchall()
    assert json.loads(unit_meta_rows[0][0])["is_seed"] is True


def test_persist_is_idempotent_replaces_prior_units(catalog):
    plan = _make_plan()
    gen.persist(catalog, plan)
    # Modify the plan's units and re-persist
    plan.units = [
        gen.GenUnit(
            unit_type="concept_node", name="Replaced", ordinal=0,
            metadata={"concept_id": 999}, logical_key="node_999",
            sources=[("concept", 999, 1.0, 1.0, None)],
        ),
    ]
    plan.files = [gen.GenFile(filename="new.mmd", content="graph LR\n", purpose="mermaid")]
    pid = gen.persist(catalog, plan)
    rows = catalog.execute(
        "SELECT name FROM generated_unit WHERE package_id = ?", [pid],
    ).fetchall()
    assert [r[0] for r in rows] == ["Replaced"]
    files = catalog.execute(
        "SELECT filename FROM generated_file WHERE package_id = ?", [pid],
    ).fetchall()
    assert [r[0] for r in files] == ["new.mmd"]


def test_clear_prior_units_drops_sources_and_files(catalog):
    plan = _make_plan()
    pid = gen.persist(catalog, plan)
    gen.clear_prior_units(catalog, pid)
    # Package row stays; everything beneath is gone.
    pkg_count = catalog.execute(
        "SELECT COUNT(*) FROM generated_package WHERE package_id = ?", [pid],
    ).fetchone()[0]
    unit_count = catalog.execute(
        "SELECT COUNT(*) FROM generated_unit WHERE package_id = ?", [pid],
    ).fetchone()[0]
    file_count = catalog.execute(
        "SELECT COUNT(*) FROM generated_file WHERE package_id = ?", [pid],
    ).fetchone()[0]
    assert pkg_count == 1
    assert unit_count == 0
    assert file_count == 0


def test_persist_files_with_unit_logical_key(catalog):
    plan = _make_plan()
    plan.files = [
        gen.GenFile(filename="bound.txt", content="x",
                    unit_logical_key="node_100", purpose="bound"),
        gen.GenFile(filename="package.txt", content="y", purpose="overview"),
    ]
    pid = gen.persist(catalog, plan)
    bound = catalog.execute(
        "SELECT u.name FROM generated_file f "
        "JOIN generated_unit u ON u.unit_id = f.unit_id "
        "WHERE f.package_id = ? AND f.filename = 'bound.txt'", [pid],
    ).fetchone()
    assert bound is not None and bound[0] == "Root Concept"
    pkg_level = catalog.execute(
        "SELECT unit_id FROM generated_file "
        "WHERE package_id = ? AND filename = 'package.txt'", [pid],
    ).fetchone()
    assert pkg_level[0] is None
