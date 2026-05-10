"""standards_translator.py — Standards Translator / Cross-Mapping generator.

Given a source healthcare standard + target standard (e.g., "HL7v2 ADT^A01"
→ "FHIR Patient+Encounter"), generates a complete mapping package: the
field-by-field mapping table, a runnable transformer in Python that wraps
``healthcare_libs.cross_standards``, end-to-end tests, and a README.

The mapping catalog covers the common industry-standard transforms and
each entry is wired to a concrete production transformer in
``healthcare_libs.cross_standards``:

  HL7v2 ADT^A01 → FHIR Patient + Encounter   (adt_a01_to_patient_encounter)
  HL7v2 ADT^A03 → FHIR Encounter (discharge) (adt_a03_to_encounter_discharge)
  HL7v2 ADT^A08 → FHIR Patient + Encounter   (adt_a08_to_patient_encounter)
  HL7v2 ORU^R01 → FHIR Observation Bundle    (oru_r01_to_observation_bundle)
  X12 837P      → FHIR Claim                 (x12_837p_to_claim)
  X12 835       → FHIR ClaimResponse         (x12_835_to_claim_response)
  DICOM Series  → FHIR ImagingStudy          (dicom_study_to_imaging_study)

For mappings outside this catalog, the generator returns an "unknown
pair" plan with a helpful note listing supported pairs.

Output:
    mapping-<source>-to-<target>/
      README.md             intent + caveats + how to run
      mapping.md            field-by-field source→target table
      transformer.py        thin CLI wrapper over cross_standards.<func>
      tests/test_mapping.py runnable end-to-end test (synthetic source → FHIR)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)
from healthcare_subagent import customization_prompts as _subagent_prompts

LOG = logging.getLogger("mypub-standards-translator")

GENERATOR_TYPE = "standards_translator"


# A mapping entry binds the catalog key to:
#   * a human-friendly source/target/purpose triple
#   * the cited tools (loaded as concept citations from the KB)
#   * a list of (source_path, target_path, transform_kind, notes) tuples
#   * the cross_standards transformer function name
#   * test_builder: name of healthcare_libs.{module}.{builder} that produces
#                   a synthetic source message for the integration test
#   * test_kwargs: kwargs to pass to the builder
#   * expected_resource_types: resourceType values expected in the output
#                              (top-level resourceType, or for Bundle the
#                              entry resourceTypes)
MAPPING_CATALOG: dict[str, dict[str, Any]] = {
    "hl7v2-adt-a01-to-fhir-patient-encounter": {
        "source": "HL7v2 ADT^A01 (Admit/Visit Notification)",
        "target": "FHIR Patient + Encounter",
        "purpose": "Convert an ADT admission message into a FHIR Patient (new "
                   "or existing) plus a new Encounter.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "HL7 Library (PHP)",
                        "Mirth/NextGen Connect", "FHIR Specification",
                        "hl7apy"],
        "transformer_func": "adt_a01_to_patient_encounter",
        "test_builder": "hl7v2.build_adt_a01",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Patient", "Encounter"],
        "fields": [
            ("PID-3 (Patient Identifier List)", "Patient.identifier",
             "lookup", "Iterate IDs; map type code to FHIR identifier system URL"),
            ("PID-5 (Patient Name)", "Patient.name",
             "split", "Family + Given + Prefix + Suffix → HumanName components"),
            ("PID-7 (Date of Birth)", "Patient.birthDate",
             "direct", "HL7v2 TS → FHIR date (YYYY-MM-DD truncation)"),
            ("PID-8 (Sex)", "Patient.gender",
             "code-translation", "HL7 Table 0001 ('M','F','O','U') → FHIR AdministrativeGender ('male','female','other','unknown')"),
            ("PID-11 (Patient Address)", "Patient.address",
             "split", "XAD components → Address.line/city/state/postalCode"),
            ("PID-13/14 (Phone)", "Patient.telecom",
             "lookup", "system=phone; use=home (PID-13) or work (PID-14)"),
            ("PID-19 (SSN)", "Patient.identifier (system=urn:oid:2.16.840.1.113883.4.1)",
             "direct", "Wrap as identifier with US SSN system OID"),
            ("PV1-2 (Patient Class)", "Encounter.class",
             "code-translation", "HL7 Table 0004 ('I','O','E','P') → ActEncounterCode"),
            ("PV1-3 (Assigned Patient Location)", "Encounter.location.location",
             "lookup", "PL components → Location reference"),
            ("PV1-7 (Attending Doctor)", "Encounter.participant.individual (Practitioner)",
             "lookup", "XCN → Practitioner reference (by NPI if present)"),
            ("PV1-19 (Visit Number)", "Encounter.identifier",
             "direct", "CX → Identifier"),
            ("PV1-44 (Admit Date/Time)", "Encounter.period.start",
             "direct", "TS → instant"),
            ("PV1-45 (Discharge Date/Time)", "Encounter.period.end",
             "direct", "TS → instant; absent for A01"),
            ("MSH-7 (Message Date/Time)", "(envelope, not mapped to resource fields)",
             "drop", "Use as Bundle.timestamp if creating a transaction Bundle"),
            ("MSH-9 (Message Type)", "(envelope, drives mapping logic)",
             "drop", "Used to dispatch this mapping; not preserved"),
        ],
    },
    "hl7v2-adt-a03-to-fhir-encounter-discharge": {
        "source": "HL7v2 ADT^A03 (Discharge / End Visit)",
        "target": "FHIR Patient + Encounter (discharge)",
        "purpose": "Convert an ADT discharge message into a FHIR Bundle that "
                   "updates the existing Encounter with a discharge time and "
                   "status=finished.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "Mirth/NextGen Connect",
                        "FHIR Specification", "hl7apy"],
        "transformer_func": "adt_a03_to_encounter_discharge",
        "test_builder": "hl7v2.build_adt_a03",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Patient", "Encounter"],
        "fields": [
            ("PID-3 (Patient Identifier List)", "Patient.identifier",
             "lookup", "Iterate IDs; map type code to FHIR identifier system URL"),
            ("PID-5 (Patient Name)", "Patient.name",
             "split", "Family + Given + Prefix + Suffix → HumanName components"),
            ("PID-7 (Date of Birth)", "Patient.birthDate",
             "direct", "HL7v2 TS → FHIR date (YYYY-MM-DD truncation)"),
            ("PID-8 (Sex)", "Patient.gender",
             "code-translation", "HL7 Table 0001 → FHIR AdministrativeGender"),
            ("PV1-2 (Patient Class)", "Encounter.class",
             "code-translation", "HL7 Table 0004 → ActEncounterCode"),
            ("PV1-19 (Visit Number)", "Encounter.identifier",
             "direct", "CX → Identifier (used to merge onto admission Encounter)"),
            ("PV1-36 (Discharge Disposition)", "Encounter.hospitalization.dischargeDisposition",
             "code-translation", "HL7 Table 0112 → DischargeDisposition CodeableConcept"),
            ("PV1-44 (Admit Date/Time)", "Encounter.period.start",
             "direct", "TS → instant; preserved on update"),
            ("PV1-45 (Discharge Date/Time)", "Encounter.period.end",
             "direct", "TS → instant; the new field for A03"),
            ("(synthesized) Encounter.status", "Encounter.status = 'finished'",
             "compute", "Discharge mapping unconditionally sets status to 'finished'"),
        ],
    },
    "hl7v2-adt-a08-to-fhir-patient-encounter-update": {
        "source": "HL7v2 ADT^A08 (Update Patient Information)",
        "target": "FHIR Patient + Encounter (PUT semantics)",
        "purpose": "Convert an ADT update message into a FHIR Bundle whose "
                   "entries use PUT (upsert) semantics so the receiver "
                   "overwrites the existing Patient + Encounter records.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "Mirth/NextGen Connect",
                        "FHIR Specification", "hl7apy"],
        "transformer_func": "adt_a08_to_patient_encounter",
        "test_builder": "hl7v2.build_adt_a08",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Patient", "Encounter"],
        "fields": [
            ("PID-3 (Patient Identifier List)", "Patient.identifier",
             "lookup", "Iterate IDs; map type code to FHIR identifier system URL"),
            ("PID-5 (Patient Name)", "Patient.name",
             "split", "Family + Given + Prefix + Suffix → HumanName components"),
            ("PID-7 (Date of Birth)", "Patient.birthDate",
             "direct", "HL7v2 TS → FHIR date"),
            ("PID-8 (Sex)", "Patient.gender",
             "code-translation", "HL7 Table 0001 → FHIR AdministrativeGender"),
            ("PID-11 (Patient Address)", "Patient.address",
             "split", "XAD components → Address.line/city/state/postalCode"),
            ("PV1-2 (Patient Class)", "Encounter.class",
             "code-translation", "HL7 Table 0004 → ActEncounterCode"),
            ("PV1-19 (Visit Number)", "Encounter.identifier",
             "direct", "CX → Identifier"),
            ("(envelope) Bundle.entry.request.method", "PUT",
             "compute", "A08 = upsert; entries declare PUT against the supplied id"),
        ],
    },
    "hl7v2-oru-r01-to-fhir-observation": {
        "source": "HL7v2 ORU^R01 (Unsolicited Observation Result)",
        "target": "FHIR Observation + DiagnosticReport (+ Patient ref)",
        "purpose": "Convert an ORU lab result message into FHIR Observations "
                   "grouped under a DiagnosticReport.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "FHIR Specification",
                        "hl7apy", "Mirth/NextGen Connect"],
        "transformer_func": "oru_r01_to_observation_bundle",
        "test_builder": "hl7v2.build_oru_r01",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Observation", "DiagnosticReport"],
        "fields": [
            ("PID-3", "Observation.subject (Patient ref)",
             "lookup", "Patient must already exist in target system; "
                       "Observation references it"),
            ("OBR-2 (Placer Order Number)", "DiagnosticReport.basedOn (ServiceRequest)",
             "lookup", "Order number → ServiceRequest reference"),
            ("OBR-3 (Filler Order Number)", "DiagnosticReport.identifier",
             "direct", "Lab's own ID for the report"),
            ("OBR-4 (Universal Service ID)", "DiagnosticReport.code (LOINC)",
             "code-translation", "CWE → CodeableConcept; map to LOINC if local code"),
            ("OBR-7 (Observation Date/Time)", "DiagnosticReport.effectiveDateTime",
             "direct", "TS → dateTime"),
            ("OBR-22 (Results Rpt/Status Chng - Date/Time)",
             "DiagnosticReport.issued",
             "direct", "TS → instant"),
            ("OBR-25 (Result Status)", "DiagnosticReport.status",
             "code-translation",
             "HL7 Table 0123 ('F','P','C','I','S','X') → FHIR ObservationStatus / DiagnosticReportStatus"),
            ("OBX-2 (Value Type)", "Observation.value[x] type",
             "compute", "NM → valueQuantity; ST → valueString; CWE → valueCodeableConcept; etc."),
            ("OBX-3 (Observation Identifier)", "Observation.code (LOINC)",
             "code-translation", "CWE → CodeableConcept; LOINC if available"),
            ("OBX-5 (Observation Value)", "Observation.value[x]",
             "compute", "Type-dispatched per OBX-2"),
            ("OBX-6 (Units)", "Observation.valueQuantity.unit (UCUM)",
             "code-translation", "CE → UCUM if not already; lossy if local units"),
            ("OBX-7 (References Range)", "Observation.referenceRange",
             "split", "Parse low-high-text → ReferenceRange"),
            ("OBX-8 (Abnormal Flags)", "Observation.interpretation",
             "code-translation", "HL7 Table 0078 ('H','L','N','A') → ObservationInterpretation"),
            ("OBX-11 (Observation Result Status)", "Observation.status",
             "code-translation", "HL7 Table 0085 → FHIR ObservationStatus"),
            ("OBX-14 (Observation Date/Time)", "Observation.effectiveDateTime",
             "direct", "Per-result time; falls back to OBR-7"),
        ],
    },
    "x12-837p-to-fhir-claim": {
        "source": "X12 837P (Professional Health Care Claim)",
        "target": "FHIR Claim",
        "purpose": "Convert a professional claim into a FHIR Claim resource "
                   "with item lines, diagnosis pointers, and provider refs.",
        "tools_cited": ["pyx12", "Ballerina EDI Module", "Stedi (clearinghouse)",
                        "FHIR Specification", "HAPI FHIR"],
        "transformer_func": "x12_837p_to_claim",
        "test_builder": "x12.build_837p",
        "test_kwargs": {},
        "expected_resource_types": ["Claim"],
        "fields": [
            ("BHT (Beginning of Hierarchical Transaction)", "Claim.created",
             "direct", "BHT04 (date) + BHT05 (time) → Claim.created"),
            ("Loop 1000A NM1*41 (Submitter)", "Claim.enterer",
             "lookup", "Submitter → Practitioner or Organization"),
            ("Loop 2000A HL/PRV (Billing Provider)", "Claim.provider",
             "lookup", "NPI → Practitioner or PractitionerRole reference"),
            ("Loop 2000B SBR (Subscriber)", "Coverage.subscriberId / Claim.insurance",
             "split", "Subscriber relationship + group/policy → Coverage resource"),
            ("Loop 2010BA NM1*IL (Subscriber Name)", "Patient.name (when subscriber=patient)",
             "lookup", "Tied to Patient resource by member ID"),
            ("Loop 2300 CLM (Claim Information)", "Claim.identifier, Claim.total, Claim.priority",
             "split", "CLM01 → identifier; CLM02 → total.value; CLM05 → facility"),
            ("Loop 2300 HI (Diagnosis)", "Claim.diagnosis.diagnosisCodeableConcept",
             "code-translation", "ICD-10-CM code → CodeableConcept; pointer order preserved"),
            ("Loop 2400 LX (Service Line Number)", "Claim.item.sequence",
             "direct", "Line number → sequence"),
            ("Loop 2400 SV1 (Professional Service)", "Claim.item.productOrService, .quantity, .unitPrice, .net",
             "split", "SV1-01 (HCPCS|modifiers) → CodeableConcept; SV1-02 → unitPrice; SV1-03/04 → quantity"),
            ("Loop 2400 DTP*472 (Service Date)", "Claim.item.servicedDate / .servicedPeriod",
             "direct", "DTP03 → date or period"),
            ("Loop 2400 REF*6R (Line Item Control Number)", "Claim.item.identifier",
             "direct", "Per-line tracking ID"),
            ("ISA/IEA envelope", "(transport metadata, not in Claim)",
             "drop", "X12 envelope is for clearinghouse routing; FHIR uses MessageHeader if needed"),
        ],
    },
    "x12-835-to-fhir-claimresponse": {
        "source": "X12 835 (Health Care Claim Payment / Advice)",
        "target": "FHIR ClaimResponse + PaymentReconciliation",
        "purpose": "Convert a remittance into a ClaimResponse per claim and a "
                   "single PaymentReconciliation for the payment instrument.",
        "tools_cited": ["pyx12", "Ballerina EDI Module", "Stedi (clearinghouse)",
                        "FHIR Specification"],
        "transformer_func": "x12_835_to_claim_response",
        "test_builder": "x12.build_835",
        "test_kwargs": {},
        # Single-CLP 835 returns a bare ClaimResponse; multi-CLP returns a
        # Bundle of PaymentReconciliation + ClaimResponse(s). Test exercises
        # the single-CLP shape (which is what build_835 emits).
        "expected_resource_types": ["ClaimResponse"],
        "fields": [
            ("BPR (Financial Information)", "PaymentReconciliation.paymentDate, .paymentAmount, .paymentIssuer",
             "split", "BPR16 → paymentDate; BPR02 → paymentAmount; BPR10 → paymentIssuer (BankID)"),
            ("TRN (Reassociation Trace)", "PaymentReconciliation.identifier",
             "direct", "TRN02 → identifier value"),
            ("Loop 1000A N1*PR (Payer Identification)", "ClaimResponse.insurer, PaymentReconciliation.paymentIssuer",
             "lookup", "Payer name + ID → Organization reference"),
            ("Loop 1000B N1*PE (Payee)", "ClaimResponse.requestor",
             "lookup", "Payee → Practitioner or Organization"),
            ("Loop 2100 CLP (Claim Payment Information)", "ClaimResponse per claim — .request, .outcome, .total",
             "split", "CLP01 → request (Claim ref by ICN); CLP02 → outcome; CLP04 → total"),
            ("Loop 2100 CAS (Claim Adjustment)", "ClaimResponse.addItem.adjudication",
             "split", "CAS01 → group code; CAS02 → reason code; CAS03 → amount"),
            ("Loop 2110 SVC (Service Payment Information)", "ClaimResponse.addItem (per service line)",
             "split", "SVC01 → productOrService; SVC02 → submittedAmount; SVC03 → providerPaid"),
        ],
    },
    "dicom-series-to-fhir-imagingstudy": {
        "source": "DICOM Series (multi-instance imaging study)",
        "target": "FHIR ImagingStudy",
        "purpose": "Convert a DICOM Study + Series + Instance hierarchy into a "
                   "FHIR ImagingStudy with per-Series and per-Instance subresources.",
        "tools_cited": ["pydicom", "DCMTK", "FHIR Specification"],
        "transformer_func": "dicom_study_to_imaging_study",
        "test_builder": "dicom.build_minimal_dataset",
        "test_kwargs": {},
        "expected_resource_types": ["ImagingStudy"],
        "fields": [
            ("(0020,000D) StudyInstanceUID", "ImagingStudy.identifier",
             "direct", "DICOM UID → Identifier with system=urn:dicom:uid"),
            ("(0010,0020) PatientID", "ImagingStudy.subject (Patient ref)",
             "lookup", "Patient must exist in target; ImagingStudy references it"),
            ("(0008,0050) AccessionNumber", "ImagingStudy.identifier (with type=ACSN)",
             "direct", "Accession number as additional identifier"),
            ("(0008,0020) StudyDate + (0008,0030) StudyTime", "ImagingStudy.started",
             "concat", "Combine into FHIR dateTime"),
            ("(0008,0061) ModalitiesInStudy", "ImagingStudy.modality",
             "code-translation", "DICOM CS values → http://dicom.nema.org/resources/ontology/DCM"),
            ("(0008,0090) ReferringPhysicianName", "ImagingStudy.referrer (Practitioner)",
             "lookup", "PN → Practitioner reference"),
            ("(0020,000E) SeriesInstanceUID", "ImagingStudy.series.uid",
             "direct", "Per-series UID"),
            ("(0008,0060) Modality (per series)", "ImagingStudy.series.modality",
             "code-translation", "Per-series modality → DICOM ontology code"),
            ("(0020,0011) SeriesNumber", "ImagingStudy.series.number",
             "direct", "Integer"),
            ("(0008,103E) SeriesDescription", "ImagingStudy.series.description",
             "direct", "String"),
            ("(0008,0018) SOPInstanceUID", "ImagingStudy.series.instance.uid",
             "direct", "Per-instance UID"),
            ("(0008,0016) SOPClassUID", "ImagingStudy.series.instance.sopClass",
             "direct", "DICOM SOP Class UID → Coding"),
            ("Pixel data (7FE0,0010)", "(stored separately; ImagingStudy.endpoint references)",
             "drop", "Pixels go to a DICOM web server; ImagingStudy holds the WADO-RS endpoint"),
        ],
    },
    "hl7v2-adt-a04-to-fhir-patient-encounter-register": {
        "source": "HL7v2 ADT^A04 (Register a Patient)",
        "target": "FHIR Patient + Encounter (registration)",
        "purpose": "Convert an outpatient registration / pre-admission ADT message "
                   "into a FHIR Patient + Encounter Bundle. Structurally identical "
                   "to A01; trigger event distinguishes registration from admission.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "Mirth/NextGen Connect",
                        "FHIR Specification", "hl7apy"],
        "transformer_func": "adt_a04_to_patient_encounter",
        "test_builder": "hl7v2.build_adt_a04",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Patient", "Encounter"],
        "fields": [
            ("PID-3 (Patient Identifier List)", "Patient.identifier",
             "lookup", "Iterate IDs; map type code to FHIR identifier system URL"),
            ("PID-5 (Patient Name)", "Patient.name",
             "split", "Family + Given + Prefix + Suffix → HumanName components"),
            ("PID-7 (Date of Birth)", "Patient.birthDate",
             "direct", "HL7v2 TS → FHIR date (YYYY-MM-DD truncation)"),
            ("PID-8 (Sex)", "Patient.gender",
             "code-translation", "HL7 Table 0001 → FHIR AdministrativeGender"),
            ("PV1-2 (Patient Class)", "Encounter.class",
             "code-translation", "Defaults to 'O' (outpatient/AMB) for A04; 'E' for ED registration"),
            ("PV1-3 (Assigned Patient Location)", "Encounter.location.location",
             "lookup", "PL → Location reference"),
            ("PV1-7 (Attending Doctor)", "Encounter.participant.individual",
             "lookup", "XCN → Practitioner reference (by NPI)"),
            ("PV1-19 (Visit Number)", "Encounter.identifier",
             "direct", "CX → Identifier"),
            ("MSH-9 trigger event", "(envelope; A04 vs A01 distinguishes registration from admission)",
             "drop", "Receiver dispatches on MSH-9.2; not preserved in FHIR resources"),
        ],
    },
    "hl7v2-orm-o01-to-fhir-servicerequest": {
        "source": "HL7v2 ORM^O01 (General Order)",
        "target": "FHIR Patient + ServiceRequest (Bundle)",
        "purpose": "Convert a lab/radiology/pharmacy order message into a FHIR "
                   "Bundle with Patient + ServiceRequest. ORC carries order-control "
                   "state (status + intent); OBR carries the ordered service.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "FHIR Specification", "hl7apy"],
        "transformer_func": "orm_o01_to_service_request",
        "test_builder": "hl7v2.build_orm_o01",
        "test_kwargs": {},
        "expected_resource_types": ["Bundle", "Patient", "ServiceRequest"],
        "fields": [
            ("PID-3/5/7/8/11/13/14/19", "Patient.*",
             "lookup", "Same PID mapping as ADT pathway"),
            ("ORC-1 (Order Control)", "ServiceRequest.status + .intent",
             "code-translation", "NW→active/order; CA/DC→revoked; HD→on-hold; CM→completed; RP/UA/UC→active"),
            ("ORC-2 (Placer Order Number)", "ServiceRequest.identifier (placer system)",
             "direct", "EI → Identifier with placer-side OID system"),
            ("ORC-3 (Filler Order Number)", "(emitted as warning, not field)",
             "lossy", "fhir.build_service_request takes one identifier; filler logged in warnings"),
            ("ORC-9 (Date/Time of Transaction)", "ServiceRequest.occurrenceDateTime",
             "direct", "TS → instant"),
            ("ORC-12 (Ordering Provider)", "ServiceRequest.requester (Practitioner)",
             "lookup", "XCN → Practitioner reference (by NPI)"),
            ("OBR-4 (Universal Service ID, CE/CWE)", "ServiceRequest.code",
             "code-translation", "code^display^system; system L→local v2-0396, LN→LOINC, SCT→SNOMED"),
        ],
    },
    "x12-271-to-fhir-coverageeligibilityresponse": {
        "source": "X12 271 (Eligibility, Coverage or Benefit Information)",
        "target": "FHIR CoverageEligibilityResponse",
        "purpose": "Convert a payer's eligibility-response transaction into the "
                   "FHIR shape downstream applications can consume without parsing X12.",
        "tools_cited": ["pyx12", "Ballerina EDI Module", "FHIR Specification"],
        "transformer_func": "x12_271_to_coverage_eligibility_response",
        "test_builder": "x12.build_271",
        "test_kwargs": {"request_270_icn": 1},
        "expected_resource_types": ["CoverageEligibilityResponse"],
        "fields": [
            ("ISA-13 (Interchange Control Number)", "CoverageEligibilityResponse.id",
             "direct", "ICN → 'resp-<icn>'; correlates response to the 270 request"),
            ("NM1*IL element 09 (Subscriber Member ID)", "CoverageEligibilityResponse.patient",
             "direct", "Member ID → Patient reference"),
            ("NM1*PR (Payer Org Name)", "CoverageEligibilityResponse.insurer",
             "direct", "Payer name → Organization reference (caller passes insurer_org_id)"),
            ("EB-01 (first Eligibility/Benefit code)", "CoverageEligibilityResponse.outcome",
             "code-translation", "1=Active→complete; 6=Inactive→complete; default→complete"),
            ("EB segments (full set)", "CoverageEligibilityResponse.disposition",
             "concat", "Summary text: count of active/inactive coverage entries + subscriber"),
            ("EQ/HSD/MSG/REF segments (per-service-type benefit detail)", "CoverageEligibilityResponse.insurance",
             "drop", "v1 emits minimal shape; pass insurance=[] to build_coverage_eligibility_response for detail"),
        ],
    },
}


@dataclass
class _Decomposition:
    mapping_key: Optional[str]  # e.g., "hl7v2-adt-a01-to-fhir-patient-encounter"
    source: str
    target: str
    purpose: str
    fields: list[tuple[str, str, str, str]]  # (src, tgt, transform, notes)
    tools_cited: list[str]
    citations: list[tuple[int, str, int, str]]  # (concept_id, name, doc_section_id, source_name)
    transformer_func: Optional[str] = None
    test_builder: Optional[str] = None
    test_kwargs: dict[str, Any] = field(default_factory=dict)
    expected_resource_types: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-").replace("/", "-").replace(",", "")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-+"
    return "".join(c for c in s if c in keep).strip("-") or "mapping"


def _normalize_query(q: str) -> str:
    """Normalize a user query like 'HL7v2 ADT^A01 to FHIR Patient' to a
    catalog key like 'hl7v2-adt-a01-to-fhir-patient-encounter'."""
    s = q.lower()
    # canonicalize separators
    s = s.replace("→", " to ").replace("->", " to ").replace("=>", " to ")
    s = s.replace("^", "-").replace(",", "").replace("+", "+")
    parts = s.split()
    # Glue with hyphens; drop noise words
    noise = {"a", "an", "the", "and", "with", "into", "from"}
    return "-".join(p for p in parts if p not in noise)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------

class StandardsTranslatorDecomposer:
    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        **_: Any,
    ) -> _Decomposition:
        norm = _normalize_query(query)
        # Try exact match against catalog keys, then prefix match
        matched: Optional[str] = None
        for key in MAPPING_CATALOG:
            if key in norm or norm in key:
                matched = key
                break
        if matched is None:
            # Substring scan: find catalog key with the most token overlap
            tokens = set(norm.split("-"))
            best, best_score = None, 0
            for key in MAPPING_CATALOG:
                key_tokens = set(key.split("-"))
                score = len(tokens & key_tokens)
                if score > best_score:
                    best, best_score = key, score
            if best_score >= 3:
                matched = best

        if matched is None:
            # Fallback: emit a "skeleton" mapping with explanatory notes
            return _Decomposition(
                mapping_key=None,
                source=query, target="(target not parsed)",
                purpose=f"unrecognized mapping pair: {query!r}",
                fields=[],
                tools_cited=[],
                citations=[],
                notes=[
                    f"no built-in mapping for {query!r}; supported pairs:",
                    *[f"  - {k}" for k in MAPPING_CATALOG],
                ],
            )

        meta = MAPPING_CATALOG[matched]
        # Load citations from the cited tools
        cited_names = meta.get("tools_cited", [])
        citations: list[tuple[int, str, int, str]] = []
        if cited_names:
            placeholders = ",".join(["?"] * len(cited_names))
            rows = conn.execute(
                f"""
                SELECT DISTINCT c.concept_id, c.name, ds.doc_section_id, src.name
                  FROM concept c
                  JOIN concept_relation cr ON cr.from_concept_id = c.concept_id
                                           OR cr.to_concept_id = c.concept_id
                  JOIN doc_section ds ON cr.source_id = ds.doc_section_id
                                      AND cr.source_type = 'doc_section'
                  JOIN doc_snapshot sn USING (snapshot_id)
                  JOIN doc_source src ON sn.doc_source_id = src.doc_source_id
                 WHERE src.name IN ({placeholders})
                 ORDER BY src.name
                 LIMIT 30
                """,
                list(cited_names),
            ).fetchall()
            citations = [(int(r[0]), r[1], int(r[2]), r[3]) for r in rows]

        notes: list[str] = []
        if not citations:
            notes.append(
                "no concept citations from the cited tools; "
                "package ships from built-in mapping spec only"
            )
        return _Decomposition(
            mapping_key=matched,
            source=meta["source"], target=meta["target"],
            purpose=meta["purpose"],
            fields=list(meta["fields"]),
            tools_cited=cited_names,
            citations=citations,
            transformer_func=meta.get("transformer_func"),
            test_builder=meta.get("test_builder"),
            test_kwargs=dict(meta.get("test_kwargs", {})),
            expected_resource_types=list(meta.get("expected_resource_types", [])),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    func = d.transformer_func or "<unknown>"
    lines = [
        f"# Standards Translator — {d.source} → {d.target}",
        "",
        f"**Purpose.** {d.purpose}",
        "",
        f"This package's transforms are powered by "
        f"`healthcare_libs.cross_standards.{func}` — a production-grade "
        f"transformer with field-level mappings, code-system translation, "
        f"and structured warnings on lossy/optional fields. The files in "
        f"this package are a thin wrapper + reference table around that "
        f"library function.",
        "",
        "## What this package contains",
        "",
        "- `mapping.md` — field-by-field source → target table with the "
        "transform pattern (direct, lookup, split, code-translation, "
        "compute, drop) and notes",
        f"- `transformer.py` — runnable CLI wrapper that calls "
        f"`{func}` and writes the resulting FHIR JSON to disk. Supports "
        "`--deid` for HIPAA Safe Harbor post-processing.",
        "- `tests/test_mapping.py` — end-to-end test that builds a "
        "synthetic source message, runs it through the transformer, and "
        "validates the output against the FHIR R4 spec via "
        "`healthcare_libs.fhir.validate`",
        "",
        "## Install",
        "",
        "```bash",
        "# healthcare_libs lives alongside this package in mypub. Either",
        "# install mypub or set PYTHONPATH to the kb-mcp directory:",
        "export PYTHONPATH=/path/to/myPub/mcp-servers/kb-mcp:$PYTHONPATH",
        "",
        "# Runtime deps (subset of healthcare_libs requirements):",
        "pip install hl7apy fhir.resources pydicom",
        "```",
        "",
        "## How to run",
        "",
        "```bash",
        "# Transform a message",
        "python transformer.py --input /path/to/source.msg --output /path/to/target.json",
        "",
        "# Transform AND apply HIPAA Safe Harbor de-id post-pass",
        "python transformer.py --input source.msg --output target.json --deid",
        "",
        "# Run the integration test (synthetic source → FHIR validation)",
        "pytest tests/test_mapping.py",
        "```",
        "",
        "## Caveats",
        "",
        "- **Lossy fields** are explicitly marked in `mapping.md` (transform "
        "type `lossy`). Round-trip equivalence does NOT hold for these — "
        "the source can be regenerated only with externally-stored context.",
        "- **Code translation** assumes your local code system is the standard "
        "one. If you use site-specific value sets, add a translation table.",
        "- **Reference resolution** (e.g., `Patient.identifier` → `Patient` "
        "ref) requires the target system has the referenced resources.",
        "- **Warnings** from `cross_standards.{func}` are printed to stderr; "
        "callers should capture them for quarantine/ACK decisions.",
        "",
    ]
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for _cid, cname, _ds, src in d.citations[:15]:
            lines.append(f"- **{cname}** — {src}")
        lines.append("")
    if d.notes:
        lines.extend(["## Notes", ""])
        for n in d.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


def _render_mapping(d: _Decomposition) -> str:
    lines = [
        f"# Field Mapping — {d.source} → {d.target}",
        "",
        f"_{len(d.fields)} field mapping(s)._",
        "",
        f"Implementation: `healthcare_libs.cross_standards.{d.transformer_func}`.",
        "",
        "| Source | Target | Transform | Notes |",
        "|---|---|---|---|",
    ]
    for src, tgt, transform, notes in d.fields:
        # Escape pipes in source path
        src_e = src.replace("|", "\\|")
        tgt_e = tgt.replace("|", "\\|")
        notes_e = notes.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{src_e}` | `{tgt_e}` | `{transform}` | {notes_e} |")
    lines.extend([
        "",
        "## Transform legend",
        "",
        "- **direct** — value copies through unchanged (modulo type coercion)",
        "- **lookup** — value resolves a reference (e.g., NPI → Practitioner)",
        "- **split** — one source field → multiple target fields",
        "- **concat** — multiple source fields → one target field",
        "- **code-translation** — value passes through a code-system mapping",
        "- **compute** — target value computed from source (type-dispatched, "
        "arithmetic, etc.)",
        "- **lossy** — full equivalence not preserved; document the loss",
        "- **drop** — source field intentionally not preserved",
        "",
    ])
    if d.citations:
        lines.extend(["## Citations from the catalog", ""])
        for cid, cname, ds, src in d.citations:
            lines.append(f"- {cname} (concept_id={cid}, doc_section={ds}, source={src})")
    return "\n".join(lines)


def _render_transformer(d: _Decomposition) -> str:
    """Generate a thin Python wrapper over healthcare_libs.cross_standards."""
    func = d.transformer_func or "adt_a01_to_patient_encounter"
    # The transformer accepts a string for HL7v2/X12, bytes for DICOM.
    is_dicom = "dicom" in func
    if is_dicom:
        read_input = "args.input.read_bytes()"
        input_doc = "DICOM Part 10 bytes (as written by pydicom.Dataset.save_as)"
    else:
        read_input = "args.input.read_text()"
        input_doc = "wire-format string (HL7 v2 or X12)"
    lines = [
        f'"""Transformer for {d.source} → {d.target}.',
        '',
        f'Uses ``healthcare_libs.cross_standards.{func}`` for the actual',
        'mapping logic. This file is a thin CLI/import wrapper; the per-field',
        f'rules live in ``healthcare_libs.cross_standards.{func}``.',
        '',
        'Usage:',
        '    python transformer.py --input source.msg --output target.json',
        '    python transformer.py --input source.msg --output target.json --deid',
        '"""',
        'from __future__ import annotations',
        '',
        'import argparse',
        'import json',
        'import sys',
        'from pathlib import Path',
        '',
        f'from healthcare_libs.cross_standards import {func}',
        'from healthcare_libs import deid',
        'from healthcare_libs.cross_standards import deidentified_transform',
        '',
        '',
        'def transform(source, *, config: dict | None = None) -> dict:',
        f'    """Transform a single source message.',
        '',
        f'    ``source`` is {input_doc}.',
        f'    Returns the FHIR resource dict (Bundle, ImagingStudy, etc.).',
        f'    Warnings from the transformer are written to stderr.',
        '    """',
        f'    result = {func}(source)',
        '    if result.warnings:',
        '        for w in result.warnings:',
        '            print(f"warning: {w}", file=sys.stderr)',
        '    if result.notes:',
        '        for n in result.notes:',
        '            print(f"note: {n}", file=sys.stderr)',
        '    return result.result',
        '',
        '',
        'def transform_with_deid(source, *, deid_config) -> dict:',
        '    """Run the transform AND post-process with HIPAA Safe Harbor de-id."""',
        '    result = deidentified_transform(',
        f'        {func}, source, deid_config=deid_config,',
        '    )',
        '    if result.warnings:',
        '        for w in result.warnings:',
        '            print(f"warning: {w}", file=sys.stderr)',
        '    return result.result',
        '',
        '',
        'def main():',
        '    p = argparse.ArgumentParser(description=__doc__)',
        '    p.add_argument("--input", type=Path, required=True,',
        f'                   help="Input source message ({input_doc})")',
        '    p.add_argument("--output", type=Path, required=True,',
        '                   help="Output FHIR JSON file")',
        '    p.add_argument("--deid", action="store_true",',
        '                   help="Apply HIPAA Safe Harbor de-id post-transform")',
        '    p.add_argument("--deid-salt", default="standards-translator-salt",',
        '                   help="Pseudonym salt for de-id (rotate per release)")',
        '    p.add_argument("--deid-seed", default="standards-translator-seed",',
        '                   help="Date-shift seed for de-id")',
        '    args = p.parse_args()',
        f'    source = {read_input}',
        '    if args.deid:',
        '        cfg = deid.DeidConfig(',
        '            pseudonym_salt=args.deid_salt,',
        '            date_offset_seed=args.deid_seed,',
        '        )',
        '        result = transform_with_deid(source, deid_config=cfg)',
        '    else:',
        '        result = transform(source)',
        '    args.output.write_text(json.dumps(result, indent=2, default=str))',
        '    print(f"wrote {args.output}", file=sys.stderr)',
        '',
        '',
        'if __name__ == "__main__":',
        '    main()',
    ]
    return "\n".join(lines)


