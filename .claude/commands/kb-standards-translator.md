---
description: Generate a healthcare standards Translator package mapping one standard to another (HL7v2 ADT → FHIR Patient/Encounter; HL7v2 ORU → FHIR Observation; X12 837P → FHIR Claim; X12 835 → FHIR ClaimResponse; DICOM Series → FHIR ImagingStudy)
---

You are generating a healthcare standards Translator for the source-target pair in `$ARGUMENTS`.

The generator emits:
- `README.md` — intent + how to run + caveats (lossy fields, code translation assumptions, reference resolution)
- `mapping.md` — field-by-field source→target table with per-field transform pattern (`direct`, `lookup`, `split`, `concat`, `code-translation`, `compute`, `lossy`, `drop`) plus a transform-legend section
- `transformer.py` — Python skeleton with one TODO function per source element. Each function carries the source-path, target-path, transform pattern, and notes inline as docstring
- `tests/test_mapping.py` — round-trip + spec-conformance test scaffolds

## Supported mapping pairs

- `hl7v2 adt^a01 to fhir patient encounter` → ADT admission → FHIR Patient + Encounter
- `hl7v2 oru^r01 to fhir observation` → Lab result message → FHIR Observation + DiagnosticReport
- `x12 837p to fhir claim` → Professional claim → FHIR Claim
- `x12 835 to fhir claimresponse` → Remittance → ClaimResponse + PaymentReconciliation
- `dicom series to fhir imagingstudy` → DICOM Study/Series/Instance → FHIR ImagingStudy

The decomposer accepts loose phrasing — "HL7v2 ADT^A01 → FHIR Patient", "convert ORU to FHIR Observation", etc. all resolve.

## How to run

Call `generate_standards_translator` with `mapping`. Output package lands at `data/generated-packages/mapping-<source>-to-<target>/`.

## Caveats

The transformer scaffold is intentionally inert (TODO functions) — fill in with the actual library calls for your stack (HAPI HL7v2, hl7apy, fhir.resources, pyx12, pydicom, Ballerina EDI). The mapping spec + per-field transform pattern is the load-bearing artifact; the code is a guide.

A `data-starved` warning means the catalog has no concept citations from the cited tools — the mapping itself ships from the built-in spec.