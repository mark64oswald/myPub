"""Tests for healthcare_libs.hl7v2 — production HL7 v2 reference impl."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

from healthcare_libs import hl7v2  # noqa: E402


# ---------------------------------------------------------------------------
# Build / parse round-trip per builder
# ---------------------------------------------------------------------------

def test_build_adt_a01_parses_back_with_correct_segments():
    """A01 emits MSH+EVN+PID+PV1 and the parser surfaces all four."""
    wire = hl7v2.build_adt_a01()
    msg = hl7v2.parse(wire)
    assert msg.msh, "MSH should be populated"
    assert msg.pid, "PID should be populated"
    assert msg.pv1 is not None, "A01 must include PV1"
    assert msg.raw == wire.replace("\r\n", "\r").replace("\n", "\r").strip()


def test_build_adt_a03_parses_back_with_discharge_disposition():
    """A03 = discharge; PV1-36 should carry the discharge disposition code."""
    wire = hl7v2.build_adt_a03(discharge_disposition="07")
    msg = hl7v2.parse(wire)
    assert msg.msh["trigger_event"] == "A03"
    assert msg.pv1 is not None
    assert msg.pv1["discharge_disposition"] == "07"
    # PV1-45 should also be filled (discharge datetime)
    assert msg.pv1["discharge_datetime"], "A03 should set PV1-45 discharge datetime"


def test_build_adt_a08_uses_adt_a01_structure():
    """ADT^A08 (update) reuses the ADT_A01 structure per HL7 v2.5 §3.1."""
    wire = hl7v2.build_adt_a08()
    msg = hl7v2.parse(wire)
    assert msg.msh["trigger_event"] == "A08"
    assert msg.msh["message_structure"] == "ADT_A01", \
        "A08 should use ADT_A01 as MSH-9.3 structure"
    assert msg.pid["last_name"] == "DOE"


def test_build_oru_r01_parses_back_with_obr_and_obx():
    """ORU^R01 round-trips OBR + OBX list intact."""
    wire = hl7v2.build_oru_r01()
    msg = hl7v2.parse(wire)
    assert msg.msh["message_code"] == "ORU"
    assert msg.msh["trigger_event"] == "R01"
    assert len(msg.obr) >= 1, "ORU R01 must have at least one OBR"
    assert len(msg.obx) >= 1, "ORU R01 must have at least one OBX"


# ---------------------------------------------------------------------------
# MSH-9 message-type carries the right code per builder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder,expected_type,expected_trigger", [
    (hl7v2.build_adt_a01, "ADT", "A01"),
    (hl7v2.build_adt_a03, "ADT", "A03"),
    (hl7v2.build_adt_a08, "ADT", "A08"),
    (hl7v2.build_oru_r01, "ORU", "R01"),
])
def test_msh9_message_type_is_correct(builder, expected_type, expected_trigger):
    """MSH-9.1 (message code) and MSH-9.2 (trigger event) match the builder."""
    wire = builder()
    msg = hl7v2.parse(wire)
    assert msg.msh["message_code"] == expected_type
    assert msg.msh["trigger_event"] == expected_trigger
    # MSH-9 raw should look like "TYPE^TRIGGER^STRUCTURE"
    assert msg.msh["message_type"].startswith(f"{expected_type}^{expected_trigger}^")


# ---------------------------------------------------------------------------
# PID flow-through — patient ID, name, DOB
# ---------------------------------------------------------------------------

def test_pid_patient_id_flows_through_builders():
    """Custom MRN passed to builder appears in PID-3 of parsed message."""
    wire = hl7v2.build_adt_a01(patient_mrn="ABC99999")
    msg = hl7v2.parse(wire)
    assert msg.pid["patient_id"] == "ABC99999"
    assert "ABC99999" in msg.pid["patient_id_list"]


def test_pid_name_components_flow_through():
    """First / last / middle name set on builder appear in PID-5 components."""
    wire = hl7v2.build_adt_a01(
        patient_first="ALICE", patient_last="ANDERSON",
        patient_middle="MARIE",
    )
    msg = hl7v2.parse(wire)
    assert msg.pid["last_name"] == "ANDERSON"
    assert msg.pid["first_name"] == "ALICE"
    assert msg.pid["middle_name"] == "MARIE"
    assert msg.pid["name"] == "ANDERSON^ALICE^MARIE"


def test_pid_dob_flows_through():
    """PID-7 (DOB) set on builder appears in parsed message."""
    wire = hl7v2.build_adt_a01(patient_dob="19720101")
    msg = hl7v2.parse(wire)
    assert msg.pid["dob"] == "19720101"


def test_pid_sex_flows_through():
    wire = hl7v2.build_adt_a01(patient_sex="F")
    msg = hl7v2.parse(wire)
    assert msg.pid["sex"] == "F"


# ---------------------------------------------------------------------------
# ORU R01: multiple OBX preservation + ordering
# ---------------------------------------------------------------------------

def test_oru_r01_multiple_obx_preserved_in_order():
    """A list of OBX observations comes out in submission order."""
    obs = [
        {"value_type": "NM", "observation_id": "WBC^White Blood Cells^L",
         "value": "7.5", "units": "10*3/uL", "reference_range": "4.0-11.0",
         "abnormal_flags": "N", "status": "F"},
        {"value_type": "NM", "observation_id": "RBC^Red Blood Cells^L",
         "value": "4.5", "units": "10*6/uL", "reference_range": "4.2-5.4",
         "abnormal_flags": "N", "status": "F"},
        {"value_type": "NM", "observation_id": "HGB^Hemoglobin^L",
         "value": "14.0", "units": "g/dL", "reference_range": "12.0-16.0",
         "abnormal_flags": "N", "status": "F"},
        {"value_type": "NM", "observation_id": "HCT^Hematocrit^L",
         "value": "42.0", "units": "%", "reference_range": "37.0-47.0",
         "abnormal_flags": "N", "status": "F"},
    ]
    wire = hl7v2.build_oru_r01(observations=obs)
    msg = hl7v2.parse(wire)
    assert len(msg.obx) == 4
    # Set IDs should be 1, 2, 3, 4 in order
    assert [o["set_id"] for o in msg.obx] == ["1", "2", "3", "4"]
    # Observation IDs preserved in order
    assert msg.obx[0]["observation_id"].startswith("WBC")
    assert msg.obx[1]["observation_id"].startswith("RBC")
    assert msg.obx[2]["observation_id"].startswith("HGB")
    assert msg.obx[3]["observation_id"].startswith("HCT")
    # Values preserved
    assert msg.obx[0]["observation_value"] == "7.5"
    assert msg.obx[3]["observation_value"] == "42.0"


def test_oru_r01_obx_units_and_range_preserved():
    """Units (OBX-6) and reference range (OBX-7) survive round-trip."""
    obs = [{
        "value_type": "NM", "observation_id": "GLU^Glucose^L", "value": "95",
        "units": "mg/dL", "reference_range": "70-100",
        "abnormal_flags": "N", "status": "F",
    }]
    wire = hl7v2.build_oru_r01(observations=obs)
    msg = hl7v2.parse(wire)
    assert msg.obx[0]["units"] == "mg/dL"
    assert msg.obx[0]["reference_range"] == "70-100"
    assert msg.obx[0]["abnormal_flags"] == "N"
    assert msg.obx[0]["observation_status"] == "F"


def test_oru_r01_default_obx_when_none_provided():
    """Calling build_oru_r01() with no observations still produces a valid msg."""
    wire = hl7v2.build_oru_r01()  # no observations arg
    msg = hl7v2.parse(wire)
    assert len(msg.obx) >= 1, "default should emit at least one OBX"
    issues = hl7v2.validate(wire)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors


# ---------------------------------------------------------------------------
# Parser correctly extracts every common segment
# ---------------------------------------------------------------------------

def test_parse_extracts_msh_pid_pv1_for_adt():
    """ADT messages surface MSH, PID, PV1 in the parsed dataclass."""
    wire = hl7v2.build_adt_a01(
        sending_app="MIRTH", receiving_app="EPIC",
        patient_mrn="MRN-1234567",
    )
    msg = hl7v2.parse(wire)
    # MSH
    assert msg.msh["sending_app"] == "MIRTH"
    assert msg.msh["receiving_app"] == "EPIC"
    # PID
    assert msg.pid["patient_id"] == "MRN-1234567"
    # PV1
    assert msg.pv1 is not None
    assert msg.pv1["patient_class"] == "I"


def test_parse_extracts_obr_and_obx_for_oru():
    wire = hl7v2.build_oru_r01(
        placer_order_number="LAB-ORDER-555",
        filler_order_number="LAB-FILL-555",
        universal_service_id="CMP^Comprehensive Metabolic Panel^L",
    )
    msg = hl7v2.parse(wire)
    assert len(msg.obr) == 1
    assert msg.obr[0]["placer_order_number"] == "LAB-ORDER-555"
    assert msg.obr[0]["filler_order_number"] == "LAB-FILL-555"
    assert msg.obr[0]["universal_service_id"].startswith("CMP")


def test_parse_pv1_is_none_when_absent():
    """ORU R01 has no PV1 → parser returns pv1=None, not crash."""
    wire = hl7v2.build_oru_r01()
    msg = hl7v2.parse(wire)
    assert msg.pv1 is None


# ---------------------------------------------------------------------------
# Validator: catches missing required segments + malformed segments
# ---------------------------------------------------------------------------

def test_validate_catches_missing_pid():
    """Removing PID from an ADT^A01 should produce an error."""
    wire = hl7v2.build_adt_a01()
    # Strip the PID segment line
    segments = [s for s in wire.split("\r") if not s.startswith("PID")]
    bad = "\r".join(segments)
    issues = hl7v2.validate(bad)
    pid_errors = [i for i in issues if i.code == "PID"]
    assert pid_errors, f"expected PID-missing error, got: {[i.message for i in issues]}"
    assert pid_errors[0].severity == "error"


def test_validate_catches_missing_pv1_in_adt():
    """ADT^A01 with no PV1 should fail validation."""
    wire = hl7v2.build_adt_a01()
    segments = [s for s in wire.split("\r") if not s.startswith("PV1")]
    bad = "\r".join(segments)
    issues = hl7v2.validate(bad)
    pv1_errors = [i for i in issues if i.code == "PV1"]
    assert pv1_errors, f"expected PV1-missing error, got: {[i.message for i in issues]}"


def test_validate_catches_malformed_msh_too_few_fields():
    """An MSH segment with fewer than 12 fields is malformed."""
    bad = "MSH|^~\\&|EHR|HOSP\rPID|1||MRN12345"
    issues = hl7v2.validate(bad)
    msh_errors = [i for i in issues if i.code == "MSH"]
    assert msh_errors
    assert msh_errors[0].severity == "error"
    assert "expected at least 12" in msh_errors[0].message


def test_validate_returns_empty_for_clean_message():
    """A builder-generated message should produce zero error issues."""
    wire = hl7v2.build_adt_a01()
    issues = hl7v2.validate(wire)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, f"clean A01 produced errors: {[(i.code, i.message) for i in errors]}"


def test_validate_rejects_empty_string():
    issues = hl7v2.validate("")
    assert issues
    assert issues[0].severity == "error"


def test_validate_rejects_non_hl7():
    """A non-HL7 string should produce a structural error, not a crash."""
    issues = hl7v2.validate("hello world this is not HL7")
    assert issues
    assert issues[0].severity == "error"
    assert issues[0].code == "STRUCT"


# ---------------------------------------------------------------------------
# round_trip helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder", [
    hl7v2.build_adt_a01,
    hl7v2.build_adt_a03,
    hl7v2.build_adt_a08,
    hl7v2.build_oru_r01,
])
def test_round_trip_returns_true_for_each_builder(builder):
    wire = builder()
    assert hl7v2.round_trip(wire), \
        f"{builder.__name__} output failed round_trip"


def test_round_trip_returns_false_for_garbage():
    assert hl7v2.round_trip("hello world") is False


# ---------------------------------------------------------------------------
# Segment accessor helpers — get_segments / get_field
# ---------------------------------------------------------------------------

def test_get_segments_filters_by_id():
    """get_segments should return only the requested segment type."""
    obs = [
        {"value_type": "NM", "observation_id": "WBC", "value": "7.5", "status": "F"},
        {"value_type": "NM", "observation_id": "RBC", "value": "4.5", "status": "F"},
        {"value_type": "NM", "observation_id": "HGB", "value": "14.0", "status": "F"},
    ]
    wire = hl7v2.build_oru_r01(observations=obs)
    obx_segs = hl7v2.get_segments(wire, "OBX")
    assert len(obx_segs) == 3, f"expected 3 OBX, got {len(obx_segs)}"
    obr_segs = hl7v2.get_segments(wire, "OBR")
    assert len(obr_segs) == 1
    msh_segs = hl7v2.get_segments(wire, "MSH")
    assert len(msh_segs) == 1
    # MSH segments are renumbered so MSH-9 lives at index 9
    assert "ORU" in msh_segs[0][9], f"MSH-9 should contain ORU, got {msh_segs[0][9]}"


def test_get_field_returns_correct_value():
    """get_field returns the field at the spec field number."""
    wire = hl7v2.build_adt_a01(patient_mrn="ZZZ-9999")
    # MSH-9 = message type
    assert hl7v2.get_field(wire, "MSH", 9) == "ADT^A01^ADT_A01"
    # PID-3 should contain the MRN
    pid_3 = hl7v2.get_field(wire, "PID", 3)
    assert pid_3 is not None
    assert "ZZZ-9999" in pid_3
    # PID-5 = patient name
    pid_5 = hl7v2.get_field(wire, "PID", 5)
    assert pid_5 == "DOE^JOHN"


def test_get_field_returns_none_for_missing_segment():
    """Asking for a segment that doesn't exist returns None, not crash."""
    wire = hl7v2.build_adt_a01()
    # NK1 isn't in the default A01
    assert hl7v2.get_field(wire, "NK1", 2) is None
    # Past end of segment
    assert hl7v2.get_field(wire, "MSH", 99) is None
    # Non-existent occurrence
    assert hl7v2.get_field(wire, "PID", 5, occurrence=99) is None


