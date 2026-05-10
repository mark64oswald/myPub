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
    # Helper present
    assert "def _hmac_pseudonym" in code
    assert "def _per_subject_offset" in code
    # Per-category functions
    fhir_categories = {el[0] for el in _decomp("fhir").elements}
    for cat in fhir_categories:
        assert f"def deid_{cat}(" in code, f"missing per-category function for {cat}"
    # Orchestrator
    assert "def deid_record" in code
    assert "def deid_dataset" in code


def test_render_test_uses_phi_regexes():
    code = deid_bundle._render_test(_decomp("clinical_trial"))
    assert "SSN_RE" in code
    assert "PHONE_RE" in code
    assert "EMAIL_RE" in code
    assert "FULL_DATE_RE" in code


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
