"""integration_channel.py — Integration Engine Channel Generator.

For an integration scenario (EHR ↔ Lab, Claim → Clearinghouse, DICOM
ingest, etc.), generates a Mirth/NextGen Connect channel scaffold plus
the JavaScript transformer code, sample messages, and deployment notes.

Output:
    integration-channel-<scenario>/
      README.md                  scenario description + deployment notes
      channel.xml                Mirth Connect channel config
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
from xml.sax.saxutils import escape as xml_escape

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)

LOG = logging.getLogger("mypub-integration-channel")

GENERATOR_TYPE = "integration_channel"


# Scenario catalog. Each entry specifies the source + destination connector
# types, the transformer kind, and the sample messages.
SCENARIO_CATALOG: dict[str, dict[str, Any]] = {
    "ehr-adt-to-lab": {
        "name": "EHR ADT → Lab system",
        "description": "EHR sends a patient admission/registration message "
                       "(ADT^A01/A04/A08) to a downstream lab system. The "
                       "lab needs the patient demographics + encounter "
                       "context to register the patient before result entry.",
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
        "source": {
            "type": "Database Reader",
            "format": "JDBC SQL",
            "details": "Poll the billing DB for claims with status='READY'. "
                       "Lock + claim each row; channel acks on successful submit.",
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
        "source": {
            "type": "HTTP Listener (FHIR)",
            "format": "FHIR JSON",
            "url": "/fhir/R4/AdverseEvent",
            "details": "Webhook from the trial EDC when a new AE is recorded.",
        },
        "destination": {
            "type": "AS2 Sender",
            "format": "ICH E2B(R3) XML",
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
    lines = [
        f"# Integration Channel — {meta.get('name', d.scenario_key)}",
        "",
        meta.get("description", ""),
        "",
        "## Source connector",
        "",
        f"- **Type:** {src.get('type')}",
        f"- **Format:** {src.get('format')}",
    ]
    if src.get("port"):
        lines.append(f"- **Port:** {src['port']}")
    if src.get("url"):
        lines.append(f"- **URL:** `{src['url']}`")
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
        "# Import the channel into a Mirth Connect server",
        "# (Channel Manager → Import Channel → channel.xml)",
        "",
        "# Edit the source/destination connector configs to match your env",
        "# (hostnames, ports, credentials, certs)",
        "",
        "# Deploy + start the channel",
        "# Test by feeding test_messages/sample_input.* to the source endpoint",
        "```",
        "",
        "## Caveats",
        "",
        "- The `channel.xml` ships with the structural skeleton — connector "
           "types, transformer hookup, channel metadata. **Connector "
           "credentials, certs, and destination URLs are placeholders** "
           "and must be filled in for your environment.",
        "- The `transformer.js` is a starting scaffold. Real-world transformers "
           "need extensive defensive code: null/missing-segment handling, "
           "code-system fallback, retry logic on transient destination errors. "
           "The scaffold focuses on the happy path.",
        "- Mirth Connect's JavaScript runtime is Rhino, not modern JS. ES6+ "
           "syntax is partial — stick with ES5 patterns inside the transformer.",
        "",
    ]),
    if d.citations:
        lines.extend(["## Cited tools from the catalog", ""])
        for cid, cname, _ds, src_name in d.citations[:15]:
            lines.append(f"- **{cname}** — {src_name}")
        lines.append("")
    return "\n".join(lines)


def _render_channel_xml(d: _Decomposition) -> str:
    """Generate a Mirth Connect channel.xml skeleton.

    The actual Mirth XML is verbose (hundreds of nested elements with
    runtime defaults). We emit the structural shell + key configurable
    fields; the operator imports + edits in Mirth Channel Manager.
    """
    meta = d.scenario_meta
    src = meta.get("source", {})
    dst = meta.get("destination", {})
    name = xml_escape(meta.get("name", d.scenario_key))
    desc = xml_escape(meta.get("description", ""))
    src_type = xml_escape(src.get("type", ""))
    dst_type = xml_escape(dst.get("type", ""))
    src_format = xml_escape(src.get("format", ""))
    dst_format = xml_escape(dst.get("format", ""))
    transformer_intent = xml_escape(meta.get("transformer_intent", ""))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--
  Mirth Connect channel skeleton — generated by /kb-integration-channel.

  Import this into Mirth Channel Manager, then configure the source +
  destination connector credentials (hostnames, ports, certs).
-->
<channel version="4.4.0">
  <id>auto-{d.scenario_key}</id>
  <nextMetaDataId>1</nextMetaDataId>
  <name>{name}</name>
  <description>{desc}</description>
  <revision>1</revision>
  <enabled>false</enabled>
  <lastModified>
    <time>0</time>
    <timezone>UTC</timezone>
  </lastModified>
  <exportData>
    <metadata>
      <enabled>false</enabled>
      <pruningSettings>
        <pruneErroredMessages>false</pruneErroredMessages>
      </pruningSettings>
    </metadata>
  </exportData>
  <properties>
    <messageStorageMode>DEVELOPMENT</messageStorageMode>
    <encryptData>false</encryptData>
    <removeContentOnCompletion>false</removeContentOnCompletion>
    <removeOnlyFilteredOnCompletion>false</removeOnlyFilteredOnCompletion>
    <removeAttachmentsOnCompletion>false</removeAttachmentsOnCompletion>
    <initialState>STOPPED</initialState>
  </properties>

  <!-- Source connector
       TYPE: {src_type}
       FORMAT: {src_format}
       Configure host/port/auth before deployment.
  -->
  <sourceConnector version="4.4.0">
    <metaDataId>0</metaDataId>
    <name>sourceConnector</name>
    <properties class="placeholderSourceProperties">
      <connectorType>{src_type}</connectorType>
      <messageFormat>{src_format}</messageFormat>
      <!-- TODO: fill in connector-specific properties (port, URL, polling
           interval, credentials, etc.) -->
    </properties>
    <transformer>
      <elements>
        <!-- See transformer.js for the JS body; Mirth Channel Manager will
             surface that file as a Step. -->
      </elements>
    </transformer>
    <filter>
      <elements/>
    </filter>
  </sourceConnector>

  <!-- Destination connector(s)
       TYPE: {dst_type}
       FORMAT: {dst_format}
  -->
  <destinationConnectors>
    <connector version="4.4.0">
      <metaDataId>1</metaDataId>
      <name>{xml_escape(dst.get("type", "destination"))}</name>
      <properties class="placeholderDestProperties">
        <connectorType>{dst_type}</connectorType>
        <messageFormat>{dst_format}</messageFormat>
        <!-- TODO: fill in destination-specific properties -->
      </properties>
      <transformer>
        <elements/>
      </transformer>
      <filter>
        <elements/>
      </filter>
      <enabled>true</enabled>
      <waitForPrevious>true</waitForPrevious>
    </connector>
  </destinationConnectors>

  <!-- Transformer intent (informational; actual code in transformer.js):
       {transformer_intent}
  -->
</channel>
"""


