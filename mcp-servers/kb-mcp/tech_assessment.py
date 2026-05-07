"""tech_assessment.py — Phase 12 Tech Assessment Generator.

Produces a uniform feature-matrix comparison across N candidate
technologies. For each candidate, the decomposer pulls a metric tuple
from the graph (chapter coverage, doc_section count, neighborhood size,
top procedure count, last doc snapshot age). The matrix and per-
candidate deep dives ship as one package.

Output:
    tech-assessment/<slug>/
      _matrix.md          comparison table
      candidates/<slug>.md  one per technology
      _recommendation.md  deterministic pick (highest weighted score)
"""
from __future__ import annotations

import logging
import re
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

LOG = logging.getLogger("mypub-tech-assessment")

GENERATOR_TYPE = "tech_assessment"


@dataclass
class _Candidate:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    chapter_count: int
    doc_section_count: int
    neighborhood_size: int
    procedure_count: int
    has_current_docs: bool
    composite_score: float


@dataclass
class _Decomposition:
    title: str                          # canonical assessment title
    candidates: list[_Candidate]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "tech"


def _short(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


class FeatureMatrixDecomposer:
    """Compute uniform metrics for each candidate."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        candidates: Optional[list[str]] = None,
        title: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        # ``query`` is the assessment title; ``candidates`` is the list
        # of technology names to compare. If candidates is missing, we
        # treat ``query`` as a comma-separated list.
        if candidates is None:
            candidates = [c.strip() for c in (query or "").split(",") if c.strip()]
        actual_title = title or query or "Tech Assessment"

        out: list[_Candidate] = []
        notes: list[str] = []
        for name in candidates:
            cid = resolver.resolve_lookup_only(name)
            if cid is None:
                notes.append(f"candidate {name!r} not found in corpus; skipped")
                continue
            row = conn.execute(
                "SELECT name, concept_type, description FROM concept "
                "WHERE concept_id = ?", [cid],
            ).fetchone()
            chap = conn.execute(
                """
                SELECT COUNT(DISTINCT chapter_id) FROM (
                  SELECT cr.from_concept_id AS cid, ch.chapter_id
                    FROM concept_relation cr
                    JOIN chapter ch ON ch.chapter_id = cr.source_id
                   WHERE cr.source_type = 'chapter' AND cr.from_concept_id = ?
                  UNION
                  SELECT cr.to_concept_id, ch.chapter_id
                    FROM concept_relation cr
                    JOIN chapter ch ON ch.chapter_id = cr.source_id
                   WHERE cr.source_type = 'chapter' AND cr.to_concept_id = ?
                )
                """,
                [cid, cid],
            ).fetchone()[0] or 0
            sec = conn.execute(
                """
                SELECT COUNT(DISTINCT doc_section_id) FROM (
                  SELECT cr.from_concept_id AS cid, ds.doc_section_id
                    FROM concept_relation cr
                    JOIN doc_section ds ON ds.doc_section_id = cr.source_id
                   WHERE cr.source_type = 'doc_section' AND cr.from_concept_id = ?
                  UNION
                  SELECT cr.to_concept_id, ds.doc_section_id
                    FROM concept_relation cr
                    JOIN doc_section ds ON ds.doc_section_id = cr.source_id
                   WHERE cr.source_type = 'doc_section' AND cr.to_concept_id = ?
                )
                """,
                [cid, cid],
            ).fetchone()[0] or 0
            nb = conn.execute(
                """
                SELECT COUNT(DISTINCT cid) FROM (
                  SELECT to_concept_id AS cid FROM concept_relation WHERE from_concept_id = ?
                  UNION SELECT from_concept_id FROM concept_relation WHERE to_concept_id = ?
                )
                """,
                [cid, cid],
            ).fetchone()[0] or 0
            proc = conn.execute(
                "SELECT COUNT(*) FROM procedure_concept WHERE concept_id = ?",
                [cid],
            ).fetchone()[0] or 0
            has_current_docs = sec > 0

            # Composite score: weighted normalisation. Picks scale-free
            # weights that play nicely across small corpora.
            composite = (
                chap * 1.0
                + sec * 3.0           # current docs weighted higher
                + min(nb, 30) * 0.5
                + proc * 1.5
            )
            out.append(_Candidate(
                concept_id=cid, name=row[0],
                concept_type=row[1], description=row[2],
                chapter_count=int(chap), doc_section_count=int(sec),
                neighborhood_size=int(nb), procedure_count=int(proc),
                has_current_docs=has_current_docs,
                composite_score=float(composite),
            ))

        if not out:
            notes.append("no candidates resolved; assessment is empty")

        out.sort(key=lambda c: -c.composite_score)
        return _Decomposition(title=actual_title, candidates=out, notes=notes)


def _render_matrix(d: _Decomposition) -> str:
    lines = [f"# {d.title} — Comparison Matrix", ""]
    if not d.candidates:
        lines.append("_No candidates resolved._")
        return "\n".join(lines)
    lines.extend([
        "| Technology | Type | Chapters | Doc sections | Neighbors | Procedures | Current? | Score |",
        "|---|---|---:|---:|---:|---:|:---:|---:|",
    ])
    for c in d.candidates:
        current = "✓" if c.has_current_docs else "—"
        lines.append(
            f"| **{c.name}** | {c.concept_type or '—'} | "
            f"{c.chapter_count} | {c.doc_section_count} | "
            f"{c.neighborhood_size} | {c.procedure_count} | "
            f"{current} | {c.composite_score:.1f} |"
        )
    lines.append("")
    lines.append("_Score = chapters + 3×doc_sections + 0.5×min(neighbors, 30) "
                 "+ 1.5×procedures. Higher = better-supported by the corpus._")
    return "\n".join(lines)


def _render_candidate(c: _Candidate) -> str:
    lines = [
        f"# {c.name}",
        "",
    ]
    if c.concept_type:
        lines.append(f"_Type: {c.concept_type}_")
        lines.append("")
    if c.description:
        lines.append("## Summary")
        lines.append("")
        lines.append(c.description.strip())
        lines.append("")
    lines.extend([
        "## Coverage profile",
        "",
        f"- **Book chapters:** {c.chapter_count}",
        f"- **Current doc sections:** {c.doc_section_count}",
        f"- **Concept-graph neighbors:** {c.neighborhood_size}",
        f"- **Procedures:** {c.procedure_count}",
        f"- **Live docs available:** {'yes' if c.has_current_docs else 'no'}",
        "",
        f"**Composite score:** {c.composite_score:.1f}",
        "",
    ])
    return "\n".join(lines)


def _render_recommendation(d: _Decomposition) -> str:
    lines = [f"# {d.title} — Recommendation", ""]
    if not d.candidates:
        lines.append("_No candidates to compare._")
        return "\n".join(lines)
    winner = d.candidates[0]
    lines.append(f"## Recommended: **{winner.name}**")
    lines.append("")
    lines.append(
        f"_Score: {winner.composite_score:.1f}. "
        f"{'Has current docs.' if winner.has_current_docs else 'No current docs.'}_"
    )
    lines.append("")
    lines.append("### Why")
    lines.append("")
    lines.append(
        f"- {winner.chapter_count} chapter(s) cover this technology in the corpus"
    )
    if winner.has_current_docs:
        lines.append(
            f"- {winner.doc_section_count} doc section(s) from current vendor docs"
        )
    if winner.procedure_count:
        lines.append(
            f"- {winner.procedure_count} procedure(s) available for hands-on use"
        )
    lines.append("")
    if len(d.candidates) > 1:
        lines.append("### Caveats")
        lines.append("")
        lines.append(
            "_The recommendation is a corpus-coverage signal, not an "
            "endorsement. The corpus may over-represent established "
            "technologies and under-represent newcomers; consider running "
            "`/kb-discover` on candidates to update the doc snapshots._"
        )
        lines.append("")
        lines.append("Runners-up:")
        for c in d.candidates[1:]:
            lines.append(f"- **{c.name}** — score {c.composite_score:.1f}")
        lines.append("")
    return "\n".join(lines)


class TechAssessmentPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.title)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.title,
            source_query=d.title,
            package_metadata={
                "n_candidates": len(d.candidates),
                "winner_concept_id": d.candidates[0].concept_id if d.candidates else None,
                "winner_score": d.candidates[0].composite_score if d.candidates else 0.0,
            },
            notes=list(d.notes),
        )
        for i, c in enumerate(d.candidates, start=1):
            plan.units.append(GenUnit(
                unit_type="tech_candidate",
                name=c.name,
                ordinal=i,
                metadata={
                    "concept_id": c.concept_id,
                    "score": c.composite_score,
                    "has_current_docs": c.has_current_docs,
                },
                logical_key=f"candidate_{c.concept_id}",
                content_markdown=_short(c.description) or "",
                sources=[("concept", c.concept_id, c.composite_score, 1.0, None)],
            ))
            plan.files.append(GenFile(
                filename=f"candidates/{_slugify(c.name)}.md",
                content=_render_candidate(c),
                purpose="candidate",
                unit_logical_key=f"candidate_{c.concept_id}",
            ))
        plan.files.extend([
            GenFile(filename="_matrix.md", content=_render_matrix(d), purpose="matrix"),
            GenFile(filename="_recommendation.md",
                    content=_render_recommendation(d), purpose="recommendation"),
        ])
        return plan


class TechAssessmentValidator:
    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no candidates resolved",
            ))
            return issues
        if len(plan.units) < 2:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="only one candidate — assessment is single-pick",
            ))
        ids = {int(u.metadata.get("concept_id"))
               for u in plan.units if u.metadata.get("concept_id") is not None}
        if ids:
            ph = ",".join(["?"] * len(ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(ids),
            ).fetchall()}
            missing = ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"candidate concept_ids missing: {sorted(missing)[:5]}",
                ))
        return issues


class TechAssessmentMaterializer:
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
            notes.append(f"skipped {len(skipped)} existing files")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


def make_tech_assessment_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=FeatureMatrixDecomposer(),
        planner=TechAssessmentPlanner(),
        ranking_mode="interactive",
        validator=TechAssessmentValidator(),
        materializer=TechAssessmentMaterializer(),
    )