def test_get_field_indexes_obx_occurrences():
    """Multiple OBX segments are addressable by occurrence index."""
    obs = [
        {"value_type": "NM", "observation_id": "WBC", "value": "7.5", "status": "F"},
        {"value_type": "NM", "observation_id": "RBC", "value": "4.5", "status": "F"},
        {"value_type": "NM", "observation_id": "HGB", "value": "14.0", "status": "F"},
    ]
    wire = hl7v2.build_oru_r01(observations=obs)
    assert hl7v2.get_field(wire, "OBX", 5, occurrence=0) == "7.5"
    assert hl7v2.get_field(wire, "OBX", 5, occurrence=1) == "4.5"
    assert hl7v2.get_field(wire, "OBX", 5, occurrence=2) == "14.0"


# ---------------------------------------------------------------------------
# Parser robustness — non-HL7 input
# ---------------------------------------------------------------------------

def test_parse_rejects_non_hl7_input():
    """Garbage input raises ValueError with a useful message."""
    with pytest.raises(ValueError, match="not an HL7 v2 message"):
        hl7v2.parse("hello world this is plain text")


def test_parse_rejects_empty_string():
    with pytest.raises(ValueError, match="empty HL7 v2"):
        hl7v2.parse("")


def test_parse_rejects_whitespace_only():
    with pytest.raises(ValueError, match="empty HL7 v2"):
        hl7v2.parse("   \n   \r   \n")


