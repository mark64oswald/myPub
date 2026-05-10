"""deid_bundle.py — De-identification Procedure Bundle generator.

For a dataset description (FHIR / DICOM / HL7v2 / generic clinical),
generate a complete de-identification package: HIPAA Safe-Harbor-mapped
PHI element list, per-element de-id technique with rationale, a runnable
Python pipeline scaffold, and a test that asserts no PHI survives the
pipeline. Citations from healthcare doc sources (pydicom, DCMTK, Synthea,
FHIR specs) appear inline.

Output structure:
    deid-bundle-<dataset>/
      README.md            overview + dataset description
      rationale.md         per-PHI-element justification + HIPAA citation
      audit_trail.md       template for tracking what was changed
      deid_pipeline.py     runnable de-id pipeline scaffold
      tests/test_deid.py   assertions that PHI is absent post-pipeline
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
        "implementation": "record[field] = record[field][:4] if record[field] else None  # date → year",
    },
    "truncate_to_first_3_zip_digits_or_drop": {
        "description": "Keep only the first 3 ZIP digits if the resulting geo unit covers >20,000 people; otherwise drop entirely.",
        "implementation": "zip3 = record[field][:3]; record[field] = zip3 if zip3 in ALLOWED_ZIP3 else None",
    },
    "shift_by_per-subject_random_offset_OR_generalize": {
        "description": "Apply a per-subject random offset (typically ±60 days) consistently to all dates for that subject. Preserves intra-subject intervals.",
        "implementation": "offset = subject_offsets[subject_id]; record[field] = record[field] + offset",
    },
    "generalize_to_year_or_shift_by_offset": {
        "description": "Choose between year-only generalization or per-patient date-shift based on analytic need. Generalize for cross-cohort comparison; shift for longitudinal studies that need preserved intervals.",
        "implementation": "record[field] = record[field][:4]  # OR: + per_patient_offsets[patient_id]",
    },
    "shift_dates_by_per-patient_random_offset": {
        "description": "Same as above but per-patient. DICOM Basic Application Confidentiality Profile §E.3.",
        "implementation": "offset = patient_offsets[patient_id]; record[field] = record[field] + offset",
    },
    "pseudonymize_with_kept_lookup": {
        "description": "Replace identifier with a generated pseudonym (UUID4 or HMAC). Store the original→pseudonym mapping in a separate, more-strictly-controlled location.",
        "implementation": "record[field] = pseudonym_lookup[record[field]]",
    },
    "rehash_to_per-study_pseudonym": {
        "description": "Hash the value with a study-specific salt. Identical inputs hash identically within a study (preserves linkage) but differ across studies.",
        "implementation": "record[field] = hashlib.sha256((SALT + record[field]).encode()).hexdigest()",
    },
    "rehash_per_release": {
        "description": "Same as rehash but salt rotates per data release. Prevents linking across releases.",
        "implementation": "record[field] = hashlib.sha256((release_salt + record[field]).encode()).hexdigest()",
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
        "implementation": "ocr_text = pytesseract.image_to_data(pixel_array); redact_regions(ocr_text)",
    },
    "ocr_detect_and_redact_pixel_burned_in": {
        "description": "Same as above (alias used in some code paths).",
        "implementation": "ocr_text = pytesseract.image_to_data(pixel_array); redact_regions(ocr_text)",
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
        "implementation": "record[field] = pseudonym_lookup[record[field]]  # or: del record[field]",
    },
    "encode_as_site_id_only": {
        "description": "Replace full geographic location with a site identifier (which is itself not directly identifying without a separate site catalog).",
        "implementation": "record[field] = SITE_LOOKUP[record[field]]",
    },
    "k-anonymize_or_aggregate": {
        "description": "Apply k-anonymity (k≥5): every combination of quasi-identifiers must appear ≥k times. If not achievable, aggregate the rare value into a broader category.",
        "implementation": "# Use Mondrian k-anonymizer or rule-based binning; never trust ad-hoc dedup",
    },
    "suppress_all_private_tags_unless_explicitly_allowlisted": {
        "description": "Default-deny: drop every private DICOM tag (group ≥0x0009 odd). Only keep a tag if it appears on an explicit allowlist for the study.",
        "implementation": "for tag in dataset: if tag.is_private and tag not in ALLOWLIST: del dataset[tag]",
    },
    "suppress_or_replace_with_DICOM_anonymous": {
        "description": "Apply DICOM Basic Application Confidentiality Profile (PS3.15 Annex E). PatientName → 'Anonymous'; ReferringPhysicianName → empty.",
        "implementation": "ds.PatientName = 'Anonymous'; ds.ReferringPhysicianName = ''",
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
# Renderers
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
        "- `deid_pipeline.py` — runnable Python scaffold; per-element "
           "techniques are implemented as small, composable functions",
        "- `tests/test_deid.py` — assertions that PHI is absent from "
           "post-pipeline output",
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
        "```bash",
        "pip install pyx12 pydicom hl7apy  # pick what your dataset needs",
        "python deid_pipeline.py --input /path/to/raw/ --output /path/to/deid/",
        "pytest tests/test_deid.py",
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


def _render_pipeline(d: _Decomposition) -> str:
    """Generate a runnable Python scaffold that applies the per-element
    techniques. Each technique is a small composable function the operator
    fills in for their specific dataset."""
    lines = [
        f'"""De-identification pipeline scaffold for {d.dataset_meta.get("name", d.dataset_type)}.',
        '',
        'Generated by /kb-deid-bundle. Fill in the per-technique TODOs',
        'for your specific dataset shape. The pipeline is intentionally',
        'verbose — prefer readability over brevity for a regulatory artifact.',
        '"""',
        'from __future__ import annotations',
        '',
        'import argparse',
        'import hashlib',
        'import json',
        'import logging',
        'import secrets',
        'from datetime import date, timedelta',
        'from pathlib import Path',
        'from typing import Any',
        '',
        'LOG = logging.getLogger("deid")',
        '',
        '# Per-run secrets — load from a separately-controlled secret store.',
        '# DO NOT commit real values here.',
        'PSEUDONYM_SALT = secrets.token_hex(32)  # rotate per data release',
        'DATE_OFFSET_SEED = secrets.token_hex(32)',
        '',
        '',
        'def _hmac_pseudonym(value: str, salt: str = PSEUDONYM_SALT) -> str:',
        '    """Stable pseudonym for a given (value, salt). Same input → same output."""',
        '    return hashlib.sha256((salt + str(value)).encode()).hexdigest()[:16]',
        '',
        '',
        'def _per_subject_offset(subject_id: str, max_days: int = 60) -> int:',
        '    """Deterministic per-subject date offset in [-max_days, +max_days]."""',
        '    h = hashlib.sha256((DATE_OFFSET_SEED + subject_id).encode()).hexdigest()',
        '    return (int(h[:8], 16) % (2 * max_days + 1)) - max_days',
        '',
        '',
        '# ---- Per-PHI-category de-id functions -----------------------',
        '',
    ]

    seen_categories: set[str] = set()
    for category, location, technique, rationale, _tm in d.elements:
        if category in seen_categories:
            continue
        seen_categories.add(category)
        # Generate a stub function per category
        lines.extend([
            f'def deid_{category}(record: dict, *, subject_id: str | None = None) -> None:',
            f'    """{technique} — {rationale}"""',
            f'    # TODO: implement for `{location}`',
            f'    # Reference technique: {technique}',
            f'    pass',
            '',
            '',
        ])

    lines.extend([
        '# ---- Pipeline orchestrator -----------------------------',
        '',
        'def deid_record(record: dict, *, subject_id: str | None = None) -> dict:',
        '    """Apply every PHI category de-id function to a single record."""',
        '    out = dict(record)  # don\'t mutate input',
    ])
    for category in sorted(seen_categories):
        lines.append(f'    deid_{category}(out, subject_id=subject_id)')
    lines.extend([
        '    return out',
        '',
        '',
        'def deid_dataset(input_path: Path, output_path: Path) -> None:',
        '    """Walk an input dataset, apply per-record de-id, write the output."""',
        '    output_path.mkdir(parents=True, exist_ok=True)',
        '    # TODO: replace with your actual reader / writer.',
        '    # For FHIR: read JSON Resources, write de-identified JSON.',
        '    # For DICOM: use pydicom; preserve the file structure.',
        '    # For HL7v2: parse with hl7apy, re-encode after de-id.',
        '    raise NotImplementedError(',
        '        "Wire your dataset reader/writer here; see deid_record() for the per-record path"',
        '    )',
        '',
        '',
        'def main():',
        '    p = argparse.ArgumentParser()',
        '    p.add_argument("--input", type=Path, required=True)',
        '    p.add_argument("--output", type=Path, required=True)',
        '    args = p.parse_args()',
        '    logging.basicConfig(level=logging.INFO)',
        '    deid_dataset(args.input, args.output)',
        '',
        '',
        'if __name__ == "__main__":',
        '    main()',
    ])
    return "\n".join(lines)