def _render_test(d: _Decomposition) -> str:
    """Generate a runnable end-to-end test using a synthetic source message."""
    func = d.transformer_func or "adt_a01_to_patient_encounter"
    builder = d.test_builder or "hl7v2.build_adt_a01"
    builder_module, builder_name = builder.split(".", 1)
    expected_types = d.expected_resource_types or ["Bundle"]
    primary_type = expected_types[0]
    is_bundle = primary_type == "Bundle"
    is_dicom = builder_module == "dicom"

    # Build the source-message construction snippet
    if is_dicom:
        # build_minimal_dataset returns a Dataset; cross_standards accepts it
        source_construction = (
            f'    return {builder_module}.{builder_name}()'
        )
    else:
        source_construction = (
            f'    return {builder_module}.{builder_name}()'
        )

    # Assertion block per result shape
    assert_lines: list[str] = []
    if is_bundle:
        assert_lines.extend([
            f'    assert result.get("resourceType") == "{primary_type}", \\',
            f'        f"expected resourceType=Bundle, got {{result.get(\'resourceType\')!r}}"',
            '    entry_types = [e["resource"]["resourceType"] for e in result.get("entry", [])]',
        ])
        for rt in expected_types[1:]:
            assert_lines.append(
                f'    assert "{rt}" in entry_types, '
                f'f"expected {rt} in bundle, got {{entry_types}}"'
            )
    else:
        assert_lines.append(
            f'    assert result.get("resourceType") == "{primary_type}", \\'
        )
        assert_lines.append(
            f'        f"expected resourceType={primary_type}, got '
            f'{{result.get(\'resourceType\')!r}}"'
        )

    lines = [
        f'"""End-to-end test for {d.source} → {d.target}.',
        '',
        'Builds a synthetic source message, runs it through the generated',
        f'transformer (which calls healthcare_libs.cross_standards.{func}),',
        'and validates the FHIR output against the R4 spec.',
        '"""',
        'from __future__ import annotations',
        '',
        'import sys',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        '# Make the local transformer.py importable',
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))',
        '',
        f'from healthcare_libs import {builder_module}, fhir  # noqa: E402',
        f'from healthcare_libs.cross_standards import {func}  # noqa: E402',
        '',
        'import transformer  # noqa: E402',
        '',
        '',
        '@pytest.fixture',
        'def source_message():',
        f'    """Construct a synthetic {d.source} via the format-lib builder."""',
        source_construction,
        '',
        '',
        'def test_transform_returns_expected_resource_type(source_message):',
        '    """The transformer returns a FHIR resource of the expected type."""',
        '    result = transformer.transform(source_message)',
        '    assert isinstance(result, dict), f"expected dict, got {type(result)}"',
        *assert_lines,
        '',
        '',
        'def test_transform_round_trip_through_cross_standards(source_message):',
        '    """The wrapped transform matches calling cross_standards directly."""',
        f'    direct = {func}(source_message)',
        '    via_wrapper = transformer.transform(source_message)',
        '    # Both paths yield the same resourceType (ids may differ across runs)',
        '    assert direct.result.get("resourceType") == via_wrapper.get("resourceType")',
        '',
        '',
        'def test_target_spec_conformance(source_message):',
        f'    """The {d.target} output validates against the FHIR R4 spec."""',
        '    result = transformer.transform(source_message)',
        '    issues = fhir.validate(result)',
        '    errors = [i for i in issues if i.severity in ("error", "fatal")]',
        '    assert not errors, f"FHIR validation errors: {[(i.location, i.message) for i in errors]}"',
        '',
    ]
    if is_bundle:
        lines.extend([
            '',
            'def test_each_bundle_entry_validates(source_message):',
            '    """Each entry resource also passes spec validation."""',
            '    result = transformer.transform(source_message)',
            '    for i, entry in enumerate(result.get("entry", [])):',
            '        resource = entry.get("resource") or {}',
            '        issues = fhir.validate(resource)',
            '        errors = [iss for iss in issues if iss.severity in ("error", "fatal")]',
            '        assert not errors, (',
            '            f"entry[{i}] ({resource.get(\'resourceType\')}) failed: "',
            '            f"{[(e.location, e.message) for e in errors]}"',
            '        )',
            '',
        ])
    lines.extend([
        '',
        'def test_lossy_fields_documented():',
        '    """Lossy transforms must be marked in mapping.md (manual sanity check)."""',
        '    mapping_md = (Path(__file__).resolve().parent.parent / "mapping.md").read_text()',
        '    if "lossy" in mapping_md.lower():',
        '        assert "**lossy**" in mapping_md, "lossy fields used but not in legend"',
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class StandardsTranslatorPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        if d.mapping_key is None:
            return GenPlan(
                generator_type=GENERATOR_TYPE,
                package_name=package_name or "mapping-unknown",
                domain=f"{d.source} → {d.target}",
                source_query=d.source,
                package_metadata={"error": "unknown mapping pair"},
                notes=list(d.notes),
            )
        pkg_name = package_name or f"mapping-{d.mapping_key}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=f"{d.source} → {d.target}",
            source_query=d.source,
            package_metadata={
                "mapping_key": d.mapping_key,
                "source": d.source,
                "target": d.target,
                "n_fields": len(d.fields),
                "n_citations": len(d.citations),
                "transformer_func": d.transformer_func,
            },
            notes=list(d.notes),
        )
        sources: list[tuple[str, int, float, float, Optional[str]]] = []
        for cid, _, _, _ in d.citations[:10]:
            sources.append(("concept", cid, 1.0, 1.0, None))
        for _, _, ds, _ in d.citations[:10]:
            sources.append(("doc_section", ds, 1.0, 1.0, None))
        plan.units.append(GenUnit(
            unit_type="standards_mapping",
            name=f"{d.source} → {d.target}",
            ordinal=1,
            metadata={"mapping_key": d.mapping_key,
                      "n_fields": len(d.fields),
                      "transformer_func": d.transformer_func},
            logical_key="mapping_main",
            sources=sources,
        ))
        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="mapping.md", content=_render_mapping(d), purpose="reference"),
            GenFile(filename="transformer.py", content=_render_transformer(d), purpose="code"),
            GenFile(filename="tests/test_mapping.py", content=_render_test(d), purpose="test"),
        ])
        plan.files.extend(_subagent_prompts("standards_translator", {
            "source_format": d.source,
            "target_format": d.target,
            "transformer_func": d.transformer_func or "(unknown)",
        }))
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class StandardsTranslatorValidator:
    def validate(self, conn, plan: GenPlan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("error") == "unknown mapping pair":
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message=f"unknown mapping pair — supported: "
                        f"{sorted(MAPPING_CATALOG)}",
            ))
            return issues
        if plan.package_metadata.get("n_fields", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="mapping_main", severity="error",
                message="no field mappings in the catalog entry — incomplete spec",
            ))
        if not plan.package_metadata.get("transformer_func"):
            issues.append(ValidationIssue(
                unit_logical_key="mapping_main", severity="error",
                message="catalog entry missing transformer_func — generated "
                        "transformer would be inert",
            ))
        if plan.package_metadata.get("n_citations", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="mapping_main", severity="warning",
                message="no concept citations from cited tools — package "
                        "ships from built-in mapping spec only",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer (identical pattern to the other healthcare generators)
# ---------------------------------------------------------------------------

class StandardsTranslatorMaterializer:
    def materialize(self, conn, package_id, output_root, *, overwrite=True):
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?",
            [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = conn.execute(
            "SELECT filename, content FROM generated_file "
            "WHERE package_id = ? ORDER BY file_id",
            [package_id],
        ).fetchall()
        written: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                continue
            target.write_text(content)
            written.append(str(target))
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=[],
        )


def make_standards_translator_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=StandardsTranslatorDecomposer(),
        planner=StandardsTranslatorPlanner(),
        ranking_mode="generation",
        validator=StandardsTranslatorValidator(),
        materializer=StandardsTranslatorMaterializer(),
    )