def test_parse_normalizes_crlf_line_endings():
    """Real-world senders sometimes use \\r\\n; we normalize on ingress."""
    wire = hl7v2.build_adt_a01()
    # Replace \r with \r\n (Windows style)
    crlf_wire = wire.replace("\r", "\r\n")
    msg = hl7v2.parse(crlf_wire)
    assert msg.pid["last_name"] == "DOE"


# ---------------------------------------------------------------------------
# Special characters in names
# ---------------------------------------------------------------------------

def test_special_characters_in_patient_name_apostrophe():
    """Names like O'BRIEN survive the build → parse round-trip."""
    wire = hl7v2.build_adt_a01(patient_last="O'BRIEN", patient_first="SEAN")
    msg = hl7v2.parse(wire)
    assert msg.pid["last_name"] == "O'BRIEN"
    assert msg.pid["first_name"] == "SEAN"
    # Also passes validation (no encoding-character collision)
    issues = hl7v2.validate(wire)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors


def test_special_characters_in_patient_name_hyphen():
    """Hyphenated names (SMITH-JONES) survive the round-trip."""
    wire = hl7v2.build_adt_a01(patient_last="SMITH-JONES", patient_first="MARY")
    msg = hl7v2.parse(wire)
    assert msg.pid["last_name"] == "SMITH-JONES"


