"""fhir_ig_scaffold.py — FHIR Implementation Guide Scaffold generator.

For a use case + base IG (US Core / IPS / IHE), generates a SUSHI/FSH
Implementation Guide skeleton with profiles, value sets, extensions,
sample resources, and the build configuration files. The result is a
buildable IG repo: `sushi build` produces the StructureDefinitions;
the IG Publisher consumes those + pagecontent to build the website.

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

LOG = logging.getLogger("mypub-fhir-ig")

GENERATOR_TYPE = "fhir_ig_scaffold"


# Use case catalog. Each entry specifies the resource set + base profiles
# the IG should constrain, plus the value sets that need binding.
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
        "resources": [
            ("Patient",          "USCorePatient",      "Constrain to research-cohort identifiers"),
            ("Group",            "ResearchSubjectGroup", "The cohort being exported"),
            ("Observation",      "USCoreLabResultObservation", "Lab results in scope"),
            ("Condition",        "USCoreCondition",    "Diagnoses in scope"),
            ("MedicationRequest", "USCoreMedicationRequest", "Medications in scope"),
            ("DocumentReference", "USCoreDocumentReference", "Clinical notes"),
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
        "resources": [
            ("Composition",      "IPSComposition",       "The bundle's table-of-contents"),
            ("Patient",          "IPSPatient",           "Patient demographics"),
            ("AllergyIntolerance", "IPSAllergyIntolerance", "Active allergies"),
            ("Condition",        "IPSCondition",         "Active problem list"),
            ("MedicationStatement", "IPSMedicationStatement", "Current meds"),
            ("Immunization",     "IPSImmunization",      "Vaccination history"),
            ("Observation",      "IPSObservationResults", "Recent vital signs + labs"),
            ("Procedure",        "IPSProcedure",         "History of procedures"),
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
        "resources": [
            ("Patient",          "OncologyPatient",         "Cancer patient demographics + cancer-relevant identifiers"),
            ("Condition",        "PrimaryCancerCondition",  "The primary diagnosis (mCODE PrimaryCancerCondition)"),
            ("Condition",        "SecondaryCancerCondition", "Metastases (mCODE)"),
            ("Observation",      "TNMStageGroup",            "TNM staging at diagnosis (mCODE)"),
            ("Observation",      "TumorMarkerObservation",   "PSA, CA-125, etc. (mCODE)"),
            ("Observation",      "GenomicVariant",           "VRS-shaped variant call (mCODE GenomicVariant + GA4GH VRS)"),
            ("Observation",      "GenomicRegionStudied",     "Sequencing scope"),
            ("MedicationRequest", "CancerRelatedMedicationRequest", "Targeted + chemo therapies (mCODE)"),
            ("Procedure",        "CancerRelatedSurgicalProcedure", "Resection, biopsy"),
            ("Procedure",        "RadiotherapyTreatmentPhase",     "Radiation course"),
            ("ImagingStudy",     "OncologyImagingStudy",     "Reference to DICOM imaging (PET/CT, MR)"),
            ("DiagnosticReport", "OncologyPathologyReport",  "Path report with structured findings"),
            ("ResearchSubject",  "ClinicalTrialSubject",     "Active trial enrollment(s)"),
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
        "resources": [
            ("Patient",          "USCorePatient",      "Beneficiary"),
            ("Coverage",         "PASCoverage",        "Insurance coverage"),
            ("Practitioner",     "USCorePractitioner", "Requesting provider"),
            ("Organization",     "PASOrganization",    "Provider organization + payer"),
            ("ServiceRequest",   "PASServiceRequest",  "Proposed service"),
            ("Claim",            "PASClaim",           "Prior auth request (mapped to X12 278 transaction)"),
            ("ClaimResponse",    "PASClaimResponse",   "Payer's auth decision"),
            ("DocumentReference", "PASClinicalDocReference", "Supporting documentation"),
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
        "resources": [
            ("Patient",          "USCorePatient",      "Trial subject (or post-market patient)"),
            ("ResearchSubject",  "ClinicalTrialSubject", "Trial enrollment context"),
            ("ResearchStudy",    "ClinicalTrial",      "The trial protocol"),
            ("AdverseEvent",     "ClinicalTrialAdverseEvent", "The AE itself + suspect agent + severity + outcome"),
            ("MedicationStatement", "SuspectMedicationStatement", "The investigational + concomitant meds"),
            ("Observation",      "AdverseEventOutcome", "Lab values supporting the AE"),
            ("Practitioner",     "ReportingInvestigator", "The PI / reporter"),
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
        "    ├── profiles/        (FSH StructureDefinition skeletons)",
        "    ├── valuesets/       (FSH ValueSet skeletons)",
        "    ├── extensions/      (FSH Extension skeletons)",
        "    └── examples/        (JSON example resources)",
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
        "## Caveats",
        "",
        "- The FSH skeletons declare resource constraints but the actual "
           "cardinality + binding rules need use-case judgment. Each profile "
           "ships with a TODO comment marking where to add `^* MS` "
           "(must-support flags) and `from <ValueSet> (extensible)` bindings.",
        "- **Examples are skeletal.** Each example file conforms to the "
           "Resource type but does NOT yet validate against the profile "
           "constraints. Fill in real data + run `sushi build && java -jar "
           "publisher.jar` to surface conformance errors.",
        "- **Code system bindings** assume the standard ones (LOINC, SNOMED "
           "CT, ICD-10, RxNorm). If you bind to local code systems, you'll "
           "need additional ConceptMaps.",
        "- For oncology IGs (e.g., tumor-board), align with mCODE rather "
           "than reinventing — the catalog entry already inherits from mCODE "
           "where appropriate.",
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
        "## Scope",
        "",
        "Resources in scope:",
        "",
    ]
    for resource_type, profile_name, _purpose in meta.get("resources", []):
        lines.append(f"- **{resource_type}** profiled as `{profile_name}`")
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
        "## Resources constrained + what each contributes",
        "",
    ]
    for resource_type, profile_name, purpose in meta.get("resources", []):
        lines.append(f"- **{profile_name}** (constrains `{resource_type}`) — {purpose}")
    lines.extend(["", "## Value sets bound", ""])
    for vs_name, vs_purpose in meta.get("value_sets", []):
        lines.append(f"- **{vs_name}** — {vs_purpose}")
    if meta.get("extensions"):
        lines.extend(["", "## Extensions defined", ""])
        for ext_name, ext_purpose in meta["extensions"]:
            lines.append(f"- **{ext_name}** — {ext_purpose}")
    return "\n".join(lines)


def _render_profile_fsh(resource_type: str, profile_name: str, purpose: str,
                        base_ig: str) -> str:
    """Generate a FHIR Shorthand StructureDefinition skeleton."""
    parent = _parent_for_profile(resource_type, base_ig)
    return f"""// Auto-generated FSH skeleton — fill in cardinality + binding rules.
