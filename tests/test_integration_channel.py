"""Tests for the Integration Channel generator."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import integration_channel  # noqa: E402
from healthcare_libs import integration_channel_xml  # noqa: E402


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


def test_every_scenario_declares_engine_target():
    """Each scenario must specify which engine the channel.xml targets."""
    for key, meta in integration_channel.SCENARIO_CATALOG.items():
        assert "engine_target" in meta, f"{key} missing engine_target"
        # Default catalog ships Mirth/OIE/BridgeLink — share the same schema
        assert meta["engine_target"], f"{key} engine_target is empty"


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


def test_render_channel_xml_xmllint_passes():
    """Every scenario's channel.xml must pass xmllint --noout if available."""
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        pytest.skip("xmllint not available")
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        xml_text = integration_channel._render_channel_xml(d)
        proc = subprocess.run(
            [xmllint, "--noout", "-"],
            input=xml_text, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            f"{key}: xmllint rejected channel.xml:\n{proc.stderr}"
        )


# ---- channel.xml structure required for Mirth/OIE/BridgeLink import ----

REQUIRED_TOP_LEVEL_ELEMENTS = [
    "id", "nextMetaDataId", "name", "description", "revision",
    "enabled", "lastModified", "exportData", "properties",
    "sourceConnector", "destinationConnectors",
]


def test_channel_xml_has_all_required_top_level_elements():
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        root = ET.fromstring(integration_channel._render_channel_xml(d))
        for elem in REQUIRED_TOP_LEVEL_ELEMENTS:
            assert root.find(elem) is not None, (
                f"{key}: channel.xml missing required <{elem}>"
            )


def test_channel_xml_id_is_uuid_shape():
    """The <id> field should look like a UUID (8-4-4-4-12 hex)."""
    uuid_re = re.compile(r"^[a-z0-9-]+$", re.IGNORECASE)
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        root = ET.fromstring(integration_channel._render_channel_xml(d))
        cid = root.find("id").text
        assert cid and uuid_re.match(cid), (
            f"{key}: channel id {cid!r} doesn't look like a Mirth id"
        )


def test_channel_xml_source_connector_has_real_properties():
    """The source connector <properties> must carry a class= attribute
    that names a real Mirth Java properties class — not the
    placeholderSourceProperties marker the old generator used.
    """
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        root = ET.fromstring(integration_channel._render_channel_xml(d))
        src = root.find("sourceConnector")
        assert src is not None
        props = src.find("properties")
        assert props is not None
        cls = props.attrib.get("class")
        assert cls and "placeholder" not in cls.lower(), (
            f"{key}: source properties class is missing or a placeholder: {cls!r}"
        )
        assert cls.startswith("com.mirth.connect."), (
            f"{key}: expected a com.mirth.connect.* class, got {cls!r}"
        )


def test_channel_xml_destination_connector_has_real_properties():
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        root = ET.fromstring(integration_channel._render_channel_xml(d))
        dests = root.find("destinationConnectors")
        assert dests is not None
        for connector in dests.findall("connector"):
            props = connector.find("properties")
            assert props is not None
            cls = props.attrib.get("class")
            assert cls and "placeholder" not in cls.lower(), (
                f"{key}: destination properties class missing or placeholder: {cls!r}"
            )
            assert cls.startswith("com.mirth.connect."), (
                f"{key}: expected a com.mirth.connect.* class, got {cls!r}"
            )


def test_channel_xml_contains_sentinel_tokens_not_silent_blanks():
    """Operator-replaceable values should ship as visible sentinel
    tokens (``_REPLACE_WITH_*_``) so operators know what to fill in.
    Channels with all-internal listener defaults are fine — but at
    least one sentinel should appear in scenarios that have an external
    destination (host, URL, credentials, path).
    """
    scenarios_with_external_endpoints = [
        "lab-result-to-ehr-fhir",
        "claim-to-clearinghouse",
        "imaging-study-ingest",
        "adverse-event-to-regulator",
    ]
    for key in scenarios_with_external_endpoints:
        d = _decomp(key)
        xml_text = integration_channel._render_channel_xml(d)
        assert "_REPLACE_WITH_" in xml_text, (
            f"{key}: external endpoint scenario should have at least one "
            f"_REPLACE_WITH_* sentinel for an operator to fill in"
        )