def test_special_characters_period_in_name():
    """Names like 'ST. JAMES' (with period) survive the round-trip."""
    wire = hl7v2.build_adt_a01(patient_last="ST. JAMES", patient_first="JOHN")
    msg = hl7v2.parse(wire)
    assert msg.pid["last_name"] == "ST. JAMES"


# ---------------------------------------------------------------------------
# Cross-validation — every builder passes the validator self-check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder", [
    hl7v2.build_adt_a01,
    hl7v2.build_adt_a03,
    hl7v2.build_adt_a08,
    hl7v2.build_oru_r01,
])
def test_all_builders_pass_validator_self_check(builder):
    """A builder's output should produce no error-level issues."""
    wire = builder()
    issues = hl7v2.validate(wire)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, \
        f"{builder.__name__} produced errors: {[(i.code, i.message) for i in errors]}"


# ---------------------------------------------------------------------------
# Parametrized envelope correctness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder,expected_struct", [
    (hl7v2.build_adt_a01, "ADT_A01"),
    (hl7v2.build_adt_a03, "ADT_A03"),
    (hl7v2.build_adt_a08, "ADT_A01"),  # A08 reuses A01 structure
    (hl7v2.build_oru_r01, "ORU_R01"),
])
def test_msh9_3_carries_correct_structure(builder, expected_struct):
    """MSH-9.3 (message structure) drives receiver dispatch — must be right."""
    wire = builder()
    msg = hl7v2.parse(wire)
    assert msg.msh["message_structure"] == expected_struct


