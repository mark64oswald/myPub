"""Tests for the EDI Round-Trip Test generator."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import edi_roundtrip  # noqa: E402


# ---- Pure-function tests (no DB needed) ---------------------------------

def test_transaction_sets_metadata_complete():
    """Every supported transaction set has the required spec metadata."""
    for code, meta in edi_roundtrip.TRANSACTION_SETS.items():
        assert "name" in meta, f"{code} missing 'name'"
        assert "purpose" in meta, f"{code} missing 'purpose'"
        assert "key_segments" in meta, f"{code} missing 'key_segments'"
        assert isinstance(meta["key_segments"], list), f"{code} key_segments not a list"
        # paired_response may be None; that's fine
        assert "paired_response" in meta


def test_pair_metadata_consistency():
    """If A.paired_response = B, then B must also be a registered transaction."""
    for code, meta in edi_roundtrip.TRANSACTION_SETS.items():
        paired = meta.get("paired_response")
        if paired is not None:
            assert paired in edi_roundtrip.TRANSACTION_SETS, (
                f"{code} pairs with {paired} but {paired} is not registered"
            )


@pytest.mark.parametrize("txn", ["270", "271", "834", "835", "837", "997", "999"])
def test_build_message_has_envelope(txn):
    """Every built message has ISA and IEA envelope."""
    msg = edi_roundtrip.build_message(txn, ctrl_num=42)
    assert "ISA*" in msg, f"{txn}: missing ISA"
    assert "IEA*" in msg, f"{txn}: missing IEA"
    assert f"ST*{txn}*" in msg, f"{txn}: missing ST with txn code"
    assert "SE*" in msg, f"{txn}: missing SE"


@pytest.mark.parametrize("txn", ["270", "271", "834", "835", "837"])
def test_build_message_has_key_segments(txn):
    """Every built message contains its declared key segments."""
    msg = edi_roundtrip.build_message(txn)
    meta = edi_roundtrip.TRANSACTION_SETS[txn]
    # Sample a couple of key segments — should appear in the body
    for seg in meta["key_segments"][:3]:
        assert f"{seg}*" in msg, f"{txn}: missing key segment {seg}"


def test_build_message_control_number_propagates():
    """ctrl_num appears in ISA, IEA, GS, GE positions."""
    msg = edi_roundtrip.build_message("270", ctrl_num=12345)
    assert "000012345" in msg, "ISA/IEA control number not propagated"
    assert "*12345*" in msg, "GS/GE control number not propagated"


# ---- Decomposer tests (no DB writes) ------------------------------------

def test_decomposer_unknown_txn():
    """Unknown transaction sets surface a clear error note."""
    dec = edi_roundtrip.EdiRoundTripDecomposer()
    # Pass conn/resolver as None — unknown txn short-circuits before queries
    d = dec.decompose(None, None, "999999")
    assert d.txn_meta == {}
    assert any("unknown" in n for n in d.notes)


def test_decomposer_strips_subtype_suffix():
    """837P, 837I, 837D should all resolve to 837."""
    dec = edi_roundtrip.EdiRoundTripDecomposer()
    # Mock conn that returns no rows
    class _MockConn:
        def execute(self, *args, **kwargs):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()
    d = dec.decompose(_MockConn(), None, "837P")
    assert d.txn_code == "837"


# ---- Renderer tests -----------------------------------------------------

def test_render_readme_contains_purpose():
    dec = edi_roundtrip.EdiRoundTripDecomposer()
    class _MockConn:
        def execute(self, *args, **kwargs):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()
    d = dec.decompose(_MockConn(), None, "270")
    md = edi_roundtrip._render_readme(d)
    assert "270" in md
    assert "Eligibility" in md
    assert "271" in md  # paired response is mentioned


def test_render_test_emits_pytest_functions():
    dec = edi_roundtrip.EdiRoundTripDecomposer()
    class _MockConn:
        def execute(self, *args, **kwargs):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()
    d = dec.decompose(_MockConn(), None, "270")
    test_code = edi_roundtrip._render_test(d)
    # Generated tests cover parse, round-trip, and validate via the lib
    assert "def test_270_request_parses_cleanly" in test_code
    assert "def test_270_request_round_trips" in test_code
    assert "def test_270_request_validates_clean" in test_code
    # Paired response exists for 270 → 271, so paired-ICN test should appear
    assert "def test_271_response_parses_cleanly" in test_code
    assert "def test_paired_icn_matches" in test_code


# ---- Output-uses-healthcare_libs tests ----------------------------------
#
# The post-rewrite contract: generated package files are thin consumers
# of healthcare_libs.x12. These tests guard that contract — without them
# a regression to inline X12 templates would slip through structurally.

def _decompose_270():
    """Helper: build a 270 decomposition with a no-rows mock conn."""
    dec = edi_roundtrip.EdiRoundTripDecomposer()
    class _MockConn:
        def execute(self, *args, **kwargs):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()
    return dec.decompose(_MockConn(), None, "270")


def test_generated_test_imports_healthcare_libs():
    """The generated test file must import from healthcare_libs.x12."""
    test_code = edi_roundtrip._render_test(_decompose_270())
    assert "from healthcare_libs import x12" in test_code, (
        "generated test must import healthcare_libs.x12"
    )
    # And it must actually USE the lib (not just import-and-ignore)
    assert "x12.parse_envelope" in test_code
    assert "x12.round_trip" in test_code
    assert "x12.validate" in test_code


def test_generated_test_is_valid_python():
    """The generated test file must parse cleanly with ast.parse."""
    import ast
    test_code = edi_roundtrip._render_test(_decompose_270())
    ast.parse(test_code)  # raises SyntaxError on bad code


def test_generated_transformer_imports_healthcare_libs():
    """The generated transformer.py must import healthcare_libs.x12."""
    code = edi_roundtrip._render_transformer(_decompose_270())
    assert "from healthcare_libs import x12" in code
    # CLI subcommands must be wired in
    assert "build" in code and "validate" in code and "extract" in code
    # The lib's segment-extraction helper must be used
    assert "x12.get_segments" in code


def test_generated_transformer_is_valid_python():
    """The generated transformer.py must parse cleanly with ast.parse."""
    import ast
    code = edi_roundtrip._render_transformer(_decompose_270())
    ast.parse(code)


def test_generated_fixture_parses_via_healthcare_libs():
    """A fixture from build_message must parse cleanly via healthcare_libs.x12."""
    sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
    from healthcare_libs import x12 as _x12
    for txn in ("270", "271", "834", "835", "837", "997", "999"):
        msg = edi_roundtrip.build_message(txn, ctrl_num=7)
        env, body = _x12.parse_envelope(msg)
        assert env.txn_set == txn, f"{txn}: ST-01 mismatch ({env.txn_set!r})"
        assert env.icn == 7, f"{txn}: ICN didn't propagate ({env.icn!r})"
        assert body, f"{txn}: empty body"
        # round_trip must be true for every fixture we ship
        assert _x12.round_trip(msg), f"{txn}: round_trip failed"


def test_generator_no_inline_x12_templates():
    """Regression guard: the generator file must not carry inline X12
    envelope templates anymore. Anything ISA/IEA-shaped should now come
    from healthcare_libs.x12."""
    src = (ROOT / "mcp-servers" / "kb-mcp" / "edi_roundtrip.py").read_text()
    # Forbid ISA_TEMPLATE / GS_TEMPLATE / etc — the giveaway pattern of
    # the old generator.
    for forbidden in ("ISA_TEMPLATE", "GS_TEMPLATE", "IEA_TEMPLATE",
                      "ST_TEMPLATE", "SE_TEMPLATE"):
        assert forbidden not in src, (
            f"generator still defines {forbidden} — should delegate to "
            "healthcare_libs.x12"
        )


# ---- Live integration test (uses real catalog) --------------------------

def test_generator_end_to_end_on_real_catalog(tmp_path):
    """End-to-end: decompose → plan → validate → persist → materialize on real catalog."""
    catalog = ROOT / "data" / "catalog.ddb"
    if not catalog.exists():
        pytest.skip("catalog not present")

    import duckdb
    sys.path.insert(0, str(ROOT / "scripts"))
    from resolution import EntityResolver  # noqa: E402

    conn = duckdb.connect(str(catalog))
    try:
        resolver = EntityResolver(conn)
        gen = edi_roundtrip.make_edi_roundtrip_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "270",
            output_root=str(tmp_path), overwrite=True,
        )
        assert package_id > 0, f"persistence failed: {issues}"
        # Errors block persistence; warnings are fine
        errors = [i for i in issues if i.severity == "error"]
        assert not errors, f"validation errors: {errors}"

        # Files materialized
        pkg_dir = tmp_path / "edi-roundtrip-270"
        assert pkg_dir.exists()
        assert (pkg_dir / "README.md").exists()
        assert (pkg_dir / "transformer.py").exists()
        assert (pkg_dir / "fixtures" / "270_request.x12").exists()
        assert (pkg_dir / "fixtures" / "271_response.x12").exists()
        assert (pkg_dir / "tests" / "test_roundtrip.py").exists()

        # Fixture has envelope
        req = (pkg_dir / "fixtures" / "270_request.x12").read_text()
        assert "ISA*" in req and "IEA*" in req

        # Generated test + transformer are valid Python and import the lib
        import ast
        test_src = (pkg_dir / "tests" / "test_roundtrip.py").read_text()
        ast.parse(test_src)
        assert "from healthcare_libs import x12" in test_src

        transformer_src = (pkg_dir / "transformer.py").read_text()
        ast.parse(transformer_src)
        assert "from healthcare_libs import x12" in transformer_src

        # Fixture round-trips through the lib
        from healthcare_libs import x12 as _x12
        env, body = _x12.parse_envelope(req)
        assert env.txn_set == "270"
        assert _x12.round_trip(req)
    finally:
        conn.close()
