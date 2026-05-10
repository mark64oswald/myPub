"""Tests for healthcare_libs.fhir — production FHIR R4 reference impl.

Tests cover:
  * Resource builders (Patient, Encounter, Observation, DiagnosticReport,
    Claim, ClaimResponse, ImagingStudy) — minimal + full input shapes
  * Helpers (reference, codeable_concept, quantity, identifier, human_name)
  * Bundle assembly (transaction + collection)
  * Validator success + failure paths
  * Round-trip stability
  * Cross-validation: every builder's output passes validate() clean
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

from healthcare_libs import fhir  # noqa: E402


# =====================================================================
# Helpers
# =====================================================================

def test_reference_builds_relative_path():
    assert fhir.reference("Patient", "abc-123") == {"reference": "Patient/abc-123"}


def test_reference_rejects_empty_inputs():
    with pytest.raises(ValueError):
        fhir.reference("", "abc")
    with pytest.raises(ValueError):
        fhir.reference("Patient", "")


def test_codeable_concept_with_display():
    cc = fhir.codeable_concept(fhir.LOINC, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma")
    assert cc == {"coding": [{
        "system": "http://loinc.org",
        "code": "2160-0",
        "display": "Creatinine [Mass/volume] in Serum or Plasma",
    }]}


def test_codeable_concept_without_display():
    cc = fhir.codeable_concept(fhir.SNOMED, "44054006")
    assert cc == {"coding": [{"system": "http://snomed.info/sct", "code": "44054006"}]}
    assert "display" not in cc["coding"][0]


def test_quantity_uses_ucum_system():
    q = fhir.quantity(1.2, "mg/dL")
    assert q["value"] == 1.2
    assert q["unit"] == "mg/dL"
    assert q["system"] == fhir.UCUM
    assert q["code"] == "mg/dL"


def test_identifier_builds_dict():
    i = fhir.identifier(fhir.SYSTEM_NPI, "1234567890")
    assert i == {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890"}


def test_identifier_rejects_empty_inputs():
    with pytest.raises(ValueError):
        fhir.identifier("", "abc")
    with pytest.raises(ValueError):
        fhir.identifier("urn:abc", "")


def test_human_name_normalizes_string_given():
    n = fhir.human_name(family="Doe", given="Jane")
    assert n == {"family": "Doe", "given": ["Jane"]}


def test_human_name_with_multiple_given_and_prefix_suffix():
    n = fhir.human_name(family="Smith", given=["John", "Q"], prefix="Dr.", suffix=["MD", "PhD"])
    assert n["family"] == "Smith"
    assert n["given"] == ["John", "Q"]
    assert n["prefix"] == ["Dr."]
    assert n["suffix"] == ["MD", "PhD"]


# =====================================================================
# Identifier system constants — sanity check the URLs
# =====================================================================

@pytest.mark.parametrize("constant,expected", [
    ("SYSTEM_NPI", "http://hl7.org/fhir/sid/us-npi"),
    ("SYSTEM_SSN", "http://hl7.org/fhir/sid/us-ssn"),
    ("SYSTEM_LOCAL_MRN", "urn:oid:LOCAL_MRN_OID"),
    ("LOINC", "http://loinc.org"),
    ("SNOMED", "http://snomed.info/sct"),
    ("ICD10_CM", "http://hl7.org/fhir/sid/icd-10-cm"),
    ("RXNORM", "http://www.nlm.nih.gov/research/umls/rxnorm"),
    ("UCUM", "http://unitsofmeasure.org"),
])
def test_system_constants(constant, expected):
    assert getattr(fhir, constant) == expected


# =====================================================================
# build_patient
# =====================================================================

def test_build_patient_minimal():
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1985-06-15")
    assert p["resourceType"] == "Patient"
    assert p["name"][0]["family"] == "Doe"
    assert p["name"][0]["given"] == ["Jane"]
    assert p["birthDate"] == "1985-06-15"
    assert p["gender"] == "unknown"
    assert "id" in p
    # No identifier / telecom / address when not provided
    assert "identifier" not in p
    assert "telecom" not in p
    assert "address" not in p


def test_build_patient_full():
    p = fhir.build_patient(
        family="Smith", given=["Mary", "Anne"], birth_date="1972-03-12", gender="female",
        mrn="MRN-99", ssn="123456789",
        address_line="42 Galaxy Way", address_city="Springfield",
        address_state="IL", address_postal_code="62701",
        telecom_phone="+1-555-0100", telecom_email="mary@example.com",
        patient_id="patient-mary",
    )
    assert p["id"] == "patient-mary"
    assert len(p["identifier"]) == 2
    mrn_id = next(i for i in p["identifier"] if i["system"] == fhir.SYSTEM_LOCAL_MRN)
    assert mrn_id["value"] == "MRN-99"
    assert mrn_id["use"] == "usual"
    ssn_id = next(i for i in p["identifier"] if i["system"] == fhir.SYSTEM_SSN)
    assert ssn_id["value"] == "123456789"
    assert ssn_id["use"] == "official"
    # Address
    assert p["address"][0]["line"] == ["42 Galaxy Way"]
    assert p["address"][0]["city"] == "Springfield"
    assert p["address"][0]["state"] == "IL"
    assert p["address"][0]["postalCode"] == "62701"
    assert p["address"][0]["country"] == "US"
    # Telecom
    phones = [t for t in p["telecom"] if t["system"] == "phone"]
    emails = [t for t in p["telecom"] if t["system"] == "email"]
    assert len(phones) == 1 and phones[0]["value"] == "+1-555-0100"
    assert len(emails) == 1 and emails[0]["value"] == "mary@example.com"
    # All 18+ demographic fields fit and pass validation
    assert not fhir.validate(p)


def test_build_patient_mrn_only_no_ssn():
    p = fhir.build_patient(family="Doe", given="John", birth_date="1990-01-01", mrn="M1")
    assert len(p["identifier"]) == 1
    assert p["identifier"][0]["system"] == fhir.SYSTEM_LOCAL_MRN


def test_build_patient_ssn_only_no_mrn():
    p = fhir.build_patient(family="Doe", given="John", birth_date="1990-01-01", ssn="000000000")
    assert len(p["identifier"]) == 1
    assert p["identifier"][0]["system"] == fhir.SYSTEM_SSN


def test_build_patient_birth_date_accepts_python_date():
    from datetime import date
    p = fhir.build_patient(family="Doe", given="A", birth_date=date(2020, 5, 1))
    assert p["birthDate"] == "2020-05-01"


def test_build_patient_address_with_multi_line():
    p = fhir.build_patient(
        family="X", given="Y", birth_date="2000-01-01",
        address_line=["Line 1", "Apt 4B"],
        address_city="C", address_state="TX", address_postal_code="73301",
    )
    assert p["address"][0]["line"] == ["Line 1", "Apt 4B"]


# =====================================================================
# build_encounter
# =====================================================================

def test_build_encounter_class_is_codeable_concept():
    e = fhir.build_encounter(patient_ref="pat-1", encounter_class_code="AMB")
    # In R4 the class is a Coding (single), not a CodeableConcept
    assert e["class"]["system"] == fhir.SYSTEM_V3_ACT_CODE
    assert e["class"]["code"] == "AMB"
    assert e["status"] == "finished"


def test_build_encounter_period_iso8601():
    e = fhir.build_encounter(
        patient_ref="pat-1", encounter_class_code="IMP",
        period_start="2026-01-15T10:00:00Z",
        period_end="2026-01-15T11:30:00Z",
    )
    assert e["period"]["start"] == "2026-01-15T10:00:00Z"
    assert e["period"]["end"] == "2026-01-15T11:30:00Z"


def test_build_encounter_normalizes_bare_patient_id():
    e = fhir.build_encounter(patient_ref="pat-7", encounter_class_code="AMB")
    assert e["subject"]["reference"] == "Patient/pat-7"


def test_build_encounter_passes_through_full_reference():
    e = fhir.build_encounter(patient_ref="Patient/pat-7", encounter_class_code="AMB")
    assert e["subject"]["reference"] == "Patient/pat-7"


def test_build_encounter_with_identifier_value():
    e = fhir.build_encounter(
        patient_ref="pat-1", encounter_class_code="EMER",
        identifier_value="VISIT-42",
    )
    assert e["identifier"][0]["value"] == "VISIT-42"


# =====================================================================
# build_observation
# =====================================================================

def test_build_observation_value_quantity():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
    )
    assert "valueQuantity" in o
    assert o["valueQuantity"]["value"] == 1.2
    assert o["valueQuantity"]["unit"] == "mg/dL"
    assert o["valueQuantity"]["system"] == fhir.UCUM


def test_build_observation_value_codeable_concept():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="883-9", code_display="ABO Blood Type",
        value={"system": fhir.SNOMED, "code": "112144000", "display": "Blood group A"},
    )
    assert "valueCodeableConcept" in o
    assert o["valueCodeableConcept"]["coding"][0]["code"] == "112144000"
    assert o["valueCodeableConcept"]["coding"][0]["display"] == "Blood group A"


def test_build_observation_value_string():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="LP12345-6", code_display="Note",
        value="No abnormalities noted.",
    )
    assert o["valueString"] == "No abnormalities noted."
    assert "valueQuantity" not in o
    assert "valueCodeableConcept" not in o


def test_build_observation_reference_range():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
        reference_range_low=0.5, reference_range_high=1.5,
    )
    rr = o["referenceRange"][0]
    assert rr["low"]["value"] == 0.5
    assert rr["high"]["value"] == 1.5
    assert rr["low"]["unit"] == "mg/dL"


def test_build_observation_numeric_requires_unit():
    with pytest.raises(ValueError, match="unit_ucum"):
        fhir.build_observation(
            patient_ref="pat-1", code_loinc="2160-0", code_display="Creatinine",
            value=1.2,
        )


def test_build_observation_effective_datetime():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
        effective_datetime="2026-01-15T10:00:00Z",
    )
    assert o["effectiveDateTime"] == "2026-01-15T10:00:00Z"


def test_build_observation_subject_reference():
    o = fhir.build_observation(
        patient_ref="pat-99", code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
    )
    assert o["subject"]["reference"] == "Patient/pat-99"


# =====================================================================
# build_diagnostic_report
# =====================================================================

def test_build_diagnostic_report_resolves_observation_refs():
    dr = fhir.build_diagnostic_report(
        patient_ref="pat-1", code_loinc="LP29684-5",
        observations=["obs-1", "obs-2", "Observation/obs-3"],
    )
    refs = [r["reference"] for r in dr["result"]]
    assert refs == ["Observation/obs-1", "Observation/obs-2", "Observation/obs-3"]


def test_build_diagnostic_report_includes_conclusion():
    dr = fhir.build_diagnostic_report(
        patient_ref="pat-1", code_loinc="LP29684-5",
        observations=["obs-1"], conclusion="Within normal limits.",
    )
    assert dr["conclusion"] == "Within normal limits."


# =====================================================================
# build_claim
# =====================================================================

def test_build_claim_service_lines_have_sequence_numbers():
    c = fhir.build_claim(
        patient_ref="pat-1", provider_ref="prac-1",
        total_amount=300.0, diagnosis_codes=["E11.9"],
        service_lines=[
            {"cpt": "99213", "charge": 150.00, "service_date": "2026-01-01"},
            {"cpt": "85025", "charge": 150.00, "service_date": "2026-01-01"},
        ],
    )
    items = c["item"]
    assert items[0]["sequence"] == 1
    assert items[1]["sequence"] == 2
    # Each line carries its own productOrService coding
    assert items[0]["productOrService"]["coding"][0]["code"] == "99213"
    assert items[1]["productOrService"]["coding"][0]["code"] == "85025"


def test_build_claim_diagnosis_codes_have_sequence_numbers():
    c = fhir.build_claim(
        patient_ref="pat-1", provider_ref="prac-1",
        total_amount=100.0, diagnosis_codes=["E11.9", "Z00.00", "I10"],
        service_lines=[{"cpt": "99213", "charge": 100.00}],
    )
    seqs = [d["sequence"] for d in c["diagnosis"]]
    assert seqs == [1, 2, 3]
    codes = [d["diagnosisCodeableConcept"]["coding"][0]["code"] for d in c["diagnosis"]]
    assert codes == ["E11.9", "Z00.00", "I10"]


def test_build_claim_total_and_currency():
    c = fhir.build_claim(
        patient_ref="pat-1", provider_ref="prac-1",
        total_amount=2000.50, currency="USD",
        diagnosis_codes=["E11.9"],
        service_lines=[{"cpt": "99213", "charge": 2000.50}],
    )
    assert c["total"]["value"] == 2000.50
    assert c["total"]["currency"] == "USD"


def test_build_claim_rejects_invalid_service_line():
    with pytest.raises(ValueError, match="service_line"):
        fhir.build_claim(
            patient_ref="pat-1", provider_ref="prac-1",
            total_amount=100, diagnosis_codes=["E11.9"],
            service_lines=[{"cpt": "99213"}],  # missing 'charge'
        )


def test_build_claim_with_identifier():
    c = fhir.build_claim(
        patient_ref="pat-1", provider_ref="prac-1",
        total_amount=100, diagnosis_codes=["E11.9"],
        service_lines=[{"cpt": "99213", "charge": 100}],
        identifier_value="CLAIM-007",
    )
    assert c["identifier"][0]["value"] == "CLAIM-007"


# =====================================================================
# build_claim_response
# =====================================================================

def test_build_claim_response_adjudications():
    cr = fhir.build_claim_response(
        claim_ref="claim-1", patient_ref="pat-1", insurer_ref="ins-1",
        total_paid=240.0,
        adjudications=[
            {"sequence": 1, "adjudication": [
                {"category": "submitted", "amount": 150.00},
                {"category": "benefit", "amount": 120.00},
                {"category": "copay", "amount": 30.00},
            ]},
        ],
    )
    item = cr["item"][0]
    assert item["itemSequence"] == 1
    assert len(item["adjudication"]) == 3
    cats = [a["category"]["coding"][0]["code"] for a in item["adjudication"]]
    assert cats == ["submitted", "benefit", "copay"]
    amts = [a["amount"]["value"] for a in item["adjudication"]]
    assert amts == [150.0, 120.0, 30.0]


def test_build_claim_response_payment_total():
    cr = fhir.build_claim_response(
        claim_ref="claim-1", patient_ref="pat-1", insurer_ref="ins-1",
        total_paid=42.42,
    )
    assert cr["payment"]["amount"]["value"] == 42.42
    assert cr["payment"]["amount"]["currency"] == "USD"
    assert cr["request"]["reference"] == "Claim/claim-1"


# =====================================================================
# build_imaging_study
# =====================================================================

def test_build_imaging_study_default_series():
    ims = fhir.build_imaging_study(
        patient_ref="pat-1", study_uid="1.2.3.4.5", modality_code="CT",
    )
    assert ims["numberOfSeries"] == 1
    assert ims["series"][0]["uid"] == "1.2.3.4.5.1"
    assert ims["series"][0]["modality"]["code"] == "CT"
    assert ims["series"][0]["instance"][0]["uid"] == "1.2.3.4.5.1.1"


def test_build_imaging_study_custom_series_preserves_uids():
    ims = fhir.build_imaging_study(
        patient_ref="pat-1", study_uid="1.2.3.4.6", modality_code="MR",
        series=[
            {"uid": "1.2.3.4.6.1", "number": 1, "description": "T1",
             "instances": [{"uid": "1.2.3.4.6.1.1", "number": 1},
                           {"uid": "1.2.3.4.6.1.2", "number": 2}]},
            {"uid": "1.2.3.4.6.2", "number": 2, "description": "T2",
             "instances": [{"uid": "1.2.3.4.6.2.1", "number": 1}]},
        ],
    )
    assert ims["numberOfSeries"] == 2
    assert ims["numberOfInstances"] == 3
    series_uids = [s["uid"] for s in ims["series"]]
    assert series_uids == ["1.2.3.4.6.1", "1.2.3.4.6.2"]
    s1_inst_uids = [i["uid"] for i in ims["series"][0]["instance"]]
    assert s1_inst_uids == ["1.2.3.4.6.1.1", "1.2.3.4.6.1.2"]


def test_build_imaging_study_with_endpoint_ref():
    ims = fhir.build_imaging_study(
        patient_ref="pat-1", study_uid="1.2.3", modality_code="CT",
        endpoint_ref="endpoint-pacs",
    )
    assert ims["endpoint"][0]["reference"] == "Endpoint/endpoint-pacs"


# =====================================================================
# Bundle assembly
# =====================================================================

def test_bundle_transaction_method_per_entry():
    """Resources with id should PUT; resources without id should POST."""
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    bundle = fhir.build_bundle_transaction([p])
    e = bundle["entry"][0]
    assert e["request"]["method"] == "PUT"
    assert e["request"]["url"] == f"Patient/{p['id']}"
    assert e["fullUrl"].startswith("urn:uuid:")


def test_bundle_transaction_post_for_idless_resource():
    res = {"resourceType": "Patient", "name": [{"family": "X", "given": ["Y"]}]}
    bundle = fhir.build_bundle_transaction([res])
    assert bundle["entry"][0]["request"]["method"] == "POST"
    assert bundle["entry"][0]["request"]["url"] == "Patient"


def test_bundle_transaction_passes_through_explicit_request():
    explicit_entry = {
        "fullUrl": "urn:uuid:custom",
        "resource": {"resourceType": "Patient", "id": "p1"},
        "request": {"method": "PATCH", "url": "Patient/p1"},
    }
    bundle = fhir.build_bundle_transaction([explicit_entry])
    assert bundle["entry"][0]["request"]["method"] == "PATCH"
    assert bundle["entry"][0]["fullUrl"] == "urn:uuid:custom"


def test_bundle_collection_has_no_request_elements():
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    e = fhir.build_encounter(patient_ref=p["id"], encounter_class_code="AMB")
    bundle = fhir.build_bundle_collection([p, e])
    assert bundle["type"] == "collection"
    for entry in bundle["entry"]:
        assert "request" not in entry
        assert "fullUrl" in entry
        assert "resource" in entry


def test_bundle_collection_rejects_non_resource_entry():
    with pytest.raises(ValueError):
        fhir.build_bundle_collection([{"foo": "bar"}])


def test_bundle_transaction_validates():
    p = fhir.build_patient(family="A", given="B", birth_date="1990-01-01")
    bundle = fhir.build_bundle_transaction([p])
    assert not fhir.validate(bundle)


# =====================================================================
# Validator
# =====================================================================

def test_validate_succeeds_on_valid_resource():
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    assert fhir.validate(p) == []


def test_validate_catches_wrong_birthdate_type():
    """birthDate must be an ISO date string, not an arbitrary object."""
    bad = {"resourceType": "Patient", "birthDate": {"foo": "bar"}}
    issues = fhir.validate(bad)
    assert any(i.severity == "error" and "birthDate" in i.location for i in issues)


def test_validate_catches_malformed_birthdate_string():
    bad = {"resourceType": "Patient", "birthDate": "not-a-date"}
    issues = fhir.validate(bad)
    errors = [i for i in issues if i.severity == "error"]
    assert errors
    assert any("birthDate" in i.location for i in errors)


def test_validate_catches_missing_resource_type():
    issues = fhir.validate({"foo": "bar"})
    assert any(i.code == "required" and "resourceType" in i.location for i in issues)


def test_validate_returns_info_for_unknown_resource_type():
    issues = fhir.validate({"resourceType": "FooResource"})
    assert len(issues) == 1
    assert issues[0].severity == "information"
    assert issues[0].code == "not-supported"


def test_validate_catches_non_dict_input():
    issues = fhir.validate(["not", "a", "dict"])  # type: ignore[arg-type]
    assert len(issues) == 1
    assert issues[0].severity == "fatal"


def test_validate_catches_observation_with_bad_quantity_value():
    """Quantity.value must be a decimal string per FHIR spec."""
    bad = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": fhir.LOINC, "code": "2160-0"}]},
        "valueQuantity": {"value": "not-a-number", "unit": "mg/dL"},
    }
    issues = fhir.validate(bad)
    assert any(i.severity == "error" for i in issues)


# =====================================================================
# Round-trip stability
# =====================================================================

def test_round_trip_preserves_patient_structure():
    p = fhir.build_patient(
        family="Doe", given=["Jane", "M"], birth_date="1985-06-15", gender="female",
        mrn="MRN-1", telecom_phone="555-1234",
    )
    p2 = fhir.round_trip(p)
    # Identifying invariants survive
    assert p2["id"] == p["id"]
    assert p2["birthDate"] == p["birthDate"]
    assert p2["name"] == p["name"]
    assert p2["identifier"] == p["identifier"]


def test_round_trip_preserves_observation_value():
    o = fhir.build_observation(
        patient_ref="pat-1", code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
    )
    o2 = fhir.round_trip(o)
    assert o2["valueQuantity"] == o["valueQuantity"]


# =====================================================================
# Integration: build → validate → assert clean across builders
# =====================================================================

def _build_one(name: str) -> dict:
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    if name == "patient":
        return p
    if name == "encounter":
        return fhir.build_encounter(
            patient_ref=p["id"], encounter_class_code="AMB",
            period_start="2026-01-01T10:00:00Z",
        )
    if name == "observation":
        return fhir.build_observation(
            patient_ref=p["id"], code_loinc="2160-0", code_display="Creatinine",
            value=1.2, unit_ucum="mg/dL",
        )
    if name == "diagnostic_report":
        o = fhir.build_observation(
            patient_ref=p["id"], code_loinc="2160-0", code_display="Creatinine",
            value=1.2, unit_ucum="mg/dL",
        )
        return fhir.build_diagnostic_report(
            patient_ref=p["id"], code_loinc="LP29684-5", observations=[o["id"]],
        )
    if name == "claim":
        return fhir.build_claim(
            patient_ref=p["id"], provider_ref="prac-1",
            total_amount=150, diagnosis_codes=["E11.9"],
            service_lines=[{"cpt": "99213", "charge": 150}],
        )
    if name == "claim_response":
        c = fhir.build_claim(
            patient_ref=p["id"], provider_ref="prac-1",
            total_amount=150, diagnosis_codes=["E11.9"],
            service_lines=[{"cpt": "99213", "charge": 150}],
        )
        return fhir.build_claim_response(
            claim_ref=c["id"], patient_ref=p["id"], insurer_ref="ins-1",
            total_paid=120,
        )
    if name == "imaging_study":
        return fhir.build_imaging_study(
            patient_ref=p["id"], study_uid="1.2.3.4.5", modality_code="CT",
        )
    raise ValueError(name)


@pytest.mark.parametrize("name", [
    "patient",
    "encounter",
    "observation",
    "diagnostic_report",
    "claim",
    "claim_response",
    "imaging_study",
])
def test_all_builders_pass_validator(name):
    """Every builder's output must pass validate() with zero errors."""
    res = _build_one(name)
    issues = fhir.validate(res)
    errors = [i for i in issues if i.severity in ("error", "fatal")]
    assert not errors, f"{name} produced errors: {[(i.code, i.location, i.message) for i in errors]}"


