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
* ``authority`` for chapters is a fixed 0.6 default — books don't have an
  explicit authority column today. A heuristic by publisher could refine
  this; tracked as a Phase 4.6 follow-up. ``authority`` for doc_sections
  flows from ``doc_source.authority_score`` directly.
* ``recency`` uses an exponential half-life of 2 years. Books published
  longer ago decay; doc snapshots retrieved within the past day are ~1.0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import duckdb


DEFAULT_HALF_LIFE_DAYS = 365 * 2          # ~2-year half-life for recency decay
DEFAULT_AUTHORITY_BOOK = 0.6              # fallback when book.publisher isn't a known anchor
DEFAULT_NEUTRAL_FACTOR = 0.5              # used when a factor is genuinely unknown
CORROBORATION_SATURATION = 3              # ~1.0 once 3+ corroborators exist


# Publisher → authority mapping. Tech-publisher reputation is reasonably well-
# anchored: established imprints with strong editorial review (O'Reilly, Manning,
# Pragmatic, Addison-Wesley/Pearson, MIT Press) get higher scores than fast-cycle
# publishers (Packt) or self-published material. Numbers here are starting points;
# Phase 4.6 eval can drive empirical tuning.
PUBLISHER_AUTHORITY: dict[str, float] = {
    "o'reilly":           0.85,
    "o'reilly media":     0.85,
    "manning":            0.85,
    "manning publications": 0.85,
    "pragmatic bookshelf": 0.80,
    "the pragmatic programmers": 0.80,
    "addison-wesley":     0.80,
    "addison-wesley professional": 0.80,
    "pearson":            0.78,
    "morgan kaufmann":    0.80,
    "mit press":          0.85,
    "no starch press":    0.78,
    "apress":             0.72,
    "wiley":              0.72,
    "wrox":               0.68,
    "packt":              0.65,
    "packt publishing":   0.65,
}


def authority_score_from_publisher(publisher: Optional[str]) -> float:
    """Map a free-form publisher string to an authority value.

    Match is case- and whitespace-tolerant; unknown / missing publisher
    falls back to ``DEFAULT_AUTHORITY_BOOK``. Common compound names
    ("O'Reilly Media", "Addison-Wesley Professional") are handled by
    explicit table entries rather than fuzzy matching, which would risk
    false positives like "Wiley-VCH" hitting the Wiley score.
    """
    if not publisher:
        return DEFAULT_AUTHORITY_BOOK
    norm = publisher.strip().lower()
    if not norm:
        return DEFAULT_AUTHORITY_BOOK
    return PUBLISHER_AUTHORITY.get(norm, DEFAULT_AUTHORITY_BOOK)


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


WEIGHT_PROFILES: dict[str, Weights] = {
    "currency_critical_interactive": Weights(0.30, 0.35, 0.20, 0.10, 0.05),
    "foundational_interactive":      Weights(0.05, 0.05, 0.30, 0.30, 0.30),
    "skill_recent_doc":              Weights(0.35, 0.40, 0.15, 0.05, 0.05),
    "skill_consensus":               Weights(0.10, 0.05, 0.25, 0.40, 0.20),
    "skill_authority":               Weights(0.05, 0.10, 0.20, 0.10, 0.55),
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
    """
    if age_days is None:
        return DEFAULT_NEUTRAL_FACTOR
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / half_life_days)


def doc_alignment_score(*, corroborates: int, contradicts: int) -> float:
    """Fraction of alignment-edge votes that are CORROBORATES.

    No edges either way ⇒ neutral 0.5. Pure CONTRADICTS ⇒ 0.0; pure
    CORROBORATES ⇒ 1.0.
    """
    total = corroborates + contradicts
    if total <= 0:
        return DEFAULT_NEUTRAL_FACTOR
    return corroborates / total


def relevance_score(*, rrf_score: float, max_rrf_score: float) -> float:
    """Normalize this candidate's RRF contribution against the result set's max.

    The result set's max RRF score becomes 1.0; everything else scales
    proportionally. Empty result set or all-zero scores ⇒ 0.0 to keep
    relevance a non-amplifier when nothing's relevant.
    """
    if max_rrf_score <= 0:
        return 0.0
    return min(1.0, max(0.0, rrf_score / max_rrf_score))


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
    *, max_rrf_score: float, now: Optional[datetime] = None,
) -> ComponentScores:
    """Compute the 5 component scores for one search-result row.

    The ``result`` dict is the per-result shape produced by
    kb-mcp/server.py:_rrf_merge — must carry ``kind``, ``result_id``, and
    ``rrf_score``. Other fields are looked up from the catalog.
    """
    kind = result.get("kind", "chapter")
    rid = int(result["result_id"])
    rrf = float(result.get("rrf_score") or 0.0)

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
        relevance=relevance_score(rrf_score=rrf, max_rrf_score=max_rrf_score),
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

        max_rrf = max(float(r.get("rrf_score") or 0.0) for r in results) or 1.0
        scored: list[ScoredResult] = []
        for r in results:
            comps = compute_components_for_result(
                self.conn, r, max_rrf_score=max_rrf, now=now,
            )
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

        max_rrf = max(float(r.get("rrf_score") or 0.0) for r in results) or 1.0
        scored: list[ScoredResult] = []
        for r in results:
            comps = compute_components_for_result(
                self.conn, r, max_rrf_score=max_rrf, now=now,
            )
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
