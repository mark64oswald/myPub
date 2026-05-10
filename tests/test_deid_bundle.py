"""Tests for the De-identification Procedure Bundle generator."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import deid_bundle  # noqa: E402


# ---- Pure-function tests ------------------------------------------------

def test_safe_harbor_18_categories():
    """The 18 HIPAA Safe Harbor identifiers (45 CFR §164.514(b)(2)) plus
    the §164.514(b)(2)(ii) actual-knowledge guard make 18 entries."""
    # 17 from §164.514(b)(2)(i)(A-Q) + the rare-traits implicit category
    assert len(deid_bundle.SAFE_HARBOR_IDENTIFIERS) == 18


def test_dataset_types_have_required_keys():
    """Every dataset type carries name, description, tools_cited, phi_elements."""
    for ds, meta in deid_bundle.DATASET_TYPES.items():
        for k in ("name", "description", "tools_cited", "phi_elements"):
            assert k in meta, f"{ds} missing {k}"
        assert isinstance(meta["phi_elements"], list)
        assert len(meta["phi_elements"]) >= 5, f"{ds} has too few PHI elements"


def test_every_phi_element_uses_known_technique():
    """Every (category, location, technique, rationale) tuple references
    a technique in TECHNIQUE_LIBRARY (no typos in the per-dataset spec)."""
    known = set(deid_bundle.TECHNIQUE_LIBRARY)
    for ds, meta in deid_bundle.DATASET_TYPES.items():
        for category, location, technique, rationale in meta["phi_elements"]:
            assert technique in known, (
                f"{ds}: {category} references unknown technique {technique!r}"
            )


def test_every_technique_has_description_and_impl_hint():
    for name, tech in deid_bundle.TECHNIQUE_LIBRARY.items():
        assert tech.get("description"), f"{name} missing description"
        assert tech.get("implementation"), f"{name} missing implementation hint"


def test_every_phi_category_has_safe_harbor_entry_or_is_documented():
    """Categories used in dataset specs should map to a Safe Harbor entry,
    or be a documented extension category like 'rare_traits'."""
    sh_codes = {code for code, _ in deid_bundle.SAFE_HARBOR_IDENTIFIERS}
    for ds, meta in deid_bundle.DATASET_TYPES.items():
        for category, *_ in meta["phi_elements"]:
            assert category in sh_codes, (
                f"{ds}: category {category!r} not in Safe Harbor list"
            )


# ---- Decomposer tests ---------------------------------------------------

class _MockConn:
    def execute(self, *args, **kwargs):
        class _R:
            def fetchall(self_inner):
                return []
        return _R()


def test_decomposer_recognizes_dataset_aliases():
    dec = deid_bundle.DeidDecomposer()
    for query, expected in [
        ("fhir", "fhir"),
        ("FHIR Patient + Observation", "fhir"),
        ("DICOM imaging study", "dicom"),
        ("hl7v2", "hl7v2"),
        ("HL7 v2 ADT messages", "hl7v2"),
        ("clinical_trial", "clinical_trial"),
        ("REDCap clinical trial export", "clinical_trial"),
    ]:
        d = dec.decompose(_MockConn(), None, query)
        assert d.dataset_type == expected, f"{query!r} → {d.dataset_type}, expected {expected}"


def test_decomposer_unknown_dataset():
    dec = deid_bundle.DeidDecomposer()
    d = dec.decompose(_MockConn(), None, "blockchain_health_data")
    assert d.dataset_meta == {}
    assert any("unrecognized" in n for n in d.notes)


def test_decomposer_populates_phi_elements():
    dec = deid_bundle.DeidDecomposer()
    d = dec.decompose(_MockConn(), None, "fhir")
    assert len(d.elements) == len(deid_bundle.DATASET_TYPES["fhir"]["phi_elements"])
    # Every element has 5 fields
    for el in d.elements:
        assert len(el) == 5


# ---- Renderer tests -----------------------------------------------------

def _decomp(query="fhir"):
    return deid_bundle.DeidDecomposer().decompose(_MockConn(), None, query)


def test_render_readme_lists_safe_harbor():
    md = deid_bundle._render_readme(_decomp("fhir"))
    assert "Safe Harbor" in md
    assert "164.514" in md


def test_render_rationale_groups_by_category():
    md = deid_bundle._render_rationale(_decomp("dicom"))
    # Should contain category headers + Safe Harbor descriptions
    assert "names" in md.lower()
    assert "Names of the individual" in md  # Safe Harbor description


def test_render_audit_includes_safe_harbor_checklist():
    md = deid_bundle._render_audit(_decomp("hl7v2"))
    assert "Safe Harbor checklist" in md
    # Every Safe Harbor identifier appears
    for code, _ in deid_bundle.SAFE_HARBOR_IDENTIFIERS:
        assert code in md, f"checklist missing {code}"


def test_render_pipeline_emits_per_category_function():
    code = deid_bundle._render_pipeline(_decomp("fhir"))
    # Imports the production primitives instead of defining stubs locally
    assert "from healthcare_libs.deid import" in code
    assert "hmac_pseudonym" in code
    assert "DeidConfig" in code
    # Per-category functions for every FHIR category in the spec
    fhir_categories = {el[0] for el in _decomp("fhir").elements}
    for cat in fhir_categories:
        assert f"def deid_{cat}(" in code, f"missing per-category function for {cat}"
    # Orchestrator
    assert "def deid_record" in code
    assert "def deid_dataset" in code
    # NO ``pass``-only stubs — every category function does real work.
    # (We grep for "    pass\n" preceded by the function header pattern.)
    import re
    stub_pattern = re.compile(
        r"def deid_\w+\([^)]*\)[^:]*:\s*(?:\"\"\"[^\"]*\"\"\"\s*)?pass\s*\n",
        re.DOTALL,
    )
    assert not stub_pattern.search(code), "found pass-only stub function in pipeline"


def test_render_test_uses_phi_regexes():
    """Generated test must rely on deid.find_phi_patterns (the centralized
    PHI heuristic) rather than reinvent SSN/PHONE/EMAIL regexes per output."""
    code = deid_bundle._render_test(_decomp("clinical_trial"))
    assert "find_phi_patterns" in code
    assert "from healthcare_libs.deid import" in code


def test_render_pipeline_imports_format_lib_per_dataset():
    """Each shape's pipeline imports the matching format-specific module."""
    expected = {
        "fhir": "from healthcare_libs.deid import",
        "dicom": "from healthcare_libs import dicom",
        "hl7v2": "from healthcare_libs import hl7v2",
        "clinical_trial": "from healthcare_libs.deid import",
    }
    for ds, needle in expected.items():
        code = deid_bundle._render_pipeline(_decomp(ds))
        assert needle in code, f"{ds} pipeline missing import: {needle!r}"


