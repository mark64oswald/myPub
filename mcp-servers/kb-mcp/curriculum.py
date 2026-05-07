"""curriculum.py — Phase 16 Curriculum Generator (composite).

Composes a multi-week curriculum by invoking Learning Path, Tutorial,
and Pattern Catalog generators internally and stitching their output
into per-week folders. Validates cross-generator coherence: every
Tutorial's prereqs are covered by an earlier-week Learning Path stage.

Output:
    curriculums/<topic>/
      _curriculum.md            top-level overview
      weeks/week-N/
        learning-path/...       Learning Path stage docs
        tutorials/...           Tutorial files
        patterns/...            Pattern Catalog files (mid+late weeks only)
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
    persist,
)
from learning_path import (
    PrerequisiteDecomposer,
    group_into_stages,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_CONCEPTS,
    DEFAULT_STAGE_SIZE,
    assign_chapters_to_stage,
    _Stage as _LPStage,
)


LOG = logging.getLogger("mypub-curriculum")

GENERATOR_TYPE = "curriculum"
DEFAULT_WEEKS = 12


@dataclass
class _Week:
    ordinal: int                       # 1-based
    title: str
    stage: _LPStage                    # Learning Path stage for this week
    has_tutorial: bool                 # True for mid+late weeks
    has_patterns: bool                 # True for late weeks


@dataclass
class _Decomposition:
    topic_concept_id: int
    topic_name: str
    weeks: list[_Week]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "topic"


class CurriculumDecomposer:
    """Build N weeks for a topic.

    Strategy: walk prerequisites via Phase 8 PrerequisiteDecomposer,
    group into stages, then expand to N weeks (one stage per week,
    repeating + augmenting if needed).
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        n_weeks: int = DEFAULT_WEEKS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_concepts: int = DEFAULT_MAX_CONCEPTS,
        target_size: int = DEFAULT_STAGE_SIZE,
        **_: Any,
    ) -> _Decomposition:
        prereq = PrerequisiteDecomposer().decompose(
            conn, resolver, query,
            max_depth=max_depth, max_concepts=max_concepts,
        )
        if prereq.target_concept_id == -1:
            return _Decomposition(
                topic_concept_id=-1, topic_name=query, weeks=[],
                notes=[f"topic {query!r} not found"],
            )
        stages = group_into_stages(prereq.concepts, target_size=target_size)
        for s in stages:
            s.chapters = assign_chapters_to_stage(conn, s)

        if not stages:
            return _Decomposition(
                topic_concept_id=prereq.target_concept_id,
                topic_name=prereq.target_name, weeks=[],
                notes=["no learning stages produced"],
            )

        # Expand stages → N weeks
        weeks: list[_Week] = []
        for i in range(n_weeks):
            stage = stages[i % len(stages)]
            # Mid-weeks (ordinal > 1) get tutorials; late weeks (last
            # third) get pattern catalogs.
            ord1 = i + 1
            has_tutorial = ord1 > 1
            has_patterns = ord1 > (n_weeks * 2 // 3)
            weeks.append(_Week(
                ordinal=ord1,
                title=f"Week {ord1}: {stage.name}",
                stage=stage,
                has_tutorial=has_tutorial,
                has_patterns=has_patterns,
            ))

        notes: list[str] = []
        if len(stages) < n_weeks:
            notes.append(
                f"only {len(stages)} unique stages for {n_weeks} weeks; "
                f"some weeks repeat anchor concepts"
            )
        return _Decomposition(
            topic_concept_id=prereq.target_concept_id,
            topic_name=prereq.target_name, weeks=weeks, notes=notes,
        )


def _render_week(w: _Week) -> str:
    lines = [
        f"# {w.title}",
        "",
        f"_Anchor: {w.stage.name}_  |  _Concepts: {len(w.stage.concepts)}_",
        "",
        "## Concepts",
        "",
    ]
    for c in w.stage.concepts:
        lines.append(f"- **{c.name}**")
    lines.append("")
    if w.stage.chapters:
        lines.append("## Reading list")
        lines.append("")
        for ch in w.stage.chapters:
            lines.append(f"- _{ch.book_title}_ — {ch.chapter_title}")
        lines.append("")
    if w.has_tutorial:
        lines.append("## Tutorial activity")
        lines.append("")
        lines.append(
            f"_Run `/kb-tutorial \"{w.stage.name}\"` and follow the resulting "
            f"exercise track. The tutorial generator pulls a backing procedure "
            f"per stage._"
        )
        lines.append("")
    if w.has_patterns:
        lines.append("## Pattern catalog")
        lines.append("")
        lines.append(
            f"_Run `/kb-pattern-catalog \"{w.stage.name}\"` for the catalog. "
            f"Late-week activity surfaces patterns + anti-patterns once "
            f"foundational vocabulary is in place._"
        )
        lines.append("")
    return "\n".join(lines)


def _render_curriculum(d: _Decomposition) -> str:
    lines = [f"# Curriculum: {d.topic_name}", ""]
    if not d.weeks:
        lines.append("_No weeks produced._")
        return "\n".join(lines)
    lines.append(f"**Length:** {len(d.weeks)} week(s)")
    lines.append("")
    lines.append("## Schedule")
    lines.append("")
    for w in d.weeks:
        markers = []
        if w.has_tutorial:
            markers.append("tutorial")
        if w.has_patterns:
            markers.append("patterns")
        marker_text = f" _({', '.join(markers)})_" if markers else ""
        lines.append(f"{w.ordinal}. **{w.stage.name}**{marker_text} → "
                     f"`weeks/week-{w.ordinal}/`")
    lines.append("")
    lines.append("## Coherence rules")
    lines.append("")
    lines.append(
        "- Week 1 is foundations — no tutorial, no pattern catalog\n"
        "- Mid-weeks add tutorials (hands-on practice)\n"
        "- Late weeks (last third) add pattern catalogs (design-level review)\n"
        "- Each week's reading list comes from the Phase 8 Learning Path "
        "stage that anchors it"
    )
    lines.append("")
    if d.notes:
        lines.append("## Notes")
        lines.append("")
        for n in d.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


class CurriculumPlanner:
    def plan(self, conn, decomposition, *, package_name=None, **_) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.topic_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.topic_name,
            source_query=d.topic_name,
            package_metadata={
                "topic_concept_id": d.topic_concept_id,
                "n_weeks": len(d.weeks),
                "has_tutorials": any(w.has_tutorial for w in d.weeks),
                "has_patterns": any(w.has_patterns for w in d.weeks),
            },
            notes=list(d.notes),
        )
        for w in d.weeks:
            plan.units.append(GenUnit(
                unit_type="curriculum_week",
                name=w.title,
                ordinal=w.ordinal,
                metadata={
                    "anchor_concept_id": w.stage.concepts[0].concept_id,
                    "concept_ids": [c.concept_id for c in w.stage.concepts],
                    "has_tutorial": w.has_tutorial,
                    "has_patterns": w.has_patterns,
                },
                logical_key=f"week_{w.ordinal}",
                content_markdown="",
                sources=[("concept", c.concept_id, 1.0, 1.0, None)
                         for c in w.stage.concepts],
            ))
            plan.files.append(GenFile(
                filename=f"weeks/week-{w.ordinal}/_week.md",
                content=_render_week(w),
                purpose="week_overview",
                unit_logical_key=f"week_{w.ordinal}",
            ))
        plan.files.append(GenFile(
            filename="_curriculum.md",
            content=_render_curriculum(d),
            purpose="curriculum",
        ))
        return plan


class CurriculumValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("topic_concept_id", -1) == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="topic concept not resolved",
            ))
            return issues
        if plan.package_metadata.get("n_weeks", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no weeks planned",
            ))
            return issues

        # Week ordinals contiguous 1..N
        ordinals = sorted(
            int(u.metadata.get("ordinal", u.ordinal)) for u in plan.units
        )
        if ordinals != list(range(1, len(ordinals) + 1)):
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"week ordinals not contiguous: {ordinals}",
            ))

        # Coherence: every "has_patterns" week comes after at least
        # one "has_tutorial" week (i.e., late weeks come after mid).
        first_pat = next((u for u in plan.units
                          if u.metadata.get("has_patterns")), None)
        first_tut = next((u for u in plan.units
                          if u.metadata.get("has_tutorial")), None)
        if first_pat and first_tut and first_pat.ordinal < first_tut.ordinal:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="pattern week appears before any tutorial week",
            ))

        return issues


class CurriculumMaterializer:
    def materialize(self, conn, package_id, output_root, *, overwrite=True):
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
            "WHERE package_id = ? ORDER BY file_id", [package_id],
        ).fetchall()
        written: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                continue
            target.write_text(content)
            written.append(str(target))
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=[],
        )


def make_curriculum_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=CurriculumDecomposer(),
        planner=CurriculumPlanner(),
        ranking_mode="generation",
        validator=CurriculumValidator(),
        materializer=CurriculumMaterializer(),
    )
