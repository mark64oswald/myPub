"""Tests for healthcare_libs.deid — production HIPAA Safe Harbor reference."""
from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

from healthcare_libs import deid  # noqa: E402


# ---- hmac_pseudonym -----------------------------------------------------

def test_hmac_pseudonym_same_input_same_output():
    """Determinism: identical (value, salt) → identical pseudonym."""
    salt = secrets.token_hex(16)
    a = deid.hmac_pseudonym("PATIENT-001", salt)
    b = deid.hmac_pseudonym("PATIENT-001", salt)
    assert a == b


def test_hmac_pseudonym_different_salt_different_output():
    """Salt rotation breaks linkability: same value, different salt → different pseudonym."""
    s1 = "release-2026-Q1"
    s2 = "release-2026-Q2"
    a = deid.hmac_pseudonym("PATIENT-001", s1)
    b = deid.hmac_pseudonym("PATIENT-001", s2)
    assert a != b


def test_hmac_pseudonym_no_collisions_on_small_set():
    """50 unique inputs → 50 unique pseudonyms (collision-resistance smoke check)."""
    salt = secrets.token_hex(16)
    pseudonyms = {deid.hmac_pseudonym(f"PATIENT-{i:04d}", salt) for i in range(50)}
    assert len(pseudonyms) == 50


def test_hmac_pseudonym_length_arg_respected():
    """`length` controls the output character count exactly."""
    salt = "salty"
    for n in (8, 16, 24, 32, 64):
        out = deid.hmac_pseudonym("X", salt, length=n)
        assert len(out) == n, f"expected length {n}, got {len(out)}"


def test_hmac_pseudonym_rejects_empty_salt():
    with pytest.raises(ValueError, match="non-empty"):
        deid.hmac_pseudonym("v", "")


def test_hmac_pseudonym_coerces_non_str_value():
    """Non-string values are stringified — common case is integer IDs."""
    salt = "s"
    a = deid.hmac_pseudonym(42, salt)
    b = deid.hmac_pseudonym("42", salt)
    assert a == b


# ---- per_subject_offset --------------------------------------------------

def test_per_subject_offset_deterministic():
    """Same (subject, seed) → same offset every call."""
    a = deid.per_subject_offset("subj-1", "seed-x", 60)
    b = deid.per_subject_offset("subj-1", "seed-x", 60)
    assert a == b


def test_per_subject_offset_different_subjects_differ():
    """Distinct subjects should (with high probability) get distinct offsets."""
    seed = "fixed-seed"
    offsets = {deid.per_subject_offset(f"subj-{i}", seed, 60) for i in range(50)}
    # With 50 subjects in a 121-value range, expect well over 30 unique values
    assert len(offsets) > 30, f"too many collisions: only {len(offsets)} unique"


def test_per_subject_offset_within_range():
    """Output must lie in [-max_days, +max_days] for every input."""
    seed = "s"
    for i in range(200):
        o = deid.per_subject_offset(f"subj-{i}", seed, 60)
        assert -60 <= o <= 60, f"offset {o} out of range for subj-{i}"


def test_per_subject_offset_seed_changes_offset():
    """Different seeds → different offsets for same subject (release rotation)."""
    a = deid.per_subject_offset("subj", "seed-A", 60)
    b = deid.per_subject_offset("subj", "seed-B", 60)
    # Not strictly required to differ, but with 121 values and HMAC the
    # probability of accidental match is ~0.8% — accept that and only
    # fail if EVERY one of several seed pairs collides
    pairs = [
        ("subj", "seed-A", "seed-B"),
        ("subj", "seed-C", "seed-D"),
        ("subj", "seed-E", "seed-F"),
    ]
    differences = sum(
        deid.per_subject_offset(s, x, 60) != deid.per_subject_offset(s, y, 60)
        for s, x, y in pairs
    )
    assert differences >= 2, "seeds should change offset for at least 2 of 3 pairs"


# ---- shift_date ----------------------------------------------------------

def test_shift_date_yyyymmdd():
    """HL7v2 / DICOM YYYYMMDD format."""
    out = deid.shift_date("19850615", 30)
    assert out == "19850715"