@pytest.mark.parametrize("builder", [
    hl7v2.build_adt_a01,
    hl7v2.build_adt_a03,
    hl7v2.build_adt_a08,
    hl7v2.build_oru_r01,
])
def test_msh_version_is_25(builder):
    """Default version is HL7 v2.5 across all builders."""
    wire = builder()
    msg = hl7v2.parse(wire)
    assert msg.msh["version"] == "2.5"


@pytest.mark.parametrize("builder", [
    hl7v2.build_adt_a01,
    hl7v2.build_adt_a03,
    hl7v2.build_adt_a08,
    hl7v2.build_oru_r01,
])
def test_processing_id_defaults_to_production(builder):
    """MSH-11 defaults to 'P' (production)."""
    wire = builder()
    msg = hl7v2.parse(wire)
    assert msg.msh["processing_id"] == "P"


@pytest.mark.parametrize("builder", [
    hl7v2.build_adt_a01,
    hl7v2.build_adt_a03,
    hl7v2.build_adt_a08,
    hl7v2.build_oru_r01,
])
def test_segment_terminator_is_carriage_return(builder):
    """HL7 v2 spec mandates \\r between segments — never \\n."""
    wire = builder()
    # Builder output should contain \r segment separators
    assert "\r" in wire, "wire should contain \\r segment terminators"
    # And should NOT contain \n (we never emit it)
    assert "\n" not in wire, \
        f"wire must not contain \\n; got: {wire!r}"


