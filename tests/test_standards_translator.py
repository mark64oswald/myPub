"""Tests for the Standards Translator generator."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import standards_translator  # noqa: E402


# ---- Catalog hygiene ----------------------------------------------------

def test_every_catalog_entry_has_required_keys():
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        for k in ("source", "target", "purpose", "tools_cited", "fields",
                  "transformer_func", "test_builder",
                  "expected_resource_types"):
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


def test_every_transformer_func_resolves_in_cross_standards():
    """Each catalog entry's transformer_func must exist in healthcare_libs.cross_standards."""
    sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
    from healthcare_libs import cross_standards
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        func_name = meta["transformer_func"]
        assert hasattr(cross_standards, func_name), (
            f"{key}: cross_standards has no function named {func_name!r}"
        )
        assert callable(getattr(cross_standards, func_name)), (
            f"{key}: cross_standards.{func_name} is not callable"
        )


def test_every_test_builder_resolves_in_healthcare_libs():
    """Each catalog entry's test_builder must resolve to a callable."""
    sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
    import healthcare_libs
    for key, meta in standards_translator.MAPPING_CATALOG.items():
        builder = meta["test_builder"]
        module_name, fn_name = builder.split(".", 1)
        module = getattr(healthcare_libs, module_name)
        assert hasattr(module, fn_name), (
            f"{key}: healthcare_libs.{module_name} has no {fn_name!r}"
        )
        assert callable(getattr(module, fn_name)), (
            f"{key}: healthcare_libs.{builder} is not callable"
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


def test_decomposer_carries_transformer_func_through():
    dec = standards_translator.StandardsTranslatorDecomposer()
    d = dec.decompose(_MockConn(), None, "HL7v2 ADT^A01 to FHIR Patient")
    assert d.transformer_func == "adt_a01_to_patient_encounter"
    assert d.test_builder == "hl7v2.build_adt_a01"
    assert "Bundle" in d.expected_resource_types


# ---- Renderers ----------------------------------------------------------

def _decomp(query="HL7v2 ADT^A01 to FHIR Patient+Encounter"):
    return standards_translator.StandardsTranslatorDecomposer().decompose(
        _MockConn(), None, query,
    )


def test_render_mapping_table_columns():
    md = standards_translator._render_mapping(_decomp())
    assert "| Source | Target | Transform | Notes |" in md
    assert "## Transform legend" in md
    # Mapping doc surfaces the implementation function
    assert "cross_standards.adt_a01_to_patient_encounter" in md
    # Every transform value in the legend
    for t in ("direct", "lookup", "split", "concat", "code-translation",
              "compute", "lossy", "drop"):
        assert f"**{t}**" in md, f"transform {t} missing from legend"


def test_render_transformer_imports_cross_standards():
    """Generated transformer wraps the cross_standards function (no NotImplementedError)."""
    code = standards_translator._render_transformer(_decomp())
    assert "from healthcare_libs.cross_standards import adt_a01_to_patient_encounter" in code
    assert "def transform(" in code
    assert "def main(" in code
    # Must NOT contain the old TODO scaffolding
    assert "raise NotImplementedError" not in code
    assert "TODO: implement" not in code


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
    assert "def test_transform_returns_expected_resource_type" in code
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


def test_render_readme_mentions_cross_standards():
    readme = standards_translator._render_readme(_decomp())
    assert "healthcare_libs.cross_standards" in readme
    assert "adt_a01_to_patient_encounter" in readme
    assert "## Install" in readme
    assert "## How to run" in readme


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


def _generate_package(tmp_path: Path, query: str) -> Path:
    """Helper: generate one mapping package against the live catalog."""
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
        gen.run_deterministic(
            conn, resolver, query,
            output_root=str(tmp_path), overwrite=True,
        )
    finally:
        conn.close()
    pkgs = list(tmp_path.iterdir())
    assert len(pkgs) == 1, f"expected 1 generated package, got {pkgs}"
    return pkgs[0]


def test_generated_transformer_imports_and_runs_for_adt(tmp_path):
    """Generated transformer.py for ADT^A01 imports cleanly + transforms a real message."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ADT^A01 to FHIR Patient+Encounter")
    transformer_py = pkg_dir / "transformer.py"
    assert transformer_py.exists()

    # Must reference the real cross_standards function and have no TODOs
    code = transformer_py.read_text()
    assert "from healthcare_libs.cross_standards import adt_a01_to_patient_encounter" in code
    assert "raise NotImplementedError" not in code

    # Build a synthetic source message + drive transformer.py via subprocess
    from healthcare_libs import hl7v2
    source_msg = hl7v2.build_adt_a01()
    src_path = tmp_path / "source.hl7"
    out_path = tmp_path / "out.json"
    src_path.write_text(source_msg)

    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, str(transformer_py),
         "--input", str(src_path), "--output", str(out_path)],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"transformer.py failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert out_path.exists()
    bundle = json.loads(out_path.read_text())
    assert bundle["resourceType"] == "Bundle"
    entry_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Patient" in entry_types
    assert "Encounter" in entry_types


def test_generated_test_passes_end_to_end_for_adt(tmp_path):
    """Generated tests/test_mapping.py PASSES against a real synthetic message."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ADT^A01 to FHIR Patient+Encounter")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    assert test_py.exists()

    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Smoke: should report PASSED for the four key tests we generate
    assert "test_transform_returns_expected_resource_type" in result.stdout
    assert "test_target_spec_conformance" in result.stdout


def test_generated_test_passes_end_to_end_for_oru(tmp_path):
    """Generated tests pass for ORU^R01 → Observation Bundle."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ORU^R01 to FHIR Observation")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"ORU generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_837p(tmp_path):
    """Generated tests pass for X12 837P → Claim."""
    pkg_dir = _generate_package(tmp_path, "X12 837P to FHIR Claim")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"837P generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_835(tmp_path):
    """Generated tests pass for X12 835 → ClaimResponse."""
    pkg_dir = _generate_package(tmp_path, "X12 835 to FHIR ClaimResponse")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"835 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_dicom(tmp_path):
    """Generated tests pass for DICOM Series → ImagingStudy."""
    pkg_dir = _generate_package(tmp_path, "DICOM Series to FHIR ImagingStudy")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"DICOM generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_adt_a03(tmp_path):
    """Generated tests pass for ADT^A03 discharge."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ADT^A03 to FHIR Encounter discharge")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"ADT^A03 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_adt_a08(tmp_path):
    """Generated tests pass for ADT^A08 update."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ADT^A08 to FHIR Patient Encounter update")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"ADT^A08 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_adt_a04(tmp_path):
    """Generated tests pass for ADT^A04 outpatient registration."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ADT^A04 to FHIR Patient Encounter register")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"ADT^A04 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_orm_o01(tmp_path):
    """Generated tests pass for ORM^O01 → ServiceRequest."""
    pkg_dir = _generate_package(tmp_path, "HL7v2 ORM^O01 to FHIR ServiceRequest")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"ORM^O01 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generated_test_passes_end_to_end_for_x12_271(tmp_path):
    """Generated tests pass for X12 271 → CoverageEligibilityResponse."""
    pkg_dir = _generate_package(tmp_path, "X12 271 to FHIR CoverageEligibilityResponse")
    test_py = pkg_dir / "tests" / "test_mapping.py"
    env_pythonpath = str(ROOT / "mcp-servers" / "kb-mcp")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_py), "-v"],
        env={"PYTHONPATH": env_pythonpath, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
        cwd=str(pkg_dir),
    )
    assert result.returncode == 0, (
        f"X12 271 generated tests failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
