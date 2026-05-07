"""slide_deck.py — Phase 9.5 Slide-deck Outline generator (deterministic v1).

Produces a talk skeleton — slide-by-slide bullets, presenter notes,
and visual suggestions — for a topic at a given duration. Single MCP
call, fully deterministic.

The decomposer is a heuristic rhetorical arc:
  opening → agenda → 3 insights (each 3-5 content slides) →
  takeaways → Q&A. Each insight is anchored on one of the topic's
  highly-connected concepts; bullets within an insight are pulled
  from procedures + concept descriptions.

Per architecture spec §2.10:
* ≤ 5 bullets per slide
* ≤ 10 words per bullet
* Presenter notes < 3 sentences per slide
* Slide count matches duration (~1 min/content slide + 20% for
  opening/closing/Q&A)

Output:
    slides/<topic>/
      _outline.md         slide-by-slide bullets + presenter notes
      _abstract.md        CFP-ready abstract
      visuals.md          per-slide visual suggestions
      speaker-notes.md    standalone speaker notes (one section per slide)
      sources.md          bibliography
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)

LOG = logging.getLogger("mypub-slide-deck")


GENERATOR_TYPE = "slide_deck"
DEFAULT_DURATION_MIN = 30
DEFAULT_AUDIENCE = "engineers"     # "engineers" | "executives" | "mixed"
DEFAULT_INSIGHTS = 3
MAX_BULLETS_PER_SLIDE = 5
MAX_WORDS_PER_BULLET = 10
MAX_SENTENCES_PRESENTER_NOTES = 3


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class _Slide:
    """One slide. ``role`` distinguishes opener/agenda/content/closer."""

    ordinal: int
    role: str                # 'title' | 'agenda' | 'insight' | 'content' | 'takeaways' | 'qa'
    title: str
    bullets: list[str] = field(default_factory=list)
    presenter_notes: str = ""
    visual_suggestion: str = ""
    insight_idx: Optional[int] = None  # which insight this slide belongs to (1..N)
    source_concept_ids: list[int] = field(default_factory=list)
    source_procedure_ids: list[int] = field(default_factory=list)
    source_chapter_ids: list[int] = field(default_factory=list)


@dataclass
class _Insight:
    """One narrative beat in the talk."""

    ordinal: int                       # 1-based
    anchor_concept_id: int
    anchor_concept_name: str
    anchor_concept_type: Optional[str]
    related_concepts: list[tuple[int, str, Optional[str], int]]  # (id, name, type, chapter_count)
    procedures: list[tuple[int, str, Optional[str]]]              # (id, name, snippet)


@dataclass
class _Decomposition:
    topic_concept_id: int
    topic_name: str
    topic_concept_type: Optional[str]
    duration_min: int
    audience: str
    n_content_slides_target: int       # derived from duration
    insights: list[_Insight]
    slides: list[_Slide]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def _slide_count_from_duration(duration_min: int, n_insights: int) -> int:
    """Heuristic: 1 minute per content slide, +20% buffer for
    opening/closing/Q&A. Returns total slide count target."""
    raw = max(8, int(duration_min * 0.8))    # ~80% time on content
    # Always at least: title + agenda + insights + takeaways + Q&A = 4 + insights
    fixed = 4 + n_insights
    return max(fixed, raw + 4)


def _content_slides_per_insight(total_content: int, n_insights: int) -> int:
    """Spread content slides across insights. Caps at 6 per spec."""
    if n_insights <= 0:
        return 0
    per = max(3, total_content // n_insights)
    return min(6, per)


def _short_phrase(text: str, max_words: int = MAX_WORDS_PER_BULLET) -> str:
    """Trim a phrase to ≤ max_words. Strips trailing punctuation,
    drops common filler at the start."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text.strip())
    # Drop leading filler ("The ", "A ", "An ", "It ", "This ")
    cleaned = re.sub(r"^(?:The|A|An|It|This|That|Their|Its)\s+", "", cleaned, flags=re.IGNORECASE)
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned.rstrip(",.;:?!")
    return " ".join(words[:max_words]).rstrip(",.;:?!") + "…"


