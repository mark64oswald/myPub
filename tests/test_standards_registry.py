from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
sys.path.insert(0, str(ROOT / "scripts"))

import standards  # noqa: E402
import standards_registry  # noqa: E402

SCHEMA = (ROOT / "schemas" / "catalog.sql").read_text()


def test_shipped_registry_is_valid():
    reg = standards.read_registry()
    assert standards.validate(reg) == []
    keys = {s["key"] for s in reg["standards"]}
    assert {"hl7v2", "fhir-r4", "x12-5010"} <= keys


def test_licensed_standard_cannot_index_its_spec():
    reg = {"standards": [{
        "key": "x", "name": "X", "license_tier": "licensed",
        "sources": [{"name": "TR3", "role": "spec", "source_type": "pdf",
                     "identifier": "tr3"}]}]}
    assert any("licensed" in e for e in standards.validate(reg))


def test_validate_catches_bad_relation_and_pointer():
    reg = {"standards": [{"key": "a", "name": "A", "license_tier": "open",
                          "sources": [{"name": "s", "role": "library"}]}],
           "relations": [{"from": "a", "to": "zz", "relation": "MAPS_TO"}]}
    errs = standards.validate(reg)
    assert any("needs source_type" in e for e in errs)
    assert any("unknown to" in e for e in errs)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA)
    c.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
              "VALUES ('pyx12 docs', 'deepwiki', 'deepwiki', 'azoner/pyx12'), "
              "('FHIR R4 core', 'fhir_package', 'local', 'hl7.fhir.r4.core@4.0.1')")
    ids = dict(c.execute("SELECT identifier, doc_source_id FROM doc_source").fetchall())
    for ident, n in (("azoner/pyx12", 2), ("hl7.fhir.r4.core@4.0.1", 0)):
        snap = c.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) "
                         "RETURNING snapshot_id", [ids[ident]]).fetchone()[0]
        for i in range(n):
            c.execute("INSERT INTO doc_section (snapshot_id, heading_text, ordinal) "
                      "VALUES (?, 'h', ?)", [snap, i])
    return c


REG = {
    "standards": [
        {"key": "x12", "name": "X12", "license_tier": "licensed",
         "generators": ["edi_roundtrip"],
         "sources": [{"name": "pyx12", "role": "library", "match": "%pyx12%"},
                     {"name": "Stedi", "role": "tool", "match": "%Stedi%"}]},
        {"key": "r4", "name": "FHIR R4", "license_tier": "open", "family": "fhir",
         "sources": [{"name": "core", "role": "spec", "source_type": "fhir_package",
                      "identifier": "hl7.fhir.r4.core@4.0.1"},
                     {"name": "R5", "role": "spec", "source_type": "fhir_package",
                      "identifier": "hl7.fhir.r5.core@5.0.0", "planned": True}]},
    ],
    "relations": [{"from": "x12", "to": "r4", "relation": "MAPS_TO"}],
}


def test_load_is_a_full_refresh(conn):
    assert standards.load(conn, REG) == {"standards": 2, "sources": 4, "relations": 1}
    assert standards.load(conn, REG)["sources"] == 4
    assert conn.execute("SELECT COUNT(*) FROM standard_source").fetchone()[0] == 4


def test_standard_sources_resolves_loaded_only(conn):
    standards.load(conn, REG)
    rows = standards.standard_sources(conn, generator="edi_roundtrip")
    assert [(r["name"], r["n_sections"]) for r in rows] == [("pyx12", 2)]
    assert standards.standard_sources(conn, family="fhir") == []


def test_status_states(conn):
    standards.load(conn, REG)
    states = {r["name"]: r["state"] for r in standards.status(conn)}
    assert states == {"pyx12": "loaded", "Stedi": "missing",
                      "core": "registered, no sections", "R5": "planned"}


def test_cli_check_and_dry_run(capsys):
    assert standards_registry.main(["check"]) == 0
    assert standards_registry.main(["load"]) == 0
    assert "dry run" in capsys.readouterr().out


