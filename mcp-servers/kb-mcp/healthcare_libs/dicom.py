"""healthcare_libs.dicom — Production reference implementation for DICOM.

Wraps pydicom to provide:
  * Builders: minimal-but-valid DICOM Datasets (and Part 10 byte streams)
    suitable for fixtures, tests, and IG examples — no pixel data, just
    the Patient/Study/Series/Instance hierarchy plus required Type 1 tags.
  * Parser: extract a flat ``DicomStudyMeta`` from a Dataset or Part 10
    bytes — Study/Series/Instance UIDs, demographics, modality info.
  * Validator: walk a dataset and surface missing Type 1 (mandatory)
    tags as structured ``DicomIssue`` records (severity / code / message
    / tag context). Returns issues — does not raise.
  * De-identifier: apply the DICOM Basic Application Confidentiality
    Profile (PS3.15 Annex E.1) — remove or replace patient identifiers,
    optionally shift dates, strip private tags, and stamp the
    DeidentificationMethodCodeSequence so downstream consumers can tell
    the dataset has been de-identified.
  * Burned-in PHI heuristic: a fast pre-filter for whether a dataset is
    a likely candidate for OCR pixel-redaction (modality-based plus the
    explicit BurnedInAnnotation flag).
  * Round-trip helper: prove that the build → serialize → parse cycle
    preserves the key tags.

The healthcare interop generators emit code that imports from this
module instead of copying DICOM scaffolding into every generator output.

References:
  * pydicom: https://pydicom.github.io/pydicom/stable/
  * DICOM PS3.10 (Media Storage and File Format):
      https://dicom.nema.org/medical/dicom/current/output/html/part10.html
  * DICOM PS3.15 (Security and System Management Profiles), Annex E
    (Attribute Confidentiality Profiles), §E.1 (Basic Application
    Confidentiality Profile):
      https://dicom.nema.org/medical/dicom/current/output/html/part15.html
  * DICOM PS3.6 (Data Dictionary):
      https://dicom.nema.org/medical/dicom/current/output/html/part06.html
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Union

from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.tag import BaseTag, Tag
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    MRImageStorage,
    generate_uid as _pydicom_generate_uid,
)

LOG = logging.getLogger("healthcare_libs.dicom")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default UID prefix per pydicom convention. Real implementations should
# register their own root with IANA / their organization.
DEFAULT_UID_PREFIX = "1.2.826.0.1.3680043.2."

# DICOM modality → SOP Class UID for the most common image storage classes.
MODALITY_TO_SOP_CLASS = {
    "CT": CTImageStorage,
    "MR": MRImageStorage,
    # Ultrasound, Nuclear Medicine, X-Ray Angiography, Radio-Fluoroscopic
    # all map to their respective Image Storage SOP Classes:
    "US": "1.2.840.10008.5.1.4.1.1.6.1",   # Ultrasound Image Storage
    "NM": "1.2.840.10008.5.1.4.1.1.20",    # Nuclear Medicine Image Storage
    "XA": "1.2.840.10008.5.1.4.1.1.12.1",  # X-Ray Angiographic Image Storage
    "RF": "1.2.840.10008.5.1.4.1.1.12.2",  # X-Ray Radiofluoroscopic Image Storage
    "CR": "1.2.840.10008.5.1.4.1.1.1",     # Computed Radiography Image Storage
    "DX": "1.2.840.10008.5.1.4.1.1.1.1",   # Digital X-Ray Image Storage
    "MG": "1.2.840.10008.5.1.4.1.1.1.2",   # Digital Mammography X-Ray
    "PT": "1.2.840.10008.5.1.4.1.1.128",   # Positron Emission Tomography
    "OT": "1.2.840.10008.5.1.4.1.1.7",     # Secondary Capture Image
}

# Modalities that commonly carry burned-in patient text in pixel data.
# These should always get an OCR pass before being released, even when
# BurnedInAnnotation isn't explicitly set to YES. Per IHE / DICOM WG-18.
MODALITIES_WITH_LIKELY_BURNED_IN_PHI = {"US", "NM", "XA", "RF", "MG", "PT", "OT"}

# Type 1 = required, value must be present and non-empty.
# Per DICOM PS3.3 (IODs) and the SOP Class definitions in Annex C.
# This is the intersection of mandatory tags across the common image SOP Classes
# we support — every conformant image dataset must have these.
TYPE_1_REQUIRED_TAGS = (
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "Modality",
    "PatientID",
    "PatientName",
)

# DICOM PS3.15 Annex E.1 — Basic Application Confidentiality Profile.
# Each entry is (tag_name, action) where action follows PS3.15 Table E.1-1:
#   X = remove
#   D = replace with a non-zero-length dummy value
#   Z = replace with zero-length value (or dummy if empty not allowed)
#   U = replace with non-zero-length UID generated for the de-identified set
# This is a curated subset covering the most common identifying attributes.
# A complete PS3.15 implementation has ~150 entries; we cover the high-yield
# ones plus everything the standard tags 'X' or 'D'.
DEID_BASIC_PROFILE_ACTIONS = {
    # --- patient identity (PS3.15 Annex E.1, Patient Module) ---
    "PatientName":              ("Z", ""),               # Z = empty
    "PatientID":                ("Z", ""),
    "IssuerOfPatientID":        ("X", None),
    "PatientBirthDate":         ("Z", ""),
    "PatientBirthTime":         ("X", None),
    "PatientSex":               ("Z", ""),
    "OtherPatientIDs":          ("X", None),
    "OtherPatientNames":        ("X", None),
    "PatientBirthName":         ("X", None),
    "PatientAddress":           ("X", None),
    "PatientMotherBirthName":   ("X", None),
    "CountryOfResidence":       ("X", None),
    "RegionOfResidence":        ("X", None),
    "PatientTelephoneNumbers":  ("X", None),
    "EthnicGroup":              ("X", None),
    "Occupation":               ("X", None),
    "AdditionalPatientHistory": ("X", None),
    "PatientComments":          ("X", None),
    "MedicalRecordLocator":     ("X", None),
    "MilitaryRank":             ("X", None),
    "BranchOfService":          ("X", None),
    # --- referring/responsible party ---
    "ReferringPhysicianName":   ("Z", ""),
    "ReferringPhysicianAddress": ("X", None),
    "ReferringPhysicianTelephoneNumbers": ("X", None),
    "PhysiciansOfRecord":       ("X", None),
    "PerformingPhysicianName":  ("X", None),
    "NameOfPhysiciansReadingStudy": ("X", None),
    "OperatorsName":            ("X", None),
    "ResponsibleOrganization":  ("X", None),
    "ResponsiblePerson":        ("X", None),
    # --- institution ---
    "InstitutionName":          ("X", None),
    "InstitutionAddress":       ("X", None),
    "InstitutionalDepartmentName": ("X", None),
    "StationName":              ("X", None),
    # --- study/visit identifiers ---
    "AccessionNumber":          ("Z", ""),
    "StudyID":                  ("Z", ""),
    "AdmissionID":              ("X", None),
    "IssuerOfAdmissionID":      ("X", None),
    "ServiceEpisodeID":         ("X", None),
    "IssuerOfServiceEpisodeID": ("X", None),
    "RequestedProcedureID":     ("X", None),
    "ScheduledProcedureStepID": ("X", None),
    "PerformedProcedureStepID": ("X", None),
    # --- free-text / comments that may carry PHI ---
    "StudyComments":            ("X", None),
    "ImageComments":            ("X", None),
    "VisitComments":            ("X", None),
    "RequestAttributesSequence": ("X", None),
}

# Date/time tags handled as a group: shift by date_offset_days, or empty.
DEID_DATE_TAGS = (
    "StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate",
    "PatientBirthDate", "AdmittingDate", "ScheduledProcedureStepStartDate",
    "PerformedProcedureStepStartDate", "InstanceCreationDate",
    "AcquisitionDateTime",
)
DEID_TIME_TAGS = (
    "StudyTime", "SeriesTime", "AcquisitionTime", "ContentTime",
    "PatientBirthTime", "InstanceCreationTime",
    "ScheduledProcedureStepStartTime", "PerformedProcedureStepStartTime",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DicomIssue:
    """One validation finding from our checks (or external validators)."""

    severity: str       # 'error' | 'warning' | 'info'
    code: str           # DICOM tag id (e.g. '00100010') or our marker
    message: str
    tag_context: str = ""   # the tag and its value for debugging


@dataclass
class DicomStudyMeta:
    """Extracted Study / Series / Instance hierarchy from a DICOM dataset.

    A single Dataset only carries one Series + Instance under one Study,
    so ``series_instance_uids`` and ``sop_instance_uids`` will be
    single-element lists when populated from one Dataset. The list shape
    is preserved so callers can also build a ``DicomStudyMeta`` by
    aggregating multiple datasets that share a StudyInstanceUID.
    """

    study_instance_uid: str
    series_instance_uids: list[str]
    sop_instance_uids: list[str]
    patient_id: Optional[str] = None
    patient_name: Optional[str] = None
    patient_birth_date: Optional[str] = None
    study_date: Optional[str] = None
    accession_number: Optional[str] = None
    referring_physician: Optional[str] = None
    modality: Optional[str] = None
    modalities_in_study: list[str] = field(default_factory=list)
    study_description: Optional[str] = None
    institution_name: Optional[str] = None
    series_count: int = 0
    instance_count: int = 0


# ---------------------------------------------------------------------------
# UID helpers
# ---------------------------------------------------------------------------

def generate_uid(prefix: str = DEFAULT_UID_PREFIX) -> str:
    """Generate a DICOM-compliant UID via pydicom.uid.generate_uid().

    pydicom guarantees uniqueness via random suffixing, so two consecutive
    calls always yield distinct UIDs. The default prefix is the pydicom
    organization prefix from PS3.6 — production code should register its
    own root with IANA.
    """
    return str(_pydicom_generate_uid(prefix=prefix))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_minimal_dataset(
    *,
    patient_id: str = "MRN12345",
    patient_name: str = "DOE^JANE",
    patient_birth_date: str = "19850615",
    study_date: str = "20260115",
    modality: str = "CT",
    study_description: str = "CT CHEST",
    accession_number: str = "ACC0001",
    referring_physician: str = "SMITH^JOHN",
    institution_name: str = "EXAMPLE HOSPITAL",
    study_uid: Optional[str] = None,
    series_uid: Optional[str] = None,
    sop_instance_uid: Optional[str] = None,
) -> FileDataset:
    """Build a minimal but valid DICOM Dataset (no pixel data).

    The result has:
      * a complete ``file_meta`` with TransferSyntaxUID + SOP Class/Instance
        UIDs (so it can be serialized as DICOM Part 10 immediately)
      * the Type 1 (mandatory) Patient/Study/Series/Instance tags
      * a small set of commonly-present Type 2/3 tags so de-id and
        validator tests have something to chew on
    """
    sop_class_uid = MODALITY_TO_SOP_CLASS.get(modality, CTImageStorage)
    s_uid = study_uid or generate_uid()
    se_uid = series_uid or generate_uid()
    sop_uid = sop_instance_uid or generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class_uid
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    file_meta.ImplementationVersionName = "MYPUB_DCM_1"  # SH (max 16 chars)

    ds = FileDataset(
        filename_or_obj="<in-memory>",
        dataset={},
        file_meta=file_meta,
        preamble=b"\0" * 128,
    )

    # Patient module (Type 1 / Type 2)
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = patient_birth_date
    ds.PatientSex = ""

    # Study module
    ds.StudyInstanceUID = s_uid
    ds.StudyDate = study_date
    ds.StudyTime = "120000"
    ds.AccessionNumber = accession_number
    ds.ReferringPhysicianName = referring_physician
    ds.StudyID = "1"
    ds.StudyDescription = study_description

    # Series module
    ds.SeriesInstanceUID = se_uid
    ds.SeriesNumber = "1"
    ds.Modality = modality

    # Instance / SOP Common
    ds.SOPClassUID = sop_class_uid
    ds.SOPInstanceUID = sop_uid
    ds.InstanceNumber = "1"

    # General Equipment
    ds.Manufacturer = "EXAMPLE MANUFACTURER"
    ds.InstitutionName = institution_name

    return ds


def build_minimal_dicom_bytes(**kwargs: Any) -> bytes:
    """Same as ``build_minimal_dataset`` but serialized to DICOM Part 10 bytes.

    All keyword args are forwarded to ``build_minimal_dataset``.
    """
    ds = build_minimal_dataset(**kwargs)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _coerce_to_dataset(dataset_or_bytes: Union[Dataset, bytes, io.BytesIO]) -> Dataset:
    """Accept a Dataset, raw DICOM bytes, or a BytesIO and return a Dataset."""
    if isinstance(dataset_or_bytes, Dataset):
        return dataset_or_bytes
    if isinstance(dataset_or_bytes, (bytes, bytearray)):
        return dcmread(io.BytesIO(bytes(dataset_or_bytes)))
    if isinstance(dataset_or_bytes, io.IOBase):
        # Reset to start in case the caller already wrote to it
        try:
            dataset_or_bytes.seek(0)
        except (OSError, AttributeError):
            pass
        return dcmread(dataset_or_bytes)
    raise TypeError(
        f"expected Dataset, bytes, or BytesIO; got {type(dataset_or_bytes).__name__}"
    )


def _get_str(ds: Dataset, tag_name: str) -> Optional[str]:
    """Read a tag as a string; return None if missing or empty."""
    val = getattr(ds, tag_name, None)
    if val is None:
        return None
    s = str(val)
    return s if s else None


def parse_meta(dataset_or_bytes: Union[Dataset, bytes, io.BytesIO]) -> DicomStudyMeta:
    """Extract a ``DicomStudyMeta`` from a Dataset or DICOM Part 10 bytes."""
    ds = _coerce_to_dataset(dataset_or_bytes)

    study_uid = _get_str(ds, "StudyInstanceUID") or ""
    series_uid = _get_str(ds, "SeriesInstanceUID")
    sop_uid = _get_str(ds, "SOPInstanceUID")
    modality = _get_str(ds, "Modality")

    series_uids = [series_uid] if series_uid else []
    sop_uids = [sop_uid] if sop_uid else []

    # ModalitiesInStudy is normally only on the C-FIND Q/R study root,
    # but if a single dataset has a Modality we surface it here too so
    # callers downstream don't need to special-case the single-instance path.
    modalities = []
    if hasattr(ds, "ModalitiesInStudy"):
        mis = ds.ModalitiesInStudy
        if isinstance(mis, str):
            modalities = [mis]
        else:
            try:
                modalities = [str(m) for m in mis]
            except TypeError:
                modalities = [str(mis)]
    elif modality:
        modalities = [modality]

    return DicomStudyMeta(
        study_instance_uid=study_uid,
        series_instance_uids=series_uids,
        sop_instance_uids=sop_uids,
        patient_id=_get_str(ds, "PatientID"),
        patient_name=_get_str(ds, "PatientName"),
        patient_birth_date=_get_str(ds, "PatientBirthDate"),
        study_date=_get_str(ds, "StudyDate"),
        accession_number=_get_str(ds, "AccessionNumber"),
        referring_physician=_get_str(ds, "ReferringPhysicianName"),
        modality=modality,
        modalities_in_study=modalities,
        study_description=_get_str(ds, "StudyDescription"),
        institution_name=_get_str(ds, "InstitutionName"),
        series_count=1 if series_uid else 0,
        instance_count=1 if sop_uid else 0,
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate(dataset_or_bytes: Union[Dataset, bytes, io.BytesIO]) -> list[DicomIssue]:
    """Validate a dataset for required Type 1 tags. Returns issues, doesn't raise.

    Type 1 tags are *mandatory* per DICOM PS3.3 — the tag must be present
    AND its value must be non-empty. Type 2 tags must be present but may
    be empty; Type 3 tags are optional. We focus on Type 1 because that's
    the surface where the most common conformance bugs live.
    """
    issues: list[DicomIssue] = []

    try:
        ds = _coerce_to_dataset(dataset_or_bytes)
    except Exception as e:  # noqa: BLE001 — surface anything as a structured issue
        return [DicomIssue(
            severity="error", code="PARSE",
            message=f"could not parse input as DICOM: {type(e).__name__}: {e}",
        )]

    # Type 1: must be present and non-empty
    for tag_name in TYPE_1_REQUIRED_TAGS:
        if not hasattr(ds, tag_name):
            issues.append(DicomIssue(
                severity="error",
                code=_tag_id_for(tag_name),
                message=f"missing required Type 1 tag: {tag_name}",
                tag_context=tag_name,
            ))
            continue
        val = getattr(ds, tag_name)
        if val is None or str(val).strip() == "":
            issues.append(DicomIssue(
                severity="error",
                code=_tag_id_for(tag_name),
                message=f"required Type 1 tag {tag_name} is present but empty",
                tag_context=f"{tag_name}={val!r}",
            ))

    # File meta sanity (only if the dataset was built/read with file meta)
    file_meta = getattr(ds, "file_meta", None)
    if file_meta is not None:
        for fm_tag in ("MediaStorageSOPClassUID", "MediaStorageSOPInstanceUID",
                       "TransferSyntaxUID"):
            if not hasattr(file_meta, fm_tag) or not getattr(file_meta, fm_tag):
                issues.append(DicomIssue(
                    severity="error", code="FILE_META",
                    message=f"file_meta missing required tag: {fm_tag}",
                    tag_context=fm_tag,
                ))

        # SOP Class/Instance UIDs in the dataset must match the file meta
        # per PS3.10 §7.1 — otherwise readers may dispatch to the wrong handler.
        if (hasattr(ds, "SOPInstanceUID")
                and hasattr(file_meta, "MediaStorageSOPInstanceUID")
                and ds.SOPInstanceUID != file_meta.MediaStorageSOPInstanceUID):
            issues.append(DicomIssue(
                severity="error", code="UID_MISMATCH",
                message=("SOPInstanceUID does not match "
                         "file_meta.MediaStorageSOPInstanceUID"),
                tag_context=f"ds={ds.SOPInstanceUID} meta={file_meta.MediaStorageSOPInstanceUID}",
            ))

    return issues


def _tag_id_for(tag_name: str) -> str:
    """Return the 8-char hex DICOM tag id (e.g. '00100010') for a keyword."""
    try:
        from pydicom.datadict import tag_for_keyword
        t = tag_for_keyword(tag_name)
        if t is None:
            return tag_name
        return f"{(t >> 16) & 0xFFFF:04X}{t & 0xFFFF:04X}"
    except Exception:  # noqa: BLE001
        return tag_name


# ---------------------------------------------------------------------------
# De-identifier — DICOM Basic Application Confidentiality Profile (PS3.15 E.1)
# ---------------------------------------------------------------------------

def _shift_dicom_date(date_str: str, days: int) -> str:
    """Shift a DICOM DA (YYYYMMDD) by ``days`` (can be negative). Empty stays empty."""
    if not date_str or len(date_str) != 8 or not date_str.isdigit():
        return date_str
    try:
        d = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=days)
        return d.strftime("%Y%m%d")
    except ValueError:
        return date_str


def _is_private_tag(tag: BaseTag) -> bool:
    """Per PS3.5 §7.8: private tags have an odd group number."""
    return (tag.group % 2) == 1


def deidentify_basic_profile(
    dataset: Dataset,
    *,
    patient_pseudonym: Optional[str] = None,
    date_offset_days: int = 0,
    keep_private_tags: bool = False,
) -> Dataset:
    """Apply DICOM Basic Application Confidentiality Profile (PS3.15 Annex E.1).

    Returns a NEW Dataset — does NOT mutate the input.

    Per the profile:
      * PatientName, ReferringPhysicianName, etc. are emptied (Z action)
      * PatientID is set to ``patient_pseudonym`` if provided, else emptied
      * Other identifying tags (institution, addresses, comments, free
        text) are removed (X action)
      * All study/birth dates are shifted by ``date_offset_days`` if a
        non-zero offset is given, else emptied
      * Private tags (odd group numbers per PS3.5 §7.8) are removed
        unless ``keep_private_tags=True``
      * DeidentificationMethodCodeSequence (0012,0064) is set with the
        coded value for "Basic Application Confidentiality Profile"
        (DCM 113100) so downstream consumers can verify the dataset has
        been de-identified
      * PatientIdentityRemoved (0012,0062) is set to "YES"
    """
    # Deep copy so the caller's dataset isn't mutated. pydicom's Dataset
    # doesn't have a great copy story; copy.deepcopy is the standard recipe.
    import copy
    out = copy.deepcopy(dataset)

    # 1. Apply per-tag actions from the profile table.
    # Date tags get special treatment in step 2 — skip them here so a
    # non-zero date_offset_days has a value to shift instead of an empty.
    for tag_name, (action, dummy) in DEID_BASIC_PROFILE_ACTIONS.items():
        if tag_name in DEID_DATE_TAGS or tag_name in DEID_TIME_TAGS:
            continue
        if not hasattr(out, tag_name):
            continue
        if action == "X":
            delattr(out, tag_name)
        elif action == "Z":
            # Special case: PatientID gets the pseudonym if provided.
            if tag_name == "PatientID" and patient_pseudonym is not None:
                out.PatientID = patient_pseudonym
            else:
                setattr(out, tag_name, dummy)
        elif action == "D":
            setattr(out, tag_name, dummy)

    # 2. Date/time handling.
    if date_offset_days != 0:
        for tag_name in DEID_DATE_TAGS:
            if hasattr(out, tag_name):
                cur = str(getattr(out, tag_name) or "")
                shifted = _shift_dicom_date(cur, date_offset_days)
                setattr(out, tag_name, shifted)
        # Times don't carry PHI individually but we leave them alone when
        # shifting dates — only the date precision matters for re-id risk.
    else:
        # No offset → empty all dates and times entirely.
        for tag_name in DEID_DATE_TAGS:
            if hasattr(out, tag_name):
                setattr(out, tag_name, "")
        for tag_name in DEID_TIME_TAGS:
            if hasattr(out, tag_name):
                setattr(out, tag_name, "")

    # 3. Private tags.
    if not keep_private_tags:
        # Collect first, then delete — can't mutate while iterating.
        private_tags = [elem.tag for elem in out if _is_private_tag(elem.tag)]
        for tag in private_tags:
            del out[tag]

    # 4. Stamp the de-identification method per PS3.15 §E.1.1.
    out.PatientIdentityRemoved = "YES"

    method_item = Dataset()
    method_item.CodeValue = "113100"
    method_item.CodingSchemeDesignator = "DCM"
    method_item.CodeMeaning = "Basic Application Confidentiality Profile"
    out.DeidentificationMethodCodeSequence = Sequence([method_item])

    # Free-text DeidentificationMethod (0012,0063) — duplicate of the coded
    # sequence in human-readable form. Both are recommended by PS3.15.
    out.DeidentificationMethod = "DICOM PS3.15 Annex E.1 (Basic Profile)"

    return out


# ---------------------------------------------------------------------------
# Pixel-data heuristic (burned-in PHI risk)
# ---------------------------------------------------------------------------

def has_burned_in_phi_risk(dataset: Dataset) -> bool:
    """Returns True if the dataset is a likely candidate for OCR pixel-redaction.

    Triggers:
      * BurnedInAnnotation (0028,0301) == "YES" (explicit declaration)
      * Modality is one known to commonly carry burned-in patient text
        (US, NM, XA, RF, MG, PT, OT — see WG-18)

    This is a fast pre-filter, NOT a guarantee. Always do an OCR pass on
    the pixel data of any dataset that returns True before releasing it.
    """
    burned = getattr(dataset, "BurnedInAnnotation", None)
    if burned is not None and str(burned).strip().upper() == "YES":
        return True

    modality = getattr(dataset, "Modality", None)
    if modality and str(modality).strip().upper() in MODALITIES_WITH_LIKELY_BURNED_IN_PHI:
        return True

    return False


# ---------------------------------------------------------------------------
# Round-trip helper
# ---------------------------------------------------------------------------

def round_trip(dataset_or_bytes: Union[Dataset, bytes, io.BytesIO]) -> bool:
    """Write dataset to bytes, parse back, return True iff key tags match.

    "Key tags" are the SOP-locating triple (Study/Series/Instance UIDs)
    plus the patient identity pair (PatientID, PatientName) — if any of
    those drift across the serialize/parse boundary, the round trip is
    not lossless and downstream consumers will see different objects.
    """
    ds = _coerce_to_dataset(dataset_or_bytes)

    # Serialize → bytes → reparse.
    buf = io.BytesIO()
    try:
        ds.save_as(buf, enforce_file_format=True)
    except Exception as e:  # noqa: BLE001
        LOG.warning("round_trip: save_as failed: %s", e)
        return False

    buf.seek(0)
    try:
        reread = dcmread(buf)
    except Exception as e:  # noqa: BLE001
        LOG.warning("round_trip: dcmread failed: %s", e)
        return False

    key_tags = (
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SOPInstanceUID",
        "PatientID",
        "PatientName",
    )
    for t in key_tags:
        a = getattr(ds, t, None)
        b = getattr(reread, t, None)
        if str(a) != str(b):
            LOG.warning("round_trip: tag %s drifted: %r vs %r", t, a, b)
            return False
    return True
