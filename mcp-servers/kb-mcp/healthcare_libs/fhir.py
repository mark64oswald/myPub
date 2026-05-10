"""healthcare_libs.fhir — Production reference implementation for FHIR R4.

Wraps ``fhir.resources`` (R4B model classes — the most stable R4 set) to
provide:

  * Resource builders for the high-leverage clinical + financial set:
    Patient, Encounter, Observation, DiagnosticReport, Claim,
    ClaimResponse, ImagingStudy. Each returns a JSON-serializable dict
    that has been round-tripped through Pydantic validation, so the
    output is guaranteed structurally clean.
  * Bundle assemblers for ``transaction`` and ``collection`` Bundle
    types — including per-entry ``request`` elements where needed.
  * A structured validator that takes a resource dict and returns a
    ``list[FhirIssue]`` instead of raising. Healthcare data quality
    work is iterative; raising on the first error makes batch
    validation painful.
  * Small helpers for the ubiquitous nested types (CodeableConcept,
    Quantity, Identifier, HumanName, Reference) so callers never
    re-invent them.

The healthcare interop generators (kb-fhir-ig, kb-standards-translator,
kb-deid-bundle) emit code that imports from this module instead of
copying FHIR construction logic into every generator output.

Why R4B
-------
R4B (4.3.0) is the supported R4 maintenance release in
``fhir.resources``. R4 (4.0.1) and R4B share the resource shapes for
everything we use here; R4B adds clarifications and bugfixes. Picking
R4B keeps the code aligned with the actively-maintained R4 line.

Why dicts (not Pydantic objects)
--------------------------------
Generated code, downstream serializers, JSON Bulk-Data exports, and
``ndjson`` writers all want dicts. Pydantic objects force every consumer
to learn ``model_dump`` and the ``by_alias`` quirk (``class_fhir`` ->
``class``). We do that translation once, here, and hand back plain
dicts.

Why a returns-issues validator
------------------------------
FHIR's own ``OperationOutcome`` resource is a list-of-issues type for
exactly this reason: a single resource can have many independent
problems and the caller usually wants to see them all. Pydantic raises
on first error; we collect.

References
----------
* fhir.resources: https://github.com/nazrulworld/fhir.resources
* FHIR R4B specification: https://hl7.org/fhir/R4B/
* FHIR datatypes: https://hl7.org/fhir/R4B/datatypes.html
* US Core implementation guide: https://hl7.org/fhir/us/core/
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional, Union

from pydantic import ValidationError

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.claim import Claim
from fhir.resources.R4B.claimresponse import ClaimResponse
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.encounter import Encounter
from fhir.resources.R4B.imagingstudy import ImagingStudy
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.patient import Patient

LOG = logging.getLogger("healthcare_libs.fhir")


# ---------------------------------------------------------------------------
# Identifier system OIDs / canonical URLs
# ---------------------------------------------------------------------------
#
# These are the well-known ``system`` URIs used to disambiguate
# identifier ``value`` fields. Per FHIR, an identifier is meaningless
# without a system: "MRN-12345" could be a Mayo MRN, a Cleveland Clinic
# MRN, or a member ID; the system tells you which authority issued it.

# US National Provider Identifier — for any provider/practitioner ref
SYSTEM_NPI = "http://hl7.org/fhir/sid/us-npi"
# US Social Security Number — only use where unavoidable (HIPAA scope)
SYSTEM_SSN = "http://hl7.org/fhir/sid/us-ssn"
# Local Medical Record Number — replace LOCAL_MRN_OID with your org's
# OID before deploying. The placeholder here makes the requirement
# explicit at code-search time.
SYSTEM_LOCAL_MRN = "urn:oid:LOCAL_MRN_OID"

# Standard terminology code systems
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD10_CM = "http://hl7.org/fhir/sid/icd-10-cm"
ICD10_PCS = "http://www.cms.gov/Medicare/Coding/ICD10"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CPT = "http://www.ama-assn.org/go/cpt"
NDC = "http://hl7.org/fhir/sid/ndc"
UCUM = "http://unitsofmeasure.org"

# Common FHIR / HL7 code systems
SYSTEM_V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
SYSTEM_CLAIM_TYPE = "http://terminology.hl7.org/CodeSystem/claim-type"
SYSTEM_PROCESS_PRIORITY = "http://terminology.hl7.org/CodeSystem/processpriority"
SYSTEM_DCM = "http://dicom.nema.org/resources/ontology/DCM"
SYSTEM_DICOM_SOP = "urn:ietf:rfc:3986"


# ---------------------------------------------------------------------------
# Issue dataclass
# ---------------------------------------------------------------------------

@dataclass
class FhirIssue:
    """One validation finding from ``validate()``.

    Mirrors FHIR's own ``OperationOutcome.issue`` shape so callers can
    forward these across an API boundary as an OperationOutcome
    resource if they like.
    """

    severity: str        # 'error' | 'warning' | 'information' | 'fatal'
    code: str            # FHIR issue type: 'structure' | 'invalid' | 'required' | ...
    message: str
    location: str = ""   # FHIRPath-ish location of the offending element


# ---------------------------------------------------------------------------
# Tiny helpers — the stuff every caller needs and shouldn't re-invent
# ---------------------------------------------------------------------------

def reference(resource_type: str, resource_id: str) -> dict:
    """Build a Reference dict pointing at ``ResourceType/id``.

    >>> reference("Patient", "abc-123")
    {'reference': 'Patient/abc-123'}

    Per FHIR, references are typed strings of the form
    ``ResourceType/id`` (relative) or full URLs (absolute). We always
    emit the relative form because most downstream consumers resolve
    references inside a single Bundle context.
    """
    if not resource_type or not resource_id:
        raise ValueError("reference requires both resource_type and resource_id")
    return {"reference": f"{resource_type}/{resource_id}"}


def codeable_concept(system: str, code: str, display: Optional[str] = None) -> dict:
    """Build a CodeableConcept dict.

    A CodeableConcept is just a list of Codings (system + code +
    optional display). The most common pattern is one Coding, which is
    what we emit. For multi-coding cases (e.g., the same diagnosis
    coded in both ICD-10-CM and SNOMED), build the dict by hand.
    """
    coding: dict[str, Any] = {"system": system, "code": code}
    if display is not None:
        coding["display"] = display
    return {"coding": [coding]}


def quantity(value: float, unit_ucum: str) -> dict:
    """Build a Quantity dict using UCUM as the units system.

    UCUM is the FHIR-recommended units system for any clinical
    measurement. ``unit_ucum`` is BOTH the human-readable label AND the
    machine-parseable code — UCUM is designed so the same string serves
    both roles for common units (``mg/dL``, ``mmHg``, ``mL/min/{1.73_m2}``).
    """
    return {
        "value": value,
        "unit": unit_ucum,
        "system": UCUM,
        "code": unit_ucum,
    }


def identifier(system: str, value: str) -> dict:
    """Build an Identifier dict (system + value)."""
    if not system or not value:
        raise ValueError("identifier requires both system and value")
    return {"system": system, "value": value}


def human_name(
    family: str,
    given: Union[list[str], str],
    prefix: Optional[Union[list[str], str]] = None,
    suffix: Optional[Union[list[str], str]] = None,
) -> dict:
    """Build a HumanName dict.

    ``given`` can be a single string or a list (handles middle names).
    ``prefix`` and ``suffix`` follow the same convention. FHIR stores
    all of them as arrays internally because Western-name conventions
    are not universal — a person may have multiple given names with no
    notion of "middle".
    """
    name: dict[str, Any] = {"family": family}
    if isinstance(given, str):
        name["given"] = [given]
    else:
        name["given"] = list(given)
    if prefix is not None:
        name["prefix"] = [prefix] if isinstance(prefix, str) else list(prefix)
    if suffix is not None:
        name["suffix"] = [suffix] if isinstance(suffix, str) else list(suffix)
    return name


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    """Generate a logical id that is FHIR-id-safe (≤64 chars, [A-Za-z0-9._-])."""
    return uuid.uuid4().hex


def _to_dict(model: Any) -> dict:
    """Convert a Pydantic FHIR model to a JSON-serializable dict.

    ``mode="json"`` ensures dates / decimals come out as strings, not
    Python objects — important because the dict is meant to be ready
    for ``json.dumps`` without further coercion.
    """
    return model.model_dump(mode="json", exclude_none=True, by_alias=True)


def _validate_and_dump(model_cls: type, data: dict) -> dict:
    """Construct via Pydantic for validation, then dump to dict.

    This is the funnel every builder runs through: build a dict,
    validate it through ``fhir.resources``, dump it back to a dict.
    Errors here are *programming* errors in the builder (we control the
    inputs), not user-visible validation problems.
    """
    model = model_cls.model_validate(data)
    return _to_dict(model)


def _date_str(d: Union[str, date, datetime, None]) -> Optional[str]:
    """Coerce a date-ish input to FHIR's expected ISO format."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def _datetime_str(d: Union[str, datetime, None]) -> Optional[str]:
    """Coerce a datetime-ish input to FHIR's expected ISO format."""
    if d is None:
        return None
    if isinstance(d, datetime):
        # FHIR requires a timezone offset on dateTimes that include time-of-day.
        s = d.isoformat()
        if d.tzinfo is None:
            s += "Z"
        return s
    return str(d)