# ---------------------------------------------------------------------------
# Empty optional segments — parser doesn't crash
# ---------------------------------------------------------------------------

def test_parser_handles_message_with_only_required_segments():
    """A bare-bones ORU with the minimum segments should parse fine."""
    # Manually compose a minimal ORU R01 — MSH, PID, OBR, OBX
    minimal = (
        "MSH|^~\\&|LAB|HOSP|EHR|CLINIC|20260101120000||"
        "ORU^R01^ORU_R01|MSG001|P|2.5\r"
        "PID|1||MRN001||DOE^JANE\r"
        "OBR|1|||CBC\r"
        "OBX|1|NM|WBC||7.5||4.0-11.0|N|||F"
    )
    msg = hl7v2.parse(minimal)
    assert msg.msh["sending_app"] == "LAB"
    assert msg.pid["patient_id"] == "MRN001"
    assert len(msg.obr) == 1
    assert len(msg.obx) == 1
    assert msg.obx[0]["observation_value"] == "7.5"
    # No PV1, no NK1 — should just be absent, not crash
    assert msg.pv1 is None
    assert msg.nk1 == []


def test_parser_handles_extra_optional_components_in_pid():
    """PID-5 with extra components beyond first/last/middle still parses."""
    # Build then surgically inject a PID-5 with suffix and degree
    wire = hl7v2.build_adt_a01()
    # Replace the default PID-5 ("DOE^JOHN") with a fully-qualified XPN
    wire2 = wire.replace(
        "DOE^JOHN", "DOE^JOHN^MICHAEL^JR^DR^^L",
    )
    msg = hl7v2.parse(wire2)
    assert msg.pid["last_name"] == "DOE"
    assert msg.pid["first_name"] == "JOHN"
    assert msg.pid["middle_name"] == "MICHAEL"


# ---------------------------------------------------------------------------
# HL7Issue dataclass shape
# ---------------------------------------------------------------------------

def test_hl7_issue_has_expected_fields():
    """HL7Issue is the structured-error dataclass returned by validate()."""
    issue = hl7v2.HL7Issue(
        severity="error", code="PID",
        message="missing required segment",
        segment_context="MSH-9=ADT^A01",
    )
    assert issue.severity == "error"
    assert issue.code == "PID"
    assert issue.message == "missing required segment"
    assert issue.segment_context == "MSH-9=ADT^A01"


def test_hl7_issue_segment_context_defaults_to_empty():
    issue = hl7v2.HL7Issue(severity="info", code="X", message="y")
    assert issue.segment_context == ""


# ---------------------------------------------------------------------------
# HL7Message dataclass shape
# ---------------------------------------------------------------------------

def test_hl7_message_dataclass_has_expected_fields():
    """HL7Message exposes msh, pid, pv1, obr, obx, nk1, raw."""
    wire = hl7v2.build_adt_a01()
    msg = hl7v2.parse(wire)
    assert isinstance(msg, hl7v2.HL7Message)
    assert isinstance(msg.msh, dict)
    assert isinstance(msg.pid, dict)
    assert msg.pv1 is None or isinstance(msg.pv1, dict)
    assert isinstance(msg.obr, list)
    assert isinstance(msg.obx, list)
    assert isinstance(msg.nk1, list)
    assert isinstance(msg.raw, str)
