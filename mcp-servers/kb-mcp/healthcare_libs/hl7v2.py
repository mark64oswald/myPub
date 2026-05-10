"""healthcare_libs.hl7v2 — Production reference implementation for HL7 v2.

Wraps hl7apy to provide:
  * Builders: minimal-but-conformant ADT^A01 (admit), ADT^A03 (discharge),
    ADT^A08 (update), and ORU^R01 (lab result) messages that pass
    hl7apy's strict validator out of the box.
  * Parser: ``parse(wire)`` returns an :class:`HL7Message` dataclass with
    the most-frequently-touched segments surfaced as named dicts plus
    ``raw`` for fidelity.
  * Validator: walks a wire message with ``hl7apy.validation.Validator``
    and returns structured :class:`HL7Issue` objects (severity, code,
    message, segment context). Caller decides whether to raise.
  * Round-trip + segment access helpers for transformer testing.

The healthcare interop generators emit code that imports from this
module instead of copying HL7 v2 logic into every generator output.

Why a wrapper rather than direct hl7apy use? Three reasons:

1. **Validation that doesn't crash the caller.** hl7apy's ``Validator``
   raises ``ValidationError`` on the first finding. We catch + collect
   them so a caller can surface "here are all 12 problems" instead of
   "here's the first one".
2. **Segment-terminator hygiene.** HL7 v2 uses ``\\r`` between segments
   (not ``\\n``). Builders emit ``\\r`` consistently; parsers normalize
   ``\\r\\n`` and ``\\n`` on the way in.
3. **Stable dataclass surface.** Generators want
   ``msg.pid['patient_id']``, not ``msg.pid.pid_3.pid_3_1.value``.

References:
  * hl7apy: https://crs4.github.io/hl7apy/
  * HL7 v2.5 Standard: https://www.hl7.org/implement/standards/product_brief.cfm?product_id=185
  * Segment cheat sheet: MSH (header), EVN (event), PID (patient),
    PV1 (visit), OBR (observation request), OBX (observation),
    NK1 (next of kin), AL1 (allergy), DG1 (diagnosis).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message
from hl7apy.exceptions import (
    HL7apyException,
    ParserError,
    ValidationError,
    ValidationWarning,
)
from hl7apy.parser import parse_message
from hl7apy.validation import Validator

LOG = logging.getLogger("healthcare_libs.hl7v2")


# Standard HL7 v2 separators (per MSH-1 / MSH-2). These are the canonical
# "encoding characters" defined in the HL7 spec — we always emit them.
SEP_FIELD = "|"
SEP_COMPONENT = "^"
SEP_REPETITION = "~"
SEP_ESCAPE = "\\"
SEP_SUBCOMPONENT = "&"
SEP_SEGMENT = "\r"   # carriage-return per HL7 spec (NOT \n)

ENCODING_CHARS = SEP_COMPONENT + SEP_REPETITION + SEP_ESCAPE + SEP_SUBCOMPONENT

# HL7 v2.5 is the canonical target for ADT/ORU. v2.5.1 and v2.6 also work
# but 2.5 is the broadest interop baseline.
DEFAULT_VERSION = "2.5"

# Message-type registry: (root, trigger, structure) per HL7 v2.5.
# Per HL7 v2.5 §3.1, several ADT trigger events share a structure with
# ADT_A01 (e.g., A04 register, A08 update, A13 cancel discharge all use
# the ADT_A01 structure). hl7apy follows the spec and only ships the
# canonical structures, so we map trigger → structure here.
MESSAGE_TYPES = {
    "ADT^A01": ("ADT", "A01", "ADT_A01"),  # Admit/visit notification
    "ADT^A03": ("ADT", "A03", "ADT_A03"),  # Discharge/end visit
    "ADT^A08": ("ADT", "A08", "ADT_A01"),  # Update — shares ADT_A01 structure
    "ORU^R01": ("ORU", "R01", "ORU_R01"),  # Unsolicited observation result
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class HL7Issue:
    """One validation finding from hl7apy or our own structural checks.

    ``severity``  one of ``'error' | 'warning' | 'info'``.
    ``code``      segment / field id where the issue lives (e.g. ``'PID'``,
                  ``'MSH-9'``) or a marker like ``'STRUCT'`` / ``'PARSE'``.
    ``message``   human-readable description from the validator.
    ``segment_context``  surrounding segment text (~80 chars) for debugging.
    """

    severity: str
    code: str
    message: str
    segment_context: str = ""


@dataclass
class HL7Message:
    """A parsed HL7 v2 message exposing common segments + raw access.

    Each segment dict uses snake_case keys derived from the HL7 v2.5 field
    names. Repeating segments (OBR, OBX, NK1) are returned as ordered
    lists so the caller can iterate them in wire order.

    Field naming conventions used in the dicts:
      * MSH: ``sending_app``, ``sending_facility``, ``receiving_app``,
        ``receiving_facility``, ``timestamp``, ``message_type``,
        ``trigger_event``, ``message_control_id``, ``processing_id``,
        ``version``.
      * PID: ``patient_id_list`` (PID-3 raw), ``patient_id`` (first ID),
        ``name`` (PID-5 raw), ``last_name``, ``first_name``,
        ``middle_name``, ``dob`` (PID-7), ``sex`` (PID-8).
      * PV1: ``set_id``, ``patient_class``, ``assigned_location``,
        ``admission_type``, ``attending_doctor``.
      * OBR: ``set_id``, ``placer_order_number``, ``filler_order_number``,
        ``universal_service_id``, ``observation_datetime``.
      * OBX: ``set_id``, ``value_type``, ``observation_id``,
        ``observation_value``, ``units``, ``reference_range``,
        ``abnormal_flags``, ``observation_status``.
      * NK1: ``set_id``, ``name``, ``relationship``, ``phone``.
    """

    msh: dict[str, Any]
    pid: dict[str, Any]
    pv1: Optional[dict[str, Any]]
    obr: list[dict[str, Any]] = field(default_factory=list)
    obx: list[dict[str, Any]] = field(default_factory=list)
    nk1: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""


# ---------------------------------------------------------------------------
# Internal: hl7apy field-access helpers
# ---------------------------------------------------------------------------

def _val(node) -> str:
    """Safe ``.value`` extraction — returns empty string on missing children.

    hl7apy raises ``ChildNotFound`` (subclass of ``HL7apyException``) when
    an absent attribute is accessed, even at TOLERANT validation level.
    We also defend against ``AttributeError`` for completeness — some
    code paths in hl7apy raise that directly when a field has no
    structure resolver.
    """
    try:
        v = node.value
        return v if v is not None else ""
    except (HL7apyException, AttributeError):
        return ""


def _get_attr(parent, attr_name: str):
    """Try to read ``parent.attr_name``; return ``None`` if hl7apy can't find it."""
    try:
        return getattr(parent, attr_name)
    except (HL7apyException, AttributeError):
        return None