def _render_transformer_js(d: _Decomposition) -> str:
    """Generate a Mirth Connect transformer.js scaffold.

    Mirth's JS runtime is Rhino-based; ES5 patterns are safest. We emit a
    skeleton that wires source → destination + leaves TODO markers for the
    use-case-specific transformation logic.
    """
    meta = d.scenario_meta
    src = meta.get("source", {})
    dst = meta.get("destination", {})
    src_format = src.get("format", "")
    dst_format = dst.get("format", "")
    intent = meta.get("transformer_intent", "")

    # Format-specific scaffolds
    if "HL7v2" in src_format and "FHIR" in dst_format:
        body = _hl7v2_to_fhir_scaffold(d)
    elif "FHIR" in src_format and "DICOM" in src_format:
        body = _generic_scaffold("FHIR JSON", dst_format)
    elif "HL7v2" in src_format and "HL7v2" in dst_format:
        body = _hl7v2_passthrough_scaffold(d)
    elif "JDBC SQL" in src_format and "X12" in dst_format:
        body = _claim_to_x12_scaffold(d)
    elif "DICOM" in src_format and "FHIR" in dst_format:
        body = _dicom_to_fhir_scaffold(d)
    elif "FHIR" in src_format and "XML" in dst_format:
        body = _fhir_to_e2b_scaffold(d)
    else:
        body = _generic_scaffold(src_format, dst_format)

    return f"""// Mirth Connect channel transformer — generated by /kb-integration-channel.
//
// Scenario: {meta.get('name', d.scenario_key)}
// Intent:   {intent}
//
// Mirth runs Rhino — stick to ES5 patterns. `msg` is the Mirth XML
// representation of the source message; the function should return the
// destination payload (string, XML, or object depending on the connector).

{body}
"""


