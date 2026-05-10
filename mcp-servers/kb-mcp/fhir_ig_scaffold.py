"""fhir_ig_scaffold.py — FHIR Implementation Guide Scaffold generator.

For a use case + base IG (US Core / IPS / IHE), generates a SUSHI/FSH
Implementation Guide skeleton with profiles, value sets, extensions,
sample resources, and the build configuration files. The result is a
buildable IG repo: ``sushi build`` produces the StructureDefinitions;
the IG Publisher consumes those + pagecontent to build the website.

Design notes
------------
The catalog now carries per-field metadata (cardinality, must-support,
value-set bindings) for each profile. The FSH renderer translates that
into real FSH constraints — no more ``// TODO`` stubs. The example
renderer builds JSON via ``healthcare_libs.fhir.build_*`` so every
example is a structurally valid FHIR resource that round-trips through
Pydantic and would pass ``healthcare_libs.fhir.validate()`` cleanly.

Output:
    fhir-ig-<use-case>/
      README.md                  intent + how to build + caveats
      sushi-config.yaml          SUSHI configuration
      ig.ini                     IG Publisher configuration
      input/
        pagecontent/
          index.md               landing page
          background.md          use-case rationale
        profiles/                FSH profile skeletons (one per resource)
        valuesets/               FSH ValueSet skeletons
        extensions/              FSH Extension skeletons (if needed)
        examples/                JSON example resources (one per profile)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)
from healthcare_libs import fhir as hfhir

# Extend the validator registry so the resource types we emit examples
# for actually get structural validation (not just an "information"
# pass-through). We import the model classes lazily — failure to import
# any one of them is acceptable; the validator falls back to its
# information issue.
try:  # pragma: no cover — defensive
    from fhir.resources.R4B.condition import Condition as _R4BCondition
    from fhir.resources.R4B.medicationrequest import MedicationRequest as _R4BMedicationRequest
    from fhir.resources.R4B.medicationstatement import (
        MedicationStatement as _R4BMedicationStatement,
    )
    from fhir.resources.R4B.documentreference import DocumentReference as _R4BDocumentReference
    from fhir.resources.R4B.composition import Composition as _R4BComposition
    from fhir.resources.R4B.allergyintolerance import AllergyIntolerance as _R4BAllergyIntolerance
    from fhir.resources.R4B.immunization import Immunization as _R4BImmunization
    from fhir.resources.R4B.procedure import Procedure as _R4BProcedure
    from fhir.resources.R4B.coverage import Coverage as _R4BCoverage
    from fhir.resources.R4B.practitioner import Practitioner as _R4BPractitioner
    from fhir.resources.R4B.organization import Organization as _R4BOrganization
    from fhir.resources.R4B.servicerequest import ServiceRequest as _R4BServiceRequest
    from fhir.resources.R4B.researchsubject import ResearchSubject as _R4BResearchSubject
    from fhir.resources.R4B.researchstudy import ResearchStudy as _R4BResearchStudy
    from fhir.resources.R4B.adverseevent import AdverseEvent as _R4BAdverseEvent
    from fhir.resources.R4B.group import Group as _R4BGroup

    hfhir._RESOURCE_REGISTRY.update({
        "Condition": _R4BCondition,
        "MedicationRequest": _R4BMedicationRequest,
        "MedicationStatement": _R4BMedicationStatement,
        "DocumentReference": _R4BDocumentReference,
        "Composition": _R4BComposition,
        "AllergyIntolerance": _R4BAllergyIntolerance,
        "Immunization": _R4BImmunization,
        "Procedure": _R4BProcedure,
        "Coverage": _R4BCoverage,
        "Practitioner": _R4BPractitioner,
        "Organization": _R4BOrganization,
        "ServiceRequest": _R4BServiceRequest,
        "ResearchSubject": _R4BResearchSubject,
        "ResearchStudy": _R4BResearchStudy,
        "AdverseEvent": _R4BAdverseEvent,
        "Group": _R4BGroup,
    })
except ImportError:  # pragma: no cover
    pass

LOG = logging.getLogger("mypub-fhir-ig")

GENERATOR_TYPE = "fhir_ig_scaffold"


# ---------------------------------------------------------------------------
# Profile-metadata structure
# ---------------------------------------------------------------------------
#
# Each resource in a use case carries a ``ProfileMeta`` dict with:
#
#   * ``cardinality`` — {field: "min..max"} where field is dotted FHIR
#     element path (e.g. "code.coding"). The renderer emits "* field min..max".
#   * ``must_support`` — list of fields to mark MS.
#   * ``bindings`` — {field: (valueset_url, strength)}. Strength is one of
#     "required" | "extensible" | "preferred" | "example".
#   * ``fixed`` — {field: literal value} for fixed-value constraints.
#   * ``slicing`` — list of slice declarations (free-form FSH lines).
#   * ``narrative`` — short prose paragraph describing the profile's
#     clinical purpose (used in pagecontent).
#
# All keys are optional. Order of the renderer output: MS → cardinality
# → bindings → fixed → slicing.

ProfileMeta = dict[str, Any]


def _pm(
    *,
    must_support: Optional[list[str]] = None,
    cardinality: Optional[dict[str, str]] = None,
    bindings: Optional[dict[str, tuple[str, str]]] = None,
    fixed: Optional[dict[str, str]] = None,
    slicing: Optional[list[str]] = None,
    narrative: Optional[str] = None,
) -> ProfileMeta:
    """Sugar for building a ProfileMeta dict — keeps the catalog readable."""
    out: ProfileMeta = {}
    if must_support:
        out["must_support"] = list(must_support)
    if cardinality:
        out["cardinality"] = dict(cardinality)
    if bindings:
        out["bindings"] = dict(bindings)
    if fixed:
        out["fixed"] = dict(fixed)
    if slicing:
        out["slicing"] = list(slicing)
    if narrative:
        out["narrative"] = narrative
    return out


# Common URLs used in bindings — kept here so swapping a value set in one
# place doesn't desync across catalog entries.
US_CORE_RACE_VS = "http://hl7.org/fhir/us/core/ValueSet/omb-race-category"
US_CORE_ETHNICITY_VS = "http://hl7.org/fhir/us/core/ValueSet/omb-ethnicity-category"
ICD10_CM_VS = "http://hl7.org/fhir/ValueSet/icd-10"
SNOMED_VS = "http://hl7.org/fhir/ValueSet/clinical-findings"
LOINC_VS = "http://hl7.org/fhir/ValueSet/observation-codes"
RXNORM_VS = "http://hl7.org/fhir/ValueSet/medication-codes"


# Use case catalog. Each entry specifies the resource set + base profiles
# the IG should constrain, plus the value sets that need binding.
#
# Resource entries are 4-tuples:
#   (resource_type, profile_name, purpose, profile_meta)
# where profile_meta is a ProfileMeta dict (see above).
USE_CASE_CATALOG: dict[str, dict[str, Any]] = {
    "bulk-data-export": {
        "name": "Bulk Data Export for Clinical Trials",
        "description": "FHIR Backend Services + Bulk Data Export — secure "
                       "extraction of cohort-scale data for clinical research. "
                       "Implements the SMART App Launch Backend Services "
                       "specification + the FHIR Bulk Data Access (Flat FHIR) "
                       "specification.",
        "fhir_version": "R4",
        "base_ig": "us-core",
        "tools_cited": ["FHIR Specification", "HAPI FHIR", "Medplum",
                        "US Core Implementation Guide", "Microsoft FHIR Server"],
        "clinical_workflow": (
            "A research coordinator submits a cohort definition to the "
            "Backend Services client. The client authenticates via "
            "client_credentials + JWT assertion, kicks off a `$export` "
            "operation against the FHIR server, polls until the manifest "
            "is ready, then downloads NDJSON files per resource type. "
            "Files land in a research data lake where the bioinformatics "
            "team runs cohort analytics."
        ),
        "resources": [
            ("Patient", "USCorePatient",
             "Constrain to research-cohort identifiers",
             _pm(
                 must_support=["identifier", "name", "gender",
                                "birthDate", "address", "telecom"],
                 cardinality={"identifier": "1..*", "name": "1..*",
                              "gender": "1..1"},
                 bindings={"gender": ("http://hl7.org/fhir/ValueSet/administrative-gender",
                                       "required")},
                 narrative="Patients in scope for the cohort export — must "
                           "carry at least one organizational MRN identifier.",
             )),
            ("Group", "ResearchSubjectGroup",
             "The cohort being exported",
             _pm(
                 must_support=["type", "actual", "member", "name",
                                "quantity", "characteristic"],
                 cardinality={"type": "1..1", "actual": "1..1",
                              "member": "1..*"},
                 fixed={"type": "person", "actual": "true"},
                 narrative="The Group resource enumerates Patient/ResearchSubject "
                           "members in the export cohort.",
             )),
            ("Observation", "USCoreLabResultObservation",
             "Lab results in scope",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "effectiveDateTime", "valueQuantity"],
                 cardinality={"status": "1..1", "category": "1..*",
                              "code": "1..1", "subject": "1..1"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="Laboratory results bound to LOINC codes.",
             )),
            ("Condition", "USCoreCondition",
             "Diagnoses in scope",
             _pm(
                 must_support=["clinicalStatus", "verificationStatus",
                                "category", "code", "subject"],
                 cardinality={"clinicalStatus": "1..1",
                              "verificationStatus": "1..1",
                              "code": "1..1", "subject": "1..1"},
                 bindings={"code": (ICD10_CM_VS, "extensible")},
                 narrative="Active and resolved diagnoses, ICD-10-CM coded.",
             )),
            ("MedicationRequest", "USCoreMedicationRequest",
             "Medications in scope",
             _pm(
                 must_support=["status", "intent", "medicationCodeableConcept",
                                "subject", "authoredOn", "requester"],
                 cardinality={"status": "1..1", "intent": "1..1",
                              "subject": "1..1"},
                 bindings={"medicationCodeableConcept": (RXNORM_VS, "extensible")},
                 narrative="Active prescriptions, RxNorm coded.",
             )),
            ("DocumentReference", "USCoreDocumentReference",
             "Clinical notes",
             _pm(
                 must_support=["status", "type", "category", "subject",
                                "content"],
                 cardinality={"status": "1..1", "type": "1..1",
                              "subject": "1..1", "content": "1..*"},
                 narrative="Clinical notes attached to the cohort.",
             )),
        ],
        "value_sets": [
            ("BulkExportResourceTypes", "Resources eligible for export"),
            ("ExportJobStatus",         "Status values for an export job"),
        ],
        "extensions": [
            ("export-job-id", "Reference back to the originating export job"),
        ],
    },
    "patient-summary": {
        "name": "International Patient Summary (IPS)",
        "description": "FHIR-based patient summary spec — a snapshot of a "
                       "patient's most relevant clinical data. Useful for "
                       "cross-border + cross-organization handoffs (tumor "
                       "boards, second opinions, ED summaries).",
        "fhir_version": "R4",
        "base_ig": "ips",
        "tools_cited": ["FHIR Specification", "HAPI FHIR", "Medplum",
                        "US Core Implementation Guide"],
        "clinical_workflow": (
            "A patient transfers care across organizations or borders. "
            "The originating EHR exports an IPS Bundle — a Composition "
            "anchoring sections for Allergies, Conditions, Medications, "
            "Immunizations, Recent Results, and Procedures. The receiving "
            "system parses the Bundle and populates its own record."
        ),
        "resources": [
            ("Composition", "IPSComposition",
             "The bundle's table-of-contents",
             _pm(
                 must_support=["status", "type", "subject", "date",
                                "author", "title", "section"],
                 cardinality={"status": "1..1", "type": "1..1",
                              "subject": "1..1", "section": "1..*"},
                 fixed={"status": "final"},
                 narrative="The IPS Composition is the entry point of the "
                           "Bundle — it lists every section the receiver "
                           "should expect.",
             )),
            ("Patient", "IPSPatient",
             "Patient demographics",
             _pm(
                 must_support=["identifier", "name", "gender",
                                "birthDate", "address"],
                 cardinality={"name": "1..*", "gender": "1..1",
                              "birthDate": "1..1"},
                 narrative="The patient who is the subject of the summary.",
             )),
            ("AllergyIntolerance", "IPSAllergyIntolerance",
             "Active allergies",
             _pm(
                 must_support=["clinicalStatus", "verificationStatus",
                                "type", "category", "code", "patient"],
                 cardinality={"code": "1..1", "patient": "1..1"},
                 narrative="Allergies/intolerances — life-critical info.",
             )),
            ("Condition", "IPSCondition",
             "Active problem list",
             _pm(
                 must_support=["clinicalStatus", "verificationStatus",
                                "category", "code", "subject", "onsetDateTime"],
                 cardinality={"code": "1..1", "subject": "1..1"},
                 bindings={"code": (ICD10_CM_VS, "extensible")},
                 narrative="Diagnoses on the active problem list.",
             )),
            ("MedicationStatement", "IPSMedicationStatement",
             "Current meds",
             _pm(
                 must_support=["status", "medicationCodeableConcept",
                                "subject", "effectivePeriod",
                                "dateAsserted", "dosage"],
                 cardinality={"status": "1..1", "subject": "1..1"},
                 bindings={"medicationCodeableConcept": (RXNORM_VS, "extensible")},
                 narrative="Current medications, RxNorm coded.",
             )),
            ("Immunization", "IPSImmunization",
             "Vaccination history",
             _pm(
                 must_support=["status", "vaccineCode", "patient",
                                "occurrenceDateTime", "primarySource",
                                "lotNumber"],
                 cardinality={"status": "1..1", "vaccineCode": "1..1",
                              "patient": "1..1"},
                 narrative="CVX-coded vaccinations.",
             )),
            ("Observation", "IPSObservationResults",
             "Recent vital signs + labs",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "effectiveDateTime", "valueQuantity"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="Recent vitals + lab results.",
             )),
            ("Procedure", "IPSProcedure",
             "History of procedures",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "performedDateTime", "bodySite"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 narrative="Past procedures — CPT or SNOMED-coded.",
             )),
        ],
        "value_sets": [
            ("IPSAllergyIntoleranceCode", "SNOMED CT subset for allergies"),
            ("IPSConditionCode",          "ICD-10 + SNOMED subset for conditions"),
            ("IPSImmunizationCode",       "CVX subset for vaccinations"),
        ],
        "extensions": [],
    },
    "tumor-board": {
        "name": "Tumor Board Data Architecture",
        "description": "Custom oncology IG for tumor board case presentations. "
                       "Aggregates clinical history, imaging, pathology, "
                       "molecular/genomic findings, treatment timeline, and "
                       "trial eligibility into a single bundle the board can "
                       "review.",
        "fhir_version": "R4",
        "base_ig": "us-core + mcode",
        "tools_cited": ["FHIR Specification", "HAPI FHIR", "Medplum",
                        "US Core Implementation Guide", "Synthea",
                        "pydicom", "DCMTK"],
        "clinical_workflow": (
            "A multi-disciplinary tumor board meets weekly to review "
            "complex cancer cases. For each case, the EHR assembles an "
            "oncology Bundle: patient demographics, primary cancer + "
            "metastases, TNM stage, tumor markers, genomic variants, "
            "imaging studies (PET/CT, MR), pathology, treatment timeline, "
            "and active trial enrollment. The board reviews, deliberates, "
            "and records consensus recommendations back into the EHR."
        ),
        "resources": [
            ("Patient", "OncologyPatient",
             "Cancer patient demographics + cancer-relevant identifiers",
             _pm(
                 must_support=["identifier", "name", "birthDate",
                                "gender", "extension"],
                 cardinality={"identifier": "1..*", "name": "1..*",
                              "birthDate": "1..1", "gender": "1..1"},
                 bindings={
                     "extension:race": (US_CORE_RACE_VS, "extensible"),
                     "extension:ethnicity": (US_CORE_ETHNICITY_VS, "extensible"),
                 },
                 narrative="Cancer patient with mCODE-aligned race + "
                           "ethnicity extensions.",
             )),
            ("Condition", "PrimaryCancerCondition",
             "The primary diagnosis (mCODE PrimaryCancerCondition)",
             _pm(
                 must_support=["clinicalStatus", "verificationStatus",
                                "code", "subject", "onsetDateTime"],
                 cardinality={"clinicalStatus": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 bindings={"code": (ICD10_CM_VS, "extensible")},
                 narrative="The primary cancer diagnosis — coded with "
                           "ICD-O-3 morphology + ICD-10-CM site.",
             )),
            ("Condition", "SecondaryCancerCondition",
             "Metastases (mCODE)",
             _pm(
                 must_support=["clinicalStatus", "verificationStatus",
                                "code", "subject", "bodySite",
                                "onsetDateTime"],
                 cardinality={"code": "1..1", "subject": "1..1"},
                 bindings={"code": (ICD10_CM_VS, "extensible")},
                 narrative="Metastatic sites tied back to the primary.",
             )),
            ("Observation", "TNMStageGroup",
             "TNM staging at diagnosis (mCODE)",
             _pm(
                 must_support=["status", "code", "subject",
                                "effectiveDateTime", "valueCodeableConcept"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="AJCC stage group at diagnosis.",
             )),
            ("Observation", "TumorMarkerObservation",
             "PSA, CA-125, etc. (mCODE)",
             _pm(
                 must_support=["status", "code", "subject",
                                "effectiveDateTime", "valueQuantity"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="Serum tumor markers tracked over treatment course.",
             )),
            ("Observation", "GenomicVariant",
             "VRS-shaped variant call (mCODE GenomicVariant + GA4GH VRS)",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "effectiveDateTime", "valueCodeableConcept",
                                "component"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1", "component": "1..*"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="Pathogenic/likely-pathogenic variant calls. "
                           "Component slice carries gene, transcript, "
                           "HGVS, and clinical significance.",
             )),
            ("Observation", "GenomicRegionStudied",
             "Sequencing scope",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "effectiveDateTime", "component"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 narrative="Which genes/regions were covered by the panel.",
             )),
            ("MedicationRequest", "CancerRelatedMedicationRequest",
             "Targeted + chemo therapies (mCODE)",
             _pm(
                 must_support=["status", "intent", "medicationCodeableConcept",
                                "subject", "authoredOn", "reasonReference"],
                 cardinality={"status": "1..1", "intent": "1..1",
                              "subject": "1..1"},
                 bindings={"medicationCodeableConcept": (RXNORM_VS, "extensible")},
                 narrative="Cancer-directed therapies — chemo, targeted, IO.",
             )),
            ("Procedure", "CancerRelatedSurgicalProcedure",
             "Resection, biopsy",
             _pm(
                 must_support=["status", "code", "subject",
                                "performedDateTime", "bodySite"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 narrative="Cancer-directed surgical procedures.",
             )),
            ("Procedure", "RadiotherapyTreatmentPhase",
             "Radiation course",
             _pm(
                 must_support=["status", "category", "code", "subject",
                                "performedPeriod", "bodySite"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 narrative="One phase of a radiotherapy treatment course.",
             )),
            ("ImagingStudy", "OncologyImagingStudy",
             "Reference to DICOM imaging (PET/CT, MR)",
             _pm(
                 must_support=["status", "subject", "started",
                                "modality", "series"],
                 cardinality={"status": "1..1", "subject": "1..1",
                              "modality": "1..*"},
                 narrative="Cross-sectional imaging tied to the case.",
             )),
            ("DiagnosticReport", "OncologyPathologyReport",
             "Path report with structured findings",
             _pm(
                 must_support=["status", "code", "subject",
                                "effectiveDateTime", "result", "conclusion"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 narrative="Surgical pathology report with structured "
                           "findings + free-text impression.",
             )),
            ("ResearchSubject", "ClinicalTrialSubject",
             "Active trial enrollment(s)",
             _pm(
                 must_support=["status", "study", "individual",
                                "period", "assignedArm", "actualArm"],
                 cardinality={"status": "1..1", "study": "1..1",
                              "individual": "1..1"},
                 narrative="Trial enrollment record(s) for the patient.",
             )),
        ],
        "value_sets": [
            ("PrimaryCancerConditionCode",  "ICD-O-3 + ICD-10-CM cancer codes"),
            ("CancerStagingSystem",         "AJCC + UICC + per-cancer-specific systems"),
            ("OncologyTherapyCode",         "RxNorm subset — oncology agents (kinase inhibitors, immunotherapy, etc.)"),
            ("HGNCGeneSymbol",              "HGNC gene symbols for variant calls"),
            ("ClinVarSignificance",         "Pathogenic/Likely-pathogenic/VUS/Benign"),
        ],
        "extensions": [
            ("biomarker-result",            "Companion-diagnostic test result link"),
            ("trial-eligibility-criteria",  "Match cancer profile to trial criteria"),
        ],
    },
    "prior-auth": {
        "name": "Prior Authorization Support (DaVinci PAS)",
        "description": "DaVinci-aligned IG for prior auth — payer review of "
                       "proposed services using FHIR Coverage, "
                       "ServiceRequest, Claim, ClaimResponse + the X12 278 "
                       "underneath.",
        "fhir_version": "R4",
        "base_ig": "us-core + davinci-pas",
        "tools_cited": ["FHIR Specification", "HAPI FHIR", "pyx12",
                        "Stedi (clearinghouse)"],
        "clinical_workflow": (
            "A provider proposes a service (procedure, DME, behavioral "
            "health visit, advanced imaging). The EHR builds a PAS "
            "Bundle: Patient + Coverage + Practitioner + Organization + "
            "ServiceRequest + Claim. The Claim is sent (over X12 278) to "
            "the payer; the payer's response comes back as a "
            "ClaimResponse with itemized review actions. Approved → "
            "service can proceed; denied → appeal workflow."
        ),
        "resources": [
            ("Patient", "USCorePatient",
             "Beneficiary",
             _pm(
                 must_support=["identifier", "name", "birthDate",
                                "gender", "address", "telecom"],
                 cardinality={"identifier": "1..*", "name": "1..*",
                              "birthDate": "1..1"},
                 narrative="The covered beneficiary.",
             )),
            ("Coverage", "PASCoverage",
             "Insurance coverage",
             _pm(
                 must_support=["status", "beneficiary", "payor",
                                "subscriberId", "type"],
                 cardinality={"status": "1..1", "beneficiary": "1..1",
                              "payor": "1..*"},
                 fixed={"status": "active"},
                 narrative="The beneficiary's active insurance coverage.",
             )),
            ("Practitioner", "USCorePractitioner",
             "Requesting provider",
             _pm(
                 must_support=["identifier", "active", "name",
                                "telecom", "address", "qualification"],
                 cardinality={"identifier": "1..*", "name": "1..*"},
                 narrative="The provider requesting the service. Must "
                           "carry an NPI identifier.",
             )),
            ("Organization", "PASOrganization",
             "Provider organization + payer",
             _pm(
                 must_support=["identifier", "active", "name", "type",
                                "telecom", "address"],
                 cardinality={"identifier": "1..*", "name": "1..1"},
                 narrative="Either the requesting org or the payer.",
             )),
            ("ServiceRequest", "PASServiceRequest",
             "Proposed service",
             _pm(
                 must_support=["status", "intent", "code", "subject",
                                "requester", "performer"],
                 cardinality={"status": "1..1", "intent": "1..1",
                              "code": "1..1", "subject": "1..1"},
                 narrative="The proposed service awaiting auth.",
             )),
            ("Claim", "PASClaim",
             "Prior auth request (mapped to X12 278 transaction)",
             _pm(
                 must_support=["status", "type", "use", "patient",
                                "created", "provider", "priority",
                                "insurance", "diagnosis", "item"],
                 cardinality={"patient": "1..1", "provider": "1..1",
                              "insurance": "1..*", "item": "1..*"},
                 fixed={"use": "preauthorization"},
                 narrative="The prior-auth request — mapped to X12 278.",
             )),
            ("ClaimResponse", "PASClaimResponse",
             "Payer's auth decision",
             _pm(
                 must_support=["status", "type", "use", "patient",
                                "created", "insurer", "request",
                                "outcome", "preAuthRef", "item"],
                 cardinality={"patient": "1..1", "insurer": "1..1",
                              "request": "1..1"},
                 narrative="The payer's response — approved/denied/pended "
                           "with itemized review actions.",
             )),
            ("DocumentReference", "PASClinicalDocReference",
             "Supporting documentation",
             _pm(
                 must_support=["status", "type", "category", "subject",
                                "date", "content"],
                 cardinality={"status": "1..1", "subject": "1..1",
                              "content": "1..*"},
                 narrative="Clinical notes / imaging / labs supporting "
                           "the auth.",
             )),
        ],
        "value_sets": [
            ("PASRequestType",         "Initial / appeal / inquiry"),
            ("PASDecisionStatus",      "Approved / denied / pending / cancelled"),
            ("ServiceRequestCategory", "Procedure / DME / behavioral health"),
        ],
        "extensions": [
            ("review-action-code", "Itemized auth decision per service line"),
        ],
    },
    "adverse-event": {
        "name": "Adverse Event Reporting (Clinical Trials + Pharmacovigilance)",
        "description": "FHIR AdverseEvent + supporting resources for clinical "
                       "trial AE reporting and post-market pharmacovigilance. "
                       "Maps cleanly to ICH E2B(R3) for regulatory submission.",
        "fhir_version": "R4",
        "base_ig": "us-core",
        "tools_cited": ["FHIR Specification", "HAPI FHIR", "Medplum",
                        "Synthea"],
        "clinical_workflow": (
            "During a clinical trial, an investigator observes an "
            "adverse event in a subject. The site EDC builds an "
            "AdverseEvent resource (with severity, causality, outcome) "
            "tied to the ResearchSubject + ResearchStudy + suspect "
            "MedicationStatement. The sponsor's safety system relays "
            "the report to regulators (FDA, EMA) as ICH E2B(R3) "
            "messages."
        ),
        "resources": [
            ("Patient", "USCorePatient",
             "Trial subject (or post-market patient)",
             _pm(
                 must_support=["identifier", "name", "birthDate",
                                "gender", "address", "telecom"],
                 cardinality={"identifier": "1..*", "name": "1..*",
                              "birthDate": "1..1"},
                 narrative="The trial subject (de-identified to a "
                           "pseudonymous ID for regulatory submission).",
             )),
            ("ResearchSubject", "ClinicalTrialSubject",
             "Trial enrollment context",
             _pm(
                 must_support=["status", "study", "individual",
                                "period", "assignedArm", "actualArm"],
                 cardinality={"status": "1..1", "study": "1..1",
                              "individual": "1..1"},
                 narrative="The subject's enrollment in the trial.",
             )),
            ("ResearchStudy", "ClinicalTrial",
             "The trial protocol",
             _pm(
                 must_support=["identifier", "status", "title",
                                "phase", "sponsor"],
                 cardinality={"status": "1..1", "title": "1..1"},
                 narrative="The trial protocol — IND number, phase, "
                           "sponsor.",
             )),
            ("AdverseEvent", "ClinicalTrialAdverseEvent",
             "The AE itself + suspect agent + severity + outcome",
             _pm(
                 must_support=["actuality", "category", "event", "subject",
                                "date", "severity", "outcome",
                                "suspectEntity"],
                 cardinality={"actuality": "1..1", "subject": "1..1",
                              "event": "1..1", "date": "1..1"},
                 fixed={"actuality": "actual"},
                 narrative="The AE itself — MedDRA-coded event, severity, "
                           "causality, outcome, suspect agent.",
             )),
            ("MedicationStatement", "SuspectMedicationStatement",
             "The investigational + concomitant meds",
             _pm(
                 must_support=["status", "medicationCodeableConcept",
                                "subject", "effectivePeriod",
                                "dateAsserted", "dosage"],
                 cardinality={"status": "1..1", "subject": "1..1"},
                 bindings={"medicationCodeableConcept": (RXNORM_VS, "extensible")},
                 narrative="Suspect investigational drug + concomitant meds.",
             )),
            ("Observation", "AdverseEventOutcome",
             "Lab values supporting the AE",
             _pm(
                 must_support=["status", "code", "subject",
                                "effectiveDateTime", "valueQuantity"],
                 cardinality={"status": "1..1", "code": "1..1",
                              "subject": "1..1"},
                 bindings={"code": (LOINC_VS, "extensible")},
                 narrative="Lab values that support the AE narrative "
                           "(e.g., elevated ALT for hepatotoxicity).",
             )),
            ("Practitioner", "ReportingInvestigator",
             "The PI / reporter",
             _pm(
                 must_support=["identifier", "active", "name",
                                "telecom", "address", "qualification"],
                 cardinality={"identifier": "1..*", "name": "1..*"},
                 narrative="The reporting investigator (PI or sub-I).",
             )),
        ],
        "value_sets": [
            ("AdverseEventSeriousness",   "ICH E2B serious-criterion codes (death, hospitalization, etc.)"),
            ("AdverseEventCausality",     "Causality assessment (definitely related / probable / possible / unlikely / unrelated)"),
            ("MedDRACode",                "MedDRA preferred terms for AE coding"),
            ("AdverseEventOutcome",       "Recovered / recovering / fatal / unknown"),
        ],
        "extensions": [
            ("regulatory-report-id", "Tracks the E2B(R3) submission"),
        ],
    },
}


@dataclass
class _Decomposition:
    use_case_key: Optional[str]
    use_case_meta: dict[str, Any]
    citations: list[tuple[int, str, int, str]]  # (concept_id, name, doc_section_id, source_name)
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "ig"


def _normalize_use_case(q: str) -> str:
    s = q.lower().replace("_", "-").replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-+"
    return "".join(c for c in s if c in keep).strip("-")


def _resource_tuple(entry: tuple) -> tuple[str, str, str, ProfileMeta]:
    """Normalize a catalog resource entry to a 4-tuple.

    Older entries were 3-tuples (resource_type, profile_name, purpose).
    The new shape is (resource_type, profile_name, purpose, profile_meta).
    Defaulting the meta to ``{}`` keeps all the renderers tolerant of
    older catalog data if anyone forks the catalog.
    """
    if len(entry) == 4:
        return entry  # type: ignore[return-value]
    if len(entry) == 3:
        rt, pn, purpose = entry
        return (rt, pn, purpose, {})
    raise ValueError(f"resource entry must be 3- or 4-tuple, got {entry!r}")


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------

class FhirIgDecomposer:
    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        **_: Any,
    ) -> _Decomposition:
        norm = _normalize_use_case(query)
        # Try direct + substring matching
        matched: Optional[str] = None
        for key in USE_CASE_CATALOG:
            if key == norm or key in norm or norm in key:
                matched = key
                break
        if matched is None:
            # Token-overlap fallback
            tokens = set(norm.split("-"))
            best, best_score = None, 0
            for key in USE_CASE_CATALOG:
                key_tokens = set(key.split("-"))
                score = len(tokens & key_tokens)
                if score > best_score:
                    best, best_score = key, score
            if best_score >= 1:
                matched = best

        if matched is None:
            return _Decomposition(
                use_case_key=None, use_case_meta={},
                citations=[],
                notes=[f"unrecognized use case: {query!r}",
                       f"supported: {sorted(USE_CASE_CATALOG)}"],
            )

        meta = USE_CASE_CATALOG[matched]
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
            notes.append("no concept citations from cited tools — "
                         "package ships from built-in use-case spec only")
        return _Decomposition(
            use_case_key=matched, use_case_meta=meta,
            citations=citations, notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers — IG file templates
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    meta = d.use_case_meta
    lines = [
        f"# FHIR IG — {meta.get('name', d.use_case_key)}",
        "",
        meta.get("description", ""),
        "",
        f"- **FHIR version:** {meta.get('fhir_version', 'R4')}",
        f"- **Base IG(s):** {meta.get('base_ig', '(none)')}",
        f"- **Resources constrained:** {len(meta.get('resources', []))}",
        f"- **ValueSets defined:** {len(meta.get('value_sets', []))}",
        f"- **Extensions defined:** {len(meta.get('extensions', []))}",
        "",
        "## What's in this package",
        "",
        "```",
        f"fhir-ig-{d.use_case_key}/",
        "├── README.md            (this file)",
        "├── sushi-config.yaml    (SUSHI build config)",
        "├── ig.ini               (IG Publisher config)",
        "└── input/",
        "    ├── pagecontent/     (narrative pages — index.md + background.md)",
        "    ├── profiles/        (FSH StructureDefinitions with cardinality + MS + bindings)",
        "    ├── valuesets/       (FSH ValueSet skeletons)",
        "    ├── extensions/      (FSH Extension skeletons)",
        "    └── examples/        (JSON example resources — built via "
        "healthcare_libs.fhir, structurally valid)",
        "```",
        "",
        "## How to build",
        "",
        "```bash",
        "# Install SUSHI (FSH compiler) + IG Publisher",
        "npm install -g fsh-sushi",
        "# Download IG Publisher: https://github.com/HL7/fhir-ig-publisher/releases",
        "",
        "# Compile FSH → StructureDefinitions",
        "sushi build",
        "",
        "# Build the IG website (downloads ~1GB of FHIR-core artifacts on first run)",
        "java -jar publisher.jar -ig ig.ini",
        "```",
        "",
        "## What this scaffold gives you",
        "",
        "- **Real FSH constraints** — every profile carries explicit "
        "cardinality (`* field min..max`), must-support flags "
        "(`* field MS`), and value-set bindings "
        "(`* field from <ValueSet> (strength)`). No `// TODO` stubs.",
        "- **Structurally valid example resources** — examples are built "
        "via ``healthcare_libs.fhir.build_*`` and round-tripped through "
        "Pydantic, so they pass FHIR datatype validation. They populate "
        "the must-support fields with realistic synthetic data.",
        "- **Sushi-buildable** — `sushi build` will compile the FSH to "
        "StructureDefinitions; `java -jar publisher.jar` will then build "
        "the full IG website.",
        "",
        "## Known limitations",
        "",
        "- **Profile-conformance validation requires the IG Publisher.** "
        "These examples pass FHIR base validation; verifying that they "
        "also conform to the *profiles* defined here requires running "
        "`sushi build && java -jar publisher.jar -ig ig.ini` and "
        "inspecting the QA report.",
        "- **Code system bindings** assume the standard ones (LOINC, "
        "SNOMED CT, ICD-10, RxNorm). If you bind to local code systems, "
        "you'll need additional ConceptMaps.",
        "- **For oncology IGs** (e.g., tumor-board), align with mCODE "
        "rather than reinventing — the catalog entry already inherits "
        "from mCODE where appropriate.",
        "",
    ]
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for cid, cname, _ds, src in d.citations[:15]:
            lines.append(f"- **{cname}** — {src}")
        lines.append("")
    return "\n".join(lines)


def _render_sushi_config(d: _Decomposition) -> str:
    meta = d.use_case_meta
    return f"""id: example.{d.use_case_key}
