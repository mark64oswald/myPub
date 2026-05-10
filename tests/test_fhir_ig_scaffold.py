"""Tests for the FHIR IG Scaffold generator."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import fhir_ig_scaffold  # noqa: E402
from healthcare_libs import fhir as hfhir  # noqa: E402


# ---- Catalog hygiene ----------------------------------------------------

def test_every_use_case_has_required_keys():
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for k in ("name", "description", "fhir_version", "base_ig",
                  "tools_cited", "resources", "value_sets", "extensions"):
            assert k in meta, f"{key} missing {k}"
        assert isinstance(meta["resources"], list)
        assert len(meta["resources"]) >= 5, f"{key} has too few resources"


def test_every_resource_entry_has_3_or_4_columns():
    """Catalog entries are now (resource_type, profile_name, purpose, profile_meta).
    The 4th column (ProfileMeta dict) is optional; older 3-tuples remain
    valid for back-compat."""
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for tup in meta["resources"]:
            assert len(tup) in (3, 4), (
                f"{key}: resource entry {tup} should be 3- or 4-tuple"
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
        for entry in meta["resources"]:
            resource_type = entry[0]
            assert resource_type in known_fhir_resources, (
                f"{key}: resource_type {resource_type!r} not in known FHIR resources"
            )


def test_every_catalog_resource_has_profile_meta():
    """The richer FSH renderer needs ProfileMeta to emit real constraints.
    Every catalog entry should now ship one (no plain 3-tuples)."""
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for entry in meta["resources"]:
            assert len(entry) == 4, (
                f"{key}: entry {entry[1]!r} missing ProfileMeta — "
                f"FSH would fall back to TODO stub"
            )
            pmeta = entry[3]
            assert isinstance(pmeta, dict)
            # At least one of must_support / cardinality / bindings should
            # be populated for the FSH to be non-trivial.
            assert pmeta.get("must_support") or pmeta.get("cardinality") or pmeta.get("bindings"), (
                f"{key}: profile {entry[1]!r} ProfileMeta is empty"
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
    for entry in meta["resources"]:
        profile_name = entry[1]
        assert profile_name in md, f"tumor-board index missing profile {profile_name}"


def test_render_index_page_includes_clinical_workflow():
    """Index pages now have a Clinical workflow section when the catalog
    entry supplies one."""
    md = fhir_ig_scaffold._render_index_page(_decomp("tumor-board"))
    assert "Clinical workflow" in md
    assert "tumor board" in md.lower()


def test_render_background_page_has_per_profile_narratives():
    md = fhir_ig_scaffold._render_background_page(_decomp("tumor-board"))
    # Each profile gets its own ### section
    meta = fhir_ig_scaffold.USE_CASE_CATALOG["tumor-board"]
    for entry in meta["resources"]:
        profile_name = entry[1]
        assert f"### {profile_name}" in md, (
            f"background page missing section for {profile_name}"
        )


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


def test_render_profile_fsh_emits_real_must_support_lines():
    """The richer renderer should emit `* field MS` lines from the catalog,
    not `// TODO` stubs."""
    pmeta = fhir_ig_scaffold._pm(
        must_support=["identifier", "name", "birthDate", "gender", "address"],
        cardinality={"identifier": "1..*", "name": "1..*"},
    )
    fsh = fhir_ig_scaffold._render_profile_fsh(
        "Patient", "OncologyPatient", "test", "us-core + mcode", pmeta,
    )
    # At least 5 * field MS lines
    ms_lines = re.findall(r"^\* \S+ MS$", fsh, re.MULTILINE)
    assert len(ms_lines) >= 5, f"expected >=5 MS lines, got {ms_lines}"
    # No TODO scaffolding
    assert "// TODO: pin must-support" not in fsh
    # Cardinality lines present
    card_lines = re.findall(r"^\* \S+ \d+\.\.[\d*]+$", fsh, re.MULTILINE)
    assert len(card_lines) >= 2, f"expected >=2 cardinality lines, got {card_lines}"


def test_every_catalog_profile_emits_5_or_more_ms_lines():
    """Every profile in the catalog should produce a meaningful FSH file —
    at least 5 must-support lines."""
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        base_ig = meta["base_ig"]
        for entry in meta["resources"]:
            rt, pn, purpose, pmeta = fhir_ig_scaffold._resource_tuple(entry)
            fsh = fhir_ig_scaffold._render_profile_fsh(rt, pn, purpose, base_ig, pmeta)
            ms_lines = re.findall(r"^\* \S+ MS$", fsh, re.MULTILINE)
            assert len(ms_lines) >= 5, (
                f"{key}/{pn} only has {len(ms_lines)} MS lines; expected >=5"
            )


def test_render_profile_fsh_emits_value_set_bindings():
    """Bindings in the catalog should produce `* field from VS (strength)` lines."""
    pmeta = fhir_ig_scaffold._pm(
        must_support=["code", "subject", "status", "category", "effectiveDateTime"],
        bindings={"code": ("http://example.org/fhir/ValueSet/test-vs", "extensible")},
    )
    fsh = fhir_ig_scaffold._render_profile_fsh(
        "Observation", "TestObs", "test", "us-core", pmeta,
    )
    assert "from http://example.org/fhir/ValueSet/test-vs (extensible)" in fsh


def test_render_example_json_is_valid_json():
    """Generated examples are pure JSON now (no trailing comments) and
    decode cleanly."""
    text = fhir_ig_scaffold._render_example_json(
        "Patient", "USCorePatient", "bulk-data-export",
    )
    parsed = json.loads(text)
    assert parsed["resourceType"] == "Patient"
    assert "meta" in parsed
    assert "profile" in parsed["meta"]


def test_render_example_json_is_populated():
    """Examples are no longer just resourceType+meta — they're realistic."""
    text = fhir_ig_scaffold._render_example_json(
        "Patient", "USCorePatient", "bulk-data-export",
    )
    parsed = json.loads(text)
    # More than just resourceType + id + meta
    assert len(parsed.keys()) > 5, f"example too sparse: keys={list(parsed.keys())}"
    # Has identifier + name (must-support fields)
    assert "name" in parsed
    assert "identifier" in parsed


