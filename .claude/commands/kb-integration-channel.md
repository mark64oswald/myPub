---
description: Generate a Mirth/NextGen Connect Integration Channel scaffold for a healthcare data flow (EHR-ADT-to-Lab, Lab-result-to-EHR-FHIR, Claim-to-clearinghouse, DICOM-ingest, ADT-to-warehouse, AdverseEvent-to-Regulator) — channel.xml + transformer.js + sample messages
---

You are generating a Mirth Connect integration channel for the scenario in `$ARGUMENTS`.

The generator emits:
- `README.md` — scenario description, source/destination connector specs, transformer intent, deployment notes, caveats (Rhino runtime quirks, credential placeholders)
- `channel.xml` — Mirth Connect channel definition with source + destination connector skeletons, ready to import into Mirth Channel Manager
- `transformer.js` — JavaScript transformer scaffold tailored to the source→destination format pair (HL7v2→FHIR, JDBC→X12, DICOM→FHIR, FHIR→ICH E2B XML)
- `test_messages/` — sample input message + expected-output template

## Supported scenarios

- `ehr-adt-to-lab` — EHR ADT^A01 → Lab (LLP HL7v2)
- `lab-result-to-ehr-fhir` — Lab ORU^R01 → FHIR Observation/DiagnosticReport → EHR REST
- `claim-to-clearinghouse` — Billing DB row → X12 837P → SFTP clearinghouse upload
- `imaging-study-ingest` — DICOM C-STORE → FHIR ImagingStudy → EHR FHIR repo
- `adt-to-warehouse` — EHR ADT → FHIR Patient + Encounter → batched Parquet → S3
- `adverse-event-to-regulator` — FHIR AdverseEvent → ICH E2B(R3) XML → AS2 to FDA

## How to run

Call `generate_integration_channel` with `scenario`. Output package lands at `data/generated-packages/integration-channel-<scenario>/`.

## How to deploy

Import `channel.xml` into Mirth Channel Manager (Channel Manager → Import Channel). Edit the source/destination connector configs to match your environment (hostnames, ports, credentials, certs). Deploy + start the channel. Test by feeding the sample input from `test_messages/` to the source endpoint.

## Caveats

- The channel.xml carries the structural skeleton; **connector credentials, certs, and destination URLs are placeholders** — fill in for your environment.
- The transformer.js is a happy-path scaffold. Real-world transformers need null/missing-segment handling, code-system fallback, retry on transient errors. The scaffold is a starting point.
- Mirth uses Rhino — stick to ES5 syntax inside transformer.js.