canonical: http://example.org/fhir/{d.use_case_key}
name: {_camel(meta.get('name', 'IG'))}
title: "{meta.get('name', '')}"
description: |
  {meta.get('description', '').replace(chr(10), ' ')}
status: draft
version: 0.1.0
fhirVersion: 4.0.1
copyrightYear: 2026+
releaseLabel: ci-build
publisher:
  name: Your Organization
  url: https://example.org
dependencies:
  hl7.fhir.us.core: 6.1.0
parameters:
  show-inherited-invariants: false

# Pages
pages:
  index.md:
    title: Home
  background.md:
    title: Background

# Menu
menu:
  Home: index.html
  Background: background.html
  Profiles: artifacts.html#structures-resource-profiles
  Examples: artifacts.html#example-instances
"""


def _render_ig_ini(d: _Decomposition) -> str:
    return f"""[IG]
ig = input/ImplementationGuide-example.{d.use_case_key}.json
template = fhir.base.template#current
"""


def _render_index_page(d: _Decomposition) -> str:
    meta = d.use_case_meta
    lines = [
        f"# {meta.get('name', d.use_case_key)}",
        "",
        meta.get("description", ""),
        "",
    ]
    workflow = meta.get("clinical_workflow")
    if workflow:
        lines.extend(["## Clinical workflow", "", workflow, ""])
    lines.extend(["## Scope", "", "Resources in scope:", ""])
    for entry in meta.get("resources", []):
        resource_type, profile_name, purpose, _pmeta = _resource_tuple(entry)
        lines.append(
            f"- **{resource_type}** profiled as `{profile_name}` — {purpose}"
        )
    lines.extend(["", "## Audience", ""])
    lines.append("- Implementers integrating this use case with EHR / "
                 "trial / clearinghouse systems")
    lines.append("- Conformance reviewers (auditors, certification testers)")
    return "\n".join(lines)


def _render_background_page(d: _Decomposition) -> str:
    meta = d.use_case_meta
    lines = [
        f"# Background — {meta.get('name', d.use_case_key)}",
        "",
        "## Why this IG exists",
        "",
        meta.get("description", ""),
        "",
    ]
    workflow = meta.get("clinical_workflow")
    if workflow:
        lines.extend(["## End-to-end clinical workflow", "", workflow, ""])
    lines.extend(["## Resources constrained + what each contributes", ""])
    for entry in meta.get("resources", []):
        resource_type, profile_name, purpose, pmeta = _resource_tuple(entry)
        narrative = pmeta.get("narrative", purpose)
        lines.append(
            f"### {profile_name} (constrains `{resource_type}`)"
        )
        lines.append("")
        lines.append(narrative)
        ms = pmeta.get("must_support", [])
        if ms:
            lines.append("")
            lines.append(
                f"**Must-support fields:** {', '.join('`' + f + '`' for f in ms)}"
            )
        bindings = pmeta.get("bindings", {})
        if bindings:
            lines.append("")
            lines.append("**Value-set bindings:**")
            for f, (vs, strength) in bindings.items():
                lines.append(f"- `{f}` → `{vs}` ({strength})")
        lines.append("")
    lines.extend(["## Value sets bound", ""])
    for vs_name, vs_purpose in meta.get("value_sets", []):
        lines.append(f"- **{vs_name}** — {vs_purpose}")
    if meta.get("extensions"):
        lines.extend(["", "## Extensions defined", ""])
        for ext_name, ext_purpose in meta["extensions"]:
            lines.append(f"- **{ext_name}** — {ext_purpose}")
    return "\n".join(lines)


def _render_profile_fsh(
    resource_type: str,
    profile_name: str,
    purpose: str,
    base_ig: str,
    profile_meta: Optional[ProfileMeta] = None,
) -> str:
    """Generate a real FSH StructureDefinition with MS + cardinality + bindings.

    Falls back to a TODO-style skeleton only when the catalog entry is
    missing per-field metadata entirely. The order of constraints in
    the output is deterministic: must-support first, then cardinality,
    then bindings, then fixed values, then any free-form slicing.
    """
    parent = _parent_for_profile(resource_type, base_ig)
    pid = profile_name.lower().replace("_", "-")
    pmeta = profile_meta or {}

    header = [
        "// Auto-generated FSH from the FHIR IG Scaffold generator.",
        f"// Profile constraints derived from the use-case catalog entry "
        f"for {profile_name}.",
        "",
        f"Profile:        {profile_name}",
        f"Parent:         {parent}",
        f"Id:             {pid}",
        f"Title:          \"{profile_name}\"",
        f"Description:    \"{purpose}\"",
        "",
    ]

    body: list[str] = []

    must_support: list[str] = pmeta.get("must_support", []) or []
    cardinality: dict[str, str] = pmeta.get("cardinality", {}) or {}
    bindings: dict[str, tuple[str, str]] = pmeta.get("bindings", {}) or {}
    fixed: dict[str, str] = pmeta.get("fixed", {}) or {}
    slicing: list[str] = pmeta.get("slicing", []) or []

    if must_support:
        body.append("// Must-support flags")
        for f in must_support:
            body.append(f"* {f} MS")
        body.append("")

    if cardinality:
        body.append("// Cardinality constraints")
        for f, card in cardinality.items():
            body.append(f"* {f} {card}")
        body.append("")

    if bindings:
        body.append("// Value-set bindings")
        for f, (vs, strength) in bindings.items():
            body.append(f"* {f} from {vs} ({strength})")
        body.append("")

    if fixed:
        body.append("// Fixed values")
        for f, val in fixed.items():
            # FSH literal: bools/ints unquoted, strings quoted with =
            if isinstance(val, bool):
                body.append(f"* {f} = {str(val).lower()}")
            elif isinstance(val, (int, float)):
                body.append(f"* {f} = {val}")
            else:
                body.append(f"* {f} = \"{val}\"")
        body.append("")

    if slicing:
        body.append("// Slicing")
        body.extend(slicing)
        body.append("")

    if not body:
        body = [
            "// No catalog metadata supplied — fill in cardinality + "
            "binding rules below.",
            "// * identifier MS",
            "// * identifier 1..*",
            "// * code 1..1",
        ]

    return "\n".join(header + body)


def _parent_for_profile(resource_type: str, base_ig: str) -> str:
    """Heuristic: use the US Core profile if the base IG lists it; otherwise FHIR base."""
    base_ig = (base_ig or "").lower()
    if "us-core" in base_ig and resource_type in {
        "Patient", "Practitioner", "Organization", "Encounter", "Observation",
        "Condition", "MedicationRequest", "MedicationStatement", "Immunization",
        "AllergyIntolerance", "DiagnosticReport", "DocumentReference",
        "Procedure", "Coverage", "Goal",
    }:
        return f"USCore{resource_type}"
    return resource_type


def _render_valueset_fsh(name: str, purpose: str) -> str:
    return f"""// Auto-generated FSH ValueSet skeleton.