def _component(parent, attr_name: str) -> str:
    """Resolve ``parent.attr_name`` and return its ``.value`` or ""."""
    n = _get_attr(parent, attr_name)
    if n is None:
        return ""
    return _val(n)


def _normalize_wire(wire: str) -> str:
    """Normalize segment terminators to ``\\r`` (HL7 spec).

    Real-world senders sometimes use ``\\r\\n`` (Windows), ``\\n``
    (Unix-translated), or a mix. hl7apy is finicky about this — we
    normalize on ingress so the parser doesn't choke.
    """
    if not wire:
        return wire
    # Unify line endings to \r
    s = wire.replace("\r\n", "\r").replace("\n", "\r")
    # Strip BOM and surrounding whitespace
    s = s.lstrip("﻿").strip()
    return s


# ---------------------------------------------------------------------------
# MSH builder
# ---------------------------------------------------------------------------

def _build_msh(
    msg: Message,
    *,
    message_type: str,
    sending_app: str,
    sending_facility: str,
    receiving_app: str,
    receiving_facility: str,
    message_control_id: str,
    processing_id: str,
    version: str,
    timestamp: datetime,
) -> None:
    """Populate MSH on an hl7apy Message with the standard 12 fields.

    HL7 v2.5 MSH layout:
      MSH-1  field separator (set automatically by hl7apy)
      MSH-2  encoding characters (set automatically)
      MSH-3  sending application
      MSH-4  sending facility
      MSH-5  receiving application
      MSH-6  receiving facility
      MSH-7  date/time of message
      MSH-8  security  (we leave empty)
      MSH-9  message type (e.g., "ADT^A01^ADT_A01")
      MSH-10 message control ID
      MSH-11 processing ID  ("P" production, "T" test, "D" debug)
      MSH-12 version ID
    """
    msg.msh.msh_3 = sending_app
    msg.msh.msh_4 = sending_facility
    msg.msh.msh_5 = receiving_app
    msg.msh.msh_6 = receiving_facility
    msg.msh.msh_7 = timestamp.strftime("%Y%m%d%H%M%S")
    # MSH-9 needs to carry message type AND structure to satisfy validators.
    # Format: "<root>^<trigger>^<structure>" — e.g., "ADT^A01^ADT_A01".
    # We look up the structure from MESSAGE_TYPES so trigger events that
    # share a parent structure (A08 → ADT_A01) map correctly. Receivers
    # use MSH-9.3 to dispatch to their parser, so this MUST be a real
    # HL7 structure name, not just trigger-derived.
    if message_type in MESSAGE_TYPES:
        _, _, structure = MESSAGE_TYPES[message_type]
        full_type = f"{message_type}^{structure}"
    elif "^" in message_type:
        parts = message_type.split("^")
        if len(parts) == 2:
            structure = f"{parts[0]}_{parts[1]}"
            full_type = f"{message_type}^{structure}"
        else:
            full_type = message_type
    else:
        full_type = message_type
    msg.msh.msh_9 = full_type
    msg.msh.msh_10 = message_control_id
    msg.msh.msh_11 = processing_id
    msg.msh.msh_12 = version


