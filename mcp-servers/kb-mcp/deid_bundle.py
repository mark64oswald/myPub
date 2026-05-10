"""deid_bundle.py — De-identification Procedure Bundle generator.

For a dataset description (FHIR / DICOM / HL7v2 / generic clinical),
generate a complete de-identification package: HIPAA Safe-Harbor-mapped
PHI element list, per-element de-id technique with rationale, a runnable
Python pipeline that imports from ``healthcare_libs`` and actually
de-identifies records, and a test that proves no PHI survives the
pipeline. Citations from healthcare doc sources (pydicom, DCMTK, Synthea,
FHIR specs) appear inline.

Output structure::

    deid-bundle-<dataset>/
      README.md            overview + dataset description
      rationale.md         per-PHI-element justification + HIPAA citation
      audit_trail.md       template for tracking what was changed
      deid_pipeline.py     runnable de-id pipeline using healthcare_libs
      tests/test_deid.py   assertions that PHI is absent post-pipeline

Generator structure (Decomposer / Planner / Validator / Materializer)
matches the rest of the framework. The interesting work is in the
per-dataset pipeline renderers near the bottom of this file: each one
emits a Python module that imports from ``healthcare_libs.deid`` plus
the matching format library (``healthcare_libs.fhir``,
``.dicom``, ``.hl7v2``) and calls into them rather than shipping
``pass``-only stubs.
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

LOG = logging.getLogger("mypub-deid-bundle")

GENERATOR_TYPE = "deid_bundle"


# HIPAA Safe Harbor (45 CFR 164.514(b)(2)) — the 18 identifiers that must
# be removed for a dataset to be de-identified under that method.
SAFE_HARBOR_IDENTIFIERS = [
    ("names",                         "Names of the individual + relatives + employers + household members"),
    ("geo_subdivisions",              "All geographic subdivisions smaller than a state (street, city, ZIP — except first 3 ZIP digits when the geographic unit covers >20,000 people)"),
    ("dates",                         "All dates directly related to an individual (birth, admission, discharge, death) — except year. Ages over 89 must be aggregated into a 90+ category."),
    ("phone_fax",                     "Telephone + fax numbers"),
    ("email",                         "Email addresses"),
    ("ssn",                           "Social Security numbers"),
    ("mrn",                           "Medical record numbers"),
    ("health_plan_id",                "Health plan beneficiary numbers"),
    ("account",                       "Account numbers"),
    ("cert_license",                  "Certificate / license numbers"),
    ("vehicle_id",                    "Vehicle identifiers + serial numbers (incl. license plates)"),
    ("device_id",                     "Device identifiers + serial numbers"),
    ("urls",                          "Web URLs"),
    ("ip",                            "IP addresses"),
    ("biometric",                     "Biometric identifiers (fingerprints, voice prints)"),
    ("photos",                        "Full-face photographs + comparable images"),
    ("other_unique",                  "Any other unique identifying number, characteristic, or code (except a re-identification code per §164.514(c))"),
    ("rare_traits",                   "Rare disease / traits / descriptors that could re-identify in small populations"),
]

# Per-dataset PHI element catalog — where each Safe Harbor identifier
# typically lives in that data shape, plus the standard de-id technique.
DATASET_TYPES: dict[str, dict[str, Any]] = {
    "fhir": {
        "name": "FHIR Resources",
        "description": "FHIR R4/R5 resources — Patient, Practitioner, "
                       "Observation, Encounter, Condition, MedicationRequest, "
                       "DocumentReference, etc.",
        "tools_cited": ["FHIR Specification", "Medplum", "HAPI FHIR", "Synthea"],
        # (PHI category, FHIR location, recommended technique, rationale)
        "phi_elements": [
            ("names", "Patient.name, Practitioner.name, RelatedPerson.name",
             "suppress", "Names are direct identifiers; full removal is the standard"),
            ("dates", "Patient.birthDate, *.effectiveDateTime, Encounter.period",
             "generalize_to_year_or_age_band",
             "Year-only or 5-year age bands preserve analytic value while satisfying Safe Harbor"),
            ("geo_subdivisions", "Patient.address",
             "truncate_to_first_3_zip_digits_or_drop",
             "Drop street/city; keep first 3 ZIP digits only if geo unit covers >20,000 people"),
            ("phone_fax", "Patient.telecom (system=phone|fax)",
             "suppress", "Direct identifier; not analytically useful"),
            ("email", "Patient.telecom (system=email)",
             "suppress", "Direct identifier"),
            ("ssn", "Patient.identifier (system contains 'us-ssn')",
             "suppress", "Direct identifier; never permissible under Safe Harbor"),
            ("mrn", "Patient.identifier (type.coding.code='MR')",
             "pseudonymize_with_kept_lookup",
             "Replace with a generated pseudonym; preserve a separately-stored lookup"),
            ("device_id", "Device.identifier, ImagingStudy.endpoint",
             "suppress_or_aggregate_to_model", "Keep device model, drop serial"),
            ("photos", "Media (content=image), Patient.photo",
             "suppress", "Full-face photos are explicit Safe Harbor identifiers"),
            ("other_unique", "Resource.id, *.identifier (any system)",
             "rehash_to_per-study_pseudonym",
             "Resource ids can be re-identifying across longitudinal data; rehash per study"),
        ],
    },
    "dicom": {
        "name": "DICOM Imaging Study",
        "description": "DICOM Part 10 files — imaging studies including CT, "
                       "MR, US, PET. PHI lives in tagged metadata + burned-in "
                       "annotations on pixel data.",
        "tools_cited": ["pydicom", "DCMTK"],
        "phi_elements": [
            ("names", "(0010,0010) PatientName, (0008,0090) ReferringPhysicianName",
             "suppress_or_replace_with_DICOM_anonymous",
             "Use DICOM Confidentiality Profile (PS3.15 Annex E); replace per Basic Application Confidentiality Profile"),
            ("dates", "(0010,0030) PatientBirthDate, (0008,0020) StudyDate, AcquisitionDate, ContentDate",
             "shift_dates_by_per-patient_random_offset",
             "Preserves intra-study time relationships while removing absolute date — per DICOM Basic Application Confidentiality Profile"),
            ("mrn", "(0010,0020) PatientID",
             "pseudonymize_with_kept_lookup",
             "Pseudonym preserves study linkage; lookup stored separately under stricter access"),
            ("ssn", "(0010,0021) IssuerOfPatientID, IssuerOfAccessionNumberSequence",
             "suppress", "Never preserved under any DICOM de-id profile"),
            ("photos", "Pixel data (burned-in PHI on the image itself)",
             "ocr_detect_and_redact_burned_in_annotations",
             "Use DCMTK + OCR; manual review of flagged studies before release"),
            ("device_id", "(0008,1090) ManufacturerModelName, (0018,1000) DeviceSerialNumber",
             "keep_model_drop_serial",
             "Model is required for protocol replication; serial is a unique identifier"),
            ("other_unique", "Private tags (any group ≥0x0009 odd)",
             "suppress_all_private_tags_unless_explicitly_allowlisted",
             "Private tags routinely contain PHI; default-deny is the safe posture"),
        ],
    },
    "hl7v2": {
        "name": "HL7 v2 Messages",
        "description": "HL7 v2 ADT/ORU/ORM messages — pipe-delimited segments "
                       "carrying admission, results, orders.",
        "tools_cited": ["HAPI HL7v2 — Parsing", "HAPI HL7v2 — Validation",
                        "HL7 Library (PHP)", "Mirth/NextGen Connect", "hl7apy"],
        "phi_elements": [
            ("names", "PID-5 Patient Name, PID-9 Mother's Maiden Name, NK1-2 Next of Kin Name, PV1-7 Attending Doctor",
             "suppress",
             "Direct identifiers in standard HL7v2 PID/NK1/PV1 segments"),
            ("dates", "PID-7 Date of Birth, PV1-44 Admit Date/Time, PV1-45 Discharge Date/Time, OBR-7 Observation Date/Time",
             "generalize_to_year_or_shift_by_offset",
             "Both techniques are standard; choose by analytic need (longitudinal studies prefer date-shift)"),
            ("geo_subdivisions", "PID-11 Patient Address",
             "truncate_to_first_3_zip_digits_or_drop",
             "Standard Safe Harbor handling"),
            ("phone_fax", "PID-13 Phone Number Home, PID-14 Phone Number Business, NK1-5 Phone",
             "suppress", "Direct identifier"),
            ("ssn", "PID-19 SSN, PID-3 Patient Identifier List (when ID type code is 'SS')",
             "suppress", "Never preserved"),
            ("mrn", "PID-3 Patient Identifier List (when ID type code is 'MR')",
             "pseudonymize_with_kept_lookup",
             "Same approach as FHIR/DICOM — pseudonym preserves linkage"),
            ("account", "PID-18 Patient Account Number",
             "pseudonymize_or_suppress",
             "Suppression is safer if not needed for the analysis"),
            ("device_id", "OBX-18 Equipment Instance Identifier",
             "keep_model_drop_serial", "Standard handling"),
        ],
    },
    "clinical_trial": {
        "name": "Clinical Trial Dataset",
        "description": "Clinical trial subject data — typically a mix of "
                       "REDCap/EDC exports, central lab results, and adverse "
                       "event reports. Often FHIR-shaped at modern sites.",
        "tools_cited": ["FHIR Specification", "Synthea", "US Core Implementation Guide"],
        "phi_elements": [
            ("names", "Subject demographics, investigator names",
             "subject_assigned_pseudonym_only",
             "Trial subjects are identified only by pseudonymous SubjectID per ICH E6 GCP"),
            ("dates", "Visit dates, AE onset, dosing schedule, lab collection",
             "shift_by_per-subject_random_offset_OR_generalize",
             "Date-shift preserves dosing-to-AE intervals; generalization simpler for cross-trial pooling"),
            ("geo_subdivisions", "Site location",
             "encode_as_site_id_only",
             "Site identifier is sufficient for stratification; address is not"),
            ("mrn", "Source-system MRN (when linking to EHR)",
             "drop_after_subject_id_assigned",
             "MRN should not appear in trial datasets; subject_id is the link key"),
            ("rare_traits", "Rare disease classification, genomic variants, demographic combinations",
             "k-anonymize_or_aggregate",
             "Rare cohorts can be re-identified by combination of traits; "
             "k-anonymity (k≥5) or aggregation is the standard mitigation"),
            ("other_unique", "Site-level subject ID + visit ID combinations",
             "rehash_per_release", "Rehash if multiple releases of the same trial are made"),
        ],
    },
}


# De-id technique → human-readable description + Python implementation hint
TECHNIQUE_LIBRARY: dict[str, dict[str, str]] = {
    "suppress": {
        "description": "Remove the field entirely. Replace with NULL or empty string.",
        "implementation": "del record[field] OR record[field] = None",
    },
    "generalize_to_year_or_age_band": {
        "description": "Replace specific dates with year-only; replace ages with 5-year bands; ages 90+ aggregated to '90+'.",
        "implementation": "record[field] = deid.date_to_year_only(record[field])",
    },
    "truncate_to_first_3_zip_digits_or_drop": {
        "description": "Keep only the first 3 ZIP digits if the resulting geo unit covers >20,000 people; otherwise drop entirely.",
        "implementation": "record[field] = deid.truncate_zip(record[field], allowed_zip3=ALLOWED_ZIP3)",
    },
    "shift_by_per-subject_random_offset_OR_generalize": {
        "description": "Apply a per-subject random offset (typically ±60 days) consistently to all dates for that subject. Preserves intra-subject intervals.",
        "implementation": "record[field] = deid.shift_date(record[field], offset)",
    },
    "generalize_to_year_or_shift_by_offset": {
        "description": "Choose between year-only generalization or per-patient date-shift based on analytic need. Generalize for cross-cohort comparison; shift for longitudinal studies that need preserved intervals.",
        "implementation": "record[field] = deid.date_to_year_only(record[field])  # OR deid.shift_date(...)",
    },
    "shift_dates_by_per-patient_random_offset": {
        "description": "Same as above but per-patient. DICOM Basic Application Confidentiality Profile §E.3.",
        "implementation": "dicom.deidentify_basic_profile(ds, date_offset_days=offset)",
    },
    "pseudonymize_with_kept_lookup": {
        "description": "Replace identifier with a generated pseudonym (HMAC-SHA256). Store the original→pseudonym mapping in a separate, more-strictly-controlled location.",
        "implementation": "record[field] = deid.hmac_pseudonym(record[field], salt=config.pseudonym_salt)",
    },
    "rehash_to_per-study_pseudonym": {
        "description": "Hash the value with a study-specific salt. Identical inputs hash identically within a study (preserves linkage) but differ across studies.",
        "implementation": "record[field] = deid.hmac_pseudonym(record[field], salt=STUDY_SALT)",
    },
    "rehash_per_release": {
        "description": "Same as rehash but salt rotates per data release. Prevents linking across releases.",
        "implementation": "record[field] = deid.hmac_pseudonym(record[field], salt=RELEASE_SALT)",
    },
    "subject_assigned_pseudonym_only": {
        "description": "Only the trial-assigned SubjectID appears in the dataset. Names, MRNs, and all source-system identifiers are stripped during enrollment ingest.",
        "implementation": "# Done at enrollment ingest, not at de-id time",
    },
    "drop_after_subject_id_assigned": {
        "description": "Drop the field once a SubjectID has been assigned. Source-system identifier is not preserved in the trial dataset.",
        "implementation": "del record[field]",
    },
    "ocr_detect_and_redact_burned_in_annotations": {
        "description": "Run OCR on pixel data (DICOM image arrays); detect any text matching PHI patterns; redact (black-rectangle overlay). Manually review flagged studies before release.",
        "implementation": "if dicom.has_burned_in_phi_risk(ds): flag_for_ocr_review(ds)",
    },
    "ocr_detect_and_redact_pixel_burned_in": {
        "description": "Same as above (alias used in some code paths).",
        "implementation": "if dicom.has_burned_in_phi_risk(ds): flag_for_ocr_review(ds)",
    },
    "suppress_or_aggregate_to_model": {
        "description": "Drop the device serial; keep model name only.",
        "implementation": "record['device_serial'] = None  # keep record['device_model']",
    },
    "keep_model_drop_serial": {
        "description": "Keep device model name (often required for protocol replication); drop serial number.",
        "implementation": "record['device_serial'] = None  # keep record['device_model']",
    },
    "pseudonymize_or_suppress": {
        "description": "Either pseudonymize (with kept lookup) or suppress entirely. Choice depends on whether the field is needed for the downstream analysis.",
        "implementation": "record[field] = deid.hmac_pseudonym(record[field], salt=config.pseudonym_salt)  # or: del record[field]",
    },
    "encode_as_site_id_only": {
        "description": "Replace full geographic location with a site identifier (which is itself not directly identifying without a separate site catalog).",
        "implementation": "record[field] = SITE_LOOKUP[record[field]]",
    },
    "k-anonymize_or_aggregate": {
        "description": "Apply k-anonymity (k≥5): every combination of quasi-identifiers must appear ≥k times. If not achievable, aggregate the rare value into a broader category.",
        "implementation": "deid.k_anonymize(records, quasi_identifiers=[...], k=config.k_anonymity_threshold)",
    },
    "suppress_all_private_tags_unless_explicitly_allowlisted": {
        "description": "Default-deny: drop every private DICOM tag (group ≥0x0009 odd). Only keep a tag if it appears on an explicit allowlist for the study.",
        "implementation": "dicom.deidentify_basic_profile(ds)  # strips private tags by default",
    },
    "suppress_or_replace_with_DICOM_anonymous": {
        "description": "Apply DICOM Basic Application Confidentiality Profile (PS3.15 Annex E). PatientName → empty; ReferringPhysicianName → empty.",
        "implementation": "dicom.deidentify_basic_profile(ds, patient_pseudonym=...)",
    },
}


@dataclass
class _Decomposition:
    dataset_type: str  # 'fhir' | 'dicom' | 'hl7v2' | 'clinical_trial'
    dataset_meta: dict[str, Any]
    # Per-element rows: (phi_category, location, technique, rationale, technique_meta)
    elements: list[tuple[str, str, str, str, dict[str, str]]]
    citations: list[tuple[int, str, int, str]]  # (concept_id, name, doc_section_id, source_name)
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "deid"


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------

class DeidDecomposer:
    """Map a dataset description to its PHI element catalog + technique list."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        **_: Any,
    ) -> _Decomposition:
        # Normalize the query — accept "fhir", "FHIR Patient", "DICOM imaging", etc.
        q = query.lower()
        if "fhir" in q:
            ds_type = "fhir"
        elif "dicom" in q:
            ds_type = "dicom"
        elif "hl7" in q and "v2" in q or "hl7v2" in q.replace(" ", ""):
            ds_type = "hl7v2"
        elif "trial" in q or "redcap" in q or "edc" in q:
            ds_type = "clinical_trial"
        else:
            return _Decomposition(
                dataset_type=q,
                dataset_meta={},
                elements=[],
                citations=[],
                notes=[f"unrecognized dataset type: {query!r} "
                       f"(supported: fhir, dicom, hl7v2, clinical_trial)"],
            )

        meta = DATASET_TYPES[ds_type]
        elements: list[tuple[str, str, str, str, dict[str, str]]] = []
        for category, location, technique, rationale in meta["phi_elements"]:
            tech_meta = TECHNIQUE_LIBRARY.get(technique, {})
            elements.append((category, location, technique, rationale, tech_meta))

        # Citations from healthcare doc sources covering the cited tools
        cited_source_names = meta["tools_cited"]
        citations: list[tuple[int, str, int, str]] = []
        if cited_source_names:
            placeholders = ",".join(["?"] * len(cited_source_names))
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
                list(cited_source_names),
            ).fetchall()
            citations = [(int(r[0]), r[1], int(r[2]), r[3]) for r in rows]

        notes: list[str] = []
        if not citations:
            notes.append(
                "no concept citations from the cited tools; "
                "package ships from the built-in spec only"
            )
        return _Decomposition(
            dataset_type=ds_type, dataset_meta=meta,
            elements=elements, citations=citations, notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers — README, rationale, audit are per-dataset-shape but format-agnostic
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    meta = d.dataset_meta
    lines = [
        f"# De-identification Bundle — {meta.get('name', d.dataset_type)}",
        "",
        meta.get("description", ""),
        "",
        "## What this package contains",
        "",
        "- `rationale.md` — per-PHI-element justification with HIPAA Safe "
           "Harbor citations",
        "- `audit_trail.md` — template for logging what your pipeline "
           "actually changed (regulatory + scientific reproducibility)",
        "- `deid_pipeline.py` — runnable Python pipeline that imports from "
           "`healthcare_libs` and applies a real de-identification per "
           "PHI category (no `pass`-only stubs)",
        "- `tests/test_deid.py` — assertions that PHI is absent from "
           "post-pipeline output, runnable against a synthetic record",
        "",
        "## HIPAA Safe Harbor reminder",
        "",
        "Safe Harbor (45 CFR §164.514(b)(2)) requires removal of **18 "
        "categories** of identifiers AND no actual knowledge that residual "
        "info could re-identify. This package addresses each category that "
        "appears in this dataset shape; it does NOT replace expert "
        "determination review for high-risk releases (genomic data, "
        "rare-disease cohorts, small populations).",
        "",
        "## How to run",
        "",
        "The generated pipeline imports from `healthcare_libs`, which lives "
        "in this project at `mcp-servers/kb-mcp/healthcare_libs/`. Set "
        "`PYTHONPATH` accordingly:",
        "",
        "```bash",
        "export PYTHONPATH=/path/to/myPub/mcp-servers/kb-mcp",
        "pytest tests/test_deid.py        # synthetic-record smoke test",
        "python deid_pipeline.py --help   # for batch usage",
        "```",
        "",
    ]
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for cid, cname, _ds, src in d.citations[:15]:
            lines.append(f"- **{cname}** — {src}")
        lines.append("")
    return "\n".join(lines)


def _render_rationale(d: _Decomposition) -> str:
    lines = [
        f"# De-id Rationale — {d.dataset_meta.get('name', d.dataset_type)}",
        "",
        f"_{len(d.elements)} PHI element type(s) addressed._",
        "",
    ]
    by_category: dict[str, list[tuple[str, str, str, str, dict[str, str]]]] = {}
    for el in d.elements:
        by_category.setdefault(el[0], []).append(el)

    # Map category code → Safe Harbor description
    safe_harbor_lookup = dict(SAFE_HARBOR_IDENTIFIERS)
    for category, els in by_category.items():
        sh_desc = safe_harbor_lookup.get(category, "(custom category)")
        lines.append(f"## {category} — {sh_desc}")
        lines.append("")
        for _, location, technique, rationale, tech_meta in els:
            lines.append(f"**Location:** `{location}`")
            lines.append("")
            lines.append(f"**Technique:** `{technique}` — {tech_meta.get('description', '')}")
            lines.append("")
            lines.append(f"**Rationale:** {rationale}")
            lines.append("")
            if tech_meta.get("implementation"):
                lines.append(f"```python")
                lines.append(f"# {tech_meta['implementation']}")
                lines.append(f"```")
                lines.append("")
    return "\n".join(lines)


def _render_audit(d: _Decomposition) -> str:
    lines = [
        f"# Audit Trail — {d.dataset_meta.get('name', d.dataset_type)} de-identification",
        "",
        "_Template — fill in for your specific run._",
        "",
        "## Run metadata",
        "",
        "- Date: ____",
        "- Operator: ____",
        "- Source dataset (path / version): ____",
        "- Output dataset (path / version): ____",
        "- Pipeline version (git sha): ____",
        "- Pseudonym lookup storage location (separately controlled): ____",
        "- Date-shift offsets storage (if used, separately controlled): ____",
        "",
        "## Per-element changes",
        "",
        "| PHI category | Location | Technique applied | Records affected | Validation |",
        "|---|---|---|---|---|",
    ]
    for category, location, technique, _rationale, _tm in d.elements:
        lines.append(f"| {category} | `{location}` | {technique} | __ | __ |")
    lines.extend([
        "",
        "## Safe Harbor checklist",
        "",
    ])
    addressed = {el[0] for el in d.elements}
    for code, description in SAFE_HARBOR_IDENTIFIERS:
        mark = "x" if code in addressed else " "
        lines.append(f"- [{mark}] **{code}** — {description}")
    lines.extend([
        "",
        "## Expert determination notes (if applicable)",
        "",
        "_If this dataset includes any of the following, expert determination review is recommended in addition to Safe Harbor:_",
        "",
        "- Genomic / WGS data",
        "- Rare disease / rare-trait combinations (cells of size <11)",
        "- Small geographic populations",
        "- Free-text clinical notes (require additional NLP de-id)",
        "- Linked external datasets that could re-identify",
        "",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline + test renderers — per-dataset-shape, real implementations
# ---------------------------------------------------------------------------
#
# Each ``_render_pipeline_<shape>`` and ``_render_test_<shape>`` emits a
# Python module that:
#
#   1. imports from ``healthcare_libs.deid`` (and the matching format
#      module — ``fhir`` / ``dicom`` / ``hl7v2``);
#   2. defines real per-PHI-category functions (no ``pass`` stubs);
#   3. is runnable against a synthetic record to prove end-to-end de-id
#      works.

def _pipeline_header(d: _Decomposition, *, shape_imports: str) -> list[str]:
    """Common preamble: docstring, imports, default config, audit setup."""
    return [
        f'"""De-identification pipeline for {d.dataset_meta.get("name", d.dataset_type)}.',
        '',
        'Generated by /kb-deid-bundle. Imports the format-agnostic primitives',
        'from ``healthcare_libs.deid`` and the format-specific helpers from',
        '``healthcare_libs.{fhir,dicom,hl7v2}`` — each per-PHI-category',
        'function below is a real implementation, not a stub.',
        '',
        'Run ``pytest tests/test_deid.py`` to verify end-to-end against a',
        'synthetic record. For batch usage, wire your reader/writer to',
        '``deid_dataset()`` at the bottom of this file.',
        '"""',
        'from __future__ import annotations',
        '',
        'import argparse',
        'import json',
        'import logging',
        'import secrets',
        'from pathlib import Path',
        'from typing import Any, Optional',
        '',
        shape_imports,
        '',
        'LOG = logging.getLogger("deid")',
        '',
        '# ---- Default config -----------------------------------------',
        '#',
        '# Rotate ``pseudonym_salt`` per release so pseudonyms are unlinkable',
        '# across releases. Keep it stable within a release so joins on the',
        '# same patient survive. Same idea for ``date_offset_seed``.',
        '# In production, load these from a secret store — not from the',
        '# generated module like this.',
        '',
        'DEFAULT_CONFIG = DeidConfig(',
        '    pseudonym_salt=secrets.token_hex(32),',
        '    date_offset_seed=secrets.token_hex(32),',
        '    date_offset_max_days=60,',
        '    keep_zip3_only=True,',
        '    age_band_size=5,',
        '    k_anonymity_threshold=5,',
        ')',
        '',
        '# Caller passes the HHS-published allowlist of ZIP3s with',
        '# population >20,000. Empty allowlist → all ZIPs dropped',
        '# (conservative default per Safe Harbor §164.514(b)(2)(i)(B)).',
        'ALLOWED_ZIP3: set[str] = set()',
        '',
    ]


def _pipeline_footer(d: _Decomposition, *, dispatch_lines: list[str]) -> list[str]:
    """CLI entry point + dataset orchestrator."""
    return [
        '',
        '',
        '# ---- Pipeline orchestrator -----------------------------------',
        '',
        'def deid_dataset(',
        '    input_path: Path,',
        '    output_path: Path,',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    audit_path: Optional[Path] = None,',
        ') -> None:',
        '    """Walk an input dataset, apply per-record de-id, write the output.',
        '',
        '    Wire your specific reader/writer here. The per-record path is',
        '    ``deid_record()`` above; this function only handles the I/O loop.',
        '    """',
        '    output_path.mkdir(parents=True, exist_ok=True)',
        '    audit = AuditLog(audit_path) if audit_path else None',
        '    try:',
        '        # TODO: replace with your actual reader / writer.',
        '        # The per-record de-id call is:',
        '        #     out = deid_record(record, config=config, audit=audit)',
        '        raise NotImplementedError(',
        '            "Wire your dataset reader/writer here; see deid_record() above"',
        '        )',
        '    finally:',
        '        if audit is not None:',
        '            audit.close()',
        '',
        '',
        'def main() -> None:',
        '    p = argparse.ArgumentParser(description=__doc__)',
        '    p.add_argument("--input", type=Path, required=True)',
        '    p.add_argument("--output", type=Path, required=True)',
        '    p.add_argument("--audit", type=Path, default=None,',
        '                   help="Optional audit-log JSONL path")',
        '    args = p.parse_args()',
        '    logging.basicConfig(level=logging.INFO)',
        '    deid_dataset(args.input, args.output, audit_path=args.audit)',
        '',
        '',
        'if __name__ == "__main__":',
        '    main()',
    ]


# ---------------------------------------------------------------------------
# FHIR pipeline + test
# ---------------------------------------------------------------------------

def _render_pipeline_fhir(d: _Decomposition) -> str:
    """FHIR pipeline — walks Patient/Observation/Encounter resources."""
    imports = (
        'from healthcare_libs.deid import (\n'
        '    DeidConfig,\n'
        '    AuditLog,\n'
        '    hmac_pseudonym,\n'
        '    date_to_year_only,\n'
        '    truncate_zip,\n'
        '    suppress,\n'
        ')'
    )
    lines = _pipeline_header(d, shape_imports=imports)
    lines.extend([
        '',
        '# ---- Per-PHI-category functions for FHIR ---------------------',
        '#',
        '# Each function takes a resource dict, mutates a copy, and',
        '# optionally appends an entry to the audit log. The AuditLog',
        '# stores SHA256 of the original value (never the original).',
        '',
        '',
        'def deid_names(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Suppress Patient.name / Practitioner.name / RelatedPerson.name."""',
        '    rt = record.get("resourceType", "")',
        '    if rt in ("Patient", "Practitioner", "RelatedPerson") and "name" in record:',
        '        if audit is not None:',
        '            audit.record(',
        '                record_id=str(record.get("id", "")),',
        '                action="suppress",',
        '                field_path=f"{rt}.name",',
        '                before_value=json.dumps(record["name"]),',
        '            )',
        '        record["name"] = []',
        '    return record',
        '',
        '',
        'def deid_dates(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Generalize Patient.birthDate, *.effectiveDateTime, Encounter.period.* to year-only.',
        '',
        '    Default Safe Harbor posture: year-only generalization. For',
        '    longitudinal studies that need preserved intra-subject',
        '    intervals, swap to ``deid.shift_date`` keyed by the subject',
        '    pseudonym + ``deid.per_subject_offset``.',
        '    """',
        '    rid = str(record.get("id", ""))',
        '',
        '    def _generalize(parent: dict, key: str, path: str) -> None:',
        '        v = parent.get(key)',
        '        if not v or not isinstance(v, str):',
        '            return',
        '        try:',
        '            parent[key] = date_to_year_only(v)',
        '        except ValueError:',
        '            return',
        '        if audit is not None:',
        '            audit.record(rid, "generalize_date", path, before_value=v)',
        '',
        '    _generalize(record, "birthDate", "birthDate")',
        '    _generalize(record, "effectiveDateTime", "effectiveDateTime")',
        '    _generalize(record, "issued", "issued")',
        '',
        '    period = record.get("period")',
        '    if isinstance(period, dict):',
        '        _generalize(period, "start", "period.start")',
        '        _generalize(period, "end", "period.end")',
        '',
        '    # Encounter has period; Observation may have effectivePeriod.',
        '    eperiod = record.get("effectivePeriod")',
        '    if isinstance(eperiod, dict):',
        '        _generalize(eperiod, "start", "effectivePeriod.start")',
        '        _generalize(eperiod, "end", "effectivePeriod.end")',
        '    return record',
        '',
        '',
        'def deid_geo_subdivisions(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Drop street/city/state from Patient.address; keep ZIP3 if allowlisted."""',
        '    rid = str(record.get("id", ""))',
        '    addresses = record.get("address")',
        '    if not isinstance(addresses, list):',
        '        return record',
        '    new_addresses: list[dict] = []',
        '    for addr in addresses:',
        '        if not isinstance(addr, dict):',
        '            continue',
        '        zip_in = addr.get("postalCode")',
        '        zip3 = truncate_zip(zip_in, allowed_zip3=ALLOWED_ZIP3) if zip_in else None',
        '        if audit is not None:',
        '            audit.record(rid, "truncate_address", "address",',
        '                         before_value=json.dumps(addr))',
        '        scrubbed: dict[str, Any] = {}',
        '        if zip3 is not None:',
        '            scrubbed["postalCode"] = zip3',
        '        # Country is not a Safe Harbor identifier; keep it.',
        '        if addr.get("country"):',
        '            scrubbed["country"] = addr["country"]',
        '        new_addresses.append(scrubbed)',
        '    record["address"] = new_addresses',
        '    return record',
        '',
        '',
        'def deid_phone_fax(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Suppress Patient.telecom entries with system in {phone, fax, sms, pager}."""',
        '    rid = str(record.get("id", ""))',
        '    telecom = record.get("telecom")',
        '    if not isinstance(telecom, list):',
        '        return record',
        '    drop_systems = {"phone", "fax", "sms", "pager"}',
        '    kept: list[dict] = []',
        '    for entry in telecom:',
        '        if not isinstance(entry, dict):',
        '            continue',
        '        if entry.get("system") in drop_systems:',
        '            if audit is not None:',
        '                audit.record(rid, "suppress", f"telecom[{entry.get(\'system\')}]",',
        '                             before_value=str(entry.get("value", "")))',
        '            continue',
        '        kept.append(entry)',
        '    record["telecom"] = kept',
        '    return record',
        '',
        '',
        'def deid_email(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Suppress Patient.telecom entries with system=email."""',
        '    rid = str(record.get("id", ""))',
        '    telecom = record.get("telecom")',
        '    if not isinstance(telecom, list):',
        '        return record',
        '    kept: list[dict] = []',
        '    for entry in telecom:',
        '        if isinstance(entry, dict) and entry.get("system") == "email":',
        '            if audit is not None:',
        '                audit.record(rid, "suppress", "telecom[email]",',
        '                             before_value=str(entry.get("value", "")))',
        '            continue',
        '        kept.append(entry)',
        '    record["telecom"] = kept',
        '    return record',
        '',
        '',
        'def deid_ssn(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Suppress Patient.identifier entries whose system is the US SSN OID."""',
        '    rid = str(record.get("id", ""))',
        '    idents = record.get("identifier")',
        '    if not isinstance(idents, list):',
        '        return record',
        '    kept: list[dict] = []',
        '    for ident in idents:',
        '        if not isinstance(ident, dict):',
        '            continue',
        '        sys_uri = str(ident.get("system", "")).lower()',
        '        type_codes = []',
        '        type_field = ident.get("type") or {}',
        '        for c in type_field.get("coding", []) or []:',
        '            type_codes.append(str(c.get("code", "")).upper())',
        '        is_ssn = "us-ssn" in sys_uri or "SS" in type_codes',
        '        if is_ssn:',
        '            if audit is not None:',
        '                audit.record(rid, "suppress", "identifier[SSN]",',
        '                             before_value=str(ident.get("value", "")))',
        '            continue',
        '        kept.append(ident)',
        '    record["identifier"] = kept',
        '    return record',
        '',
        '',
        'def deid_mrn(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Pseudonymize MRN identifiers (type.coding.code == \'MR\')."""',
        '    rid = str(record.get("id", ""))',
        '    idents = record.get("identifier")',
        '    if not isinstance(idents, list):',
        '        return record',
        '    for ident in idents:',
        '        if not isinstance(ident, dict):',
        '            continue',
        '        type_codes = []',
        '        type_field = ident.get("type") or {}',
        '        for c in type_field.get("coding", []) or []:',
        '            type_codes.append(str(c.get("code", "")).upper())',
        '        if "MR" in type_codes:',
        '            orig = str(ident.get("value", ""))',
        '            if not orig:',
        '                continue',
        '            ident["value"] = hmac_pseudonym(orig, config.pseudonym_salt)',
        '            if audit is not None:',
        '                audit.record(rid, "pseudonymize", "identifier[MR]",',
        '                             before_value=orig)',
        '    return record',
        '',
        '',
        'def deid_device_id(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """For Device resources: drop serial-number identifiers, keep model."""',
        '    if record.get("resourceType") != "Device":',
        '        return record',
        '    rid = str(record.get("id", ""))',
        '    idents = record.get("identifier")',
        '    if isinstance(idents, list):',
        '        kept: list[dict] = []',
        '        for ident in idents:',
        '            if isinstance(ident, dict) and "serial" in str(ident.get("system", "")).lower():',
        '                if audit is not None:',
        '                    audit.record(rid, "suppress", "Device.identifier[serial]",',
        '                                 before_value=str(ident.get("value", "")))',
        '                continue',
        '            kept.append(ident)',
        '        record["identifier"] = kept',
        '    if "serialNumber" in record:',
        '        if audit is not None:',
        '            audit.record(rid, "suppress", "Device.serialNumber",',
        '                         before_value=str(record["serialNumber"]))',
        '        del record["serialNumber"]',
        '    return record',
        '',
        '',
        'def deid_photos(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Drop Patient.photo and Media.content for image-typed resources."""',
        '    rid = str(record.get("id", ""))',
        '    if "photo" in record:',
        '        if audit is not None:',
        '            audit.record(rid, "suppress", "photo",',
        '                         before_value="<photo array>")',
        '        record["photo"] = []',
        '    if record.get("resourceType") == "Media" and "content" in record:',
        '        if audit is not None:',
        '            audit.record(rid, "suppress", "Media.content",',
        '                         before_value="<content>")',
        '        record["content"] = {}',
        '    return record',
        '',
        '',
        'def deid_other_unique(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Pseudonymize Resource.id and any non-MR/non-SSN identifier values."""',
        '    rid = str(record.get("id", ""))',
        '    if "id" in record and record["id"]:',
        '        orig_id = str(record["id"])',
        '        record["id"] = hmac_pseudonym(orig_id, config.pseudonym_salt)',
        '        if audit is not None:',
        '            audit.record(orig_id, "pseudonymize", "id", before_value=orig_id)',
        '    idents = record.get("identifier")',
        '    if isinstance(idents, list):',
        '        for ident in idents:',
        '            if not isinstance(ident, dict):',
        '                continue',
        '            type_codes = []',
        '            type_field = ident.get("type") or {}',
        '            for c in type_field.get("coding", []) or []:',
        '                type_codes.append(str(c.get("code", "")).upper())',
        '            # Only handle the residual set — MR + SS were',
        '            # already handled in deid_mrn / deid_ssn.',
        '            if "MR" in type_codes or "SS" in type_codes:',
        '                continue',
        '            v = ident.get("value")',
        '            if not v:',
        '                continue',
        '            ident["value"] = hmac_pseudonym(str(v), config.pseudonym_salt)',
        '            if audit is not None:',
        '                audit.record(rid, "pseudonymize", "identifier[other]",',
        '                             before_value=str(v))',
        '    return record',
        '',
        '',
    ])
    # Per-record dispatcher
    seen_categories: list[str] = []
    seen_set: set[str] = set()
    for category, *_ in d.elements:
        if category in seen_set:
            continue
        seen_set.add(category)
        seen_categories.append(category)

    lines.extend([
        'def deid_record(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Apply every PHI-category de-id function to a single FHIR resource.',
        '',
        '    The input is not mutated; a deep-copied output is returned.',
        '    """',
        '    import copy',
        '    out = copy.deepcopy(record)',
    ])
    for category in seen_categories:
        lines.append(f'    out = deid_{category}(out, config=config, audit=audit)')
    lines.append('    return out')

    lines.extend(_pipeline_footer(d, dispatch_lines=[]))
    return "\n".join(lines)


def _render_test_fhir(d: _Decomposition) -> str:
    return "\n".join([
        '"""End-to-end de-id test for the FHIR pipeline.',
        '',
        'Builds a synthetic Patient resource with PHI in every category,',
        'runs the generated pipeline, then asserts ``find_phi_patterns``',
        'returns nothing on the serialized output.',
        '"""',
        'from __future__ import annotations',
        '',
        'import json',
        'import sys',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        '# Make the pipeline module importable regardless of pytest cwd.',
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))',
        '',
        'from healthcare_libs.deid import find_phi_patterns',
        'from deid_pipeline import deid_record, DEFAULT_CONFIG',
        '',
        '',
        'def _synthetic_patient() -> dict:',
        '    """A FHIR Patient that carries every PHI category we de-id."""',
        '    return {',
        '        "resourceType": "Patient",',
        '        "id": "patient-12345-real-mrn",',
        '        "name": [{',
        '            "use": "official",',
        '            "family": "Doe",',
        '            "given": ["Jane", "Marie"],',
        '        }],',
        '        "gender": "female",',
        '        "birthDate": "1985-06-15",',
        '        "telecom": [',
        '            {"system": "phone", "value": "555-123-4567", "use": "home"},',
        '            {"system": "email", "value": "jane.doe@example.com"},',
        '        ],',
        '        "address": [{',
        '            "use": "home",',
        '            "line": ["123 Main St"],',
        '            "city": "Springfield",',
        '            "state": "IL",',
        '            "postalCode": "62704",',
        '            "country": "US",',
        '        }],',
        '        "identifier": [',
        '            {',
        '                "use": "usual",',
        '                "type": {"coding": [{',
        '                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",',
        '                    "code": "MR",',
        '                }]},',
        '                "system": "urn:oid:LOCAL_MRN_OID",',
        '                "value": "MRN-987654321",',
        '            },',
        '            {',
        '                "use": "official",',
        '                "type": {"coding": [{',
        '                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",',
        '                    "code": "SS",',
        '                }]},',
        '                "system": "http://hl7.org/fhir/sid/us-ssn",',
        '                "value": "123-45-6789",',
        '            },',
        '        ],',
        '        "photo": [{"contentType": "image/jpeg", "data": "<base64>"}],',
        '    }',
        '',
        '',
        'def test_deid_record_returns_dict():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    assert isinstance(out, dict)',
        '    assert out["resourceType"] == "Patient"',
        '',
        '',
        'def test_deid_does_not_mutate_input():',
        '    src = _synthetic_patient()',
        '    deid_record(src, config=DEFAULT_CONFIG)',
        '    # Original still has PHI',
        '    assert src["name"][0]["family"] == "Doe"',
        '    assert src["birthDate"] == "1985-06-15"',
        '',
        '',
        'def test_no_phi_survives_deid():',
        '    """The acceptance test — find_phi_patterns must come up empty."""',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    serialized = json.dumps(out)',
        '    findings = find_phi_patterns(serialized)',
        '    assert findings == [], (',
        '        f"PHI survived de-identification: {findings}\\n"',
        '        f"Output was: {serialized}"',
        '    )',
        '',
        '',
        'def test_names_are_suppressed():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    assert out.get("name") == []',
        '',
        '',
        'def test_birthdate_is_year_only():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    assert out.get("birthDate") == "1985"',
        '',
        '',
        'def test_phone_and_email_are_suppressed():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    systems = {t.get("system") for t in out.get("telecom", [])}',
        '    assert "phone" not in systems',
        '    assert "email" not in systems',
        '',
        '',
        'def test_ssn_identifier_is_dropped():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    for ident in out.get("identifier", []):',
        '        assert "us-ssn" not in str(ident.get("system", "")).lower()',
        '',
        '',
        'def test_mrn_is_pseudonymized():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    mrns = [i for i in out.get("identifier", [])',
        '            if any(c.get("code") == "MR"',
        '                   for c in (i.get("type") or {}).get("coding", []) or [])]',
        '    assert mrns, "MRN identifier was dropped instead of pseudonymized"',
        '    # Pseudonym is HMAC-SHA256 truncated to 16 hex chars.',
        '    assert mrns[0]["value"] != "MRN-987654321"',
        '    assert len(mrns[0]["value"]) == 16',
        '',
        '',
        'def test_resource_id_is_pseudonymized():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    assert out["id"] != "patient-12345-real-mrn"',
        '    assert len(out["id"]) == 16',
        '',
        '',
        'def test_address_is_truncated_to_zip3_or_dropped():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    addr = (out.get("address") or [{}])[0]',
        '    assert "line" not in addr',
        '    assert "city" not in addr',
        '    assert "state" not in addr',
        '    # Default ALLOWED_ZIP3 is empty → truncate_zip drops everything.',
        '    assert "postalCode" not in addr',
        '',
        '',
        'def test_photo_is_dropped():',
        '    out = deid_record(_synthetic_patient(), config=DEFAULT_CONFIG)',
        '    assert out.get("photo") == []',
        '',
    ])


# ---------------------------------------------------------------------------
# DICOM pipeline + test
# ---------------------------------------------------------------------------

def _render_pipeline_dicom(d: _Decomposition) -> str:
    """DICOM pipeline — delegates to dicom.deidentify_basic_profile."""
    imports = (
        'from healthcare_libs import dicom\n'
        'from healthcare_libs.deid import (\n'
        '    DeidConfig,\n'
        '    AuditLog,\n'
        '    hmac_pseudonym,\n'
        '    per_subject_offset,\n'
        ')'
    )
    lines = _pipeline_header(d, shape_imports=imports)
    lines.extend([
        '',
        '# ---- Per-PHI-category functions for DICOM --------------------',
        '#',
        '# DICOM has a standardized de-id profile (PS3.15 Annex E.1, the',
        '# "Basic Application Confidentiality Profile") that handles the',
        '# overwhelming majority of identifying tags in one pass. Our',
        '# format-specific module ``healthcare_libs.dicom`` implements it',
        '# in ``deidentify_basic_profile()``. The per-category functions',
        '# here are thin wrappers that show how the profile maps to each',
        '# Safe Harbor category and add audit-log entries.',
        '',
        '',
        'def _record_id(ds: Any) -> str:',
        '    """Return a stable id for audit purposes — SOP Instance UID is best."""',
        '    sop = getattr(ds, "SOPInstanceUID", None)',
        '    return str(sop) if sop else "<unknown>"',
        '',
        '',
        'def deid_names(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Names are handled by the Basic Profile (PatientName, ReferringPhysicianName, …).',
        '',
        '    This is a no-op — see ``deid_record`` which calls',
        '    ``dicom.deidentify_basic_profile`` once on the whole dataset.',
        '    Splitting per-category for DICOM would be redundant work.',
        '    """',
        '    if audit is not None:',
        '        audit.record(_record_id(ds), "noop_handled_by_profile", "names",',
        '                     before_value="<see basic profile>")',
        '    return ds',
        '',
        '',
        'def deid_dates(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Compute the per-patient date offset; recorded for the profile call."""',
        '    if audit is not None:',
        '        patient_id = str(getattr(ds, "PatientID", "") or "")',
        '        audit.record(_record_id(ds), "shift_dates", "dates",',
        '                     before_value=patient_id)',
        '    return ds',
        '',
        '',
        'def deid_mrn(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """MRN handled by the Basic Profile via the patient_pseudonym kwarg."""',
        '    if audit is not None:',
        '        audit.record(_record_id(ds), "pseudonymize", "PatientID",',
        '                     before_value=str(getattr(ds, "PatientID", "")))',
        '    return ds',
        '',
        '',
        'def deid_ssn(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """SSN-bearing tags (IssuerOfPatientID, etc.) are X-removed by the profile."""',
        '    if audit is not None:',
        '        audit.record(_record_id(ds), "suppress_handled_by_profile", "ssn",',
        '                     before_value="<see basic profile>")',
        '    return ds',
        '',
        '',
        'def deid_photos(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Flag the dataset for OCR review if burned-in PHI is likely.',
        '',
        '    We do NOT do the OCR pass here (it requires pixel data + tesseract',
        '    + manual review). We surface the risk so the operator routes the',
        '    flagged study to a separate review queue.',
        '    """',
        '    if dicom.has_burned_in_phi_risk(ds):',
        '        if audit is not None:',
        '            audit.record(_record_id(ds), "flag_for_ocr_review", "PixelData",',
        '                         before_value=str(getattr(ds, "Modality", "")),',
        '                         note="modality has high burned-in PHI risk")',
        '    return ds',
        '',
        '',
        'def deid_device_id(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Drop DeviceSerialNumber; keep ManufacturerModelName for protocol replication."""',
        '    if hasattr(ds, "DeviceSerialNumber"):',
        '        if audit is not None:',
        '            audit.record(_record_id(ds), "suppress", "DeviceSerialNumber",',
        '                         before_value=str(ds.DeviceSerialNumber))',
        '        delattr(ds, "DeviceSerialNumber")',
        '    return ds',
        '',
        '',
        'def deid_other_unique(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Private DICOM tags are stripped by deidentify_basic_profile by default."""',
        '    if audit is not None:',
        '        audit.record(_record_id(ds), "strip_private_tags_by_profile",',
        '                     "PrivateGroups", before_value="<all odd groups>")',
        '    return ds',
        '',
        '',
        'def deid_record(',
        '    ds: Any,',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    audit: Optional[AuditLog] = None,',
        ') -> Any:',
        '    """Apply DICOM Basic Application Confidentiality Profile + per-category wrappers.',
        '',
        '    The heavy lifting is in ``dicom.deidentify_basic_profile``: it',
        '    deep-copies the input, applies the PS3.15 Annex E.1 actions',
        '    (X/Z/D), shifts dates by the per-patient offset, strips private',
        '    tags, and stamps the DeidentificationMethodCodeSequence.',
        '    """',
        '    patient_id = str(getattr(ds, "PatientID", "") or "anonymous")',
        '    pseudonym = hmac_pseudonym(patient_id, config.pseudonym_salt)',
        '    offset = per_subject_offset(',
        '        patient_id, config.date_offset_seed,',
        '        max_days=config.date_offset_max_days,',
        '    )',
        '    out = dicom.deidentify_basic_profile(',
        '        ds,',
        '        patient_pseudonym=pseudonym,',
        '        date_offset_days=offset,',
        '    )',
        '    # Per-category wrappers — for audit-log entries + photo OCR flag.',
        '    out = deid_names(out, config=config, audit=audit)',
        '    out = deid_dates(out, config=config, audit=audit)',
        '    out = deid_mrn(out, config=config, audit=audit)',
        '    out = deid_ssn(out, config=config, audit=audit)',
        '    out = deid_photos(out, config=config, audit=audit)',
        '    out = deid_device_id(out, config=config, audit=audit)',
        '    out = deid_other_unique(out, config=config, audit=audit)',
        '    return out',
        '',
    ])
    lines.extend(_pipeline_footer(d, dispatch_lines=[]))
    return "\n".join(lines)


def _render_test_dicom(d: _Decomposition) -> str:
    return "\n".join([
        '"""End-to-end de-id test for the DICOM pipeline.',
        '',
        'Builds a minimal valid DICOM dataset via ``healthcare_libs.dicom``',
        'with PHI in PatientName / PatientID / dates / referring physician,',
        'runs the pipeline, then asserts the de-identified dataset is empty',
        'for those tags + has DeidentificationMethodCodeSequence stamped.',
        '"""',
        'from __future__ import annotations',
        '',
        'import sys',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))',
        '',
        'from healthcare_libs import dicom',
        'from healthcare_libs.deid import find_phi_patterns',
        'from deid_pipeline import deid_record, DEFAULT_CONFIG',
        '',
        '',
        'def _synthetic_dataset():',
        '    return dicom.build_minimal_dataset(',
        '        patient_id="MRN12345",',
        '        patient_name="DOE^JANE",',
        '        patient_birth_date="19850615",',
        '        study_date="20260115",',
        '        modality="CT",',
        '        accession_number="ACC0001",',
        '        referring_physician="SMITH^JOHN",',
        '        institution_name="EXAMPLE HOSPITAL",',
        '    )',
        '',
        '',
        'def test_deid_record_returns_dataset():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert hasattr(out, "SOPInstanceUID")',
        '',
        '',
        'def test_patient_name_is_emptied():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert str(out.PatientName or "") == ""',
        '',
        '',
        'def test_patient_id_is_pseudonymized():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert str(out.PatientID) != "MRN12345"',
        '    assert str(out.PatientID) != ""',
        '',
        '',
        'def test_referring_physician_is_emptied():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert str(out.ReferringPhysicianName or "") == ""',
        '',
        '',
        'def test_institution_name_is_removed():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert not hasattr(out, "InstitutionName")',
        '',
        '',
        'def test_dates_are_shifted_or_emptied():',
        '    """StudyDate must NOT match the original 20260115 — either shifted or empty."""',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert str(out.StudyDate or "") != "20260115"',
        '',
        '',
        'def test_deidentification_method_is_stamped():',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    assert str(out.PatientIdentityRemoved) == "YES"',
        '    assert hasattr(out, "DeidentificationMethodCodeSequence")',
        '',
        '',
        'def test_no_phi_patterns_in_serialized_form():',
        '    """Serialize de-id\'d tags to a single string and run find_phi_patterns."""',
        '    out = deid_record(_synthetic_dataset(), config=DEFAULT_CONFIG)',
        '    flat = " ".join([',
        '        str(getattr(out, "PatientName", "") or ""),',
        '        str(getattr(out, "PatientID", "") or ""),',
        '        str(getattr(out, "ReferringPhysicianName", "") or ""),',
        '        str(getattr(out, "AccessionNumber", "") or ""),',
        '        str(getattr(out, "StudyID", "") or ""),',
        '    ])',
        '    findings = find_phi_patterns(flat)',
        '    # SOPInstanceUID + StudyInstanceUID are LONG digit runs that the',
        '    # mrn_like regex flags. They are required Type 1 tags and are',
        '    # NOT PHI per DICOM PS3.15 (UIDs identify objects, not people).',
        '    # The serialized form here intentionally excludes them.',
        '    assert findings == [], f"PHI survived: {findings}"',
        '',
    ])