@pytest.mark.parametrize("name", [
    "patient",
    "encounter",
    "observation",
    "diagnostic_report",
    "claim",
    "claim_response",
    "imaging_study",
])
def test_all_builders_produce_json_serializable_dicts(name):
    """The dict returned by each builder must json.dumps cleanly."""
    res = _build_one(name)
    s = json.dumps(res)
    assert json.loads(s) == res


# =====================================================================
# Patient with all 18 demographic fields
# =====================================================================

def test_patient_with_full_demographics():
    p = fhir.build_patient(
        family="Smith-Jones", given=["Mary", "Anne", "Beth"],
        birth_date="1972-03-12", gender="female",
        mrn="MRN-12345", ssn="123456789",
        address_line=["42 Galaxy Way", "Apt 4B"],
        address_city="Springfield", address_state="IL",
        address_postal_code="62701",
        telecom_phone="+1-555-0100", telecom_email="mary@example.com",
        patient_id="full-demo-patient",
    )
    # Verify all the high-leverage fields landed
    assert p["id"] == "full-demo-patient"
    assert p["name"][0]["given"] == ["Mary", "Anne", "Beth"]
    assert p["name"][0]["family"] == "Smith-Jones"
    assert p["birthDate"] == "1972-03-12"
    assert p["gender"] == "female"
    assert len(p["identifier"]) == 2
    assert len(p["telecom"]) == 2
    assert p["address"][0]["line"] == ["42 Galaxy Way", "Apt 4B"]
    assert p["address"][0]["city"] == "Springfield"
    assert p["address"][0]["postalCode"] == "62701"
    # And it validates clean
    assert fhir.validate(p) == []


