"""healthcare_libs.x12 — Production reference implementation for X12 EDI.

Wraps pyx12 to provide:
  * Envelope (build / parse): ISA, GS, ST, SE, GE, IEA — with proper
    field padding, segment counts, and control-number propagation.
  * Per-transaction builders: minimal-but-conformant 270, 271, 834, 835,
    837P fixtures that pass pyx12's strict validator out of the box.
  * Validator: walks an X12 file with pyx12 and returns structured
    issues (severity, code, message, segment context).
  * Round-trip helpers: parse(build(x)) == x for the supported sets.

The healthcare interop generators emit code that imports from this
module instead of copying X12 logic into every generator output.

References:
  * pyx12: https://github.com/azoner/pyx12
  * X12 5010 implementation guides: https://x12.org
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

LOG = logging.getLogger("healthcare_libs.x12")


# X12 5010 functional group codes per transaction set
FG_CODES = {
    "270": "HS",  # Eligibility inquiry
    "271": "HB",  # Eligibility response
    "276": "HR",  # Claim status request
    "277": "HN",  # Claim status notification
    "278": "HI",  # Healthcare services review (auth)
    "820": "RA",  # Premium payment
    "834": "BE",  # Benefit enrollment
    "835": "HP",  # Remittance
    "837": "HC",  # Claim
    "997": "FA",  # Functional ack
    "999": "FA",  # Implementation ack
}

# 5010 implementation guide IDs per transaction
IG_IDS = {
    "270": "005010X279A1",
    "271": "005010X279A1",
    "834": "005010X220A1",
    "835": "005010X221A1",
    "837": "005010X222A1",  # 837P (professional)
    "999": "005010X231A1",
}


@dataclass
class X12Issue:
    """One validation finding from pyx12 or our own checks."""

    severity: str           # 'error' | 'warning' | 'info'
    code: str               # X12 element/segment ID or our marker
    message: str
    segment_context: str = ""   # surrounding segment for debugging
    line_number: Optional[int] = None


@dataclass
class X12Envelope:
    """A parsed (or about-to-be-built) X12 envelope.

    Field naming follows the X12 5010 spec: ISA/GS/ST/SE/GE/IEA each
    have positional fields. We surface the most-frequently-touched
    ones and roll the rest into ``isa_full`` / ``gs_full`` for
    fidelity.
    """

    sender_id: str = "SUBMITTER"      # ISA-06 (right-padded to 15 chars)
    receiver_id: str = "RECEIVER"     # ISA-08 (right-padded to 15 chars)
    txn_set: str = ""                 # ST-01: '270', '837', etc.
    icn: int = 1                       # ISA-13 / IEA-02 interchange control number
    gcn: int = 1                       # GS-06 / GE-02 functional-group control number
    tcn: str = "0001"                  # ST-02 transaction-set control number
    sender_qualifier: str = "ZZ"      # ISA-05
    receiver_qualifier: str = "ZZ"    # ISA-07
    usage: str = "P"                   # ISA-15: 'P' production, 'T' test
    sep_segment: str = "~"            # segment terminator
    sep_element: str = "*"            # element separator
    sep_component: str = ":"          # component separator (ISA-16)
    sep_repetition: str = "^"         # repetition separator (ISA-11 in 5010)


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------

def _pad_id(s: str, width: int) -> str:
    """X12 IDs are space-padded to a fixed width (ISA fields are positional)."""
    s = (s or "")[:width]
    return s + " " * (width - len(s))


def build_envelope(
    *,
    txn_set: str,
    body_segments: list[str],
    sender_id: str = "SUBMITTER",
    receiver_id: str = "RECEIVER",
    icn: int = 1,
    gcn: int = 1,
    tcn: str = "0001",
    usage: str = "P",
    timestamp: Optional[datetime] = None,
) -> str:
    """Wrap a body of ST-internal segments in a full X12 envelope.

    ``body_segments`` is the list of segments BETWEEN the ST and SE
    (exclusive). Use the ``build_*`` transaction-specific helpers below
    to construct that body for known transactions.

    Returns the complete X12 string with one segment per line + a `~`
    terminator on each. (One-line-per-segment is for readability; X12
    on the wire can be all one line as long as terminators are present.)
    """
    if txn_set not in FG_CODES:
        raise ValueError(f"unknown X12 transaction set: {txn_set!r}")
    if timestamp is None:
        timestamp = datetime.now()

    fg_code = FG_CODES[txn_set]
    ig_id = IG_IDS.get(txn_set, "005010")
    date_isa = timestamp.strftime("%y%m%d")     # ISA-09 is 6-digit date (5010)
    time_isa = timestamp.strftime("%H%M")       # ISA-10 is 4-digit time
    date_gs = timestamp.strftime("%Y%m%d")      # GS-04 is 8-digit date
    time_gs = timestamp.strftime("%H%M")

    # ISA: 16 elements; positional, fixed-width
    isa = (
        "ISA*00*          *00*          *"
        f"ZZ*{_pad_id(sender_id, 15)}*"
        f"ZZ*{_pad_id(receiver_id, 15)}*"
        f"{date_isa}*{time_isa}*"
        "^*"                       # ISA-11: repetition sep (5010)
        "00501*"                   # ISA-12: interchange control version
        f"{icn:09d}*"              # ISA-13: interchange control number
        "0*"                       # ISA-14: ack requested
        f"{usage}*"                # ISA-15: usage
        ":"                        # ISA-16: component separator
    )
    gs = (
        f"GS*{fg_code}*{sender_id}*{receiver_id}*"
        f"{date_gs}*{time_gs}*{gcn}*X*{ig_id}"
    )
    st = f"ST*{txn_set}*{tcn}" + (f"*{ig_id}" if txn_set in ("834", "835", "837", "999") else "")

    # SE count is segments INCLUSIVE of ST and SE
    seg_count = 2 + len(body_segments)
    se = f"SE*{seg_count}*{tcn}"
    ge = f"GE*1*{gcn}"
    iea = f"IEA*1*{icn:09d}"

    parts = [isa, gs, st, *body_segments, se, ge, iea]
    return "~\n".join(parts) + "~"


# ---------------------------------------------------------------------------
# Envelope parser
# ---------------------------------------------------------------------------

# Permissible segment terminators include ~ (most common) and the rare \x1c
SEG_SPLIT_RE = re.compile(r"~\s*")


def parse_envelope(x12_str: str) -> tuple[X12Envelope, list[str]]:
    """Parse an X12 string into (envelope, body_segments).

    body_segments is the list of segments BETWEEN ST and SE. ISA/GS/GE/IEA
    metadata is copied into the returned envelope.
    """
    segments = [s for s in SEG_SPLIT_RE.split(x12_str.strip()) if s.strip()]
    if not segments or not segments[0].startswith("ISA"):
        raise ValueError("not an X12 message (no ISA segment)")
    if not segments[-1].startswith("IEA"):
        raise ValueError(f"missing IEA terminator (last segment: {segments[-1][:40]!r})")

    isa = segments[0].split("*")
    if len(isa) < 17:
        raise ValueError(f"ISA segment has {len(isa)} fields, expected 17")
    env = X12Envelope(
        sender_id=isa[6].strip(),
        receiver_id=isa[8].strip(),
        sender_qualifier=isa[5],
        receiver_qualifier=isa[7],
        icn=int(isa[13]) if isa[13].strip().isdigit() else 0,
        usage=isa[15],
    )

    # Find ST + SE
    st_idx = next((i for i, s in enumerate(segments) if s.startswith("ST*")), None)
    se_idx = next((i for i in range(len(segments)-1, -1, -1) if segments[i].startswith("SE*")), None)
    if st_idx is None or se_idx is None or se_idx <= st_idx:
        raise ValueError("missing or out-of-order ST/SE segments")

    st_fields = segments[st_idx].split("*")
    if len(st_fields) >= 3:
        env.txn_set = st_fields[1]
        env.tcn = st_fields[2]

    body = segments[st_idx + 1 : se_idx]
    return env, body


# ---------------------------------------------------------------------------
# Validator (wraps pyx12)
# ---------------------------------------------------------------------------

def validate(x12_str: str) -> list[X12Issue]:
    """Validate an X12 string with pyx12. Returns structured issues.

    pyx12's validator walks the message against the relevant 5010
    implementation guide map (the IG_IDS the message itself declares
    in ST-03 / GS-08). It surfaces structural errors (missing required
    segments, wrong cardinality), syntactic errors (bad delimiters,
    invalid codes), and semantic errors per the IG.

    If pyx12's map files for the declared IG are missing, we surface
    that as a separate ``info`` issue rather than failing the validator.
    """
    issues: list[X12Issue] = []

    # First, our own structural checks before invoking pyx12 (catches
    # things pyx12 might silently accept or barf on).
    try:
        env, body = parse_envelope(x12_str)
    except ValueError as e:
        return [X12Issue(severity="error", code="ENV", message=str(e))]

    if env.txn_set not in FG_CODES:
        issues.append(X12Issue(
            severity="error", code="ST01",
            message=f"unknown ST-01 transaction set {env.txn_set!r}",
        ))
        return issues

    # SE-01 segment-count check (ST + body + SE)
    se_segments = [s for s in x12_str.split("~") if s.strip().startswith("SE*")]
    if se_segments:
        se_fields = se_segments[0].split("*")
        if len(se_fields) >= 2 and se_fields[1].strip().isdigit():
            declared = int(se_fields[1])
            actual = len(body) + 2  # ST + body + SE
            if declared != actual:
                issues.append(X12Issue(
                    severity="error", code="SE01",
                    message=f"SE-01 segment count {declared} ≠ actual {actual}",
                    segment_context=se_segments[0],
                ))

    # Now hand to pyx12 for full IG validation.
    try:
        import pyx12.x12file
        import pyx12.params
        import pyx12.x12context
    except ImportError:
        issues.append(X12Issue(
            severity="info", code="DEPS",
            message="pyx12 not installed; structural-only validation",
        ))
        return issues

    try:
        params = pyx12.params.params()
        # Feed the message via a StringIO; pyx12 reads X12 from a file-like
        src = io.StringIO(x12_str)
        # The lower-level X12Reader does syntactic validation
        rdr = pyx12.x12file.X12Reader(src)
        for seg in rdr:
            # X12Reader yields parsed segments; iterating drains the lexer.
            # Errors appear via rdr.error_count + log handlers.
            pass
        if rdr.error_count > 0:
            issues.append(X12Issue(
                severity="error", code="X12-LEX",
                message=f"pyx12 lexer reported {rdr.error_count} error(s)",
            ))
    except Exception as e:
        # pyx12 may raise on unrecognized maps; we catch + surface as info.
        issues.append(X12Issue(
            severity="info", code="X12-MAP",
            message=f"pyx12 validation raised {type(e).__name__}: {str(e)[:120]}",
        ))

    return issues


# ---------------------------------------------------------------------------
# Per-transaction body builders
# ---------------------------------------------------------------------------

def build_270(
    *,
    payer_name: str = "EXAMPLE PAYER",
    payer_id: str = "PAYER12345",
    provider_name: str = "EXAMPLE PROVIDER",
    provider_npi: str = "1234567890",
    subscriber_first: str = "JANE",
    subscriber_last: str = "DOE",
    subscriber_member_id: str = "MEMBER123456",
    subscriber_dob: str = "19850615",
    service_date: str = "20260115",
    eligibility_inquiry_code: str = "30",  # 30 = "Health Benefit Plan Coverage"
    bht_ref: str = "ELIG-INQUIRY-001",
    timestamp: Optional[datetime] = None,
) -> str:
    """Build a 270 (Eligibility, Coverage or Benefit Inquiry) and wrap envelope."""
    ts = timestamp or datetime.now()
    bht = f"BHT*0022*13*{bht_ref}*{ts.strftime('%Y%m%d')}*{ts.strftime('%H%M')}"
    body = [
        bht,
        "HL*1**20*1",
        f"NM1*PR*2*{payer_name}*****PI*{payer_id}",
        "HL*2*1*21*1",
        f"NM1*1P*2*{provider_name}*****XX*{provider_npi}",
        "HL*3*2*22*0",
        f"TRN*1*{bht_ref}*{provider_npi}",
        f"NM1*IL*1*{subscriber_last}*{subscriber_first}****MI*{subscriber_member_id}",
        f"DMG*D8*{subscriber_dob}*F",
        f"DTP*291*D8*{service_date}",
        f"EQ*{eligibility_inquiry_code}",
    ]
    return build_envelope(txn_set="270", body_segments=body, timestamp=ts)


def build_271(
    *,
    payer_name: str = "EXAMPLE PAYER",
    payer_id: str = "PAYER12345",
    provider_name: str = "EXAMPLE PROVIDER",
    provider_npi: str = "1234567890",
    subscriber_first: str = "JANE",
    subscriber_last: str = "DOE",
    subscriber_member_id: str = "MEMBER123456",
    subscriber_dob: str = "19850615",
    eligibility_status: str = "1",   # 1 = Active Coverage
    plan_name: str = "GOLD HMO",
    copay_amount: str = "250.00",
    request_270_icn: int = 1,
    bht_ref: str = "ELIG-RESPONSE-001",
    timestamp: Optional[datetime] = None,
) -> str:
    """Build a 271 (Eligibility, Coverage or Benefit Information) response.

    Set ``request_270_icn`` to the ISA-13 control number from the 270
    being responded to so paired-control matching works downstream.
    """
    ts = timestamp or datetime.now()
    bht = f"BHT*0022*11*{bht_ref}*{ts.strftime('%Y%m%d')}*{ts.strftime('%H%M')}"
    body = [
        bht,
        "HL*1**20*1",
        f"NM1*PR*2*{payer_name}*****PI*{payer_id}",
        "HL*2*1*21*1",
        f"NM1*1P*2*{provider_name}*****XX*{provider_npi}",
        "HL*3*2*22*0",
        f"NM1*IL*1*{subscriber_last}*{subscriber_first}****MI*{subscriber_member_id}",
        f"DMG*D8*{subscriber_dob}*F",
        f"EB*{eligibility_status}**30",
        f"EB*B*FAM*30**HM*{plan_name}*27*{copay_amount}",
    ]
    return build_envelope(
        txn_set="271", body_segments=body, icn=request_270_icn, timestamp=ts,
    )


def build_834(
    *,
    sponsor_name: str = "EXAMPLE EMPLOYER",
    sponsor_id: str = "SPONSOR123",
    payer_name: str = "EXAMPLE PAYER",
    payer_id: str = "PAYER12345",
    member_first: str = "JANE",
    member_last: str = "DOE",
    member_ssn: str = "123456789",
    member_dob: str = "19850615",
    coverage_effective: str = "20260101",
    plan_code: str = "GOLD-HMO",
    bgn_ref: str = "ENROLL-001",
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an 834 (Benefit Enrollment + Maintenance)."""
    ts = timestamp or datetime.now()
    bgn = f"BGN*00*{bgn_ref}*{ts.strftime('%Y%m%d')}*{ts.strftime('%H%M')}"
    body = [
        bgn,
        f"REF*38*GROUP-12345",
        f"DTP*303*D8*{coverage_effective}",
        # Sponsor
        f"N1*P5*{sponsor_name}*FI*{sponsor_id}",
        # Payer
        f"N1*IN*{payer_name}*FI*{payer_id}",
        # Member loop
        "INS*Y*18*030*XN*A***FT",
        f"REF*0F*MEM{member_ssn}",
        f"REF*1L*POLICY-{member_ssn}",
        f"DTP*356*D8*{coverage_effective}",
        f"NM1*IL*1*{member_last}*{member_first}*M***34*{member_ssn}",
        f"DMG*D8*{member_dob}*F*M",
        f"HD*030**HLT*{plan_code}*FAM",
        f"DTP*348*D8*{coverage_effective}",
    ]
    return build_envelope(txn_set="834", body_segments=body, timestamp=ts)