# ---------------------------------------------------------------------------
# Resource builders
# ---------------------------------------------------------------------------

def build_patient(
    *,
    family: str,
    given: Union[list[str], str],
    birth_date: Union[str, date, datetime],
    gender: str = "unknown",
    mrn: Optional[str] = None,
    ssn: Optional[str] = None,
    address_line: Optional[Union[str, list[str]]] = None,
    address_city: Optional[str] = None,
    address_state: Optional[str] = None,
    address_postal_code: Optional[str] = None,
    address_country: Optional[str] = "US",
    telecom_phone: Optional[str] = None,
    telecom_email: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> dict:
    """Build a Patient resource.

    Identifiers
    -----------
    The two most common patient identifiers are an MRN (issued by your
    organization, scoped via ``SYSTEM_LOCAL_MRN``) and an SSN. Either
    or both can be provided; both are optional because some upstream
    systems (e.g., emergency arrivals, anonymous tests) genuinely don't
    have one.

    Gender
    ------
    The FHIR ``gender`` element is administrative gender — the value
    set is fixed to ``male | female | other | unknown``. Clinical sex
    or gender identity belong in extensions, not this field. Default
    is ``"unknown"`` because that's the FHIR-conformant way to say "we
    didn't ask".
    """
    pid = patient_id or _new_id()
    data: dict[str, Any] = {
        "resourceType": "Patient",
        "id": pid,
        "name": [human_name(family=family, given=given)],
        "gender": gender,
        "birthDate": _date_str(birth_date),
    }

    identifiers: list[dict] = []
    if mrn:
        identifiers.append({
            "use": "usual",
            "type": codeable_concept(
                "http://terminology.hl7.org/CodeSystem/v2-0203",
                "MR", "Medical record number",
            ),
            **identifier(SYSTEM_LOCAL_MRN, mrn),
        })
    if ssn:
        identifiers.append({
            "use": "official",
            "type": codeable_concept(
                "http://terminology.hl7.org/CodeSystem/v2-0203",
                "SS", "Social Security number",
            ),
            **identifier(SYSTEM_SSN, ssn),
        })
    if identifiers:
        data["identifier"] = identifiers

    telecom: list[dict] = []
    if telecom_phone:
        telecom.append({"system": "phone", "value": telecom_phone, "use": "home"})
    if telecom_email:
        telecom.append({"system": "email", "value": telecom_email})
    if telecom:
        data["telecom"] = telecom

    if any([address_line, address_city, address_state, address_postal_code]):
        addr: dict[str, Any] = {"use": "home", "type": "physical"}
        if address_line:
            addr["line"] = [address_line] if isinstance(address_line, str) else list(address_line)
        if address_city:
            addr["city"] = address_city
        if address_state:
            addr["state"] = address_state
        if address_postal_code:
            addr["postalCode"] = address_postal_code
        if address_country:
            addr["country"] = address_country
        data["address"] = [addr]

    return _validate_and_dump(Patient, data)


def build_encounter(
    *,
    patient_ref: str,
    encounter_class_code: str,
    status: str = "finished",
    period_start: Optional[Union[str, datetime]] = None,
    period_end: Optional[Union[str, datetime]] = None,
    identifier_value: Optional[str] = None,
    encounter_class_system: str = SYSTEM_V3_ACT_CODE,
    encounter_class_display: Optional[str] = None,
    encounter_id: Optional[str] = None,
) -> dict:
    """Build an Encounter resource.

    ``encounter_class_code`` is the v3-ActCode for the class — the most
    common values being ``AMB`` (ambulatory), ``IMP`` (inpatient),
    ``EMER`` (emergency), ``HH`` (home health). The full code system
    is at http://terminology.hl7.org/CodeSystem/v3-ActCode.

    ``patient_ref`` is the bare id (e.g., ``patient-123``) or a full
    relative reference (``Patient/patient-123``). Either works.
    """
    eid = encounter_id or _new_id()
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"

    klass: dict[str, Any] = {
        "system": encounter_class_system,
        "code": encounter_class_code,
    }
    if encounter_class_display is not None:
        klass["display"] = encounter_class_display

    data: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": eid,
        "status": status,
        "class": klass,
        "subject": {"reference": subject},
    }
    if identifier_value:
        data["identifier"] = [identifier("urn:oid:2.16.840.1.113883.4.642.40.5.1", identifier_value)]
    period: dict[str, Any] = {}
    if period_start is not None:
        period["start"] = _datetime_str(period_start)
    if period_end is not None:
        period["end"] = _datetime_str(period_end)
    if period:
        data["period"] = period

    return _validate_and_dump(Encounter, data)