def test_channel_xml_filter_block_present():
    for key in integration_channel.SCENARIO_CATALOG:
        d = _decomp(key)
        root = ET.fromstring(integration_channel._render_channel_xml(d))
        src = root.find("sourceConnector")
        assert src.find("filter") is not None, f"{key}: source has no <filter>"
        for connector in root.find("destinationConnectors").findall("connector"):
            assert connector.find("filter") is not None, (
                f"{key}: a destination has no <filter>"
            )


# ---- transformer.js completeness + validity ----------------------------

def test_render_transformer_js_picks_correct_handler():
    """Scenario format pair determines which complete handler body is used."""
    expected_fns = {
        "lab-result-to-ehr-fhir": "transformOruR01ToFhir",
        "claim-to-clearinghouse": "claimRowToX12_837P",
        "imaging-study-ingest": "dicomStudyToImagingStudy",
        "adverse-event-to-regulator": "adverseEventToE2B",
        "ehr-adt-to-lab": "normalizeAndForward",
        "adt-to-warehouse": "transformAdtToWarehouse",
    }
    for key, fn_name in expected_fns.items():
        d = _decomp(key)
        js = integration_channel._render_transformer_js(d)
        assert fn_name in js, (
            f"{key}: expected handler function {fn_name!r} in transformer.js"
        )


# Scenarios that ship complete handler bodies — all in the catalog.
ALL_SCENARIOS = list(integration_channel.SCENARIO_CATALOG)


@pytest.mark.parametrize("key", ALL_SCENARIOS)
def test_transformer_js_has_no_todo_markers_in_handler_body(key):
    """Every generated transformer.js must contain a complete handler
    body — no `TODO:` markers (case-insensitive) in the body. The
    leading comment header may mention 'TODO' if it documents a known
    deployment-time step, but handler functions must be complete.
    """
    d = _decomp(key)
    js = integration_channel._render_transformer_js(d)
    # Strip the leading // header comment block (starts with `//`,
    # ends at first non-comment line). What remains is the handler body.
    body_lines = []
    in_header = True
    for line in js.split("\n"):
        if in_header and line.lstrip().startswith("//"):
            continue
        in_header = False
        body_lines.append(line)
    body = "\n".join(body_lines)
    todo_pattern = re.compile(r"\bTODO\b", re.IGNORECASE)
    matches = todo_pattern.findall(body)
    assert not matches, (
        f"{key}: transformer.js handler body contains TODO marker(s): {matches}"
    )


@pytest.mark.parametrize("key", ALL_SCENARIOS)
def test_transformer_js_has_balanced_braces(key):
    """Quick syntactic-shape check: braces, parens, brackets all balanced.

    Walks the JS character-by-character, tracking string/regex/comment
    state so that braces inside string literals or comments don't get
    counted. Good enough as a smoke check; the authoritative parse
    check is :func:`test_transformer_js_passes_node_check` below.
    """
    d = _decomp(key)
    js = integration_channel._render_transformer_js(d)
    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {v: k for k, v in pairs.items()}
    stack: list[tuple[str, int]] = []
    i = 0
    n = len(js)
    state = "code"   # one of: code, string_s, string_d, line_comment, block_comment, regex
    prev_meaningful = ""  # last non-whitespace char, for regex disambiguation
    while i < n:
        ch = js[i]
        if state == "code":
            if ch == "/" and i + 1 < n and js[i + 1] == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and i + 1 < n and js[i + 1] == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == "'":
                state = "string_s"
                i += 1
                continue
            if ch == '"':
                state = "string_d"
                i += 1
                continue
            # regex literal heuristic: '/' after an operator/(/[/,/=/!/:/;/{
            if ch == "/" and prev_meaningful in "(=,!:;{[&|?+-*%~^<>" + "":
                if prev_meaningful != "":
                    state = "regex"
                    i += 1
                    continue
            if ch in pairs:
                stack.append((ch, i))
            elif ch in closers:
                if not stack or stack[-1][0] != closers[ch]:
                    pytest.fail(f"{key}: unbalanced JS: unexpected {ch!r} at {i}")
                stack.pop()
            if not ch.isspace():
                prev_meaningful = ch
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and i + 1 < n and js[i + 1] == "/":
                state = "code"
                i += 2
                continue
        elif state == "string_s":
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                state = "code"
                prev_meaningful = "'"
        elif state == "string_d":
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                state = "code"
                prev_meaningful = '"'
        elif state == "regex":
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "/":
                state = "code"
                prev_meaningful = "/"
        i += 1
    assert not stack, f"{key}: unclosed JS braces left: {[c for c, _ in stack]}"


