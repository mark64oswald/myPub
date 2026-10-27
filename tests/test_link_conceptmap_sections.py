from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import link_conceptmap_sections as lcm  # noqa: E402
import standards  # noqa: E402


def test_parse_heading():
    src, tgt = lcm.parse_heading("Segment PID to Patient Map")
    assert src == [("PID segment", True), ("HL7 v2 PID", True), ("PID", False)]
    assert tgt[0] == ("Patient resource", True) and tgt[-1] == ("Patient", False)
    assert lcm.parse_heading("Message ADT_A01 to Bundle Map")[0][0] == ("ADT^A01 message", True)
    # Datatype / vocabulary maps collide with unrelated concepts: not linked.
    assert lcm.parse_heading("Datatype CWE to Coding Map") is None
    assert lcm.parse_heading("Introduction") is None


def _world():
    c = duckdb.connect(":memory:")
    c.execute((ROOT / "schemas" / "catalog.sql").read_text())

    def source(name, ident):
        sid = c.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                        "VALUES (?, 'fhir_package', 'local', ?) RETURNING doc_source_id",
                        [name, ident]).fetchone()[0]
        snap = c.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) "
                         "RETURNING snapshot_id", [sid]).fetchone()[0]
        return sid, snap

    v2, v2snap = source("v2-to-FHIR", "hl7.fhir.uv.v2mappings@1.0.0")
    r4, r4snap = source("FHIR R4 core", "hl7.fhir.r4.core@4.0.1")
    for i, h in enumerate(["Segment PID to Patient Map", "Segment ED to Encounter Map"]):
        c.execute("INSERT INTO doc_section (snapshot_id, heading_text, ordinal) VALUES (?, ?, ?)",
                  [v2snap, h, i])
    r4sec = c.execute("INSERT INTO doc_section (snapshot_id, ordinal) VALUES (?, 0) "
                      "RETURNING doc_section_id", [r4snap]).fetchone()[0]
    ids = {}
    for n in ("PID segment", "Patient", "ED", "Encounter", "Resource"):
        ids[n] = c.execute("INSERT INTO concept (name) VALUES (?) RETURNING concept_id",
                           [n]).fetchone()[0]
    # "Patient" is tied to FHIR docs; "Encounter" and "ED" (the editor) aren't.
    c.execute("INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
              "source_type, source_id) VALUES (?, ?, 'EXTENDS', 'doc_section', ?)",
              [ids["Patient"], ids["Resource"], r4sec])
    standards.load(c, {"standards": [
        {"key": "fhir-r4", "name": "R4", "license_tier": "open",
         "sources": [{"name": "core", "role": "spec", "source_type": "fhir_package",
                      "identifier": "hl7.fhir.r4.core@4.0.1"}]}]})
    return c, v2


def test_bare_names_need_hl7_fhir_scope():
    c, v2 = _world()
    edges = lcm.plan_edges(c, v2)
    assert [e[3] for e in edges] == ["Segment PID to Patient Map"]
    assert lcm.write_edges(c, edges) == 1
    assert lcm.write_edges(c, edges) == 0
    assert c.execute("SELECT count(*) FROM concept_relation WHERE relation_type='MAPS_TO'"
                     ).fetchone()[0] == 1