def _hl7v2_to_fhir_scaffold(d: _Decomposition) -> str:
    return """function transformHl7v2ToFhir() {
    // `msg` is the Mirth-parsed HL7v2 message; access via E4X (e.g. msg['PID']['PID.5'])
    var patientMrn = String(msg['PID']['PID.3']['PID.3.1'].toString());
    var familyName = String(msg['PID']['PID.5']['PID.5.1'].toString());
    var givenName = String(msg['PID']['PID.5']['PID.5.2'].toString());
    var dob = String(msg['PID']['PID.7']['PID.7.1'].toString());

    // Build a FHIR Bundle (transaction) — Patient + per-Observation entries
    var bundle = {
        resourceType: "Bundle",
        type: "transaction",
        entry: []
    };

    // Patient (upsert by MRN)
    bundle.entry.push({
        request: { method: "PUT", url: "Patient?identifier=urn:oid:LOCAL_MRN_OID|" + patientMrn },
        resource: {
            resourceType: "Patient",
            identifier: [{ system: "urn:oid:LOCAL_MRN_OID", value: patientMrn }],
            name: [{ family: familyName, given: [givenName] }],
            birthDate: dob.substring(0, 4) + "-" + dob.substring(4, 6) + "-" + dob.substring(6, 8)
        }
    });

    // TODO: Per OBX, build an Observation; group under DiagnosticReport keyed
    // by OBR fill order number. LOINC-translate any local codes; UCUM-translate units.
    // Example pattern:
    //   for each (var obx in msg..OBX) {
    //       var obs = { resourceType: "Observation", ... };
    //       bundle.entry.push({ request: { method: "POST", url: "Observation" }, resource: obs });
    //   }

    return JSON.stringify(bundle);
}

return transformHl7v2ToFhir();
"""


def _hl7v2_passthrough_scaffold(d: _Decomposition) -> str:
    return """function normalizeAndForward() {
    // Validate: required segments
    if (!msg['MSH']['MSH.10'] || msg['MSH']['MSH.10'].toString() === '') {
        throw new Error('MSH-10 (Message Control ID) is required');
    }

    // Normalize patient names: strip trailing whitespace
    if (msg['PID']['PID.5']['PID.5.1']) {
        msg['PID']['PID.5']['PID.5.1'] = String(msg['PID']['PID.5']['PID.5.1']).replace(/\\s+$/, '');
    }

    // Tag PID-3 with originating system identifier in the assigning authority
    // TODO: pull the originating system from a channel constant or message metadata
    var pidThree = msg['PID']['PID.3'];
    if (pidThree && pidThree['PID.3.4']) {
        pidThree['PID.3.4'] = 'EHR';  // assigning authority
    }

    return msg;  // Mirth re-encodes to HL7v2 wire format on send
}

return normalizeAndForward();
"""


def _claim_to_x12_scaffold(d: _Decomposition) -> str:
    return """function claimToX12_837P() {
    // `msg` is the JDBC row as XML; pull fields with E4X
    var claimId = String(msg['claim_id']);
    var patientMrn = String(msg['patient_mrn']);
    var npi = String(msg['billing_provider_npi']);
    var serviceDate = String(msg['service_date']).replace(/-/g, '');  // YYYYMMDD

    // Build X12 837P envelope + body
    // Real impl uses pyx12 or a Java library inside Mirth via the Custom JAR loader
    var seg = function(name, fields) { return name + '*' + fields.join('*') + '~'; };

    var x12 = '';
    x12 += seg('ISA', ['00', '          ', '00', '          ', 'ZZ', 'SUBMITTER123  ', 'ZZ', 'CLEARING456    ', '260115', '1430', '^', '00501', '000000001', '0', 'P', ':']);
    x12 += seg('GS', ['HC', 'SUBMITTER', 'CLEARING', '20260115', '1430', '1', 'X', '005010X222A1']);
    x12 += seg('ST', ['837', '0001', '005010X222A1']);
    x12 += seg('BHT', ['0019', '00', claimId, '20260115', '1430', 'CH']);
    x12 += seg('NM1', ['85', '2', 'BILLING PROVIDER', '', '', '', '', 'XX', npi]);
    x12 += seg('CLM', [claimId, '150.00', '', '', '11:B:1', 'Y', 'A', 'Y', 'Y']);
    // TODO: complete service-line loop (LX, SV1, DTP*472)
    // x12 += seg('SE', ...) // segment count
    // x12 += seg('GE', ['1', '1']);
    // x12 += seg('IEA', ['1', '000000001']);
    return x12;
}

return claimToX12_837P();
"""