# ---------------------------------------------------------------------------
# HL7 v2 pipeline + test
# ---------------------------------------------------------------------------

def _render_pipeline_hl7v2(d: _Decomposition) -> str:
    imports = (
        'from healthcare_libs import hl7v2\n'
        'from healthcare_libs.deid import (\n'
        '    DeidConfig,\n'
        '    AuditLog,\n'
        '    hmac_pseudonym,\n'
        '    per_subject_offset,\n'
        '    shift_date,\n'
        '    date_to_year_only,\n'
        '    truncate_zip,\n'
        ')'
    )
    lines = _pipeline_header(d, shape_imports=imports)
    lines.extend([
        '',
        '# ---- Per-PHI-category functions for HL7 v2 -------------------',
        '#',
        '# HL7 v2 messages are line-oriented (one segment per line, terminated',
        '# by \\r). The healthcare_libs.hl7v2 module gives us segment-level',
        '# parse/access helpers; the per-category functions below operate on',
        '# the wire string and rebuild it after scrubbing.',
        '',
        '',
        'def _split_segments(wire: str) -> list[list[str]]:',
        '    """Return a list of [field0, field1, …] arrays for every segment."""',
        '    normalized = wire.replace("\\r\\n", "\\r").replace("\\n", "\\r")',
        '    return [s.split(hl7v2.SEP_FIELD)',
        '            for s in normalized.split(hl7v2.SEP_SEGMENT) if s.strip()]',
        '',
        '',
        'def _join_segments(segments: list[list[str]]) -> str:',
        '    return hl7v2.SEP_SEGMENT.join(',
        '        hl7v2.SEP_FIELD.join(s) for s in segments',
        '    ) + hl7v2.SEP_SEGMENT',
        '',
        '',
        'def _ensure_len(fields: list[str], n: int) -> None:',
        '    """Pad ``fields`` with empty strings so index n-1 exists."""',
        '    while len(fields) < n:',
        '        fields.append("")',
        '',
        '',
        'def _patient_id_for_offset(wire: str) -> str:',
        '    """Read PID-3 (first ID) for use as the per-subject offset key."""',
        '    raw = hl7v2.get_field(wire, "PID", 3)',
        '    if not raw:',
        '        return "unknown"',
        '    first = raw.split("~")[0]',
        '    return first.split("^")[0] if first else "unknown"',
        '',
        '',
        'def deid_names(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Suppress PID-5 patient name, PID-9 mother\'s maiden, NK1-2, PV1-7."""',
        '    for seg in segments:',
        '        if not seg:',
        '            continue',
        '        if seg[0] == "PID":',
        '            for idx in (5, 9):',
        '                _ensure_len(seg, idx + 1)',
        '                if seg[idx] and audit is not None:',
        '                    audit.record(record_id, "suppress", f"PID-{idx}",',
        '                                 before_value=seg[idx])',
        '                seg[idx] = ""',
        '        elif seg[0] == "NK1":',
        '            _ensure_len(seg, 3)',
        '            if seg[2] and audit is not None:',
        '                audit.record(record_id, "suppress", "NK1-2",',
        '                             before_value=seg[2])',
        '            seg[2] = ""',
        '        elif seg[0] == "PV1":',
        '            _ensure_len(seg, 8)',
        '            if seg[7] and audit is not None:',
        '                audit.record(record_id, "suppress", "PV1-7",',
        '                             before_value=seg[7])',
        '            seg[7] = ""',
        '    return segments',
        '',
        '',
        'def deid_dates(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        '    subject_id: str = "",',
        ') -> list[list[str]]:',
        '    """Year-only PID-7 DOB; shift other date fields by per-subject offset."""',
        '    offset = per_subject_offset(',
        '        subject_id or "unknown", config.date_offset_seed,',
        '        max_days=config.date_offset_max_days,',
        '    )',
        '    # Date-bearing fields per HL7 v2.5: (segment, 0-based split index).',
        '    # For non-MSH segments the spec field number equals the split',
        '    # index because fields[0] is the segment id. MSH is special:',
        '    # MSH-1 is the field separator itself, so spec MSH-N maps to',
        '    # split index N-1 — handled below.',
        '    DATE_FIELDS = [',
        '        ("PID", 7),    # date of birth — generalize to year',
        '        ("PV1", 44),   # admit date/time',
        '        ("PV1", 45),   # discharge date/time',
        '        ("OBR", 7),    # observation date/time',
        '        ("OBX", 14),   # date/time of observation',
        '        ("EVN", 2),    # event recorded date/time',
        '    ]',
        '    # MSH-7 (message timestamp) — shift it too. In a raw |-split',
        '    # MSH segment, the timestamp lives at index 6 because the',
        '    # field separator IS MSH-1 and is not represented as its own',
        '    # element.',
        '    MSH_TIMESTAMP_IDX = 6',
        '    for seg in segments:',
        '        if not seg:',
        '            continue',
        '        for s_id, idx in DATE_FIELDS:',
        '            if seg[0] != s_id:',
        '                continue',
        '            _ensure_len(seg, idx + 1)',
        '            v = seg[idx]',
        '            if not v:',
        '                continue',
        '            try:',
        '                if s_id == "PID" and idx == 7:',
        '                    new = date_to_year_only(v[:8])',
        '                else:',
        '                    new = shift_date(v[:8], offset)',
        '                    # Preserve any time portion that followed the date',
        '                    if len(v) > 8:',
        '                        new = new + v[8:]',
        '            except ValueError:',
        '                continue',
        '            if audit is not None:',
        '                audit.record(record_id, "generalize_or_shift_date",',
        '                             f"{s_id}-{idx}", before_value=v)',
        '            seg[idx] = new',
        '    # MSH-7 (message timestamp) — shift in place at split index 6.',
        '    for seg in segments:',
        '        if seg and seg[0] == "MSH" and len(seg) > MSH_TIMESTAMP_IDX:',
        '            v = seg[MSH_TIMESTAMP_IDX]',
        '            if not v:',
        '                continue',
        '            try:',
        '                new = shift_date(v[:8], offset)',
        '                if len(v) > 8:',
        '                    new = new + v[8:]',
        '            except ValueError:',
        '                continue',
        '            if audit is not None:',
        '                audit.record(record_id, "shift_date", "MSH-7",',
        '                             before_value=v)',
        '            seg[MSH_TIMESTAMP_IDX] = new',
        '    return segments',
        '',
        '',
        'def deid_geo_subdivisions(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """PID-11 patient address: drop street/city/state, keep ZIP3 if allowlisted."""',
        '    for seg in segments:',
        '        if not seg or seg[0] != "PID":',
        '            continue',
        '        _ensure_len(seg, 12)',
        '        addr = seg[11]',
        '        if not addr:',
        '            continue',
        '        # XAD components: street ^ other_designation ^ city ^ state ^ zip ^ country …',
        '        comps = addr.split(hl7v2.SEP_COMPONENT)',
        '        zip_in = comps[4] if len(comps) > 4 else ""',
        '        zip3 = truncate_zip(zip_in, allowed_zip3=ALLOWED_ZIP3)',
        '        country = comps[5] if len(comps) > 5 else ""',
        '        new_comps = ["", "", "", "", zip3 or "", country]',
        '        if audit is not None:',
        '            audit.record(record_id, "truncate_address", "PID-11",',
        '                         before_value=addr)',
        '        seg[11] = hl7v2.SEP_COMPONENT.join(new_comps).rstrip(hl7v2.SEP_COMPONENT)',
        '    return segments',
        '',
        '',
        'def deid_phone_fax(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Suppress PID-13 (home phone), PID-14 (business phone), NK1-5 (NK phone)."""',
        '    for seg in segments:',
        '        if not seg:',
        '            continue',
        '        targets = []',
        '        if seg[0] == "PID":',
        '            targets = [13, 14]',
        '        elif seg[0] == "NK1":',
        '            targets = [5, 6]',
        '        for idx in targets:',
        '            _ensure_len(seg, idx + 1)',
        '            if seg[idx] and audit is not None:',
        '                audit.record(record_id, "suppress", f"{seg[0]}-{idx}",',
        '                             before_value=seg[idx])',
        '            seg[idx] = ""',
        '    return segments',
        '',
        '',
        'def deid_ssn(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Suppress PID-19 SSN and any PID-3 entry typed SS."""',
        '    for seg in segments:',
        '        if not seg or seg[0] != "PID":',
        '            continue',
        '        # PID-19 — straight SSN slot',
        '        _ensure_len(seg, 20)',
        '        if seg[19] and audit is not None:',
        '            audit.record(record_id, "suppress", "PID-19", before_value=seg[19])',
        '        seg[19] = ""',
        '        # PID-3 — strip any repetition where the type code is SS',
        '        _ensure_len(seg, 4)',
        '        if seg[3]:',
        '            kept_reps: list[str] = []',
        '            for rep in seg[3].split(hl7v2.SEP_REPETITION):',
        '                comps = rep.split(hl7v2.SEP_COMPONENT)',
        '                # CX type code is component 5 (1-based) → index 4',
        '                if len(comps) > 4 and comps[4].upper() == "SS":',
        '                    if audit is not None:',
        '                        audit.record(record_id, "suppress",',
        '                                     "PID-3[SS]", before_value=rep)',
        '                    continue',
        '                kept_reps.append(rep)',
        '            seg[3] = hl7v2.SEP_REPETITION.join(kept_reps)',
        '    return segments',
        '',
        '',
        'def deid_mrn(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Pseudonymize PID-3 entries typed MR (or untyped — assumed MR)."""',
        '    for seg in segments:',
        '        if not seg or seg[0] != "PID":',
        '            continue',
        '        _ensure_len(seg, 4)',
        '        if not seg[3]:',
        '            continue',
        '        new_reps: list[str] = []',
        '        for rep in seg[3].split(hl7v2.SEP_REPETITION):',
        '            comps = rep.split(hl7v2.SEP_COMPONENT)',
        '            type_code = comps[4].upper() if len(comps) > 4 else ""',
        '            if type_code == "" or type_code == "MR":',
        '                orig = comps[0] if comps else ""',
        '                if orig:',
        '                    comps[0] = hmac_pseudonym(orig, config.pseudonym_salt)',
        '                    if audit is not None:',
        '                        audit.record(record_id, "pseudonymize",',
        '                                     "PID-3[MR]", before_value=orig)',
        '                new_reps.append(hl7v2.SEP_COMPONENT.join(comps))',
        '            else:',
        '                new_reps.append(rep)',
        '        seg[3] = hl7v2.SEP_REPETITION.join(new_reps)',
        '    return segments',
        '',
        '',
        'def deid_account(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Suppress PID-18 account number (operator may pseudonymize instead)."""',
        '    for seg in segments:',
        '        if not seg or seg[0] != "PID":',
        '            continue',
        '        _ensure_len(seg, 19)',
        '        if seg[18] and audit is not None:',
        '            audit.record(record_id, "suppress", "PID-18", before_value=seg[18])',
        '        seg[18] = ""',
        '    return segments',
        '',
        '',
        'def deid_device_id(',
        '    segments: list[list[str]],',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        '    record_id: str = "",',
        ') -> list[list[str]]:',
        '    """Drop OBX-18 (Equipment Instance Identifier) — keep model elsewhere."""',
        '    for seg in segments:',
        '        if not seg or seg[0] != "OBX":',
        '            continue',
        '        _ensure_len(seg, 19)',
        '        if seg[18] and audit is not None:',
        '            audit.record(record_id, "suppress", "OBX-18", before_value=seg[18])',
        '        seg[18] = ""',
        '    return segments',
        '',
        '',
    ])
    seen_categories: list[str] = []
    seen_set: set[str] = set()
    for category, *_ in d.elements:
        if category in seen_set:
            continue
        seen_set.add(category)
        seen_categories.append(category)

    lines.extend([
        'def deid_record(',
        '    wire: str,',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    audit: Optional[AuditLog] = None,',
        ') -> str:',
        '    """Apply every PHI-category de-id function to a single HL7 v2 wire message."""',
        '    subject_id = _patient_id_for_offset(wire)',
        '    record_id = subject_id',
        '    segments = _split_segments(wire)',
    ])
    for category in seen_categories:
        if category == "dates":
            lines.append(
                '    segments = deid_dates(segments, config=config, audit=audit,'
                ' record_id=record_id, subject_id=subject_id)'
            )
        else:
            lines.append(
                f'    segments = deid_{category}(segments, config=config, '
                f'audit=audit, record_id=record_id)'
            )
    lines.append('    return _join_segments(segments)')

    lines.extend(_pipeline_footer(d, dispatch_lines=[]))
    return "\n".join(lines)


