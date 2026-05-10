"""Tests for healthcare_libs.dicom — production DICOM reference impl."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import pydicom  # noqa: E402
from pydicom.dataset import Dataset, FileDataset  # noqa: E402
from pydicom.tag import Tag  # noqa: E402

from healthcare_libs import dicom  # noqa: E402


# ---- build_minimal_dataset ----------------------------------------------

def test_build_minimal_dataset_has_valid_file_meta():
    """The result must have file_meta with the three Part-10-mandatory tags."""
    ds = dicom.build_minimal_dataset()
    assert ds.file_meta is not None
    assert ds.file_meta.MediaStorageSOPClassUID
    assert ds.file_meta.MediaStorageSOPInstanceUID
    assert ds.file_meta.TransferSyntaxUID
    # SOPInstanceUID in dataset must match file_meta per PS3.10 §7.1
    assert ds.SOPInstanceUID == ds.file_meta.MediaStorageSOPInstanceUID


def test_build_minimal_dataset_has_required_type1_tags():
    """All Type 1 (mandatory, non-empty) tags must be populated."""
    ds = dicom.build_minimal_dataset()
    for tag_name in dicom.TYPE_1_REQUIRED_TAGS:
        assert hasattr(ds, tag_name), f"missing Type 1 tag: {tag_name}"
        val = getattr(ds, tag_name)
        assert val is not None and str(val).strip() != "", \
            f"Type 1 tag {tag_name} is empty"


def test_build_minimal_dataset_kwargs_flow_through():
    """Custom kwargs must end up on the resulting dataset."""
    ds = dicom.build_minimal_dataset(
        patient_id="ABC999",
        patient_name="SMITH^ALEX",
        patient_birth_date="19920101",
        study_date="20251231",
        modality="MR",
        study_description="MR BRAIN",
    )
    assert str(ds.PatientID) == "ABC999"
    assert str(ds.PatientName) == "SMITH^ALEX"
    assert str(ds.PatientBirthDate) == "19920101"
    assert str(ds.StudyDate) == "20251231"
    assert str(ds.Modality) == "MR"
    assert str(ds.StudyDescription) == "MR BRAIN"


def test_build_minimal_dataset_modality_picks_correct_sop_class():
    """Each modality should map to its standard image SOP class UID."""
    for modality, expected_sop in [
        ("CT", "1.2.840.10008.5.1.4.1.1.2"),
        ("MR", "1.2.840.10008.5.1.4.1.1.4"),
        ("US", "1.2.840.10008.5.1.4.1.1.6.1"),
    ]:
        ds = dicom.build_minimal_dataset(modality=modality)
        assert str(ds.SOPClassUID) == expected_sop, \
            f"{modality} should map to {expected_sop}, got {ds.SOPClassUID}"


# ---- build_minimal_dicom_bytes ------------------------------------------

def test_build_minimal_dicom_bytes_produces_parseable_part10():
    """Bytes output must round-trip through pydicom.dcmread()."""
    raw = dicom.build_minimal_dicom_bytes(patient_id="MRN42")
    assert isinstance(raw, bytes)
    assert len(raw) > 132  # at least 128-byte preamble + DICM magic + something
    assert raw[128:132] == b"DICM", "missing DICOM Part 10 magic at offset 128"
    parsed = pydicom.dcmread(io.BytesIO(raw))
    assert str(parsed.PatientID) == "MRN42"


# ---- parse_meta ----------------------------------------------------------

def test_parse_meta_extracts_uid_hierarchy():
    """Study/Series/Instance UIDs must come through correctly."""
    s_uid = dicom.generate_uid()
    se_uid = dicom.generate_uid()
    sop_uid = dicom.generate_uid()
    ds = dicom.build_minimal_dataset(
        study_uid=s_uid, series_uid=se_uid, sop_instance_uid=sop_uid,
    )
    meta = dicom.parse_meta(ds)
    assert meta.study_instance_uid == s_uid
    assert meta.series_instance_uids == [se_uid]
    assert meta.sop_instance_uids == [sop_uid]
    assert meta.series_count == 1
    assert meta.instance_count == 1


def test_parse_meta_extracts_patient_demographics():
    """Patient ID, name, DOB must be parsed."""
    ds = dicom.build_minimal_dataset(
        patient_id="DEMO1", patient_name="LAST^FIRST",
        patient_birth_date="19700704",
    )
    meta = dicom.parse_meta(ds)
    assert meta.patient_id == "DEMO1"
    assert meta.patient_name == "LAST^FIRST"
    assert meta.patient_birth_date == "19700704"


def test_parse_meta_extracts_study_metadata():
    """Study date, accession, modality, description, institution."""
    ds = dicom.build_minimal_dataset(
        study_date="20260301",
        accession_number="A98765",
        modality="CT",
        study_description="CT ABDOMEN",
        institution_name="MAYO CLINIC",
        referring_physician="DOC^WHO",
    )
    meta = dicom.parse_meta(ds)
    assert meta.study_date == "20260301"
    assert meta.accession_number == "A98765"
    assert meta.modality == "CT"
    assert meta.study_description == "CT ABDOMEN"
    assert meta.institution_name == "MAYO CLINIC"
    assert meta.referring_physician == "DOC^WHO"
    assert "CT" in meta.modalities_in_study


def test_parse_meta_handles_missing_optional_tags():
    """Missing optional tags should produce None, not raise."""
    ds = dicom.build_minimal_dataset()
    # Strip a few optional tags to verify graceful handling
    del ds.AccessionNumber
    del ds.StudyDescription
    del ds.InstitutionName
    meta = dicom.parse_meta(ds)
    assert meta.accession_number is None
    assert meta.study_description is None
    assert meta.institution_name is None


def test_parse_meta_accepts_bytes():
    """Bytes input should work as well as Dataset input."""
    raw = dicom.build_minimal_dicom_bytes(patient_id="BYTES1")
    meta = dicom.parse_meta(raw)
    assert meta.patient_id == "BYTES1"
    assert meta.study_instance_uid  # non-empty


def test_parse_meta_rejects_wrong_type():
    with pytest.raises(TypeError):
        dicom.parse_meta(12345)  # type: ignore[arg-type]


# ---- generate_uid -------------------------------------------------------

def test_generate_uid_returns_unique_values():
    """Two consecutive calls must yield different UIDs."""
    a = dicom.generate_uid()
    b = dicom.generate_uid()
    assert a != b, "generate_uid produced duplicate"


def test_generate_uid_returns_valid_format():
    """A DICOM UID is dot-separated digits, max 64 chars."""
    uid = dicom.generate_uid()
    assert len(uid) <= 64
    assert "." in uid
    parts = uid.split(".")
    assert all(p.isdigit() for p in parts), \
        f"UID has non-digit components: {uid}"


def test_generate_uid_honors_prefix():
    uid = dicom.generate_uid(prefix="1.2.3.4.")
    assert uid.startswith("1.2.3.4.")


# ---- validate -----------------------------------------------------------

def test_validate_clean_dataset_returns_no_errors():
    """A freshly built minimal dataset should validate clean."""
    ds = dicom.build_minimal_dataset()
    issues = dicom.validate(ds)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"clean dataset produced errors: {errors}"


def test_validate_catches_missing_required_type1():
    """Removing a Type 1 tag should produce an error issue."""
    ds = dicom.build_minimal_dataset()
    del ds.PatientID
    issues = dicom.validate(ds)
    pid_errors = [i for i in issues if "PatientID" in i.message]
    assert pid_errors, f"expected PatientID error, got: {[i.message for i in issues]}"
    assert pid_errors[0].severity == "error"


def test_validate_catches_empty_required_type1():
    """An empty value for a Type 1 tag should also error."""
    ds = dicom.build_minimal_dataset()
    ds.PatientID = ""
    issues = dicom.validate(ds)
    pid_errors = [i for i in issues if "PatientID" in i.message]
    assert pid_errors
    assert pid_errors[0].severity == "error"


def test_validate_catches_uid_mismatch_with_file_meta():
    """SOPInstanceUID must match file_meta.MediaStorageSOPInstanceUID."""
    ds = dicom.build_minimal_dataset()
    ds.SOPInstanceUID = dicom.generate_uid()  # diverges from file_meta
    issues = dicom.validate(ds)
    mismatch = [i for i in issues if i.code == "UID_MISMATCH"]
    assert mismatch, f"expected UID_MISMATCH, got: {[(i.code, i.message) for i in issues]}"


def test_validate_accepts_bytes_input():
    raw = dicom.build_minimal_dicom_bytes()
    issues = dicom.validate(raw)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


# ---- deidentify_basic_profile -------------------------------------------

def test_deidentify_removes_patient_name():
    """PatientName is a Z action (replace with empty)."""
    ds = dicom.build_minimal_dataset(patient_name="DOE^JANE")
    out = dicom.deidentify_basic_profile(ds)
    assert str(out.PatientName) == ""


def test_deidentify_removes_referring_physician():
    """ReferringPhysicianName is a Z action."""
    ds = dicom.build_minimal_dataset(referring_physician="SMITH^JOHN")
    out = dicom.deidentify_basic_profile(ds)
    assert str(out.ReferringPhysicianName) == ""


def test_deidentify_replaces_patient_id_with_pseudonym():
    """When patient_pseudonym is provided, PatientID gets the pseudonym."""
    ds = dicom.build_minimal_dataset(patient_id="REAL_MRN_999")
    out = dicom.deidentify_basic_profile(ds, patient_pseudonym="PSEUDO_001")
    assert str(out.PatientID) == "PSEUDO_001"


def test_deidentify_empties_patient_id_without_pseudonym():
    """Without a pseudonym, PatientID is emptied (Z action)."""
    ds = dicom.build_minimal_dataset(patient_id="REAL_MRN_999")
    out = dicom.deidentify_basic_profile(ds)
    assert str(out.PatientID) == ""


def test_deidentify_shifts_dates_by_offset_days():
    """date_offset_days must shift StudyDate, PatientBirthDate, etc."""
    ds = dicom.build_minimal_dataset(
        study_date="20260115", patient_birth_date="19850615",
    )
    out = dicom.deidentify_basic_profile(ds, date_offset_days=-30)
    # 2026-01-15 minus 30 days = 2025-12-16
    assert str(out.StudyDate) == "20251216"
    # 1985-06-15 minus 30 days = 1985-05-16
    assert str(out.PatientBirthDate) == "19850516"


def test_deidentify_empties_dates_when_no_offset():
    """date_offset_days=0 means clear dates entirely."""
    ds = dicom.build_minimal_dataset(study_date="20260115")
    out = dicom.deidentify_basic_profile(ds, date_offset_days=0)
    assert str(out.StudyDate) == ""


def test_deidentify_removes_private_tags_by_default():
    """Private tags (odd group #) should be stripped by default."""
    ds = dicom.build_minimal_dataset()
    # Add a private tag
    ds.add_new(Tag(0x0009, 0x0010), "LO", "ACME_PRIVATE_CREATOR")
    ds.add_new(Tag(0x0009, 0x1001), "LO", "PRIVATE_VALUE_HERE")
    assert Tag(0x0009, 0x1001) in ds  # sanity

    out = dicom.deidentify_basic_profile(ds)
    assert Tag(0x0009, 0x0010) not in out, "private creator tag should be removed"
    assert Tag(0x0009, 0x1001) not in out, "private value tag should be removed"


def test_deidentify_keeps_private_tags_when_flag_set():
    """keep_private_tags=True should preserve private tags."""
    ds = dicom.build_minimal_dataset()
    ds.add_new(Tag(0x0009, 0x0010), "LO", "ACME_PRIVATE_CREATOR")
    ds.add_new(Tag(0x0009, 0x1001), "LO", "PRIVATE_VALUE_HERE")

    out = dicom.deidentify_basic_profile(ds, keep_private_tags=True)
    assert Tag(0x0009, 0x0010) in out
    assert Tag(0x0009, 0x1001) in out


def test_deidentify_does_not_mutate_input():
    """The input dataset must remain unchanged after de-id."""
    ds = dicom.build_minimal_dataset(
        patient_id="ORIG_MRN", patient_name="ORIG^NAME", study_date="20260115",
    )
    orig_pid = str(ds.PatientID)
    orig_name = str(ds.PatientName)
    orig_date = str(ds.StudyDate)

    _ = dicom.deidentify_basic_profile(ds, patient_pseudonym="PSEUDO")

    assert str(ds.PatientID) == orig_pid
    assert str(ds.PatientName) == orig_name
    assert str(ds.StudyDate) == orig_date


def test_deidentify_adds_method_code_sequence():
    """DeidentificationMethodCodeSequence must be set with DCM 113100."""
    ds = dicom.build_minimal_dataset()
    out = dicom.deidentify_basic_profile(ds)

    assert hasattr(out, "DeidentificationMethodCodeSequence")
    seq = out.DeidentificationMethodCodeSequence
    assert len(seq) >= 1
    item = seq[0]
    assert str(item.CodeValue) == "113100"
    assert str(item.CodingSchemeDesignator) == "DCM"
    assert "Basic Application" in str(item.CodeMeaning)

    # PatientIdentityRemoved should also be set
    assert str(out.PatientIdentityRemoved) == "YES"


def test_deidentify_removes_institution_and_accession():
    """InstitutionName (X) and AccessionNumber (Z) are both PHI vectors."""
    ds = dicom.build_minimal_dataset(
        institution_name="MAYO CLINIC", accession_number="ACC12345",
    )
    out = dicom.deidentify_basic_profile(ds)
    assert not hasattr(out, "InstitutionName"), \
        "InstitutionName should be removed (X action)"
    assert str(out.AccessionNumber) == "", \
        "AccessionNumber should be emptied (Z action)"


# ---- has_burned_in_phi_risk --------------------------------------------

def test_burned_in_risk_true_when_annotation_yes():
    """Explicit BurnedInAnnotation=YES always triggers True."""
    ds = dicom.build_minimal_dataset(modality="CT")
    ds.BurnedInAnnotation = "YES"
    assert dicom.has_burned_in_phi_risk(ds) is True


def test_burned_in_risk_true_for_ultrasound():
    """US is in the at-risk modality set."""
    ds = dicom.build_minimal_dataset(modality="US")
    assert dicom.has_burned_in_phi_risk(ds) is True


@pytest.mark.parametrize("modality", ["US", "NM", "XA", "RF"])
def test_burned_in_risk_true_for_at_risk_modalities(modality):
    """All canonical at-risk modalities trigger True without explicit flag."""
    ds = dicom.build_minimal_dataset(modality=modality)
    assert dicom.has_burned_in_phi_risk(ds) is True, \
        f"{modality} should trigger burned-in risk"


def test_burned_in_risk_false_for_ct_without_flag():
    """CT does not carry burned-in PHI by default."""
    ds = dicom.build_minimal_dataset(modality="CT")
    assert dicom.has_burned_in_phi_risk(ds) is False


def test_burned_in_risk_false_for_mr_without_flag():
    """MR does not carry burned-in PHI by default."""
    ds = dicom.build_minimal_dataset(modality="MR")
    assert dicom.has_burned_in_phi_risk(ds) is False


# ---- round_trip ---------------------------------------------------------

def test_round_trip_returns_true_for_minimal_dataset():
    ds = dicom.build_minimal_dataset()
    assert dicom.round_trip(ds) is True


def test_round_trip_preserves_patient_id_and_study_uid():
    """After serialize+reparse, the key tags must match the input."""
    s_uid = dicom.generate_uid()
    ds = dicom.build_minimal_dataset(patient_id="RT_PATIENT_42", study_uid=s_uid)

    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    buf.seek(0)
    reread = pydicom.dcmread(buf)

    assert str(reread.PatientID) == "RT_PATIENT_42"
    assert str(reread.StudyInstanceUID) == s_uid


def test_round_trip_works_with_bytes_input():
    raw = dicom.build_minimal_dicom_bytes()
    assert dicom.round_trip(raw) is True


def test_parse_meta_accepts_bytesio():
    """BytesIO should be accepted just like raw bytes."""
    raw = dicom.build_minimal_dicom_bytes(patient_id="BIO1")
    bio = io.BytesIO(raw)
    meta = dicom.parse_meta(bio)
    assert meta.patient_id == "BIO1"


# ---- Parametrized: end-to-end per modality ------------------------------

@pytest.mark.parametrize("modality", ["CT", "MR", "US", "NM"])
def test_each_modality_builds_parses_and_validates(modality):
    """For each common modality: build → parse → validate → round-trip."""
    ds = dicom.build_minimal_dataset(modality=modality)

    # Parse extracts the right modality
    meta = dicom.parse_meta(ds)
    assert meta.modality == modality
    assert meta.study_instance_uid

    # Validates clean
    issues = dicom.validate(ds)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"{modality} produced errors: {errors}"

    # Round trips
    assert dicom.round_trip(ds) is True

    # Bytes path also works
    raw = dicom.build_minimal_dicom_bytes(modality=modality)
    meta2 = dicom.parse_meta(raw)
    assert meta2.modality == modality