# =====================================================================
# Cross-referencing sanity check
# =====================================================================

def test_observation_references_its_patient():
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    o = fhir.build_observation(
        patient_ref=p["id"], code_loinc="2160-0", code_display="Creatinine",
        value=1.2, unit_ucum="mg/dL",
    )
    assert o["subject"]["reference"] == f"Patient/{p['id']}"


def test_claim_response_references_its_claim():
    p = fhir.build_patient(family="Doe", given="Jane", birth_date="1990-01-01")
    c = fhir.build_claim(
        patient_ref=p["id"], provider_ref="prac-1",
        total_amount=150, diagnosis_codes=["E11.9"],
        service_lines=[{"cpt": "99213", "charge": 150}],
    )
    cr = fhir.build_claim_response(
        claim_ref=c["id"], patient_ref=p["id"], insurer_ref="ins-1",
        total_paid=120,
    )
    assert cr["request"]["reference"] == f"Claim/{c['id']}"
    assert cr["patient"]["reference"] == f"Patient/{p['id']}"
    assert cr["insurer"]["reference"] == "Organization/ins-1"


# =====================================================================
# FhirIssue dataclass shape
# =====================================================================

def test_fhir_issue_dataclass_fields():
    issue = fhir.FhirIssue(severity="error", code="invalid", message="x", location="y")
    assert issue.severity == "error"
    assert issue.code == "invalid"
    assert issue.message == "x"
    assert issue.location == "y"


def test_fhir_issue_default_location():
    issue = fhir.FhirIssue(severity="warning", code="structure", message="m")
    assert issue.location == ""
