"""
ranking.py — Phase 4.5 ranking engine.

Five-factor scoring (§8.5) + two-mode selection (§8.1, §8.2) for the myPub
v2 retrieval pipeline. The kb-mcp server's search_chapters tool calls into
``InteractiveRanker`` to produce {primary, corroborations, conflicts}; the
Skills Factory (Phase 5) calls ``GenerationRanker`` with one of three
selection strategies.

The five factors combine linearly: ::

    score(p, q) = w_rec  × recency(p)
                + w_doc  × doc_alignment(p)
                + w_rel  × relevance(p, q)
                + w_corr × corroboration(p)
                + w_auth × authority(p)

Weights ``w_*`` are query-/Skill-type specific and live in ``WEIGHT_PROFILES``
matching arch §8.5 starting points; tuning happens in Phase 4.6 with the
retrieval eval set.

Scope notes
-----------
* ``doc_alignment`` for doc_sections is computed from outbound alignment
  edges this section emits (CORROBORATES/CONTRADICTS pointing at chapters).
  Cross-source doc-vs-doc alignment isn't wired yet (Phase 4.4b only writes
  Doc → Chapter edges); a doc_section with no outbound edges defaults to a
  neutral 0.5 — they ARE the current source, but we lack the signal to say
  so positively until cross-source alignment lands.
* ``authority`` for chapters is publisher-keyed via ``PUBLISHER_AUTHORITY``
  (~14 known tech imprints; unknown / missing publisher falls back to
  ``DEFAULT_AUTHORITY_BOOK = 0.6``). ``authority`` for doc_sections flows
  from ``doc_source.authority_score`` directly. Empirical tuning of the
  publisher table is tracked as a Phase 4.6 follow-up.
* ``recency`` uses an exponential half-life of 2 years. Books published
  longer ago decay; doc snapshots retrieved within the past day are ~1.0.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import duckdb


DEFAULT_HALF_LIFE_DAYS = 365 * 2          # ~2-year half-life for recency decay
RECENCY_FLOOR = 0.20                      # very old material still has non-zero recency
DEFAULT_AUTHORITY_BOOK = 0.6              # fallback when book.publisher isn't a known anchor
DEFAULT_NEUTRAL_FACTOR = 0.5              # used when a factor is genuinely unknown
CORROBORATION_SATURATION = 3              # ~1.0 once 3+ corroborators exist


# Publisher → authority mapping. Tech-publisher reputation is reasonably well-
# anchored: established imprints with strong editorial review (O'Reilly, Manning,
# Pragmatic, Addison-Wesley/Pearson, MIT Press) get higher scores than fast-cycle
# publishers (Packt) or self-published material. Numbers here are starting points;
# Phase 4.6 eval can drive empirical tuning.
PUBLISHER_AUTHORITY: dict[str, float] = {
    "o'reilly":                    0.85,
    "o'reilly media":              0.85,
    "manning":                     0.85,
    "manning publications":        0.85,
    "pragmatic bookshelf":         0.80,
    "pragmatic programmers":       0.80,
    "addison-wesley":              0.80,
    "addison-wesley professional": 0.80,
    "pearson":                     0.78,
    "pearson education":           0.78,
    "morgan kaufmann":             0.80,
    "mit press":                   0.85,
    "no starch press":             0.78,
    "apress":                      0.72,
    "wiley":                       0.72,
    "john wiley & sons":           0.72,
    "wiley & sons":                0.72,
    "wrox":                        0.68,
    "packt":                       0.65,
    "elsevier":                    0.78,
    "ft press":                    0.70,
    "morgan kaufmann publishers":  0.80,
    "academic press":              0.78,
    "mcgraw-hill":                 0.78,
    "mcgraw hill":                 0.78,
    "mcgraw-hill education":       0.78,
    "mcgraw-hill companies":       0.78,
    "crc press":                   0.75,
    "taylor & francis":            0.75,
    "taylor & francis group":      0.75,
    "sas institute":               0.70,
    "sas press":                   0.70,
    "jossey-bass":                 0.72,
    "pearson education india":     0.74,
    "pearson india education services": 0.74,
}


# Corporate-form suffixes — always safe to strip (no signal, no semantic
# overlap with legitimate publisher names).
_CORP_SUFFIX_RE = re.compile(
    r"[,\s]*\b("
    r"inc(?:orporated)?|co|company|"
    r"ltd|limited|llc|llp|"
    r"pvt(?:\.?\s*ltd\.?)?|private\s+limited|"
    r"plc|gmbh|sa|s\.a\.|s\.a\.r\.l\."
    r")\.?\b\.?",
    re.IGNORECASE,
)
# Publisher-type words — strip ONLY if a corp-stripped form didn't match.
# Reason: "no starch press" is itself a table key (0.78), so we mustn't
# strip "Press" before lookup or "No Starch Press, Inc." would misroute.
_PUBTYPE_SUFFIX_RE = re.compile(
    r"[,\s]*\b(publishing|publishers?|publications|press|books|media)\b\.?",
    re.IGNORECASE,
)


def _normalize_basic(publisher: str) -> str:
    """Lowercase, normalize apostrophes, strip leading article."""
    norm = publisher.strip().lower()
    norm = norm.translate(str.maketrans({"’": "'", "‘": "'",
                                          "“": '"', "”": '"'}))
    if norm.startswith("the "):
        norm = norm[4:]
    return norm


def _strip_iter(norm: str, pattern: "re.Pattern[str]") -> str:
    """Apply suffix-stripping regex repeatedly until stable."""
    prev = None
    while prev != norm:
        prev = norm
        norm = pattern.sub("", norm).strip(" ,.")
    return norm


def authority_score_from_publisher(publisher: Optional[str]) -> float:
    """Map a free-form publisher string to an authority value.

    Tiered matching, returning the most specific hit:

      1. Match the case-/apostrophe-normalized name directly.
      2. Strip corporate suffixes ("Inc.", "Co.", "Pvt Ltd", "LLC") and retry.
         This catches "O'Reilly Media, Inc." → "o'reilly media".
      3. Strip publisher-type words ("Publishing", "Press", "Media") and
         retry. This catches "Packt Publishing Pvt Ltd" → "packt".

    Tiered (vs single-pass) so that "No Starch Press, Inc." matches
    ``no starch press`` instead of being over-stripped to "no starch".

    Why robust matching matters: the catalog ships with values like
    "O'Reilly Media, Inc." and "Manning Publications Co." that exact-match
    silently misses, leaving ~370 high-authority books at the default 0.6.

    Suffix stripping does not introduce false positives like "Wiley-VCH"
    matching "Wiley" — only trailing corporate-form words are removed.
    """
    if not publisher:
        return DEFAULT_AUTHORITY_BOOK
    norm = _normalize_basic(publisher)
    if not norm:
        return DEFAULT_AUTHORITY_BOOK

    # Tier 1: direct match.
    if norm in PUBLISHER_AUTHORITY:
        return PUBLISHER_AUTHORITY[norm]
    # Tier 2: strip corporate suffix.
    norm_corp = _strip_iter(norm, _CORP_SUFFIX_RE)
    if norm_corp and norm_corp in PUBLISHER_AUTHORITY:
        return PUBLISHER_AUTHORITY[norm_corp]
    # Tier 3: also strip publisher-type words.
    norm_full = _strip_iter(norm_corp, _PUBTYPE_SUFFIX_RE)
    if norm_full and norm_full in PUBLISHER_AUTHORITY:
        return PUBLISHER_AUTHORITY[norm_full]

    return DEFAULT_AUTHORITY_BOOK


# ---------------------------------------------------------------------------
# Weights + profiles (§8.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Weights:
    """Per-factor weights for the linear scoring formula.

    Constructor enforces the weights sum to ~1.0 so the combined score stays
    in [0, 1] when each factor is in [0, 1]. This catches typo'd profiles
    early; it does not normalize on the user's behalf (better to fail loudly).
    """

    w_rec: float
    w_doc: float
    w_rel: float
    w_corr: float
    w_auth: float

    def __post_init__(self) -> None:
        total = self.w_rec + self.w_doc + self.w_rel + self.w_corr + self.w_auth
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(
                f"Weights must sum to 1.0; got {total} "
                f"(w_rec={self.w_rec}, w_doc={self.w_doc}, w_rel={self.w_rel}, "
                f"w_corr={self.w_corr}, w_auth={self.w_auth})"
            )


# Weight profiles
# ---------------
# These were originally calibrated against a relevance signal that always
# saturated at 1.0 (RRF rank-normalized), so other components had to
# differentiate. With absolute relevance (BM25/VSS-saturated, real range
# 0.1–0.9), the profiles are rebalanced to give relevance its proper share
# while keeping each profile's intent intact:
#
#   balanced_interactive — sane default: relevance leads, modest
#       recency + authority tilt, no extreme bias toward freshness.
#       Right for "what does my library know about X" queries.
#
#   currency_critical_interactive — when the user genuinely cares about
#       the latest API/spec ("what's the current way to X"). Recency +
#       doc_alignment dominate; relevance still meaningful.
#
#   foundational_interactive — timeless concepts (algorithms, design
#       patterns, classical CS). Authority + corroboration + relevance
#       lead; recency barely matters.
#
#   skill_* profiles drive the Skills Factory generation modes (§8.3).
WEIGHT_PROFILES: dict[str, Weights] = {
    # Default profile — relevance leads (0.45). Recency intentionally low
    # (0.10): in a default search, freshness shouldn't be allowed to tip a
    # foundational topical query toward a tangential-but-recent doc. The
    # currency_critical profile is the right tool for current-API queries.
    "balanced_interactive":          Weights(0.10, 0.10, 0.45, 0.15, 0.20),
    "currency_critical_interactive": Weights(0.40, 0.25, 0.20, 0.10, 0.05),
    "foundational_interactive":      Weights(0.05, 0.10, 0.35, 0.30, 0.20),
    "skill_recent_doc":              Weights(0.30, 0.30, 0.25, 0.05, 0.10),
    "skill_consensus":               Weights(0.05, 0.10, 0.30, 0.35, 0.20),
    "skill_authority":               Weights(0.05, 0.10, 0.25, 0.10, 0.50),
}


# ---------------------------------------------------------------------------
# Pure factor functions
# ---------------------------------------------------------------------------


def recency_score(*, age_days: Optional[float],
                  half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Exponential decay: a passage at age = half_life scores 0.5.

    None ⇒ neutral 0.5 (we don't know how recent it is, neither penalize
    nor reward). Negative ages (e.g., snapshot retrieved a few seconds in
    the future due to clock skew) clamp to 1.0.

    Floored at ``RECENCY_FLOOR`` (0.20) so very old foundational books
    (10+ years past the half-life) don't drop to effectively-zero
    recency. Without the floor, a 7-year-old book scores rec=0.09; a
    20-year-old book scores rec=0.001 — both effectively eliminating
    the recency component for that result. With the floor, recency is
    a "modest tilt" rather than an "effective veto" for old material.
    Currency-critical queries (rec weight 0.40) still strongly favor
    fresh content; the floor just prevents the rec component from
    becoming a near-zero-multiplier on chapters that are otherwise
    excellent matches.
    """
    if age_days is None:
        return DEFAULT_NEUTRAL_FACTOR
    if age_days <= 0:
        return 1.0
    return max(RECENCY_FLOOR, math.exp(-math.log(2) * age_days / half_life_days))