def test_render_dicom_pipeline_uses_basic_profile():
    """DICOM should delegate to dicom.deidentify_basic_profile (PS3.15 Annex E.1)."""
    code = deid_bundle._render_pipeline(_decomp("dicom"))
    assert "dicom.deidentify_basic_profile" in code


def test_render_clinical_trial_emits_subject_id_enforcement():
    """Clinical-trial pipeline runs an ingest-time subject-id-only guardrail."""
    code = deid_bundle._render_pipeline(_decomp("clinical_trial"))
    assert "enforce_subject_id_only" in code
    assert "STRIPPED_AT_INGEST" in code


def test_pipeline_scaffold_is_syntactically_valid_python():
    """The generated pipeline must at least parse as Python."""
    import ast
    for ds in deid_bundle.DATASET_TYPES:
        code = deid_bundle._render_pipeline(_decomp(ds))
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"{ds}: pipeline scaffold doesn't parse: {e}")


def test_test_scaffold_is_syntactically_valid_python():
    import ast
    for ds in deid_bundle.DATASET_TYPES:
        code = deid_bundle._render_test(_decomp(ds))
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"{ds}: test scaffold doesn't parse: {e}")


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
        gen = deid_bundle.make_deid_bundle_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "fhir",
            output_root=str(tmp_path), overwrite=True,
        )
        errors = [i for i in issues if i.severity == "error"]
        assert package_id > 0 and not errors, (
            f"persistence failed: {issues}"
        )

        pkg_dir = tmp_path / "deid-bundle-fhir"
        for f in ("README.md", "rationale.md", "audit_trail.md",
                  "deid_pipeline.py", "tests/test_deid.py"):
            assert (pkg_dir / f).exists(), f"missing {f}"

        # Pipeline scaffold parses + has the orchestrator
        pipeline_code = (pkg_dir / "deid_pipeline.py").read_text()
        import ast
        ast.parse(pipeline_code)  # raises if invalid
        assert "def deid_record" in pipeline_code
    finally:
        conn.close()


# ---- Generated-pipeline runtime verification ------------------------------
#
# These tests prove the GENERATED output actually runs, not just that it
# parses. We import the generated ``deid_pipeline`` module from a tmpdir
# and run it against a synthetic record, then assert PHI patterns from
# ``healthcare_libs.deid.find_phi_patterns`` come up empty.