def test_shift_date_iso_date():
    """FHIR YYYY-MM-DD format."""
    out = deid.shift_date("1985-06-15", 30)
    assert out == "1985-07-15"


def test_shift_date_iso_datetime_z():
    """ISO 8601 with Z timezone."""
    out = deid.shift_date("2026-05-08T14:30:00Z", 1)
    assert out == "2026-05-09T14:30:00Z"


def test_shift_date_iso_datetime_no_zone():
    """ISO 8601 without timezone."""
    out = deid.shift_date("2026-05-08T14:30:00", 7)
    assert out == "2026-05-15T14:30:00"


def test_shift_date_preserves_format():
    """Output format always matches input format."""
    pairs = [
        ("19850615", "%Y%m%d"),
        ("1985-06-15", "%Y-%m-%d"),
        ("2026-05-08T14:30:00Z", "%Y-%m-%dT%H:%M:%SZ"),
    ]
    for input_str, expected_fmt in pairs:
        shifted = deid.shift_date(input_str, 5)
        # Round-tripping back through the same fmt should succeed
        datetime.strptime(shifted, expected_fmt)


def test_shift_date_negative_offset():
    """Negative days shifts the date into the past."""
    out = deid.shift_date("20260115", -45)
    assert out == "20251201"


def test_shift_date_unrecognized_format_raises():
    with pytest.raises(ValueError, match="unrecognized date format"):
        deid.shift_date("Jan 15, 2026", 5)


# ---- date_to_year_only ---------------------------------------------------

def test_date_to_year_only_yyyymmdd():
    assert deid.date_to_year_only("19850615") == "1985"


def test_date_to_year_only_iso():
    assert deid.date_to_year_only("1985-06-15") == "1985"


def test_date_to_year_only_iso_datetime():
    assert deid.date_to_year_only("2026-05-08T14:30:00Z") == "2026"


# ---- date_to_age_band ----------------------------------------------------

def test_date_to_age_band_known_age():
    """1985-06-15 vs 2026-05-09 → age 40 → band 40-44 (band_size=5)."""
    band = deid.date_to_age_band("19850615", reference_date="20260509", band_size=5)
    assert band == "40-44"


def test_date_to_age_band_caps_at_90_plus():
    """Per Safe Harbor (b)(2)(i)(C): ages > 89 collapse to '90+'."""
    # birth 1930 vs reference 2026 → age 95 → '90+'
    band = deid.date_to_age_band("19300101", reference_date="20260101", cap_at_90=True)
    assert band == "90+"


def test_date_to_age_band_no_cap_when_disabled():
    """With cap_at_90=False, 95 → '95-99'."""
    band = deid.date_to_age_band(
        "19300101", reference_date="20260101", band_size=5, cap_at_90=False
    )
    assert band == "95-99"


def test_date_to_age_band_band_size_flows():
    """band_size=10 produces decadal bands."""
    band = deid.date_to_age_band("19850615", reference_date="20260509", band_size=10)
    assert band == "40-49"


def test_date_to_age_band_exact_birthday_handling():
    """Birthday hasn't happened yet → age is one less."""
    # Born June 15, reference May 9 of same year offset
    band_before_bday = deid.date_to_age_band(
        "19860615", reference_date="20260509", band_size=5
    )
    # 2026 - 1986 = 40, but birthday hasn't happened, so age = 39 → 35-39
    assert band_before_bday == "35-39"


# ---- truncate_zip --------------------------------------------------------

def test_truncate_zip_keeps_first_three_when_allowed():
    """When ZIP3 is in the allowlist, keep the first 3 digits."""
    allowed = {"902", "100"}
    assert deid.truncate_zip("90210", allowed_zip3=allowed) == "902"
    assert deid.truncate_zip("10001", allowed_zip3=allowed) == "100"


def test_truncate_zip_drops_when_not_allowed():
    """ZIP3s not on the allowlist must be dropped."""
    allowed = {"902"}
    assert deid.truncate_zip("12345", allowed_zip3=allowed) is None


def test_truncate_zip_no_allowlist_drops_all():
    """Conservative default — no allowlist provided → return None."""
    assert deid.truncate_zip("90210") is None


def test_truncate_zip_handles_zip_plus_4():
    """ZIP+4 (90210-1234) is stripped before evaluation."""
    allowed = {"902"}
    assert deid.truncate_zip("90210-1234", allowed_zip3=allowed) == "902"


