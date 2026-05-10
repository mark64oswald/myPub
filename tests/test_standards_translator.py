"""Tests for the Standards Translator generator."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import standards_translator  # noqa: E402


# ---- Catalog hygiene ----------------------------------------------------

def test_every_catalog_entry_has_required_keys():
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        for k in ("source", "target", "purpose", "tools_cited", "fields"):
            assert k in meta, f"{key} missing {k}"
        assert isinstance(meta["fields"], list)
        assert len(meta["fields"]) >= 5, f"{key} has too few field mappings"


def test_every_field_has_4_columns():
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        for field_tuple in meta["fields"]:
            assert len(field_tuple) == 4, (
                f"{key}: field tuple {field_tuple} should be (source, target, transform, notes)"
            )


def test_every_transform_is_in_known_set():
    valid_transforms = {
        "direct", "lookup", "split", "concat", "code-translation",
        "compute", "lossy", "drop",
    }
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        for src, tgt, transform, notes in meta["fields"]:
            assert transform in valid_transforms, (
                f"{key}: unknown transform {transform!r} (valid: {valid_transforms})"
            )


# ---- Decomposer ---------------------------------------------------------

class _MockConn:
    def execute(self, *args, **kwargs):
        class _R:
            def fetchall(self_inner):
                return []
        return _R()


def test_normalize_query_handles_arrows():
    norm = standards_translator._normalize_query("HL7v2 ADT^A01 → FHIR Patient")
    assert "to" in norm  # arrow normalized to ' to '
    assert "adt-a01" in norm  # ^ converted to -


def test_decomposer_resolves_loose_phrasing():
    dec = standards_translator.StandardsTranslatorDecomposer()
    samples = [
        ("HL7v2 ADT^A01 to FHIR Patient+Encounter",  "hl7v2-adt-a01-to-fhir-patient-encounter"),
        ("convert ORU into FHIR Observation",         "hl7v2-oru-r01-to-fhir-observation"),
        ("X12 837P → FHIR Claim",                    "x12-837p-to-fhir-claim"),
        ("835 to claimresponse",                     "x12-835-to-fhir-claimresponse"),
        ("DICOM Series → FHIR ImagingStudy",         "dicom-series-to-fhir-imagingstudy"),
    ]
    for query, expected_key in samples:
        d = dec.decompose(_MockConn(), None, query)
        assert d.mapping_key == expected_key, (
            f"{query!r} → {d.mapping_key}, expected {expected_key}"
        )


def test_decomposer_unknown_pair_emits_helpful_notes():
    dec = standards_translator.StandardsTranslatorDecomposer()
    d = dec.decompose(_MockConn(), None, "blockchain to quantum")
    assert d.mapping_key is None
    # Helpful notes list supported pairs
    assert any("supported" in n.lower() for n in d.notes)
    assert any("hl7v2-adt-a01" in n for n in d.notes)


# ---- Renderers ----------------------------------------------------------

def _decomp(query="HL7v2 ADT^A01 to FHIR Patient+Encounter"):
    return standards_translator.StandardsTranslatorDecomposer().decompose(
        _MockConn(), None, query,
    )


def test_render_mapping_table_columns():
    md = standards_translator._render_mapping(_decomp())
    assert "| Source | Target | Transform | Notes |" in md
    assert "## Transform legend" in md
    # Every transform value in the legend
    for t in ("direct", "lookup", "split", "concat", "code-translation",
              "compute", "lossy", "drop"):
        assert f"**{t}**" in md, f"transform {t} missing from legend"


def test_render_transformer_emits_per_field_function():
    code = standards_translator._render_transformer(_decomp())
    # Has per-field function names
    assert "def transform_pid_3" in code
    assert "def transform_pv1_2" in code
    # Has top-level orchestrator
    assert "def transform_message" in code


def test_render_transformer_is_valid_python():
    for key in standards_translator.MAPPING_CATALOG:
        d = standards_translator.StandardsTranslatorDecomposer().decompose(
            _MockConn(), None, key.replace("-", " "),
        )
        if d.mapping_key is None:
            continue
        code = standards_translator._render_transformer(d)
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"{key}: transformer doesn't parse: {e}")


def test_render_test_emits_pytest_functions():
    code = standards_translator._render_test(_decomp())
    assert "def test_transform_round_trip" in code
    assert "def test_target_spec_conformance" in code
    assert "def test_lossy_fields_documented" in code


def test_render_test_is_valid_python():
    for key in standards_translator.MAPPING_CATALOG:
        d = standards_translator.StandardsTranslatorDecomposer().decompose(
            _MockConn(), None, key.replace("-", " "),
        )
        if d.mapping_key is None:
            continue
        code = standards_translator._render_test(d)
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"{key}: test scaffold doesn't parse: {e}")


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
        gen = standards_translator.make_standards_translator_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "X12 837P to FHIR Claim",
            output_root=str(tmp_path), overwrite=True,
        )
        errors = [i for i in issues if i.severity == "error"]
        assert package_id > 0 and not errors, f"persistence failed: {issues}"

        pkg_dir = tmp_path / "mapping-x12-837p-to-fhir-claim"
        for f in ("README.md", "mapping.md", "transformer.py",
                  "tests/test_mapping.py"):
            assert (pkg_dir / f).exists(), f"missing {f}"

        # Generated transformer parses
        ast.parse((pkg_dir / "transformer.py").read_text())

        # Mapping table includes the source field paths
        mapping = (pkg_dir / "mapping.md").read_text()
        assert "BHT" in mapping
        assert "FHIR" in mapping
    finally:
        conn.close()