def doc_alignment_score(*, corroborates: int, contradicts: int) -> float:
    """Fraction of alignment-edge votes that are CORROBORATES.

    No edges either way ⇒ neutral 0.5. Pure CONTRADICTS ⇒ 0.0; pure
    CORROBORATES ⇒ 1.0.
    """
    total = corroborates + contradicts
    if total <= 0:
        return DEFAULT_NEUTRAL_FACTOR
    return corroborates / total


# Saturation constants for the absolute relevance components.
# Picked so a "strong" raw signal lands near 1.0 and a "weak" one near 0.2:
#   BM25=10  → 1 - exp(-2.0) ≈ 0.86          BM25=1   → 1 - exp(-0.2) ≈ 0.18
#   concept_hits=3 → 1 - exp(-1.0) ≈ 0.63    concept_hits=1 → ≈ 0.28
# VSS similarity is already in [0, 1] (cosine), so it passes through.
FTS_SATURATION_SCORE = 5.0
GRAPH_SATURATION_HITS = 3.0
# Multiplicative boost applied per unit of title_coverage. coverage=1.0 →
# relevance × 1.8 (capped at 1.0). coverage=0.0 → relevance unchanged.
#
# Title coverage is the strongest available "this result is ABOUT the
# topic" signal — a chapter titled "B-Trees as Database Indexes" is more
# likely on-topic for a B-tree query than a doc page that happens to
# BM25-match individual tokens. Calibrated so a partial-coverage chapter
# (e.g., 0.67) overcomes the recency × authority margin that fresh docs
# carry by default.
TITLE_COVERAGE_BOOST = 0.8

