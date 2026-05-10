"""Tests for the shared healthcare sub-agent dispatch layer."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import healthcare_subagent as hsa  # noqa: E402
from generator import GenFile  # noqa: E402


def test_supported_kinds_matches_5_generators():
    assert hsa.supported_kinds() == [
        "deid_bundle",
        "edi_roundtrip",
        "fhir_ig_scaffold",
        "integration_channel",
        "standards_translator",
    ]


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown generator kind"):
        hsa.customization_prompts("not_a_real_kind", {})


@pytest.mark.parametrize("kind", hsa.supported_kinds())
def test_each_kind_emits_prompts_plus_readme(kind):
    files = hsa.customization_prompts(kind, {})
    assert all(isinstance(f, GenFile) for f in files)
    # At least 2 customization prompts + 1 README
    assert len(files) >= 3
    # All rooted at _sub_agent_prompts/
    assert all(f.filename.startswith("_sub_agent_prompts/") for f in files)
    # Exactly one README
    readmes = [f for f in files if f.filename.endswith("README.md")]
    assert len(readmes) == 1
    # All non-README prompts are markdown files
    prompts = [f for f in files if not f.filename.endswith("README.md")]
    assert all(f.filename.endswith(".md") for f in prompts)


@pytest.mark.parametrize("kind", hsa.supported_kinds())
def test_filenames_are_zero_padded_and_ordered(kind):
    files = hsa.customization_prompts(kind, {})
    prompts = [f for f in files if not f.filename.endswith("README.md")]
    names = [f.filename.split("/")[-1] for f in prompts]
    # Each filename starts with NN_
    for n in names:
        assert n[:2].isdigit() and n[2] == "_", f"bad name: {n}"
    # Already sorted
    assert names == sorted(names)


def test_edi_roundtrip_template_substitution():
    files = hsa.customization_prompts("edi_roundtrip", {
        "txn_code": "270",
        "txn_code_lower": "270",
        "txn_name": "Eligibility Inquiry",
        "paired": "271",
    })
    contents = [f.content for f in files]
    blob = "\n".join(contents)
    # Real values landed (no leftover {placeholder} markers)
    assert "270" in blob
    assert "Eligibility Inquiry" in blob
    assert "271" in blob
    # Specific anchor: trading-partner prompt mentions ISA-06/ISA-08
    tp = next(f for f in files if "trading_partner" in f.filename)
    assert "ISA-06" in tp.content
    assert "ISA-08" in tp.content


def test_deid_bundle_shape_propagates():
    files = hsa.customization_prompts("deid_bundle", {
        "shape": "fhir",
        "shape_ext": "json",
    })
    blob = "\n".join(f.content for f in files)
    assert "fhir" in blob
    # k-anonymity prompt names healthcare_libs.deid.k_anonymize
    ka = next(f for f in files if "k_anonymity" in f.filename)
    assert "k_anonymize" in ka.content
    assert "healthcare_libs.deid" in ka.content


def test_standards_translator_substitution():
    files = hsa.customization_prompts("standards_translator", {
        "source_format": "HL7v2 ADT",
        "target_format": "FHIR R4",
        "transformer_func": "adt_a01_to_patient_encounter",
    })
    blob = "\n".join(f.content for f in files)
    assert "HL7v2 ADT" in blob
    assert "FHIR R4" in blob
    assert "adt_a01_to_patient_encounter" in blob


def test_fhir_ig_scaffold_substitution():
    files = hsa.customization_prompts("fhir_ig_scaffold", {
        "ig_name": "US Core 6.1",
    })
    blob = "\n".join(f.content for f in files)
    assert "US Core 6.1" in blob
    # Must-support review prompt anchors on FSH `MS` flag
    ms = next(f for f in files if "must_support" in f.filename)
    assert "MS" in ms.content
    assert "input/fsh/profiles" in ms.content


def test_integration_channel_substitution():
    files = hsa.customization_prompts("integration_channel", {
        "scenario": "Lab → EHR",
        "engine_target": "OIE",
        "source_format": "HL7v2 ORU^R01",
        "target_format": "FHIR Bundle",
    })
    blob = "\n".join(f.content for f in files)
    assert "Lab → EHR" in blob
    assert "OIE" in blob
    assert "HL7v2 ORU^R01" in blob


def test_missing_context_keys_leave_placeholders():
    """Missing format-map keys render as literal `{key}` markers, not crash."""
    files = hsa.customization_prompts("edi_roundtrip", {})  # nothing!
    blob = "\n".join(f.content for f in files)
    # The literal placeholder is preserved as a TODO marker for the sub-agent
    assert "{txn_code}" in blob


@pytest.mark.parametrize("kind", hsa.supported_kinds())
def test_dispatch_readme_lists_every_prompt(kind):
    """The README references every customization prompt by filename."""
    readme = hsa.dispatch_readme(kind)
    files = hsa.customization_prompts(kind, {})
    prompts = [f for f in files if not f.filename.endswith("README.md")]
    for p in prompts:
        # bare filename should appear in the README (without the dir prefix)
        bare = p.filename.split("/")[-1]
        assert bare in readme, f"{kind}: README missing {bare}"


def test_dispatch_readme_explains_how_to_run():
    readme = hsa.dispatch_readme("edi_roundtrip")
    # Both manual + sub-agent paths called out
    assert "Manually" in readme
    assert "Sub-agent" in readme or "sub-agent" in readme
    # Mentions the Task tool path
    assert "Task tool" in readme


# ---- End-to-end integration: each generator's planner emits prompts -------

def _mock_no_rows():
    class _MockConn:
        def execute(self, *args, **kwargs):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()
    return _MockConn()


def _has_subagent_prompts(plan) -> bool:
    """Plan includes at least one _sub_agent_prompts/ file + a README."""
    prompts = [f for f in plan.files
               if f.filename.startswith("_sub_agent_prompts/")]
    if not prompts:
        return False
    has_readme = any(f.filename == "_sub_agent_prompts/README.md"
                     for f in prompts)
    return has_readme and len(prompts) >= 3


def test_edi_roundtrip_plan_includes_subagent_prompts():
    import edi_roundtrip as eg
    dec = eg.EdiRoundTripDecomposer().decompose(_mock_no_rows(), None, "270")
    plan = eg.EdiRoundTripPlanner().plan(_mock_no_rows(), dec)
    assert _has_subagent_prompts(plan)


def test_deid_bundle_plan_includes_subagent_prompts():
    import deid_bundle as dg
    dec = dg.DeidDecomposer().decompose(_mock_no_rows(), None, "fhir")
    plan = dg.DeidPlanner().plan(_mock_no_rows(), dec)
    assert _has_subagent_prompts(plan)


def test_standards_translator_plan_includes_subagent_prompts():
    import standards_translator as st
    dec = st.StandardsTranslatorDecomposer().decompose(
        _mock_no_rows(), None, "hl7v2-adt-a01-to-fhir-patient-encounter",
    )
    plan = st.StandardsTranslatorPlanner().plan(_mock_no_rows(), dec)
    assert _has_subagent_prompts(plan)


def test_fhir_ig_scaffold_plan_includes_subagent_prompts():
    import fhir_ig_scaffold as fg
    dec = fg.FhirIgDecomposer().decompose(
        _mock_no_rows(), None, "us_core_patient",
    )
    plan = fg.FhirIgPlanner().plan(_mock_no_rows(), dec)
    assert _has_subagent_prompts(plan)


def test_integration_channel_plan_includes_subagent_prompts():
    import integration_channel as ig
    dec = ig.IntegrationChannelDecomposer().decompose(
        _mock_no_rows(), None, "lab_result_to_ehr_fhir",
    )
    plan = ig.IntegrationChannelPlanner().plan(_mock_no_rows(), dec)
    assert _has_subagent_prompts(plan)