def test_infer_standard_keys():
    assert standards.infer_standard_keys("HL7v2 ADT^A01 to FHIR Patient") == [
        "hl7v2", "fhir-r4", "hl7v2-to-fhir"]
    assert standards.infer_standard_keys("X12 837P to FHIR Claim") == ["fhir-r4", "x12-5010"]
    assert standards.infer_standard_keys("dicom series") == ["dicom-deid"]
    assert standards.infer_standard_keys("event sourcing") == []


def test_cited_source_names_adds_loaded_registry_sources(conn):
    # No registry tables yet → tools_cited unchanged.
    assert standards.cited_source_names(conn, ["Stedi"], "x12 835") == ["Stedi"]
    standards.load(conn, {"standards": [
        {"key": "x12-5010", "name": "X12", "license_tier": "licensed",
         "sources": [{"name": "pyx12", "role": "library", "match": "%pyx12%"}]}]})
    assert standards.cited_source_names(conn, ["Stedi"], "x12 835") == ["Stedi", "pyx12 docs"]
    # Registry spec sources lead, ahead of the hard-coded tools.
    standards.load(conn, {"standards": [
        {"key": "fhir-r4", "name": "R4", "license_tier": "open",
         "sources": [{"name": "core", "role": "spec", "source_type": "fhir_package",
                      "identifier": "hl7.fhir.r4.core@4.0.1"}]}]})
    conn.execute("INSERT INTO doc_section (snapshot_id, ordinal) SELECT snapshot_id, 0 "
                 "FROM doc_snapshot JOIN doc_source USING (doc_source_id) "
                 "WHERE name = 'FHIR R4 core'")
    assert standards.cited_source_names(conn, ["HAPI"], "fhir") == ["FHIR R4 core", "HAPI"]
    assert standards.cited_source_names(conn, ["Stedi"], "dicom only") == ["Stedi"]


def test_cite_concepts_round_robins_across_sources(conn):
    ids = dict(conn.execute("SELECT name, doc_source_id FROM doc_source").fetchall())
    sec = {}
    for name, sid in ids.items():
        snap = conn.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) "
                            "RETURNING snapshot_id", [sid]).fetchone()[0]
        sec[name] = conn.execute("INSERT INTO doc_section (snapshot_id, ordinal) "
                                 "VALUES (?, 0) RETURNING doc_section_id", [snap]).fetchone()[0]
    hub = conn.execute("INSERT INTO concept (name) VALUES ('hub') RETURNING concept_id").fetchone()[0]
    for name, n in (("FHIR R4 core", 5), ("pyx12 docs", 5)):
        for i in range(n):
            cid = conn.execute("INSERT INTO concept (name) VALUES (?) RETURNING concept_id",
                               [f"{name} c{i}"]).fetchone()[0]
            conn.execute("INSERT INTO concept_relation VALUES (?, ?, 'RELATED_TO', 1.0, "
                         "'doc_section', ?, now())", [cid, hub, sec[name]])
    cites = standards.cite_concepts(conn, ["pyx12 docs", "FHIR R4 core"], limit=4)
    assert len(cites) == 4
    assert {c[3] for c in cites} == {"pyx12 docs", "FHIR R4 core"}
    # The first-named source leads each round.
    assert [c[3] for c in cites] == ["pyx12 docs", "FHIR R4 core"] * 2
    first = standards.cite_concepts(conn, ["FHIR R4 core", "pyx12 docs"], limit=1)
    assert first[0][3] == "FHIR R4 core"
    only = standards.cite_concepts(conn, ["pyx12 docs"], limit=10, name_like="%c1%")
    assert [c[1] for c in only] == ["pyx12 docs c1"]


def test_infer_us_core_with_hyphen():
    assert "us-core" in standards.infer_standard_keys("base_ig us-core + davinci-pas")
    assert "davinci-pas" in standards.infer_standard_keys("base_ig us-core + davinci-pas")
