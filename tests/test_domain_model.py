"""Tests for /kb-domain-model (domain_model_gen.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "mcp-servers" / "kb-mcp")]

import domain_model_gen as dm  # noqa: E402
import model_review_gen as mr  # noqa: E402
import ref_models as rm  # noqa: E402

from test_model_review import BAD_DDL  # noqa: E402


def _model():
    return rm.parse_ddl_model(BAD_DDL, industry="healthcare", name="test")


def test_select_entities():
    m = _model()
    assert dm.select_entities(m, domains=["billing"]) == [
        "billing.billing_coverage", "billing.claim", "billing.audit_note"]
    picked = dm.select_entities(m, description="guarantor and patient registration")
    assert "patient.guarantor" in picked and "billing.claim" not in picked
    assert len(dm.select_entities(m)) == len(m.entities)


def test_rewrite_applies_patterns():
    m = _model()
    out, changes = dm.rewrite(m, [e.key for e in m.entities], name="t")
    idx = out.entity_index()
    kinds = {c.kind for c in changes}
    assert {"party", "contact_mechanism", "single_owner"} <= kinds
    # identity moved to person; roles reference party_role
    g = {a.name: a for a in idx["patient.guarantor"].attributes}
    assert "first_name" not in g and g["party_role_id"].fk_to == "party.party_role.party_role_id"
    assert "first_name" in {a.name for a in idx["party.person"].attributes}
    # inline address columns gone
    assert "city" not in {a.name for a in idx["patient.patient"].attributes}
    assert "party.postal_address" in idx
    # the base model is untouched
    assert "first_name" in {a.name for a in m.entity_index()["patient.guarantor"].attributes}


def test_rewrite_drops_fks_outside_scope():
    m = _model()
    out, changes = dm.rewrite(m, ["billing.claim"], name="t", refactor=())
    assert not any(a.fk_to for a in out.entity_index()["billing.claim"].attributes)
    assert changes[0].kind == "scope"


def test_review_improves_after_rewrite():
    m = _model()
    cat = rm.load_patterns()
    before = mr.grade(mr.score(mr.run_checks(m, mr.pattern_links(None, m, None, cat), cat)))
    out, _ = dm.rewrite(m, [e.key for e in m.entities], name="t")
    after = mr.grade(mr.score(mr.run_checks(out, mr.pattern_links(None, out, None, cat), cat)))
    assert after[1] > before[1]


def test_model_json_roundtrip(tmp_path):
    m = _model()
    out, changes = dm.rewrite(m, [e.key for e in m.entities], name="t")
    doc = dm.to_model_json(out, changes, base_label="test", description="d")
    p = tmp_path / "model.json"
    p.write_text(json.dumps(doc))
    back = rm.parse_databricks_model(p, industry="healthcare", flavour="domain")
    assert {e.key for e in back.entities} == {e.key for e in out.entities}
    assert len(back.relationships()) == len(out.relationships())


def test_generator_package(tmp_path, monkeypatch):
    conn = duckdb.connect()
    conn.execute((ROOT / "schemas" / "catalog.sql").read_text())
    monkeypatch.setattr(dm.mr, "load_input", lambda c, s: (_model(), None, ""))
    d = dm.DomainModelDecomposer().decompose(conn, None, "patient billing", base="x",
                                             name="Billing model")
    plan = dm.DomainModelPlanner().plan(conn, d)
    assert not [i for i in dm.DomainModelValidator().validate(conn, plan) if i.severity == "error"]
    files = {f.filename: f.content for f in plan.files}
    assert {"model.json", "ddl/model.sql", "model.dbml", "changes.md", "review.md",
            "vibes.txt", "_prompts/refine-model.md"} <= set(files)
    assert "CREATE TABLE IF NOT EXISTS party.party" in files["ddl/model.sql"]
    assert files["ddl/model.sql"].index("party.party (") < files["ddl/model.sql"].index(
        "party.party_role (")
    meta = plan.package_metadata
    assert meta["roundtrip_ok"] and meta["score_after"] > meta["score_before"]


def test_no_base_model():
    d = dm.DomainModelDecomposer().decompose(duckdb.connect(), None, "x")
    plan = dm.DomainModelPlanner().plan(duckdb.connect(), d)
    assert dm.DomainModelValidator().validate(None, plan)[0].severity == "error"
