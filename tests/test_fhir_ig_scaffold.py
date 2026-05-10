"""Tests for the FHIR IG Scaffold generator."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import fhir_ig_scaffold  # noqa: E402


# ---- Catalog hygiene ----------------------------------------------------

def test_every_use_case_has_required_keys():
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for k in ("name", "description", "fhir_version", "base_ig",
                  "tools_cited", "resources", "value_sets", "extensions"):
            assert k in meta, f"{key} missing {k}"
        assert isinstance(meta["resources"], list)
        assert len(meta["resources"]) >= 5, f"{key} has too few resources"


def test_every_resource_entry_has_3_columns():
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for tup in meta["resources"]:
            assert len(tup) == 3, (
                f"{key}: resource entry {tup} should be (resource_type, profile_name, purpose)"
            )


def test_every_resource_type_is_a_known_fhir_resource():
    """Light sanity check: resource types should look like FHIR resources."""
    known_fhir_resources = {
        "Patient", "Practitioner", "Organization", "Encounter", "Observation",
        "Condition", "MedicationRequest", "MedicationStatement", "Immunization",
        "AllergyIntolerance", "DiagnosticReport", "DocumentReference",
        "Procedure", "Coverage", "Goal", "Composition", "Group",
        "ServiceRequest", "Claim", "ClaimResponse", "ResearchSubject",
        "ResearchStudy", "AdverseEvent", "ImagingStudy",
    }
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for resource_type, _, _ in meta["resources"]:
            assert resource_type in known_fhir_resources, (
                f"{key}: resource_type {resource_type!r} not in known FHIR resources"
            )


# ---- Decomposer ---------------------------------------------------------

class _MockConn:
    def execute(self, *args, **kwargs):
        class _R:
            def fetchall(self_inner):
                return []
        return _R()


def test_decomposer_resolves_use_case_keys():
    dec = fhir_ig_scaffold.FhirIgDecomposer()
    for key in fhir_ig_scaffold.USE_CASE_CATALOG:
        d = dec.decompose(_MockConn(), None, key)
        assert d.use_case_key == key, f"{key!r} → {d.use_case_key}"


def test_decomposer_resolves_loose_phrasing():
    dec = fhir_ig_scaffold.FhirIgDecomposer()
    samples = [
        ("Bulk Data Export for trials", "bulk-data-export"),
        ("IPS patient summary", "patient-summary"),
        ("oncology tumor board IG", "tumor-board"),
        ("DaVinci prior auth", "prior-auth"),
        ("clinical trial adverse event reporting", "adverse-event"),
    ]
    for query, expected in samples:
        d = dec.decompose(_MockConn(), None, query)
        assert d.use_case_key == expected, (
            f"{query!r} → {d.use_case_key}, expected {expected}"
        )


def test_decomposer_unknown_use_case():
    dec = fhir_ig_scaffold.FhirIgDecomposer()
    d = dec.decompose(_MockConn(), None, "blockchain immunization registry")
    assert d.use_case_key is None
    assert any("supported" in n.lower() for n in d.notes)


# ---- Renderers ----------------------------------------------------------

def _decomp(use_case="bulk-data-export"):
    return fhir_ig_scaffold.FhirIgDecomposer().decompose(_MockConn(), None, use_case)


def test_render_sushi_config_is_valid_yaml():
    for key in fhir_ig_scaffold.USE_CASE_CATALOG:
        d = _decomp(key)
        text = fhir_ig_scaffold._render_sushi_config(d)
        parsed = yaml.safe_load(text)
        assert "id" in parsed
        assert "fhirVersion" in parsed
        assert "name" in parsed


def test_render_index_page_lists_resources():
    md = fhir_ig_scaffold._render_index_page(_decomp("tumor-board"))
    # Should mention all the constrained profiles
    meta = fhir_ig_scaffold.USE_CASE_CATALOG["tumor-board"]
    for resource_type, profile_name, _ in meta["resources"]:
        assert profile_name in md, f"tumor-board index missing profile {profile_name}"


def test_render_profile_fsh_inherits_from_us_core_when_appropriate():
    """For US Core base IGs, the profile parent should be the US Core profile."""
    bulk = fhir_ig_scaffold.USE_CASE_CATALOG["bulk-data-export"]
    fsh = fhir_ig_scaffold._render_profile_fsh(
        "Patient", "USCorePatient", "test", bulk["base_ig"],
    )
    assert "Parent:" in fsh and "USCorePatient" in fsh


def test_render_profile_fsh_falls_back_to_base_resource():
    """For non-US-Core resources, the profile parent should be the base FHIR resource."""
    fsh = fhir_ig_scaffold._render_profile_fsh(
        "ResearchStudy", "ClinicalTrial", "test", "us-core",
    )
    assert "Parent:         ResearchStudy" in fsh


def test_render_example_json_is_valid_json():
    """The example file ends with a // comment, so we need to peel that off
    before json-parsing, but the leading object should be valid."""
    text = fhir_ig_scaffold._render_example_json("Patient", "USCorePatient", "us-core")
    json_part = text.split("\n//")[0]
    parsed = json.loads(json_part)
    assert parsed["resourceType"] == "Patient"
    assert "meta" in parsed
    assert "profile" in parsed["meta"]


def test_camel_helper():
    assert fhir_ig_scaffold._camel("Bulk Data Export") == "BulkDataExport"
    assert fhir_ig_scaffold._camel("tumor-board") == "TumorBoard"


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
        gen = fhir_ig_scaffold.make_fhir_ig_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "tumor-board",
            output_root=str(tmp_path), overwrite=True,
        )
        errors = [i for i in issues if i.severity == "error"]
        assert package_id > 0 and not errors, f"persistence failed: {issues}"

        pkg_dir = tmp_path / "fhir-ig-tumor-board"
        for f in ("README.md", "sushi-config.yaml", "ig.ini",
                  "input/pagecontent/index.md",
                  "input/pagecontent/background.md"):
            assert (pkg_dir / f).exists(), f"missing {f}"

        # sushi-config.yaml is valid YAML
        sushi = yaml.safe_load((pkg_dir / "sushi-config.yaml").read_text())
        assert sushi["fhirVersion"] == "4.0.1"

        # All declared profiles got an FSH file + an example
        meta = fhir_ig_scaffold.USE_CASE_CATALOG["tumor-board"]
        for resource_type, profile_name, _ in meta["resources"]:
            slug = profile_name.lower().replace("_", "-")
            fsh_path = pkg_dir / f"input/profiles/{slug}.fsh"
            ex_path = pkg_dir / f"input/examples/{slug}-example.json"
            assert fsh_path.exists(), f"missing profile {fsh_path}"
            assert ex_path.exists(), f"missing example {ex_path}"
    finally:
        conn.close()
