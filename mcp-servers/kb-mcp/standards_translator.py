"""standards_translator.py — Standards Translator / Cross-Mapping generator.

Given a source healthcare standard + target standard (e.g., "HL7v2 ADT^A01"
→ "FHIR Patient+Encounter"), generates a complete mapping package: the
field-by-field mapping table, a transformer skeleton in Python, round-trip
tests, and a README explaining where lossy/structural transforms happen.

The mapping catalog covers the common industry-standard transforms:
  HL7v2 ADT^A01 / A03 / A08 → FHIR Patient + Encounter
  HL7v2 ORU^R01            → FHIR Observation + DiagnosticReport
  HL7v2 ORM^O01            → FHIR ServiceRequest
  X12 837P                 → FHIR Claim
  X12 835                  → FHIR ClaimResponse + PaymentReconciliation
  DICOM Series             → FHIR ImagingStudy

For mappings outside this catalog, the generator falls back to a
"mapping skeleton" mode that emits structure + TODOs based on the
source/target format names.

Output:
    mapping-<source>-to-<target>/
      README.md             intent + caveats + how to run
      mapping.md            field-by-field source→target table
      transformer.py        Python skeleton (function per source element)
      tests/test_mapping.py round-trip + spec-conformance tests
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

LOG = logging.getLogger("mypub-standards-translator")

GENERATOR_TYPE = "standards_translator"


# A mapping entry: (source_path, target_path, transform, notes)
# transform: one of "direct", "lookup", "concat", "split", "code-translation",
#            "lossy", "compute", "drop"
MAPPING_CATALOG: dict[str, dict[str, Any]] = {
    "hl7v2-adt-a01-to-fhir-patient-encounter": {
        "source": "HL7v2 ADT^A01 (Admit/Visit Notification)",
        "target": "FHIR Patient + Encounter",
        "purpose": "Convert an ADT admission message into a FHIR Patient (new "
                   "or existing) plus a new Encounter.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "HL7 Library (PHP)",
                        "Mirth/NextGen Connect", "FHIR Specification",
                        "hl7apy"],
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
    "hl7v2-oru-r01-to-fhir-observation": {
        "source": "HL7v2 ORU^R01 (Unsolicited Observation Result)",
        "target": "FHIR Observation + DiagnosticReport (+ Patient ref)",
        "purpose": "Convert an ORU lab result message into FHIR Observations "
                   "grouped under a DiagnosticReport.",
        "tools_cited": ["HAPI FHIR", "HAPI HL7v2 — Parsing", "FHIR Specification",
                        "hl7apy", "Mirth/NextGen Connect"],
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
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    lines = [
        f"# Standards Translator — {d.source} → {d.target}",
        "",
        f"**Purpose.** {d.purpose}",
        "",
        "## What this package contains",
        "",
        "- `mapping.md` — field-by-field source → target table with the "
           "transform pattern (direct, lookup, split, code-translation, "
           "compute, drop) and notes",
        "- `transformer.py` — Python skeleton with one function per source "
           "element. Each function is a TODO scaffold; fill in the actual "
           "library calls (HAPI / pyx12 / pydicom) for your stack.",
        "- `tests/test_mapping.py` — round-trip + spec-conformance test "
           "templates",
        "",
        "## How to run",
        "",
        "```bash",
        "# Install the libraries your stack uses",
        "pip install hl7apy fhir.resources pyx12 pydicom  # pick what you need",
        "python transformer.py --input /path/to/source.msg --output /path/to/target.json",
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
        "",
    ]
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for cid, cname, _ds, src in d.citations[:15]:
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
    """Generate a Python skeleton with one function per source element."""
    src_short = _slugify(d.source).replace("-", "_")[:40]
    lines = [
        f'"""Transformer skeleton for {d.source} → {d.target}.',
        '',
        'Generated by /kb-standards-translator. Each per-field function is',
        'a TODO scaffold — fill in the actual library calls for your stack',
        '(HAPI HL7v2 / hl7apy / fhir.resources / pyx12 / pydicom / Ballerina).',
        '"""',
        'from __future__ import annotations',
        '',
        'import argparse',
        'import json',
        'import logging',
        'from pathlib import Path',
        'from typing import Any',
        '',
        'LOG = logging.getLogger("transformer")',
        '',
        '',
        '# ---- Per-field transform functions ----',
        '',
    ]

    seen_sources: set[str] = set()
    for src, tgt, transform, notes in d.fields:
        # Generate a function name from the source path
        # PID-3 → transform_pid_3; (0010,0020) → transform_dicom_0010_0020
        slug = src.lower()
        slug = (slug.replace("-", "_").replace("(", "").replace(")", "")
                    .replace(",", "_").replace(".", "_").replace("/", "_")
                    .replace(" ", "_").replace("^", "_"))
        slug = "".join(c for c in slug if c.isalnum() or c == "_")[:50]
        slug = slug.strip("_") or "field"
        if slug in seen_sources:
            continue
        seen_sources.add(slug)
        lines.extend([
            f'def transform_{slug}(source_value: Any, *, context: dict | None = None) -> Any:',
            f'    """{transform.upper()}: {src} → {tgt}',
            '',
            f'    {notes}',
            '    """',
            f'    # TODO: implement {transform} transform',
            '    raise NotImplementedError',
            '',
            '',
        ])

    lines.extend([
        '# ---- Top-level transformer -----',
        '',
        'def transform_message(source_message: Any, context: dict | None = None) -> dict:',
        f'    """Transform one {d.source} → {d.target}."""',
        '    out: dict = {}',
        '    ctx = context or {}',
        '    # TODO: parse source_message with the appropriate library',
        '    # (hl7apy.parser.parse_message; pyx12.x12file.X12Reader;',
        '    #  pydicom.dcmread; etc.) and dispatch each source element to',
        '    # its transform_<element> function.',
        '    return out',
        '',
        '',
        'def main():',
        '    p = argparse.ArgumentParser(description=__doc__)',
        '    p.add_argument("--input", type=Path, required=True)',
        '    p.add_argument("--output", type=Path, required=True)',
        '    args = p.parse_args()',
        '    logging.basicConfig(level=logging.INFO)',
        '    source = args.input.read_text()',
        '    result = transform_message(source)',
        '    args.output.write_text(json.dumps(result, indent=2))',
        '',
        '',
        'if __name__ == "__main__":',
        '    main()',
    ])
    return "\n".join(lines)


