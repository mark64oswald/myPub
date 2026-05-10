"""Tests for healthcare_libs.x12 — production X12 EDI reference impl."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

from healthcare_libs import x12  # noqa: E402


# ---- Envelope build/parse round-trip ------------------------------------

@pytest.mark.parametrize("txn", ["270", "271", "834", "835", "837", "999"])
def test_envelope_build_parse_round_trip(txn):
    """build_envelope → parse_envelope round-trips the body losslessly."""
    body = ["BHT*0022*13*REF-001*20260115*1430"]
    msg = x12.build_envelope(
        txn_set=txn, body_segments=body,
        sender_id="SENDER", receiver_id="RECEIVER", icn=42, gcn=7,
    )
    env, parsed_body = x12.parse_envelope(msg)
    assert env.txn_set == txn
    assert env.sender_id == "SENDER"
    assert env.receiver_id == "RECEIVER"
    assert env.icn == 42
    assert parsed_body == body


def test_envelope_isa_field_widths_per_spec():
    """ISA fields are positional + fixed-width per X12 5010 §B.1.1."""
    msg = x12.build_envelope(
        txn_set="270", body_segments=["BHT*0022*13*REF*20260115*1430"],
        sender_id="ABC", receiver_id="X",
    )
    isa = msg.split("\n")[0]
    fields = isa.split("*")
    # ISA-06 (sender) + ISA-08 (receiver) are 15 chars padded right
    assert len(fields[6]) == 15, f"ISA-06 should be 15 chars, got {len(fields[6])}"
    assert len(fields[8]) == 15
    assert fields[6] == "ABC            "
    assert fields[8] == "X              "
    # ISA-13 (interchange control number) is 9 digits zero-padded
    assert fields[13] == "000000001"
    # ISA-12 (interchange control version) is "00501"
    assert fields[12] == "00501"


def test_envelope_se_count_is_correct():
    """SE-01 should be ST + body + SE = body_count + 2."""
    body = ["BHT*0022*13*X*20260115*1430", "HL*1**20*1", "EQ*30"]
    msg = x12.build_envelope(txn_set="270", body_segments=body)
    se_seg = next(s for s in msg.split("~") if s.strip().startswith("SE*"))
    se_fields = se_seg.split("*")
    assert se_fields[1] == str(len(body) + 2), \
        f"SE-01 mismatch: {se_fields[1]} vs expected {len(body) + 2}"


def test_envelope_unknown_txn_raises():
    with pytest.raises(ValueError, match="unknown X12 transaction set"):
        x12.build_envelope(txn_set="999999", body_segments=["BHT*0*0*0"])


# ---- Parser robustness --------------------------------------------------

def test_parse_rejects_non_x12():
    with pytest.raises(ValueError, match="not an X12"):
        x12.parse_envelope("hello world")


def test_parse_rejects_missing_iea():
    bogus = "ISA*00*          *00*          *ZZ*X              *ZZ*Y              *260115*1430*^*00501*000000001*0*P*:~ST*270*0001~SE*2*0001~"
    with pytest.raises(ValueError, match="missing IEA"):
        x12.parse_envelope(bogus)


def test_get_segments_filters_correctly():
    msg = x12.build_envelope(
        txn_set="270",
        body_segments=["BHT*0022*13*X*20260115*1430", "HL*1**20*1", "HL*2*1*21*1", "NM1*PR*2*PAYER"],
    )
    hl_segs = x12.get_segments(msg, "HL")
    assert len(hl_segs) == 2
    nm1_segs = x12.get_segments(msg, "NM1")
    assert len(nm1_segs) == 1
    assert nm1_segs[0][1] == "PR"


# ---- Per-transaction builders -------------------------------------------

def test_build_270_has_required_segments():
    msg = x12.build_270(subscriber_member_id="ABC123", service_date="20260201")
    env, body = x12.parse_envelope(msg)
    assert env.txn_set == "270"
    body_str = "~".join(body)
    # Required segments per X12 5010X279A1
    for required in ("BHT*", "HL*", "NM1*PR*", "NM1*1P*", "NM1*IL*", "EQ*"):
        assert required in body_str, f"missing {required} in 270 body"
    # Custom values flowed through
    assert "ABC123" in body_str
    assert "20260201" in body_str


def test_build_271_pairs_with_270_via_icn():
    """271 should reuse the 270's ICN so paired-control-number tracking works."""
    request = x12.build_270(timestamp=__import__("datetime").datetime(2026, 1, 15, 14, 30))
    req_env, _ = x12.parse_envelope(request)
    response = x12.build_271(request_270_icn=req_env.icn)
    resp_env, _ = x12.parse_envelope(response)
    assert resp_env.icn == req_env.icn
    assert resp_env.txn_set == "271"