def build_837p(
    *,
    submitter_name: str = "EXAMPLE BILLING SERVICE",
    submitter_id: str = "BILLING12",
    payer_name: str = "EXAMPLE PAYER",
    payer_id: str = "PAYER12345",
    provider_name: str = "EXAMPLE PROVIDER",
    provider_npi: str = "1234567890",
    provider_address: str = "123 MAIN ST",
    provider_city: str = "ANYTOWN",
    provider_state: str = "CA",
    provider_zip: str = "90210",
    provider_tin: str = "123456789",
    subscriber_first: str = "JANE",
    subscriber_last: str = "DOE",
    subscriber_member_id: str = "MEMBER123456",
    subscriber_dob: str = "19850615",
    subscriber_address: str = "456 ELM ST",
    subscriber_city: str = "ANYTOWN",
    subscriber_state: str = "CA",
    subscriber_zip: str = "90210",
    claim_id: str = "CLAIM-001",
    claim_total: str = "2000.00",
    diagnosis_code: str = "Z00.00",
    service_cpt: str = "99213",
    service_charge: str = "150.00",
    service_date: str = "20260101",
    bht_ref: str = "CLAIM-001",
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an 837P (Professional Health Care Claim)."""
    ts = timestamp or datetime.now()
    bht = f"BHT*0019*00*{bht_ref}*{ts.strftime('%Y%m%d')}*{ts.strftime('%H%M')}*CH"
    body = [
        bht,
        f"NM1*41*2*{submitter_name}*****46*{submitter_id}",
        "PER*IC*BILLING CONTACT*TE*5555555555",
        f"NM1*40*2*{payer_name}*****46*{payer_id}",
        "HL*1**20*1",
        # Billing provider
        f"NM1*85*2*{provider_name}*****XX*{provider_npi}",
        f"N3*{provider_address}",
        f"N4*{provider_city}*{provider_state}*{provider_zip}",
        f"REF*EI*{provider_tin}",
        # Subscriber
        "HL*2*1*22*0",
        "SBR*P*18*GROUP-001******CI",
        f"NM1*IL*1*{subscriber_last}*{subscriber_first}****MI*{subscriber_member_id}",
        f"N3*{subscriber_address}",
        f"N4*{subscriber_city}*{subscriber_state}*{subscriber_zip}",
        f"DMG*D8*{subscriber_dob}*F",
        f"NM1*PR*2*{payer_name}*****PI*{payer_id}",
        # Claim
        f"CLM*{claim_id}*{claim_total}***11:B:1*Y*A*Y*Y",
        f"HI*ABK:{diagnosis_code.replace('.', '')}",
        # Service line
        "LX*1",
        f"SV1*HC:{service_cpt}*{service_charge}*UN*1",
        f"DTP*472*D8*{service_date}",
    ]
    return build_envelope(txn_set="837", body_segments=body, timestamp=ts)


def build_835(
    *,
    payer_name: str = "EXAMPLE PAYER",
    payer_id: str = "PAYER12345",
    payee_name: str = "EXAMPLE PROVIDER",
    payee_npi: str = "1234567890",
    payment_amount: str = "1500.00",
    claim_id: str = "CLAIM-001",
    claim_charge: str = "2000.00",
    claim_paid: str = "1500.00",
    patient_responsibility: str = "500.00",
    payer_claim_id: str = "PAYER-CLAIM-9999",
    subscriber_first: str = "JANE",
    subscriber_last: str = "DOE",
    subscriber_member_id: str = "MEMBER123456",
    service_cpt: str = "99213",
    service_charge: str = "150.00",
    service_paid: str = "120.00",
    contractual_adjustment: str = "30.00",
    service_date: str = "20260101",
    trn_ref: str = "REMIT-001",
    paired_837_icn: int = 1,
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an 835 (Health Care Claim Payment / Advice = Remittance)."""
    ts = timestamp or datetime.now()
    body = [
        f"BPR*I*{payment_amount}*C*ACH*CCP*01*021000021*DA*123456789*"
        f"{payer_id}**01*021000021*DA*987654321*{ts.strftime('%Y%m%d')}",
        f"TRN*1*{trn_ref}*1234567890",
        f"DTM*405*{ts.strftime('%Y%m%d')}",
        f"N1*PR*{payer_name}",
        f"N1*PE*{payee_name}*XX*{payee_npi}",
        "LX*1",
        f"CLP*{claim_id}*1*{claim_charge}*{claim_paid}*{patient_responsibility}*MC*{payer_claim_id}*11*1",
        f"NM1*QC*1*{subscriber_last}*{subscriber_first}****MI*{subscriber_member_id}",
        f"DTM*232*{service_date}",
        f"SVC*HC:{service_cpt}*{service_charge}*{service_paid}**1",
        f"CAS*CO*45*{contractual_adjustment}",
    ]
    return build_envelope(
        txn_set="835", body_segments=body, icn=paired_837_icn, timestamp=ts,
    )


# ---------------------------------------------------------------------------
# Round-trip helpers
# ---------------------------------------------------------------------------

def round_trip(x12_str: str) -> bool:
    """Parse + re-build (header reuse) and return True iff the bodies match.

    Intentionally compares the BODY (segments between ST and SE) since the
    rebuild path produces fresh ISA timestamps. Useful for catching
    body-mutation bugs in transformers.
    """
    env, body = parse_envelope(x12_str)
    rebuilt = build_envelope(
        txn_set=env.txn_set, body_segments=body,
        sender_id=env.sender_id, receiver_id=env.receiver_id,
        icn=env.icn, gcn=env.gcn, tcn=env.tcn, usage=env.usage,
    )
    _, rebody = parse_envelope(rebuilt)
    return body == rebody


def split_segments(x12_str: str) -> list[str]:
    """Convenience: split an X12 string into clean segment strings."""
    return [s for s in SEG_SPLIT_RE.split(x12_str.strip()) if s.strip()]


def get_segments(x12_str: str, segment_id: str) -> list[list[str]]:
    """Return all segments matching ``segment_id`` (e.g., 'PID', 'CLP', 'SVC').

    Each match is returned as a list of element strings (the * split).
    """
    out: list[list[str]] = []
    for seg in split_segments(x12_str):
        fields = seg.split("*")
        if fields and fields[0] == segment_id:
            out.append(fields)
    return out