@pytest.mark.parametrize("key", ALL_SCENARIOS)
def test_transformer_js_passes_node_check(key):
    """Use `node --check` if available to confirm the JS parses.

    Mirth runs Rhino but Node's V8 parser is a strict superset for ES5
    syntactic constructs. We only check parse, not runtime semantics.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    d = _decomp(key)
    js = integration_channel._render_transformer_js(d)
    # Wrap in a function so the bare top-level `return` statement parses
    # under Node's script context (Rhino allows it because Mirth wraps
    # the body in an implicit function — Node doesn't).
    wrapped = "function _wrap(msg, tmp) {\n" + js + "\n}"
    proc = subprocess.run(
        [node, "--check", "-"],
        input=wrapped, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"{key}: node --check rejected transformer.js:\n{proc.stderr}"
    )


# ---- README + XML edge cases -------------------------------------------

def test_render_readme_lists_connectors():
    md = integration_channel._render_readme(_decomp("lab-result-to-ehr-fhir"))
    assert "## Source connector" in md
    assert "## Destination connector" in md
    assert "## Transformer intent" in md
    assert "## Caveats" in md


def test_render_readme_mentions_engine_target():
    md = integration_channel._render_readme(_decomp("lab-result-to-ehr-fhir"))
    assert "Engine target" in md or "engine target" in md.lower()
    assert "Mirth/OIE/BridgeLink" in md


def test_xml_safely_escapes_special_chars():
    """The XML renderer should not break when content has &, <, >."""
    fake_meta = dict(integration_channel.SCENARIO_CATALOG["ehr-adt-to-lab"])
    fake_meta["description"] = "EHR <-> Lab & friends"
    d = integration_channel._Decomposition(
        scenario_key="ehr-adt-to-lab",
        scenario_meta=fake_meta,
        citations=[],
    )
    xml_text = integration_channel._render_channel_xml(d)
    root = ET.fromstring(xml_text)
    desc = root.find("description")
    assert desc is not None and "EHR <-> Lab & friends" in desc.text


# ---- integration_channel_xml.py builder direct tests -------------------

def test_build_channel_rejects_unknown_source_connector_type():
    with pytest.raises(ValueError, match="unsupported source"):
        integration_channel_xml.build_channel(
            name="x",
            description="y",
            source=integration_channel_xml.SourceConfig(
                connector_type="MagicListener",
                message_format="HL7v2",
            ),
            destinations=[
                integration_channel_xml.DestConfig(
                    name="d",
                    connector_type="HTTP Sender",
                    message_format="FHIR JSON",
                )
            ],
        )


def test_build_channel_rejects_no_destinations():
    with pytest.raises(ValueError, match="at least one destination"):
        integration_channel_xml.build_channel(
            name="x",
            description="y",
            source=integration_channel_xml.SourceConfig(
                connector_type="LLP Listener",
                message_format="HL7v2",
            ),
            destinations=[],
        )


def test_build_channel_uses_explicit_channel_id():
    xml_text = integration_channel_xml.build_channel(
        name="x",
        description="y",
        source=integration_channel_xml.SourceConfig(
            connector_type="LLP Listener",
            message_format="HL7v2",
            port=6661,
        ),
        destinations=[
            integration_channel_xml.DestConfig(
                name="d",
                connector_type="HTTP Sender",
                message_format="FHIR JSON",
            )
        ],
        channel_id="my-explicit-id",
    )
    root = ET.fromstring(xml_text)
    assert root.find("id").text == "my-explicit-id"


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
        assert "transformOruR01ToFhir" in js
    finally:
        conn.close()