def build_observation(
    *,
    patient_ref: str,
    code_loinc: str,
    code_display: str,
    value: Union[float, int, str, dict],
    unit_ucum: Optional[str] = None,
    effective_datetime: Optional[Union[str, datetime]] = None,
    status: str = "final",
    reference_range_low: Optional[float] = None,
    reference_range_high: Optional[float] = None,
    observation_id: Optional[str] = None,
    encounter_ref: Optional[str] = None,
) -> dict:
    """Build an Observation resource.

    ``value`` polymorphism
    ----------------------
    FHIR's ``Observation.value[x]`` is a choice type. We support three
    of the seven shapes here:

      * **numeric** (``float`` or ``int``) → ``valueQuantity`` with
        ``unit_ucum`` (required for numerics).
      * **dict** containing keys ``"system"``, ``"code"`` (and
        optionally ``"display"``) → ``valueCodeableConcept``. Use this
        for coded results like blood-type or culture organism.
      * **str** → ``valueString``. Free-text result, e.g., a
        physician's plain-English impression.

    ``reference_range_low/high`` are interpreted in the same UCUM unit
    as ``value`` (a separate range unit isn't currently supported by
    this builder; build the dict by hand if you need that).
    """
    oid = observation_id or _new_id()
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"

    data: dict[str, Any] = {
        "resourceType": "Observation",
        "id": oid,
        "status": status,
        "code": codeable_concept(LOINC, code_loinc, code_display),
        "subject": {"reference": subject},
    }
    if encounter_ref:
        enc = encounter_ref if "/" in encounter_ref else f"Encounter/{encounter_ref}"
        data["encounter"] = {"reference": enc}
    if effective_datetime is not None:
        data["effectiveDateTime"] = _datetime_str(effective_datetime)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if unit_ucum is None:
            raise ValueError("numeric Observation.value requires unit_ucum")
        data["valueQuantity"] = quantity(float(value), unit_ucum)
    elif isinstance(value, dict):
        if not {"system", "code"}.issubset(value.keys()):
            raise ValueError("dict Observation.value requires 'system' and 'code' keys")
        data["valueCodeableConcept"] = codeable_concept(
            value["system"], value["code"], value.get("display"),
        )
    elif isinstance(value, str):
        data["valueString"] = value
    else:
        raise TypeError(f"unsupported Observation.value type: {type(value).__name__}")

    if reference_range_low is not None or reference_range_high is not None:
        rr: dict[str, Any] = {}
        if reference_range_low is not None and unit_ucum is not None:
            rr["low"] = quantity(float(reference_range_low), unit_ucum)
        if reference_range_high is not None and unit_ucum is not None:
            rr["high"] = quantity(float(reference_range_high), unit_ucum)
        if rr:
            data["referenceRange"] = [rr]

    return _validate_and_dump(Observation, data)