def _trim_sentences(text: str, max_sentences: int = MAX_SENTENCES_PRESENTER_NOTES) -> str:
    """Take first N sentences; collapse whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return " ".join(parts[:max_sentences]).strip()


class RhetoricalArcDecomposer:
    """Builds a talk skeleton from a topic concept.

    Strategy:
      1. Resolve topic to a graph concept.
      2. Find the topic's most-connected related concepts via concept_relation
         (REQUIRES + EXTENDS + CITES). The top-N become the talk's "insights".
      3. Per insight: pull a few related concepts as bullet sources, plus
         procedures linked to the insight concept (for code-snippet slides).
      4. Render the slide sequence: title / agenda / per-insight cluster /
         takeaways / Q&A.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        duration_min: int = DEFAULT_DURATION_MIN,
        audience: str = DEFAULT_AUDIENCE,
        n_insights: int = DEFAULT_INSIGHTS,
        thesis: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        topic_id = resolver.resolve_lookup_only(query)
        if topic_id is None:
            return _Decomposition(
                topic_concept_id=-1, topic_name=query,
                topic_concept_type=None,
                duration_min=duration_min, audience=audience,
                n_content_slides_target=0,
                insights=[], slides=[],
                notes=[f"topic concept {query!r} not found"],
            )
        row = conn.execute(
            "SELECT name, concept_type, description FROM concept WHERE concept_id = ?",
            [topic_id],
        ).fetchone()
        topic_name, topic_type, topic_desc = row[0], row[1], row[2]

        # 1. Find candidate insights — the topic's most-connected neighbors
        candidates = self._find_insight_candidates(conn, topic_id, n_insights)
        if not candidates:
            # Fall back to topic-only single-insight talk
            candidates = [(topic_id, topic_name, topic_type, 0)]

        # 2. Per-candidate insight: enrich with related concepts + procedures
        insights: list[_Insight] = []
        for ordinal, (cid, cname, ctype, _hits) in enumerate(candidates, start=1):
            related = self._related_concepts(conn, cid, exclude_ids={topic_id})[:6]
            procs = self._linked_procedures(conn, cid)[:3]
            insights.append(_Insight(
                ordinal=ordinal,
                anchor_concept_id=cid,
                anchor_concept_name=cname,
                anchor_concept_type=ctype,
                related_concepts=related,
                procedures=procs,
            ))

        # 3. Slide layout
        total_target = _slide_count_from_duration(duration_min, len(insights))
        per_insight = _content_slides_per_insight(
            total_target - 4, len(insights),  # subtract title/agenda/takeaways/qa
        )
        slides = self._render_slide_sequence(
            topic_name=topic_name, topic_desc=topic_desc, audience=audience,
            insights=insights, per_insight=per_insight, thesis=thesis,
        )

        notes: list[str] = []
        if not insights:
            notes.append("topic has no related concepts; talk is title-only")

        return _Decomposition(
            topic_concept_id=topic_id, topic_name=topic_name,
            topic_concept_type=topic_type,
            duration_min=duration_min, audience=audience,
            n_content_slides_target=total_target,
            insights=insights, slides=slides, notes=notes,
        )

    @staticmethod
    def _find_insight_candidates(
        conn: duckdb.DuckDBPyConnection, topic_id: int, n: int,
    ) -> list[tuple[int, str, Optional[str], int]]:
        """Concepts most-connected to the topic via REQUIRES/EXTENDS/CITES,
        ranked by chapter coverage."""
        rows = conn.execute(
            """
            WITH neighbors AS (
              SELECT to_concept_id AS cid, COUNT(*) AS edges
                FROM concept_relation
               WHERE from_concept_id = ?
                 AND relation_type IN ('REQUIRES', 'EXTENDS', 'CITES')
               GROUP BY to_concept_id
              UNION ALL
              SELECT from_concept_id AS cid, COUNT(*) AS edges
                FROM concept_relation
               WHERE to_concept_id = ?
                 AND relation_type IN ('REQUIRES', 'EXTENDS', 'CITES')
               GROUP BY from_concept_id
            ),
            ranked AS (
              SELECT cid, SUM(edges) AS total_edges FROM neighbors GROUP BY cid
            ),
            mentions AS (
              SELECT from_concept_id AS cid, source_id
                FROM concept_relation
               WHERE source_type = 'chapter'
              UNION
              SELECT to_concept_id AS cid, source_id
                FROM concept_relation
               WHERE source_type = 'chapter'
            ),
            chap_count AS (
              SELECT m.cid, COUNT(DISTINCT m.source_id) AS chapters
                FROM mentions m
                JOIN ranked r ON r.cid = m.cid
               GROUP BY m.cid
            )
            SELECT c.concept_id, c.name, c.concept_type,
                   COALESCE(cc.chapters, 0) AS chapters
              FROM ranked r
              JOIN concept c ON c.concept_id = r.cid
              LEFT JOIN chap_count cc ON cc.cid = r.cid
             WHERE c.concept_id != ?
             ORDER BY chapters DESC, r.total_edges DESC, c.name
             LIMIT ?
            """,
            [topic_id, topic_id, topic_id, n],
        ).fetchall()
        return [(int(r[0]), r[1], r[2], int(r[3])) for r in rows]

    @staticmethod
    def _related_concepts(
        conn: duckdb.DuckDBPyConnection, cid: int,
        *, exclude_ids: set[int],
    ) -> list[tuple[int, str, Optional[str], int]]:
        rows = conn.execute(
            """
            WITH n AS (
              SELECT to_concept_id AS rid FROM concept_relation
               WHERE from_concept_id = ?
              UNION
              SELECT from_concept_id AS rid FROM concept_relation
               WHERE to_concept_id = ?
            )
            SELECT c.concept_id, c.name, c.concept_type,
                   (SELECT COUNT(DISTINCT cr.source_id)
                      FROM concept_relation cr
                      JOIN chapter ch ON ch.chapter_id = cr.source_id
                     WHERE cr.source_type = 'chapter'
                       AND (cr.from_concept_id = c.concept_id
                            OR cr.to_concept_id = c.concept_id)
                   ) AS chapters
              FROM n
              JOIN concept c ON c.concept_id = n.rid
             ORDER BY chapters DESC, c.name
            """,
            [cid, cid],
        ).fetchall()
        out = []
        for r in rows:
            cc = int(r[0])
            if cc in exclude_ids or cc == cid:
                continue
            out.append((cc, r[1], r[2], int(r[3])))
        return out

    @staticmethod
    def _linked_procedures(
        conn: duckdb.DuckDBPyConnection, cid: int,
    ) -> list[tuple[int, str, Optional[str]]]:
        rows = conn.execute(
            """
            SELECT p.procedure_id, p.name, p.steps
              FROM procedure p
              JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
             WHERE pc.concept_id = ?
               AND p.steps IS NOT NULL
             ORDER BY length(p.steps) ASC, p.procedure_id
             LIMIT 5
            """,
            [cid],
        ).fetchall()
        return [(int(r[0]), r[1] or "(unnamed)", r[2]) for r in rows]

    @staticmethod
    def _render_slide_sequence(
        *,
        topic_name: str, topic_desc: Optional[str], audience: str,
        insights: list[_Insight], per_insight: int, thesis: Optional[str],
    ) -> list[_Slide]:
        slides: list[_Slide] = []

        # Slide 1: Title
        slides.append(_Slide(
            ordinal=len(slides) + 1, role="title",
            title=topic_name,
            bullets=[],
            presenter_notes=_trim_sentences(
                f"Open by stating the talk's thesis. {thesis or ''} "
                f"Audience: {audience}.".strip()
            ),
            visual_suggestion="Title card with the topic name and presenter affiliation.",
        ))

        # Slide 2: Agenda
        slides.append(_Slide(
            ordinal=len(slides) + 1, role="agenda",
            title="Agenda",
            bullets=[
                _short_phrase(f"{i.ordinal}. {i.anchor_concept_name}")
                for i in insights
            ][:MAX_BULLETS_PER_SLIDE],
            presenter_notes=_trim_sentences(
                "Walk the audience through the three sections. "
                "Set the expectation for code/no-code density."
            ),
            visual_suggestion="Numbered list with one-icon-per-section.",
        ))

        # Per-insight cluster: 1 insight-anchor slide + content slides + (optional) code slide
        for insight in insights:
            slides.append(_Slide(
                ordinal=len(slides) + 1, role="insight",
                title=f"Insight {insight.ordinal}: {insight.anchor_concept_name}",
                bullets=[
                    _short_phrase(f"Why {insight.anchor_concept_name} matters"),
                    _short_phrase(
                        f"What {insight.anchor_concept_name} solves"
                    ),
                    _short_phrase(f"When it falls short"),
                ][:MAX_BULLETS_PER_SLIDE],
                presenter_notes=_trim_sentences(
                    f"Frame why the audience should care about "
                    f"{insight.anchor_concept_name}. Tie back to the thesis."
                ),
                visual_suggestion="Section divider with the insight number prominent.",
                insight_idx=insight.ordinal,
                source_concept_ids=[insight.anchor_concept_id],
            ))

            # Content slides — one per related concept, up to (per_insight - 1)
            n_content = max(2, per_insight - 1)
            for j, (rid, rname, _rtype, _rchaps) in enumerate(insight.related_concepts[:n_content]):
                slides.append(_Slide(
                    ordinal=len(slides) + 1, role="content",
                    title=_short_phrase(rname, max_words=8),
                    bullets=[
                        _short_phrase(f"Definition of {rname}"),
                        _short_phrase(
                            f"Connection to {insight.anchor_concept_name}"
                        ),
                        _short_phrase("Practical implications"),
                    ][:MAX_BULLETS_PER_SLIDE],
                    presenter_notes=_trim_sentences(
                        f"Spend ~1 minute on {rname}. "
                        f"Lean on a concrete example."
                    ),
                    visual_suggestion=(
                        "Comparison table or diagram"
                        if j == 0 else
                        "Code snippet or before/after illustration"
                    ),
                    insight_idx=insight.ordinal,
                    source_concept_ids=[rid],
                ))

            # Optional code slide if a procedure is available
            if insight.procedures:
                pid, pname, _psnippet = insight.procedures[0]
                slides.append(_Slide(
                    ordinal=len(slides) + 1, role="content",
                    title=_short_phrase(f"Demo: {pname}", max_words=8),
                    bullets=[
                        _short_phrase("Live walkthrough"),
                        _short_phrase("Highlight one gotcha"),
                        _short_phrase("Show the result"),
                    ][:MAX_BULLETS_PER_SLIDE],
                    presenter_notes=_trim_sentences(
                        f"Run the {pname} procedure. "
                        f"Pause for one question before moving on."
                    ),
                    visual_suggestion=(
                        f"Live code from procedure_id={pid} or "
                        "screencast fallback."
                    ),
                    insight_idx=insight.ordinal,
                    source_procedure_ids=[pid],
                ))

        # Takeaways
        slides.append(_Slide(
            ordinal=len(slides) + 1, role="takeaways",
            title="Takeaways",
            bullets=[
                _short_phrase(f"{i.anchor_concept_name} earns its place")
                for i in insights
            ][:MAX_BULLETS_PER_SLIDE] or [_short_phrase(f"{topic_name} is worth your time")],
            presenter_notes=_trim_sentences(
                "Restate the thesis. Give one concrete next step the audience can take."
            ),
            visual_suggestion="Three bold takeaways with a call-to-action footer.",
        ))

        # Q&A
        slides.append(_Slide(
            ordinal=len(slides) + 1, role="qa",
            title="Q&A",
            bullets=[_short_phrase("Discussion"), _short_phrase("Where to learn more")],
            presenter_notes=_trim_sentences(
                "Take questions; if there are none, prompt with one yourself."
            ),
            visual_suggestion="Contact info + reference list.",
        ))

        return slides


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "topic"


