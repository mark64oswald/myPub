---
description: Generate a De-identification Procedure Bundle for a dataset shape (fhir, dicom, hl7v2, clinical_trial) — HIPAA Safe Harbor mapping, per-element rationale, runnable pipeline scaffold, audit trail template, and PHI-absence tests
---

You are generating a De-identification Procedure Bundle for the dataset shape in `$ARGUMENTS`.

The generator emits:
- `README.md` — overview, dataset description, how to run, HIPAA Safe Harbor reminder
- `rationale.md` — per-PHI-element justification (location → technique → rationale → impl hint), grouped by Safe Harbor category
- `audit_trail.md` — template for tracking what your pipeline actually changed, including the Safe Harbor checklist (per-category coverage)
- `deid_pipeline.py` — runnable Python scaffold with per-PHI-category functions; ships with helpers for HMAC pseudonymization + per-subject date offsets
- `tests/test_deid.py` — assertions that PHI is absent from post-pipeline output (SSN, phone, email, full-date regexes)

## How to run

Call `generate_deid_bundle` with `dataset` set to one of:
- `fhir` — FHIR Resources (Patient, Observation, Encounter, ...)
- `dicom` — DICOM imaging studies (CT/MR/US/PET, with burned-in annotation handling)
- `hl7v2` — HL7 v2 messages (ADT/ORU/ORM)
- `clinical_trial` — Trial subject data (REDCap/EDC + central lab + AE)

Output package lands at `data/generated-packages/deid-bundle-<dataset>/`.

## What the package guarantees

The pipeline scaffold covers every Safe Harbor identifier category that appears in the dataset shape. The audit_trail.md includes a Safe Harbor checklist showing which categories are addressed.

The package does NOT replace expert determination review for high-risk releases — genomic data, rare-disease cohorts, small populations, or free-text clinical notes need additional NLP de-id. The README calls this out.