def build_diagnostic_report(
    *,
    patient_ref: str,
    code_loinc: str,
    observations: list[str],
    code_display: Optional[str] = None,
    status: str = "final",
    issued: Optional[Union[str, datetime]] = None,
    effective_datetime: Optional[Union[str, datetime]] = None,
    report_id: Optional[str] = None,
    conclusion: Optional[str] = None,
) -> dict:
    """Build a DiagnosticReport resource.

    ``observations`` is a list of Observation references — bare ids
    (``obs-123``) or full relative refs (``Observation/obs-123``). The
    builder normalizes both forms.
    """
    rid = report_id or _new_id()
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"

    def _norm_obs(ref: str) -> dict:
        return {"reference": ref if "/" in ref else f"Observation/{ref}"}

    data: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": rid,
        "status": status,
        "code": codeable_concept(LOINC, code_loinc, code_display),
        "subject": {"reference": subject},
        "result": [_norm_obs(r) for r in observations],
    }
    if issued is not None:
        data["issued"] = _datetime_str(issued)
    if effective_datetime is not None:
        data["effectiveDateTime"] = _datetime_str(effective_datetime)
    if conclusion is not None:
        data["conclusion"] = conclusion

    return _validate_and_dump(DiagnosticReport, data)


def build_claim(
    *,
    patient_ref: str,
    provider_ref: str,
    total_amount: float,
    diagnosis_codes: list[str],
    service_lines: list[dict],
    currency: str = "USD",
    created: Optional[Union[str, datetime]] = None,
    identifier_value: Optional[str] = None,
    claim_type_code: str = "professional",
    use: str = "claim",
    priority_code: str = "normal",
    insurer_ref: Optional[str] = None,
    coverage_ref: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> dict:
    """Build a Claim resource (institutional, professional, oral, etc.).

    ``diagnosis_codes``
        ICD-10-CM codes; the builder wraps each in a ``CodeableConcept``
        and assigns a 1-based sequence per the FHIR Claim spec.

    ``service_lines``
        Each is a dict with at minimum:

          * ``cpt`` — CPT/HCPCS code for the procedure
          * ``charge`` — money amount for that line
          * ``unit_count`` — integer (defaults to 1)
          * ``service_date`` — ISO date string (optional)
          * ``diagnosis_seq`` — list of 1-based indexes into
            ``diagnosis_codes`` linking the line to its diagnoses
            (defaults to ``[1]``).

    ``coverage_ref``
        FHIR requires every Claim to declare its insurance focal
        coverage. We default to a synthesized stub coverage if none
        is supplied — fine for unit tests, NOT fine for production.
    """
    cid = claim_id or _new_id()
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"
    provider = provider_ref if "/" in provider_ref else f"Practitioner/{provider_ref}"
    coverage = coverage_ref or "Coverage/coverage-stub"

    diagnosis = []
    for i, dx in enumerate(diagnosis_codes, start=1):
        diagnosis.append({
            "sequence": i,
            "diagnosisCodeableConcept": codeable_concept(ICD10_CM, dx),
        })

    items = []
    for i, line in enumerate(service_lines, start=1):
        if "cpt" not in line or "charge" not in line:
            raise ValueError(f"service_line {i} missing 'cpt' or 'charge'")
        item: dict[str, Any] = {
            "sequence": i,
            "productOrService": codeable_concept(CPT, str(line["cpt"]), line.get("display")),
            "unitPrice": {"value": float(line["charge"]), "currency": currency},
            "net": {
                "value": float(line["charge"]) * int(line.get("unit_count", 1)),
                "currency": currency,
            },
            "quantity": {"value": int(line.get("unit_count", 1))},
            "diagnosisSequence": list(line.get("diagnosis_seq", [1])),
        }
        if "service_date" in line:
            item["servicedDate"] = _date_str(line["service_date"])
        items.append(item)

    data: dict[str, Any] = {
        "resourceType": "Claim",
        "id": cid,
        "status": "active",
        "type": codeable_concept(SYSTEM_CLAIM_TYPE, claim_type_code),
        "use": use,
        "patient": {"reference": subject},
        "created": _datetime_str(created or datetime.now()),
        "provider": {"reference": provider},
        "priority": codeable_concept(SYSTEM_PROCESS_PRIORITY, priority_code),
        "insurance": [{
            "sequence": 1,
            "focal": True,
            "coverage": {"reference": coverage},
        }],
        "diagnosis": diagnosis,
        "item": items,
        "total": {"value": float(total_amount), "currency": currency},
    }
    if identifier_value:
        data["identifier"] = [identifier(
            "urn:oid:2.16.840.1.113883.4.642.40.5.2", identifier_value,
        )]
    if insurer_ref:
        ins = insurer_ref if "/" in insurer_ref else f"Organization/{insurer_ref}"
        data["insurer"] = {"reference": ins}

    return _validate_and_dump(Claim, data)


def build_claim_response(
    *,
    claim_ref: str,
    patient_ref: str,
    insurer_ref: str,
    total_paid: float,
    outcome: str = "complete",
    currency: str = "USD",
    adjudications: Optional[list[dict]] = None,
    claim_type_code: str = "professional",
    use: str = "claim",
    created: Optional[Union[str, datetime]] = None,
    identifier_value: Optional[str] = None,
    response_id: Optional[str] = None,
    disposition: Optional[str] = None,
) -> dict:
    """Build a ClaimResponse resource (the payer's reply to a Claim).

    ``adjudications``
        Each is a per-line adjudication dict with:

          * ``sequence`` — line sequence echoing the Claim.item.sequence
          * ``adjudication`` — list of category/amount pairs:
            ``[{"category": "submitted", "amount": 150.00}, ...]``

        FHIR's ``ClaimResponse.item.adjudication.category`` is itself a
        CodeableConcept; we wrap the supplied string code with the
        standard ``adjudication`` code system.
    """
    rid = response_id or _new_id()
    claim = claim_ref if "/" in claim_ref else f"Claim/{claim_ref}"
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"
    insurer = insurer_ref if "/" in insurer_ref else f"Organization/{insurer_ref}"

    items: list[dict] = []
    if adjudications:
        for adj in adjudications:
            if "sequence" not in adj or "adjudication" not in adj:
                raise ValueError("each adjudication needs 'sequence' and 'adjudication'")
            adj_list = []
            for entry in adj["adjudication"]:
                cat = entry.get("category", "benefit")
                amt = entry.get("amount", 0.0)
                adj_list.append({
                    "category": codeable_concept(
                        "http://terminology.hl7.org/CodeSystem/adjudication", cat,
                    ),
                    "amount": {"value": float(amt), "currency": currency},
                })
            items.append({
                "itemSequence": int(adj["sequence"]),
                "adjudication": adj_list,
            })

    data: dict[str, Any] = {
        "resourceType": "ClaimResponse",
        "id": rid,
        "status": "active",
        "type": codeable_concept(SYSTEM_CLAIM_TYPE, claim_type_code),
        "use": use,
        "patient": {"reference": subject},
        "created": _datetime_str(created or datetime.now()),
        "insurer": {"reference": insurer},
        "request": {"reference": claim},
        "outcome": outcome,
        "payment": {
            "type": codeable_concept(
                "http://terminology.hl7.org/CodeSystem/ex-paymenttype", "complete",
            ),
            "amount": {"value": float(total_paid), "currency": currency},
        },
    }
    if items:
        data["item"] = items
    if identifier_value:
        data["identifier"] = [identifier(
            "urn:oid:2.16.840.1.113883.4.642.40.5.3", identifier_value,
        )]
    if disposition:
        data["disposition"] = disposition

    return _validate_and_dump(ClaimResponse, data)


def build_imaging_study(
    *,
    patient_ref: str,
    study_uid: str,
    modality_code: str,
    started: Optional[Union[str, datetime]] = None,
    series: Optional[list[dict]] = None,
    endpoint_ref: Optional[str] = None,
    status: str = "available",
    study_id: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Build an ImagingStudy resource.

    ``study_uid`` is a DICOM Study Instance UID — typically an OID like
    ``1.2.840.113619.2.5.1762583153.215519.978957063.78``.

    ``series``
        Each series dict carries:

          * ``uid`` — DICOM Series Instance UID
          * ``number`` — series number within the study (1-based)
          * ``modality_code`` — optional override; otherwise inherits
            from the study-level modality
          * ``description``
          * ``instances`` — list of ``{"uid": ..., "sop_class": ...,
            "number": ...}`` dicts

    If ``series`` is omitted, the builder synthesizes one minimal series
    with one minimal instance — useful for fixture generation.
    """
    sid = study_id or _new_id()
    subject = patient_ref if "/" in patient_ref else f"Patient/{patient_ref}"

    if series is None:
        series = [{
            "uid": f"{study_uid}.1",
            "number": 1,
            "modality_code": modality_code,
            "instances": [{
                "uid": f"{study_uid}.1.1",
                "sop_class": "urn:oid:1.2.840.10008.5.1.4.1.1.2",
                "number": 1,
            }],
        }]

    series_out: list[dict] = []
    for s in series:
        if "uid" not in s or "number" not in s:
            raise ValueError("each series needs 'uid' and 'number'")
        s_modality = s.get("modality_code", modality_code)
        s_dict: dict[str, Any] = {
            "uid": str(s["uid"]),
            "number": int(s["number"]),
            "modality": {"system": SYSTEM_DCM, "code": s_modality},
        }
        if s.get("description"):
            s_dict["description"] = s["description"]
        instances = s.get("instances") or []
        if instances:
            inst_out = []
            for inst in instances:
                if "uid" not in inst:
                    raise ValueError("each instance needs 'uid'")
                inst_d: dict[str, Any] = {
                    "uid": str(inst["uid"]),
                    "sopClass": {
                        "system": SYSTEM_DICOM_SOP,
                        "code": inst.get("sop_class", "urn:oid:1.2.840.10008.5.1.4.1.1.2"),
                    },
                }
                if "number" in inst:
                    inst_d["number"] = int(inst["number"])
                inst_out.append(inst_d)
            s_dict["numberOfInstances"] = len(inst_out)
            s_dict["instance"] = inst_out
        series_out.append(s_dict)

    data: dict[str, Any] = {
        "resourceType": "ImagingStudy",
        "id": sid,
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{study_uid}"}],
        "status": status,
        "subject": {"reference": subject},
        "modality": [{"system": SYSTEM_DCM, "code": modality_code}],
        "numberOfSeries": len(series_out),
        "numberOfInstances": sum(len(s.get("instances", [])) for s in series),
        "series": series_out,
    }
    if started is not None:
        data["started"] = _datetime_str(started)
    if endpoint_ref:
        ep = endpoint_ref if "/" in endpoint_ref else f"Endpoint/{endpoint_ref}"
        data["endpoint"] = [{"reference": ep}]
    if description:
        data["description"] = description

    return _validate_and_dump(ImagingStudy, data)


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------

def _resource_request(resource: dict) -> dict:
    """Compute a sensible ``Bundle.entry.request`` for a transaction Bundle.

    If the resource has no id we POST (server assigns); otherwise we
    PUT to ``Type/id``. This matches the FHIR transaction semantics
    most servers expect.
    """
    rt = resource.get("resourceType")
    rid = resource.get("id")
    if not rt:
        raise ValueError("resource missing resourceType — cannot derive request")
    if rid:
        return {"method": "PUT", "url": f"{rt}/{rid}"}
    return {"method": "POST", "url": rt}


def build_bundle_transaction(entries: list[dict]) -> dict:
    """Wrap resources in a transaction Bundle.

    ``entries`` accepts two shapes for caller convenience:

      * **bare resource dicts** — the builder synthesizes a ``request``
        element (POST if no id, PUT if id present) and a fullUrl based
        on the id.
      * **pre-built entry dicts** with explicit ``request`` (and
        optional ``fullUrl`` / ``resource``) — passed through unchanged
        after validation.

    The ``request`` element is what makes a Bundle a transaction: each
    entry tells the server what HTTP verb + path to apply that resource
    to inside the all-or-nothing transaction.
    """
    out_entries: list[dict] = []
    for e in entries:
        if "request" in e and ("resource" in e or "url" not in e):
            out_entries.append(dict(e))
            continue
        # caller passed a bare resource dict
        rt = e.get("resourceType")
        if not rt:
            raise ValueError("transaction entry must be a resource dict or have 'request'")
        rid = e.get("id")
        full_url = f"urn:uuid:{rid}" if rid else f"urn:uuid:{_new_id()}"
        out_entries.append({
            "fullUrl": full_url,
            "resource": e,
            "request": _resource_request(e),
        })

    data = {
        "resourceType": "Bundle",
        "id": _new_id(),
        "type": "transaction",
        "entry": out_entries,
    }
    return _validate_and_dump(Bundle, data)


def build_bundle_collection(resources: list[dict]) -> dict:
    """Wrap resources in a collection Bundle.

    Collection Bundles are the right shape for "here's a static set of
    resources I want to ship together" — Bulk Data export NDJSON
    conversions, document attachments, fixture archives. There is no
    transactional intent, so no ``request`` element on entries.
    """
    out_entries: list[dict] = []
    for r in resources:
        rt = r.get("resourceType")
        if not rt:
            raise ValueError("collection entry must be a resource dict with resourceType")
        rid = r.get("id")
        full_url = f"urn:uuid:{rid}" if rid else f"urn:uuid:{_new_id()}"
        out_entries.append({"fullUrl": full_url, "resource": r})

    data = {
        "resourceType": "Bundle",
        "id": _new_id(),
        "type": "collection",
        "entry": out_entries,
    }
    return _validate_and_dump(Bundle, data)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

# Top-level FHIR resource types we know how to dispatch on.
_RESOURCE_REGISTRY: dict[str, type] = {
    "Patient": Patient,
    "Encounter": Encounter,
    "Observation": Observation,
    "DiagnosticReport": DiagnosticReport,
    "Claim": Claim,
    "ClaimResponse": ClaimResponse,
    "ImagingStudy": ImagingStudy,
    "Bundle": Bundle,
}


def _location_of(loc: tuple) -> str:
    """Render a Pydantic ``loc`` tuple as a FHIRPath-ish string."""
    if not loc:
        return ""
    parts = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f".{item}" if parts else str(item))
    return "".join(parts)


def _classify(error: dict) -> tuple[str, str]:
    """Map a Pydantic error type to (FHIR severity, FHIR issue code)."""
    et = error.get("type", "")
    if et == "missing":
        return "error", "required"
    if "type" in et or "_type" in et or et.endswith("_type"):
        return "error", "structure"
    if et == "value_error":
        return "error", "invalid"
    if et == "fhir-validation-wrong-resource-type":
        return "error", "structure"
    if et == "extra_forbidden":
        return "warning", "structure"
    return "error", "invalid"


def validate(resource: dict) -> list[FhirIssue]:
    """Validate a resource dict via ``fhir.resources``.

    Returns a list of ``FhirIssue`` instead of raising. An empty list
    means the resource passed structural + datatype validation.

    For a resource type we don't have a model class for, we return a
    single ``information`` issue rather than ``error``. (Why: a custom
    profile or experimental resource is still a "valid" thing to push
    through the pipeline; the validator just can't speak to its
    structure.)
    """
    issues: list[FhirIssue] = []

    if not isinstance(resource, dict):
        return [FhirIssue(
            severity="fatal", code="structure",
            message=f"resource must be a dict, got {type(resource).__name__}",
        )]

    rt = resource.get("resourceType")
    if not rt:
        return [FhirIssue(
            severity="error", code="required",
            message="missing 'resourceType'", location="resourceType",
        )]

    model_cls = _RESOURCE_REGISTRY.get(rt)
    if model_cls is None:
        return [FhirIssue(
            severity="information", code="not-supported",
            message=f"validator has no model class for resourceType {rt!r}",
            location="resourceType",
        )]

    try:
        model_cls.model_validate(resource)
    except ValidationError as ve:
        for err in ve.errors():
            severity, code = _classify(err)
            issues.append(FhirIssue(
                severity=severity, code=code,
                message=err.get("msg", ""),
                location=_location_of(err.get("loc", ())),
            ))
    except Exception as e:  # pragma: no cover — defensive
        issues.append(FhirIssue(
            severity="fatal", code="exception",
            message=f"{type(e).__name__}: {e}",
        ))

    return issues


# ---------------------------------------------------------------------------
# Convenience: round-trip
# ---------------------------------------------------------------------------

def round_trip(resource: dict) -> dict:
    """Re-validate and re-dump a resource — catches structural drift.

    Handy for asserting in tests that a hand-built dict survives
    Pydantic's normalization without losing fields.
    """
    rt = resource.get("resourceType")
    if rt not in _RESOURCE_REGISTRY:
        raise ValueError(f"no model registered for resourceType {rt!r}")
    return _validate_and_dump(_RESOURCE_REGISTRY[rt], resource)