def _render_outline(d: _Decomposition) -> str:
    lines = [
        f"# {d.topic_name} — Slide Outline",
        "",
        f"**Audience:** {d.audience}  |  **Duration:** {d.duration_min} min  |  **Slides:** {len(d.slides)}",
        "",
    ]
    for s in d.slides:
        lines.append(f"## Slide {s.ordinal} — {s.title}")
        lines.append(f"_{s.role}_")
        lines.append("")
        for b in s.bullets:
            lines.append(f"- {b}")
        if s.presenter_notes:
            lines.append("")
            lines.append(f"> {s.presenter_notes}")
        lines.append("")
    return "\n".join(lines)


def _render_abstract(d: _Decomposition) -> str:
    insights_phrase = ", ".join(i.anchor_concept_name for i in d.insights[:3]) or d.topic_name
    return (
        f"# {d.topic_name} — Talk Abstract\n\n"
        f"A {d.duration_min}-minute talk for **{d.audience}** on {d.topic_name}.\n\n"
        f"This session walks through {insights_phrase} — the three lenses through which "
        f"{d.topic_name} matters today. Each insight is grounded in book chapters and current "
        f"vendor docs from the knowledge base, with one short live demo per insight where a "
        f"procedure is available.\n\n"
        f"Audience takeaways:\n\n"
        + "\n".join(f"- Where {i.anchor_concept_name} fits in the {d.topic_name} story"
                    for i in d.insights[:3])
        + "\n"
    )