def test_build_834_is_well_formed():
    msg = x12.build_834(member_first="ALEX", member_last="JONES",
                         coverage_effective="20260601")
    env, body = x12.parse_envelope(msg)
    assert env.txn_set == "834"
    body_str = "~".join(body)
    assert "BGN*" in body_str
    assert "INS*Y*" in body_str
    assert "ALEX" in body_str
    assert "20260601" in body_str


def test_build_837p_full_claim():
    msg = x12.build_837p(
        claim_id="CLM-99", claim_total="450.00",
        diagnosis_code="E11.9", service_cpt="99214", service_charge="225.00",
    )
    env, body = x12.parse_envelope(msg)
    assert env.txn_set == "837"
    body_str = "~".join(body)
    # Required loops
    assert "BHT*0019*" in body_str
    assert "NM1*41*" in body_str    # Submitter
    assert "NM1*40*" in body_str    # Receiver (payer)
    assert "NM1*85*" in body_str    # Billing provider
    assert "SBR*P*" in body_str     # Subscriber
    assert "NM1*IL*" in body_str    # Subscriber name
    assert "CLM*CLM-99" in body_str
    assert "HI*ABK:E119" in body_str  # ICD-10 with dot stripped
    assert "SV1*HC:99214*225.00" in body_str


def test_build_835_pairs_with_837():
    """835 should reuse the 837's ICN for paired-control matching."""
    claim = x12.build_837p()
    claim_env, _ = x12.parse_envelope(claim)
    remit = x12.build_835(paired_837_icn=claim_env.icn)
    remit_env, _ = x12.parse_envelope(remit)
    assert remit_env.icn == claim_env.icn
    assert remit_env.txn_set == "835"
    body_str = "~".join(x12.parse_envelope(remit)[1])
    assert "BPR*I*" in body_str
    assert "TRN*1*" in body_str
    assert "CLP*" in body_str
    assert "SVC*" in body_str
    assert "CAS*CO*" in body_str


# ---- round-trip helper --------------------------------------------------

@pytest.mark.parametrize("builder,kwargs", [
    (x12.build_270, {}),
    (x12.build_271, {"request_270_icn": 1}),
    (x12.build_834, {}),
    (x12.build_837p, {}),
    (x12.build_835, {"paired_837_icn": 1}),
])
def test_round_trip_helper_returns_true(builder, kwargs):
    msg = builder(**kwargs)
    assert x12.round_trip(msg)


# ---- Validator ----------------------------------------------------------

def test_validate_catches_se_count_mismatch():
    """If the SE-01 segment count is wrong, the validator should flag it."""
    body = ["BHT*0022*13*REF*20260115*1430", "HL*1**20*1"]
    msg = x12.build_envelope(txn_set="270", body_segments=body)
    # Hand-corrupt SE-01 by replacing the count with a wrong value
    bad_msg = msg.replace("SE*4*", "SE*99*")
    issues = x12.validate(bad_msg)
    se_issues = [i for i in issues if i.code == "SE01"]
    assert se_issues, f"expected SE-01 mismatch issue, got: {[i.message for i in issues]}"
    assert se_issues[0].severity == "error"


def test_validate_catches_unknown_txn_set():
    body = ["BHT*0022*13*REF*20260115*1430"]
    msg = x12.build_envelope(txn_set="270", body_segments=body)
    bad_msg = msg.replace("ST*270*", "ST*999999*")
    issues = x12.validate(bad_msg)
    st_issues = [i for i in issues if i.code == "ST01"]
    assert st_issues
    assert st_issues[0].severity == "error"


def test_validate_returns_empty_or_info_for_valid_message():
    """A clean message should produce no errors (info-level pyx12 messages OK)."""
    msg = x12.build_270()
    issues = x12.validate(msg)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, f"valid 270 produced errors: {errors}"


# ---- Cross-validation: builder output passes our own validator ----------

@pytest.mark.parametrize("builder,kwargs", [
    (x12.build_270, {}),
    (x12.build_271, {"request_270_icn": 1}),
    (x12.build_834, {}),
    (x12.build_837p, {}),
    (x12.build_835, {"paired_837_icn": 1}),
])
def test_all_builders_pass_validator_self_check(builder, kwargs):
    msg = builder(**kwargs)
    issues = x12.validate(msg)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, f"{builder.__name__} produced errors: {[(i.code, i.message) for i in errors]}"
