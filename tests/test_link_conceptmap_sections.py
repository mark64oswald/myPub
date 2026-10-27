from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import link_conceptmap_sections as lcm  # noqa: E402


def test_parse_heading():
    assert lcm.parse_heading("Segment PID to Patient Map") == (
        ["PID segment", "PID"], ["Patient resource", "Patient", "FHIR Patient"])
    src, _ = lcm.parse_heading("Message ADT_A01 to Bundle Map")
    assert src[0] == "ADT^A01 message"
    assert lcm.parse_heading("Introduction") is None


def test_plan_and_write_edges():
    c = duckdb.connect(":memory:")
    c.execute((ROOT / "schemas" / "catalog.sql").read_text())
    sid = c.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                    "VALUES ('v2', 'fhir_package', 'local', 'x') RETURNING doc_source_id").fetchone()[0]
    snap = c.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) RETURNING snapshot_id",
                     [sid]).fetchone()[0]
    for i, h in enumerate(["Segment PID to Patient Map", "Segment ZZZ to Nothing Map", "Intro"]):
        c.execute("INSERT INTO doc_section (snapshot_id, heading_text, ordinal) VALUES (?, ?, ?)",
                  [snap, h, i])
    c.execute("INSERT INTO concept (name) VALUES ('PID segment'), ('Patient')")
    edges = lcm.plan_edges(c, sid)
    assert len(edges) == 1 and edges[0][3] == "Segment PID to Patient Map"
    assert lcm.write_edges(c, edges) == 1
    assert lcm.write_edges(c, edges) == 0
    assert c.execute("SELECT relation_type FROM concept_relation").fetchall() == [("MAPS_TO",)]