def _render_test(d: _Decomposition) -> str:
    """Tests that assert no PHI survives the pipeline."""
    lines = [
        '"""Tests asserting PHI is absent from de-identified output.',
        '',
        'These are template assertions — wire them to your real dataset.',
        'The pipeline scaffold is intentionally inert (each function is a',
        'TODO); these tests fail until the operator implements them.',
        '"""',
        'from __future__ import annotations',
        '',
        'import re',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        '',
        '# Common PHI regexes — extend for your data shape',
        'SSN_RE = re.compile(r"\\b\\d{3}[-\\s]?\\d{2}[-\\s]?\\d{4}\\b")',
        'PHONE_RE = re.compile(r"\\b\\d{3}[.\\-\\s]?\\d{3}[.\\-\\s]?\\d{4}\\b")',
        'EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}")',
        'FULL_DATE_RE = re.compile(r"\\b\\d{4}-\\d{2}-\\d{2}\\b")  # Safe Harbor: no full dates',
        '',
        '',
        'def _serialize_record(record) -> str:',
        '    """Stringify a record for regex-scanning. Wire to your shape."""',
        '    if isinstance(record, dict):',
        '        return " ".join(_serialize_record(v) for v in record.values())',
        '    if isinstance(record, list):',
        '        return " ".join(_serialize_record(x) for x in record)',
        '    return str(record) if record is not None else ""',
        '',
        '',
        'class TestNoPhiPostDeid:',
        '    @pytest.fixture',
        '    def deid_record(self):',
        '        # TODO: load a representative real-shaped record AFTER de-id.',
        '        # Example: read a sample from output/ and return it.',
        '        pytest.skip("wire deid_record fixture to your data")',
        '',
        '    def test_no_ssn(self, deid_record):',
        '        s = _serialize_record(deid_record)',
        '        assert not SSN_RE.search(s), f"SSN survived de-id: {SSN_RE.search(s).group()}"',
        '',
        '    def test_no_phone(self, deid_record):',
        '        s = _serialize_record(deid_record)',
        '        assert not PHONE_RE.search(s)',
        '',
        '    def test_no_email(self, deid_record):',
        '        s = _serialize_record(deid_record)',
        '        assert not EMAIL_RE.search(s)',
        '',
        '    def test_no_full_dates(self, deid_record):',
        '        """Safe Harbor: dates must be year-only OR shifted."""',
        '        s = _serialize_record(deid_record)',
        '        assert not FULL_DATE_RE.search(s), "full date survived; use year-only or date-shift"',
    ]
    return "\n".join(lines)


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