# When the chapter title matches EVERY significant query token (and the
# query has ≥2 tokens — generic single-word queries are excluded by the
# caller), relevance is floored at this value regardless of FTS/VSS
# strength. A chapter literally titled "Circuit Breaker Pattern" should
# win a "circuit breaker pattern" query even if its content is short and
# BM25 is low — a fully-matched title is the strongest available
# "this result is the topic" signal.
FULL_TITLE_MATCH_FLOOR = 0.95


def relevance_score(
    modality_scores: Optional[dict[str, Any]] = None,
    *, title_coverage: float = 0.0,
    full_title_match: bool = False,
) -> float:
    """Compose relevance from absolute per-modality signal strength.

    The previous formulation normalized RRF against the result set's max,
    which collapsed ALL "top of bucket" results to 1.0 — a low-quality
    BM25=1.4 match scored as relevant as a strong BM25=10 match. This made
    the combined score blind to actual match quality and let
    recency × authority pick the winner.

    The new formulation looks at the actual per-modality scores carried
    forward by ``_rrf_merge`` and saturates each independently:

      * FTS (BM25)             → ``1 - exp(-bm25 / FTS_SATURATION_SCORE)``
      * VSS (cosine similarity) → already in [0, 1], pass through
      * Graph (concept hits)    → ``1 - exp(-hits / GRAPH_SATURATION_HITS)``

    The candidate's relevance is the MAX of these three — best signal across
    modalities wins, so a strong FTS match isn't penalized by missing VSS,
    and vice versa. A candidate that hits both gets the better of the two,
    not a watered-down average.

    Title-coverage boost: when ``title_coverage`` is provided (the fraction
    of significant query tokens present in the result's chapter_title /
    heading_text), the base relevance is multiplied by
    ``1 + TITLE_COVERAGE_BOOST × coverage`` and clamped to [0, 1]. This
    rewards results whose TITLE matches the query intent over results
    that BM25-match on individual content tokens — a chapter titled
    "What Is Terraform State?" should win for "Terraform state locking"
    queries even if a tangential doc snippet has slightly higher BM25.

    None / missing modality_scores ⇒ 0.0 (no signal known), regardless of
    title_coverage. The boost is multiplicative; nothing × 1.5 is still
    nothing.
    """
    if not modality_scores:
        return 0.0

    fts_raw = max(
        float(modality_scores.get("fts_chapter") or 0.0),
        float(modality_scores.get("fts_doc_section") or 0.0),
    )
    vss_raw = max(
        float(modality_scores.get("vss_chapter") or 0.0),
        float(modality_scores.get("vss_doc_section") or 0.0),
    )
    graph_raw = max(
        float(modality_scores.get("graph_chapter") or 0.0),
        float(modality_scores.get("graph_doc_section") or 0.0),
    )

    fts_norm = 1.0 - math.exp(-fts_raw / FTS_SATURATION_SCORE) if fts_raw > 0 else 0.0
    vss_norm = max(0.0, min(1.0, vss_raw))
    graph_norm = (
        1.0 - math.exp(-graph_raw / GRAPH_SATURATION_HITS) if graph_raw > 0 else 0.0
    )

    base = max(fts_norm, vss_norm, graph_norm)
    if base <= 0.0:
        return 0.0
    cov = max(0.0, min(1.0, float(title_coverage)))
    boosted = min(1.0, base * (1.0 + TITLE_COVERAGE_BOOST * cov))
    # Full title match (caller-asserted: every significant query token in
    # title AND ≥2 tokens) floors relevance at FULL_TITLE_MATCH_FLOOR.
    # This decisively rewards chapters whose title IS the query topic.
    if full_title_match:
        return max(boosted, FULL_TITLE_MATCH_FLOOR)
    return boosted