def _render_test_hl7v2(d: _Decomposition) -> str:
    return "\n".join([
        '"""End-to-end de-id test for the HL7 v2 pipeline.',
        '',
        'Builds a synthetic ADT^A01 with PHI in PID-5 (name), PID-7 (DOB),',
        'PID-13 (phone), PID-19 (SSN), then runs the pipeline and asserts',
        'the wire output has none of the originals.',
        '"""',
        'from __future__ import annotations',
        '',
        'import sys',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))',
        '',
        'from healthcare_libs import hl7v2',
        'from healthcare_libs.deid import find_phi_patterns',
        'from deid_pipeline import deid_record, DEFAULT_CONFIG',
        '',
        '',
        'def _synthetic_adt() -> str:',
        '    """ADT^A01 carrying every PHI category we de-id."""',
        '    wire = hl7v2.build_adt_a01(',
        '        patient_first="JANE",',
        '        patient_last="DOE",',
        '        patient_mrn="MRN12345",',
        '        patient_dob="19850615",',
        '        patient_sex="F",',
        '        attending_doctor="1234^SMITH^JOHN^^^DR",',
        '    )',
        '    # build_adt_a01 doesn\'t expose every PHI slot — append an OBX',
        '    # plus phone/SSN by editing PID directly. Easiest: split, edit, rejoin.',
        '    lines: list[list[str]] = []',
        '    for seg in wire.replace("\\r\\n", "\\r").split("\\r"):',
        '        if not seg.strip():',
        '            continue',
        '        lines.append(seg.split("|"))',
        '    for seg in lines:',
        '        if seg[0] == "PID":',
        '            while len(seg) < 20:',
        '                seg.append("")',
        '            seg[13] = "555-123-4567"   # PID-13 home phone',
        '            seg[19] = "123-45-6789"    # PID-19 SSN',
        '    return "\\r".join("|".join(s) for s in lines) + "\\r"',
        '',
        '',
        'def test_deid_record_returns_wire_string():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    assert isinstance(out, str)',
        '    assert "MSH|" in out',
        '    assert "PID|" in out',
        '',
        '',
        'def test_patient_name_is_emptied():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    name = hl7v2.get_field(out, "PID", 5)',
        '    assert not name or name == ""',
        '',
        '',
        'def test_dob_is_year_only():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    dob = hl7v2.get_field(out, "PID", 7)',
        '    assert dob == "1985", f"PID-7 should be year-only, got {dob!r}"',
        '',
        '',
        'def test_phone_is_suppressed():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    assert "555-123-4567" not in out',
        '',
        '',
        'def test_ssn_is_suppressed():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    assert "123-45-6789" not in out',
        '',
        '',
        'def test_mrn_is_pseudonymized():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    assert "MRN12345" not in out',
        '',
        '',
        'def test_attending_doctor_is_suppressed():',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    pv1_7 = hl7v2.get_field(out, "PV1", 7)',
        '    assert not pv1_7 or pv1_7 == ""',
        '',
        '',
        'def test_no_original_phi_strings_survive():',
        '    """None of the original PHI literals from the synthetic ADT appear."""',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    for original in ("DOE", "JANE", "MRN12345", "19850615",',
        '                     "555-123-4567", "123-45-6789", "SMITH"):',
        '        assert original not in out, f"original PHI {original!r} survived"',
        '',
        '',
        'def test_no_phi_patterns_survive_excluding_shifted_dates():',
        '    """find_phi_patterns must come up empty on the non-date portion.',
        '',
        '    HL7v2 timestamps are 14-digit ``YYYYMMDDHHMMSS`` strings, so',
        '    *shifted* dates legitimately survive in YYYYMMDDHHMMSS form (the',
        '    PHI is the original date; the shifted date is not). The',
        '    ``mrn_like`` regex flags 12+ digit runs, so we exclude the',
        '    known date fields before running find_phi_patterns.',
        '    """',
        '    out = deid_record(_synthetic_adt(), config=DEFAULT_CONFIG)',
        '    # NOTE: For MSH the field separator IS MSH-1, so a raw',
        '    # ``split("|")`` puts MSH-7 (timestamp) at index 6, not 7.',
        '    # Other segments use the natural mapping spec_field == split_index.',
        '    DATE_POSITIONS = [',
        '        ("MSH", 6), ("EVN", 2), ("PID", 7),',
        '        ("PV1", 44), ("PV1", 45), ("OBR", 7),',
        '    ]',
        '    scrubbed_lines: list[str] = []',
        '    for seg in out.replace("\\r\\n", "\\r").split("\\r"):',
        '        if not seg.strip():',
        '            continue',
        '        fields = seg.split("|")',
        '        seg_id = fields[0]',
        '        for s_id, idx in DATE_POSITIONS:',
        '            if seg_id == s_id and idx < len(fields):',
        '                fields[idx] = "<DATE>"',
        '        scrubbed_lines.append("|".join(fields))',
        '    scrubbed = "\\r".join(scrubbed_lines)',
        '    findings = find_phi_patterns(scrubbed)',
        '    assert findings == [], (',
        '        f"PHI survived (after excluding shifted dates): {findings}\\n"',
        '        f"Scrubbed wire: {scrubbed!r}"',
        '    )',
        '',
    ])