def test_truncate_zip_handles_short_input():
    """Less than 3 digits → return None."""
    assert deid.truncate_zip("12", allowed_zip3={"123"}) is None


# ---- suppress ------------------------------------------------------------

def test_suppress_always_returns_none():
    """The suppress technique returns None for any input — by design."""
    for v in ("abc", 123, None, [], {}, 0.0, True):
        assert deid.suppress(v) is None


# ---- k_anonymize ---------------------------------------------------------

def test_k_anonymize_keeps_groups_at_or_above_k():
    """QI tuples with count ≥ k are unchanged."""
    records = [
        {"age": 40, "zip": "902", "dx": "I10"},
        {"age": 40, "zip": "902", "dx": "E11"},
        {"age": 40, "zip": "902", "dx": "Z00"},
    ]
    out = deid.k_anonymize(records, ["age", "zip"], k=3)
    assert out[0]["age"] == 40
    assert out[0]["zip"] == "902"
    assert out[1]["age"] == 40
    assert out[2]["zip"] == "902"


def test_k_anonymize_masks_groups_below_k():
    """QI tuples with count < k get '*' on every QI value."""
    records = [
        {"age": 40, "zip": "902", "dx": "I10"},
        {"age": 40, "zip": "902", "dx": "E11"},
        {"age": 99, "zip": "999", "dx": "Z00"},  # singleton
    ]
    out = deid.k_anonymize(records, ["age", "zip"], k=2)
    # Group of 2 (40, 902) survives
    assert out[0]["age"] == 40
    assert out[1]["zip"] == "902"
    # Singleton gets masked
    assert out[2]["age"] == "*"
    assert out[2]["zip"] == "*"
    # Non-QI fields are untouched
    assert out[2]["dx"] == "Z00"


def test_k_anonymize_does_not_mutate_input():
    """Input list AND its dicts are unchanged."""
    records = [
        {"age": 99, "zip": "999"},
    ]
    snapshot = [dict(r) for r in records]
    _ = deid.k_anonymize(records, ["age", "zip"], k=5)
    assert records == snapshot
    assert records[0]["age"] == 99
    assert records[0]["zip"] == "999"


def test_k_anonymize_no_qi_returns_copy_unchanged():
    """Empty QI list → no work to do, returns a copy."""
    records = [{"a": 1}, {"a": 2}]
    out = deid.k_anonymize(records, [], k=5)
    assert out == records
    assert out is not records


# ---- find_phi_patterns ---------------------------------------------------

def test_find_phi_patterns_detects_ssn():
    found = deid.find_phi_patterns("SSN: 123-45-6789 on file")
    names = [n for n, _ in found]
    assert "ssn" in names


def test_find_phi_patterns_detects_phone_with_separators():
    """Phone numbers with various separators are detected."""
    samples = [
        "Call 555-555-5555",
        "Call (555) 555-5555",
        "Call 555.555.5555",
        "Call +1 555 555 5555",
    ]
    for s in samples:
        found = deid.find_phi_patterns(s)
        names = [n for n, _ in found]
        assert "phone" in names, f"no phone detected in {s!r}: {found}"


def test_find_phi_patterns_detects_email():
    found = deid.find_phi_patterns("Contact jane.doe@example.com for details")
    names = [n for n, _ in found]
    assert "email" in names


def test_find_phi_patterns_detects_iso_date():
    found = deid.find_phi_patterns("Date of admission: 2026-05-08")
    names = [n for n, _ in found]
    assert "iso_date" in names


def test_find_phi_patterns_clean_text_returns_empty():
    """Text with no PHI shapes → empty list."""
    out = deid.find_phi_patterns("This sentence contains no patient identifiers.")
    assert out == []


def test_find_phi_patterns_handles_none_and_non_str():
    assert deid.find_phi_patterns(None) == []
    # Non-string input is coerced to str
    out = deid.find_phi_patterns(["call", "555-555-5555"])
    assert any(n == "phone" for n, _ in out)


# ---- AuditLog ------------------------------------------------------------