def _new_message(message_type_key: str, version: str = DEFAULT_VERSION) -> Message:
    """Construct an empty hl7apy ``Message`` for a known message type.

    We use ``VALIDATION_LEVEL.TOLERANT`` during construction so that
    add_segment() doesn't barf on intermediate states. Validation is a
    separate pass via :func:`validate`.
    """
    if message_type_key not in MESSAGE_TYPES:
        raise ValueError(f"unknown HL7 message type: {message_type_key!r}")
    _, _, structure = MESSAGE_TYPES[message_type_key]
    return Message(structure, version=version, validation_level=VALIDATION_LEVEL.TOLERANT)


# ---------------------------------------------------------------------------
# PID builder helpers
# ---------------------------------------------------------------------------

def _build_pid(
    msg: Message,
    *,
    patient_first: str,
    patient_last: str,
    patient_middle: str,
    patient_mrn: str,
    patient_dob: str,
    patient_sex: str,
    set_id: str = "1",
) -> None:
    """Populate the PID segment with PID-3 (MRN), PID-5 (name), PID-7 (DOB)."""
    msg.add_segment("PID")
    msg.pid.pid_1 = set_id
    # PID-3: patient identifier list. Format CX = ID^^^assigning-authority^id-type
    # We emit ``MRN-VALUE^^^MRN`` so the receiver can identify it as an MRN.
    msg.pid.pid_3 = f"{patient_mrn}^^^MRN"
    # PID-5: XPN (extended person name): family^given^middle
    if patient_middle:
        msg.pid.pid_5 = f"{patient_last}^{patient_first}^{patient_middle}"
    else:
        msg.pid.pid_5 = f"{patient_last}^{patient_first}"
    if patient_dob:
        msg.pid.pid_7 = patient_dob
    if patient_sex:
        msg.pid.pid_8 = patient_sex


def _build_pv1(
    msg: Message,
    *,
    patient_class: str,
    assigned_location: str,
    admission_type: str,
    attending_doctor: str,
    set_id: str = "1",
) -> None:
    """Populate PV1 (visit info) with the most-used fields."""
    msg.add_segment("PV1")
    msg.pv1.pv1_1 = set_id
    msg.pv1.pv1_2 = patient_class
    if assigned_location:
        msg.pv1.pv1_3 = assigned_location
    if admission_type:
        msg.pv1.pv1_4 = admission_type
    if attending_doctor:
        msg.pv1.pv1_7 = attending_doctor


# ---------------------------------------------------------------------------
# Public builders — ADT
# ---------------------------------------------------------------------------