def test_oncology_patient_example_has_race_and_ethnicity():
    """Tumor-board OncologyPatient should include US Core race + ethnicity
    extensions — they're must-support per mCODE alignment."""
    text = fhir_ig_scaffold._render_example_json(
        "Patient", "OncologyPatient", "tumor-board",
    )
    parsed = json.loads(text)
    assert "extension" in parsed
    urls = [e["url"] for e in parsed["extension"]]
    assert any("us-core-race" in u for u in urls)
    assert any("us-core-ethnicity" in u for u in urls)


def test_every_catalog_example_validates():
    """Every (use_case, profile) example must pass healthcare_libs.fhir.validate()
    with zero error-severity issues. Information-only issues are fine."""
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for entry in meta["resources"]:
            rt, pn, _purpose, _pmeta = fhir_ig_scaffold._resource_tuple(entry)
            text = fhir_ig_scaffold._render_example_json(rt, pn, key)
            parsed = json.loads(text)
            issues = hfhir.validate(parsed)
            errors = [
                i for i in issues
                if i.severity in ("error", "fatal")
            ]
            assert not errors, (
                f"{key}/{pn} example failed validation: "
                f"{[(i.severity, i.code, i.message, i.location) for i in errors]}"
            )


def test_every_catalog_example_is_populated():
    """Every example should have more than 5 fields (not just
    resourceType+id+meta+name=4)."""
    for key, meta in fhir_ig_scaffold.USE_CASE_CATALOG.items():
        for entry in meta["resources"]:
            rt, pn, _purpose, _pmeta = fhir_ig_scaffold._resource_tuple(entry)
            text = fhir_ig_scaffold._render_example_json(rt, pn, key)
            parsed = json.loads(text)
            assert len(parsed.keys()) >= 5, (
                f"{key}/{pn} example too sparse: keys={list(parsed.keys())}"
            )


def test_tumor_board_each_profile_validates():
    """Tumor-board is the canonical mCODE-aligned use case. Every profile's
    example should validate cleanly."""
    meta = fhir_ig_scaffold.USE_CASE_CATALOG["tumor-board"]
    for entry in meta["resources"]:
        rt, pn, _purpose, _pmeta = fhir_ig_scaffold._resource_tuple(entry)
        text = fhir_ig_scaffold._render_example_json(rt, pn, "tumor-board")
        parsed = json.loads(text)
        issues = hfhir.validate(parsed)
        errors = [i for i in issues if i.severity in ("error", "fatal")]
        assert not errors, f"tumor-board/{pn} validation: {errors}"


def test_prior_auth_each_profile_validates():
    meta = fhir_ig_scaffold.USE_CASE_CATALOG["prior-auth"]
    for entry in meta["resources"]:
        rt, pn, _purpose, _pmeta = fhir_ig_scaffold._resource_tuple(entry)
        text = fhir_ig_scaffold._render_example_json(rt, pn, "prior-auth")
        parsed = json.loads(text)
        issues = hfhir.validate(parsed)
        errors = [i for i in issues if i.severity in ("error", "fatal")]
        assert not errors, f"prior-auth/{pn} validation: {errors}"


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

    try:
        conn = duckdb.connect(str(catalog))
    except duckdb.IOException:
        pytest.skip("catalog locked by another process")
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
        for entry in meta["resources"]:
            profile_name = entry[1]
            slug = profile_name.lower().replace("_", "-")
            fsh_path = pkg_dir / f"input/profiles/{slug}.fsh"
            ex_path = pkg_dir / f"input/examples/{slug}-example.json"
            assert fsh_path.exists(), f"missing profile {fsh_path}"
            assert ex_path.exists(), f"missing example {ex_path}"

            # FSH is real — has MS lines, no TODO stubs
            fsh = fsh_path.read_text()
            ms_lines = re.findall(r"^\* \S+ MS$", fsh, re.MULTILINE)
            assert len(ms_lines) >= 5, (
                f"{profile_name} FSH only has {len(ms_lines)} MS lines"
            )
            assert "// TODO: pin must-support" not in fsh

            # Example is real JSON, populated, validates clean
            example = json.loads(ex_path.read_text())
            assert len(example.keys()) >= 5, (
                f"{profile_name} example too sparse"
            )
            errors = [
                i for i in hfhir.validate(example)
                if i.severity in ("error", "fatal")
            ]
            assert not errors, (
                f"{profile_name} example failed validation: {errors}"
            )
    finally:
        conn.close()