def test_audit_log_record_appends_and_close_flushes(tmp_path: Path):
    """record() appends one line per call; close() flushes."""
    log_path = tmp_path / "audit.jsonl"
    log = deid.AuditLog(log_path)
    log.record("REC-1", "pseudonymize", "Patient.id", "MRN-12345")
    log.record("REC-1", "shift_date", "Patient.birthDate", "19850615")
    log.record("REC-2", "suppress", "Patient.name", "JANE DOE")
    log.close()

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_audit_log_summary_counts_by_action(tmp_path: Path):
    """summary() reports counts and unique records correctly."""
    log = deid.AuditLog(tmp_path / "audit.jsonl")
    log.record("REC-1", "pseudonymize", "id", "MRN-1")
    log.record("REC-1", "pseudonymize", "id2", "MRN-2")
    log.record("REC-1", "shift_date", "dob", "19850615")
    log.record("REC-2", "suppress", "name", "JANE")
    log.record("REC-3", "pseudonymize", "id", "MRN-9")
    summary = log.summary()
    log.close()

    assert summary["total_actions"] == 5
    assert summary["records_touched"] == 3
    assert summary["by_action"]["pseudonymize"] == 3
    assert summary["by_action"]["shift_date"] == 1
    assert summary["by_action"]["suppress"] == 1


def test_audit_log_jsonl_is_valid(tmp_path: Path):
    """Every line of the audit log parses as JSON with the expected fields."""
    log = deid.AuditLog(tmp_path / "audit.jsonl")
    log.record("REC-1", "pseudonymize", "Patient.id", "MRN-12345", note="rotated salt")
    log.record("REC-2", "shift_date", "Patient.birthDate", "19850615")
    log.close()

    for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines():
        obj = json.loads(line)
        assert "timestamp" in obj
        assert "record_id" in obj
        assert "action" in obj
        assert "field_path" in obj
        assert "before_hash" in obj
        # before_hash is SHA256 hex (64 chars)
        assert len(obj["before_hash"]) == 64
        # The original value MUST NOT appear
        assert "MRN-12345" not in line
        assert "19850615" not in line


def test_audit_log_record_after_close_raises(tmp_path: Path):
    log = deid.AuditLog(tmp_path / "a.jsonl")
    log.record("R", "act", "f", "v")
    log.close()
    with pytest.raises(RuntimeError, match="closed"):
        log.record("R", "act", "f", "v2")


def test_audit_log_close_is_idempotent(tmp_path: Path):
    log = deid.AuditLog(tmp_path / "a.jsonl")
    log.close()
    log.close()  # should not raise


def test_audit_log_context_manager(tmp_path: Path):
    """Context-manager usage closes the log automatically."""
    p = tmp_path / "a.jsonl"
    with deid.AuditLog(p) as log:
        log.record("R-1", "pseudonymize", "id", "MRN-1")
    # After the with-block, further record() must fail
    with pytest.raises(RuntimeError):
        log.record("R-2", "pseudonymize", "id", "MRN-2")


# ---- pseudonym_lookup_writer --------------------------------------------

def test_pseudonym_lookup_writer_appends(tmp_path: Path):
    """The returned function appends one JSON line per call."""
    writer = deid.pseudonym_lookup_writer(tmp_path)
    writer("PATIENT-001", "abc123")
    writer("PATIENT-002", "def456")
    lookup = (tmp_path / "pseudonym_lookup.jsonl").read_text().strip().splitlines()
    assert len(lookup) == 2
    assert json.loads(lookup[0]) == {"original": "PATIENT-001", "pseudonym": "abc123"}
    assert json.loads(lookup[1]) == {"original": "PATIENT-002", "pseudonym": "def456"}


# ---- SAFE_HARBOR_CATEGORIES ---------------------------------------------

def test_safe_harbor_categories_has_18_entries():
    """Per §164.514(b)(2)(i)(A)–(R) — 18 identifier categories."""
    assert len(deid.SAFE_HARBOR_CATEGORIES) == 18


def test_safe_harbor_categories_unique():
    """No duplicate category names."""
    assert len(set(deid.SAFE_HARBOR_CATEGORIES)) == 18


# ---- DeidConfig defaults -------------------------------------------------

