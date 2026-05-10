"""Tests for the Integration Channel generator."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import integration_channel  # noqa: E402


# ---- Catalog hygiene ----------------------------------------------------

def test_every_scenario_has_required_keys():
    for key, meta in integration_channel.SCENARIO_CATALOG.items():
        for k in ("name", "description", "source", "destination",
                  "transformer_intent", "tools_cited",
                  "sample_input_filename", "sample_input"):
            assert k in meta, f"{key} missing {k}"
        for inner in ("source", "destination"):
            for sub in ("type", "format", "details"):
                assert sub in meta[inner], f"{key}.{inner} missing {sub}"


def test_sample_inputs_are_nonempty():
    for key, meta in integration_channel.SCENARIO_CATALOG.items():
        sample = meta["sample_input"]
        assert sample.strip(), f"{key} sample_input is empty"
        assert len(sample) > 50, f"{key} sample_input is suspiciously short"


# ---- Decomposer ---------------------------------------------------------

class _MockConn:
    def execute(self, *args, **kwargs):
        class _R:
            def fetchall(self_inner):
                return []
        return _R()


def test_decomposer_resolves_scenario_keys():
    dec = integration_channel.IntegrationChannelDecomposer()
    for key in integration_channel.SCENARIO_CATALOG:
        d = dec.decompose(_MockConn(), None, key)
        assert d.scenario_key == key, f"{key!r} → {d.scenario_key}"


def test_decomposer_resolves_loose_phrasing():
    dec = integration_channel.IntegrationChannelDecomposer()
    samples = [
        ("EHR ADT to Lab",                           "ehr-adt-to-lab"),
        ("lab result to EHR FHIR",                   "lab-result-to-ehr-fhir"),
        ("claim to clearinghouse",                   "claim-to-clearinghouse"),
        ("imaging study ingest from DICOM",          "imaging-study-ingest"),
        ("ADT to data warehouse",                    "adt-to-warehouse"),
        ("adverse event to regulator submission",    "adverse-event-to-regulator"),
    ]
    for query, expected in samples:
        d = dec.decompose(_MockConn(), None, query)
        assert d.scenario_key == expected, (
            f"{query!r} → {d.scenario_key}, expected {expected}"
        )


def test_decomposer_unknown_scenario():
    dec = integration_channel.IntegrationChannelDecomposer()
    d = dec.decompose(_MockConn(), None, "blockchain immunization registry")
    assert d.scenario_key is None
    assert any("supported" in n.lower() for n in d.notes)


# ---- Renderers ----------------------------------------------------------

def _decomp(key="ehr-adt-to-lab"):
    return integration_channel.IntegrationChannelDecomposer().decompose(
        _MockConn(), None, key,
    )


def test_render_channel_xml_is_valid_xml():
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        xml_text = integration_channel._render_channel_xml(d)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            pytest.fail(f"{key}: channel.xml not valid XML: {e}")
        assert root.tag == "channel"
        # Has source + destination connectors
        assert root.find("sourceConnector") is not None
        assert root.find("destinationConnectors") is not None


def test_render_transformer_js_picks_correct_scaffold():
    """Scenario format pair determines which scaffold body is used."""
    # HL7v2 → FHIR
    d = _decomp("lab-result-to-ehr-fhir")
    js = integration_channel._render_transformer_js(d)
    assert "transformHl7v2ToFhir" in js
    # JDBC → X12
    d = _decomp("claim-to-clearinghouse")
    js = integration_channel._render_transformer_js(d)
    assert "claimToX12_837P" in js
    # DICOM → FHIR
    d = _decomp("imaging-study-ingest")
    js = integration_channel._render_transformer_js(d)
    assert "dicomStudyToImagingStudy" in js
    # FHIR → E2B
    d = _decomp("adverse-event-to-regulator")
    js = integration_channel._render_transformer_js(d)
    assert "adverseEventToE2B" in js
    # HL7v2 passthrough (ADT to Lab is HL7v2 → HL7v2)
    d = _decomp("ehr-adt-to-lab")
    js = integration_channel._render_transformer_js(d)
    assert "normalizeAndForward" in js


def test_render_readme_lists_connectors():
    md = integration_channel._render_readme(_decomp("lab-result-to-ehr-fhir"))
    assert "## Source connector" in md
    assert "## Destination connector" in md
    assert "## Transformer intent" in md
    assert "## Caveats" in md


def test_xml_safely_escapes_special_chars():
    """The XML renderer should not break when content has &, <, >."""
    # Patch a scenario meta with special chars; build via the renderer
    fake_meta = dict(integration_channel.SCENARIO_CATALOG["ehr-adt-to-lab"])
    fake_meta["description"] = "EHR <-> Lab & friends"
    d = integration_channel._Decomposition(
        scenario_key="ehr-adt-to-lab",
        scenario_meta=fake_meta,
        citations=[],
    )
    xml_text = integration_channel._render_channel_xml(d)
    # Should not crash; should contain the escaped chars
    root = ET.fromstring(xml_text)
    desc = root.find("description")
    assert desc is not None and "EHR <-> Lab & friends" in desc.text


# ---- Live integration ---------------------------------------------------

def test_generator_end_to_end_on_real_catalog(tmp_path):
    catalog = ROOT / "data" / "catalog.ddb"
    if not catalog.exists():
        pytest.skip("catalog not present")

    import duckdb
    sys.path.insert(0, str(ROOT / "scripts"))
    from resolution import EntityResolver  # noqa: E402

    conn = duckdb.connect(str(catalog))
    try:
        resolver = EntityResolver(conn)
        gen = integration_channel.make_integration_channel_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "lab-result-to-ehr-fhir",
            output_root=str(tmp_path), overwrite=True,
        )
        errors = [i for i in issues if i.severity == "error"]
        assert package_id > 0 and not errors, f"persistence failed: {issues}"

        pkg_dir = tmp_path / "integration-channel-lab-result-to-ehr-fhir"
        for f in ("README.md", "channel.xml", "transformer.js",
                  "test_messages/sample_oru_r01.hl7"):
            assert (pkg_dir / f).exists(), f"missing {f}"

        # channel.xml is valid XML
        ET.fromstring((pkg_dir / "channel.xml").read_text())

        # transformer.js calls the scenario-specific function
        js = (pkg_dir / "transformer.js").read_text()
        assert "transformHl7v2ToFhir" in js
    finally:
        conn.close()
