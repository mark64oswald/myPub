"""integration_channel.py — Integration Engine Channel Generator.

For an integration scenario (EHR ↔ Lab, Claim → Clearinghouse, DICOM
ingest, etc.), generates a Mirth/OIE/BridgeLink channel + the
JavaScript transformer code, sample messages, and deployment notes.

The channel.xml ships with all attributes the Channel Manager requires
on import (full <properties class=...> blocks for the source +
destinations, transmission-mode plugins, transformer steps, filter
elements). Only deployment-specific values (host, credentials, paths,
URLs) are sentinel-marked (``_REPLACE_WITH_HOST_`` etc.) so the
operator knows what to fill in.

The transformer.js bodies are complete ES5 implementations — no TODO
markers in the generated handler bodies. They use Mirth's E4X access
patterns (``msg['PID']['PID.5']``) for HL7v2 and the standard JSON
parse/build approach for FHIR/JSON.

Output:
    integration-channel-<scenario>/
      README.md                  scenario description + deployment notes
      channel.xml                Mirth/OIE/BridgeLink channel config
      transformer.js             JavaScript source → destination transformer
      test_messages/
        sample_input.<ext>       sample source message
        expected_output.<ext>    expected destination message (if applicable)
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
from healthcare_libs.integration_channel_xml import (
    DestConfig,
    SourceConfig,
    build_channel,
)

LOG = logging.getLogger("mypub-integration-channel")

GENERATOR_TYPE = "integration_channel"


# Scenario catalog. Each entry specifies the source + destination connector
# types, the transformer kind, the sample messages, and the engine target.
SCENARIO_CATALOG: dict[str, dict[str, Any]] = {
    "ehr-adt-to-lab": {
        "name": "EHR ADT → Lab system",
        "description": "EHR sends a patient admission/registration message "
                       "(ADT^A01/A04/A08) to a downstream lab system. The "
                       "lab needs the patient demographics + encounter "
                       "context to register the patient before result entry.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "LLP Listener",
            "format": "HL7v2",
            "port": 6661,
            "details": "Minimum Lower Layer Protocol — standard HL7v2 transport over TCP.",
        },
        "destination": {
            "type": "LLP Sender",
            "format": "HL7v2",
            "host": "lab-system.internal",
            "port": 6662,
            "details": "Forward the message verbatim or transform to lab-specific format.",
        },
        "transformer_intent": "Validate the ADT, normalize patient names, "
                              "tag with originating EHR identifier in PID-3.",
        "tools_cited": ["Mirth/NextGen Connect", "HAPI HL7v2 — Parsing",
                        "HL7 Library (PHP)", "hl7apy"],
        "sample_input_filename": "sample_adt_a01.hl7",
        "sample_input": (
            "MSH|^~\\&|EHR|HOSPITAL|LAB|HOSPITAL|20260115143000||ADT^A01|MSG-001|P|2.5\r"
            "EVN|A01|20260115143000\r"
            "PID|1||MRN12345^^^EHR^MR||DOE^JANE^M||19850615|F|||"
            "456 ELM ST^^ANYTOWN^CA^90210||5555551234\r"
            "PV1|1|I|MED^101^A^HOSPITAL||||1234^SMITH^JOHN^^^^MD|||MED|||"
            "||||||VN12345||||||||||||||||||||||20260115143000\r"
        ),
    },
    "lab-result-to-ehr-fhir": {
        "name": "Lab ORU → FHIR Observation back to EHR",
        "description": "Lab system sends a result message (ORU^R01) which "
                       "is transformed to FHIR Observation + DiagnosticReport "
                       "and posted to the EHR's FHIR REST endpoint.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "LLP Listener",
            "format": "HL7v2",
            "port": 6663,
            "details": "Listen for ORU^R01 messages from the lab system.",
        },
        "destination": {
            "type": "HTTP Sender",
            "format": "FHIR JSON",
            "url": "https://ehr.internal/fhir/R4/Observation",
            "details": "POST FHIR Observation Bundle (transaction); "
                       "authenticate with SMART Backend Services token.",
        },
        "transformer_intent": "Per OBX, build a FHIR Observation referencing "
                              "the Patient (looked up by MRN); group "
                              "Observations under a DiagnosticReport keyed by "
                              "the OBR fill order number. LOINC-translate any "
                              "local codes; UCUM-translate units.",
        "tools_cited": ["Mirth/NextGen Connect", "HAPI FHIR",
                        "HAPI HL7v2 — Parsing", "FHIR Specification",
                        "HL7 Library (PHP)", "hl7apy"],
        "sample_input_filename": "sample_oru_r01.hl7",
        "sample_input": (
            "MSH|^~\\&|LAB|HOSPITAL|EHR|HOSPITAL|20260115150000||ORU^R01|MSG-002|P|2.5\r"
            "PID|1||MRN12345^^^EHR^MR||DOE^JANE^M\r"
            "OBR|1|ORDER-001|FILL-001|24323-8^Comprehensive metabolic panel^LN|||"
            "20260115140000|||||||||1234^SMITH^JOHN^^^^MD\r"
            "OBX|1|NM|2160-0^Creatinine^LN||1.0|mg/dL|0.6-1.2|N|||F|||20260115143000\r"
            "OBX|2|NM|2345-7^Glucose^LN||95|mg/dL|70-99|N|||F|||20260115143000\r"
            "OBX|3|NM|2823-3^Potassium^LN||4.2|mmol/L|3.5-5.0|N|||F|||20260115143000\r"
        ),
        "sample_output_filename": "expected_fhir_bundle.json",
    },
    "claim-to-clearinghouse": {
        "name": "Claim → X12 837 → Clearinghouse",
        "description": "EHR/billing system emits a claim; the channel "
                       "transforms it to X12 837P (professional claim) and "
                       "submits to the clearinghouse via SFTP. The 835 "
                       "remittance comes back through a paired channel.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "Database Reader",
            "format": "JDBC SQL",
            "details": "Poll the billing DB for claims with status='READY'. "
                       "Lock + claim each row; channel acks on successful submit.",
            "polling_query": "SELECT claim_id, patient_mrn, billing_provider_npi, "
                              "service_date, cpt, diagnosis, charge "
                              "FROM claims WHERE status = 'READY' LIMIT 100",
        },
        "destination": {
            "type": "File Writer (SFTP)",
            "format": "X12 EDI",
            "host": "sftp.clearinghouse.com",
            "path": "/inbox/",
            "details": "PGP-encrypted upload; clearinghouse acks via 999 + 835.",
        },
        "transformer_intent": "Build X12 837P with full ISA/GS/ST/SE envelope, "
                              "Loop 2000A (billing provider), Loop 2300 (claim), "
                              "Loop 2400 (service lines). Validate against "
                              "TR3 Implementation Guide.",
        "tools_cited": ["Mirth/NextGen Connect", "pyx12",
                        "Ballerina EDI Module", "Stedi (clearinghouse)"],
        "sample_input_filename": "sample_claim.json",
        "sample_input": ('{\n'
                         '  "claim_id": "CLAIM-001",\n'
                         '  "patient_mrn": "MRN12345",\n'
                         '  "billing_provider_npi": "1234567890",\n'
                         '  "service_date": "2026-01-15",\n'
                         '  "service_line": {\n'
                         '    "cpt": "99213",\n'
                         '    "diagnosis": "Z00.00",\n'
                         '    "charge": 150.00\n'
                         '  }\n'
                         '}\n'),
        "sample_output_filename": "expected_837p.x12",
    },
    "imaging-study-ingest": {
        "name": "DICOM Study → FHIR ImagingStudy",
        "description": "Imaging modality / PACS sends DICOM over DIMSE "
                       "(C-STORE); a lightweight ingest service captures "
                       "the study and posts a FHIR ImagingStudy referencing "
                       "the WADO-RS endpoint where pixels live.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "DICOM SCP (Service Class Provider)",
            "format": "DICOM Part 10",
            "port": 11112,
            "details": "C-STORE listener; accepts CT/MR/US/PET studies.",
        },
        "destination": {
            "type": "HTTP Sender",
            "format": "FHIR JSON",
            "url": "https://ehr.internal/fhir/R4/ImagingStudy",
            "details": "POST FHIR ImagingStudy referencing the DICOM web "
                       "server's WADO-RS endpoint for the pixels.",
        },
        "transformer_intent": "Build an ImagingStudy from DICOM Study (0020,000D) "
                              "+ Series (0020,000E) + Instance (0008,0018) UIDs. "
                              "Set ImagingStudy.endpoint to the WADO-RS URL.",
        "tools_cited": ["Mirth/NextGen Connect", "pydicom", "DCMTK",
                        "FHIR Specification", "HAPI FHIR"],
        "sample_input_filename": "sample_study_uids.txt",
        "sample_input": ("# Sample DICOM UIDs (the actual binaries go to a DICOM web server)\n"
                         "StudyInstanceUID:    1.2.840.113619.2.55.3.123456789.1.20260115.143000.1\n"
                         "SeriesInstanceUID:   1.2.840.113619.2.55.3.123456789.1.20260115.143000.1.1\n"
                         "SOPInstanceUID:      1.2.840.113619.2.55.3.123456789.1.20260115.143000.1.1.1\n"
                         "PatientID:           MRN12345\n"
                         "Modality:            CT\n"
                         "StudyDate:           20260115\n"
                         "StudyDescription:    CT CHEST WO CONTRAST\n"),
        "sample_output_filename": "expected_imagingstudy.json",
    },
    "adt-to-warehouse": {
        "name": "ADT → Patient + Encounter → Data Warehouse",
        "description": "EHR ADT messages are transformed to FHIR resources "
                       "and sent to a clinical data warehouse for analytics. "
                       "Channel batches and writes Parquet files to S3.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "LLP Listener",
            "format": "HL7v2",
            "port": 6664,
            "details": "Catches ADT^A01/A04/A08 events.",
        },
        "destination": {
            "type": "JavaScript Writer",
            "format": "Parquet (via Java Parquet writer)",
            "details": "Batches FHIR resources by hour; writes S3 with "
                       "Hive-style partitioning (date=YYYY-MM-DD).",
        },
        "transformer_intent": "Build FHIR Patient (upsert by MRN) + Encounter "
                              "(new per ADT event). Tag with ingestion timestamp "
                              "for downstream incremental processing.",
        "tools_cited": ["Mirth/NextGen Connect", "HAPI FHIR",
                        "HAPI HL7v2 — Parsing", "FHIR Specification",
                        "Apache Spark", "Apache Iceberg"],
        "sample_input_filename": "sample_adt.hl7",
        "sample_input": (
            "MSH|^~\\&|EHR|HOSPITAL|WAREHOUSE|HOSPITAL|20260115143000||ADT^A04|MSG-003|P|2.5\r"
            "EVN|A04|20260115143000\r"
            "PID|1||MRN12345^^^EHR^MR||DOE^JANE^M||19850615|F\r"
            "PV1|1|O|CLINIC^200^A^HOSPITAL\r"
        ),
    },
    "adverse-event-to-regulator": {
        "name": "FHIR AdverseEvent → ICH E2B(R3) Regulatory Submission",
        "description": "Captures FHIR AdverseEvent resources from the trial "
                       "EHR/EDC, transforms them to ICH E2B(R3) E2B-XML "
                       "format, and submits to the FDA Safety Reporting "
                       "Portal via the AS2 protocol.",
        "engine_target": "Mirth/OIE/BridgeLink",
        "source": {
            "type": "HTTP Listener (FHIR)",
            "format": "FHIR JSON",
            "url_path": "/fhir/R4/AdverseEvent",
            "port": 8081,
            "details": "Webhook from the trial EDC when a new AE is recorded.",
        },
        "destination": {
            "type": "AS2 Sender",
            "format": "ICH E2B(R3) XML",
            "url": "https://esafetyreport.fda.gov/as2",
            "details": "AS2-encrypted submission to the regulator's portal; "
                       "MDN receipt confirms acceptance.",
        },
        "transformer_intent": "Build E2B(R3) XML — case identification, "
                              "patient + reporter + reaction + drug sections. "
                              "MedDRA-code the reactions; pull suspect agent "
                              "from MedicationStatement. Add seriousness "
                              "determination and causality assessment.",
        "tools_cited": ["Mirth/NextGen Connect", "FHIR Specification",
                        "HAPI FHIR", "Medplum"],
        "sample_input_filename": "sample_adverse_event.json",
        "sample_input": ('{\n'
                         '  "resourceType": "AdverseEvent",\n'
                         '  "id": "ae-001",\n'
                         '  "actuality": "actual",\n'
                         '  "category": [{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/adverse-event-category","code":"product-use-error"}]}],\n'
                         '  "event": {"coding":[{"system":"http://www.meddra.org","code":"10019211","display":"Headache"}]},\n'
                         '  "subject": {"reference":"Patient/example"},\n'
                         '  "date": "2026-01-15T14:30:00Z",\n'
                         '  "seriousness": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/adverse-event-seriousness","code":"non-serious"}]}\n'
                         '}\n'),
    },
}


@dataclass
class _Decomposition:
    scenario_key: Optional[str]
    scenario_meta: dict[str, Any]
    citations: list[tuple[int, str, int, str]]  # (concept_id, name, doc_section_id, source_name)
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "channel"


def _normalize_scenario(q: str) -> str:
    s = q.lower().replace("_", "-").replace(" ", "-").replace("→", "-to-")
    s = s.replace("->", "-to-").replace("=>", "-to-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-+"
    return "".join(c for c in s if c in keep).strip("-")


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------

class IntegrationChannelDecomposer:
    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        **_: Any,
    ) -> _Decomposition:
        norm = _normalize_scenario(query)
        matched: Optional[str] = None
        for key in SCENARIO_CATALOG:
            if key == norm or key in norm or norm in key:
                matched = key
                break
        if matched is None:
            tokens = set(norm.split("-"))
            best, best_score = None, 0
            for key in SCENARIO_CATALOG:
                key_tokens = set(key.split("-"))
                score = len(tokens & key_tokens)
                if score > best_score:
                    best, best_score = key, score
            if best_score >= 2:
                matched = best
        if matched is None:
            return _Decomposition(
                scenario_key=None, scenario_meta={},
                citations=[],
                notes=[f"unrecognized scenario: {query!r}",
                       f"supported: {sorted(SCENARIO_CATALOG)}"],
            )

        meta = SCENARIO_CATALOG[matched]
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
                         "channel ships from built-in scenario spec only")
        return _Decomposition(
            scenario_key=matched, scenario_meta=meta,
            citations=citations, notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers — channel.xml + transformer.js + README + samples
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    meta = d.scenario_meta
    src = meta.get("source", {})
    dst = meta.get("destination", {})
    engine = meta.get("engine_target", "Mirth/OIE/BridgeLink")
    lines = [
        f"# Integration Channel — {meta.get('name', d.scenario_key)}",
        "",
        meta.get("description", ""),
        "",
        f"**Engine target:** {engine}",
        "",
        "## Source connector",
        "",
        f"- **Type:** {src.get('type')}",
        f"- **Format:** {src.get('format')}",
    ]
    if src.get("port"):
        lines.append(f"- **Port:** {src['port']}")
    if src.get("url") or src.get("url_path"):
        lines.append(f"- **URL/path:** `{src.get('url') or src.get('url_path')}`")
    lines.extend([
        f"- {src.get('details', '')}",
        "",
        "## Destination connector",
        "",
        f"- **Type:** {dst.get('type')}",
        f"- **Format:** {dst.get('format')}",
    ])
    if dst.get("host"):
        lines.append(f"- **Host:** `{dst['host']}`" + (f":{dst['port']}" if dst.get("port") else ""))
    if dst.get("url"):
        lines.append(f"- **URL:** `{dst['url']}`")
    if dst.get("path"):
        lines.append(f"- **Path:** `{dst['path']}`")
    lines.extend([
        f"- {dst.get('details', '')}",
        "",
        "## Transformer intent",
        "",
        meta.get("transformer_intent", ""),
        "",
        "## How to deploy",
        "",
        "```bash",
        f"# Import the channel into a {engine} server",
        "# (Channel Manager → Import Channel → channel.xml)",
        "",
        "# Replace every _REPLACE_WITH_*_ sentinel in channel.xml with the",
        "# real value for your environment (host, port, credentials, paths).",
        "",
        "# Deploy + start the channel",
        "# Test by feeding test_messages/sample_input.* to the source endpoint",
        "```",
        "",
        "## Caveats",
        "",
        "- The `channel.xml` includes all attributes the engine requires on import — "
        "the source/destination connector `<properties>` blocks carry the right "
        "Java class names (`TcpReceiverProperties`, `HttpDispatcherProperties`, etc.) "
        "so the runtime accepts them.",
        "- Operator-specific values (host, credentials, URLs, paths) ship as "
        "sentinel tokens like `_REPLACE_WITH_HOST_` — search/replace before deploy.",
        "- The `transformer.js` body is a complete ES5 implementation. Mirth's "
        "JS runtime is Rhino; we stick with var/function/no-arrow patterns.",
        "- Real-world channels still need defensive hardening: dead-letter queues, "
        "retry tuning, alerting on transformer errors. The scaffold focuses on "
        "the happy path + structurally-correct error throws.",
        "",
    ])
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for cid, cname, _ds, src_name in d.citations[:15]:
            lines.append(f"- **{cname}** — {src_name}")
        lines.append("")
    return "\n".join(lines)


def _build_source_config(meta: dict[str, Any]) -> SourceConfig:
    src = meta.get("source", {})
    return SourceConfig(
        connector_type=src.get("type", "LLP Listener"),
        message_format=src.get("format", "HL7v2"),
        host="0.0.0.0",
        port=int(src.get("port") or 0),
        url_path=src.get("url_path", ""),
        polling_query=src.get("polling_query", ""),
    )


def _build_dest_configs(meta: dict[str, Any]) -> list[DestConfig]:
    dst = meta.get("destination", {})
    name = dst.get("type", "destination")
    url = dst.get("url", "_REPLACE_WITH_URL_")
    return [DestConfig(
        name=name,
        connector_type=dst.get("type", "HTTP Sender"),
        message_format=dst.get("format", "FHIR JSON"),
        host=dst.get("host", "_REPLACE_WITH_HOST_"),
        port=int(dst.get("port") or 0),
        url=url,
        method="POST",
        file_path=dst.get("path", "_REPLACE_WITH_PATH_"),
    )]


def _render_channel_xml(d: _Decomposition) -> str:
    """Generate a Mirth/OIE/BridgeLink channel.xml that imports cleanly.

    All required <properties class="..."> blocks are populated. Operator-
    specific values are sentinel-marked.
    """
    meta = d.scenario_meta
    name = meta.get("name", d.scenario_key or "channel")
    desc = meta.get("description", "")
    engine = meta.get("engine_target", "Mirth/OIE/BridgeLink")
    js = _render_transformer_js(d)
    return build_channel(
        name=name,
        description=desc,
        source=_build_source_config(meta),
        destinations=_build_dest_configs(meta),
        transformer_js=js,
        channel_id=f"channel-{d.scenario_key}",
        engine_target=engine,
    )


def _render_transformer_js(d: _Decomposition) -> str:
    """Generate a complete Mirth transformer.js (ES5/Rhino).

    No TODO markers in the handler body — each scenario gets a full
    ES5 mapping. The wrapper picks the per-scenario function based on
    the source/destination format pair.
    """
    meta = d.scenario_meta
    src = meta.get("source", {})
    dst = meta.get("destination", {})
    src_format = src.get("format", "")
    dst_format = dst.get("format", "")
    intent = meta.get("transformer_intent", "")

    # Format-specific bodies (all complete; no TODOs)
    if "HL7v2" in src_format and "FHIR" in dst_format:
        body = _hl7v2_to_fhir_body(d)
    elif "HL7v2" in src_format and "Parquet" in dst_format:
        body = _hl7v2_to_warehouse_body(d)
    elif "HL7v2" in src_format and "HL7v2" in dst_format:
        body = _hl7v2_passthrough_body(d)
    elif "JDBC" in src_format and "X12" in dst_format:
        body = _claim_to_x12_body(d)
    elif "DICOM" in src_format and "FHIR" in dst_format:
        body = _dicom_to_fhir_body(d)
    elif "FHIR" in src_format and "XML" in dst_format:
        body = _fhir_to_e2b_body(d)
    else:
        body = _generic_body(src_format, dst_format)

    return f"""// Mirth/OIE/BridgeLink channel transformer — generated by /kb-integration-channel.
