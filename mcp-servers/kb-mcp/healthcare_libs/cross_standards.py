"""healthcare_libs.cross_standards — Production cross-format transformers.

The five sibling modules (``x12``, ``hl7v2``, ``fhir``, ``dicom``, ``deid``)
each speak one healthcare interchange format. This module composes them
into runnable transformations across formats:

  * **HL7 v2 → FHIR R4** for demographic/visit/lab feeds:
      - ``adt_a01_to_patient_encounter`` — admission
      - ``adt_a03_to_encounter_discharge`` — discharge
      - ``adt_a08_to_patient_encounter`` — update (PUT semantics)
      - ``oru_r01_to_observation_bundle`` — lab results

  * **X12 EDI → FHIR R4** for the financial transactions:
      - ``x12_837p_to_claim`` — professional claim
      - ``x12_835_to_claim_response`` — remittance / payment

  * **DICOM → FHIR R4** for the imaging hierarchy:
      - ``dicom_study_to_imaging_study``

  * **Pipeline-level**:
      - ``deidentified_transform`` — wrap a transformer with a post-pass
        that strips identifiers from the result per a ``DeidConfig``
      - ``round_trip_supported`` — declarative table of which (source →
        target) pairs have an inverse partner in this module

Every transformer accepts a SOURCE message (HL7v2 wire string, X12 wire
string, DICOM ``Dataset``, or FHIR dict) and returns a ``TransformResult``
whose ``result`` is the TARGET resource (typically a FHIR Bundle or
resource dict). Hard errors raise ``ValueError`` (malformed source,
unsupported variant); soft losses (missing optional fields, lossy code
mappings, non-standard values) appear as strings on
``TransformResult.warnings`` so the caller can decide whether to forward,
quarantine, or fail the message.

Why a single module rather than per-pair files? The shared concerns —
canonical code-system maps, identifier system OIDs, address/telecom
parsing helpers, the ``TransformResult`` dataclass — would otherwise
duplicate across files. Keeping them here also makes it straightforward
to add inverse transformers later: ``patient_encounter_to_adt_a01``
would sit next to its forward partner.

References:
  * HL7 v2-to-FHIR Implementation Guide:
    https://build.fhir.org/ig/HL7/v2-to-fhir/
  * Da Vinci Health Care Payer Data Exchange (HRex / PDex / CRD):
    https://www.hl7.org/about/davinci/
  * FHIR R4B Claim ↔ X12 837 mapping (CMS Da Vinci):
    https://hl7.org/fhir/us/davinci-pas/STU2/
  * FHIR ImagingStudy ↔ DICOM mapping (PS3.18 + R4B):
    https://hl7.org/fhir/R4B/imagingstudy.html#dicom
  * HL7 v2.5 Tables 0001 (sex), 0004 (patient class), 0078 (abnormal
    flag), 0085 (observation result status), 0123 (result status),
    0125 (value type)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Union

from . import deid, dicom, fhir, hl7v2, x12

LOG = logging.getLogger("healthcare_libs.cross_standards")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TransformResult:
    """Returned by every transformer.

    ``result`` is the transformed resource — always a JSON-serializable
    dict (FHIR Bundle / resource), an X12 wire string, or an HL7 v2 wire
    string depending on the target format.

    ``warnings`` holds soft losses: optional fields the source omitted,
    code-mapping fall-throughs (an HL7 v2 sex code that wasn't in Table
    0001), or fields the transformer chose not to map because the
    target format has no equivalent.

    ``notes`` holds informational annotations: which mapping table was
    applied, which optional sections were synthesized, etc.

    Hard errors (malformed source, missing required fields) raise
    ``ValueError`` rather than landing here — warnings are for things a
    receiver should ACK with caveats, errors are for things that must be
    rejected.
    """

    result: Any
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_format: str = ""
    target_format: str = ""

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_note(self, msg: str) -> None:
        self.notes.append(msg)


# ---------------------------------------------------------------------------
# Code-system mapping tables (the small inline lookups per the spec)
# ---------------------------------------------------------------------------

# HL7 v2 Table 0001 — Administrative Sex → FHIR administrative-gender
HL7V2_SEX_TO_FHIR_GENDER = {
    "M": "male",
    "F": "female",
    "O": "other",
    "U": "unknown",
    "A": "unknown",   # ambiguous → unknown for FHIR's narrow value set
    "N": "unknown",   # not applicable
}

# HL7 v2 Table 0004 — Patient Class → FHIR Encounter.class (v3-ActCode)
# The FHIR Encounter.class system is v3-ActCode:
# http://terminology.hl7.org/CodeSystem/v3-ActCode
HL7V2_PATIENT_CLASS_TO_FHIR_CLASS = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
    "R": ("AMB", "recurring patient"),       # treat as ambulatory
    "B": ("AMB", "obstetrics"),               # closest match
    "C": ("AMB", "commercial account"),       # admin only — pick AMB
    "N": ("AMB", "not applicable"),
    "U": ("AMB", "unknown"),
}

# HL7 v2 Table 0123 — Result Status → FHIR DiagnosticReport.status
HL7V2_RESULT_STATUS_TO_DR_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "X": "cancelled",
    "S": "amended",         # partial
    "I": "registered",      # in-progress (no results)
    "A": "partial",
    "R": "registered",      # results stored, not yet verified
    "O": "registered",      # order received
}

# HL7 v2 Table 0085 — Observation Result Status → FHIR Observation.status
HL7V2_OBS_STATUS_TO_FHIR_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "I": "cancelled",       # in error / cancelled
    "S": "amended",
    "X": "cancelled",
    "R": "registered",
    "U": "preliminary",
    "W": "entered-in-error",  # post-original (rare)
}

# HL7 v2 Table 0078 — Abnormal Flags → FHIR observation-interpretation
# (https://terminology.hl7.org/CodeSystem-v3-ObservationInterpretation.html)
HL7V2_ABNORMAL_TO_FHIR_INTERPRETATION = {
    "H": ("H", "High"),
    "L": ("L", "Low"),
    "N": ("N", "Normal"),
    "A": ("A", "Abnormal"),
    "AA": ("HH", "Critical high"),     # commonly mapped, though spec is loose
    "HH": ("HH", "Critical high"),
    "LL": ("LL", "Critical low"),
    "<": ("L", "Below low normal"),
    ">": ("H", "Above high normal"),
    "S": ("S", "Susceptible"),
    "R": ("R", "Resistant"),
    "I": ("I", "Intermediate"),
    "U": ("U", "Significant change up"),
    "D": ("D", "Significant change down"),
    "B": ("B", "Better"),
    "W": ("W", "Worse"),
}

FHIR_INTERPRETATION_SYSTEM = (
    "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
)

# HL7 v2 Table 0125 — Value Type → FHIR Observation.value[x] dispatch hint.
# The mapping describes which FHIR value[x] choice we'll synthesize.
HL7V2_VALUE_TYPE_TO_FHIR_KIND = {
    "NM": "Quantity",
    "ST": "string",
    "FT": "string",
    "TX": "string",
    "CWE": "CodeableConcept",
    "CE": "CodeableConcept",
    "CF": "CodeableConcept",
    "CNE": "CodeableConcept",
    "DT": "dateTime",
    "TS": "dateTime",
    "DTM": "dateTime",
}


# ---------------------------------------------------------------------------
# Small parsing utilities
# ---------------------------------------------------------------------------

def _split_caret(value: str) -> list[str]:
    """Split an HL7 v2 component string by ``^``; never returns None."""
    if not value:
        return []
    return value.split("^")


def _parse_xpn(xpn: str) -> dict[str, str]:
    """Parse an HL7 v2 XPN (eXtended Person Name) into FHIR HumanName parts.

    XPN layout: ``family^given^middle^suffix^prefix^degree^name_type_code``.
    We always return a dict — missing components come out as empty strings
    rather than missing keys, so downstream code can use ``.get()`` safely.
    """
    parts = _split_caret(xpn)
    return {
        "family": parts[0] if len(parts) >= 1 else "",
        "given": parts[1] if len(parts) >= 2 else "",
        "middle": parts[2] if len(parts) >= 3 else "",
        "suffix": parts[3] if len(parts) >= 4 else "",
        "prefix": parts[4] if len(parts) >= 5 else "",
    }


def _parse_xad(xad: str) -> dict[str, str]:
    """Parse an HL7 v2 XAD (eXtended Address) into FHIR Address parts.

    XAD layout: ``street^other_designation^city^state^postal^country^address_type``.
    """
    parts = _split_caret(xad)
    return {
        "line": parts[0] if len(parts) >= 1 else "",
        "other": parts[1] if len(parts) >= 2 else "",
        "city": parts[2] if len(parts) >= 3 else "",
        "state": parts[3] if len(parts) >= 4 else "",
        "postal_code": parts[4] if len(parts) >= 5 else "",
        "country": parts[5] if len(parts) >= 6 else "",
    }


def _parse_xtn(xtn: str) -> str:
    """Parse an HL7 v2 XTN (eXtended Telecommunication Number) → phone digits.

    XTN layout in v2.5: ``raw_number^use^equip_type^email^country^area^local^ext``.
    For most purposes we want either the raw number (component 0) or, if
    that's empty, the area+local digits joined.
    """
    if not xtn:
        return ""
    parts = _split_caret(xtn)
    if parts and parts[0]:
        return parts[0]
    # Fallback: assemble from area + local
    if len(parts) >= 7 and parts[5] and parts[6]:
        return parts[5] + parts[6]
    return ""


def _parse_xcn(xcn: str) -> dict[str, str]:
    """Parse an HL7 v2 XCN (eXtended Composite ID + Name) → id + name parts.

    XCN layout: ``id^family^given^middle^suffix^prefix^degree``. The ID
    component is typically a provider's NPI (when followed by an
    assigning-authority of ``XX`` later in the string).
    """
    parts = _split_caret(xcn)
    return {
        "id": parts[0] if len(parts) >= 1 else "",
        "family": parts[1] if len(parts) >= 2 else "",
        "given": parts[2] if len(parts) >= 3 else "",
        "middle": parts[3] if len(parts) >= 4 else "",
        "suffix": parts[4] if len(parts) >= 5 else "",
        "prefix": parts[5] if len(parts) >= 6 else "",
    }


def _parse_cx_list(cx_field: str) -> list[dict[str, str]]:
    """Parse a PID-3 CX (Composite ID) list, repetitions joined by ``~``.

    Each CX is ``id^check_digit^check_digit_scheme^assigning_authority^id_type_code``.
    """
    if not cx_field:
        return []
    out: list[dict[str, str]] = []
    for rep in cx_field.split("~"):
        parts = _split_caret(rep)
        out.append({
            "id": parts[0] if len(parts) >= 1 else "",
            "check_digit": parts[1] if len(parts) >= 2 else "",
            "assigning_authority": parts[3] if len(parts) >= 4 else "",
            "id_type_code": parts[4] if len(parts) >= 5 else "",
        })
    return out


def _hl7_dt_to_fhir(dt_str: str) -> Optional[str]:
    """Convert an HL7 v2 date or datetime string to a FHIR-compatible string.

    HL7 v2 timestamps are ``YYYYMMDDHHMMSS[.SSSS][+ZZZZ]`` with truncation
    allowed at any field boundary. FHIR wants ``YYYY-MM-DD`` for dates
    and ``YYYY-MM-DDTHH:MM:SS+ZZ:ZZ`` (or ``Z``) for datetimes.

    Returns None if input is empty or unparseable.
    """
    if not dt_str:
        return None
    s = dt_str.strip()
    # Strip any timezone suffix for now and reattach later
    tz = ""
    m = re.match(r"^(\d{4,14}(?:\.\d+)?)([+-]\d{4})?$", s)
    if not m:
        return None
    digits = m.group(1).split(".")[0]
    tz = m.group(2) or ""
    if len(digits) < 4:
        return None
    year = digits[0:4]
    if len(digits) == 4:
        return year
    if len(digits) < 6:
        return None
    month = digits[4:6]
    if len(digits) == 6:
        return f"{year}-{month}"
    if len(digits) < 8:
        return None
    day = digits[6:8]
    if len(digits) == 8:
        return f"{year}-{month}-{day}"
    if len(digits) < 10:
        return f"{year}-{month}-{day}"
    hour = digits[8:10]
    minute = digits[10:12] if len(digits) >= 12 else "00"
    second = digits[12:14] if len(digits) >= 14 else "00"
    iso = f"{year}-{month}-{day}T{hour}:{minute}:{second}"
    if tz:
        # Convert ±HHMM to ±HH:MM for FHIR
        iso += f"{tz[0:3]}:{tz[3:5]}"
    else:
        iso += "Z"
    return iso


def _hl7_date_to_fhir(date_str: str) -> Optional[str]:
    """Convert an HL7 v2 ``YYYYMMDD`` to FHIR ``YYYY-MM-DD``. Empty → None."""
    if not date_str:
        return None
    s = date_str.strip()[:8]
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _icd10_with_dot(code: str) -> str:
    """Insert the ICD-10 decimal point: ``E119`` → ``E11.9``. Already-dotted stays."""
    if not code or "." in code:
        return code
    s = code.strip()
    # ICD-10-CM format: 3-char base (1 letter + 2 alphanumerics) + optional
    # 1-4 char extension after the dot. If 4+ chars and no dot, insert.
    if len(s) > 3 and s[0].isalpha():
        return s[0:3] + "." + s[3:]
    return s


def _icd10_no_dot(code: str) -> str:
    """Strip ICD-10 decimal: ``E11.9`` → ``E119``. Already-undotted stays."""
    return code.replace(".", "") if code else code


# ---------------------------------------------------------------------------
# HL7 v2 helper: extract a parsed message + raw segment access
# ---------------------------------------------------------------------------

def _coerce_hl7(message: Union[str, hl7v2.HL7Message]) -> tuple[hl7v2.HL7Message, str]:
    """Accept a wire string OR an already-parsed ``HL7Message``; return both.

    Many transformers want both the structured surface AND raw segment
    access (for fields the structured surface doesn't expose, like
    PID-19 SSN or PV1-44 admit datetime). Returning both means callers
    don't reparse.
    """
    if isinstance(message, hl7v2.HL7Message):
        return message, message.raw
    if isinstance(message, str):
        parsed = hl7v2.parse(message)
        return parsed, parsed.raw
    raise TypeError(
        f"hl7_message must be str or HL7Message, got {type(message).__name__}"
    )


# ---------------------------------------------------------------------------
# HL7 v2 ADT → FHIR Patient + Encounter
# ---------------------------------------------------------------------------

def _patient_from_pid(
    parsed: hl7v2.HL7Message,
    raw: str,
    *,
    mrn_system: str,
    patient_id: str,
    warnings: list[str],
) -> dict:
    """Build a FHIR Patient resource from PID + a few raw-segment lookups.

    The structured ``parsed.pid`` covers PID-3 (id list), PID-5 (name),
    PID-7 (DOB), PID-8 (sex), PID-11 (address), PID-13 (home phone). We
    pull PID-14 (work phone) and PID-19 (SSN) from raw segments because
    the v1 ``parse()`` surface doesn't expose them.
    """
    pid = parsed.pid

    # --- name: PID-5 XPN ---
    name_parts = _parse_xpn(pid.get("name", ""))
    given_list = []
    if name_parts["given"]:
        given_list.append(name_parts["given"])
    if name_parts["middle"]:
        given_list.append(name_parts["middle"])
    if not name_parts["family"]:
        warnings.append("PID-5: missing family name (lossy)")

    # --- gender: PID-8 via Table 0001 ---
    sex_raw = (pid.get("sex", "") or "").upper().strip()
    gender = HL7V2_SEX_TO_FHIR_GENDER.get(sex_raw, "unknown")
    if sex_raw and sex_raw not in HL7V2_SEX_TO_FHIR_GENDER:
        warnings.append(f"PID-8: sex code {sex_raw!r} not in Table 0001 → unknown")

    # --- birthDate: PID-7 ---
    dob = _hl7_date_to_fhir(pid.get("dob", ""))

    # --- address: PID-11 (XAD) ---
    addr_parts = _parse_xad(pid.get("address", ""))
    address_line = addr_parts["line"] or None
    address_city = addr_parts["city"] or None
    address_state = addr_parts["state"] or None
    address_postal = addr_parts["postal_code"] or None

    # --- phone: PID-13 home, PID-14 work, PID-19 SSN via raw ---
    home_phone_raw = pid.get("phone", "")
    home_phone = _parse_xtn(home_phone_raw) if home_phone_raw else ""
    work_phone_raw = hl7v2.get_field(raw, "PID", 14) or ""
    work_phone = _parse_xtn(work_phone_raw)
    ssn_raw = hl7v2.get_field(raw, "PID", 19) or ""
    ssn = ssn_raw.split("^")[0].strip() if ssn_raw else ""

    # --- MRN: prefer first PID-3 entry; fall back to id alone ---
    cx_list = _parse_cx_list(pid.get("patient_id_list", ""))
    mrn = ""
    for cx in cx_list:
        # Per HL7 §2A.14.5.5: id_type_code MR or PI = MRN
        if cx["id_type_code"] in ("MR", "PI", "MRN", ""):
            if cx["id"]:
                mrn = cx["id"]
                break
    if not mrn and pid.get("patient_id"):
        mrn = pid["patient_id"]

    return fhir.build_patient(
        family=name_parts["family"] or "UNKNOWN",
        given=given_list or ["UNKNOWN"],
        birth_date=dob or "1900-01-01",
        gender=gender,
        mrn=mrn or None,
        ssn=ssn or None,
        address_line=address_line,
        address_city=address_city,
        address_state=address_state,
        address_postal_code=address_postal,
        telecom_phone=home_phone or None,
        patient_id=patient_id,
    )


def _enrich_patient_with_telecom(patient: dict, work_phone: str) -> dict:
    """Add a work-phone telecom entry to an existing Patient dict.

    Our ``fhir.build_patient`` only emits a single home-phone telecom; the
    HL7 v2 source may carry both home and work, so we layer the second
    in post-build.
    """
    if not work_phone:
        return patient
    out = dict(patient)
    telecom = list(out.get("telecom", []))
    telecom.append({"system": "phone", "value": work_phone, "use": "work"})
    out["telecom"] = telecom
    return out


def _encounter_from_pv1(
    parsed: hl7v2.HL7Message,
    raw: str,
    *,
    patient_ref: str,
    encounter_id: str,
    is_discharge: bool,
    warnings: list[str],
    notes: list[str],
) -> dict:
    """Build a FHIR Encounter resource from PV1 + raw lookups.

    For ADT^A01/A08 we set ``period.start`` (admit time) only.
    For ADT^A03 we ALSO set ``period.end`` (discharge time).
    """
    if parsed.pv1 is None:
        # No PV1 → synthesize a minimal Encounter (still required by ADT
        # IGs that pair Patient with Encounter)
        warnings.append("PV1 segment absent — emitting Encounter with default class AMB")
        return fhir.build_encounter(
            patient_ref=patient_ref,
            encounter_class_code="AMB",
            status="finished",
            encounter_id=encounter_id,
        )

    # Class translation per Table 0004
    pc_raw = (parsed.pv1.get("patient_class", "") or "").upper().strip()
    if pc_raw in HL7V2_PATIENT_CLASS_TO_FHIR_CLASS:
        klass_code, klass_display = HL7V2_PATIENT_CLASS_TO_FHIR_CLASS[pc_raw]
    else:
        klass_code, klass_display = "AMB", "ambulatory (default)"
        if pc_raw:
            warnings.append(f"PV1-2: patient class {pc_raw!r} not in Table 0004 → AMB")

    # Periods
    admit_raw = hl7v2.get_field(raw, "PV1", 44) or ""
    discharge_raw = hl7v2.get_field(raw, "PV1", 45) or parsed.pv1.get(
        "discharge_datetime", ""
    )
    period_start = _hl7_dt_to_fhir(admit_raw) if admit_raw else None
    period_end = _hl7_dt_to_fhir(discharge_raw) if (is_discharge and discharge_raw) else None

    # Visit number (PV1-19): becomes Encounter.identifier
    visit_no_raw = hl7v2.get_field(raw, "PV1", 19) or ""
    visit_no = visit_no_raw.split("^")[0].strip() if visit_no_raw else ""

    encounter = fhir.build_encounter(
        patient_ref=patient_ref,
        encounter_class_code=klass_code,
        encounter_class_display=klass_display,
        status="in-progress" if not is_discharge else "finished",
        period_start=period_start,
        period_end=period_end,
        identifier_value=visit_no or None,
        encounter_id=encounter_id,
    )

    # Layer in attending practitioner reference (PV1-7 XCN)
    attending_xcn = parsed.pv1.get("attending_doctor", "")
    if attending_xcn:
        att = _parse_xcn(attending_xcn)
        npi = att["id"]
        if npi:
            encounter = dict(encounter)
            participants = list(encounter.get("participant", []))
            participants.append({
                "individual": {"reference": f"Practitioner/{npi}"},
            })
            encounter["participant"] = participants
            notes.append(f"PV1-7 attending mapped to Practitioner/{npi}")

    # Layer in assigned location (PV1-3 PL)
    location_raw = parsed.pv1.get("assigned_location", "")
    if location_raw:
        # Take first component as the location name (PL.point_of_care)
        loc_name = location_raw.split("^")[0].strip()
        if loc_name:
            encounter = dict(encounter)
            locations = list(encounter.get("location", []))
            locations.append({
                "location": {"display": loc_name, "reference": f"Location/{loc_name}"},
            })
            encounter["location"] = locations
            notes.append(f"PV1-3 location mapped to Location/{loc_name}")

    return encounter


def _adt_to_bundle(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str,
    is_discharge: bool,
    is_update: bool,
    source_format: str,
) -> TransformResult:
    """Shared driver for ADT^A01/A03/A08 → Patient + Encounter bundle."""
    parsed, raw = _coerce_hl7(hl7_message)

    # Confirm trigger event matches expectation
    msh_type = parsed.msh.get("message_type", "")
    if "^" not in msh_type:
        raise ValueError(f"MSH-9 missing trigger event: {msh_type!r}")

    warnings: list[str] = []
    notes: list[str] = []

    # IDs we'll cross-reference between Patient and Encounter
    patient_id = f"adt-patient-{fhir._new_id()}"
    encounter_id = f"adt-encounter-{fhir._new_id()}"

    patient = _patient_from_pid(
        parsed, raw, mrn_system=mrn_system,
        patient_id=patient_id, warnings=warnings,
    )

    # Layer in work phone if PID-14 was populated (build_patient only
    # emits home phone)
    work_phone_raw = hl7v2.get_field(raw, "PID", 14) or ""
    work_phone = _parse_xtn(work_phone_raw)
    if work_phone:
        patient = _enrich_patient_with_telecom(patient, work_phone)

    encounter = _encounter_from_pv1(
        parsed, raw,
        patient_ref=patient_id,
        encounter_id=encounter_id,
        is_discharge=is_discharge,
        warnings=warnings,
        notes=notes,
    )

    # ---- Bundle assembly ------------------------------------------------
    # For A01/A03 we use POST semantics (server assigns IDs); for A08 we
    # use PUT semantics so the receiver upserts on the supplied IDs.
    if is_update:
        # PUT each entry against its supplied id
        entries = [
            {
                "fullUrl": f"urn:uuid:{patient_id}",
                "resource": patient,
                "request": {"method": "PUT", "url": f"Patient/{patient_id}"},
            },
            {
                "fullUrl": f"urn:uuid:{encounter_id}",
                "resource": encounter,
                "request": {"method": "PUT", "url": f"Encounter/{encounter_id}"},
            },
        ]
    else:
        # POST each entry; the resources keep their generated id but
        # the server may reassign on creation
        entries = [
            {
                "fullUrl": f"urn:uuid:{patient_id}",
                "resource": patient,
                "request": {"method": "POST", "url": "Patient"},
            },
            {
                "fullUrl": f"urn:uuid:{encounter_id}",
                "resource": encounter,
                "request": {"method": "POST", "url": "Encounter"},
            },
        ]

    bundle = fhir.build_bundle_transaction(entries)

    return TransformResult(
        result=bundle,
        warnings=warnings,
        notes=notes,
        source_format=source_format,
        target_format="fhir.Bundle[Patient,Encounter]",
    )


def adt_a01_to_patient_encounter(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
) -> TransformResult:
    """Convert HL7v2 ADT^A01 (admission) to FHIR Patient + Encounter Bundle.

    Returns Bundle dict with two entries (Patient + Encounter) ready for
    POST as a transaction.

    Field mapping:
      PID-3 (patient identifier list, CX repetitions) → Patient.identifier
        (loop) — first MR/PI/empty entry becomes the MRN
      PID-5 (XPN: family^given^middle^suffix^prefix) → Patient.name
      PID-7 (DOB ``YYYYMMDD``) → Patient.birthDate
      PID-8 (sex per Table 0001) → Patient.gender (M→male, F→female,
        O→other, U→unknown)
      PID-11 (XAD: street^other^city^state^zip) → Patient.address
      PID-13 (XTN home phone) → Patient.telecom[system=phone, use=home]
      PID-14 (XTN work phone) → Patient.telecom[system=phone, use=work]
      PID-19 (SSN) → Patient.identifier[system=SYSTEM_SSN]
      PV1-2 (patient class per Table 0004) → Encounter.class
        (I→IMP, O→AMB, E→EMER, P→PRENC; v3-ActCode system)
      PV1-3 (PL assigned location) → Encounter.location.location (Reference
        with display = first component, name-based reference)
      PV1-7 (XCN attending) → Encounter.participant.individual
        (Practitioner reference by NPI from XCN component 1)
      PV1-19 (visit number) → Encounter.identifier
      PV1-44 (admit date/time) → Encounter.period.start
      PV1-45 (discharge date/time) → None (admission, not discharge)
    """
    return _adt_to_bundle(
        hl7_message,
        mrn_system=mrn_system,
        is_discharge=False,
        is_update=False,
        source_format="hl7v2.ADT_A01",
    )


def adt_a03_to_encounter_discharge(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
) -> TransformResult:
    """ADT^A03 = discharge. Updates an existing Encounter with discharge time.

    Field mapping is identical to A01 except:
      * Encounter.status = ``finished``
      * Encounter.period.end = PV1-45 (discharge date/time)

    The receiver is expected to merge this Encounter onto the Encounter
    created at admission (via the visit-number identifier from PV1-19).
    """
    return _adt_to_bundle(
        hl7_message,
        mrn_system=mrn_system,
        is_discharge=True,
        is_update=False,
        source_format="hl7v2.ADT_A03",
    )


def adt_a08_to_patient_encounter(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
) -> TransformResult:
    """ADT^A08 = update. Same shape as A01 but the bundle's request method
    is ``PUT`` (upsert) rather than ``POST``.

    Per the HL7 v2-to-FHIR Implementation Guide, A08 maps to a transaction
    Bundle whose entries declare PUT semantics so the receiver overwrites
    the existing Patient + Encounter records.
    """
    return _adt_to_bundle(
        hl7_message,
        mrn_system=mrn_system,
        is_discharge=False,
        is_update=True,
        source_format="hl7v2.ADT_A08",
    )


def adt_a04_to_patient_encounter(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
) -> TransformResult:
    """ADT^A04 = register a patient (outpatient registration / pre-admission).

    Structurally identical to A01 in v2.5 — same MSH/EVN/PID/PV1 segments —
    so the field mapping rules are the same as `adt_a01_to_patient_encounter`.
    The semantic difference is conveyed by the trigger event (A04 vs A01)
    on MSH-9; receivers route on that and decide whether the resulting
    Encounter is an outpatient visit, ED registration, or admission.
    PV1-2 (patient class) is the disambiguator: O / E / I.
    """
    return _adt_to_bundle(
        hl7_message,
        mrn_system=mrn_system,
        is_discharge=False,
        is_update=False,
        source_format="hl7v2.ADT_A04",
    )


# ---------------------------------------------------------------------------
# HL7 v2 ORU → FHIR Observation Bundle
# ---------------------------------------------------------------------------

def _parse_obx_value(
    obx: dict[str, Any],
    *,
    warnings: list[str],
) -> tuple[Union[float, str, dict, None], Optional[str]]:
    """Convert an OBX-5 value to a FHIR-ready value and an optional UCUM unit.

    Dispatches on OBX-2 per Table 0125. Returns ``(value, unit_ucum)``;
    ``unit_ucum`` is None for non-numeric kinds. ``value`` shape matches
    what ``fhir.build_observation`` expects:
      * ``Quantity`` → float
      * ``string`` → str
      * ``CodeableConcept`` → dict with system/code/display
    """
    vt = (obx.get("value_type", "") or "").upper().strip()
    raw_value = obx.get("observation_value", "")
    units = obx.get("units", "")

    kind = HL7V2_VALUE_TYPE_TO_FHIR_KIND.get(vt, "string")
    if vt and vt not in HL7V2_VALUE_TYPE_TO_FHIR_KIND:
        warnings.append(f"OBX-2: unknown value type {vt!r} (Table 0125) → string")

    if kind == "Quantity":
        try:
            num = float(str(raw_value).strip())
        except (TypeError, ValueError):
            warnings.append(
                f"OBX-5: NM value {raw_value!r} not numeric → degraded to string"
            )
            return str(raw_value), None
        # Units: take the first component (CE/CWE in OBX-6)
        unit_code = (units.split("^")[0].strip() if units else "") or "1"
        return num, unit_code

    if kind == "CodeableConcept":
        # OBX-5 for CWE: code^display^system or code^display
        parts = _split_caret(str(raw_value))
        code = parts[0] if len(parts) >= 1 else ""
        display = parts[1] if len(parts) >= 2 else None
        system_raw = parts[2] if len(parts) >= 3 else ""
        # Map the v2 coding-system designator to a FHIR system URI
        if system_raw == "LN":
            system = fhir.LOINC
        elif system_raw == "SCT":
            system = fhir.SNOMED
        elif system_raw and system_raw.startswith("http"):
            system = system_raw
        else:
            system = "http://terminology.hl7.org/CodeSystem/v2-0078"
        if not code:
            warnings.append("OBX-5: CWE value missing code component")
            return str(raw_value), None
        d: dict[str, str] = {"system": system, "code": code}
        if display:
            d["display"] = display
        return d, None

    if kind == "dateTime":
        # Render the dateTime to FHIR-style string-as-value
        iso = _hl7_dt_to_fhir(str(raw_value))
        return (iso or str(raw_value)), None

    # Default: string
    return str(raw_value), None


def _parse_reference_range(rr: str) -> tuple[Optional[float], Optional[float]]:
    """Parse OBX-7 reference range — supports ``low-high``, ``<X``, ``>X``.

    Returns ``(low, high)``; either may be None. If the format isn't
    parseable, returns ``(None, None)`` — the caller decides whether to
    drop or stringify.
    """
    if not rr or not rr.strip():
        return None, None
    s = rr.strip()
    # ``low-high`` (most common). Accept negative low (rare in clinical
    # ranges but technically possible).
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    # ``<X``  → upper bound only
    m = re.match(r"^<\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        return None, float(m.group(1))
    # ``>X``  → lower bound only
    m = re.match(r"^>\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        return float(m.group(1)), None
    return None, None


def oru_r01_to_observation_bundle(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
) -> TransformResult:
    """Convert HL7v2 ORU^R01 (lab result) to FHIR Bundle of
    Observations + DiagnosticReport.

    Returns a transaction Bundle with one Patient, one DiagnosticReport,
    and N Observations (one per OBX). The DiagnosticReport.result list
    references each Observation by its bundle-local id.

    Field mapping:
      PID-3/5/7/8 (etc.) → Patient (same as ADT)
      OBR-2 (placer order) → DiagnosticReport.basedOn (synthesized
        ServiceRequest reference)
      OBR-3 (filler order) → DiagnosticReport.identifier
      OBR-4 (universal service ID, CE/CWE) → DiagnosticReport.code (LOINC
        if OBR-4 component 3 = LN)
      OBR-7 (observation date/time) → DiagnosticReport.effectiveDateTime
      OBR-22 (status change date/time) → DiagnosticReport.issued
      OBR-25 (result status, Table 0123) → DiagnosticReport.status
      OBX-2 (value type, Table 0125) → Observation.value[x] dispatch
      OBX-3 (observation identifier) → Observation.code (LOINC if OBX-3
        component 3 = LN)
      OBX-5 (observation value) → Observation.valueQuantity / valueString
        / valueCodeableConcept
      OBX-6 (units) → Observation.valueQuantity.unit (UCUM)
      OBX-7 (reference range) → Observation.referenceRange (parses
        ``low-high``, ``<X``, ``>X``)
      OBX-8 (abnormal flag, Table 0078) → Observation.interpretation
      OBX-11 (result status, Table 0085) → Observation.status
      OBX-14 (observation date/time) → Observation.effectiveDateTime
        (falls back to OBR-7 if OBX-14 is absent)
    """
    parsed, raw = _coerce_hl7(hl7_message)
    if not parsed.obr:
        raise ValueError("ORU^R01 has no OBR segment — cannot build DiagnosticReport")
    if not parsed.obx:
        raise ValueError("ORU^R01 has no OBX segments — no observations to emit")

    warnings: list[str] = []
    notes: list[str] = []

    # ---- Patient ----
    patient_id = f"oru-patient-{fhir._new_id()}"
    patient = _patient_from_pid(
        parsed, raw, mrn_system=mrn_system,
        patient_id=patient_id, warnings=warnings,
    )
    work_phone_raw = hl7v2.get_field(raw, "PID", 14) or ""
    work_phone = _parse_xtn(work_phone_raw)
    if work_phone:
        patient = _enrich_patient_with_telecom(patient, work_phone)

    # ---- Observations ----
    obx_list = parsed.obx
    obr_first = parsed.obr[0]
    obr_eff = _hl7_dt_to_fhir(obr_first.get("observation_datetime", ""))

    observation_ids: list[str] = []
    observation_resources: list[dict] = []

    for i, obx in enumerate(obx_list, start=1):
        # OBX-3 → code
        obs_id_full = obx.get("observation_id", "")
        parts = _split_caret(obs_id_full)
        loinc_code = parts[0] if len(parts) >= 1 else "UNKNOWN"
        loinc_display = parts[1] if len(parts) >= 2 else loinc_code
        coding_system = parts[2] if len(parts) >= 3 else ""
        if coding_system and coding_system != "LN":
            warnings.append(
                f"OBX[{i}]-3: coding system {coding_system!r} not LOINC; "
                f"emitting as LOINC anyway (lossy)"
            )

        # OBX-5/6 → value
        value, unit = _parse_obx_value(obx, warnings=warnings)

        # OBX-11 → status
        status_raw = (obx.get("observation_status", "") or "F").upper()
        status = HL7V2_OBS_STATUS_TO_FHIR_STATUS.get(status_raw, "final")
        if status_raw not in HL7V2_OBS_STATUS_TO_FHIR_STATUS:
            warnings.append(f"OBX[{i}]-11: status {status_raw!r} not in Table 0085 → final")

        # OBX-7 → reference range (numeric only)
        rr_low, rr_high = _parse_reference_range(obx.get("reference_range", ""))

        # OBX-14 effective datetime — fall back to raw lookup, then OBR-7
        obx_eff_raw = hl7v2.get_field(raw, "OBX", 14, occurrence=i - 1) or ""
        obx_eff = _hl7_dt_to_fhir(obx_eff_raw) or obr_eff

        obs_resource_id = f"oru-obs-{i}-{fhir._new_id()}"

        # Build the Observation. For numeric values pass through reference
        # range; for non-numeric, omit (FHIR's referenceRange is allowed
        # on non-quantitative obs but its low/high need a Quantity, which
        # only makes sense for numerics).
        kwargs: dict = dict(
            patient_ref=patient_id,
            code_loinc=loinc_code,
            code_display=loinc_display,
            value=value,
            effective_datetime=obx_eff,
            status=status,
            observation_id=obs_resource_id,
        )
        if isinstance(value, (int, float)) and unit is not None:
            kwargs["unit_ucum"] = unit
            if rr_low is not None:
                kwargs["reference_range_low"] = rr_low
            if rr_high is not None:
                kwargs["reference_range_high"] = rr_high
        observation = fhir.build_observation(**kwargs)

        # OBX-8 → interpretation (post-build, since build_observation
        # doesn't expose this knob)
        flag = (obx.get("abnormal_flags", "") or "").upper().strip()
        if flag:
            interp = HL7V2_ABNORMAL_TO_FHIR_INTERPRETATION.get(flag)
            if interp is not None:
                code, display = interp
                observation = dict(observation)
                observation["interpretation"] = [{
                    "coding": [{
                        "system": FHIR_INTERPRETATION_SYSTEM,
                        "code": code,
                        "display": display,
                    }],
                }]
            else:
                warnings.append(
                    f"OBX[{i}]-8: abnormal flag {flag!r} not in Table 0078 (no interp emitted)"
                )

        observation_ids.append(obs_resource_id)
        observation_resources.append(observation)

    # ---- DiagnosticReport ----
    dr_code_full = obr_first.get("universal_service_id", "")
    dr_parts = _split_caret(dr_code_full)
    dr_loinc = dr_parts[0] if len(dr_parts) >= 1 else "REPORT"
    dr_display = dr_parts[1] if len(dr_parts) >= 2 else dr_loinc
    dr_coding_sys = dr_parts[2] if len(dr_parts) >= 3 else ""
    if dr_coding_sys and dr_coding_sys != "LN":
        warnings.append(
            f"OBR-4: coding system {dr_coding_sys!r} not LOINC; emitting as LOINC anyway"
        )

    # OBR-25 → DR status (Table 0123)
    obr25_raw = (hl7v2.get_field(raw, "OBR", 25) or "F").upper()
    dr_status = HL7V2_RESULT_STATUS_TO_DR_STATUS.get(obr25_raw, "final")
    if obr25_raw not in HL7V2_RESULT_STATUS_TO_DR_STATUS:
        warnings.append(f"OBR-25: result status {obr25_raw!r} not in Table 0123 → final")

    # OBR-22 → DR.issued
    issued_raw = hl7v2.get_field(raw, "OBR", 22) or ""
    issued = _hl7_dt_to_fhir(issued_raw)

    dr_id = f"oru-dr-{fhir._new_id()}"
    diag_report = fhir.build_diagnostic_report(
        patient_ref=patient_id,
        code_loinc=dr_loinc,
        code_display=dr_display,
        observations=observation_ids,
        status=dr_status,
        effective_datetime=obr_eff,
        issued=issued,
        report_id=dr_id,
    )

    # OBR-3 → DiagnosticReport.identifier
    filler_order = obr_first.get("filler_order_number", "")
    if filler_order:
        diag_report = dict(diag_report)
        identifiers = list(diag_report.get("identifier", []))
        identifiers.append({
            "system": "urn:oid:2.16.840.1.113883.4.642.40.5.10",
            "value": filler_order,
        })
        diag_report["identifier"] = identifiers
        notes.append(f"OBR-3 filler {filler_order} → DiagnosticReport.identifier")

    # OBR-2 → DiagnosticReport.basedOn (synthesize a ServiceRequest ref)
    placer_order = obr_first.get("placer_order_number", "")
    if placer_order:
        diag_report = dict(diag_report)
        based_on = list(diag_report.get("basedOn", []))
        based_on.append({"reference": f"ServiceRequest/{placer_order}"})
        diag_report["basedOn"] = based_on
        notes.append(f"OBR-2 placer {placer_order} → ServiceRequest reference")

    # ---- Bundle ----
    entries = [
        {
            "fullUrl": f"urn:uuid:{patient_id}",
            "resource": patient,
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": f"urn:uuid:{dr_id}",
            "resource": diag_report,
            "request": {"method": "POST", "url": "DiagnosticReport"},
        },
    ]
    for obs in observation_resources:
        entries.append({
            "fullUrl": f"urn:uuid:{obs['id']}",
            "resource": obs,
            "request": {"method": "POST", "url": "Observation"},
        })

    bundle = fhir.build_bundle_transaction(entries)
    return TransformResult(
        result=bundle,
        warnings=warnings,
        notes=notes,
        source_format="hl7v2.ORU_R01",
        target_format="fhir.Bundle[Patient,DiagnosticReport,Observation]",
    )


# ---------------------------------------------------------------------------
# X12 837P → FHIR Claim
# ---------------------------------------------------------------------------

def _money(s: str) -> float:
    """Parse an X12 monetary amount; raise ValueError on bad input."""
    try:
        return float(s)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid monetary amount: {s!r}") from e


def x12_837p_to_claim(x12_msg: str, **kwargs: Any) -> TransformResult:
    """Convert X12 837P (professional claim) to FHIR Claim resource.

    Walks the loops and pulls fields per the standard mapping derived from
    the CMS 005010X222A1 implementation guide (the "current" 837P version
    that nearly all production payers accept).

    Field mapping:
      ISA/GS/ST envelope → ignored (FHIR Claim has no transport metadata)
      BHT-3 → Claim.identifier
      NM1*85 (billing provider name) + NPI → Claim.provider (Practitioner ref)
      NM1*40 (receiver / payer) → Claim.insurer
      NM1*IL (subscriber) → Claim.patient (Patient ref)
      CLM-1 → Claim.identifier (claim id)
      CLM-2 → Claim.total (currency = USD by default)
      HI*ABK / *BK / *ABF / *BF → Claim.diagnosis (ICD-10-CM, dot inserted)
      LX + SV1 segments → Claim.item (per-line entries)
        SV1-1 → productOrService (CPT code)
        SV1-2 → unitPrice + net amount
        SV1-4 → quantity (units)
      DTP*472 → Claim.item.servicedDate
    """
    warnings: list[str] = []
    notes: list[str] = []

    env, body = x12.parse_envelope(x12_msg)
    if env.txn_set != "837":
        raise ValueError(f"expected ST-01=837, got {env.txn_set!r}")

    # Walk segments by type
    segs_by_type: dict[str, list[list[str]]] = {}
    for seg_str in body:
        fields = seg_str.split("*")
        if not fields:
            continue
        segs_by_type.setdefault(fields[0], []).append(fields)

    # ---- BHT (Beginning of Hierarchical Transaction) ----
    bht_id = ""
    if "BHT" in segs_by_type and len(segs_by_type["BHT"][0]) >= 4:
        bht_id = segs_by_type["BHT"][0][3]

    # ---- Provider, payer, subscriber via NM1 entity-type codes ----
    nm1s = segs_by_type.get("NM1", [])
    provider_name = ""
    provider_npi = ""
    payer_name = ""
    payer_id = ""
    subscriber_name_parts: dict[str, str] = {}
    subscriber_id = ""

    for nm1 in nm1s:
        if len(nm1) < 2:
            continue
        ent = nm1[1]
        if ent == "85":  # Billing provider
            provider_name = nm1[3] if len(nm1) > 3 else ""
            # NM1-9 is the ID; NM1-8 is the qualifier ("XX" = NPI)
            if len(nm1) >= 10 and nm1[8] == "XX":
                provider_npi = nm1[9]
        elif ent == "40":  # Receiver (payer)
            payer_name = nm1[3] if len(nm1) > 3 else ""
            if len(nm1) >= 10:
                payer_id = nm1[9]
        elif ent == "IL":  # Subscriber
            subscriber_name_parts = {
                "family": nm1[3] if len(nm1) > 3 else "",
                "given": nm1[4] if len(nm1) > 4 else "",
            }
            if len(nm1) >= 10 and nm1[8] == "MI":
                subscriber_id = nm1[9]

    if not provider_npi:
        warnings.append("NM1*85 NPI not present in 837P (provider ref will be by name)")
    if not subscriber_id:
        warnings.append("NM1*IL subscriber ID not present in 837P (patient ref synthesized)")

    # ---- CLM segment (claim header) ----
    clm = segs_by_type.get("CLM", [])
    if not clm:
        raise ValueError("837P missing required CLM (claim header) segment")
    clm_id = clm[0][1] if len(clm[0]) > 1 else ""
    clm_total_raw = clm[0][2] if len(clm[0]) > 2 else "0"
    try:
        claim_total = _money(clm_total_raw)
    except ValueError:
        warnings.append(f"CLM-2: invalid claim total {clm_total_raw!r} → 0.00")
        claim_total = 0.0

    # ---- HI segments (diagnosis) ----
    diag_codes: list[str] = []
    for hi in segs_by_type.get("HI", []):
        # Each HI element after the segment name is a composite:
        # qualifier:code:date_format:date:other (qualifier ABK = principal,
        # BK = principal pre-2015, ABF = secondary, BF = secondary pre-2015)
        for comp in hi[1:]:
            parts = comp.split(":")
            if len(parts) < 2:
                continue
            qual = parts[0]
            code = parts[1]
            if qual in ("ABK", "BK", "ABF", "BF") and code:
                diag_codes.append(_icd10_with_dot(code))
    if not diag_codes:
        warnings.append("No diagnosis codes (HI*ABK/BK/ABF/BF) found in 837P")
        diag_codes = ["Z00.00"]   # unspecified screening — FHIR requires ≥1

    # ---- LX + SV1 + DTP*472 service lines ----
    service_lines: list[dict] = []
    current_lx: Optional[int] = None
    current_sv1: Optional[list[str]] = None
    current_service_date: Optional[str] = None

    for seg_str in body:
        fields = seg_str.split("*")
        if not fields:
            continue
        seg = fields[0]
        if seg == "LX":
            # Flush any in-progress line
            if current_sv1:
                service_lines.append(_build_service_line(
                    current_lx, current_sv1, current_service_date, warnings,
                ))
                current_sv1 = None
                current_service_date = None
            current_lx = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
        elif seg == "SV1":
            current_sv1 = fields
        elif seg == "DTP" and len(fields) >= 4 and fields[1] == "472":
            current_service_date = fields[3]

    if current_sv1:
        service_lines.append(_build_service_line(
            current_lx, current_sv1, current_service_date, warnings,
        ))

    if not service_lines:
        warnings.append("No SV1 service lines found in 837P (synthesized empty line)")
        service_lines = [{
            "cpt": "99213", "charge": 0.0, "unit_count": 1,
            "diagnosis_seq": [1],
        }]

    # ---- Build Claim ----
    patient_ref = f"x12-837-patient-{fhir._new_id()}"
    if subscriber_id:
        # Use a deterministic id derived from the member ID for cross-bundle
        # joins
        patient_ref = f"member-{re.sub(r'[^A-Za-z0-9._-]', '-', subscriber_id)}"
    provider_ref = (
        f"Practitioner/{provider_npi}" if provider_npi else f"Practitioner/{provider_name or 'unknown'}"
    )
    insurer_ref = (
        f"Organization/{payer_id}" if payer_id else f"Organization/{payer_name or 'unknown'}"
    )

    claim = fhir.build_claim(
        patient_ref=patient_ref,
        provider_ref=provider_ref,
        total_amount=claim_total,
        diagnosis_codes=diag_codes,
        service_lines=service_lines,
        identifier_value=clm_id or bht_id or None,
        insurer_ref=insurer_ref if payer_id or payer_name else None,
    )

    notes.append(f"837P: provider={provider_name!r} NPI={provider_npi!r} payer={payer_name!r}")

    return TransformResult(
        result=claim,
        warnings=warnings,
        notes=notes,
        source_format="x12.837P",
        target_format="fhir.Claim",
    )


def _build_service_line(
    lx: Optional[int],
    sv1: list[str],
    service_date: Optional[str],
    warnings: list[str],
) -> dict:
    """Translate one SV1 segment + optional DTP*472 → Claim.service_line dict.

    SV1 layout per 5010X222A1:
      SV1-1: composite ``HC:CPT[:modifier...]`` (or other code qualifiers)
      SV1-2: monetary amount (charge for the line)
      SV1-3: unit basis ("UN" = units, "MJ" = minutes)
      SV1-4: unit count
      SV1-5..: place of service, etc.
    """
    cpt = "UNKNOWN"
    if len(sv1) > 1 and sv1[1]:
        comp = sv1[1].split(":")
        # Expect "HC:CPT" — accept anything after the qualifier
        if len(comp) >= 2:
            cpt = comp[1]
        elif len(comp) == 1:
            cpt = comp[0]

    charge = 0.0
    if len(sv1) > 2 and sv1[2]:
        try:
            charge = _money(sv1[2])
        except ValueError:
            warnings.append(f"SV1-2: invalid charge {sv1[2]!r} → 0.00")

    unit_count = 1
    if len(sv1) > 4 and sv1[4]:
        try:
            unit_count = max(1, int(float(sv1[4])))
        except (TypeError, ValueError):
            warnings.append(f"SV1-4: invalid unit count {sv1[4]!r} → 1")

    line: dict[str, Any] = {
        "cpt": cpt,
        "charge": charge,
        "unit_count": unit_count,
        "diagnosis_seq": [1],
    }
    if service_date:
        line["service_date"] = _hl7_date_to_fhir(service_date) or service_date
    return line


# ---------------------------------------------------------------------------
# X12 835 → FHIR ClaimResponse
# ---------------------------------------------------------------------------

def x12_835_to_claim_response(x12_msg: str, **kwargs: Any) -> TransformResult:
    """Convert X12 835 (remittance) to FHIR ClaimResponse + (notional)
    PaymentReconciliation.

    Per claim payment loop (CLP), build a ClaimResponse. Multiple CLPs in
    one 835 produce multiple ClaimResponse resources, all aggregated under
    one transaction Bundle whose first entry is a notional
    PaymentReconciliation. The PaymentReconciliation wrapper is what binds
    the ClaimResponses to the TRN (the EFT trace number that joins the
    835 to the underlying ACH).

    Field mapping:
      BPR (financial info) → PaymentReconciliation.paymentAmount
      TRN (trace) → PaymentReconciliation.identifier (TRN-2 = check/EFT no)
      N1*PR (payer) → PaymentReconciliation.paymentIssuer
      N1*PE (payee/provider) → PaymentReconciliation.requestor
      DTM*405 (production date) → PaymentReconciliation.created

      Per claim:
        CLP-1 (patient control number) → ClaimResponse.request → Claim ref
        CLP-2 (claim status code, Table 1271) → ClaimResponse.outcome
        CLP-3 (charge) → ClaimResponse.adjudication[submitted]
        CLP-4 (paid)   → ClaimResponse.adjudication[benefit] + payment.amount
        CLP-5 (patient responsibility)
                       → ClaimResponse.adjudication[copay/coinsurance]
        CLP-7 (payer ICN) → ClaimResponse.identifier
        NM1*QC (claimant) → ClaimResponse.patient
        SVC + CAS rows  → ClaimResponse.item[].adjudication[]
    """
    warnings: list[str] = []
    notes: list[str] = []

    env, body = x12.parse_envelope(x12_msg)
    if env.txn_set != "835":
        raise ValueError(f"expected ST-01=835, got {env.txn_set!r}")

    # Walk segments preserving order so we can group by CLP
    parsed_segs: list[list[str]] = [s.split("*") for s in body]

    # ---- Header-level: BPR / TRN / N1 / DTM ----
    bpr = next((s for s in parsed_segs if s[0] == "BPR"), None)
    trn = next((s for s in parsed_segs if s[0] == "TRN"), None)
    payer_name = ""
    payee_name = ""
    payee_npi = ""
    for s in parsed_segs:
        if s[0] == "N1" and len(s) > 1:
            if s[1] == "PR" and len(s) > 2:
                payer_name = s[2]
            elif s[1] == "PE" and len(s) > 2:
                payee_name = s[2]
                if len(s) > 4 and s[3] == "XX":
                    payee_npi = s[4]

    total_paid = 0.0
    if bpr is not None and len(bpr) > 2:
        try:
            total_paid = _money(bpr[2])
        except ValueError:
            warnings.append(f"BPR-2: invalid total payment {bpr[2]!r} → 0.00")

    trn_value = ""
    if trn is not None and len(trn) > 2:
        trn_value = trn[2]

    # ---- Walk the CLP loops ----
    # Group: CLP starts a loop, NM1*QC + SVC + CAS belong to it until the
    # next CLP or PLB.
    clp_groups: list[dict] = []
    current: Optional[dict] = None
    for s in parsed_segs:
        seg = s[0]
        if seg == "CLP":
            if current is not None:
                clp_groups.append(current)
            current = {"clp": s, "nm1_qc": None, "svc_blocks": [], "cas": []}
        elif seg == "NM1" and len(s) > 1 and s[1] == "QC" and current is not None:
            current["nm1_qc"] = s
        elif seg == "SVC" and current is not None:
            current["svc_blocks"].append({"svc": s, "cas": []})
        elif seg == "CAS" and current is not None:
            # CAS belongs to the current SVC if there is one, else to the claim
            if current["svc_blocks"]:
                current["svc_blocks"][-1]["cas"].append(s)
            else:
                current["cas"].append(s)
    if current is not None:
        clp_groups.append(current)

    if not clp_groups:
        raise ValueError("835 has no CLP segments — nothing to adjudicate")

    # ---- Build a ClaimResponse per CLP ----
    responses: list[dict] = []
    for grp in clp_groups:
        clp = grp["clp"]
        nm1_qc = grp["nm1_qc"]
        if len(clp) < 5:
            warnings.append("CLP segment has fewer than 5 fields — partial response")
        claim_ref_id = clp[1] if len(clp) > 1 else "unknown-claim"
        try:
            charge = _money(clp[3]) if len(clp) > 3 else 0.0
            paid = _money(clp[4]) if len(clp) > 4 else 0.0
            patient_resp = _money(clp[5]) if len(clp) > 5 else 0.0
        except ValueError as e:
            raise ValueError(f"CLP money parse error: {e}") from e

        clp_status = clp[2] if len(clp) > 2 else "0"
        # Status codes per Table 1271:
        #   1 = primary processed as primary (full pay or partial)
        #   2 = secondary
        #   3 = tertiary
        #   4 = denied
        #   19/20 = PIP / capitated
        #   22 = reversal
        outcome = "complete"
        if clp_status == "4":
            outcome = "error"
        elif clp_status == "22":
            outcome = "queued"

        payer_icn = clp[7] if len(clp) > 7 else ""

        # Patient ref from NM1*QC (claimant)
        if nm1_qc is not None and len(nm1_qc) > 9:
            member_id = nm1_qc[9] if nm1_qc[8] == "MI" else ""
            patient_ref = (
                f"member-{re.sub(r'[^A-Za-z0-9._-]', '-', member_id)}"
                if member_id else f"x12-835-patient-{fhir._new_id()}"
            )
        else:
            patient_ref = f"x12-835-patient-{fhir._new_id()}"

        # Adjudication entries — header-level CAS rows + SVC adjudications
        adjudications: list[dict] = []
        # Line 1 = the (single) header item
        header_adj = [
            {"category": "submitted", "amount": charge},
            {"category": "benefit",   "amount": paid},
        ]
        if patient_resp > 0:
            header_adj.append({"category": "copay", "amount": patient_resp})
        # CAS rows: ``CAS*group_code*reason*amount[*qty][...]``
        for cas in grp["cas"]:
            if len(cas) >= 4:
                cas_amount = 0.0
                try:
                    cas_amount = _money(cas[3])
                except ValueError:
                    warnings.append(f"CAS amount {cas[3]!r} invalid → skipped")
                    continue
                cat = "deductible" if cas[1] == "PR" else "coinsurance"
                # GROUP CO = contractual, CR = correction, OA = other
                if cas[1] == "CO":
                    cat = "noncovered"
                header_adj.append({"category": cat, "amount": cas_amount})

        adjudications.append({"sequence": 1, "adjudication": header_adj})

        # SVC blocks → additional item entries
        for i, sv in enumerate(grp["svc_blocks"], start=2):
            svc = sv["svc"]
            if len(svc) < 4:
                continue
            try:
                svc_charge = _money(svc[2])
                svc_paid = _money(svc[3])
            except ValueError:
                warnings.append(f"SVC money parse error in line {i} (skipped)")
                continue
            svc_adj = [
                {"category": "submitted", "amount": svc_charge},
                {"category": "benefit",   "amount": svc_paid},
            ]
            for cas in sv["cas"]:
                if len(cas) >= 4:
                    try:
                        cas_amount = _money(cas[3])
                    except ValueError:
                        continue
                    cat = "deductible" if cas[1] == "PR" else "coinsurance"
                    if cas[1] == "CO":
                        cat = "noncovered"
                    svc_adj.append({"category": cat, "amount": cas_amount})
            adjudications.append({"sequence": i, "adjudication": svc_adj})

        cr = fhir.build_claim_response(
            claim_ref=f"Claim/{claim_ref_id}",
            patient_ref=patient_ref,
            insurer_ref=f"Organization/{payer_name or 'unknown'}",
            total_paid=paid,
            outcome=outcome,
            adjudications=adjudications,
            identifier_value=payer_icn or claim_ref_id,
        )
        responses.append(cr)

    # ---- PaymentReconciliation wrapper (notional dict — no fhir builder) ----
    pr_id = f"x12-835-pr-{fhir._new_id()}"
    today_iso = datetime.now().date().isoformat()
    created_iso = datetime.now().isoformat()
    if "+" not in created_iso and not created_iso.endswith("Z"):
        created_iso += "Z"
    payment_reconciliation = {
        "resourceType": "PaymentReconciliation",
        "id": pr_id,
        # status, created, paymentDate are all element_required per the
        # R4B PaymentReconciliation profile (fhir.resources enforces).
        "status": "active",
        "created": created_iso,
        "paymentDate": today_iso,
        "paymentAmount": {"value": total_paid, "currency": "USD"},
        "identifier": [{
            "system": "urn:oid:2.16.840.1.113883.4.642.40.5.4",
            "value": trn_value or "TRN-MISSING",
        }],
        "paymentIssuer": {"display": payer_name or "unknown"},
        "requestor": {"display": payee_name or "unknown"},
        "detail": [
            {
                # PaymentReconciliationDetail.type is a required CodeableConcept;
                # FHIR's payment-type value set covers payment | adjustment |
                # advance | etc. We pick "payment" by default since 835 CLPs
                # represent payments against claims.
                "type": fhir.codeable_concept(
                    "http://terminology.hl7.org/CodeSystem/payment-type",
                    "payment", "Payment",
                ),
                "request": cr["request"],
                "response": {"reference": f"ClaimResponse/{cr['id']}"},
                "amount": cr["payment"]["amount"],
            }
            for cr in responses
        ],
    }

    # ---- Bundle ----
    entries = [
        {
            "fullUrl": f"urn:uuid:{pr_id}",
            "resource": payment_reconciliation,
            "request": {"method": "POST", "url": "PaymentReconciliation"},
        },
    ]
    for cr in responses:
        entries.append({
            "fullUrl": f"urn:uuid:{cr['id']}",
            "resource": cr,
            "request": {"method": "POST", "url": "ClaimResponse"},
        })

    bundle = fhir.build_bundle_transaction(entries)

    # Convenience: surface the FIRST ClaimResponse on result for callers
    # who expect a single resource. Bundle is also accessible via the
    # entries.
    if len(responses) == 1:
        result_payload: Any = responses[0]
    else:
        result_payload = bundle

    notes.append(
        f"835: {len(responses)} ClaimResponse(s), TRN={trn_value!r}, total_paid={total_paid}"
    )

    return TransformResult(
        result=result_payload,
        warnings=warnings,
        notes=notes,
        source_format="x12.835",
        target_format="fhir.ClaimResponse" if len(responses) == 1
                      else "fhir.Bundle[PaymentReconciliation,ClaimResponse]",
    )


# ---------------------------------------------------------------------------
# DICOM Study → FHIR ImagingStudy
# ---------------------------------------------------------------------------

def dicom_study_to_imaging_study(
    dataset_or_meta: Any,
    *,
    wado_endpoint: str = "Endpoint/wado-rs-default",
) -> TransformResult:
    """Convert DICOM Study/Series/Instance to FHIR ImagingStudy.

    Accepts either a pydicom ``Dataset`` (single instance — series and
    instance lists end up with one element each), DICOM Part 10 bytes,
    or a pre-parsed ``DicomStudyMeta``.

    Field mapping (per FHIR R4B ImagingStudy + DICOM PS3.18):
      (0020,000D) StudyInstanceUID → ImagingStudy.identifier[system=urn:dicom:uid]
      (0010,0020) PatientID → ImagingStudy.subject (Patient ref by MRN)
      (0008,0050) AccessionNumber → ImagingStudy.identifier (type ACSN)
      (0008,0020) StudyDate + (0008,0030) StudyTime → ImagingStudy.started
      (0008,0061) ModalitiesInStudy → ImagingStudy.modality
      (0008,0090) ReferringPhysicianName → ImagingStudy.referrer (Practitioner)
      Per-series:
        (0020,000E) SeriesInstanceUID → series.uid
        (0008,0060) Modality → series.modality
        (0020,0011) SeriesNumber → series.number
        (0008,103E) SeriesDescription → series.description
        (0020,1209) NumberOfSeriesRelatedInstances → series.numberOfInstances
      Per-instance:
        (0008,0018) SOPInstanceUID → instance.uid
        (0008,0016) SOPClassUID → instance.sopClass

      Pixel data is intentionally NOT carried in ImagingStudy — the
      ImagingStudy.endpoint reference points at a WADO-RS / WADO-URI
      endpoint where the actual image bytes can be fetched.
    """
    warnings: list[str] = []
    notes: list[str] = []

    if isinstance(dataset_or_meta, dicom.DicomStudyMeta):
        meta = dataset_or_meta
        ds = None
    else:
        try:
            meta = dicom.parse_meta(dataset_or_meta)
        except Exception as e:
            raise ValueError(f"could not parse DICOM input: {e}") from e
        try:
            ds = dicom._coerce_to_dataset(dataset_or_meta)
        except Exception:
            ds = None

    if not meta.study_instance_uid:
        raise ValueError("DICOM input has no StudyInstanceUID — cannot build ImagingStudy")

    # Patient ref by MRN
    if meta.patient_id:
        patient_ref = f"patient-{re.sub(r'[^A-Za-z0-9._-]', '-', meta.patient_id)}"
    else:
        warnings.append("PatientID missing — synthesizing patient ref")
        patient_ref = f"patient-{fhir._new_id()}"

    # started: combine StudyDate + StudyTime
    started_iso: Optional[str] = None
    if meta.study_date:
        date_part = meta.study_date.strip()
        time_part = ""
        if ds is not None:
            time_part = (str(getattr(ds, "StudyTime", "") or "")).strip()
        # date+time → 14-digit string
        combined = date_part + (time_part[:6].ljust(6, "0") if time_part else "")
        started_iso = _hl7_dt_to_fhir(combined) or _hl7_date_to_fhir(date_part)

    modality_code = meta.modality or "OT"

    # Build series list. We have at least one (from DicomStudyMeta).
    series_specs: list[dict] = []
    if meta.series_instance_uids:
        for i, suid in enumerate(meta.series_instance_uids, start=1):
            s_modality = modality_code
            description = None
            number_of_instances: Optional[int] = None
            if ds is not None and i == 1:
                # The Dataset only carries one series's metadata; populate
                # extra fields for that series only.
                s_modality = str(getattr(ds, "Modality", modality_code) or modality_code)
                desc_raw = getattr(ds, "SeriesDescription", None)
                if desc_raw:
                    description = str(desc_raw)
                noi = getattr(ds, "NumberOfSeriesRelatedInstances", None)
                if noi is not None:
                    try:
                        number_of_instances = int(str(noi))
                    except (TypeError, ValueError):
                        warnings.append(
                            "NumberOfSeriesRelatedInstances not parseable as int"
                        )
            instances = [
                {
                    "uid": uid,
                    "sop_class": _instance_sop_class(ds) if ds is not None else "1.2.840.10008.5.1.4.1.1.2",
                    "number": j,
                }
                for j, uid in enumerate(meta.sop_instance_uids, start=1)
            ]
            series_dict: dict[str, Any] = {
                "uid": suid,
                "number": i,
                "modality_code": s_modality,
                "instances": instances,
            }
            if description:
                series_dict["description"] = description
            series_specs.append(series_dict)
            if number_of_instances is not None and number_of_instances != len(instances):
                notes.append(
                    f"Series {suid}: NumberOfSeriesRelatedInstances={number_of_instances} "
                    f"vs SOPInstance count={len(instances)} (kept actual count)"
                )
    else:
        warnings.append("No SeriesInstanceUID(s) — synthesizing single empty series")

    imaging_study = fhir.build_imaging_study(
        patient_ref=patient_ref,
        study_uid=meta.study_instance_uid,
        modality_code=modality_code,
        started=started_iso,
        series=series_specs or None,
        endpoint_ref=wado_endpoint,
        description=meta.study_description,
    )

    # AccessionNumber → identifier with type ACSN
    if meta.accession_number:
        imaging_study = dict(imaging_study)
        identifiers = list(imaging_study.get("identifier", []))
        identifiers.append({
            "system": "urn:oid:2.16.840.1.113883.4.642.40.5.5",
            "value": meta.accession_number,
            "type": fhir.codeable_concept(
                "http://terminology.hl7.org/CodeSystem/v2-0203",
                "ACSN", "Accession ID",
            ),
        })
        imaging_study["identifier"] = identifiers
        notes.append(f"AccessionNumber → identifier with type ACSN ({meta.accession_number})")

    # ReferringPhysicianName → referrer (Practitioner reference)
    if meta.referring_physician:
        physician_id = re.sub(r"[^A-Za-z0-9._-]", "-", meta.referring_physician)
        imaging_study = dict(imaging_study)
        imaging_study["referrer"] = {"reference": f"Practitioner/{physician_id}"}
        notes.append(f"ReferringPhysician → Practitioner/{physician_id}")

    # Pixel data sanity: ImagingStudy NEVER carries pixel data — this is
    # a regression guard.
    if "pixelData" in imaging_study or "pixel_data" in imaging_study:
        # Defensive — should never happen given build_imaging_study's
        # output shape.
        for k in ("pixelData", "pixel_data"):
            imaging_study.pop(k, None)
        warnings.append("Stripped pixel-data key from ImagingStudy result")

    return TransformResult(
        result=imaging_study,
        warnings=warnings,
        notes=notes,
        source_format="dicom.Dataset",
        target_format="fhir.ImagingStudy",
    )


def _instance_sop_class(ds) -> str:
    """Extract the SOPClassUID from a Dataset, with sane fallbacks."""
    if ds is None:
        return "1.2.840.10008.5.1.4.1.1.2"
    sop = getattr(ds, "SOPClassUID", None)
    if sop:
        return str(sop)
    return "1.2.840.10008.5.1.4.1.1.2"


# ---------------------------------------------------------------------------
# Pipeline-level helpers
# ---------------------------------------------------------------------------

def deidentified_transform(
    transformer: Callable[..., TransformResult],
    source: Any,
    *,
    deid_config: deid.DeidConfig,
    audit_log: Optional[deid.AuditLog] = None,
    **transformer_kwargs: Any,
) -> TransformResult:
    """Run ``transformer(source)`` then post-process the result with de-id.

    The post-pass is dispatched on the shape of the result:
      * dict (FHIR resource or Bundle) — recursively walk, replace
        identifiers + names + addresses + telecoms with pseudonyms or
        empty strings; date fields shifted by a per-subject offset
      * pydicom Dataset — applied via ``dicom.deidentify_basic_profile``
      * str — caller's problem (we don't introspect arbitrary strings)

    Returns a fresh ``TransformResult`` with the de-identified payload
    on ``.result`` and a note on ``.notes`` describing what happened.
    """
    pre_result = transformer(source, **transformer_kwargs)

    out = TransformResult(
        result=pre_result.result,
        warnings=list(pre_result.warnings),
        notes=list(pre_result.notes),
        source_format=pre_result.source_format,
        target_format=pre_result.target_format,
    )

    payload = pre_result.result

    if isinstance(payload, dict):
        deid_payload = _deid_fhir_dict(payload, deid_config, audit_log)
        out.result = deid_payload
        out.add_note("de-identified FHIR dict via cross_standards.deidentified_transform")
    elif hasattr(payload, "save_as"):  # pydicom Dataset duck-typing
        # Use the basic profile from dicom module — date_offset_days not
        # parameterized here; if caller wants control, they should call
        # dicom.deidentify_basic_profile directly.
        out.result = dicom.deidentify_basic_profile(payload)
        out.add_note("de-identified DICOM via dicom.deidentify_basic_profile")
    else:
        out.add_warning(
            f"deidentified_transform: payload type {type(payload).__name__} "
            f"not recognized; result not de-identified"
        )

    return out


# Fields that the FHIR de-id walker will scrub or pseudonymize. Keys are
# field-name suffixes (since FHIR puts identifiers everywhere); values are
# the action to take.
_FHIR_PII_KEYS = {
    "name",          # HumanName arrays → blank surname/given
    "telecom",       # Phone/email/etc. → emptied
    "address",       # Postal → city/state/postal stripped
    "birthDate",     # → year shift (or empty)
    "identifier",    # Patient identifiers → pseudonymized
    "deceasedDateTime",
}


def _deid_fhir_dict(
    payload: dict,
    cfg: deid.DeidConfig,
    audit: Optional[deid.AuditLog] = None,
    *,
    record_id_hint: str = "",
) -> dict:
    """Recursively walk a FHIR resource (or Bundle) and scrub PII.

    Pseudonymizes identifier values, blanks names/telecoms/addresses,
    truncates birthDate to year. Always returns a NEW dict — input is
    not mutated.
    """
    if not isinstance(payload, dict):
        return payload

    out: dict[str, Any] = {}
    rt = payload.get("resourceType", "")
    rid = payload.get("id", "")
    record_id = record_id_hint or rid or rt or "unknown"

    for key, val in payload.items():
        if key == "id" and isinstance(val, str) and val:
            # Pseudonymize the resource id so cross-resource references
            # remain consistent within the same release (same salt).
            new_id = deid.hmac_pseudonym(val, cfg.pseudonym_salt, length=16)
            if audit is not None:
                audit.record(record_id, "pseudonymize_id", "id", val, "resource id")
            out[key] = new_id
        elif key == "reference" and isinstance(val, str) and "/" in val:
            # References: pseudonymize the id portion only
            try:
                rt_part, id_part = val.split("/", 1)
                new_id = deid.hmac_pseudonym(id_part, cfg.pseudonym_salt, length=16)
                out[key] = f"{rt_part}/{new_id}"
            except ValueError:
                out[key] = val
        elif key == "name" and isinstance(val, list):
            # HumanName: replace with a single anonymized name
            out[key] = [{"family": "REDACTED", "given": ["REDACTED"]}]
            if audit is not None:
                audit.record(record_id, "redact", "name", str(val), "HumanName")
        elif key == "telecom" and isinstance(val, list):
            # Drop telecom entirely (Safe Harbor §164.514(b)(2)(i)(D)(E))
            if audit is not None:
                audit.record(record_id, "remove", "telecom", str(val), "phone/email/etc.")
            # Skip — don't add to output
        elif key == "address" and isinstance(val, list):
            # Strip line/city/postal; keep state if present (Safe Harbor allows)
            new_addrs = []
            for a in val:
                if isinstance(a, dict):
                    pruned = {k2: v2 for k2, v2 in a.items() if k2 in ("state", "country", "use")}
                    new_addrs.append(pruned)
            out[key] = new_addrs
            if audit is not None:
                audit.record(record_id, "generalize", "address", str(val), "kept state/country only")
        elif key == "birthDate" and isinstance(val, str) and val:
            # Year-only generalization — but Safe Harbor also allows full
            # date for ages <90, so we apply year-only as the safer default.
            try:
                year = deid.date_to_year_only(val)
                out[key] = year
            except ValueError:
                # Unparseable — drop
                out[key] = ""
            if audit is not None:
                audit.record(record_id, "generalize", "birthDate", val, "year only")
        elif key == "identifier" and isinstance(val, list):
            new_ids = []
            for idr in val:
                if isinstance(idr, dict) and idr.get("value"):
                    new_id_value = deid.hmac_pseudonym(
                        idr["value"], cfg.pseudonym_salt, length=16,
                    )
                    new_idr = dict(idr)
                    new_idr["value"] = new_id_value
                    new_ids.append(new_idr)
                else:
                    new_ids.append(idr)
            out[key] = new_ids
            if audit is not None:
                audit.record(record_id, "pseudonymize", "identifier", str(val), "system+value preserved, value hashed")
        elif key in ("effectiveDateTime", "issued", "started", "created") and isinstance(val, str):
            # Date-shift, but keep ISO format. Use the resource id as the
            # subject key so all dates on one resource shift together.
            try:
                offset = deid.per_subject_offset(record_id, cfg.date_offset_seed,
                                                  max_days=cfg.date_offset_max_days)
                out[key] = deid.shift_date(val, offset)
            except ValueError:
                out[key] = val
        elif key == "period" and isinstance(val, dict):
            # Apply same offset to start + end
            new_period = {}
            for pk in ("start", "end"):
                if pk in val and isinstance(val[pk], str):
                    try:
                        offset = deid.per_subject_offset(
                            record_id, cfg.date_offset_seed,
                            max_days=cfg.date_offset_max_days,
                        )
                        new_period[pk] = deid.shift_date(val[pk], offset)
                    except ValueError:
                        new_period[pk] = val[pk]
                elif pk in val:
                    new_period[pk] = val[pk]
            out[key] = new_period
        elif isinstance(val, dict):
            out[key] = _deid_fhir_dict(val, cfg, audit, record_id_hint=record_id)
        elif isinstance(val, list):
            out[key] = [
                _deid_fhir_dict(item, cfg, audit, record_id_hint=record_id)
                if isinstance(item, dict) else item
                for item in val
            ]
        else:
            out[key] = val

    return out


# Pairs of (source → target) format keys for which a forward AND inverse
# transformer both exist in this module. Used by ``round_trip_supported``
# to declare round-trip parity claims for tests + downstream tooling.
#
# ---------------------------------------------------------------------------
# HL7 v2 ORM^O01 → FHIR ServiceRequest
# ---------------------------------------------------------------------------

# ORC-1 → ServiceRequest.status / .intent. Per the v2-to-FHIR IG mapping
# table for orders (ORC-1 has 17 codes; we cover the ones we see in real
# traffic — NW/RP/CA/CM/HD/RL/UA/UC). Anything else falls back to the
# safe defaults (active/order) and surfaces a warning.
_ORC1_TO_FHIR_STATUS = {
    "NW": ("active",    "order"),     # New order
    "RP": ("active",    "order"),     # Replace prior; receiver supersedes by placer-id
    "CA": ("revoked",   "order"),     # Cancel
    "DC": ("revoked",   "order"),     # Discontinue
    "HD": ("on-hold",   "order"),     # Hold
    "RL": ("active",    "order"),     # Release prior hold
    "CM": ("completed", "order"),     # Complete
    "UA": ("active",    "order"),     # Update affecting authorization
    "UC": ("active",    "order"),     # Update without state change
}


def orm_o01_to_service_request(
    hl7_message: Union[str, hl7v2.HL7Message],
    *,
    mrn_system: str = fhir.SYSTEM_LOCAL_MRN,
    placer_system: str = "urn:oid:LOCAL_PLACER_OID",
) -> TransformResult:
    """Convert HL7v2 ORM^O01 (general order) to a FHIR Bundle with
    Patient + ServiceRequest entries.

    Field mapping:
      PID-3 / PID-5 / PID-7 / PID-8 → Patient (same as ADT path)
      ORC-1 (order control)         → ServiceRequest.status + .intent
        (NW → active/order; CA/DC → revoked; HD → on-hold; CM → completed)
      ORC-2 (placer order number)   → ServiceRequest.identifier
        (system = ``placer_system``)
      ORC-3 (filler order number)   → ServiceRequest.identifier (second entry)
      ORC-9 (transaction date/time) → ServiceRequest.occurrenceDateTime
      ORC-12 (ordering provider, XCN) → ServiceRequest.requester (Practitioner)
      OBR-4 (universal service id, CE/CWE: code^display^system)
        → ServiceRequest.code (LOINC by default; fallback to local system)
    """
    parsed, raw = _coerce_hl7(hl7_message)
    warnings: list[str] = []

    # Patient (re-use the same builder ADT uses)
    patient = _patient_from_pid(
        parsed, raw, mrn_system=mrn_system,
        patient_id=parsed.pid.get("patient_id") or "patient-1",
        warnings=warnings,
    )

    # ORC + OBR via raw-segment lookup (HL7Message doesn't surface ORC structurally)
    orc_segs = hl7v2.get_segments(raw, "ORC")
    obr_segs = hl7v2.get_segments(raw, "OBR")
    orc = orc_segs[0] if orc_segs else []
    obr = obr_segs[0] if obr_segs else []

    def _f(seg: list[str], idx: int) -> str:
        return seg[idx] if idx < len(seg) else ""

    placer = _f(orc, 2)
    filler = _f(orc, 3)
    orc1 = (_f(orc, 1) or "NW").upper()
    if orc1 not in _ORC1_TO_FHIR_STATUS:
        warnings.append(f"ORC-1: unknown order control {orc1!r} → defaulted to active/order")
    status, intent = _ORC1_TO_FHIR_STATUS.get(orc1, ("active", "order"))

    obr4 = _f(obr, 4)
    code_parts = obr4.split("^") if obr4 else []
    code = code_parts[0] if len(code_parts) >= 1 and code_parts[0] else "UNKNOWN"
    display = code_parts[1] if len(code_parts) >= 2 and code_parts[1] else code
    code_sys_raw = code_parts[2] if len(code_parts) >= 3 else ""
    if code_sys_raw == "L":
        code_system = "http://terminology.hl7.org/CodeSystem/v2-0396"
    elif code_sys_raw == "LN":
        code_system = fhir.LOINC
    elif code_sys_raw == "SCT":
        code_system = fhir.SNOMED
    elif code_sys_raw and code_sys_raw.startswith("http"):
        code_system = code_sys_raw
    else:
        code_system = fhir.LOINC
        if code_sys_raw:
            warnings.append(
                f"OBR-4: unknown code system {code_sys_raw!r} → defaulted to LOINC"
            )

    orc_9 = _f(orc, 9)
    occ = _hl7_dt_to_fhir(orc_9) if orc_9 else None

    requester_ref: Optional[str] = None
    orc_12 = _f(orc, 12)
    if orc_12:
        npi = orc_12.split("^")[0].strip()
        if npi:
            requester_ref = f"Practitioner/{npi}"

    sr_kwargs: dict[str, Any] = {
        "patient_ref": patient["id"],
        "code_system": code_system,
        "code": code,
        "code_display": display,
        "status": status,
        "intent": intent,
    }
    if placer:
        sr_kwargs["identifier_value"] = placer
        sr_kwargs["identifier_system"] = placer_system
    if filler:
        warnings.append(
            f"OBR-3 filler order number {filler!r} not emitted (builder takes one identifier); "
            f"add manually if needed for filler-side reconciliation"
        )
    if requester_ref:
        sr_kwargs["requester_ref"] = requester_ref
    if occ:
        sr_kwargs["occurrence_datetime"] = occ
    service_request = fhir.build_service_request(**sr_kwargs)

    bundle = fhir.build_bundle_transaction([patient, service_request])
    return TransformResult(
        result=bundle,
        warnings=warnings,
        source_format="hl7v2.ORM_O01",
        target_format="fhir.Bundle[Patient,ServiceRequest]",
    )


# ---------------------------------------------------------------------------
# X12 271 → FHIR CoverageEligibilityResponse
# ---------------------------------------------------------------------------

# X12 271 EB-01 (Eligibility/Benefit Information code) — partial mapping.
# Full code list is large; we cover the values that drive the
# FHIR.outcome decision. The full code list from X12 005010X279A1 has
# ~50 values — most carry benefit detail rather than outcome.
_X12_271_EB01_TO_OUTCOME = {
    "1": "complete",   # Active Coverage
    "6": "complete",   # Inactive
    "V": "complete",   # Cannot Process — but we have a definitive answer
}


def x12_271_to_coverage_eligibility_response(
    x12_msg: str,
    *,
    insurer_org_id: str = "PAYER-DEFAULT",
    request_ref: Optional[str] = None,
) -> TransformResult:
    """Convert X12 271 (eligibility response) to FHIR
    CoverageEligibilityResponse.

    Segment mapping:
      ISA-13 (interchange control number) → response.id (fallback)
      NM1*IL (subscriber)                 → response.patient (Reference)
      NM1*PR (payer)                      → response.insurer (Reference)
      EB-01 (eligibility code, first occurrence)
                                          → response.outcome
      EB segments (full set)              → response.disposition (text summary)

    The 271 carries rich benefit detail per service-type; we surface a
    minimal FHIR shape (outcome + disposition) so downstream consumers
    can render eligibility status without needing to model every EB
    permutation. Callers needing per-service-type benefit detail can
    extend the ``insurance`` parameter when calling
    ``fhir.build_coverage_eligibility_response`` directly.
    """
    warnings: list[str] = []

    # Verify ST-01 = 271 (envelope check)
    env, _ = x12.parse_envelope(x12_msg)
    if env.txn_set != "271":
        raise ValueError(f"expected ST-01=271, got {env.txn_set!r}")

    # Subscriber NM1*IL (loop 2100C) — element 09 is the member identifier
    nm1_il = [
        seg for seg in x12.get_segments(x12_msg, "NM1")
        if len(seg) >= 2 and seg[1] == "IL"
    ]
    if nm1_il:
        member_id = nm1_il[0][9] if len(nm1_il[0]) >= 10 else ""
    else:
        member_id = ""
        warnings.append("271: missing NM1*IL (subscriber) loop")

    patient_ref = f"Patient/{member_id}" if member_id else "Patient/UNKNOWN"

    # EB segments — first non-empty EB-01 drives outcome
    eb_segs = x12.get_segments(x12_msg, "EB")
    if not eb_segs:
        warnings.append("271: no EB segments — outcome defaulted to 'error'")
        outcome = "error"
        disposition = "No EB segments in 271 response"
    else:
        first_eb01 = eb_segs[0][1] if len(eb_segs[0]) >= 2 else ""
        outcome = _X12_271_EB01_TO_OUTCOME.get(first_eb01, "complete")
        active = sum(1 for s in eb_segs if len(s) >= 2 and s[1] == "1")
        inactive = sum(1 for s in eb_segs if len(s) >= 2 and s[1] == "6")
        disposition = (
            f"X12 271 carries {len(eb_segs)} EB segment(s): "
            f"{active} active coverage, {inactive} inactive. "
            f"Subscriber: {member_id or '(unknown)'}."
        )

    # Use the parsed envelope's ICN for the FHIR response id
    response_id = f"resp-{env.icn}" if env.icn else None

    response = fhir.build_coverage_eligibility_response(
        patient_ref=patient_ref,
        insurer_ref=insurer_org_id,
        outcome=outcome,
        disposition=disposition,
        request_ref=request_ref,
        response_id=response_id,
    )
    return TransformResult(
        result=response,  # single-resource result; caller can wrap if desired
        warnings=warnings,
        source_format="x12.271",
        target_format="fhir.CoverageEligibilityResponse",
    )


# ---------------------------------------------------------------------------
# Round-trip parity registry
# ---------------------------------------------------------------------------
#
# As of v1, NO inverse transformers are implemented (FHIR-to-HL7v2 etc.).
# This table is the single source of truth so adding an inverse later is
# a one-line change.
_ROUND_TRIP_PAIRS: set[tuple[str, str]] = set()
"""Empty in v1. Populated as inverse transformers are added. Example:
    {("hl7v2.ADT_A01", "fhir.Bundle[Patient,Encounter]"),
     ("fhir.Bundle[Patient,Encounter]", "hl7v2.ADT_A01")}
"""


def round_trip_supported(source_format: str, target_format: str) -> bool:
    """Returns True iff (source → target) AND (target → source) both exist.

    Round-trip parity is a strong claim — it asserts that a message can
    survive a forward + reverse transformation without losing fields the
    transformer chose to map. We are conservative: a pair is True only
    when an explicit inverse transformer is registered.
    """
    return (
        (source_format, target_format) in _ROUND_TRIP_PAIRS
        and (target_format, source_format) in _ROUND_TRIP_PAIRS
    )
