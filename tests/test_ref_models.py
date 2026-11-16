"""Tests for the reference-model layer (mcp-servers/kb-mcp/ref_models.py + scripts/load_ref_models.py)."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "mcp-servers" / "kb-mcp")]

import ref_models as rm  # noqa: E402


def _load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("load_ref_models", ROOT / "scripts" / "load_ref_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _attr(name, typ="STRING", fk="", ref="", tags=""):
    return {"name": name, "column_name": name, "type": typ, "foreign_key_to": fk,
            "references": ref, "tags": tags, "description": f"{name} column"}


MODEL_JSON = {
    "agent_version": "x", "model": {
        "name": "Healthcare", "version": "v2_mvm", "description": "test model",
        "domains": [
            {"name": "patient", "products": [
                {"name": "patient", "table_name": "patient", "primary_key": "patient_id",
                 "subdomain": "identity", "description": "A person receiving care.",
                 "reference": "HL7 FHIR Patient",
                 "attributes": [_attr("patient_id", "BIGINT"), _attr("first_name"),
                                _attr("last_name"), _attr("birth_date", "DATE", tags="pii"),
                                _attr("primary_clinician_id", "BIGINT",
                                      fk="provider.clinician.clinician_id",
                                      ref="Internal - LLM Generated Cross-Domain Link")]},
                {"name": "insurance_coverage", "table_name": "insurance_coverage",
                 "primary_key": "insurance_coverage_id", "description": "Coverage.",
                 "attributes": [_attr("insurance_coverage_id", "BIGINT"),
                                _attr("patient_id", "BIGINT", fk="patient.patient.patient_id"),
                                _attr("member_number")]},
                {"name": "address", "table_name": "address", "primary_key": "address_id",
                 "attributes": [_attr("address_id", "BIGINT"), _attr("city"), _attr("postal_code")]},
            ]},
            {"name": "provider", "products": [
                {"name": "clinician", "table_name": "clinician", "primary_key": "clinician_id",
                 "attributes": [_attr("clinician_id", "BIGINT"), _attr("first_name"),
                                _attr("last_name"), _attr("npi")]}]},
            {"name": "billing", "products": [
                {"name": "billing_coverage", "table_name": "billing_coverage",
                 "primary_key": "billing_coverage_id",
                 "attributes": [_attr("billing_coverage_id", "BIGINT"), _attr("member_number")]}]},
            {"name": "reference", "products": [
                {"name": "icd_code", "table_name": "icd_code", "primary_key": "icd_code_id",
                 "attributes": [_attr("icd_code_id", "BIGINT"), _attr("code")]}]},
        ],
        "metric_views": [{"view_name": "patient_metrics", "owner_domain": "patient",
                          "owner_product": "patient", "sql": "CREATE VIEW x WITH METRICS",
                          "description": "Patient counts", "dimensions_count": 3,
                          "measures_count": 2}],
    }}


@pytest.fixture
def model_path(tmp_path):
    p = tmp_path / "data-models" / "healthcare" / "v2" / "mvm" / "model.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(MODEL_JSON))
    return p


@pytest.fixture
def conn():
    c = duckdb.connect()
    rm.ensure_schema(c)
    yield c
    c.close()


def test_parse_databricks_model(model_path):
    m = rm.parse_databricks_model(model_path)
    assert (m.source, m.industry, m.flavour, m.version) == ("databricks", "healthcare", "mvm", "v2_mvm")
    assert [e.key for e in m.entities][:2] == ["patient.patient", "patient.insurance_coverage"]
    pat = m.entity_index()["patient.patient"]
    assert pat.primary_key == "patient_id" and pat.attributes[0].is_pk
    assert pat.attributes[-1].generated_ref
    rels = set(m.relationships())
    assert ("patient.patient", "provider.clinician", "primary_clinician_id") in rels
    assert ("patient.insurance_coverage", "patient.patient", "patient_id") in rels
    assert m.metrics[0].entity_key == "patient.patient"


def test_load_read_roundtrip_and_replace(conn, model_path):
    model = rm.parse_databricks_model(model_path)
    mid = rm.load_model(conn, model)
    assert conn.execute("SELECT count(*) FROM ref_entity").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM ref_relationship").fetchone()[0] == 2
    assert conn.execute("SELECT sum(generated_ref::INT) FROM ref_attribute").fetchone()[0] == 1
    back = rm.read_model(conn, mid)
    assert [e.key for e in back.entities] == [e.key for e in model.entities]
    assert back.metrics[0].entity_key == "patient.patient"
    mid2 = rm.load_model(conn, model)           # reload replaces
    assert mid2 != mid
    assert conn.execute("SELECT count(*) FROM ref_model").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ref_entity").fetchone()[0] == 6
    assert rm.list_models(conn)[0]["entities"] == 6


SILVERSTON = """entity,attribute,pk,fk,domain
PARTY,PARTY ID,Yes,No,ID
,DESCRIPTION,No,No,Description
CLAIM,CLAIM ID,Yes,No,ID
,AGREEMENT ID,No,Yes,ID
,CLAIM SUBMISSION DATE,No,No,Datetime
CLAIM ITEM,CLAIM ID,Yes,Yes,ID
,CLAIM ITEM SEQ ID,Yes,No,ID
AGREEMENT,AGREEMENT ID,Yes,No,ID
,"PARTY ID FROM",No,Yes,ID
AGREEMENT ROLE,AGREEMENT ID,Yes,Yes,ID
,PARTY ID,Yes,Yes,ID
"""


def test_parse_silverston_csv():
    m = rm.parse_silverston_csv(SILVERSTON, industry="healthcare", appendix="Appendix C")
    names = [e.name for e in m.entities]
    assert names == ["party", "claim", "claim_item", "agreement", "agreement_role"]
    claim = m.entity_index()["claim"]
    assert claim.primary_key == "claim_id"
    assert [a.name for a in claim.attributes] == ["claim_id", "agreement_id", "claim_submission_date"]
    assert claim.attributes[0].fk_to is None             # own key is never an FK
    item = m.entity_index()["claim_item"]
    assert item.primary_key == "claim_id,claim_item_seq_id"
    rels = set(m.relationships())
    assert ("claim", "agreement", "agreement_id") in rels
    assert ("claim_item", "claim", "claim_id") in rels
    assert ("agreement_role", "party", "party_id") in rels
    # PARTY ID FROM names no entity's PK, so it stays unresolved
    assert not any(v == "party_id_from" for _f, _t, v in rels)


OMOP_FIELDS = """cdmTableName,cdmFieldName,isRequired,cdmDatatype,userGuidance,etlConventions,isPrimaryKey,isForeignKey,fkTableName,fkFieldName,fkDomain,fkClass
person,person_id,TRUE,integer,A person.,x,TRUE,FALSE,NA,NA,NA,NA
person,gender_concept_id,TRUE,integer,Sex at birth.,x,FALSE,TRUE,CONCEPT,CONCEPT_ID,Gender,NA
visit_occurrence,visit_occurrence_id,TRUE,integer,A visit.,x,TRUE,FALSE,NA,NA,NA,NA
visit_occurrence,person_id,TRUE,integer,,x,FALSE,TRUE,PERSON,PERSON_ID,NA,NA
concept,concept_id,TRUE,integer,,x,TRUE,FALSE,NA,NA,NA,NA
"""
OMOP_TABLES = """cdmTableName,schema,isRequired,conceptPrefix,measurePersonCompleteness,measurePersonCompletenessThreshold,validation,tableDescription,userGuidance,etlConventions
person,CDM,TRUE,NA,FALSE,NA,NA,Central identity table.,x,x
visit_occurrence,CDM,TRUE,NA,FALSE,NA,NA,Visits.,x,x
concept,VOCAB,TRUE,NA,FALSE,NA,NA,Standard concepts.,x,x
"""


def test_parse_omop():
    m = rm.parse_omop(OMOP_FIELDS, OMOP_TABLES)
    assert [e.key for e in m.entities] == ["cdm.person", "cdm.visit_occurrence", "vocab.concept"]
    assert m.entity_index()["cdm.person"].description == "Central identity table."
    rels = set(m.relationships())
    assert ("cdm.visit_occurrence", "cdm.person", "person_id") in rels
    assert ("cdm.person", "vocab.concept", "gender_concept_id") in rels


DDL = """
CREATE OR REPLACE TABLE `cat`.`patient`.`patient` (
    `patient_id` BIGINT COMMENT 'Surrogate key, it''s unique',
    `first_name` STRING COMMENT 'Given name',
    `clinician_id` BIGINT,
    `amount` DECIMAL(18,2),
    CONSTRAINT pk_patient PRIMARY KEY(`patient_id`)
) COMMENT 'People receiving care';
CREATE TABLE provider.clinician (clinician_id BIGINT PRIMARY KEY, npi STRING);
ALTER TABLE `cat`.`patient`.`patient` ADD CONSTRAINT `fk_x` FOREIGN KEY (`clinician_id`) REFERENCES `cat`.`provider`.`clinician`(`clinician_id`);
"""


def test_parse_ddl_model():
    m = rm.parse_ddl_model(DDL, industry="healthcare")
    pat = m.entity_index()["patient.patient"]
    assert pat.description == "People receiving care"
    assert [a.name for a in pat.attributes] == ["patient_id", "first_name", "clinician_id", "amount"]
    assert pat.attributes[0].is_pk and pat.primary_key == "patient_id"
    assert pat.attributes[0].description == "Surrogate key, it's unique"
    assert pat.attributes[3].data_type == "DECIMAL(18,2)"
    assert m.entity_index()["provider.clinician"].primary_key == "clinician_id"
    assert ("patient.patient", "provider.clinician", "clinician_id") in set(m.relationships())


def test_patterns_file_is_valid():
    cat = rm.load_patterns()
    keys = [p.key for p in cat.patterns]
    assert len(keys) == len(set(keys)) >= 20
    for p in cat.patterns:
        assert p.names and p.description
        for b in p.books:
            assert b["book"] in cat.books, (p.key, b)


def test_classify_entities(model_path):
    cat = rm.load_patterns()
    m = rm.parse_databricks_model(model_path)
    got = {e.name: {k for k, _c, _r in rm.classify_entity(e, cat)} for e in m.entities}
    assert "party_role" in got["patient"]
    assert "party_role" in got["clinician"]
    assert "coverage" in got["insurance_coverage"] and "coverage" in got["billing_coverage"]
    assert "contact_mechanism" in got["address"]
    assert "classification" in got["icd_code"]
    assert rm.carries_person_identity(m.entity_index()["patient.patient"]) == [
        "first_name", "last_name", "birth_date"]


def test_classify_model_keeps_subagent_rows(conn, model_path):
    cat = rm.load_patterns()
    mid = rm.load_model(conn, rm.parse_databricks_model(model_path))
    n = rm.classify_model(conn, mid, cat)
    assert n > 0
    eid = conn.execute("SELECT ref_entity_id FROM ref_entity WHERE name = 'address'").fetchone()[0]
    conn.execute("DELETE FROM ref_entity_concept WHERE ref_entity_id = ?", [eid])
    conn.execute("INSERT INTO ref_entity_concept VALUES (?, 'contact_mechanism', NULL, 0.9, "
                 "'subagent', 'ok')", [eid])
    rm.classify_model(conn, mid, cat)
    rows = conn.execute("SELECT method, confidence FROM ref_entity_concept WHERE ref_entity_id = ? "
                        "AND pattern_key = 'contact_mechanism'", [eid]).fetchall()
    assert rows == [("subagent", 0.9)]
    pats = rm.entity_patterns(conn, mid)
    assert pats["patient.patient"][0][0] == "party_role"


def test_map_batches_and_apply(conn, model_path):
    cat = rm.load_patterns()
    mid = rm.load_model(conn, rm.parse_databricks_model(model_path))
    rm.classify_model(conn, mid, cat)
    batches = rm.map_batches(conn, [mid], cat, batch_size=4)
    assert [len(b["entity_ids"]) for b in batches] == [4, 2]
    assert "party_role: Party Role" in batches[0]["prompt"]
    assert "patient (patient)" in batches[0]["prompt"]
    ids = batches[0]["entity_ids"]
    rows = [{"ref_entity_id": ids[0], "pattern_key": "party_role", "confidence": 0.99, "note": "role"},
            {"ref_entity_id": ids[0], "pattern_key": "nonsense"},
            {"ref_entity_id": 999999, "pattern_key": "party"},
            {"bad": 1}]
    n, problems = rm.apply_mapping(conn, rows, cat, allowed_ids=set(ids))
    assert n == 1 and len(problems) == 3
    assert conn.execute("SELECT confidence, method FROM ref_entity_concept WHERE ref_entity_id = ?",
                        [ids[0]]).fetchall() == [(0.95, "subagent")]
    # entities in the batch with no rows lose their heuristic guesses
    assert conn.execute("SELECT count(*) FROM ref_entity_concept WHERE ref_entity_id IN (?, ?, ?)",
                        ids[1:]).fetchone()[0] == 0
    # entities outside the batch keep theirs
    assert conn.execute("SELECT count(*) FROM ref_entity_concept WHERE ref_entity_id IN (?, ?)",
                        batches[1]["entity_ids"]).fetchone()[0] > 0


def test_embeddings_and_similarity(conn, model_path):
    import numpy as np

    class FakeEmbedder:
        def encode(self, texts, normalize_embeddings=True):
            out = []
            for t in texts:
                v = np.zeros(384, dtype="float32")
                v[0 if "coverage" in t else 1] = 1.0
                out.append(v)
            return np.array(out)

    mid = rm.load_model(conn, rm.parse_databricks_model(model_path))
    assert rm.embed_entities(conn, [mid], FakeEmbedder()) == 6
    q = [1.0] + [0.0] * 383
    hits = rm.similar_entities(conn, q, limit=2)
    assert {h["name"] for h in hits} == {"insurance_coverage", "billing_coverage"}
    assert rm.similar_entities(conn, q, sources=["omop"]) == []


def test_entity_card_is_short(model_path):
    e = rm.parse_databricks_model(model_path).entity_index()["patient.patient"]
    card = rm.entity_card(e)
    assert card.startswith("patient (patient). A person receiving care.")
    assert "Attributes: patient_id, first_name" in card


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_databricks_model_files_latest_version(tmp_path, model_path):
    s = _load_script()
    v1 = tmp_path / "data-models" / "healthcare" / "v1" / "mvm" / "model.json"
    v1.parent.mkdir(parents=True)
    v1.write_text("{}")
    (tmp_path / "data-models" / "healthcare" / "v2" / "ecm").mkdir()
    files = s.databricks_model_files(tmp_path, None, ["mvm"])
    assert files == [model_path]
    assert len(s.databricks_model_files(tmp_path, None, ["mvm"], all_versions=True)) == 2
    assert s.databricks_model_files(tmp_path, ["banking"], ["mvm"]) == []


def test_cli_load_classify_status(tmp_path, model_path, caplog):
    s = _load_script()
    cat = tmp_path / "c.ddb"
    assert s.main(["--catalog", str(cat), "load-databricks", "--repo", str(tmp_path)]) == 0
    assert s.main(["--catalog", str(cat), "classify", "--all"]) == 0
    caplog.clear()
    import logging
    with caplog.at_level(logging.INFO):
        assert s.main(["--catalog", str(cat), "status"]) == 0
    assert "healthcare" in caplog.text and "heuristic" in caplog.text
    out = tmp_path / "run"
    assert s.main(["--catalog", str(cat), "map-prep", "--industry", "healthcare",
                   "--out", str(out), "--batch-size", "3"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest) == 2
    first = manifest[0]
    Path(first["result"]).write_text(json.dumps(
        [{"ref_entity_id": first["entity_ids"][0], "pattern_key": "party_role", "confidence": 0.9}]))
    assert s.main(["--catalog", str(cat), "map-apply", "--dir", str(out)]) == 0
    c = duckdb.connect(str(cat), read_only=True)
    assert c.execute("SELECT count(*) FROM ref_entity_concept WHERE method = 'subagent'"
                     ).fetchone()[0] == 1
    c.close()


def test_silverston_pages_and_csv(tmp_path):
    s = _load_script()
    epub = tmp_path / "dmrb2.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("OEBPS/images/ap03-001.jpg", b"img1")
        z.writestr("OEBPS/images/ap03-002.jpg", b"img2")
        z.writestr("OEBPS/images/ap04-001.jpg", b"other")
    root = tmp_path / "silverston"
    pages = s.silverston_pages(epub, "healthcare", out_root=root)
    assert [Path(p["image"]).name for p in pages] == ["ap03-001.jpg", "ap03-002.jpg"]
    prompt = Path(pages[0]["prompt"]).read_text()
    assert "entity,attribute,pk,fk,domain" in prompt and str(Path(pages[0]["csv"]).resolve()) in prompt
    Path(pages[0]["csv"]).write_text("entity,attribute,pk,fk,domain\nPARTY,PARTY ID,Yes,No,ID\n")
    text, missing = s.silverston_csv("healthcare", root=root)
    assert missing == ["p02.csv"]
    assert text.splitlines() == ["entity,attribute,pk,fk,domain", "PARTY,PARTY ID,Yes,No,ID"]
    with pytest.raises(SystemExit):
        s.silverston_pages(epub, "astrology", out_root=root)