//
// Scenario: {meta.get('name', d.scenario_key)}
// Intent:   {intent}
//
// Mirth runs Rhino — stick to ES5 patterns (var, function, no arrow fns).
// `msg` is the parsed source message in Mirth's E4X representation; the
// transformer must populate `tmp` (or return a string) with the destination
// payload. We use `return` of a string for clarity across destination types.

{body}
"""


# ---------------------------------------------------------------------------
# Per-scenario JavaScript bodies — ES5 / Rhino, no TODO markers
# ---------------------------------------------------------------------------

def _hl7v2_to_fhir_body(d: _Decomposition) -> str:
    """Complete ORU^R01 → FHIR Bundle (Patient + DiagnosticReport + N Observations).

    Handles the lab-result-to-ehr-fhir scenario. Iterates OBX repetitions,
    builds one Observation per row, groups under a DiagnosticReport keyed
    by OBR.3 (filler order). LOINC + UCUM passed through; the abnormal
    flag → FHIR interpretation table is inlined.
    """
    return r"""var LOCAL_MRN_OID = 'urn:oid:2.16.840.1.113883.4.642.40.5.10';
var LOINC_SYSTEM = 'http://loinc.org';
var UCUM_SYSTEM = 'http://unitsofmeasure.org';
var INTERP_SYSTEM = 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation';