def corroboration_score(*, corroborator_count: int,
                        saturate_at: int = CORROBORATION_SATURATION) -> float:
    """Saturating curve: 0 corroborators → 0.0, 3 → ~0.63, 9 → ~0.95."""
    if corroborator_count <= 0:
        return 0.0
    return 1.0 - math.exp(-corroborator_count / saturate_at)


def authority_score_from_raw(raw: Optional[float]) -> float:
    """Pass-through; clamped to [0, 1]. Unknown ⇒ neutral 0.5."""
    if raw is None:
        return DEFAULT_NEUTRAL_FACTOR
    return max(0.0, min(1.0, float(raw)))


def combined_score(
    weights: Weights, *, recency: float, doc_alignment: float,
    relevance: float, corroboration: float, authority: float,
) -> float:
    """Linear combination per §8.5. Inputs must be in [0, 1]; output is also."""
    return (
        weights.w_rec * recency
        + weights.w_doc * doc_alignment
        + weights.w_rel * relevance
        + weights.w_corr * corroboration
        + weights.w_auth * authority
    )


# ---------------------------------------------------------------------------
# DB-backed factor lookups
# ---------------------------------------------------------------------------


def _age_days(value: Any, now: datetime) -> Optional[float]:
    """Convert a stored timestamp/date to age in days from ``now``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (now - v).total_seconds() / 86400.0
    # ``date`` (no time component)
    try:
        v = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    except AttributeError:
        return None
    return (now - v).total_seconds() / 86400.0


def chapter_age_days(conn: duckdb.DuckDBPyConnection, chapter_id: int,
                     *, now: Optional[datetime] = None) -> Optional[float]:
    """Days since the chapter's parent book was published; None if unknown."""
    row = conn.execute(
        "SELECT b.publication_date FROM chapter c "
        "  JOIN book b ON c.book_id = b.book_id "
        " WHERE c.chapter_id = ?", [chapter_id],
    ).fetchone()
    return _age_days(row[0] if row else None, now or datetime.now(timezone.utc))


