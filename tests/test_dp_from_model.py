"""Tests for /kb-dp-from-model (model_products_gen.py) and model_emit.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "mcp-servers" / "kb-mcp")]

import model_emit as me  # noqa: E402
import model_products_gen as mpg  # noqa: E402
import ref_models as rm  # noqa: E402

from test_model_review import BAD_DDL  # noqa: E402

METRIC = """CREATE OR REPLACE VIEW `x`.`_metrics`.`claim` WITH METRICS LANGUAGE YAML AS $$
  version: 1.1
  source: "`x`.`billing`.`claim`"
  dimensions:
    - name: "claim_status"
      expr: claim_status
  measures:
    - name: "total_claims"
      expr: COUNT(1)
      comment: "Claims submitted"
    - name: "denial_rate"
      expr: AVG(CASE WHEN claim_status = 'denied' THEN 1 ELSE 0 END)
      comment: "Share of claims denied"
$$"""


@pytest.fixture
def model():
    m = rm.parse_ddl_model(BAD_DDL, industry="healthcare", name="test")
    m.metrics.append(rm.RefMetric(name="claim", entity_key="billing.claim", definition=METRIC))
    return m


@pytest.fixture
def catalog():
    conn = duckdb.connect()
    conn.execute((ROOT / "schemas" / "catalog.sql").read_text())
    resolver = MagicMock()
    resolver.resolve_lookup_only.return_value = None
    return conn, resolver


def test_resolve_tables(model):
    found, missing = mpg.resolve_tables(["billing.claim", "guarantor", "domain:patient", "nope"],
                                        model)
    assert found[:2] == ["billing.claim", "patient.guarantor"]
    assert "patient.patient" in found and missing == ["nope"]


def test_uc_ddl_orders_parents_and_comments_outside_fks(model):
    r = me.FkResolver(model)
    ents = [r["patient.guarantor"], r["patient.patient"]]
    ddl = me.uc_ddl(ents, r, catalog="demo")
    assert ddl.index("demo.patient.patient (") < ddl.index("demo.patient.guarantor (")
    assert "REFERENCES demo.patient.patient (patient_id)" in ddl
    claim = me.uc_ddl([r["billing.claim"]], r)
    assert "-- billing_coverage_id -> billing.billing_coverage" in claim


def test_dbml(model):
    r = me.FkResolver(model)
    out = me.dbml(model.entities, r, project="test")
    assert "Table patient.guarantor {" in out or 'Table "patient.guarantor" {' in out
    assert "Ref:" in out and "[pk" in out


def test_product_kpis_from_metric_views(model):
    item = mpg.opg.Item("Claims", "")
    kpis = mpg.product_kpis(item, model.metrics)
    assert kpis[0]["name"] == "Denial Rate"          # rates rank before counts
    assert kpis[0]["unit"] == "percentage" and kpis[0]["direction"] == "decrease"
    assert all({"id", "name", "unit", "target", "calculation"} <= set(k) for k in kpis)


def test_generator_package(model, catalog, tmp_path, monkeypatch):
    conn, resolver = catalog
    monkeypatch.setattr(mpg.mr, "load_input", lambda c, s: (model, None, ""))
    gen = mpg.make_dp_from_model_generator()
    pid, _report, issues = gen.run_deterministic(
        conn, resolver, "test model", model="x", output_root=str(tmp_path), catalog="demo",
        products=["Claims: claims and coverage => billing.claim, billing.billing_coverage",
                  "People: who the patients are => domain:patient"],
        use_cases=["Denials: find causes => Claims, People"])
    assert pid != -1, issues
    root = next(tmp_path.iterdir())
    contract = yaml.safe_load((root / "contracts/claims.odcs.yaml").read_text())
    assert [o["name"] for o in contract["schema"]] == ["claim", "billing_coverage"]
    odps = yaml.safe_load((root / "products/claims.odps.yaml").read_text())
    names = [k["name"] for k in odps["product"]["productStrategy"]["productKPIs"]]
    assert "Denial Rate" in names
    assert "CREATE TABLE IF NOT EXISTS demo.billing.claim" in (root / "ddl/claims.sql").read_text()
    assert (root / "metrics/claims.sql").is_file()
    notes = (root / "model-notes.md").read_text()
    assert "Person identity is copied onto each role" in notes      # model-wide finding
    req = (root / "sample-data/healthsim-request.md").read_text()
    assert "healthsim" in req and req.index("patient.patient |") < req.index("patient.guarantor |")


def test_healthcare_example_used_without_products(catalog, monkeypatch):
    conn, resolver = catalog
    m = rm.RefModel(source="databricks", industry="healthcare", flavour="mvm", version="v2")
    monkeypatch.setattr(mpg.mr, "load_input", lambda c, s: (m, None, ""))
    d = mpg.DpFromModelDecomposer().decompose(conn, resolver, "x")
    assert d["example"] and len(d["products_raw"]) == 3
    plan = mpg.DpFromModelPlanner().plan(conn, d)
    issues = mpg.DpFromModelValidator().validate(conn, plan)
    assert any("no tables matched" in i.message for i in issues)