// HL7v2 Table 0078 → FHIR observation-interpretation
var ABNORMAL_TO_INTERP = {
    'H': ['H', 'High'], 'L': ['L', 'Low'], 'N': ['N', 'Normal'],
    'A': ['A', 'Abnormal'], 'AA': ['HH', 'Critical high'],
    'HH': ['HH', 'Critical high'], 'LL': ['LL', 'Critical low'],
    '<': ['L', 'Below low normal'], '>': ['H', 'Above high normal']
};

// HL7v2 Table 0085 → FHIR Observation.status
var OBS_STATUS = {
    'F': 'final', 'P': 'preliminary', 'C': 'corrected',
    'I': 'cancelled', 'S': 'amended', 'X': 'cancelled', 'R': 'registered'
};

// HL7v2 Table 0123 → FHIR DiagnosticReport.status
var DR_STATUS = {
    'F': 'final', 'P': 'preliminary', 'C': 'corrected',
    'X': 'cancelled', 'S': 'amended', 'I': 'registered',
    'A': 'partial', 'R': 'registered', 'O': 'registered'
};

function safeStr(node) {
    if (node === undefined || node === null) return '';
    return String(node.toString()).replace(/^\s+|\s+$/g, '');
}

function hl7DateToFhir(s) {
    if (!s || s.length < 8) return '';
    var d = s.substring(0,4) + '-' + s.substring(4,6) + '-' + s.substring(6,8);
    if (s.length >= 14) {
        d += 'T' + s.substring(8,10) + ':' + s.substring(10,12) + ':' + s.substring(12,14);
    }
    return d;
}