//
// Purpose: {purpose}

ValueSet:       {name}
Id:             {name.lower().replace('_', '-')}
Title:          "{name}"
Description:    "{purpose}"

// TODO: include codes from the appropriate code system(s).
// Examples:
// * include codes from system http://loinc.org
// * include http://snomed.info/sct#12345 "Concept name"
// * include codes from valueset http://example.org/fhir/ValueSet/parent
"""


def _render_extension_fsh(name: str, purpose: str) -> str:
    snake = name.replace("-", "_")
    return f"""// Auto-generated FSH Extension skeleton.
//
// Purpose: {purpose}

Extension:      {snake}
Id:             {name}
Title:          "{snake}"
Description:    "{purpose}"

* value[x] only string
// TODO: replace string with the right value type, or add nested sub-extensions
"""


# ---------------------------------------------------------------------------
# Example resource builders
# ---------------------------------------------------------------------------
#
# For each (use_case_key, profile_name) pair we'd like to ship a
# realistic example. The builders below return JSON-serializable dicts
# that all pass ``healthcare_libs.fhir.validate()`` cleanly. Where a
# resource type has a dedicated ``build_*`` in healthcare_libs.fhir, we
# use it; otherwise we hand-build a minimal-but-realistic resource that
# still validates structurally.

# Stable example IDs so the same input always produces the same example
# (deterministic, important for catalog regeneration + git diffing).
_EX_PATIENT_ID = "example-patient"
_EX_PRACTITIONER_ID = "example-practitioner"
_EX_ORGANIZATION_ID = "example-organization"
_EX_PAYER_ID = "example-payer"
_EX_COVERAGE_ID = "example-coverage"
_EX_STUDY_ID = "example-study"
_EX_SUBJECT_ID = "example-research-subject"

_PATIENT_REF = f"Patient/{_EX_PATIENT_ID}"
_PRACTITIONER_REF = f"Practitioner/{_EX_PRACTITIONER_ID}"
_PAYER_REF = f"Organization/{_EX_PAYER_ID}"
_COVERAGE_REF = f"Coverage/{_EX_COVERAGE_ID}"
_STUDY_REF = f"ResearchStudy/{_EX_STUDY_ID}"
_SUBJECT_REF = f"ResearchSubject/{_EX_SUBJECT_ID}"


def _profile_url(use_case_key: str, profile_name: str) -> str:
    pid = profile_name.lower().replace("_", "-")
    return f"http://example.org/fhir/StructureDefinition/{pid}"


def _stamp_profile(resource: dict, use_case_key: str, profile_name: str) -> dict:
    """Add ``meta.profile`` to a resource dict so it claims conformance."""
    meta = resource.setdefault("meta", {})
    profiles = list(meta.get("profile", []))
    url = _profile_url(use_case_key, profile_name)
    if url not in profiles:
        profiles.append(url)
    meta["profile"] = profiles
    return resource


def _ex_oncology_patient(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_patient(
        family="Reyes",
        given=["Maria", "Elena"],
        birth_date="1962-04-17",
        gender="female",
        mrn="ONC-00045821",
        patient_id=_EX_PATIENT_ID,
        address_line="500 Cancer Ctr Pkwy",
        address_city="Boston",
        address_state="MA",
        address_postal_code="02114",
        telecom_phone="617-555-0142",
    )
    res["extension"] = [
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
            "extension": [
                {"url": "ombCategory", "valueCoding": {
                    "system": "urn:oid:2.16.840.1.113883.6.238",
                    "code": "2106-3", "display": "White"}},
                {"url": "text", "valueString": "White"},
            ],
        },
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
            "extension": [
                {"url": "ombCategory", "valueCoding": {
                    "system": "urn:oid:2.16.840.1.113883.6.238",
                    "code": "2135-2", "display": "Hispanic or Latino"}},
                {"url": "text", "valueString": "Hispanic or Latino"},
            ],
        },
    ]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_patient(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_patient(
        family="Doe",
        given=["John", "Q"],
        birth_date="1975-09-22",
        gender="male",
        mrn="MRN-1029384",
        patient_id=_EX_PATIENT_ID,
        address_line="123 Main St",
        address_city="Cambridge",
        address_state="MA",
        address_postal_code="02139",
        telecom_phone="617-555-0188",
        telecom_email="john.doe@example.org",
    )
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_patient(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_patient(
        family="Müller",
        given=["Anna", "Sofia"],
        birth_date="1958-11-03",
        gender="female",
        mrn="IPS-7766554",
        patient_id=_EX_PATIENT_ID,
        address_line="Hauptstrasse 24",
        address_city="Bern",
        address_state="BE",
        address_postal_code="3000",
        address_country="CH",
        telecom_phone="+41-31-555-0119",
    )
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_practitioner(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Practitioner",
        "id": _EX_PRACTITIONER_ID,
        "identifier": [{
            "system": hfhir.SYSTEM_NPI,
            "value": "1234567893",
        }],
        "active": True,
        "name": [hfhir.human_name(family="Chen", given=["Wei"],
                                   prefix="Dr", suffix="MD")],
        "telecom": [{"system": "phone", "value": "617-555-0177", "use": "work"}],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_reporting_investigator(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Practitioner",
        "id": _EX_PRACTITIONER_ID,
        "identifier": [{
            "system": hfhir.SYSTEM_NPI,
            "value": "1356789012",
        }],
        "active": True,
        "name": [hfhir.human_name(family="Patel", given=["Neha"],
                                   prefix="Dr", suffix="MD, PhD")],
        "qualification": [{
            "code": hfhir.codeable_concept(
                "http://terminology.hl7.org/CodeSystem/v2-0360",
                "MD", "Doctor of Medicine",
            ),
        }],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_organization(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Organization",
        "id": _EX_PAYER_ID,
        "identifier": [{
            "system": "http://hl7.org/fhir/sid/us-npi",
            "value": "9876543210",
        }],
        "active": True,
        "type": [hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/organization-type",
            "ins", "Insurance Company",
        )],
        "name": "Acme Health Plan",
        "telecom": [{"system": "phone", "value": "1-800-555-0199"}],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_coverage(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Coverage",
        "id": _EX_COVERAGE_ID,
        "status": "active",
        "type": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "EHCPOL", "extended healthcare",
        ),
        "subscriberId": "SUB-998877",
        "beneficiary": {"reference": _PATIENT_REF},
        "payor": [{"reference": _PAYER_REF}],
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_research_subject_group(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Group",
        "id": "example-cohort",
        "type": "person",
        "actual": True,
        "name": "Phase II IO+TKI Cohort",
        "quantity": 1,
        "member": [{"entity": {"reference": _PATIENT_REF}}],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_lab_obs(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="1751-7",
        code_display="Albumin [Mass/volume] in Serum or Plasma",
        value=4.2,
        unit_ucum="g/dL",
        effective_datetime="2026-04-15T08:30:00Z",
        reference_range_low=3.5,
        reference_range_high=5.0,
        observation_id="example-lab-obs",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "laboratory", "Laboratory",
    )]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_observation(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="8480-6",
        code_display="Systolic blood pressure",
        value=132.0,
        unit_ucum="mm[Hg]",
        effective_datetime="2026-04-22T09:15:00Z",
        observation_id="example-vitals-obs",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "vital-signs", "Vital Signs",
    )]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_tnm_stage(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="21908-9",
        code_display="Stage group.clinical Cancer",
        value={"system": "http://cancerstaging.org", "code": "3A",
                "display": "Stage IIIA"},
        effective_datetime="2026-02-20T00:00:00Z",
        observation_id="example-tnm-stage",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "exam", "Exam",
    )]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_tumor_marker(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="2857-1",
        code_display="Prostate specific Ag [Mass/volume] in Serum or Plasma",
        value=12.4,
        unit_ucum="ng/mL",
        effective_datetime="2026-04-10T07:45:00Z",
        reference_range_low=0.0,
        reference_range_high=4.0,
        observation_id="example-tumor-marker",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "laboratory", "Laboratory",
    )]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_genomic_variant(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="69548-6",
        code_display="Genetic variant assessment",
        value={"system": "http://loinc.org", "code": "LA9633-4",
                "display": "Present"},
        effective_datetime="2026-03-05T00:00:00Z",
        observation_id="example-genomic-variant",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "laboratory", "Laboratory",
    )]
    res["component"] = [
        {
            "code": hfhir.codeable_concept(hfhir.LOINC, "48018-6",
                                           "Gene studied"),
            "valueCodeableConcept": hfhir.codeable_concept(
                "http://www.genenames.org/geneId", "HGNC:6973", "MET",
            ),
        },
        {
            "code": hfhir.codeable_concept(hfhir.LOINC, "48004-6",
                                           "DNA change (c.HGVS)"),
            "valueCodeableConcept": hfhir.codeable_concept(
                "http://varnomen.hgvs.org",
                "NM_000245.3:c.3082+1G>T",
                "MET exon 14 skipping splice variant",
            ),
        },
        {
            "code": hfhir.codeable_concept(hfhir.LOINC, "53037-8",
                                           "Genetic variation clinical significance"),
            "valueCodeableConcept": hfhir.codeable_concept(
                hfhir.LOINC, "LA6668-3", "Pathogenic",
            ),
        },
    ]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_genomic_region(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="53041-0",
        code_display="DNA region of interest panel",
        value={"system": "http://loinc.org", "code": "LA9633-4",
                "display": "Present"},
        effective_datetime="2026-03-05T00:00:00Z",
        observation_id="example-genomic-region",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "laboratory", "Laboratory",
    )]
    res["component"] = [
        {
            "code": hfhir.codeable_concept(hfhir.LOINC, "48018-6",
                                           "Gene studied"),
            "valueCodeableConcept": hfhir.codeable_concept(
                "http://www.genenames.org/geneId", "HGNC:6973", "MET"),
        },
    ]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ae_outcome_observation(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_observation(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="1742-6",
        code_display="Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma",
        value=212.0,
        unit_ucum="U/L",
        effective_datetime="2026-03-18T11:00:00Z",
        reference_range_low=7.0,
        reference_range_high=56.0,
        observation_id="example-ae-outcome-lab",
    )
    res["category"] = [hfhir.codeable_concept(
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "laboratory", "Laboratory",
    )]
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_condition(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Condition",
        "id": "example-condition",
        "clinicalStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "active", "Active",
        ),
        "verificationStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "confirmed", "Confirmed",
        ),
        "category": [hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-category",
            "problem-list-item", "Problem List Item",
        )],
        "code": hfhir.codeable_concept(
            hfhir.ICD10_CM, "E11.9", "Type 2 diabetes mellitus without complications",
        ),
        "subject": {"reference": _PATIENT_REF},
        "onsetDateTime": "2018-06-12",
        "recordedDate": "2018-06-15",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_condition(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Condition",
        "id": "example-ips-condition",
        "clinicalStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "active", "Active",
        ),
        "verificationStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "confirmed", "Confirmed",
        ),
        "code": hfhir.codeable_concept(
            hfhir.SNOMED, "38341003", "Hypertensive disorder",
        ),
        "subject": {"reference": _PATIENT_REF},
        "onsetDateTime": "2015-01-20",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_primary_cancer(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Condition",
        "id": "example-primary-cancer",
        "clinicalStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "active", "Active",
        ),
        "verificationStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "confirmed", "Confirmed",
        ),
        "category": [hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-category",
            "problem-list-item",
        )],
        "code": hfhir.codeable_concept(
            hfhir.ICD10_CM, "C34.11", "Malignant neoplasm of upper lobe, right bronchus or lung",
        ),
        "subject": {"reference": _PATIENT_REF},
        "onsetDateTime": "2026-02-01",
        "recordedDate": "2026-02-12",
        "bodySite": [hfhir.codeable_concept(
            hfhir.SNOMED, "31294006", "Right upper lobe of lung",
        )],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_secondary_cancer(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Condition",
        "id": "example-secondary-cancer",
        "clinicalStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "active",
        ),
        "verificationStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "confirmed",
        ),
        "code": hfhir.codeable_concept(
            hfhir.ICD10_CM, "C79.31", "Secondary malignant neoplasm of brain",
        ),
        "subject": {"reference": _PATIENT_REF},
        "onsetDateTime": "2026-04-08",
        "bodySite": [hfhir.codeable_concept(
            hfhir.SNOMED, "12738006", "Brain structure",
        )],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_med_request(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "MedicationRequest",
        "id": "example-med-request",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": hfhir.codeable_concept(
            hfhir.RXNORM, "860975", "Metformin hydrochloride 500 MG Oral Tablet",
        ),
        "subject": {"reference": _PATIENT_REF},
        "authoredOn": "2026-04-01",
        "requester": {"reference": _PRACTITIONER_REF},
        "dosageInstruction": [{
            "text": "Take one tablet by mouth twice daily with meals",
        }],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_cancer_med_request(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "MedicationRequest",
        "id": "example-cancer-med-request",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": hfhir.codeable_concept(
            hfhir.RXNORM, "1373447", "pembrolizumab 25 MG/ML Injection",
        ),
        "subject": {"reference": _PATIENT_REF},
        "authoredOn": "2026-03-01",
        "requester": {"reference": _PRACTITIONER_REF},
        "dosageInstruction": [{
            "text": "200 mg IV every 3 weeks",
        }],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_medication_statement(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "MedicationStatement",
        "id": "example-medication-statement",
        "status": "active",
        "medicationCodeableConcept": hfhir.codeable_concept(
            hfhir.RXNORM, "314076", "lisinopril 10 MG Oral Tablet",
        ),
        "subject": {"reference": _PATIENT_REF},
        "effectivePeriod": {"start": "2024-05-01"},
        "dateAsserted": "2026-04-22",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_suspect_medication_statement(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "MedicationStatement",
        "id": "example-suspect-medication",
        "status": "active",
        "medicationCodeableConcept": hfhir.codeable_concept(
            hfhir.RXNORM, "1546356", "Investigational compound RM-018",
        ),
        "subject": {"reference": _PATIENT_REF},
        "effectivePeriod": {"start": "2026-02-15"},
        "dateAsserted": "2026-03-18",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_us_core_doc_ref(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "DocumentReference",
        "id": "example-doc-ref",
        "status": "current",
        "type": hfhir.codeable_concept(
            hfhir.LOINC, "11506-3", "Progress note",
        ),
        "category": [hfhir.codeable_concept(
            "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
            "clinical-note", "Clinical Note",
        )],
        "subject": {"reference": _PATIENT_REF},
        "date": "2026-04-22T10:00:00Z",
        "content": [{
            "attachment": {
                "contentType": "text/plain",
                "data": "UGF0aWVudCBkb2luZyB3ZWxsLg==",  # "Patient doing well."
                "title": "Progress Note",
            },
        }],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_clinical_doc_ref(use_case_key: str, profile_name: str) -> dict:
    res = _ex_us_core_doc_ref(use_case_key, profile_name)
    res["id"] = "example-pas-doc-ref"
    res["type"] = hfhir.codeable_concept(
        hfhir.LOINC, "57133-1", "Referral note",
    )
    return res


def _ex_ips_composition(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Composition",
        "id": "example-composition",
        "status": "final",
        "type": hfhir.codeable_concept(
            hfhir.LOINC, "60591-5", "Patient summary Document",
        ),
        "subject": {"reference": _PATIENT_REF},
        "date": "2026-04-22T10:00:00Z",
        "author": [{"reference": _PRACTITIONER_REF}],
        "title": "International Patient Summary",
        "section": [
            {
                "title": "Allergies and Intolerances",
                "code": hfhir.codeable_concept(
                    hfhir.LOINC, "48765-2", "Allergies and adverse reactions Document",
                ),
                "text": {
                    "status": "generated",
                    "div": "<div xmlns=\"http://www.w3.org/1999/xhtml\">"
                            "<p>See AllergyIntolerance entries.</p></div>",
                },
            },
            {
                "title": "Active Problems",
                "code": hfhir.codeable_concept(
                    hfhir.LOINC, "11450-4", "Problem list - Reported",
                ),
                "text": {
                    "status": "generated",
                    "div": "<div xmlns=\"http://www.w3.org/1999/xhtml\">"
                            "<p>See Condition entries.</p></div>",
                },
            },
            {
                "title": "Medication Summary",
                "code": hfhir.codeable_concept(
                    hfhir.LOINC, "10160-0", "History of Medication use Narrative",
                ),
                "text": {
                    "status": "generated",
                    "div": "<div xmlns=\"http://www.w3.org/1999/xhtml\">"
                            "<p>See MedicationStatement entries.</p></div>",
                },
            },
        ],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_allergy(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "AllergyIntolerance",
        "id": "example-allergy",
        "clinicalStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
            "active",
        ),
        "verificationStatus": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
            "confirmed",
        ),
        "type": "allergy",
        "category": ["medication"],
        "criticality": "high",
        "code": hfhir.codeable_concept(
            hfhir.SNOMED, "294505008", "Allergy to amoxicillin",
        ),
        "patient": {"reference": _PATIENT_REF},
        "recordedDate": "2020-09-12",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_immunization(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Immunization",
        "id": "example-immunization",
        "status": "completed",
        "vaccineCode": hfhir.codeable_concept(
            "http://hl7.org/fhir/sid/cvx", "207", "COVID-19 mRNA vaccine",
        ),
        "patient": {"reference": _PATIENT_REF},
        "occurrenceDateTime": "2026-01-15T09:00:00Z",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_ips_procedure(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Procedure",
        "id": "example-procedure",
        "status": "completed",
        "code": hfhir.codeable_concept(
            hfhir.SNOMED, "80146002", "Excision of appendix",
        ),
        "subject": {"reference": _PATIENT_REF},
        "performedDateTime": "2010-08-19T14:00:00Z",
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_cancer_surgical_procedure(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Procedure",
        "id": "example-cancer-surgery",
        "status": "completed",
        "category": hfhir.codeable_concept(
            hfhir.SNOMED, "387713003", "Surgical procedure",
        ),
        "code": hfhir.codeable_concept(
            hfhir.SNOMED, "359615001", "Partial lobectomy of lung",
        ),
        "subject": {"reference": _PATIENT_REF},
        "performedDateTime": "2026-02-25T08:00:00Z",
        "bodySite": [hfhir.codeable_concept(
            hfhir.SNOMED, "31294006", "Right upper lobe of lung",
        )],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_radiotherapy_phase(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "Procedure",
        "id": "example-radiotherapy",
        "status": "completed",
        "category": hfhir.codeable_concept(
            hfhir.SNOMED, "108290001", "Radiation oncology AND/OR radiotherapy",
        ),
        "code": hfhir.codeable_concept(
            hfhir.SNOMED, "152198000", "Brachytherapy",
        ),
        "subject": {"reference": _PATIENT_REF},
        "performedPeriod": {"start": "2026-03-10", "end": "2026-04-04"},
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_oncology_imaging(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_imaging_study(
        patient_ref=_EX_PATIENT_ID,
        study_uid="1.2.840.113619.2.55.3.4271125756.123.1742583153.456",
        modality_code="CT",
        started="2026-04-01T13:30:00Z",
        description="PET/CT staging study",
        study_id="example-imaging-study",
        series=[
            {
                "uid": "1.2.840.113619.2.55.3.4271125756.123.1742583153.456.1",
                "number": 1,
                "modality_code": "CT",
                "description": "Axial CT chest/abdomen/pelvis with contrast",
                "instances": [
                    {"uid": "1.2.840.113619.2.55.3.4271125756.123.1742583153.456.1.1",
                     "sop_class": "1.2.840.10008.5.1.4.1.1.2", "number": 1},
                    {"uid": "1.2.840.113619.2.55.3.4271125756.123.1742583153.456.1.2",
                     "sop_class": "1.2.840.10008.5.1.4.1.1.2", "number": 2},
                ],
            },
            {
                "uid": "1.2.840.113619.2.55.3.4271125756.123.1742583153.456.2",
                "number": 2,
                "modality_code": "PT",
                "description": "Whole-body PET (FDG)",
                "instances": [
                    {"uid": "1.2.840.113619.2.55.3.4271125756.123.1742583153.456.2.1",
                     "sop_class": "1.2.840.10008.5.1.4.1.1.128", "number": 1},
                ],
            },
        ],
    )
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_oncology_path_report(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_diagnostic_report(
        patient_ref=_EX_PATIENT_ID,
        code_loinc="60568-3",
        code_display="Pathology Synoptic report",
        observations=["example-tumor-marker", "example-genomic-variant"],
        status="final",
        issued="2026-02-22T16:00:00Z",
        effective_datetime="2026-02-19T10:00:00Z",
        report_id="example-path-report",
        conclusion="Invasive adenocarcinoma, right upper lobe of lung. "
                    "MET exon 14 skipping variant identified, supporting "
                    "targeted therapy with capmatinib or tepotinib.",
    )
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_research_subject(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "ResearchSubject",
        "id": _EX_SUBJECT_ID,
        "status": "on-study",
        "study": {"reference": _STUDY_REF},
        "individual": {"reference": _PATIENT_REF},
        "period": {"start": "2026-03-01"},
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_clinical_trial(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "ResearchStudy",
        "id": _EX_STUDY_ID,
        "identifier": [{
            "system": "http://clinicaltrials.gov",
            "value": "NCT05123456",
        }],
        "status": "active",
        "title": "A Phase II Study of Investigational RM-018 in Solid Tumors",
        "phase": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/research-study-phase",
            "phase-2", "Phase 2",
        ),
        "primaryPurposeType": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/research-study-prim-purp-type",
            "treatment", "Treatment",
        ),
        "sponsor": {"reference": "Organization/example-sponsor"},
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_service_request(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "ServiceRequest",
        "id": "example-service-request",
        "status": "active",
        "intent": "order",
        "category": [hfhir.codeable_concept(
            hfhir.SNOMED, "387713003", "Surgical procedure",
        )],
        "code": hfhir.codeable_concept(
            hfhir.CPT, "27447",
            "Arthroplasty, knee, condyle and plateau; medial AND lateral "
            "compartments with or without patella resurfacing",
        ),
        "subject": {"reference": _PATIENT_REF},
        "authoredOn": "2026-04-15T10:00:00Z",
        "requester": {"reference": _PRACTITIONER_REF},
        "performer": [{"reference": _PRACTITIONER_REF}],
    }
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_claim(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_claim(
        patient_ref=_EX_PATIENT_ID,
        provider_ref=_EX_PRACTITIONER_ID,
        total_amount=15000.0,
        diagnosis_codes=["M17.11"],
        service_lines=[{
            "cpt": "27447",
            "display": "Total knee arthroplasty",
            "charge": 15000.0,
            "unit_count": 1,
            "service_date": "2026-05-15",
            "diagnosis_seq": [1],
        }],
        claim_type_code="institutional",
        use="preauthorization",
        coverage_ref=_COVERAGE_REF,
        insurer_ref=_EX_PAYER_ID,
        identifier_value="PAS-REQ-202604-0001",
        claim_id="example-pas-claim",
    )
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_pas_claim_response(use_case_key: str, profile_name: str) -> dict:
    res = hfhir.build_claim_response(
        claim_ref="example-pas-claim",
        patient_ref=_EX_PATIENT_ID,
        insurer_ref=_EX_PAYER_ID,
        total_paid=0.0,  # auth, not payment
        outcome="complete",
        claim_type_code="institutional",
        use="preauthorization",
        identifier_value="PAS-RESP-202604-0001",
        response_id="example-pas-claim-response",
        disposition="Authorization approved for service line 1.",
        adjudications=[{
            "sequence": 1,
            "adjudication": [
                {"category": "submitted", "amount": 15000.0},
                {"category": "benefit", "amount": 12000.0},
            ],
        }],
    )
    res["preAuthRef"] = "PA-2026-04-AUTH-0001"
    return _stamp_profile(res, use_case_key, profile_name)


def _ex_clinical_trial_adverse_event(use_case_key: str, profile_name: str) -> dict:
    res = {
        "resourceType": "AdverseEvent",
        "id": "example-adverse-event",
        "actuality": "actual",
        "category": [hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/adverse-event-category",
            "product-use-error", "Product Use Error",
        )],
        "event": hfhir.codeable_concept(
            "http://terminology.hl7.org/MedDRA",
            "10019851", "Hepatocellular injury",
        ),
        "subject": {"reference": _PATIENT_REF},
        "date": "2026-03-18T11:00:00Z",
        "severity": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/adverse-event-severity",
            "moderate", "Moderate",
        ),
        "outcome": hfhir.codeable_concept(
            "http://terminology.hl7.org/CodeSystem/adverse-event-outcome",
            "resolved", "Resolved",
        ),
        "suspectEntity": [{
            "instance": {"reference": "MedicationStatement/example-suspect-medication"},
        }],
    }
    return _stamp_profile(res, use_case_key, profile_name)


# Mapping (use_case_key, profile_name) → builder. Builders take
# (use_case_key, profile_name) and return a structurally valid resource
# dict with ``meta.profile`` already stamped.
_EXAMPLE_BUILDERS: dict[tuple[str, str], Callable[[str, str], dict]] = {
    # bulk-data-export
    ("bulk-data-export", "USCorePatient"): _ex_us_core_patient,
    ("bulk-data-export", "ResearchSubjectGroup"): _ex_research_subject_group,
    ("bulk-data-export", "USCoreLabResultObservation"): _ex_us_core_lab_obs,
    ("bulk-data-export", "USCoreCondition"): _ex_us_core_condition,
    ("bulk-data-export", "USCoreMedicationRequest"): _ex_us_core_med_request,
    ("bulk-data-export", "USCoreDocumentReference"): _ex_us_core_doc_ref,
    # patient-summary
    ("patient-summary", "IPSComposition"): _ex_ips_composition,
    ("patient-summary", "IPSPatient"): _ex_ips_patient,
    ("patient-summary", "IPSAllergyIntolerance"): _ex_ips_allergy,
    ("patient-summary", "IPSCondition"): _ex_ips_condition,
    ("patient-summary", "IPSMedicationStatement"): _ex_ips_medication_statement,
    ("patient-summary", "IPSImmunization"): _ex_ips_immunization,
    ("patient-summary", "IPSObservationResults"): _ex_ips_observation,
    ("patient-summary", "IPSProcedure"): _ex_ips_procedure,
    # tumor-board
    ("tumor-board", "OncologyPatient"): _ex_oncology_patient,
    ("tumor-board", "PrimaryCancerCondition"): _ex_primary_cancer,
    ("tumor-board", "SecondaryCancerCondition"): _ex_secondary_cancer,
    ("tumor-board", "TNMStageGroup"): _ex_tnm_stage,
    ("tumor-board", "TumorMarkerObservation"): _ex_tumor_marker,
    ("tumor-board", "GenomicVariant"): _ex_genomic_variant,
    ("tumor-board", "GenomicRegionStudied"): _ex_genomic_region,
    ("tumor-board", "CancerRelatedMedicationRequest"): _ex_cancer_med_request,
    ("tumor-board", "CancerRelatedSurgicalProcedure"): _ex_cancer_surgical_procedure,
    ("tumor-board", "RadiotherapyTreatmentPhase"): _ex_radiotherapy_phase,
    ("tumor-board", "OncologyImagingStudy"): _ex_oncology_imaging,
    ("tumor-board", "OncologyPathologyReport"): _ex_oncology_path_report,
    ("tumor-board", "ClinicalTrialSubject"): _ex_research_subject,
    # prior-auth
    ("prior-auth", "USCorePatient"): _ex_us_core_patient,
    ("prior-auth", "PASCoverage"): _ex_pas_coverage,
    ("prior-auth", "USCorePractitioner"): _ex_us_core_practitioner,
    ("prior-auth", "PASOrganization"): _ex_pas_organization,
    ("prior-auth", "PASServiceRequest"): _ex_pas_service_request,
    ("prior-auth", "PASClaim"): _ex_pas_claim,
    ("prior-auth", "PASClaimResponse"): _ex_pas_claim_response,
    ("prior-auth", "PASClinicalDocReference"): _ex_pas_clinical_doc_ref,
    # adverse-event
    ("adverse-event", "USCorePatient"): _ex_us_core_patient,
    ("adverse-event", "ClinicalTrialSubject"): _ex_research_subject,
    ("adverse-event", "ClinicalTrial"): _ex_clinical_trial,
    ("adverse-event", "ClinicalTrialAdverseEvent"): _ex_clinical_trial_adverse_event,
    ("adverse-event", "SuspectMedicationStatement"): _ex_suspect_medication_statement,
    ("adverse-event", "AdverseEventOutcome"): _ex_ae_outcome_observation,
    ("adverse-event", "ReportingInvestigator"): _ex_reporting_investigator,
}


def _build_example(
    use_case_key: str,
    resource_type: str,
    profile_name: str,
) -> dict:
    """Build an example resource for (use_case_key, profile_name).

    Falls back to a profile-stamped minimal resource if no use-case
    builder is registered. The fallback always includes the resource
    type's commonly required fields so the resource passes structural
    validation.
    """
    builder = _EXAMPLE_BUILDERS.get((use_case_key, profile_name))
    if builder is not None:
        return builder(use_case_key, profile_name)

    # Generic fallback: minimal but structurally complete per resource type.
    pid = profile_name.lower().replace("_", "-")
    minimal: dict[str, Any] = {
        "resourceType": resource_type,
        "id": f"example-{pid}",
    }
    if resource_type == "Patient":
        minimal.update({
            "name": [hfhir.human_name(family="Doe", given=["Jane"])],
            "gender": "female",
            "birthDate": "1980-01-01",
        })
    elif resource_type == "Practitioner":
        minimal.update({
            "name": [hfhir.human_name(family="Smith", given=["John"])],
            "active": True,
        })
    elif resource_type == "Organization":
        minimal.update({"name": "Example Org", "active": True})
    return _stamp_profile(minimal, use_case_key, profile_name)


def _render_example_json(
    resource_type: str,
    profile_name: str,
    use_case_key: str,
) -> str:
    """Build + serialize a real example resource as pretty-printed JSON."""
    res = _build_example(use_case_key, resource_type, profile_name)
    return json.dumps(res, indent=2, sort_keys=False) + "\n"


def _camel(s: str) -> str:
    """Lower → CamelCase (no spaces, alpha only)."""
    parts = "".join(c if c.isalnum() or c == " " else " " for c in s).split()
    return "".join(p.capitalize() for p in parts) or "Ig"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class FhirIgPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        if d.use_case_key is None:
            return GenPlan(
                generator_type=GENERATOR_TYPE,
                package_name=package_name or "fhir-ig-unknown",
                domain="(unknown use case)",
                source_query=str(d.notes[0]) if d.notes else "",
                package_metadata={"error": "unknown use case"},
                notes=list(d.notes),
            )
        meta = d.use_case_meta
        pkg_name = package_name or f"fhir-ig-{d.use_case_key}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=meta.get("name", d.use_case_key),
            source_query=d.use_case_key,
            package_metadata={
                "use_case_key": d.use_case_key,
                "fhir_version": meta.get("fhir_version"),
                "base_ig": meta.get("base_ig"),
                "n_resources": len(meta.get("resources", [])),
                "n_value_sets": len(meta.get("value_sets", [])),
                "n_extensions": len(meta.get("extensions", [])),
                "n_citations": len(d.citations),
            },
            notes=list(d.notes),
        )

        sources: list[tuple[str, int, float, float, Optional[str]]] = []
        for cid, _, _, _ in d.citations[:10]:
            sources.append(("concept", cid, 1.0, 1.0, None))
        for _, _, ds, _ in d.citations[:10]:
            sources.append(("doc_section", ds, 1.0, 1.0, None))

        plan.units.append(GenUnit(
            unit_type="fhir_ig",
            name=meta.get("name", d.use_case_key),
            ordinal=1,
            metadata={"use_case_key": d.use_case_key},
            logical_key="ig_main",
            sources=sources,
        ))

        # Top-level files
        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="sushi-config.yaml", content=_render_sushi_config(d), purpose="config"),
            GenFile(filename="ig.ini", content=_render_ig_ini(d), purpose="config"),
            GenFile(filename="input/pagecontent/index.md",
                    content=_render_index_page(d), purpose="page"),
            GenFile(filename="input/pagecontent/background.md",
                    content=_render_background_page(d), purpose="page"),
        ])

        base_ig = meta.get("base_ig", "")
        # Profiles + examples (one per resource entry)
        for entry in meta.get("resources", []):
            resource_type, profile_name, purpose, pmeta = _resource_tuple(entry)
            slug = profile_name.lower().replace("_", "-")
            plan.files.append(GenFile(
                filename=f"input/profiles/{slug}.fsh",
                content=_render_profile_fsh(resource_type, profile_name,
                                             purpose, base_ig, pmeta),
                purpose="profile",
            ))
            plan.files.append(GenFile(
                filename=f"input/examples/{slug}-example.json",
                content=_render_example_json(resource_type, profile_name,
                                              d.use_case_key),
                purpose="example",
            ))

        # Value sets
        for vs_name, vs_purpose in meta.get("value_sets", []):
            slug = vs_name.lower().replace("_", "-")
            plan.files.append(GenFile(
                filename=f"input/valuesets/{slug}.fsh",
                content=_render_valueset_fsh(vs_name, vs_purpose),
                purpose="valueset",
            ))

        # Extensions
        for ext_name, ext_purpose in meta.get("extensions", []):
            plan.files.append(GenFile(
                filename=f"input/extensions/{ext_name}.fsh",
                content=_render_extension_fsh(ext_name, ext_purpose),
                purpose="extension",
            ))

        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class FhirIgValidator:
    def validate(self, conn, plan: GenPlan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("error") == "unknown use case":
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message=f"unknown use case — supported: "
                        f"{sorted(USE_CASE_CATALOG)}",
            ))
            return issues
        if plan.package_metadata.get("n_resources", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="ig_main", severity="error",
                message="use case spec has no resources — incomplete",
            ))
        if plan.package_metadata.get("n_citations", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="ig_main", severity="warning",
                message="no concept citations from cited tools — "
                        "package ships from built-in use case spec only",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------

class FhirIgMaterializer:
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


def make_fhir_ig_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=FhirIgDecomposer(),
        planner=FhirIgPlanner(),
        ranking_mode="generation",
        validator=FhirIgValidator(),
        materializer=FhirIgMaterializer(),
    )