def test_deid_config_defaults():
    cfg = deid.DeidConfig(pseudonym_salt="s", date_offset_seed="d")
    assert cfg.date_offset_max_days == 60
    assert cfg.keep_zip3_only is True
    assert cfg.age_band_size == 5
    assert cfg.k_anonymity_threshold == 5


# ---- End-to-end: synthetic record → de-id → no PHI patterns remain ------

def test_end_to_end_synthetic_record_has_no_phi_after_deid(tmp_path: Path):
    """Apply pseudonymize + date_shift + suppress to a record;
    stringified output must have no PHI patterns left."""
    cfg = deid.DeidConfig(
        pseudonym_salt=secrets.token_hex(16),
        date_offset_seed=secrets.token_hex(16),
    )
    raw = {
        "patient_id": "MRN-123456789012",
        "ssn": "123-45-6789",
        "name": "JANE DOE",
        "email": "jane.doe@example.com",
        "phone": "555-555-5555",
        "dob": "1985-06-15",
        "encounter_date": "2026-05-08",
        "zip": "90210",
    }

    offset = deid.per_subject_offset(raw["patient_id"], cfg.date_offset_seed,
                                       cfg.date_offset_max_days)
    deid_record = {
        # pseudonymize the linkable identifier
        "patient_id": deid.hmac_pseudonym(raw["patient_id"], cfg.pseudonym_salt),
        # suppress direct identifiers
        "ssn": deid.suppress(raw["ssn"]),
        "name": deid.suppress(raw["name"]),
        "email": deid.suppress(raw["email"]),
        "phone": deid.suppress(raw["phone"]),
        # generalize dates: birth → year only; encounter → shifted then YYYYMMDD
        "dob_year": deid.date_to_year_only(raw["dob"]),
        "encounter_date_shifted_yyyymmdd": deid.shift_date(
            raw["encounter_date"].replace("-", ""),
            offset,
        ),
        # truncate ZIP — no allowlist supplied → drop
        "zip3": deid.truncate_zip(raw["zip"]),
    }

    rendered = json.dumps(deid_record, sort_keys=True, default=str)
    findings = deid.find_phi_patterns(rendered)
    # The pseudonym is 16 hex chars; no PHI shapes should match
    assert findings == [], f"PHI patterns remained after de-id: {findings}"
    # Sanity: original PHI is NOT present
    assert "MRN-123456789012" not in rendered
    assert "123-45-6789" not in rendered
    assert "JANE DOE" not in rendered
    assert "jane.doe@example.com" not in rendered
    assert "1985-06-15" not in rendered
    assert "90210" not in rendered


def test_end_to_end_audit_log_records_actions(tmp_path: Path):
    """Driving an audit log through a small de-id pipeline yields
    a complete, parseable trail with correct counts."""
    cfg = deid.DeidConfig(
        pseudonym_salt=secrets.token_hex(16),
        date_offset_seed=secrets.token_hex(16),
    )
    raw_records = [
        {"id": "MRN-1", "ssn": "111-22-3333", "dob": "19850615"},
        {"id": "MRN-2", "ssn": "444-55-6666", "dob": "19900101"},
    ]
    audit_path = tmp_path / "deid_audit.jsonl"
    with deid.AuditLog(audit_path) as log:
        for rec in raw_records:
            offset = deid.per_subject_offset(rec["id"], cfg.date_offset_seed,
                                               cfg.date_offset_max_days)
            _ = deid.hmac_pseudonym(rec["id"], cfg.pseudonym_salt)
            log.record(rec["id"], "pseudonymize", "id", rec["id"])
            _ = deid.suppress(rec["ssn"])
            log.record(rec["id"], "suppress", "ssn", rec["ssn"])
            _ = deid.shift_date(rec["dob"], offset)
            log.record(rec["id"], "shift_date", "dob", rec["dob"],
                        note=f"offset={offset}")
        summary = log.summary()

    assert summary["total_actions"] == 6  # 3 actions × 2 records
    assert summary["records_touched"] == 2
    assert summary["by_action"] == {
        "pseudonymize": 2, "suppress": 2, "shift_date": 2,
    }
    # Original PHI must not appear in the audit
    audit_text = audit_path.read_text()
    for r in raw_records:
        assert r["ssn"] not in audit_text
        assert r["dob"] not in audit_text