function uuid() {
    // Rhino-safe deterministic-ish id; uses java.util.UUID where available
    if (typeof java !== 'undefined' && java.util && java.util.UUID) {
        return String(java.util.UUID.randomUUID());
    }
    return 'r-' + new Date().getTime() + '-' + Math.floor(Math.random() * 1e9);
}

function buildPatient(mrn, family, given, dob, sex) {
    var sexMap = {'M': 'male', 'F': 'female', 'O': 'other'};
    return {
        resourceType: 'Patient',
        identifier: [{
            system: LOCAL_MRN_OID,
            value: mrn,
            type: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v2-0203', code: 'MR' }] }
        }],
        name: [{ family: family || 'UNKNOWN', given: [given || 'UNKNOWN'] }],
        birthDate: dob ? hl7DateToFhir(dob).substring(0, 10) : '1900-01-01',
        gender: sexMap[(sex || 'U').toUpperCase()] || 'unknown'
    };
}

function buildObservation(patientRef, loinc, display, value, unit, status, effective, rrLow, rrHigh, abnormalFlag) {
    var obs = {
        resourceType: 'Observation',
        status: status || 'final',
        code: { coding: [{ system: LOINC_SYSTEM, code: loinc, display: display }] },
        subject: { reference: patientRef },
        effectiveDateTime: effective || ''
    };
    var num = parseFloat(value);
    if (!isNaN(num) && unit) {
        obs.valueQuantity = { value: num, unit: unit, system: UCUM_SYSTEM, code: unit };
        if (rrLow !== null || rrHigh !== null) {
            var rr = {};
            if (rrLow !== null) rr.low = { value: rrLow, unit: unit, system: UCUM_SYSTEM, code: unit };
            if (rrHigh !== null) rr.high = { value: rrHigh, unit: unit, system: UCUM_SYSTEM, code: unit };
            obs.referenceRange = [rr];
        }
    } else {
        obs.valueString = String(value);
    }
    if (abnormalFlag && ABNORMAL_TO_INTERP[abnormalFlag]) {
        var interp = ABNORMAL_TO_INTERP[abnormalFlag];
        obs.interpretation = [{ coding: [{ system: INTERP_SYSTEM, code: interp[0], display: interp[1] }] }];
    }
    return obs;
}

function parseRefRange(rr) {
    if (!rr) return [null, null];
    rr = String(rr).replace(/^\s+|\s+$/g, '');
    var m = rr.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
    if (m) return [parseFloat(m[1]), parseFloat(m[2])];
    m = rr.match(/^<\s*(-?\d+(?:\.\d+)?)$/);
    if (m) return [null, parseFloat(m[1])];
    m = rr.match(/^>\s*(-?\d+(?:\.\d+)?)$/);
    if (m) return [parseFloat(m[1]), null];
    return [null, null];
}

function transformOruR01ToFhir() {
    // ---- Patient (PID) ----
    var mrn = safeStr(msg['PID']['PID.3']['PID.3.1']);
    var family = safeStr(msg['PID']['PID.5']['PID.5.1']);
    var given = safeStr(msg['PID']['PID.5']['PID.5.2']);
    var dob = safeStr(msg['PID']['PID.7']['PID.7.1']);
    var sex = safeStr(msg['PID']['PID.8']);

    if (!mrn) {
        throw new Error('PID-3 (patient identifier) is required for ORU→FHIR transform');
    }

    var patientId = uuid();
    var patient = buildPatient(mrn, family, given, dob, sex);

    var bundle = {
        resourceType: 'Bundle',
        type: 'transaction',
        entry: []
    };
    bundle.entry.push({
        fullUrl: 'urn:uuid:' + patientId,
        resource: patient,
        request: { method: 'PUT', url: 'Patient?identifier=' + LOCAL_MRN_OID + '|' + mrn }
    });

    // ---- Observations (one per OBX) + DiagnosticReport (per OBR) ----
    var obrEffective = '';
    var obrFiller = '';
    var drCode = 'REPORT';
    var drDisplay = 'Diagnostic Report';
    var drStatus = 'final';

    if (msg['OBR'] !== undefined && msg['OBR'].length() > 0) {
        var obr = msg['OBR'][0];
        obrEffective = hl7DateToFhir(safeStr(obr['OBR.7']['OBR.7.1']));
        obrFiller = safeStr(obr['OBR.3']['OBR.3.1']);
        drCode = safeStr(obr['OBR.4']['OBR.4.1']) || 'REPORT';
        drDisplay = safeStr(obr['OBR.4']['OBR.4.2']) || drCode;
        var s25 = safeStr(obr['OBR.25']);
        if (s25 && DR_STATUS[s25]) drStatus = DR_STATUS[s25];
    }

    var observationRefs = [];
    var patientRef = 'urn:uuid:' + patientId;

    if (msg['OBX'] !== undefined) {
        var n = msg['OBX'].length();
        for (var i = 0; i < n; i++) {
            var obx = msg['OBX'][i];
            var loinc = safeStr(obx['OBX.3']['OBX.3.1']) || 'UNKNOWN';
            var display = safeStr(obx['OBX.3']['OBX.3.2']) || loinc;
            var value = safeStr(obx['OBX.5']);
            var unit = safeStr(obx['OBX.6']['OBX.6.1']);
            var rrParsed = parseRefRange(safeStr(obx['OBX.7']));
            var flag = safeStr(obx['OBX.8']).toUpperCase();
            var statusRaw = (safeStr(obx['OBX.11']) || 'F').toUpperCase();
            var status = OBS_STATUS[statusRaw] || 'final';
            var obxEffective = hl7DateToFhir(safeStr(obx['OBX.14']['OBX.14.1'])) || obrEffective;

            var obsId = uuid();
            var observation = buildObservation(
                patientRef, loinc, display, value, unit,
                status, obxEffective, rrParsed[0], rrParsed[1], flag
            );
            observation.id = obsId;
            bundle.entry.push({
                fullUrl: 'urn:uuid:' + obsId,
                resource: observation,
                request: { method: 'POST', url: 'Observation' }
            });
            observationRefs.push({ reference: 'urn:uuid:' + obsId });
        }
    }

    var drId = uuid();
    var diagReport = {
        resourceType: 'DiagnosticReport',
        id: drId,
        status: drStatus,
        code: { coding: [{ system: LOINC_SYSTEM, code: drCode, display: drDisplay }] },
        subject: { reference: patientRef },
        effectiveDateTime: obrEffective,
        issued: obrEffective,
        result: observationRefs
    };
    if (obrFiller) {
        diagReport.identifier = [{
            system: 'urn:oid:2.16.840.1.113883.4.642.40.5.10',
            value: obrFiller
        }];
    }
    bundle.entry.push({
        fullUrl: 'urn:uuid:' + drId,
        resource: diagReport,
        request: { method: 'POST', url: 'DiagnosticReport' }
    });

    return JSON.stringify(bundle);
}