def _generate_to_tmpdir(tmp_path, dataset_type: str) -> Path:
    """Run the generator and return the package directory."""
    catalog = ROOT / "data" / "catalog.ddb"
    if not catalog.exists():
        pytest.skip("catalog not present")
    import duckdb
    sys.path.insert(0, str(ROOT / "scripts"))
    from resolution import EntityResolver  # noqa: E402
    conn = duckdb.connect(str(catalog))
    try:
        gen = deid_bundle.make_deid_bundle_generator()
        package_id, _, issues = gen.run_deterministic(
            conn, EntityResolver(conn), dataset_type,
            output_root=str(tmp_path), overwrite=True,
        )
        errors = [i for i in issues if i.severity == "error"]
        assert package_id > 0 and not errors, f"persistence failed: {issues}"
    finally:
        conn.close()
    return tmp_path / f"deid-bundle-{dataset_type}"


def _import_generated_pipeline(pkg_dir: Path):
    """Import the generated deid_pipeline.py as a module.

    Sets PYTHONPATH so ``from healthcare_libs.deid import …`` works, then
    loads the module from disk. Cleans up sys.modules on each call so
    successive tests don't see stale module state.
    """
    import importlib.util

    sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
    sys.path.insert(0, str(pkg_dir))
    sys.modules.pop("deid_pipeline", None)
    spec = importlib.util.spec_from_file_location(
        "deid_pipeline", pkg_dir / "deid_pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_fhir_pipeline_imports_cleanly(tmp_path):
    """The generated FHIR pipeline must import without raising."""
    pkg_dir = _generate_to_tmpdir(tmp_path, "fhir")
    mod = _import_generated_pipeline(pkg_dir)
    assert callable(getattr(mod, "deid_record", None))
    assert hasattr(mod, "DEFAULT_CONFIG")


def test_generated_fhir_pipeline_has_no_pass_stubs(tmp_path):
    """No per-category function should be a one-line ``pass`` stub."""
    pkg_dir = _generate_to_tmpdir(tmp_path, "fhir")
    code = (pkg_dir / "deid_pipeline.py").read_text()
    # Each category function must do real work — i.e., have body lines
    # other than ``pass`` or just a docstring. We check by looking for
    # the function header followed (after any docstring) immediately by
    # ``pass``.
    pattern = re.compile(
        r"def deid_\w+\([^)]*\)[^:]*:\s*"
        r"(?:\"\"\"[^\"]*\"\"\"\s*)?"
        r"pass\s*\n",
        re.DOTALL,
    )
    matches = pattern.findall(code)
    assert not matches, f"found stub functions in generated FHIR pipeline: {matches}"


def test_generated_fhir_pipeline_runs_against_synthetic_patient(tmp_path):
    """End-to-end: build a Patient, run deid_record, assert no PHI survives."""
    pkg_dir = _generate_to_tmpdir(tmp_path, "fhir")
    mod = _import_generated_pipeline(pkg_dir)

    sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
    from healthcare_libs.deid import find_phi_patterns  # noqa: E402

    synthetic = {
        "resourceType": "Patient",
        "id": "patient-12345-real-mrn",
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "birthDate": "1985-06-15",
        "telecom": [
            {"system": "phone", "value": "555-123-4567"},
            {"system": "email", "value": "jane@example.com"},
        ],
        "address": [{
            "line": ["123 Main St"], "city": "Springfield",
            "state": "IL", "postalCode": "62704",
        }],
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]},
             "system": "urn:oid:LOCAL_MRN_OID", "value": "MRN-987654321"},
            {"type": {"coding": [{"code": "SS"}]},
             "system": "http://hl7.org/fhir/sid/us-ssn", "value": "123-45-6789"},
        ],
    }
    out = mod.deid_record(synthetic, config=mod.DEFAULT_CONFIG)
    import json
    findings = find_phi_patterns(json.dumps(out))
    assert findings == [], f"PHI survived de-id: {findings}"
    # Year-only generalization
    assert out["birthDate"] == "1985"
    # Names suppressed
    assert out["name"] == []


def test_generated_fhir_test_suite_passes_against_synthetic_record(tmp_path):
    """Run ``pytest`` against the GENERATED test/test_deid.py — proves the
    full generated package is internally consistent and the synthetic
    fixture in the generated test exercises the generated pipeline."""
    pkg_dir = _generate_to_tmpdir(tmp_path, "fhir")
    import subprocess
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(ROOT / "mcp-servers" / "kb-mcp"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_deid.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(pkg_dir), env=env, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"generated FHIR test suite failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