def _dicom_to_fhir_scaffold(d: _Decomposition) -> str:
    return """function dicomStudyToImagingStudy() {
    // `msg` here is the metadata extracted by the DICOM SCP (study/series/instance UIDs)
    // The actual pixel data is stored separately and accessed via WADO-RS.

    var studyUid = String(msg['StudyInstanceUID']);
    var patientMrn = String(msg['PatientID']);
    var modality = String(msg['Modality']);
    var studyDate = String(msg['StudyDate']);

    var imagingStudy = {
        resourceType: "ImagingStudy",
        identifier: [{
            system: "urn:dicom:uid",
            value: "urn:oid:" + studyUid
        }],
        status: "available",
        subject: { reference: "Patient?identifier=urn:oid:LOCAL_MRN_OID|" + patientMrn },
        started: studyDate.substring(0,4) + "-" + studyDate.substring(4,6) + "-" + studyDate.substring(6,8),
        modality: [{ system: "http://dicom.nema.org/resources/ontology/DCM", code: modality }],
        endpoint: [{ reference: "Endpoint/wado-rs-default" }],
        // TODO: populate series + instance lists from msg
        series: []
    };

    return JSON.stringify(imagingStudy);
}

return dicomStudyToImagingStudy();
"""


def _fhir_to_e2b_scaffold(d: _Decomposition) -> str:
    return """function adverseEventToE2B() {
    // Mirth has parsed the incoming JSON; access via msg.<field>
    var ae = JSON.parse(msg);  // or use msg directly if Mirth has parsed

    // Build ICH E2B(R3) XML shell
    // Real impl uses an XSD-validating XML serializer
    var caseId = ae.id || 'AE-UNKNOWN';
    var meddraCode = ae.event && ae.event.coding && ae.event.coding[0]
        ? ae.event.coding[0].code : '0000000';
    var seriousness = ae.seriousness && ae.seriousness.coding && ae.seriousness.coding[0]
        ? ae.seriousness.coding[0].code : 'unknown';

    var xml = '<?xml version="1.0" encoding="UTF-8"?>\\n';
    xml += '<safetyreport>\\n';
    xml += '  <safetyreportid>' + caseId + '</safetyreportid>\\n';
    xml += '  <patient>\\n';
    xml += '    <reaction>\\n';
    xml += '      <primarysourcereaction>' + meddraCode + '</primarysourcereaction>\\n';
    xml += '      <reactionmeddrapt>' + meddraCode + '</reactionmeddrapt>\\n';
    xml += '    </reaction>\\n';
    xml += '  </patient>\\n';
    xml += '  <serious>' + (seriousness === 'serious' ? '1' : '2') + '</serious>\\n';
    // TODO: populate suspect agent (from MedicationStatement.medicationReference),
    //       reporter qualification, causality assessment
    xml += '</safetyreport>\\n';
    return xml;
}

return adverseEventToE2B();
"""


def _generic_scaffold(src_format: str, dst_format: str) -> str:
    return f"""function transformMessage() {{
    // Source format: {src_format}
    // Destination format: {dst_format}
    //
    // TODO: implement the transformation logic.
    // Mirth provides `msg` (parsed source message) and expects you to
    // return the destination payload (string, XML, or object).
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
            metadata={"scenario_key": d.scenario_key},
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