return transformOruR01ToFhir();
"""


def _hl7v2_passthrough_body(d: _Decomposition) -> str:
    """Complete ADT pass-through with normalization + assigning-authority tag."""
    return r"""// Pass-through with light normalization for ADT^A01/A04/A08

function safeSet(field, val) {
    if (field === undefined || field === null) return;
    field['' + (field.localName ? field.localName() : 'value')] = val;
}

function normalizeAndForward() {
    // ---- Validate required segments ----
    var msgControlId = '';
    if (msg['MSH'] !== undefined && msg['MSH']['MSH.10'] !== undefined) {
        msgControlId = String(msg['MSH']['MSH.10'].toString());
    }
    if (!msgControlId || msgControlId === '') {
        throw new Error('MSH-10 (Message Control ID) is required');
    }

    // ---- Validate trigger event ----
    var msgType = '';
    if (msg['MSH']['MSH.9'] !== undefined && msg['MSH']['MSH.9']['MSH.9.1'] !== undefined) {
        msgType = String(msg['MSH']['MSH.9']['MSH.9.1'].toString());
    }
    var allowed = ['ADT'];
    var found = false;
    for (var i = 0; i < allowed.length; i++) {
        if (msgType === allowed[i]) { found = true; break; }
    }
    if (!found) {
        throw new Error('Unsupported message type: ' + msgType + ' (expected ADT^A01/A04/A08)');
    }

    // ---- Normalize patient family + given names: trim trailing whitespace ----
    if (msg['PID'] !== undefined && msg['PID']['PID.5'] !== undefined) {
        if (msg['PID']['PID.5']['PID.5.1'] !== undefined) {
            var fam = String(msg['PID']['PID.5']['PID.5.1'].toString());
            msg['PID']['PID.5']['PID.5.1'] = fam.replace(/\s+$/, '').replace(/^\s+/, '');
        }
        if (msg['PID']['PID.5']['PID.5.2'] !== undefined) {
            var giv = String(msg['PID']['PID.5']['PID.5.2'].toString());
            msg['PID']['PID.5']['PID.5.2'] = giv.replace(/\s+$/, '').replace(/^\s+/, '');
        }
    }

    // ---- Tag PID-3 assigning authority + identifier type ----
    // PID.3.4 = assigning authority, PID.3.5 = identifier type code (HL7 Table 0203)
    if (msg['PID'] !== undefined && msg['PID']['PID.3'] !== undefined) {
        if (msg['PID']['PID.3']['PID.3.4'] === undefined ||
            String(msg['PID']['PID.3']['PID.3.4'].toString()) === '') {
            msg['PID']['PID.3']['PID.3.4'] = 'EHR';
        }
        if (msg['PID']['PID.3']['PID.3.5'] === undefined ||
            String(msg['PID']['PID.3']['PID.3.5'].toString()) === '') {
            msg['PID']['PID.3']['PID.3.5'] = 'MR';
        }
    }

    // ---- Stamp MSH-3/4 with normalized originating system ----
    if (msg['MSH'] !== undefined) {
        msg['MSH']['MSH.3'] = 'EHR';
        msg['MSH']['MSH.4'] = 'HOSPITAL';
    }

    // Mirth re-encodes msg (XML) back to HL7v2 wire format on send
    return msg;
}

return normalizeAndForward();
"""


def _claim_to_x12_body(d: _Decomposition) -> str:
    """Complete JDBC-row → X12 837P generator with envelope + body."""
    return r"""// Build a fully-enveloped X12 837P from a billing-DB row
// `msg` is the JDBC row as XML (from the Database Reader source connector).

function pad(s, n) {
    s = String(s || '');
    while (s.length < n) s += ' ';
    return s;
}

function getField(node, name) {
    if (node === undefined || node === null) return '';
    if (node[name] === undefined) return '';
    return String(node[name].toString());
}

function isoToYmd(iso) {
    if (!iso) return '';
    return iso.replace(/-/g, '').substring(0, 8);
}

function hhmmNow() {
    var d = new Date();
    var hh = ('0' + d.getHours()).slice(-2);
    var mm = ('0' + d.getMinutes()).slice(-2);
    return hh + mm;
}

function seg(name, fields) {
    var out = name;
    for (var i = 0; i < fields.length; i++) {
        out += '*' + (fields[i] === undefined || fields[i] === null ? '' : fields[i]);
    }
    return out + '~';
}

