---
description: Generate a FHIR Implementation Guide Scaffold for a use case (bulk-data-export, patient-summary, tumor-board, prior-auth, adverse-event) — SUSHI/FSH skeleton with profiles, value sets, extensions, examples, and the build configuration files
---

You are generating a FHIR Implementation Guide scaffold for the use case in `$ARGUMENTS`.

The generator emits a buildable IG repo structure:

```
fhir-ig-<use-case>/
├── README.md                  intent + how to build + caveats
├── sushi-config.yaml          SUSHI (FSH compiler) configuration
├── ig.ini                     IG Publisher configuration
└── input/
    ├── pagecontent/
    │   ├── index.md           landing page
    │   └── background.md      use-case rationale
    ├── profiles/              FSH StructureDefinition skeletons (one per resource)
    ├── valuesets/             FSH ValueSet skeletons
    ├── extensions/            FSH Extension skeletons
    └── examples/              JSON example resources (one per profile)
```

## Supported use cases

- `bulk-data-export` — FHIR Bulk Data Access for clinical trial cohort extraction (SMART Backend Services + Flat FHIR)
- `patient-summary` — International Patient Summary (IPS) for cross-org handoffs
- `tumor-board` — Custom oncology IG for tumor board case presentations (mCODE-aligned, cancer + genomics + imaging + treatment + trial-eligibility)
- `prior-auth` — DaVinci PAS for payer prior auth (FHIR + X12 278 underneath)
- `adverse-event` — Clinical trial AE reporting (FHIR + ICH E2B(R3) alignment)

## How to run

Call `generate_fhir_ig` with `use_case`. Output package lands at `data/generated-packages/fhir-ig-<use-case>/`.

## How to build the IG

```bash
npm install -g fsh-sushi              # FSH compiler
sushi build                            # FSH → StructureDefinitions
java -jar publisher.jar -ig ig.ini     # build the IG website
```

## Caveats

The FSH skeletons declare resource constraints with `// TODO` markers for cardinality + must-support flags + value-set bindings — those need use-case judgment. Examples are skeletal (the resource type + profile reference); they don't yet validate against profile constraints. Run `sushi build && publisher.jar` to surface the conformance errors that need filling in.

For oncology IGs (tumor-board), the profiles inherit from mCODE patterns where possible rather than reinventing.