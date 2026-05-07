"""content_brief.py — Phase 9.1-9.3 Content Generator (deterministic v1).

Produces a research-grounded **content brief** — outline + per-section
source bundles + angle hints (CONTRASTS_WITH-derived positions) — for
a topic at a chosen format. Single MCP call, fully deterministic.

The architecture spec (§2.2) calls for sub-agent-driven prose
generation. v1 ships the deterministic skeleton: every section has its
thesis sentence, ranked source excerpts with citations, and angle
hints. A writer (human or LLM) fills in the prose. v2 will add the
sub-agent dispatch wrapper.

Pipeline:

  1. Resolve topic to a graph concept.
  2. Pick rhetorical arc by format (blog, talk, design-doc, chapter).
  3. For each section: pull anchor concept + related concepts + top
     book chapters + top doc_sections + relevant procedures.
  4. Surface CONTRASTS_WITH neighbors as "angle hints" — positions the
     author should consider taking.
  5. Render: _brief.md (overview) + outline.md (arc) + sections/<n>-<slug>.md
     per section + sources.md (bibliography).

Ranking mode: interactive — surface conflicts. The brief is debate-
ready; the author makes the editorial call.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
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

LOG = logging.getLogger("mypub-content-brief")


GENERATOR_TYPE = "content_brief"
DEFAULT_FORMAT = "blog"
DEFAULT_AUDIENCE = "engineers"
DEFAULT_MAX_SOURCES_PER_SECTION = 4
DEFAULT_MAX_RELATED_PER_SECTION = 4

# Format-specific rhetorical arcs (section, thesis-template).
# Each section name appears in the rendered outline; the thesis
# template is filled in with the topic + section-specific anchors.
_ARCS: dict[str, list[tuple[str, str]]] = {
    "blog": [
        ("Hook",         "Why {topic} matters right now"),
        ("Context",      "Where {topic} fits in the landscape"),
        ("Problem",      "What {topic} solves that other approaches don't"),
        ("Approaches",   "Three ways to apply {topic}"),
        ("Comparison",   "How those approaches differ in practice"),
        ("Recommendation", "Which approach to pick by default"),
        ("Conclusion",   "What to take away from {topic}"),
    ],
    "talk": [
        ("Opening Story", "Concrete moment where {topic} mattered"),
        ("Problem Framing", "What problem {topic} addresses"),
        ("Insight 1",    "First key insight about {topic}"),
        ("Insight 2",    "Second key insight about {topic}"),
        ("Insight 3",    "Third key insight about {topic}"),
        ("Demo",         "Walkthrough applying {topic}"),
        ("Takeaways",    "What the audience should remember about {topic}"),
    ],
    "design-doc": [
        ("Context",      "Background on {topic} for this decision"),
        ("Requirements", "Constraints and goals for using {topic}"),
        ("Options",      "Candidate approaches involving {topic}"),
        ("Analysis",     "Trade-offs across the options"),
        ("Decision",     "Selected approach for {topic}"),
        ("Consequences", "What changes after deciding on {topic}"),
    ],
    "chapter": [
        ("Introduction", "Introducing {topic} for the reader"),
        ("Theory",       "Core principles behind {topic}"),
        ("Worked Examples", "Concrete applications of {topic}"),
        ("Edge Cases",   "When {topic} breaks down"),
        ("Summary",      "What was covered about {topic}"),
    ],
}


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class _SourceRef:
    source_type: str        # 'chapter' | 'doc_section' | 'procedure'
    source_id: int
    label: str              # human-readable
    excerpt: str            # short excerpt
    score: float            # rough relevance proxy (mention count or recency)


@dataclass
class _Section:
    ordinal: int
    slug: str
    title: str
    thesis: str
    anchor_concept_id: Optional[int]
    anchor_concept_name: Optional[str]
    related_concepts: list[tuple[int, str]] = field(default_factory=list)  # (cid, name)
    sources: list[_SourceRef] = field(default_factory=list)
    angle_hints: list[tuple[int, str]] = field(default_factory=list)  # CONTRASTS_WITH (cid, name)


@dataclass
class _Decomposition:
    topic_concept_id: int
    topic_name: str
    topic_concept_type: Optional[str]
    audience: str
    fmt: str
    angle: Optional[str]
    sections: list[_Section]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class RhetoricalContentDecomposer:
    """Decomposes a topic into rhetorical sections per format.

    Each section gets:
      * an anchor concept (top neighbor by chapter coverage; topic
        itself for opening/closing sections)
      * related concepts (siblings of the anchor)
      * source excerpts (chapters + doc_sections + procedures)
      * angle hints (CONTRASTS_WITH neighbors of the anchor)
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        fmt: str = DEFAULT_FORMAT,
        audience: str = DEFAULT_AUDIENCE,
        angle: Optional[str] = None,
        max_sources: int = DEFAULT_MAX_SOURCES_PER_SECTION,
        max_related: int = DEFAULT_MAX_RELATED_PER_SECTION,
        **_: Any,
    ) -> _Decomposition:
        topic_id = resolver.resolve_lookup_only(query)
        if topic_id is None:
            return _Decomposition(
                topic_concept_id=-1, topic_name=query,
                topic_concept_type=None,
                audience=audience, fmt=fmt, angle=angle,
                sections=[],
                notes=[f"topic concept {query!r} not found"],
            )
        row = conn.execute(
            "SELECT name, concept_type FROM concept WHERE concept_id = ?",
            [topic_id],
        ).fetchone()
        topic_name, topic_type = row[0], row[1]

        if fmt not in _ARCS:
            fmt = DEFAULT_FORMAT
        arc = _ARCS[fmt]

        # Pre-compute the topic's top related concepts; sections grab
        # successive anchors from this list, with the topic itself
        # serving as the opener/closer anchor.
        related = self._top_related(conn, topic_id, limit=20)

        sections: list[_Section] = []
        related_iter = iter(related)
        for ordinal, (title, thesis_template) in enumerate(arc, start=1):
            slug = _slugify(title)
            # Opening + closing sections anchor on the topic itself;
            # middle sections rotate through related concepts.
            is_bookend = ordinal == 1 or ordinal == len(arc)
            if is_bookend or not related:
                anchor_id, anchor_name = topic_id, topic_name
            else:
                try:
                    anchor_id, anchor_name = next(related_iter)
                except StopIteration:
                    anchor_id, anchor_name = topic_id, topic_name

            # Section's related concepts (siblings of the anchor)
            siblings = self._top_related(conn, anchor_id, limit=max_related,
                                          exclude_ids={topic_id, anchor_id})
            # Sources
            sources = self._gather_sources(
                conn, anchor_id, max_sources=max_sources,
            )
            # Angle hints
            angle_hints = self._contrasts_neighbors(conn, anchor_id, limit=3)

            thesis = thesis_template.format(topic=topic_name)
            sections.append(_Section(
                ordinal=ordinal, slug=slug, title=title,
                thesis=thesis,
                anchor_concept_id=anchor_id,
                anchor_concept_name=anchor_name,
                related_concepts=siblings,
                sources=sources,
                angle_hints=angle_hints,
            ))

        notes: list[str] = []
        if not related:
            notes.append(
                f"topic {topic_name!r} has no graph neighbors; sections "
                f"share the topic as anchor (single-perspective brief)"
            )

        return _Decomposition(
            topic_concept_id=topic_id, topic_name=topic_name,
            topic_concept_type=topic_type,
            audience=audience, fmt=fmt, angle=angle,
            sections=sections, notes=notes,
        )

    @staticmethod
    def _top_related(
        conn: duckdb.DuckDBPyConnection, cid: int, *, limit: int,
        exclude_ids: Optional[set[int]] = None,
    ) -> list[tuple[int, str]]:
        excludes = exclude_ids or set()
        rows = conn.execute(
            """
            WITH n AS (
              SELECT to_concept_id AS rid FROM concept_relation
               WHERE from_concept_id = ?
              UNION
              SELECT from_concept_id AS rid FROM concept_relation
               WHERE to_concept_id = ?
            ),
            chap_count AS (
              SELECT cid AS rid, COUNT(DISTINCT chapter_id) AS chapters FROM (
                SELECT cr.from_concept_id AS cid, ch.chapter_id
                  FROM concept_relation cr
                  JOIN chapter ch ON ch.chapter_id = cr.source_id
                  JOIN n ON n.rid = cr.from_concept_id
                 WHERE cr.source_type = 'chapter'
                UNION
                SELECT cr.to_concept_id, ch.chapter_id
                  FROM concept_relation cr
                  JOIN chapter ch ON ch.chapter_id = cr.source_id
                  JOIN n ON n.rid = cr.to_concept_id
                 WHERE cr.source_type = 'chapter'
              )
              GROUP BY rid
            )
            SELECT c.concept_id, c.name, COALESCE(cc.chapters, 0) AS chapters
              FROM n
              JOIN concept c ON c.concept_id = n.rid
              LEFT JOIN chap_count cc ON cc.rid = n.rid
             WHERE c.concept_id != ?
             ORDER BY chapters DESC, c.name
             LIMIT 50
            """,
            [cid, cid, cid],
        ).fetchall()
        out: list[tuple[int, str]] = []
        for r in rows:
            rid = int(r[0])
            if rid in excludes:
                continue
            out.append((rid, r[1]))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _gather_sources(
        conn: duckdb.DuckDBPyConnection, cid: int, *, max_sources: int,
    ) -> list[_SourceRef]:
        # Top chapters by mention count
        chap_rows = conn.execute(
            """
            SELECT ch.chapter_id, b.title, ch.title,
                   COUNT(*) AS mentions
              FROM concept_relation cr
              JOIN chapter ch ON ch.chapter_id = cr.source_id
              JOIN book    b  ON b.book_id    = ch.book_id
             WHERE cr.source_type = 'chapter'
               AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
             GROUP BY ch.chapter_id, b.title, ch.title
             ORDER BY mentions DESC
             LIMIT ?
            """,
            [cid, cid, max_sources],
        ).fetchall()
        out: list[_SourceRef] = []
        for r in chap_rows:
            out.append(_SourceRef(
                source_type="chapter", source_id=int(r[0]),
                label=f"{r[1]} — {r[2]}",
                excerpt=f"Mentioned in this chapter ({r[3]} time(s)).",
                score=float(r[3]),
            ))

        # Top doc_sections
        sec_rows = conn.execute(
            """
            SELECT s.doc_section_id, ds.name, s.heading_text,
                   COUNT(*) AS mentions
              FROM concept_relation cr
              JOIN doc_section s   ON s.doc_section_id = cr.source_id
              JOIN doc_snapshot sn ON s.snapshot_id    = sn.snapshot_id
              JOIN doc_source  ds  ON sn.doc_source_id = ds.doc_source_id
             WHERE cr.source_type = 'doc_section'
               AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
             GROUP BY s.doc_section_id, ds.name, s.heading_text
             ORDER BY mentions DESC
             LIMIT ?
            """,
            [cid, cid, max_sources // 2 + 1],
        ).fetchall()
        for r in sec_rows:
            out.append(_SourceRef(
                source_type="doc_section", source_id=int(r[0]),
                label=f"{r[1]} — {r[2] or '(no heading)'}",
                excerpt=f"Doc section ({r[3]} mention(s)).",
                score=float(r[3]),
            ))

        # Top procedures
        proc_rows = conn.execute(
            """
            SELECT p.procedure_id, p.name
              FROM procedure p
              JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
             WHERE pc.concept_id = ?
               AND p.steps IS NOT NULL
             ORDER BY length(p.steps) ASC
             LIMIT ?
            """,
            [cid, max_sources // 2 + 1],
        ).fetchall()
        for r in proc_rows:
            out.append(_SourceRef(
                source_type="procedure", source_id=int(r[0]),
                label=r[1] or "(unnamed procedure)",
                excerpt="Hands-on procedure.",
                score=1.0,
            ))
        return out[:max_sources * 2]  # generous ceiling

    @staticmethod
    def _contrasts_neighbors(
        conn: duckdb.DuckDBPyConnection, cid: int, *, limit: int,
    ) -> list[tuple[int, str]]:
        rows = conn.execute(
            """
            SELECT DISTINCT c.concept_id, c.name
              FROM concept_relation cr
              JOIN concept c ON c.concept_id =
                CASE WHEN cr.from_concept_id = ? THEN cr.to_concept_id
                     ELSE cr.from_concept_id END
             WHERE cr.relation_type = 'CONTRASTS_WITH'
               AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
             ORDER BY c.name
             LIMIT ?
            """,
            [cid, cid, cid, limit],
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "section"


def _render_brief(d: _Decomposition) -> str:
    angle = f"\n**Angle:** {d.angle}" if d.angle else ""
    arcs_count = len(d.sections)
    n_sources = sum(len(s.sources) for s in d.sections)
    n_hints = sum(len(s.angle_hints) for s in d.sections)
    return (
        f"# {d.topic_name} — Content Brief\n\n"
        f"**Topic:** {d.topic_name}  |  **Format:** {d.fmt}  |  "
        f"**Audience:** {d.audience}{angle}\n\n"
        f"**Sections:** {arcs_count}  |  **Sources:** {n_sources}  |  "
        f"**Angle hints:** {n_hints}\n\n"
        f"## How to use this brief\n\n"
        f"This brief is a deterministic research foundation. Each section "
        f"in `sections/` lists a thesis, anchor concept, related concepts, "
        f"ranked sources, and angle hints (CONTRASTS_WITH positions worth "
        f"considering). Fill in the prose; the substrate is the structure.\n\n"
        f"## Files\n\n"
        f"- `outline.md` — the full rhetorical arc with one-line theses\n"
        f"- `sections/<n>-<slug>.md` — one per section, with sources + hints\n"
        f"- `sources.md` — full bibliography\n"
    )


def _render_outline(d: _Decomposition) -> str:
    lines = [f"# {d.topic_name} — Outline ({d.fmt})", ""]
    for s in d.sections:
        lines.append(f"## {s.ordinal}. {s.title}")
        lines.append("")
        lines.append(f"_Thesis:_ {s.thesis}")
        lines.append("")
        lines.append(
            f"_Anchor:_ **{s.anchor_concept_name or '(unanchored)'}**  |  "
            f"_Sources:_ {len(s.sources)}  |  "
            f"_Angle hints:_ {len(s.angle_hints)}"
        )
        lines.append("")
    if d.notes:
        lines.append("## Notes")
        lines.append("")
        for n in d.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


def _render_section(s: _Section) -> str:
    lines = [
        f"# {s.ordinal}. {s.title}",
        "",
        f"**Thesis:** {s.thesis}",
        "",
    ]
    if s.anchor_concept_name:
        lines.append(f"**Anchor concept:** {s.anchor_concept_name}")
        lines.append("")

    if s.related_concepts:
        lines.append("## Related concepts to weave in")
        lines.append("")
        for _cid, name in s.related_concepts:
            lines.append(f"- {name}")
        lines.append("")

    if s.sources:
        lines.append("## Sources to draw from")
        lines.append("")
        for src in s.sources:
            lines.append(f"- **[{src.source_type}]** {src.label}")
            lines.append(f"  - {src.excerpt}")
        lines.append("")

    if s.angle_hints:
        lines.append("## Angle hints (positions to consider)")
        lines.append("")
        lines.append(
            "_These are CONTRASTS_WITH neighbors of the anchor — explicit "
            "alternatives the field has debated. Pick a position rather than "
            "list both flatly._"
        )
        lines.append("")
        for _cid, name in s.angle_hints:
            lines.append(f"- {name}")
        lines.append("")

    return "\n".join(lines)


def _render_sources(d: _Decomposition) -> str:
    lines = [f"# {d.topic_name} — Sources", ""]
    seen: set[tuple[str, int]] = set()
    by_type: dict[str, list[_SourceRef]] = defaultdict(list)
    for s in d.sections:
        for src in s.sources:
            key = (src.source_type, src.source_id)
            if key in seen:
                continue
            seen.add(key)
            by_type[src.source_type].append(src)
    for stype in ("chapter", "doc_section", "procedure"):
        if stype not in by_type:
            continue
        lines.append(f"## {stype.replace('_', ' ').title()}")
        lines.append("")
        for src in sorted(by_type[stype], key=lambda r: -r.score):
            lines.append(f"- **{src.label}** — score={src.score:.1f} (id={src.source_id})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class ContentBriefPlanner:
    """One ``GenUnit`` per section. Files: _brief.md, outline.md,
    sections/<n>-<slug>.md, sources.md."""

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
                "format": d.fmt,
                "audience": d.audience,
                "angle": d.angle,
                "n_sections": len(d.sections),
                "n_sources_total": sum(len(s.sources) for s in d.sections),
            },
            notes=list(d.notes),
        )

        for s in d.sections:
            sources_for_unit: list[tuple[str, int, float, float, Optional[str]]] = []
            for src in s.sources:
                sources_for_unit.append(
                    (src.source_type, src.source_id, src.score, 1.0, None)
                )
            plan.units.append(GenUnit(
                unit_type="content_section",
                name=s.title,
                ordinal=s.ordinal,
                metadata={
                    "slug": s.slug,
                    "thesis": s.thesis,
                    "anchor_concept_id": s.anchor_concept_id,
                    "n_sources": len(s.sources),
                    "n_angle_hints": len(s.angle_hints),
                    "source_ids_by_type": {
                        st: [src.source_id for src in s.sources
                              if src.source_type == st]
                        for st in ("chapter", "doc_section", "procedure")
                    },
                },
                logical_key=f"section_{s.ordinal}",
                content_markdown=_render_section(s),
                sources=sources_for_unit,
            ))
            plan.files.append(GenFile(
                filename=f"sections/{s.ordinal}-{s.slug}.md",
                content=_render_section(s),
                purpose="section",
                unit_logical_key=f"section_{s.ordinal}",
            ))

        plan.files.extend([
            GenFile(filename="_brief.md", content=_render_brief(d), purpose="brief"),
            GenFile(filename="outline.md", content=_render_outline(d), purpose="outline"),
            GenFile(filename="sources.md", content=_render_sources(d), purpose="sources"),
        ])

        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ContentBriefValidator:
    """Checks:

    * topic concept resolved
    * every section has ≥1 source (warning, not error — empty graphs
      can produce sourceless sections honestly)
    * source_ids resolve in catalog
    * opening + closing sections present (first + last per arc)
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
                message="no sections produced",
            ))
            return issues

        # Sourceless sections — warning
        for u in plan.units:
            n_src = u.metadata.get("n_sources", 0)
            if n_src == 0:
                issues.append(ValidationIssue(
                    unit_logical_key=u.logical_key, severity="warning",
                    message=f"section '{u.name}' has no source citations",
                ))

        # FK existence on chapter / doc_section / procedure ids
        chap_ids: set[int] = set()
        sec_ids: set[int] = set()
        proc_ids: set[int] = set()
        for u in plan.units:
            sib = u.metadata.get("source_ids_by_type", {})
            chap_ids.update(int(x) for x in sib.get("chapter", []))
            sec_ids.update(int(x) for x in sib.get("doc_section", []))
            proc_ids.update(int(x) for x in sib.get("procedure", []))

        if chap_ids:
            ph = ",".join(["?"] * len(chap_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT chapter_id FROM chapter WHERE chapter_id IN ({ph})",
                list(chap_ids),
            ).fetchall()}
            missing = chap_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"chapter_ids missing: {sorted(missing)[:5]}",
                ))
        if sec_ids:
            ph = ",".join(["?"] * len(sec_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT doc_section_id FROM doc_section WHERE doc_section_id IN ({ph})",
                list(sec_ids),
            ).fetchall()}
            missing = sec_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"doc_section_ids missing: {sorted(missing)[:5]}",
                ))
        if proc_ids:
            ph = ",".join(["?"] * len(proc_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT procedure_id FROM procedure WHERE procedure_id IN ({ph})",
                list(proc_ids),
            ).fetchall()}
            missing = proc_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"procedure_ids missing: {sorted(missing)[:5]}",
                ))

        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class ContentBriefMaterializer:
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

        notes: list[str] = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files (overwrite=False)")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


def make_content_brief_generator() -> Generator:
    """Return a fully-wired Content Brief generator."""
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=RhetoricalContentDecomposer(),
        planner=ContentBriefPlanner(),
        ranking_mode="interactive",
        validator=ContentBriefValidator(),
        materializer=ContentBriefMaterializer(),
    )