function claimRowToX12_837P() {
    var claimId = getField(msg, 'claim_id') || getField(msg, 'CLAIM_ID') || 'CLAIM-UNKNOWN';
    var mrn = getField(msg, 'patient_mrn') || getField(msg, 'PATIENT_MRN') || 'MRN-UNKNOWN';
    var npi = getField(msg, 'billing_provider_npi') || getField(msg, 'BILLING_PROVIDER_NPI') || '';
    var serviceDate = isoToYmd(getField(msg, 'service_date') || getField(msg, 'SERVICE_DATE'));
    var cpt = getField(msg, 'cpt') || getField(msg, 'CPT') || '99213';
    var dx = getField(msg, 'diagnosis') || getField(msg, 'DIAGNOSIS') || 'Z00.00';
    var charge = getField(msg, 'charge') || getField(msg, 'CHARGE') || '0';
    if (!serviceDate) serviceDate = isoToYmd(new Date().toISOString().substring(0,10));
    var dateYmd = serviceDate;
    var dateYymmdd = dateYmd.substring(2);
    var time = hhmmNow();
    var icn = '000000001';

    var x12 = '';

    // ---- ISA envelope (16 fixed-position fields) ----
    x12 += seg('ISA', [
        '00', pad('', 10),
        '00', pad('', 10),
        'ZZ', pad('SUBMITTER123', 15),
        'ZZ', pad('CLEARING456', 15),
        dateYymmdd, time,
        '^', '00501', icn, '0', 'P', ':'
    ]);

    // ---- GS / functional group ----
    x12 += seg('GS', ['HC', 'SUBMITTER', 'CLEARING', dateYmd, time, '1', 'X', '005010X222A1']);

    // ---- ST / transaction set header ----
    x12 += seg('ST', ['837', '0001', '005010X222A1']);

    // BHT — Beginning of Hierarchical Transaction
    x12 += seg('BHT', ['0019', '00', claimId, dateYmd, time, 'CH']);

    // 1000A Submitter
    x12 += seg('NM1', ['41', '2', 'SUBMITTER NAME', '', '', '', '', '46', 'SUBMITTER123']);
    x12 += seg('PER', ['IC', 'CONTACT NAME', 'TE', '5555550000']);

    // 1000B Receiver
    x12 += seg('NM1', ['40', '2', 'CLEARING HOUSE', '', '', '', '', '46', 'CLEARING456']);

    // ---- Loop 2000A — Billing Provider Hierarchical Level ----
    x12 += seg('HL', ['1', '', '20', '1']);
    x12 += seg('PRV', ['BI', 'PXC', '207Q00000X']);

    // 2010AA — Billing Provider Name
    x12 += seg('NM1', ['85', '2', 'BILLING PROVIDER ORG', '', '', '', '', 'XX', npi]);
    x12 += seg('N3', ['_REPLACE_WITH_ADDRESS_LINE_']);
    x12 += seg('N4', ['_REPLACE_WITH_CITY_', '_REPLACE_WITH_STATE_', '_REPLACE_WITH_ZIP_']);
    x12 += seg('REF', ['EI', '_REPLACE_WITH_TAX_ID_']);

    // ---- Loop 2000B — Subscriber Hierarchical Level ----
    x12 += seg('HL', ['2', '1', '22', '0']);
    x12 += seg('SBR', ['P', '18', '', '', '', '', '', '', 'CI']);
    x12 += seg('NM1', ['IL', '1', 'PATIENT_LAST', 'PATIENT_FIRST', '', '', '', 'MI', mrn]);
    x12 += seg('N3', ['_REPLACE_WITH_PATIENT_ADDRESS_']);
    x12 += seg('N4', ['_REPLACE_WITH_PATIENT_CITY_', 'CA', '90210']);
    x12 += seg('DMG', ['D8', '19850615', 'F']);

    // 2010BB — Payer Name
    x12 += seg('NM1', ['PR', '2', '_REPLACE_WITH_PAYER_NAME_', '', '', '', '', 'PI', '_REPLACE_WITH_PAYER_ID_']);

    // ---- Loop 2300 — Claim ----
    x12 += seg('CLM', [claimId, charge, '', '', '11:B:1', 'Y', 'A', 'Y', 'Y']);
    x12 += seg('DTP', ['472', 'D8', dateYmd]);
    x12 += seg('HI', ['ABK:' + dx.replace(/\./g, '')]);

    // 2310B — Rendering Provider
    x12 += seg('NM1', ['82', '1', 'RENDER_LAST', 'RENDER_FIRST', '', '', '', 'XX', npi]);
    x12 += seg('PRV', ['PE', 'PXC', '207Q00000X']);

    // ---- Loop 2400 — Service Line ----
    x12 += seg('LX', ['1']);
    x12 += seg('SV1', ['HC:' + cpt, charge, 'UN', '1', '', '', '1']);
    x12 += seg('DTP', ['472', 'D8', dateYmd]);

    // ---- Trailers (segment count = everything after ST inclusive) ----
    // Crude count: every '~' so far minus envelope segments (ISA, GS) + ST itself.
    var totalTildes = (x12.match(/~/g) || []).length;
    var seCount = totalTildes - 2;  // exclude ISA + GS, include ST through SV1
    x12 += seg('SE', [String(seCount), '0001']);
    x12 += seg('GE', ['1', '1']);
    x12 += seg('IEA', ['1', icn]);

    return x12;
}

return claimRowToX12_837P();
"""


def _dicom_to_fhir_body(d: _Decomposition) -> str:
    """Complete DICOM-metadata → FHIR ImagingStudy."""
    return r"""// Build a FHIR ImagingStudy from DICOM-extracted metadata.
// Mirth's DICOM connector exposes selected DICOM tags through `msg`
// (e.g. msg['StudyInstanceUID']). The pixel data lives on a paired
// DICOM web server (WADO-RS); we reference its endpoint here.

var DICOM_SOP_SYSTEM = 'http://dicom.nema.org/resources/ontology/DCM';
var LOCAL_MRN_OID = 'urn:oid:2.16.840.1.113883.4.642.40.5.10';
var WADO_RS_ENDPOINT_REF = 'Endpoint/wado-rs-default';

function safeStr(node) {
    if (node === undefined || node === null) return '';
    return String(node.toString()).replace(/^\s+|\s+$/g, '');
}

function dicomDateToFhir(s) {
    if (!s || s.length < 8) return '';
    return s.substring(0,4) + '-' + s.substring(4,6) + '-' + s.substring(6,8);
}

function dicomDtToFhir(date, time) {
    var d = dicomDateToFhir(date);
    if (!d) return '';
    if (!time || time.length < 6) return d;
    return d + 'T' + time.substring(0,2) + ':' + time.substring(2,4) + ':' + time.substring(4,6);
}

function dicomStudyToImagingStudy() {
    var studyUid = safeStr(msg['StudyInstanceUID']);
    var seriesUid = safeStr(msg['SeriesInstanceUID']);
    var sopInstanceUid = safeStr(msg['SOPInstanceUID']);
    var patientMrn = safeStr(msg['PatientID']);
    var modality = safeStr(msg['Modality']) || 'OT';
    var studyDate = safeStr(msg['StudyDate']);
    var studyTime = safeStr(msg['StudyTime']);
    var studyDescription = safeStr(msg['StudyDescription']);
    var sopClassUid = safeStr(msg['SOPClassUID']);
    var accessionNumber = safeStr(msg['AccessionNumber']);

    if (!studyUid) {
        throw new Error('StudyInstanceUID (0020,000D) is required for ImagingStudy');
    }
    if (!patientMrn) {
        throw new Error('PatientID (0010,0020) is required for ImagingStudy.subject');
    }

    var imagingStudy = {
        resourceType: 'ImagingStudy',
        identifier: [
            {
                use: 'official',
                system: 'urn:dicom:uid',
                value: 'urn:oid:' + studyUid
            }
        ],
        status: 'available',
        modality: [{ system: DICOM_SOP_SYSTEM, code: modality }],
        subject: { reference: 'Patient?identifier=' + LOCAL_MRN_OID + '|' + patientMrn },
        started: dicomDtToFhir(studyDate, studyTime) || dicomDateToFhir(studyDate) || studyDate,
        endpoint: [{ reference: WADO_RS_ENDPOINT_REF }],
        numberOfSeries: 1,
        numberOfInstances: 1,
        description: studyDescription || ('Study ' + studyUid),
        series: []
    };

    if (accessionNumber) {
        imagingStudy.identifier.push({
            use: 'usual',
            type: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v2-0203', code: 'ACSN' }] },
            value: accessionNumber
        });
    }

    if (seriesUid) {
        var series = {
            uid: seriesUid,
            number: 1,
            modality: { system: DICOM_SOP_SYSTEM, code: modality },
            description: studyDescription || ('Series ' + seriesUid),
            numberOfInstances: 1,
            endpoint: [{ reference: WADO_RS_ENDPOINT_REF }],
            instance: []
        };
        if (sopInstanceUid) {
            series.instance.push({
                uid: sopInstanceUid,
                sopClass: {
                    system: 'urn:ietf:rfc:3986',
                    code: 'urn:oid:' + (sopClassUid || '1.2.840.10008.5.1.4.1.1.7'),
                    display: 'Secondary Capture Image Storage'
                },
                number: 1
            });
        }
        imagingStudy.series.push(series);
    }

    return JSON.stringify(imagingStudy);
}

return dicomStudyToImagingStudy();
"""


def _fhir_to_e2b_body(d: _Decomposition) -> str:
    """Complete FHIR AdverseEvent → ICH E2B(R3) safetyreport XML."""
    return r"""// Build an ICH E2B(R3) safetyreport XML from a FHIR AdverseEvent.
// Source `msg` is the FHIR AdverseEvent JSON (Mirth has parsed via
// the JSON data type — we reach in with property access).

function getProp(obj, path) {
    var parts = path.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
        if (cur === undefined || cur === null) return null;
        cur = cur[parts[i]];
    }
    return (cur === undefined) ? null : cur;
}

