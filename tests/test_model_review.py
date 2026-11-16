"""Tests for /kb-model-review (mcp-servers/kb-mcp/model_review_gen.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "mcp-servers" / "kb-mcp")]

import model_review_gen as mr  # noqa: E402
import ref_models as rm  # noqa: E402

# A small model with the usual problems: identity copied onto roles, coverage
# held twice, inline addresses, an FK cycle and a table without a PK.
BAD_DDL = """
CREATE TABLE patient.patient (
  patient_id BIGINT, first_name STRING, last_name STRING, date_of_birth DATE, ssn STRING,
  address_line1 STRING, city STRING, postal_code STRING, phone_number STRING,
  CONSTRAINT pk_patient PRIMARY KEY (patient_id));
CREATE TABLE patient.guarantor (
  guarantor_id BIGINT, patient_id BIGINT, first_name STRING, last_name STRING,
  date_of_birth DATE, ssn STRING, address_line1 STRING, city STRING, postal_code STRING,
  CONSTRAINT pk_guarantor PRIMARY KEY (guarantor_id),
  CONSTRAINT fk_g_p FOREIGN KEY (patient_id) REFERENCES patient.patient (patient_id));
CREATE TABLE patient.insurance_coverage (
  insurance_coverage_id BIGINT, patient_id BIGINT, payer_name STRING, member_number STRING,
  group_number STRING, coverage_type STRING, coverage_status STRING,
  CONSTRAINT pk_ic PRIMARY KEY (insurance_coverage_id),
  CONSTRAINT fk_ic_p FOREIGN KEY (patient_id) REFERENCES patient.patient (patient_id));
CREATE TABLE billing.billing_coverage (
  billing_coverage_id BIGINT, patient_id BIGINT, payer_name STRING, member_number STRING,
  group_number STRING, coverage_type STRING, claim_id BIGINT,
  CONSTRAINT pk_bc PRIMARY KEY (billing_coverage_id),
  CONSTRAINT fk_bc_p FOREIGN KEY (patient_id) REFERENCES patient.patient (patient_id));
CREATE TABLE billing.claim (
  claim_id BIGINT, billing_coverage_id BIGINT, claim_status STRING, claim_type STRING,
  denial_reason_code STRING, place_of_service_code STRING, revenue_code STRING,
  CONSTRAINT pk_claim PRIMARY KEY (claim_id),
  CONSTRAINT fk_c_bc FOREIGN KEY (billing_coverage_id) REFERENCES billing.billing_coverage (billing_coverage_id));
ALTER TABLE billing.billing_coverage ADD CONSTRAINT fk_bc_c FOREIGN KEY (claim_id) REFERENCES billing.claim (claim_id);
CREATE TABLE billing.audit_note (note_text STRING, note_type STRING);
"""

GOOD_DDL = """
CREATE TABLE party.party (party_id BIGINT, party_type STRING, CONSTRAINT pk PRIMARY KEY (party_id));
CREATE TABLE party.person (party_id BIGINT, first_name STRING, last_name STRING, birth_date DATE,
  CONSTRAINT pk PRIMARY KEY (party_id),
  CONSTRAINT fk FOREIGN KEY (party_id) REFERENCES party.party (party_id));
CREATE TABLE party.party_role (party_role_id BIGINT, party_id BIGINT, role_type_id BIGINT,
  CONSTRAINT pk PRIMARY KEY (party_role_id),
  CONSTRAINT fk FOREIGN KEY (party_id) REFERENCES party.party (party_id));
"""


def _model(ddl):
    return rm.parse_ddl_model(ddl, industry="healthcare", name="test")


def _review(model):
    cat = rm.load_patterns()
    links = mr.pattern_links(duckdb.connect(), model, None, cat)
    return mr.run_checks(model, links, cat)


def test_bad_model_findings():
    fs = _review(_model(BAD_DDL))
    by = {f.section for f in fs}
    assert {"party_role", "single_owner", "contact_mechanism", "structure"} <= by
    pr = next(f for f in fs if f.section == "party_role")
    assert "patient.guarantor" in pr.entities and "party" in pr.vibe
    so = [f for f in fs if f.section == "single_owner"]
    assert any({"patient.insurance_coverage", "billing.billing_coverage"} <= set(f.entities)
               for f in so)
    st = " ".join(f.title for f in fs if f.section == "structure")
    assert "cycle" in st and "primary key" in st
    letter, _pct = mr.grade(mr.score(fs))
    assert letter in {"D", "F"}


def test_good_model_is_clean():
    fs = _review(_model(GOOD_DDL))
    assert not [f for f in fs if f.severity == "error"]
    assert mr.grade(mr.score(fs))[0] in {"A", "B"}


def test_cycles():
    assert mr._cycles("abc", [("a", "b"), ("b", "a"), ("b", "c")]) == [["a", "b"]]
    assert mr._cycles("ab", [("a", "b")]) == []


def test_vibes_ordered_by_severity():
    fs = [mr.Finding("structure", "advice", "t", "d", vibe="third"),
          mr.Finding("party_role", "error", "t", "d", vibe="first"),
          mr.Finding("contact_mechanism", "warning", "t", "d", vibe="second")]
    assert mr.render_vibes(fs) == "1. first\n\n2. second\n\n3. third\n"


def test_load_input_variants(tmp_path):
    ddl_dir = tmp_path / "healthcare"
    ddl_dir.mkdir()
    (ddl_dir / "a.sql").write_text(BAD_DDL)
    conn = duckdb.connect()
    model, mid, err = mr.load_input(conn, str(ddl_dir))
    assert model is not None and mid is None and not err
    assert model.industry == "healthcare" and len(model.entities) == 6
    _m, _mid, err = mr.load_input(conn, str(tmp_path / "missing.sql"))
    assert "not found" in err
    _m, _mid, err = mr.load_input(conn, "databricks:healthcare")
    assert "load it first" in err


def test_generator_writes_package(tmp_path):
    (tmp_path / "model.sql").write_text(BAD_DDL)
    conn = duckdb.connect()
    d = mr.ModelReviewDecomposer().decompose(conn, None, str(tmp_path / "model.sql"))
    plan = mr.ModelReviewPlanner().plan(conn, d)
    assert not mr.ModelReviewValidator().validate(conn, plan)
    files = {f.filename: f.content for f in plan.files}
    assert set(files) == {"review.md", "findings.json", "vibes.txt", "_provenance.md"}
    data = json.loads(files["findings.json"])
    assert data["grade"] == plan.package_metadata["grade"]
    assert files["vibes.txt"].startswith("1. ")
    assert "| Party and roles |" in files["review.md"]


def test_unreadable_model_fails_validation(tmp_path):
    conn = duckdb.connect()
    d = mr.ModelReviewDecomposer().decompose(conn, None, str(tmp_path / "nope.json"))
    plan = mr.ModelReviewPlanner().plan(conn, d)
    assert mr.ModelReviewValidator().validate(conn, plan)


def test_reference_matching_uses_sdtm_labels():
    ref = rm.RefModel(source="cdisc", industry="clinical_trials", flavour="sdtmig", version="3.4")
    ref.entities = [rm.RefEntity(name="ae", display_name="AE", description="Adverse Events (AE)"),
                    rm.RefEntity(name="vs", display_name="VS", description="Vital Signs (VS)")]
    model = rm.RefModel(source="x", industry="clinical_trials", flavour="x", version="1")
    model.entities = [rm.RefEntity(name="adverse_event", domain="safety")]
    fs = mr.check_reference(model, [ref])
    assert len(fs) == 1 and "VS (Vital Signs)" in fs[0].detail and "AE" not in fs[0].detail
