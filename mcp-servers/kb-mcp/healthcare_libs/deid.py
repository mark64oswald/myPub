"""healthcare_libs.deid — Production reference for HIPAA Safe Harbor de-id.

Provides format-agnostic primitives that the format-specific modules
(``healthcare_libs.fhir``, ``.dicom``, ``.hl7v2``) call into when
de-identifying protected health information (PHI). Generators emit code
that imports from this module rather than reinventing crypto, date math,
or audit trails per output.

What's here:

  * **Pseudonymization** — HMAC-SHA256 of (value, salt) to a stable
    opaque token. Same input + same salt → same token (joins survive);
    different salt → different token (releases are unlinkable).
  * **Date generalization + per-subject shifting** — deterministic
    ±N-day offsets seeded by ``(subject_id, seed)`` so all dates for a
    subject move together; format-tolerant for HL7v2 ``YYYYMMDD``,
    FHIR ``YYYY-MM-DD``, and ISO 8601 datetimes.
  * **Geographic truncation** — Safe Harbor §164.514(b)(2)(i)(B)
    requires dropping all but the first three ZIP digits, AND only
    when the resulting ZIP3 covers >20,000 people per the most recent
    Census. Caller passes the allowlist; the default is conservative
    (drop unless allowlisted).
  * **Suppression / k-anonymity** — basic suppress-or-mask k-anonymity
    over caller-named quasi-identifiers, plus a one-line ``suppress``.
  * **PHI pattern detection** — heuristic regexes used in tests and
    post-pipeline validation to assert that PHI-shaped strings are
    absent from de-id output.
  * **Append-only audit log** — JSONL writer that records every action
    with a SHA256 of the original value (so the original is never
    written, but verification of "did we touch this field?" is
    possible later).

Format modules consume this layer; they do NOT live here. This module
has no hard dependency on ``hl7apy``, ``fhir.resources``, or
``pydicom`` — keeping the primitive layer lightweight means the
generators can compose it with whatever message shape they emit.

References:
  * 45 CFR §164.514(b)(2) — Safe Harbor de-identification standard:
    https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.514
  * HHS de-identification guidance:
    https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/index.html
  * Sweeney, L. (2002). k-anonymity: a model for protecting privacy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("healthcare_libs.deid")


# ---------------------------------------------------------------------------
# HIPAA Safe Harbor — the 18 identifier categories per §164.514(b)(2)(i)
# ---------------------------------------------------------------------------

SAFE_HARBOR_CATEGORIES = [
    "names",                # (A) names
    "geo_subdivisions",     # (B) geographic subdivisions smaller than state
    "dates",                # (C) all elements of dates (except year) for ages <90
    "phone_fax",            # (D) phone + (E) fax — collapsed: both telecom
    "email",                # (F) email
    "ssn",                  # (G) SSN
    "mrn",                  # (H) medical record numbers
    "health_plan_id",       # (I) health plan beneficiary numbers
    "account",              # (J) account numbers
    "cert_license",         # (K) certificate / license numbers
    "vehicle_id",           # (L) vehicle identifiers + serials, plates
    "device_id",            # (M) device identifiers + serials
    "urls",                 # (N) URLs
    "ip",                   # (O) IP addresses
    "biometric",            # (P) biometric identifiers (fingerprints, voiceprints)
    "photos",               # (Q) full-face photos + comparable images
    "other_unique",         # (R) any other unique identifying number/code/characteristic
    "rare_traits",          # implementation-side: rare traits flagged for expert review
]


# ---------------------------------------------------------------------------
# Configuration + audit dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DeidConfig:
    """Configuration for a single de-identification run.

    Rotate ``pseudonym_salt`` per release to make pseudonyms unlinkable
    across releases; keep it stable within a release so joins on the
    same patient survive.

    ``date_offset_seed`` does the analogous job for date shifts: same
    seed + same subject_id → same offset, so all dates for a subject
    move together (preserving relative intervals).
    """

    pseudonym_salt: str
    date_offset_seed: str
    date_offset_max_days: int = 60
    keep_zip3_only: bool = True
    age_band_size: int = 5
    k_anonymity_threshold: int = 5


@dataclass
class DeidAuditEntry:
    """One record of a de-id action — for the audit trail.

    ``before_hash`` is a SHA256 of the original value, NOT the original
    value itself. This lets a reviewer verify "did this field get
    touched, and was the value what we expected?" (by re-hashing the
    expected value and comparing) without leaving PHI in the audit.
    """

    timestamp: str
    record_id: str
    action: str
    field_path: str
    before_hash: str
    note: str = ""


# ---------------------------------------------------------------------------
# Pseudonymization
# ---------------------------------------------------------------------------

def hmac_pseudonym(value: str, salt: str, *, length: int = 16) -> str:
    """Stable, opaque pseudonym for ``value`` keyed by ``salt``.

    Uses HMAC-SHA256 — the salt acts as a key, so an attacker who
    knows the algorithm but not the salt cannot pre-compute a rainbow
    table over candidate values (which a plain ``sha256(value)`` would
    allow).

    Same ``(value, salt)`` → same pseudonym (joins on the pseudonym
    survive within a release). Different ``salt`` → different
    pseudonym (cross-release linkage is broken on rotation).

    ``length`` is the number of hex chars in the output (default 16 →
    64 bits of identifier space, comfortable for typical patient
    cohorts up to billions).
    """
    if value is None:
        raise ValueError("hmac_pseudonym: value must not be None")
    if not isinstance(value, str):
        value = str(value)
    if not isinstance(salt, str) or not salt:
        raise ValueError("hmac_pseudonym: salt must be a non-empty string")
    if length < 4:
        raise ValueError(f"hmac_pseudonym: length must be ≥4 (got {length})")
    digest = hmac.new(
        key=salt.encode("utf-8"),
        msg=value.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return digest[:length]


def pseudonym_lookup_writer(directory: Path) -> Callable[[str, str], None]:
    """Return a function that appends ``(original, pseudonym)`` to a lookup file.

    The lookup file lives in ``directory/pseudonym_lookup.jsonl`` and
    is meant to be access-controlled SEPARATELY from the de-id output
    (different bucket, different IAM policy). This is the re-id
    bridge: holding both the de-id output AND the lookup file is the
    only path to re-identification, so they MUST NOT travel together.

    Returns a closure so the caller can pass it as a callback into a
    pipeline without re-opening the file per record.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lookup_path = directory / "pseudonym_lookup.jsonl"

    def _write(original: str, pseudonym: str) -> None:
        line = json.dumps({"original": original, "pseudonym": pseudonym})
        with open(lookup_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    return _write


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------

# Format detectors. Order matters — try longest/most-specific first so
# e.g. "2026-05-08T14:30:00Z" doesn't get matched as "2026-05-08".
_DATE_FORMATS: list[tuple[str, re.Pattern]] = [
    # ISO 8601 with timezone (Z or ±HH:MM)
    ("%Y-%m-%dT%H:%M:%S%z", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}$")),
    # ISO 8601 with Z
    ("%Y-%m-%dT%H:%M:%SZ", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")),
    # ISO 8601 without zone
    ("%Y-%m-%dT%H:%M:%S", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")),
    # FHIR / ISO date
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    # HL7 v2 / DICOM YYYYMMDD
    ("%Y%m%d", re.compile(r"^\d{8}$")),
    # Year-month
    ("%Y-%m", re.compile(r"^\d{4}-\d{2}$")),
    # Year only
    ("%Y", re.compile(r"^\d{4}$")),
]


def _detect_date_format(date_str: str) -> tuple[str, str]:
    """Return ``(strptime_fmt, original_input)`` or raise ``ValueError``.

    A small wrapper over the ``_DATE_FORMATS`` table so callers don't
    need to know the format zoo.
    """
    if not isinstance(date_str, str):
        raise ValueError(f"date_str must be str, got {type(date_str).__name__}")
    s = date_str.strip()
    for fmt, pat in _DATE_FORMATS:
        if pat.match(s):
            return fmt, s
    raise ValueError(f"unrecognized date format: {date_str!r}")


def _parse_date(date_str: str) -> tuple[datetime, str]:
    """Parse a date string and return ``(datetime, fmt_used)``."""
    fmt, s = _detect_date_format(date_str)
    # Python's %z accepts ±HHMM and ±HH:MM in 3.7+, but the colon form
    # is only accepted on some platforms — normalize defensively.
    if "%z" in fmt and ":" in s[-6:]:
        # Strip the colon in the offset for portable parsing
        s_norm = s[:-6] + s[-6:].replace(":", "")
        return datetime.strptime(s_norm, fmt), fmt
    return datetime.strptime(s, fmt), fmt


def per_subject_offset(subject_id: str, seed: str, max_days: int = 60) -> int:
    """Deterministic offset in ``[-max_days, +max_days]`` for a subject.

    Used to shift ALL dates for a single subject by the SAME number of
    days, so within-subject intervals (e.g., admission → discharge,
    diagnosis → treatment) survive de-id while absolute dates do not.

    The offset is keyed by HMAC-SHA256 of ``subject_id`` under
    ``seed`` so it's not guessable from the subject_id alone.
    """
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("per_subject_offset: subject_id must be non-empty str")
    if not isinstance(seed, str) or not seed:
        raise ValueError("per_subject_offset: seed must be non-empty str")
    if max_days < 1:
        raise ValueError(f"per_subject_offset: max_days must be ≥1 (got {max_days})")
    digest = hmac.new(
        key=seed.encode("utf-8"),
        msg=subject_id.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    # Use the first 8 bytes as an unsigned integer; map to [-max, +max]
    raw = int.from_bytes(digest[:8], "big", signed=False)
    span = 2 * max_days + 1
    return (raw % span) - max_days


def shift_date(date_str: str, days: int, *, fmt: str | None = None) -> str:
    """Shift a date string by ``days`` (signed). Preserves input format.

    Auto-detects HL7v2 ``YYYYMMDD``, FHIR ``YYYY-MM-DD``, and ISO 8601
    datetimes (with or without timezone). Pass ``fmt`` to force a
    specific strftime format if auto-detection isn't reliable for your
    inputs.
    """
    if fmt is None:
        dt, used_fmt = _parse_date(date_str)
    else:
        dt = datetime.strptime(date_str, fmt)
        used_fmt = fmt
    shifted = dt + timedelta(days=days)
    out = shifted.strftime(used_fmt)
    # Restore the colon in timezone if the original had one
    if "%z" in used_fmt and len(date_str) >= 6 and date_str[-3] == ":":
        # strftime emits ±HHMM; reinsert colon to match input
        if len(out) >= 5 and (out[-5] in "+-") and out[-3] != ":":
            out = out[:-2] + ":" + out[-2:]
    return out


def date_to_year_only(date_str: str) -> str:
    """Generalize any supported date format to its 4-digit year string."""
    dt, _ = _parse_date(date_str)
    return dt.strftime("%Y")


def date_to_age_band(
    birth_date: str,
    reference_date: str | None = None,
    band_size: int = 5,
    cap_at_90: bool = True,
) -> str:
    """Convert birth date → age band string.

    Per Safe Harbor §164.514(b)(2)(i)(C), ages > 89 must be aggregated
    into a single ``90+`` category (because individuals aged 90+ are
    disproportionately re-identifiable). With ``cap_at_90=False`` you
    get full bands at the high end — useful for synthetic data tests
    but never appropriate for real PHI release.

    ``band_size=5`` is the default; ``band_size=10`` is also common
    for low-cell-count studies. Bands are inclusive on both ends:
    ``35-39`` for ``band_size=5``.
    """
    if band_size < 1:
        raise ValueError(f"band_size must be ≥1 (got {band_size})")
    birth_dt, _ = _parse_date(birth_date)
    if reference_date is None:
        ref_dt = datetime.now()
    else:
        ref_dt, _ = _parse_date(reference_date)
    age = ref_dt.year - birth_dt.year - (
        (ref_dt.month, ref_dt.day) < (birth_dt.month, birth_dt.day)
    )
    if age < 0:
        raise ValueError(
            f"negative age computed: birth {birth_date} after reference {reference_date}"
        )
    if cap_at_90 and age >= 90:
        return "90+"
    band_start = (age // band_size) * band_size
    band_end = band_start + band_size - 1
    return f"{band_start}-{band_end}"


# ---------------------------------------------------------------------------
# Geographic
# ---------------------------------------------------------------------------

def truncate_zip(
    zip_code: str,
    *,
    allowed_zip3: set[str] | None = None,
) -> str | None:
    """Per Safe Harbor: keep first 3 ZIP digits if covered population >20,000.

    The Safe Harbor rule §164.514(b)(2)(i)(B) requires dropping all
    geographic subdivisions smaller than the state EXCEPT the first
    three ZIP digits, AND only when the geographic unit formed by all
    ZIP codes with the same three initial digits contains more than
    20,000 people per the most recent Census.

    The current HHS-published list of restricted ZIP3s (sparse rural
    areas + a couple US territories) changes between Census releases.
    Rather than embedding it (and going stale), we accept an
    ``allowed_zip3`` set; the default is to return None when no
    allowlist is provided (conservative — better to drop than to leak).

    Returns the ZIP3 string if allowed, else None. ZIP+4 is stripped
    before evaluation.
    """
    if zip_code is None:
        return None
    if not isinstance(zip_code, str):
        zip_code = str(zip_code)
    # Strip ZIP+4 suffix and any non-digits
    zip5 = re.sub(r"[^0-9]", "", zip_code)[:5]
    if len(zip5) < 3:
        return None
    zip3 = zip5[:3]
    if allowed_zip3 is None:
        # Conservative default: no allowlist → drop everything
        return None
    if zip3 in allowed_zip3:
        return zip3
    return None


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def suppress(value: Any) -> None:
    """The 'suppress' technique. Always returns None — by design.

    Useful as a callback in transformer pipelines (``transform=suppress``)
    so the suppression intent is grep-able rather than hidden behind
    ``lambda _: None``.
    """
    del value  # explicitly discard — that IS the technique
    return None


# ---------------------------------------------------------------------------
# K-anonymity
# ---------------------------------------------------------------------------

def k_anonymize(
    records: list[dict],
    quasi_identifiers: list[str],
    k: int = 5,
) -> list[dict]:
    """Mask quasi-identifier values so every QI combination appears ≥ k times.

    Implements a simple suppress-or-mask approach (a one-step Mondrian
    degeneracy):

      1. Compute the multiset of QI tuples across ``records``.
      2. For each record whose QI tuple appears < k times, replace
         each QI value in that record with the string ``'*'``.
      3. Other fields are left untouched.

    Returns a NEW list of dicts (deep copy). Does not mutate input.

    For real-world releases prefer a proper Mondrian or Datafly
    implementation (generalize before suppressing). This primitive is
    appropriate for tests, small cohorts, and as a final guardrail
    after format-specific generalization has run.
    """
    if k < 1:
        raise ValueError(f"k must be ≥1 (got {k})")
    if not quasi_identifiers:
        # No QIs to enforce on → nothing to do
        return deepcopy(records)

    out = deepcopy(records)
    # Build the QI tuple per record (None for missing keys → distinct from "")
    qi_tuples = [
        tuple(r.get(qi, None) for qi in quasi_identifiers) for r in out
    ]
    counts = Counter(qi_tuples)
    for rec, qit in zip(out, qi_tuples):
        if counts[qit] < k:
            for qi in quasi_identifiers:
                if qi in rec:
                    rec[qi] = "*"
                else:
                    # Add the masked value so the record's QI shape is
                    # uniform across the output (downstream code can
                    # rely on the column being present).
                    rec[qi] = "*"
    return out


# ---------------------------------------------------------------------------
# PHI pattern detection (heuristic, for post-pipeline validation)
# ---------------------------------------------------------------------------

# Anchored where reasonable, generous where format varies in the wild.
# These are HEURISTICS — they catch the obvious shapes and serve as
# tripwires in test scaffolds. They are not a substitute for proper
# de-id design.
_PHI_PATTERNS: list[tuple[str, re.Pattern]] = [
    # SSN — 9 digits with optional dashes: 123-45-6789 or 123456789
    ("ssn", re.compile(r"\b(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}\b")),
    # US phone — many separators: 555-555-5555, (555) 555-5555, 555.555.5555, +1 555 555 5555
    ("phone", re.compile(r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}\b")),
    # Email — standard, conservative
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Full ISO date YYYY-MM-DD (Safe Harbor forbids day+month for ages <90)
    ("iso_date", re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")),
    # MRN-like: a long run of digits (12+) — catches health-plan IDs and account numbers too
    ("mrn_like", re.compile(r"\b\d{12,}\b")),
]


def find_phi_patterns(text: str) -> list[tuple[str, str]]:
    """Return ``(pattern_name, matched_text)`` for PHI-shaped strings.

    Used in tests as ``assert not find_phi_patterns(rendered)``. False
    positives ARE possible (e.g., a legitimate accession number
    happens to have 12 digits) — treat findings as something to
    examine, not as proof of leak.

    Patterns covered: ``ssn``, ``phone``, ``email``, ``iso_date``,
    ``mrn_like``.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        text = str(text)
    found: list[tuple[str, str]] = []
    for name, pat in _PHI_PATTERNS:
        for m in pat.finditer(text):
            found.append((name, m.group(0)))
    return found


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only JSONL audit log for one de-id run.

    Each ``record(...)`` call appends one line. ``close()`` flushes +
    closes the file handle (calling ``record`` after ``close`` raises).
    ``summary()`` returns counts; it can be called both before and
    after ``close()`` since it tracks counts in memory.

    Concurrency: NOT thread-safe by design. Wrap in your own lock if
    multiple workers share a log. Per-worker logs (one file per worker)
    + a downstream merge is the recommended pattern.

    Why hash the original value rather than store it? Because the
    audit log itself is then PHI-free and can live alongside the
    de-id output in the same access tier. Verification ("did we
    pseudonymize the right field?") is still possible — re-hash the
    expected value and compare.
    """

    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.output_path, "a", encoding="utf-8")
        self._closed = False
        self._action_counts: Counter = Counter()
        self._records_touched: set[str] = set()
        self._total: int = 0

    def record(
        self,
        record_id: str,
        action: str,
        field_path: str,
        before_value: Any,
        note: str = "",
    ) -> None:
        """Append a single audit entry. Hash the before-value, never store it."""
        if self._closed:
            raise RuntimeError("AuditLog is closed; cannot record further entries")
        if before_value is None:
            before_str = ""
        elif isinstance(before_value, str):
            before_str = before_value
        else:
            before_str = str(before_value)
        before_hash = hashlib.sha256(before_str.encode("utf-8")).hexdigest()
        entry = DeidAuditEntry(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            record_id=record_id,
            action=action,
            field_path=field_path,
            before_hash=before_hash,
            note=note,
        )
        self._file.write(json.dumps(asdict(entry)) + "\n")
        # Counters
        self._action_counts[action] += 1
        self._records_touched.add(record_id)
        self._total += 1

    def close(self) -> None:
        """Flush and close the underlying file. Idempotent."""
        if self._closed:
            return
        self._file.flush()
        self._file.close()
        self._closed = True

    def summary(self) -> dict:
        """Return summary stats for this run."""
        return {
            "total_actions": self._total,
            "by_action": dict(self._action_counts),
            "records_touched": len(self._records_touched),
        }

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