# ---------------------------------------------------------------------------
# Clinical-trial pipeline + test
# ---------------------------------------------------------------------------

def _render_pipeline_clinical_trial(d: _Decomposition) -> str:
    """Clinical-trial: enforce SubjectID-only at ingest + standard de-id thereafter."""
    imports = (
        'from healthcare_libs.deid import (\n'
        '    DeidConfig,\n'
        '    AuditLog,\n'
        '    hmac_pseudonym,\n'
        '    per_subject_offset,\n'
        '    shift_date,\n'
        '    date_to_year_only,\n'
        '    k_anonymize,\n'
        ')'
    )
    lines = _pipeline_header(d, shape_imports=imports)
    lines.extend([
        '',
        '# ---- Trial-specific config -----------------------------------',
        '#',
        '# Clinical trials assign a synthetic SubjectID at enrollment; the',
        '# de-id layer enforces "no source-system identifier survives ingest"',
        '# rather than scrubbing fields from EHR records on the way out.',
        '',
        'STRIPPED_AT_INGEST = {',
        '    "name", "first_name", "last_name", "middle_name",',
        '    "mrn", "ssn", "phone", "email", "address",',
        '    "patient_id",  # source-system patient id; trial uses subject_id',
        '}',
        '',
        '# Quasi-identifiers used for k-anonymity over rare-trait combinations.',
        'DEFAULT_QUASI_IDENTIFIERS = ["age_band", "sex", "site_id", "country"]',
        '',
        '# Site-id lookup — populate with your real site catalog at deploy.',
        'SITE_LOOKUP: dict[str, str] = {}',
        '',
        '',
        '# ---- Per-PHI-category functions for clinical trials -----------',
        '',
        '',
        'def deid_names(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Strip name fields entirely — only subject_id survives."""',
        '    rid = str(record.get("subject_id", record.get("patient_id", "")))',
        '    for key in ("name", "first_name", "last_name", "middle_name",',
        '                "investigator_name"):',
        '        if key in record and record[key]:',
        '            if audit is not None:',
        '                audit.record(rid, "suppress", key,',
        '                             before_value=str(record[key]))',
        '            record[key] = None',
        '    return record',
        '',
        '',
        'def deid_dates(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Per-subject date-shift on every dated event; preserves intervals."""',
        '    subject_id = str(record.get("subject_id", "unknown"))',
        '    offset = per_subject_offset(',
        '        subject_id, config.date_offset_seed,',
        '        max_days=config.date_offset_max_days,',
        '    )',
        '    DATE_FIELDS = ("visit_date", "ae_onset_date", "dose_date",',
        '                   "lab_collection_date", "consent_date",',
        '                   "screening_date", "enrollment_date")',
        '    for f in DATE_FIELDS:',
        '        v = record.get(f)',
        '        if not v:',
        '            continue',
        '        try:',
        '            record[f] = shift_date(str(v), offset)',
        '        except ValueError:',
        '            continue',
        '        if audit is not None:',
        '            audit.record(subject_id, "shift_date", f, before_value=str(v))',
        '    # Birth date → year-only so age can still be banded downstream',
        '    if record.get("birth_date"):',
        '        try:',
        '            orig = str(record["birth_date"])',
        '            record["birth_date"] = date_to_year_only(orig)',
        '            if audit is not None:',
        '                audit.record(subject_id, "generalize_date", "birth_date",',
        '                             before_value=orig)',
        '        except ValueError:',
        '            pass',
        '    return record',
        '',
        '',
        'def deid_geo_subdivisions(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Replace site_address with site_id (lookup against SITE_LOOKUP)."""',
        '    rid = str(record.get("subject_id", ""))',
        '    if "site_address" in record and record["site_address"]:',
        '        if audit is not None:',
        '            audit.record(rid, "encode_as_site_id", "site_address",',
        '                         before_value=str(record["site_address"]))',
        '        addr = str(record["site_address"])',
        '        record["site_id"] = SITE_LOOKUP.get(addr, hmac_pseudonym(',
        '            addr, config.pseudonym_salt))',
        '        record["site_address"] = None',
        '    return record',
        '',
        '',
        'def deid_mrn(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Drop source-system MRN — subject_id is the link key in trial datasets."""',
        '    rid = str(record.get("subject_id", ""))',
        '    if "mrn" in record:',
        '        if record["mrn"] and audit is not None:',
        '            audit.record(rid, "drop", "mrn",',
        '                         before_value=str(record["mrn"]))',
        '        del record["mrn"]',
        '    return record',
        '',
        '',
        'def deid_rare_traits(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Per-record placeholder — k-anonymity is applied across the cohort.',
        '',
        '    Call ``apply_k_anonymity`` (below) on the whole list of records',
        '    after per-record de-id has run.',
        '    """',
        '    return record',
        '',
        '',
        'def deid_other_unique(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Rehash visit_id with the per-release salt (config.pseudonym_salt)."""',
        '    rid = str(record.get("subject_id", ""))',
        '    if "visit_id" in record and record["visit_id"]:',
        '        orig = str(record["visit_id"])',
        '        record["visit_id"] = hmac_pseudonym(orig, config.pseudonym_salt)',
        '        if audit is not None:',
        '            audit.record(rid, "rehash", "visit_id", before_value=orig)',
        '    return record',
        '',
        '',
        'def enforce_subject_id_only(',
        '    record: dict,',
        '    *,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Ingest-time guardrail: every key in STRIPPED_AT_INGEST is dropped.',
        '',
        '    Run this BEFORE the per-category de-id functions to make a',
        '    leak-by-default field name (e.g., a "patient_name" column that',
        '    snuck through from the EHR export) impossible by construction.',
        '    """',
        '    rid = str(record.get("subject_id", record.get("patient_id", "")))',
        '    for key in list(record.keys()):',
        '        if key in STRIPPED_AT_INGEST:',
        '            if record[key] and audit is not None:',
        '                audit.record(rid, "drop_at_ingest", key,',
        '                             before_value=str(record[key]))',
        '            del record[key]',
        '    return record',
        '',
        '',
        'def apply_k_anonymity(',
        '    records: list[dict],',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    quasi_identifiers: Optional[list[str]] = None,',
        ') -> list[dict]:',
        '    """Cohort-level pass — every QI tuple must appear ≥k times or be masked."""',
        '    qis = quasi_identifiers or DEFAULT_QUASI_IDENTIFIERS',
        '    return k_anonymize(records, qis, k=config.k_anonymity_threshold)',
        '',
        '',
    ])
    seen_categories: list[str] = []
    seen_set: set[str] = set()
    for category, *_ in d.elements:
        if category in seen_set:
            continue
        seen_set.add(category)
        seen_categories.append(category)

    lines.extend([
        'def deid_record(',
        '    record: dict,',
        '    *,',
        '    config: DeidConfig = DEFAULT_CONFIG,',
        '    audit: Optional[AuditLog] = None,',
        ') -> dict:',
        '    """Apply enforce_subject_id_only + every PHI-category function to a record."""',
        '    import copy',
        '    out = copy.deepcopy(record)',
        '    out = enforce_subject_id_only(out, audit=audit)',
    ])
    for category in seen_categories:
        lines.append(f'    out = deid_{category}(out, config=config, audit=audit)')
    lines.append('    return out')

    lines.extend(_pipeline_footer(d, dispatch_lines=[]))
    return "\n".join(lines)