def _render_visuals(d: _Decomposition) -> str:
    lines = [
        f"# {d.topic_name} — Visual Suggestions",
        "",
        "Per-slide visual suggestion. Use as a brief for the talk's design pass.",
        "",
    ]
    for s in d.slides:
        if s.visual_suggestion:
            lines.append(f"- **Slide {s.ordinal} ({s.title})**: {s.visual_suggestion}")
    lines.append("")
    return "\n".join(lines)


def _render_speaker_notes(d: _Decomposition) -> str:
    lines = [f"# {d.topic_name} — Speaker Notes", ""]
    for s in d.slides:
        lines.append(f"## Slide {s.ordinal} — {s.title}")
        lines.append("")
        lines.append(s.presenter_notes or "_(no notes)_")
        lines.append("")
    return "\n".join(lines)


def _render_sources(conn: duckdb.DuckDBPyConnection, d: _Decomposition) -> str:
    """Bibliography of every concept and procedure cited."""
    concept_ids: set[int] = {d.topic_concept_id}
    procedure_ids: set[int] = set()
    for s in d.slides:
        concept_ids.update(s.source_concept_ids)
        procedure_ids.update(s.source_procedure_ids)

    lines = [f"# {d.topic_name} — Sources", ""]
    if concept_ids:
        lines.append("## Concepts")
        lines.append("")
        ph = ",".join(["?"] * len(concept_ids))
        rows = conn.execute(
            f"SELECT concept_id, name, concept_type FROM concept "
            f"WHERE concept_id IN ({ph}) ORDER BY name",
            list(concept_ids),
        ).fetchall()
        for r in rows:
            lines.append(f"- **{r[1]}** ({r[2] or 'concept'}) — `concept_id={r[0]}`")
        lines.append("")
    if procedure_ids:
        lines.append("## Procedures")
        lines.append("")
        ph = ",".join(["?"] * len(procedure_ids))
        rows = conn.execute(
            f"SELECT procedure_id, name FROM procedure "
            f"WHERE procedure_id IN ({ph}) ORDER BY procedure_id",
            list(procedure_ids),
        ).fetchall()
        for r in rows:
            lines.append(f"- **{r[1] or '(unnamed)'}** — `procedure_id={r[0]}`")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class SlideDeckPlanner:
    """Produces a fully-rendered GenPlan from the decomposition.

    One ``GenUnit`` per slide. Files: _outline, _abstract, visuals,
    speaker-notes, sources.
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.topic_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.topic_name,
            target_audience=d.audience,
            source_query=d.topic_name,
            package_metadata={
                "topic_concept_id": d.topic_concept_id,
                "topic_concept_type": d.topic_concept_type,
                "duration_min": d.duration_min,
                "audience": d.audience,
                "n_slides": len(d.slides),
                "n_insights": len(d.insights),
            },
            notes=list(d.notes),
        )

        for s in d.slides:
            plan.units.append(GenUnit(
                unit_type="slide",
                name=s.title,
                ordinal=s.ordinal,
                metadata={
                    "role": s.role,
                    "insight_idx": s.insight_idx,
                    "n_bullets": len(s.bullets),
                    "concept_ids": list(s.source_concept_ids),
                    "procedure_ids": list(s.source_procedure_ids),
                },
                logical_key=f"slide_{s.ordinal}",
                content_markdown=(
                    "\n".join(f"- {b}" for b in s.bullets)
                    + (f"\n\n_Notes:_ {s.presenter_notes}" if s.presenter_notes else "")
                ),
                generation_notes=f"role={s.role}",
                sources=(
                    [("concept", cid, 1.0, 1.0, None)
                     for cid in s.source_concept_ids]
                    + [("procedure", pid, 1.0, 1.0, None)
                       for pid in s.source_procedure_ids]
                ),
            ))

        plan.files.extend([
            GenFile(filename="_outline.md", content=_render_outline(d), purpose="outline"),
            GenFile(filename="_abstract.md", content=_render_abstract(d), purpose="abstract"),
            GenFile(filename="visuals.md", content=_render_visuals(d), purpose="visuals"),
            GenFile(filename="speaker-notes.md",
                    content=_render_speaker_notes(d), purpose="speaker_notes"),
            GenFile(filename="sources.md",
                    content=_render_sources(conn, d), purpose="sources"),
        ])
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _word_count_bullet(b: str) -> int:
    return len(re.findall(r"\b\w[\w'-]*\b", b))


def _sentence_count(text: str) -> int:
    if not text:
        return 0
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return sum(1 for p in parts if p.strip())


class SlideDeckValidator:
    """Checks the spec invariants:

    * topic concept resolved
    * total slide count broadly matches duration (±50%)
    * no slide has > MAX_BULLETS_PER_SLIDE bullets
    * no bullet has > MAX_WORDS_PER_BULLET words
    * presenter notes ≤ MAX_SENTENCES_PRESENTER_NOTES sentences
    * concept_ids and procedure_ids exist in the catalog
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        topic_id = plan.package_metadata.get("topic_concept_id", -1)
        if topic_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="topic concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no slides produced",
            ))
            return issues

        # Slide-count vs duration heuristic (warning, not error)
        duration = plan.package_metadata.get("duration_min", DEFAULT_DURATION_MIN)
        n_slides = len(plan.units)
        # Spec heuristic: 1 min/content slide + 20% buffer ⇒ 0.8x duration
        # is content; +4 fixed (title/agenda/takeaways/qa).
        target = max(8, int(duration * 0.8) + 4)
        if abs(n_slides - target) > target * 0.5:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"slide count {n_slides} far from duration target ~{target} "
                        f"(±50%) for {duration} min talk",
            ))

        all_concept_ids: set[int] = {topic_id}
        all_procedure_ids: set[int] = set()

        for u in plan.units:
            body = u.content_markdown or ""
            bullets = [
                line[2:].strip() for line in body.splitlines()
                if line.startswith("- ")
            ]
            if len(bullets) > MAX_BULLETS_PER_SLIDE:
                issues.append(ValidationIssue(
                    unit_logical_key=u.logical_key, severity="error",
                    message=f"slide '{u.name}' has {len(bullets)} bullets "
                            f"(max {MAX_BULLETS_PER_SLIDE})",
                ))
            for b in bullets:
                wc = _word_count_bullet(b)
                if wc > MAX_WORDS_PER_BULLET:
                    issues.append(ValidationIssue(
                        unit_logical_key=u.logical_key, severity="error",
                        message=f"bullet exceeds {MAX_WORDS_PER_BULLET} words "
                                f"({wc}): {b!r}",
                    ))

            # Presenter notes sentence cap
            notes_match = re.search(r"_Notes:_\s*(.*)$", body, flags=re.DOTALL)
            if notes_match:
                notes_text = notes_match.group(1).strip()
                sc = _sentence_count(notes_text)
                if sc > MAX_SENTENCES_PRESENTER_NOTES:
                    issues.append(ValidationIssue(
                        unit_logical_key=u.logical_key, severity="warning",
                        message=f"presenter notes have {sc} sentences "
                                f"(max {MAX_SENTENCES_PRESENTER_NOTES})",
                    ))

            for cid in u.metadata.get("concept_ids", []):
                all_concept_ids.add(int(cid))
            for pid in u.metadata.get("procedure_ids", []):
                all_procedure_ids.add(int(pid))

        # FK existence
        if all_concept_ids:
            ph = ",".join(["?"] * len(all_concept_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(all_concept_ids),
            ).fetchall()}
            missing = all_concept_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"concept_ids missing from catalog: {sorted(missing)[:5]}",
                ))
        if all_procedure_ids:
            ph = ",".join(["?"] * len(all_procedure_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT procedure_id FROM procedure WHERE procedure_id IN ({ph})",
                list(all_procedure_ids),
            ).fetchall()}
            missing = all_procedure_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"procedure_ids missing from catalog: {sorted(missing)[:5]}",
                ))

        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class SlideDeckMaterializer:
    """Writes the slides package to disk."""

    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?", [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)

        rows = conn.execute(
            "SELECT filename, content FROM generated_file "
            "WHERE package_id = ? ORDER BY file_id",
            [package_id],
        ).fetchall()

        written: list[str] = []
        skipped: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                skipped.append(str(target))
                continue
            target.write_text(content)
            written.append(str(target))

        notes = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files (overwrite=False)")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


# ---------------------------------------------------------------------------
# Convenience constructor
# ---------------------------------------------------------------------------


def make_slide_deck_generator() -> Generator:
    """Return a fully-wired Slide-deck Outline generator."""
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=RhetoricalArcDecomposer(),
        planner=SlideDeckPlanner(),
        ranking_mode="generation",
        validator=SlideDeckValidator(),
        materializer=SlideDeckMaterializer(),
    )
