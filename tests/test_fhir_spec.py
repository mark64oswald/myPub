"""Tests for scripts/fhir_spec.py — FHIR packages as a doc source."""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "kb-mcp"))

import fhir_spec as fs  # noqa: E402
from sectionizer import sectionize  # noqa: E402

PATIENT = {
    "resourceType": "StructureDefinition", "id": "Patient", "name": "Patient",
    "kind": "resource", "derivation": "specialization", "abstract": False,
    "url": "http://hl7.org/fhir/StructureDefinition/Patient",
    "baseDefinition": "http://hl7.org/fhir/StructureDefinition/DomainResource",
    "description": "Demographics and other administrative information about an individual.",
    "extension": [{"url": "http://hl7.org/fhir/StructureDefinition/structuredefinition-fmm",
                   "valueInteger": 5}],
    "snapshot": {"element": [
        {"path": "Patient", "min": 0, "max": "*"},
        {"path": "Patient.identifier", "min": 0, "max": "*", "type": [{"code": "Identifier"}],
         "short": "An identifier for this patient"},
        {"path": "Patient.gender", "min": 0, "max": "1", "type": [{"code": "code"}],
         "short": "male | female | other | unknown",
         "binding": {"strength": "required",
                     "valueSet": "http://hl7.org/fhir/ValueSet/administrative-gender"}},
        {"path": "Patient.contact.name.given", "min": 0, "max": "*"},   # depth 3: skipped
    ]},
}
DOMAIN = {"resourceType": "StructureDefinition", "name": "DomainResource",
          "kind": "resource", "derivation": "specialization", "abstract": True}
US_CORE = {
    "resourceType": "StructureDefinition", "name": "USCorePatientProfile",
    "kind": "resource", "derivation": "constraint",
    "baseDefinition": "http://hl7.org/fhir/StructureDefinition/Patient",
    "differential": {"element": [
        {"path": "Patient.identifier", "min": 1, "max": "*", "mustSupport": True},
    ]},
}


def _package(tmp_path, docs):
    tgz = tmp_path / "pkg.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        for name, doc in docs.items():
            data = json.dumps(doc).encode()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tgz


def test_render_core_resources_only_by_default(tmp_path):
    tgz = _package(tmp_path, {
        "package.json": {"name": "hl7.fhir.r4.core", "version": "4.0.1", "title": "FHIR R4"},
        "StructureDefinition-Patient.json": PATIENT,
        "StructureDefinition-DomainResource.json": DOMAIN,
        "StructureDefinition-us-core-patient.json": US_CORE,
    })
    ident, md = fs.package_markdown(tgz)
    assert ident == "hl7.fhir.r4.core@4.0.1"
    assert "## Patient" in md and "DomainResource\n" not in md.split("## Patient")[0]
    assert "USCorePatientProfile" not in md
    assert "maturity FMM5" in md
    assert "| `Patient.gender` | 0..1 | code | male \\| female \\| other \\| unknown " \
           "(binding required: administrative-gender) |" in md
    assert "Patient.contact.name.given" not in md

    _, with_profiles = fs.package_markdown(tgz, include_profiles=True)
    assert "## USCorePatientProfile" in with_profiles
    assert "### Constrained elements" in with_profiles
    assert "| `Patient.identifier` | 1..* MS |" in with_profiles


def test_fetcher_output_sectionizes_one_section_per_resource(tmp_path):
    tgz = _package(tmp_path, {
        "package.json": {"name": "hl7.fhir.us.core", "version": "6.1.0"},
        "a.json": PATIENT, "b.json": US_CORE,
    })
    fetched = fs.FhirPackageFetcher(tgz, include_profiles=True).fetch("hl7.fhir.us.core@6.1.0")
    assert fetched.url == "https://packages.fhir.org/hl7.fhir.us.core/6.1.0"
    roots = sectionize({"source_type": fetched.source_type, "content": fetched.content})
    headings = [c.heading_text for r in roots for c in (r.children or [])]
    assert headings == ["Patient", "USCorePatientProfile"]


PID_MAP = {
    "resourceType": "ConceptMap", "name": "SegmentPIDToPatient",
    "title": "Segment PID to Patient Map",
    "sourceCanonical": "http://hl7.org/v2/PID", "targetCanonical": "http://hl7.org/fhir/Patient",
    "group": [{"element": [
        {"code": "PID-3", "display": "Patient Identifier List",
         "target": [{"code": "Patient.identifier", "relationship": "equivalent",
                     "dependsOn": [{"property": "PID-3.4", "value": "MR"}]}]},
        {"code": "PID-8", "display": "Administrative Sex",
         "target": [{"code": "Patient.gender", "equivalence": "equivalent",
                     "comment": "via v2-0001 map"}]},
    ]}],
}


def test_conceptmaps_render_only_when_asked(tmp_path):
    tgz = _package(tmp_path, {"package.json": {"name": "hl7.fhir.uv.v2mappings", "version": "1.0.0"},
                              "ConceptMap-pid.json": PID_MAP})
    _, md = fs.package_markdown(tgz)
    assert "PID-3" not in md
    _, md = fs.package_markdown(tgz, include_conceptmaps=True)
    assert "## Segment PID to Patient Map" in md
    assert "**Maps:** `PID` → `Patient`" in md
    assert "| `PID-3` | Patient Identifier List | `Patient.identifier` | equivalent | PID-3.4=MR |" in md
    assert "via v2-0001 map" in md
    assert "1 ConceptMap(s)." in md