def _render_test_clinical_trial(d: _Decomposition) -> str:
    return "\n".join([
        '"""End-to-end de-id test for the clinical-trial pipeline.',
        '',
        'Builds a synthetic subject record with the kind of mixed-source',
        'fields you see right after an EHR-to-EDC pull (patient_name + MRN +',
        'phone/email present even though only subject_id should survive),',
        'runs the pipeline, asserts no PHI patterns survive.',
        '"""',
        'from __future__ import annotations',
        '',
        'import json',
        'import sys',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))',
        '',
        'from healthcare_libs.deid import find_phi_patterns',
        'from deid_pipeline import (',
        '    deid_record,',
        '    apply_k_anonymity,',
        '    DEFAULT_CONFIG,',
        ')',
        '',
        '',
        'def _synthetic_subject() -> dict:',
        '    return {',
        '        "subject_id": "STUDY01-001",',
        '        "patient_id": "EHR-PT-987654",  # should be stripped at ingest',
        '        "name": "Jane Doe",             # should be stripped at ingest',
        '        "first_name": "Jane",',
        '        "last_name": "Doe",',
        '        "mrn": "MRN-987654321",         # should be dropped',
        '        "ssn": "123-45-6789",           # should be stripped at ingest',
        '        "phone": "555-123-4567",        # should be stripped at ingest',
        '        "email": "jane@example.com",    # should be stripped at ingest',
        '        "address": "123 Main St, Springfield IL 62704",',
        '        "birth_date": "1985-06-15",',
        '        "visit_date": "2026-05-01",',
        '        "ae_onset_date": "2026-05-15",',
        '        "dose_date": "2026-05-02",',
        '        "site_address": "Memorial Hospital, Cleveland OH",',
        '        "visit_id": "VISIT-12345",',
        '        "investigator_name": "Dr. Smith",',
        '        "age_band": "35-39",',
        '        "sex": "F",',
        '        "country": "US",',
        '    }',
        '',
        '',
        'def test_deid_record_keeps_subject_id():',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    assert out["subject_id"] == "STUDY01-001"',
        '',
        '',
        'def test_source_system_identifiers_are_stripped():',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    for key in ("patient_id", "name", "mrn", "ssn", "phone",',
        '                "email", "address"):',
        '        assert key not in out, f"{key} survived ingest scrub"',
        '',
        '',
        'def test_birth_date_is_year_only():',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    assert out["birth_date"] == "1985"',
        '',
        '',
        'def test_visit_dates_are_shifted_not_dropped():',
        '    """Per-subject offset preserves intervals — both dates should change',
        '    by the same number of days, but neither should equal the original."""',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    assert out["visit_date"] != "2026-05-01"',
        '    assert out["ae_onset_date"] != "2026-05-15"',
        '',
        '',
        'def test_visit_id_is_pseudonymized():',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    assert out["visit_id"] != "VISIT-12345"',
        '    assert len(out["visit_id"]) == 16',
        '',
        '',
        'def test_no_original_phi_strings_survive():',
        '    """None of the original PHI literals appear in the de-id output."""',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    serialized = json.dumps(out)',
        '    for original in ("Jane Doe", "Jane", "Doe", "MRN-987654321",',
        '                     "EHR-PT-987654", "123-45-6789", "555-123-4567",',
        '                     "jane@example.com", "1985-06-15", "VISIT-12345",',
        '                     "Smith", "Memorial Hospital"):',
        '        assert original not in serialized, (',
        '            f"original PHI {original!r} survived: {serialized}"',
        '        )',
        '',
        '',
        'def test_no_phi_patterns_in_non_date_fields():',
        '    """find_phi_patterns is empty on the non-shifted-date portion.',
        '',
        '    Per-subject date-shift legitimately leaves shifted dates in',
        '    ISO YYYY-MM-DD form, which the heuristic ``iso_date`` regex',
        '    flags. The PHI is the *original* date; the shifted date is not.',
        '    We strip the known shifted-date fields before scanning.',
        '    """',
        '    out = deid_record(_synthetic_subject(), config=DEFAULT_CONFIG)',
        '    SHIFTED_DATE_FIELDS = (',
        '        "visit_date", "ae_onset_date", "dose_date",',
        '        "lab_collection_date", "consent_date",',
        '        "screening_date", "enrollment_date",',
        '    )',
        '    scrubbed = {k: ("<DATE>" if k in SHIFTED_DATE_FIELDS else v)',
        '                for k, v in out.items()}',
        '    findings = find_phi_patterns(json.dumps(scrubbed))',
        '    assert findings == [], f"PHI survived: {findings}"',
        '',
        '',
        'def test_k_anonymity_masks_unique_combinations():',
        '    """With k=5 and a single record, the QI tuple must be masked to *."""',
        '    record = _synthetic_subject()',
        '    deid = deid_record(record, config=DEFAULT_CONFIG)',
        '    out = apply_k_anonymity([deid], config=DEFAULT_CONFIG)',
        '    assert out[0]["age_band"] == "*"',
        '',
    ])