def doc_section_age_days(conn: duckdb.DuckDBPyConnection, doc_section_id: int,
                         *, now: Optional[datetime] = None) -> Optional[float]:
    """Days since this section's snapshot was retrieved."""
    row = conn.execute(
        "SELECT sn.retrieved_at FROM doc_section s "
        "  JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id "
        " WHERE s.doc_section_id = ?", [doc_section_id],
    ).fetchone()
    return _age_days(row[0] if row else None, now or datetime.now(timezone.utc))


def chapter_alignment_stats(conn: duckdb.DuckDBPyConnection,
                            chapter_id: int) -> tuple[int, int]:
    """Return (corroborates, contradicts) counts of edges pointing AT this chapter."""
    row = conn.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE relation_type = 'CORROBORATES'),
          COUNT(*) FILTER (WHERE relation_type = 'CONTRADICTS')
        FROM alignment_edge
        WHERE to_chapter_id = ?
        """, [chapter_id],
    ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def doc_section_alignment_stats(conn: duckdb.DuckDBPyConnection,
                                doc_section_id: int) -> tuple[int, int]:
    """Return (corroborates, contradicts) counts of edges this section emits."""
    row = conn.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE relation_type = 'CORROBORATES'),
          COUNT(*) FILTER (WHERE relation_type = 'CONTRADICTS')
        FROM alignment_edge
        WHERE from_doc_section_id = ?
        """, [doc_section_id],
    ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def chapter_raw_authority(conn: duckdb.DuckDBPyConnection,
                          chapter_id: int) -> Optional[float]:
    """Look up the chapter's parent book.publisher and map via PUBLISHER_AUTHORITY.

    Returns ``DEFAULT_AUTHORITY_BOOK`` when the book has no publisher
    recorded or the publisher isn't in the known-imprint table — never
    None, since chapters always have a positive default authority.
    """
    row = conn.execute(
        "SELECT b.publisher FROM chapter c "
        "  JOIN book b ON c.book_id = b.book_id "
        " WHERE c.chapter_id = ?", [chapter_id],
    ).fetchone()
    publisher = row[0] if row else None
    return authority_score_from_publisher(publisher)