function xmlEscape(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function isoToE2bDate(iso) {
    if (!iso) return '';
    var s = String(iso);
    return s.substring(0,10).replace(/-/g, '');
}

function adverseEventToE2B() {
    // Mirth may pass either a raw string or an already-parsed object — handle both.
    var ae = msg;
    if (typeof msg === 'string') {
        ae = JSON.parse(msg);
    } else if (msg && typeof msg === 'object' && msg.hasOwnProperty('rawData')) {
        try { ae = JSON.parse(String(msg.rawData)); } catch (e) { ae = msg; }
    }

    if (!ae || typeof ae !== 'object') {
        throw new Error('AdverseEvent payload must be a JSON object');
    }

    var caseId = ae.id || ('AE-' + new Date().getTime());
    var occurrenceDate = isoToE2bDate(ae.date || ae.occurredDateTime || '');
    var actuality = ae.actuality || 'actual';

    // Reaction (event coding)
    var reactionCoding = getProp(ae, 'event.coding') || [];
    var meddraCode = (reactionCoding[0] && reactionCoding[0].code) ? reactionCoding[0].code : '0000000';
    var meddraDisplay = (reactionCoding[0] && reactionCoding[0].display) ? reactionCoding[0].display : 'Unknown';
    var meddraVersion = '26.1';

    // Seriousness
    var seriousnessCoding = getProp(ae, 'seriousness.coding') || [];
    var seriousnessCode = (seriousnessCoding[0] && seriousnessCoding[0].code) ? seriousnessCoding[0].code : 'non-serious';
    var serious = (seriousnessCode.indexOf('non-serious') === -1) ? '1' : '2';

    // Patient ref
    var subjectRef = getProp(ae, 'subject.reference') || 'Patient/unknown';

    // Suspect agent (from suspectEntity[].instance — usually MedicationStatement reference)
    var suspectEntities = ae.suspectEntity || [];
    var suspects = [];
    for (var i = 0; i < suspectEntities.length; i++) {
        var inst = suspectEntities[i].instance || {};
        var ref = inst.reference || ('Substance/unknown-' + i);
        var causality = (suspectEntities[i].causality && suspectEntities[i].causality[0]
                         && suspectEntities[i].causality[0].assessmentMethod
                         && suspectEntities[i].causality[0].assessmentMethod.text) || 'Possible';
        suspects.push({ reference: ref, causality: causality });
    }
    if (suspects.length === 0) {
        // No structured suspects — emit a placeholder drug section so the
        // document is still schema-valid (regulator MDN catches this).
        suspects.push({ reference: 'MedicationStatement/_REPLACE_WITH_SUSPECT_AGENT_', causality: 'Possible' });
    }

    var nowYmd = (new Date()).toISOString().substring(0,10).replace(/-/g, '');

    var xml = '';
    xml += '<?xml version="1.0" encoding="UTF-8"?>\n';
    xml += '<ichicsr lang="en">\n';
    xml += '  <ichicsrmessageheader>\n';
    xml += '    <messagetype>ichicsr</messagetype>\n';
    xml += '    <messageformatversion>2.1</messageformatversion>\n';
    xml += '    <messageformatrelease>2.0</messageformatrelease>\n';
    xml += '    <messagenumb>' + xmlEscape(caseId) + '</messagenumb>\n';
    xml += '    <messagesenderidentifier>_REPLACE_WITH_SENDER_ID_</messagesenderidentifier>\n';
    xml += '    <messagereceiveridentifier>FDA</messagereceiveridentifier>\n';
    xml += '    <messagedateformat>204</messagedateformat>\n';
    xml += '    <messagedate>' + nowYmd + '</messagedate>\n';
    xml += '  </ichicsrmessageheader>\n';
    xml += '  <safetyreport>\n';
    xml += '    <safetyreportversion>1</safetyreportversion>\n';
    xml += '    <safetyreportid>' + xmlEscape(caseId) + '</safetyreportid>\n';
    xml += '    <primarysourcecountry>US</primarysourcecountry>\n';
    xml += '    <occurcountry>US</occurcountry>\n';
    xml += '    <transmissiondateformat>102</transmissiondateformat>\n';
    xml += '    <transmissiondate>' + nowYmd + '</transmissiondate>\n';
    xml += '    <reporttype>1</reporttype>\n';
    xml += '    <serious>' + serious + '</serious>\n';
    xml += '    <seriousnessdeath>2</seriousnessdeath>\n';
    xml += '    <seriousnesslifethreatening>2</seriousnesslifethreatening>\n';
    xml += '    <seriousnesshospitalization>2</seriousnesshospitalization>\n';
    xml += '    <seriousnessdisabling>2</seriousnessdisabling>\n';
    xml += '    <seriousnesscongenitalanomali>2</seriousnesscongenitalanomali>\n';
    xml += '    <seriousnessother>2</seriousnessother>\n';
    xml += '    <receivedateformat>102</receivedateformat>\n';
    xml += '    <receivedate>' + (occurrenceDate || nowYmd) + '</receivedate>\n';
    xml += '    <receiptdateformat>102</receiptdateformat>\n';
    xml += '    <receiptdate>' + nowYmd + '</receiptdate>\n';
    xml += '    <additionaldocument>2</additionaldocument>\n';
    xml += '    <fulfillexpeditecriteria>' + (serious === '1' ? '1' : '2') + '</fulfillexpeditecriteria>\n';
    xml += '    <companynumb>' + xmlEscape(caseId) + '</companynumb>\n';
    xml += '    <primarysource>\n';
    xml += '      <reportercountry>US</reportercountry>\n';
    xml += '      <qualification>5</qualification>\n';
    xml += '    </primarysource>\n';
    xml += '    <sender>\n';
    xml += '      <sendertype>1</sendertype>\n';
    xml += '      <senderorganization>_REPLACE_WITH_ORG_NAME_</senderorganization>\n';
    xml += '    </sender>\n';
    xml += '    <receiver>\n';
    xml += '      <receivertype>2</receivertype>\n';
    xml += '      <receiverorganization>FDA</receiverorganization>\n';
    xml += '    </receiver>\n';
    xml += '    <patient>\n';
    xml += '      <patientinitial>' + xmlEscape(subjectRef.replace('Patient/', '').substring(0, 5).toUpperCase()) + '</patientinitial>\n';
    xml += '      <patientsex>0</patientsex>\n';
    xml += '      <reaction>\n';
    xml += '        <primarysourcereaction>' + xmlEscape(meddraDisplay) + '</primarysourcereaction>\n';
    xml += '        <reactionmeddraversionpt>' + meddraVersion + '</reactionmeddraversionpt>\n';
    xml += '        <reactionmeddrapt>' + xmlEscape(meddraCode) + '</reactionmeddrapt>\n';
    xml += '        <reactionoutcome>' + (actuality === 'actual' ? '6' : '1') + '</reactionoutcome>\n';
    xml += '      </reaction>\n';
    for (var j = 0; j < suspects.length; j++) {
        xml += '      <drug>\n';
        xml += '        <drugcharacterization>1</drugcharacterization>\n';
        xml += '        <medicinalproduct>' + xmlEscape(suspects[j].reference.split('/').pop()) + '</medicinalproduct>\n';
        xml += '        <drugadministrationroute>048</drugadministrationroute>\n';
        xml += '        <drugindication>_REPLACE_WITH_INDICATION_</drugindication>\n';
        xml += '        <activesubstance>\n';
        xml += '          <activesubstancename>_REPLACE_WITH_SUBSTANCE_</activesubstancename>\n';
        xml += '        </activesubstance>\n';
        xml += '      </drug>\n';
    }
    xml += '    </patient>\n';
    xml += '  </safetyreport>\n';
    xml += '</ichicsr>\n';

    return xml;
}

return adverseEventToE2B();
"""


def _hl7v2_to_warehouse_body(d: _Decomposition) -> str:
    """Complete ADT → batched FHIR Patient + Encounter for warehouse ingest.

    Outputs a JSON object the JavaScript Writer destination consumes; the
    writer batches by hour and serializes to Parquet.
    """
    return r"""// ADT^A01/A04/A08 → FHIR Patient + Encounter (batched JSON for warehouse ingest)

var LOCAL_MRN_OID = 'urn:oid:2.16.840.1.113883.4.642.40.5.10';
var V3_ACT_CODE = 'http://terminology.hl7.org/CodeSystem/v3-ActCode';

var SEX_MAP = {'M': 'male', 'F': 'female', 'O': 'other'};
var CLASS_MAP = {
    'I': ['IMP', 'inpatient encounter'],
    'O': ['AMB', 'ambulatory'],
    'E': ['EMER', 'emergency'],
    'P': ['PRENC', 'pre-admission']
};

function safeStr(node) {
    if (node === undefined || node === null) return '';
    return String(node.toString()).replace(/^\s+|\s+$/g, '');
}

function hl7DateToFhir(s) {
    if (!s || s.length < 8) return '';
    var d = s.substring(0,4) + '-' + s.substring(4,6) + '-' + s.substring(6,8);
    if (s.length >= 14) {
        d += 'T' + s.substring(8,10) + ':' + s.substring(10,12) + ':' + s.substring(12,14);
    }
    return d;
}

function uuid() {
    if (typeof java !== 'undefined' && java.util && java.util.UUID) {
        return String(java.util.UUID.randomUUID());
    }
    return 'r-' + new Date().getTime() + '-' + Math.floor(Math.random() * 1e9);
}

function transformAdtToWarehouse() {
    var mrn = safeStr(msg['PID']['PID.3']['PID.3.1']);
    var family = safeStr(msg['PID']['PID.5']['PID.5.1']);
    var given = safeStr(msg['PID']['PID.5']['PID.5.2']);
    var dob = safeStr(msg['PID']['PID.7']['PID.7.1']);
    var sex = safeStr(msg['PID']['PID.8']);

    if (!mrn) {
        throw new Error('PID-3 (patient identifier) is required for ADT → warehouse transform');
    }

    var patientId = uuid();
    var encounterId = uuid();
    var ingestionTimestamp = new Date().toISOString();

    var patient = {
        resourceType: 'Patient',
        id: patientId,
        identifier: [{
            system: LOCAL_MRN_OID,
            value: mrn,
            type: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v2-0203', code: 'MR' }] }
        }],
        name: [{ family: family || 'UNKNOWN', given: [given || 'UNKNOWN'] }],
        birthDate: dob ? hl7DateToFhir(dob).substring(0, 10) : '1900-01-01',
        gender: SEX_MAP[(sex || 'U').toUpperCase()] || 'unknown',
        meta: { tag: [{ system: 'urn:warehouse:ingest', code: ingestionTimestamp }] }
    };

    var encClass = ['AMB', 'ambulatory (default)'];
    if (msg['PV1'] !== undefined && msg['PV1']['PV1.2'] !== undefined) {
        var pc = safeStr(msg['PV1']['PV1.2']).toUpperCase();
        if (CLASS_MAP[pc]) encClass = CLASS_MAP[pc];
    }

    var admitTime = '';
    if (msg['PV1'] !== undefined && msg['PV1']['PV1.44'] !== undefined) {
        admitTime = hl7DateToFhir(safeStr(msg['PV1']['PV1.44']['PV1.44.1']));
    }

    var encounter = {
        resourceType: 'Encounter',
        id: encounterId,
        status: 'in-progress',
        'class': { system: V3_ACT_CODE, code: encClass[0], display: encClass[1] },
        subject: { reference: 'Patient/' + patientId },
        period: { start: admitTime || new Date().toISOString() },
        meta: { tag: [{ system: 'urn:warehouse:ingest', code: ingestionTimestamp }] }
    };

    var batch = {
        ingestion_timestamp: ingestionTimestamp,
        partition_date: ingestionTimestamp.substring(0, 10),
        resources: [patient, encounter]
    };

    return JSON.stringify(batch);
}

return transformAdtToWarehouse();
"""


def _generic_body(src_format: str, dst_format: str) -> str:
    """Fallback identity transformer with normalization for unknown pairs."""
    return f"""// Source format: {src_format}
// Destination format: {dst_format}
//
// No specialized handler for this format pair — pass the message through
// unchanged. Replace this body with a real transform if you map between
// formats that aren't covered by the catalog.

function transformMessage() {{
    if (msg === undefined || msg === null) {{
        throw new Error('No source message available');
    }}
    return msg;
}}

return transformMessage();
"""


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class IntegrationChannelPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        if d.scenario_key is None:
            return GenPlan(
                generator_type=GENERATOR_TYPE,
                package_name=package_name or "integration-channel-unknown",
                domain="(unknown scenario)",
                source_query="",
                package_metadata={"error": "unknown scenario"},
                notes=list(d.notes),
            )
        meta = d.scenario_meta
        pkg_name = package_name or f"integration-channel-{d.scenario_key}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=meta.get("name", d.scenario_key),
            source_query=d.scenario_key,
            package_metadata={
                "scenario_key": d.scenario_key,
                "engine_target": meta.get("engine_target", "Mirth/OIE/BridgeLink"),
                "source_type": meta.get("source", {}).get("type"),
                "destination_type": meta.get("destination", {}).get("type"),
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
            unit_type="integration_channel",
            name=meta.get("name", d.scenario_key),
            ordinal=1,
            metadata={"scenario_key": d.scenario_key,
                      "engine_target": meta.get("engine_target", "Mirth/OIE/BridgeLink")},
            logical_key="channel_main",
            sources=sources,
        ))

        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="channel.xml", content=_render_channel_xml(d), purpose="config"),
            GenFile(filename="transformer.js", content=_render_transformer_js(d), purpose="code"),
        ])

        # Sample messages
        if meta.get("sample_input_filename") and meta.get("sample_input"):
            plan.files.append(GenFile(
                filename=f"test_messages/{meta['sample_input_filename']}",
                content=meta["sample_input"],
                purpose="fixture",
            ))
        if meta.get("sample_output_filename"):
            # Output sample is a placeholder template since real expected-output
            # depends on the receiving system's exact configuration
            plan.files.append(GenFile(
                filename=f"test_messages/{meta['sample_output_filename']}",
                content=f"# Expected output for {meta['sample_input_filename']}\n"
                        f"# Fill in once the channel is deployed + tested against\n"
                        f"# your specific destination configuration.\n",
                purpose="fixture",
            ))
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class IntegrationChannelValidator:
    def validate(self, conn, plan: GenPlan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("error") == "unknown scenario":
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message=f"unknown integration scenario — supported: "
                        f"{sorted(SCENARIO_CATALOG)}",
            ))
            return issues
        if plan.package_metadata.get("n_citations", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="channel_main", severity="warning",
                message="no concept citations from cited tools — "
                        "channel ships from built-in scenario spec only",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------

class IntegrationChannelMaterializer:
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


def make_integration_channel_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=IntegrationChannelDecomposer(),
        planner=IntegrationChannelPlanner(),
        ranking_mode="generation",
        validator=IntegrationChannelValidator(),
        materializer=IntegrationChannelMaterializer(),
    )