//
// Purpose: {purpose}

Profile:        {profile_name}
Parent:         {parent}
Id:             {profile_name.lower().replace('_', '-')}
Title:          "{profile_name}"
Description:    "{purpose}"

// TODO: pin must-support flags + cardinality constraints
// Examples (uncomment + adapt for the use case):
// * identifier MS
// * identifier 1..*
// * code 1..1
// * code from {profile_name}Code (extensible)
// * subject only Reference(Patient)
"""


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


def _render_example_json(resource_type: str, profile_name: str, base_ig: str) -> str:
    """Minimal example resource that conforms to the resource type but is
    intentionally skeletal — operator must fill in real data."""
    parent = _parent_for_profile(resource_type, base_ig)
    profile_url = f"http://example.org/fhir/StructureDefinition/{profile_name.lower().replace('_', '-')}"
    return ('{\n'
            f'  "resourceType": "{resource_type}",\n'
            f'  "id": "example-{profile_name.lower().replace("_", "-")}",\n'
            '  "meta": {\n'
            f'    "profile": ["{profile_url}"]\n'
            '  }\n'
            '}\n'
            '// TODO: populate with conformant data; sushi + publisher will\n'
            '// surface validation errors against the profile.\n')


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
        for resource_type, profile_name, purpose in meta.get("resources", []):
            slug = profile_name.lower().replace("_", "-")
            plan.files.append(GenFile(
                filename=f"input/profiles/{slug}.fsh",
                content=_render_profile_fsh(resource_type, profile_name,
                                             purpose, base_ig),
                purpose="profile",
            ))
            plan.files.append(GenFile(
                filename=f"input/examples/{slug}-example.json",
                content=_render_example_json(resource_type, profile_name,
                                              base_ig),
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