def doc_section_raw_authority(conn: duckdb.DuckDBPyConnection,
                              doc_section_id: int) -> Optional[float]:
    """Pull doc_source.authority_score for the source this section came from."""
    row = conn.execute(
        """
        SELECT src.authority_score FROM doc_section s
          JOIN doc_snapshot sn  ON s.snapshot_id  = sn.snapshot_id
          JOIN doc_source   src ON sn.doc_source_id = src.doc_source_id
         WHERE s.doc_section_id = ?
        """, [doc_section_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# ---------------------------------------------------------------------------
# Per-result component scoring
# ---------------------------------------------------------------------------


@dataclass
class ComponentScores:
    """The five raw component scores in [0, 1] for one candidate."""

    recency: float
    doc_alignment: float
    relevance: float
    corroboration: float
    authority: float

    def combine(self, weights: Weights) -> float:
        return combined_score(
            weights,
            recency=self.recency, doc_alignment=self.doc_alignment,
            relevance=self.relevance, corroboration=self.corroboration,
            authority=self.authority,
        )


def compute_components_for_result(
    conn: duckdb.DuckDBPyConnection, result: dict[str, Any],
    *, now: Optional[datetime] = None,
) -> ComponentScores:
    """Compute the 5 component scores for one search-result row.

    The ``result`` dict is the per-result shape produced by
    kb-mcp/server.py:_rrf_merge — must carry ``kind``, ``result_id``, and
    ``modality_scores`` (the absolute per-modality scores carried forward
    from FTS/VSS/graph). ``rrf_score`` is no longer used for the relevance
    component; see relevance_score() for the rationale.
    """
    kind = result.get("kind", "chapter")
    rid = int(result["result_id"])
    modality_scores = result.get("modality_scores") or {}

    if kind == "chapter":
        age = chapter_age_days(conn, rid, now=now)
        corr_n, contr_n = chapter_alignment_stats(conn, rid)
        auth_raw = chapter_raw_authority(conn, rid)
    elif kind == "doc_section":
        age = doc_section_age_days(conn, rid, now=now)
        corr_n, contr_n = doc_section_alignment_stats(conn, rid)
        auth_raw = doc_section_raw_authority(conn, rid)
    else:
        raise ValueError(f"unknown result kind {kind!r}")

    return ComponentScores(
        recency=recency_score(age_days=age),
        doc_alignment=doc_alignment_score(
            corroborates=corr_n, contradicts=contr_n,
        ),
        relevance=relevance_score(
            modality_scores,
            title_coverage=float(result.get("title_coverage") or 0.0),
            full_title_match=bool(result.get("full_title_match")),
        ),
        corroboration=corroboration_score(corroborator_count=corr_n),
        authority=authority_score_from_raw(auth_raw),
    )


# ---------------------------------------------------------------------------
# Interactive Ranker (§8.1) — {primary, corroborations, conflicts}
# ---------------------------------------------------------------------------


@dataclass
class ScoredResult:
    """A search result enriched with component + combined scores."""

    result: dict[str, Any]
    components: ComponentScores
    combined: float


@dataclass
class InteractiveOutput:
    """Per §8.1: a primary + corroborations + conflicts, with the raw scored
    list available for callers that want to re-rank."""

    primary: Optional[ScoredResult]
    corroborations: list[ScoredResult] = field(default_factory=list)
    conflicts: list[ScoredResult] = field(default_factory=list)
    all_scored: list[ScoredResult] = field(default_factory=list)


class InteractiveRanker:
    """Score a result set and emit the §8.1 {primary, corroborations, conflicts}
    decomposition.

    Conflicts come from ``alignment_edge`` rows linking the primary to other
    candidates with relation_type='CONTRADICTS'. Corroborations are the
    remaining top-ranked candidates that aren't flagged as conflicts.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, weights: Weights,
                 *, corroborations_limit: int = 5, conflicts_limit: int = 5):
        self.conn = conn
        self.weights = weights
        self.corroborations_limit = corroborations_limit
        self.conflicts_limit = conflicts_limit

    def rank(
        self, results: Sequence[dict[str, Any]], *,
        now: Optional[datetime] = None,
    ) -> InteractiveOutput:
        if not results:
            return InteractiveOutput(primary=None)

        scored: list[ScoredResult] = []
        for r in results:
            comps = compute_components_for_result(self.conn, r, now=now)
            scored.append(ScoredResult(
                result=r, components=comps, combined=comps.combine(self.weights),
            ))
        scored.sort(key=lambda s: s.combined, reverse=True)

        primary = scored[0]
        conflict_keys = self._conflict_keys_for(primary)

        corroborations: list[ScoredResult] = []
        conflicts: list[ScoredResult] = []
        for s in scored[1:]:
            key = (s.result.get("kind"), int(s.result["result_id"]))
            if key in conflict_keys:
                if len(conflicts) < self.conflicts_limit:
                    conflicts.append(s)
            else:
                if len(corroborations) < self.corroborations_limit:
                    corroborations.append(s)

        return InteractiveOutput(
            primary=primary,
            corroborations=corroborations,
            conflicts=conflicts,
            all_scored=scored,
        )

    def _conflict_keys_for(self, primary: ScoredResult) -> set[tuple[str, int]]:
        """Return (kind, result_id) keys for results that CONTRADICT the primary.

        For a chapter primary: doc_sections with a CONTRADICTS alignment_edge
        pointing at the chapter. For a doc_section primary: chapters that
        CONTRADICTS edges (originating from this section) point at.
        """
        kind = primary.result.get("kind")
        rid = int(primary.result["result_id"])
        keys: set[tuple[str, int]] = set()
        if kind == "chapter":
            rows = self.conn.execute(
                "SELECT from_doc_section_id FROM alignment_edge "
                " WHERE relation_type='CONTRADICTS' AND to_chapter_id = ?",
                [rid],
            ).fetchall()
            for r in rows:
                keys.add(("doc_section", int(r[0])))
        elif kind == "doc_section":
            rows = self.conn.execute(
                "SELECT to_chapter_id, to_doc_section_id FROM alignment_edge "
                " WHERE relation_type='CONTRADICTS' AND from_doc_section_id = ?",
                [rid],
            ).fetchall()
            for to_ch, to_sec in rows:
                if to_ch is not None:
                    keys.add(("chapter", int(to_ch)))
                if to_sec is not None:
                    keys.add(("doc_section", int(to_sec)))
        return keys


# ---------------------------------------------------------------------------
# Generation Ranker (§8.2 / §8.3) — three strategies
# ---------------------------------------------------------------------------


SELECTION_STRATEGIES = ("recent_doc_anchored", "consensus_synthesis", "authority_pick")


@dataclass
class GenerationOutput:
    """Consolidated result set for Skill generation, plus dropped-source
    provenance for §8.6 auditability."""

    selected: list[ScoredResult] = field(default_factory=list)
    dropped: list[tuple[ScoredResult, str]] = field(default_factory=list)
    strategy: str = ""


class GenerationRanker:
    """Apply a selection strategy and return a consolidated source set.

    Strategies (per §8.3):
      - recent_doc_anchored: keep the highest-scoring doc_section for each
        concept; drop chapters that contradict current docs.
      - consensus_synthesis: keep results with corroboration ≥ threshold;
        prefer corroborated material over single-source.
      - authority_pick: keep only the top-authority result.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, weights: Weights,
                 *, top_k: int = 5):
        self.conn = conn
        self.weights = weights
        self.top_k = top_k

    def select(
        self, results: Sequence[dict[str, Any]], *,
        strategy: str, now: Optional[datetime] = None,
    ) -> GenerationOutput:
        if strategy not in SELECTION_STRATEGIES:
            raise ValueError(
                f"unknown strategy {strategy!r}; expected one of {SELECTION_STRATEGIES}"
            )
        if not results:
            return GenerationOutput(strategy=strategy)

        scored: list[ScoredResult] = []
        for r in results:
            comps = compute_components_for_result(self.conn, r, now=now)
            scored.append(ScoredResult(
                result=r, components=comps, combined=comps.combine(self.weights),
            ))
        scored.sort(key=lambda s: s.combined, reverse=True)

        if strategy == "recent_doc_anchored":
            return self._recent_doc_anchored(scored)
        if strategy == "consensus_synthesis":
            return self._consensus_synthesis(scored)
        return self._authority_pick(scored)

    # --- strategy implementations ---

    def _recent_doc_anchored(self, scored: list[ScoredResult]) -> GenerationOutput:
        """Keep top-k by combined score; drop chapters that have CONTRADICTS
        edges pointing AT them (book content the docs supersede)."""
        out = GenerationOutput(strategy="recent_doc_anchored")
        for s in scored:
            if s.result.get("kind") == "chapter":
                _, contradicts = chapter_alignment_stats(
                    self.conn, int(s.result["result_id"]),
                )
                if contradicts > 0:
                    out.dropped.append((s, "contradicted by current docs"))
                    continue
            if len(out.selected) < self.top_k:
                out.selected.append(s)
        return out

    def _consensus_synthesis(self, scored: list[ScoredResult]) -> GenerationOutput:
        """Prefer corroborated material — keep top-k after filtering to results
        whose component-level corroboration ≥ neutral."""
        out = GenerationOutput(strategy="consensus_synthesis")
        for s in scored:
            if s.components.corroboration < DEFAULT_NEUTRAL_FACTOR:
                out.dropped.append((s, "single-source (no corroboration)"))
                continue
            if len(out.selected) < self.top_k:
                out.selected.append(s)
        return out

    def _authority_pick(self, scored: list[ScoredResult]) -> GenerationOutput:
        """Keep only the top-1 by authority component, with everything else
        recorded as dropped (still auditable for §8.6 provenance)."""
        out = GenerationOutput(strategy="authority_pick")
        if not scored:
            return out
        primary = max(scored, key=lambda s: s.components.authority)
        out.selected.append(primary)
        for s in scored:
            if s is primary:
                continue
            out.dropped.append((s, "lower authority than chosen primary"))
        return out