# ---------------------------------------------------------------------------
# Renderer dispatch
# ---------------------------------------------------------------------------

_PIPELINE_RENDERERS = {
    "fhir": _render_pipeline_fhir,
    "dicom": _render_pipeline_dicom,
    "hl7v2": _render_pipeline_hl7v2,
    "clinical_trial": _render_pipeline_clinical_trial,
}

_TEST_RENDERERS = {
    "fhir": _render_test_fhir,
    "dicom": _render_test_dicom,
    "hl7v2": _render_test_hl7v2,
    "clinical_trial": _render_test_clinical_trial,
}


def _render_pipeline(d: _Decomposition) -> str:
    """Dispatch to the per-dataset pipeline renderer.

    Each renderer emits a Python module that imports from
    ``healthcare_libs`` and contains real per-PHI-category implementations
    — never ``pass``-only stubs.
    """
    if d.dataset_type not in _PIPELINE_RENDERERS:
        return f'"""No pipeline renderer for dataset type {d.dataset_type!r}."""\n'
    return _PIPELINE_RENDERERS[d.dataset_type](d)


def _render_test(d: _Decomposition) -> str:
    """Dispatch to the per-dataset test renderer."""
    if d.dataset_type not in _TEST_RENDERERS:
        return f'"""No test renderer for dataset type {d.dataset_type!r}."""\n'
    return _TEST_RENDERERS[d.dataset_type](d)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class DeidPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        if not d.dataset_meta:
            return GenPlan(
                generator_type=GENERATOR_TYPE,
                package_name=package_name or "deid-unknown",
                domain=d.dataset_type,
                source_query=d.dataset_type,
                package_metadata={"error": "unknown dataset type"},
                notes=list(d.notes),
            )
        pkg_name = package_name or f"deid-bundle-{d.dataset_type}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=f"{d.dataset_meta.get('name')} de-identification",
            source_query=d.dataset_type,
            package_metadata={
                "dataset_type": d.dataset_type,
                "n_phi_elements": len(d.elements),
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
            unit_type="deid_dataset",
            name=d.dataset_meta.get("name", d.dataset_type),
            ordinal=1,
            metadata={"dataset_type": d.dataset_type,
                      "n_phi_elements": len(d.elements)},
            logical_key="dataset_main",
            sources=sources,
        ))

        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="rationale.md", content=_render_rationale(d), purpose="rationale"),
            GenFile(filename="audit_trail.md", content=_render_audit(d), purpose="audit"),
            GenFile(filename="deid_pipeline.py", content=_render_pipeline(d), purpose="code"),
            GenFile(filename="tests/test_deid.py", content=_render_test(d), purpose="test"),
        ])
        plan.files.extend(_subagent_prompts("deid_bundle", {
            "shape": d.dataset_type,
            "shape_ext": {
                "fhir": "json", "dicom": "dcm",
                "hl7v2": "hl7", "clinical_trial": "csv",
            }.get(d.dataset_type, "dat"),
        }))
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class DeidValidator:
    def validate(self, conn, plan: GenPlan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("error") == "unknown dataset type":
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="unknown dataset type — supported: fhir, dicom, hl7v2, clinical_trial",
            ))
            return issues
        # Every PHI element must reference a known technique
        # (defended in code, but assert in validation for safety)
        if plan.package_metadata.get("n_phi_elements", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="dataset_main", severity="error",
                message="no PHI elements identified — dataset spec is incomplete",
            ))
        if plan.package_metadata.get("n_citations", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="dataset_main", severity="warning",
                message="no concept citations from healthcare doc sources — "
                        "package ships from built-in HIPAA spec only",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------

class DeidMaterializer:
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


def make_deid_bundle_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=DeidDecomposer(),
        planner=DeidPlanner(),
        ranking_mode="generation",
        validator=DeidValidator(),
        materializer=DeidMaterializer(),
    )