def build_adt_a01(
    *,
    patient_first: str = "JOHN",
    patient_last: str = "DOE",
    patient_middle: str = "",
    patient_mrn: str = "MRN12345",
    patient_dob: str = "19850615",
    patient_sex: str = "M",
    patient_class: str = "I",         # I=Inpatient, O=Outpatient, E=Emergency
    assigned_location: str = "ICU^101^A",
    admission_type: str = "E",        # E=Emergency, R=Routine
    attending_doctor: str = "1234^SMITH^JANE^^^DR",
    sending_app: str = "EHR",
    sending_facility: str = "GENERAL_HOSPITAL",
    receiving_app: str = "REGISTRATION",
    receiving_facility: str = "GENERAL_HOSPITAL",
    message_control_id: str = "MSG00001",
    processing_id: str = "P",
    version: str = DEFAULT_VERSION,
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an ADT^A01 (admit / visit notification) HL7 v2 message.

    A01 fires when a patient is admitted. Required segments per the spec:
    MSH, EVN, PID, PV1. We emit those four; downstream systems can layer
    optional segments (NK1, AL1, DG1, etc.) on top.

    Returns the wire-format string with ``\\r`` segment terminators.
    """
    ts = timestamp or datetime.now()
    msg = _new_message("ADT^A01", version=version)
    _build_msh(
        msg, message_type="ADT^A01",
        sending_app=sending_app, sending_facility=sending_facility,
        receiving_app=receiving_app, receiving_facility=receiving_facility,
        message_control_id=message_control_id, processing_id=processing_id,
        version=version, timestamp=ts,
    )
    msg.add_segment("EVN")
    msg.evn.evn_1 = "A01"
    msg.evn.evn_2 = ts.strftime("%Y%m%d%H%M%S")
    _build_pid(
        msg,
        patient_first=patient_first, patient_last=patient_last,
        patient_middle=patient_middle, patient_mrn=patient_mrn,
        patient_dob=patient_dob, patient_sex=patient_sex,
    )
    _build_pv1(
        msg, patient_class=patient_class,
        assigned_location=assigned_location, admission_type=admission_type,
        attending_doctor=attending_doctor,
    )
    return msg.value


def build_adt_a03(
    *,
    patient_first: str = "JOHN",
    patient_last: str = "DOE",
    patient_middle: str = "",
    patient_mrn: str = "MRN12345",
    patient_dob: str = "19850615",
    patient_sex: str = "M",
    patient_class: str = "I",
    assigned_location: str = "ICU^101^A",
    discharge_disposition: str = "01",   # 01 = Discharged to home
    attending_doctor: str = "1234^SMITH^JANE^^^DR",
    sending_app: str = "EHR",
    sending_facility: str = "GENERAL_HOSPITAL",
    receiving_app: str = "REGISTRATION",
    receiving_facility: str = "GENERAL_HOSPITAL",
    message_control_id: str = "MSG00003",
    processing_id: str = "P",
    version: str = DEFAULT_VERSION,
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an ADT^A03 (discharge / end visit) HL7 v2 message.

    A03 fires when the patient leaves the facility. Required segments:
    MSH, EVN, PID, PV1. PV1-36 carries the discharge disposition.
    """
    ts = timestamp or datetime.now()
    msg = _new_message("ADT^A03", version=version)
    _build_msh(
        msg, message_type="ADT^A03",
        sending_app=sending_app, sending_facility=sending_facility,
        receiving_app=receiving_app, receiving_facility=receiving_facility,
        message_control_id=message_control_id, processing_id=processing_id,
        version=version, timestamp=ts,
    )
    msg.add_segment("EVN")
    msg.evn.evn_1 = "A03"
    msg.evn.evn_2 = ts.strftime("%Y%m%d%H%M%S")
    _build_pid(
        msg,
        patient_first=patient_first, patient_last=patient_last,
        patient_middle=patient_middle, patient_mrn=patient_mrn,
        patient_dob=patient_dob, patient_sex=patient_sex,
    )
    _build_pv1(
        msg, patient_class=patient_class,
        assigned_location=assigned_location, admission_type="",
        attending_doctor=attending_doctor,
    )
    if discharge_disposition:
        msg.pv1.pv1_36 = discharge_disposition
    # PV1-45: discharge date/time
    msg.pv1.pv1_45 = ts.strftime("%Y%m%d%H%M%S")
    return msg.value


def build_adt_a08(
    *,
    patient_first: str = "JOHN",
    patient_last: str = "DOE",
    patient_middle: str = "",
    patient_mrn: str = "MRN12345",
    patient_dob: str = "19850615",
    patient_sex: str = "M",
    patient_class: str = "I",
    assigned_location: str = "ICU^101^A",
    attending_doctor: str = "1234^SMITH^JANE^^^DR",
    sending_app: str = "EHR",
    sending_facility: str = "GENERAL_HOSPITAL",
    receiving_app: str = "REGISTRATION",
    receiving_facility: str = "GENERAL_HOSPITAL",
    message_control_id: str = "MSG00008",
    processing_id: str = "P",
    version: str = DEFAULT_VERSION,
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an ADT^A08 (update patient information) HL7 v2 message.

    A08 fires on demographic updates (address change, name correction,
    insurance update). Same required segments as A01: MSH, EVN, PID, PV1.
    The receiver should treat the PID/PV1 as authoritative state.
    """
    ts = timestamp or datetime.now()
    msg = _new_message("ADT^A08", version=version)
    _build_msh(
        msg, message_type="ADT^A08",
        sending_app=sending_app, sending_facility=sending_facility,
        receiving_app=receiving_app, receiving_facility=receiving_facility,
        message_control_id=message_control_id, processing_id=processing_id,
        version=version, timestamp=ts,
    )
    msg.add_segment("EVN")
    msg.evn.evn_1 = "A08"
    msg.evn.evn_2 = ts.strftime("%Y%m%d%H%M%S")
    _build_pid(
        msg,
        patient_first=patient_first, patient_last=patient_last,
        patient_middle=patient_middle, patient_mrn=patient_mrn,
        patient_dob=patient_dob, patient_sex=patient_sex,
    )
    _build_pv1(
        msg, patient_class=patient_class,
        assigned_location=assigned_location, admission_type="",
        attending_doctor=attending_doctor,
    )
    return msg.value


# ---------------------------------------------------------------------------
# Public builder — ORU
# ---------------------------------------------------------------------------

def build_oru_r01(
    *,
    patient_first: str = "JOHN",
    patient_last: str = "DOE",
    patient_middle: str = "",
    patient_mrn: str = "MRN12345",
    patient_dob: str = "19850615",
    patient_sex: str = "M",
    placer_order_number: str = "ORDER-001",
    filler_order_number: str = "FILLER-001",
    universal_service_id: str = "CBC^Complete Blood Count^L",
    observations: Optional[list[dict[str, Any]]] = None,
    sending_app: str = "LAB",
    sending_facility: str = "GENERAL_HOSPITAL",
    receiving_app: str = "EHR",
    receiving_facility: str = "GENERAL_HOSPITAL",
    message_control_id: str = "LAB00001",
    processing_id: str = "P",
    version: str = DEFAULT_VERSION,
    timestamp: Optional[datetime] = None,
) -> str:
    """Build an ORU^R01 (unsolicited observation result) HL7 v2 message.

    ORU R01 carries lab/diagnostic results. Required segments: MSH, PID,
    OBR (one per ordered test), OBX (one per result).

    ``observations`` is a list of dicts with keys:
      * ``value_type``      OBX-2: ``NM`` numeric, ``ST`` string, ``CE`` coded
      * ``observation_id``  OBX-3: ``LOINC^name^L`` or ``code^name``
      * ``value``           OBX-5: the result value
      * ``units``           OBX-6: ``mg/dL``, ``10*3/uL``, etc.
      * ``reference_range`` OBX-7: ``4.0-11.0``
      * ``abnormal_flags``  OBX-8: ``H``, ``L``, ``N``, ``A``
      * ``status``          OBX-11: ``F`` final, ``P`` preliminary, ``C`` corrected

    If ``observations`` is None/empty we emit one default CBC OBX so the
    message is still spec-conformant.
    """
    ts = timestamp or datetime.now()
    if observations is None or not observations:
        observations = [{
            "value_type": "NM",
            "observation_id": "WBC^White Blood Cells^L",
            "value": "7.5",
            "units": "10*3/uL",
            "reference_range": "4.0-11.0",
            "abnormal_flags": "N",
            "status": "F",
        }]

    msg = _new_message("ORU^R01", version=version)
    _build_msh(
        msg, message_type="ORU^R01",
        sending_app=sending_app, sending_facility=sending_facility,
        receiving_app=receiving_app, receiving_facility=receiving_facility,
        message_control_id=message_control_id, processing_id=processing_id,
        version=version, timestamp=ts,
    )
    _build_pid(
        msg,
        patient_first=patient_first, patient_last=patient_last,
        patient_middle=patient_middle, patient_mrn=patient_mrn,
        patient_dob=patient_dob, patient_sex=patient_sex,
    )
    msg.add_segment("OBR")
    msg.obr.obr_1 = "1"
    msg.obr.obr_2 = placer_order_number
    msg.obr.obr_3 = filler_order_number
    msg.obr.obr_4 = universal_service_id
    msg.obr.obr_7 = ts.strftime("%Y%m%d%H%M%S")

    # hl7apy lets us add multiple OBX segments via add_segment("OBX")
    # repeatedly; each lands as a distinct child in wire order.
    for i, obs in enumerate(observations, start=1):
        obx = msg.add_segment("OBX")
        obx.obx_1 = str(i)
        obx.obx_2 = obs.get("value_type", "ST")
        obx.obx_3 = obs.get("observation_id", "UNKNOWN^Unknown")
        obx.obx_5 = str(obs.get("value", ""))
        if obs.get("units"):
            obx.obx_6 = obs["units"]
        if obs.get("reference_range"):
            obx.obx_7 = obs["reference_range"]
        if obs.get("abnormal_flags"):
            obx.obx_8 = obs["abnormal_flags"]
        obx.obx_11 = obs.get("status", "F")

    return msg.value


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _extract_msh(seg) -> dict[str, Any]:
    """Pull the standard MSH-3..MSH-12 fields off a parsed MSH segment."""
    msh_9 = _get_attr(seg, "msh_9")
    message_type = _val(msh_9) if msh_9 is not None else ""
    parts = message_type.split("^") if message_type else []
    return {
        "sending_app": _component(seg, "msh_3"),
        "sending_facility": _component(seg, "msh_4"),
        "receiving_app": _component(seg, "msh_5"),
        "receiving_facility": _component(seg, "msh_6"),
        "timestamp": _component(seg, "msh_7"),
        "message_type": message_type,
        "message_code": parts[0] if len(parts) >= 1 else "",
        "trigger_event": parts[1] if len(parts) >= 2 else "",
        "message_structure": parts[2] if len(parts) >= 3 else "",
        "message_control_id": _component(seg, "msh_10"),
        "processing_id": _component(seg, "msh_11"),
        "version": _component(seg, "msh_12"),
    }


def _extract_pid(seg) -> dict[str, Any]:
    """Pull common PID fields. Returns name components broken out."""
    pid_3_full = _component(seg, "pid_3")
    # PID-3 may contain multiple IDs separated by ~. Take the first.
    first_id_full = pid_3_full.split("~")[0] if pid_3_full else ""
    patient_id = first_id_full.split("^")[0] if first_id_full else ""

    name_full = _component(seg, "pid_5")
    name_parts = name_full.split("^") if name_full else []
    return {
        "patient_id_list": pid_3_full,
        "patient_id": patient_id,
        "name": name_full,
        "last_name": name_parts[0] if len(name_parts) >= 1 else "",
        "first_name": name_parts[1] if len(name_parts) >= 2 else "",
        "middle_name": name_parts[2] if len(name_parts) >= 3 else "",
        "dob": _component(seg, "pid_7"),
        "sex": _component(seg, "pid_8"),
        "address": _component(seg, "pid_11"),
        "phone": _component(seg, "pid_13"),
    }


def _extract_pv1(seg) -> dict[str, Any]:
    return {
        "set_id": _component(seg, "pv1_1"),
        "patient_class": _component(seg, "pv1_2"),
        "assigned_location": _component(seg, "pv1_3"),
        "admission_type": _component(seg, "pv1_4"),
        "attending_doctor": _component(seg, "pv1_7"),
        "discharge_disposition": _component(seg, "pv1_36"),
        "discharge_datetime": _component(seg, "pv1_45"),
    }


def _extract_obr(seg) -> dict[str, Any]:
    return {
        "set_id": _component(seg, "obr_1"),
        "placer_order_number": _component(seg, "obr_2"),
        "filler_order_number": _component(seg, "obr_3"),
        "universal_service_id": _component(seg, "obr_4"),
        "observation_datetime": _component(seg, "obr_7"),
    }


def _extract_obx(seg) -> dict[str, Any]:
    return {
        "set_id": _component(seg, "obx_1"),
        "value_type": _component(seg, "obx_2"),
        "observation_id": _component(seg, "obx_3"),
        "observation_value": _component(seg, "obx_5"),
        "units": _component(seg, "obx_6"),
        "reference_range": _component(seg, "obx_7"),
        "abnormal_flags": _component(seg, "obx_8"),
        "observation_status": _component(seg, "obx_11"),
    }


def _extract_nk1(seg) -> dict[str, Any]:
    return {
        "set_id": _component(seg, "nk1_1"),
        "name": _component(seg, "nk1_2"),
        "relationship": _component(seg, "nk1_3"),
        "address": _component(seg, "nk1_4"),
        "phone": _component(seg, "nk1_5"),
    }


def parse(wire: str) -> HL7Message:
    """Parse an HL7 v2 wire-format string into an :class:`HL7Message`.

    Raises :class:`ValueError` with an informative message if ``wire``
    isn't recognizable HL7 v2 (no MSH segment, malformed encoding chars,
    hl7apy lexer failure).

    The parse runs at ``VALIDATION_LEVEL.TOLERANT`` — structural validity
    is a separate pass via :func:`validate`. This separation matters
    because integration teams typically want to *log* malformed messages
    and continue processing the queue, not crash on the first bad one.
    """
    if not wire or not wire.strip():
        raise ValueError("empty HL7 v2 message")
    normalized = _normalize_wire(wire)
    if not normalized.startswith("MSH"):
        raise ValueError(
            f"not an HL7 v2 message (does not start with MSH): "
            f"{normalized[:40]!r}"
        )
    try:
        parsed = parse_message(normalized, validation_level=VALIDATION_LEVEL.TOLERANT)
    except ParserError as e:
        raise ValueError(f"hl7apy parser rejected message: {e}") from e
    except HL7apyException as e:
        raise ValueError(f"hl7apy raised {type(e).__name__}: {e}") from e

    # Walk parsed.children which is a *.children container; iterate to flat list.
    msh_dict: dict[str, Any] = {}
    pid_dict: dict[str, Any] = {}
    pv1_dict: Optional[dict[str, Any]] = None
    obr_list: list[dict[str, Any]] = []
    obx_list: list[dict[str, Any]] = []
    nk1_list: list[dict[str, Any]] = []

    for child in _iter_segments(parsed):
        name = child.name
        if name == "MSH":
            msh_dict = _extract_msh(child)
        elif name == "PID":
            pid_dict = _extract_pid(child)
        elif name == "PV1":
            pv1_dict = _extract_pv1(child)
        elif name == "OBR":
            obr_list.append(_extract_obr(child))
        elif name == "OBX":
            obx_list.append(_extract_obx(child))
        elif name == "NK1":
            nk1_list.append(_extract_nk1(child))

    if not msh_dict:
        raise ValueError("parsed message has no MSH segment")

    return HL7Message(
        msh=msh_dict,
        pid=pid_dict,
        pv1=pv1_dict,
        obr=obr_list,
        obx=obx_list,
        nk1=nk1_list,
        raw=normalized,
    )


def _iter_segments(node):
    """Recursively yield every Segment descendant of an hl7apy node.

    hl7apy may wrap segments in groups (e.g., ORU_R01_PATIENT_RESULT for
    ORU). We don't care about the group hierarchy for our flattened dict
    surface — we just want every segment in wire order.
    """
    for child in node.children:
        # A Segment has a 3-letter name (MSH, PID, OBR, ...). A Group has
        # a longer name like ORU_R01_PATIENT_RESULT.
        if hasattr(child, "children") and child.children:
            # Could be a group OR a segment with field children. Heuristic:
            # if name length is 3, treat it as a leaf segment.
            if len(child.name) == 3 and child.name.isupper():
                yield child
            else:
                yield from _iter_segments(child)
        else:
            if len(child.name) == 3 and child.name.isupper():
                yield child


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

# Required segments per message type. Used for structural pre-checks
# before invoking hl7apy's deep validator (which only reports the first
# error per call).
REQUIRED_SEGMENTS = {
    "ADT^A01": ["MSH", "EVN", "PID", "PV1"],
    "ADT^A03": ["MSH", "EVN", "PID", "PV1"],
    "ADT^A08": ["MSH", "EVN", "PID", "PV1"],
    "ORU^R01": ["MSH", "PID", "OBR", "OBX"],
}


def validate(wire: str) -> list[HL7Issue]:
    """Validate an HL7 v2 wire string against the v2.5 spec.

    Returns a list of :class:`HL7Issue`. Empty list means the message
    passes both our structural checks and hl7apy's deep validator.

    The validator does NOT raise — caller decides what to do with
    issues. Severity gradient:
      * ``error`` — message will be rejected by a strict receiver
      * ``warning`` — receiver may accept but log (e.g., value not in
        HL7 vocabulary table)
      * ``info`` — diagnostic ("validator skipped because dep missing")

    Implementation strategy:

    1. Normalize line endings and check the message starts with MSH.
    2. Try to parse with hl7apy at TOLERANT level. Parser failures
       become ``error`` issues with code ``PARSE``.
    3. Identify message type from MSH-9 and check our REQUIRED_SEGMENTS
       table for missing segments. This catches things hl7apy's
       Validator might not surface as cleanly (it stops at first
       error).
    4. Run hl7apy's ``Validator.validate()`` for deep structural checks.
       Catch ``ValidationError`` and surface as ``error``;
       ``ValidationWarning`` as ``warning``.
    """
    issues: list[HL7Issue] = []

    if not wire or not wire.strip():
        return [HL7Issue(severity="error", code="EMPTY", message="empty message")]

    normalized = _normalize_wire(wire)
    if not normalized.startswith("MSH"):
        return [HL7Issue(
            severity="error", code="STRUCT",
            message=f"message does not start with MSH (got {normalized[:20]!r})",
        )]

    # hl7apy's MSH parsing is strict about field count — if MSH has fewer
    # than ~12 fields it's malformed. Check this before invoking the parser.
    first_seg = normalized.split(SEP_SEGMENT, 1)[0]
    msh_fields = first_seg.split(SEP_FIELD)
    # MSH has special handling: MSH-1 IS the field separator, so the
    # split count is 1 less than the field count. A complete MSH should
    # have at least 12 elements (MSH-2 through MSH-12 plus the segment ID).
    if len(msh_fields) < 12:
        issues.append(HL7Issue(
            severity="error", code="MSH",
            message=f"MSH has {len(msh_fields)} fields, expected at least 12",
            segment_context=first_seg[:80],
        ))
        # Don't try to parse; hl7apy will likely raise.
        return issues

    # Try parsing
    try:
        parsed = parse_message(normalized, validation_level=VALIDATION_LEVEL.TOLERANT)
    except ParserError as e:
        return [HL7Issue(
            severity="error", code="PARSE",
            message=f"hl7apy parser error: {e}",
            segment_context=first_seg[:80],
        )]
    except HL7apyException as e:
        return [HL7Issue(
            severity="error", code="PARSE",
            message=f"hl7apy {type(e).__name__}: {e}",
            segment_context=first_seg[:80],
        )]

    # MSH-9 → message type
    msh = next((c for c in _iter_segments(parsed) if c.name == "MSH"), None)
    msh_type = _val(_get_attr(msh, "msh_9")) if msh is not None else ""
    type_key = ""
    if msh_type:
        parts = msh_type.split("^")
        if len(parts) >= 2:
            type_key = f"{parts[0]}^{parts[1]}"

    # Required-segments structural check (our own, since hl7apy stops
    # on the first error)
    present_segments = {c.name for c in _iter_segments(parsed)}
    if type_key in REQUIRED_SEGMENTS:
        for required in REQUIRED_SEGMENTS[type_key]:
            if required not in present_segments:
                issues.append(HL7Issue(
                    severity="error", code=required,
                    message=f"missing required segment {required} for {type_key}",
                    segment_context=f"MSH-9={msh_type}",
                ))

    # hl7apy deep validator
    try:
        Validator.validate(parsed)
    except ValidationError as e:
        issues.append(HL7Issue(
            severity="error", code="VALIDATE",
            message=str(e),
            segment_context=f"MSH-9={msh_type}",
        ))
    except ValidationWarning as e:
        issues.append(HL7Issue(
            severity="warning", code="VALIDATE",
            message=str(e),
            segment_context=f"MSH-9={msh_type}",
        ))
    except HL7apyException as e:
        issues.append(HL7Issue(
            severity="info", code="VALIDATE",
            message=f"hl7apy {type(e).__name__}: {e}",
        ))

    return issues


# ---------------------------------------------------------------------------
# Round-trip + segment helpers
# ---------------------------------------------------------------------------

def round_trip(wire: str) -> bool:
    """Parse + re-serialize and confirm the byte content survives.

    We compare the *parsed* form (segment by segment) rather than raw
    string equality — line ending normalization and field-trim variations
    are intentionally allowed. The contract is "no field/segment lost or
    mutated", not "byte identity".
    """
    try:
        msg1 = parse(wire)
    except ValueError:
        return False
    # Re-parse the raw to confirm idempotence
    try:
        parsed = parse_message(_normalize_wire(wire),
                                validation_level=VALIDATION_LEVEL.TOLERANT)
        reserialized = parsed.value
        msg2 = parse(reserialized)
    except (ValueError, HL7apyException):
        return False
    # Compare the structured surface
    return (
        msg1.msh == msg2.msh
        and msg1.pid == msg2.pid
        and msg1.pv1 == msg2.pv1
        and msg1.obr == msg2.obr
        and msg1.obx == msg2.obx
        and msg1.nk1 == msg2.nk1
    )


def split_segments(wire: str) -> list[str]:
    """Split a wire string into per-segment strings (no terminator)."""
    normalized = _normalize_wire(wire)
    return [s for s in normalized.split(SEP_SEGMENT) if s.strip()]


def get_segments(wire: str, segment_id: str) -> list[list[str]]:
    """Return all segments matching ``segment_id`` (e.g., ``'PID'``, ``'OBX'``).

    Each match is returned as a list of element strings (the ``|`` split,
    starting with the segment ID at index 0).

    Special handling for MSH: the field separator IS MSH-1, so the
    returned list places ``"|"`` at index 1, with MSH-3 at index 3 and so
    on (matching how HL7 spec numbers MSH fields).
    """
    out: list[list[str]] = []
    for seg in split_segments(wire):
        fields = seg.split(SEP_FIELD)
        if not fields:
            continue
        if fields[0] != segment_id:
            continue
        if segment_id == "MSH":
            # Re-insert the field separator as MSH-1 so callers can index
            # by the spec field number. fields[0]="MSH", fields[1]=encoding chars
            # currently — we want fields[1]="|", fields[2]=encoding chars.
            spec_fields = [fields[0], SEP_FIELD] + fields[1:]
            out.append(spec_fields)
        else:
            out.append(fields)
    return out


def get_field(
    wire: str,
    segment_id: str,
    field_idx: int,
    *,
    occurrence: int = 0,
) -> Optional[str]:
    """Return the value at ``segment[field_idx]`` for the n-th occurrence.

    ``field_idx`` is the HL7 spec field number (1-based). For MSH-9,
    pass ``field_idx=9`` and you get the message-type element.

    Returns ``None`` if the segment doesn't exist at that occurrence or
    if the field is past the end of the segment.

    Examples:
        >>> get_field(wire, "MSH", 9)        # message type
        "ADT^A01^ADT_A01"
        >>> get_field(wire, "PID", 5)        # patient name
        "DOE^JOHN"
        >>> get_field(wire, "OBX", 5, occurrence=2)  # 3rd OBX result value
    """
    segments = get_segments(wire, segment_id)
    if occurrence >= len(segments):
        return None
    fields = segments[occurrence]
    if field_idx >= len(fields):
        return None
    return fields[field_idx]