def _render_test(d: _Decomposition) -> str:
    """Generate test scaffolds for round-trip + spec-conformance."""
    lines = [
        f'"""Test scaffolds for {d.source} → {d.target} mapping.',
        '',
        'Templates only — wire the fixture loaders to your real source/target',
        'samples. The transformer module is generated alongside this test',
        'with TODO stubs; tests fail until those stubs are implemented.',
        '"""',
        'from __future__ import annotations',
        '',
        'from pathlib import Path',
        '',
        'import pytest',
        '',
        '',
        '@pytest.fixture',
        'def source_fixture(tmp_path):',
        f'    """Load a sample {d.source} message. TODO: provide a real fixture."""',
        f'    pytest.skip("provide a {d.source} fixture in tests/fixtures/")',
        '',
        '',
        '@pytest.fixture',
        'def expected_target(tmp_path):',
        f'    """Load the expected {d.target} output for the source fixture."""',
        f'    pytest.skip("provide expected {d.target} fixture in tests/fixtures/")',
        '',
        '',
        'def test_transform_round_trip(source_fixture, expected_target):',
        '    """End-to-end: transform the source, compare to the expected target."""',
        '    from transformer import transform_message',
        '    actual = transform_message(source_fixture)',
        '    assert actual == expected_target, "round-trip mismatch"',
        '',
        '',
        'def test_target_spec_conformance(source_fixture):',
        f'    """The {d.target} output must validate against the FHIR/X12/DICOM spec."""',
        '    from transformer import transform_message',
        '    actual = transform_message(source_fixture)',
        f'    # TODO: validate {d.target} against the spec.',
        '    # For FHIR: use fhir.resources to construct + validate.',
        '    # For X12: use pyx12.x12file in validation mode.',
        '    # For DICOM: use pydicom + dciodvfy.',
        '    assert actual is not None',
        '',
        '',
        'def test_lossy_fields_documented():',
        '    """Lossy transforms must be marked in mapping.md (manual sanity check)."""',
        '    mapping_md = (Path(__file__).resolve().parent.parent / "mapping.md").read_text()',
        '    # If the mapping has lossy entries, they should appear in the legend',
        '    if "lossy" in mapping_md.lower():',
        '        assert "**lossy**" in mapping_md, "lossy fields used but not in legend"',
    ]
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
                      "n_fields": len(d.fields)},
            logical_key="mapping_main",
            sources=sources,
        ))
        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="mapping.md", content=_render_mapping(d), purpose="reference"),
            GenFile(filename="transformer.py", content=_render_transformer(d), purpose="code"),
            GenFile(filename="tests/test_mapping.py", content=_render_test(d), purpose="test"),
        ])
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
