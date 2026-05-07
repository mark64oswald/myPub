"""tutorial.py — Phase 10 Tutorial Generator (deterministic v1).

Produces a sequenced hands-on tutorial from a target concept: walks
prerequisite stages, attaches one backing procedure per stage, renders
exercises with numbered command lists, and emits per-stage checkpoints
derived from procedure postconditions.

Reuses Phase 8's PrerequisiteDecomposer for the prereq traversal +
stage grouping, then filters stages to those with procedure backing
(no procedure ⇒ stage dropped or flagged). Renders procedure JSON
steps as readable numbered exercises.

Output:

    tutorials/<topic>/
      tutorial.md       sequenced exercise track (the deliverable)
      _setup.md         prerequisites checklist
      _checkpoints.md   per-section "you can do X if..." checks
"""
from __future__ import annotations

import json
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
from learning_path import (
    PrerequisiteDecomposer,
    group_into_stages,
    DEFAULT_MAX_DEPTH as PREREQ_DEFAULT_DEPTH,
    DEFAULT_MAX_CONCEPTS as PREREQ_DEFAULT_MAX,
    DEFAULT_STAGE_SIZE,
)


LOG = logging.getLogger("mypub-tutorial")


GENERATOR_TYPE = "tutorial"
DEFAULT_LEVEL = "intermediate"        # "beginner" | "intermediate" | "advanced"
DEFAULT_MAX_STAGES = 5
DEFAULT_MAX_STEPS_PER_EXERCISE = 8


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class _StageProcedure:
    procedure_id: int
    name: str
    preconditions: Optional[str]
    steps_raw: Optional[str]          # JSON-encoded list of step dicts
    postconditions: Optional[str]
    failure_modes: Optional[str]


@dataclass
class _TutorialStage:
    ordinal: int                       # 1-based
    slug: str
    title: str                         # anchor concept name
    concept_ids: list[int]
    procedure: Optional[_StageProcedure]  # backing procedure (may be None)
    other_concepts: list[str] = field(default_factory=list)


@dataclass
class _Decomposition:
    target_concept_id: int
    target_name: str
    target_concept_type: Optional[str]
    level: str
    stages: list[_TutorialStage]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer (reuses Phase 8 PrerequisiteDecomposer)
# ---------------------------------------------------------------------------


class ProcedureBackedDecomposer:
    """Walks prerequisites, then filters/orders stages by procedure
    availability — every stage in the tutorial has at least one backing
    procedure when possible.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        level: str = DEFAULT_LEVEL,
        max_depth: int = PREREQ_DEFAULT_DEPTH,
        max_concepts: int = PREREQ_DEFAULT_MAX,
        max_stages: int = DEFAULT_MAX_STAGES,
        target_size: int = DEFAULT_STAGE_SIZE,
        **_: Any,
    ) -> _Decomposition:
        # Reuse Phase 8 prereq decomposer
        prereq = PrerequisiteDecomposer().decompose(
            conn, resolver, query,
            max_depth=max_depth, max_concepts=max_concepts,
        )
        if prereq.target_concept_id == -1:
            return _Decomposition(
                target_concept_id=-1, target_name=query,
                target_concept_type=None, level=level,
                stages=[],
                notes=[f"target concept {query!r} not found"],
            )

        target_row = conn.execute(
            "SELECT name, concept_type FROM concept WHERE concept_id = ?",
            [prereq.target_concept_id],
        ).fetchone()

        # Group concepts into stages (reuse Phase 8 helper)
        prereq_stages = group_into_stages(prereq.concepts, target_size=target_size)
        # Cap to max_stages — keep deepest (foundations) and the last one
        # (target). Drop middle stages on overflow.
        if len(prereq_stages) > max_stages:
            head = prereq_stages[: max_stages - 1]
            tail = prereq_stages[-1]
            prereq_stages = head + [tail]

        # Per-stage: pick a backing procedure
        stages: list[_TutorialStage] = []
        for ps in prereq_stages:
            cids = [c.concept_id for c in ps.concepts]
            anchor = ps.concepts[0]
            proc = self._best_procedure_for_stage(conn, cids)
            stages.append(_TutorialStage(
                ordinal=ps.ordinal,
                slug=_slugify(anchor.name),
                title=anchor.name,
                concept_ids=cids,
                procedure=proc,
                other_concepts=[c.name for c in ps.concepts[1:]],
            ))

        notes: list[str] = []
        n_unbacked = sum(1 for s in stages if s.procedure is None)
        if n_unbacked > 0:
            notes.append(
                f"{n_unbacked} stage(s) have no backing procedure — exercises "
                f"will be conceptual only for those stages"
            )

        return _Decomposition(
            target_concept_id=prereq.target_concept_id,
            target_name=target_row[0],
            target_concept_type=target_row[1],
            level=level,
            stages=stages, notes=notes,
        )

    @staticmethod
    def _best_procedure_for_stage(
        conn: duckdb.DuckDBPyConnection, concept_ids: list[int],
    ) -> Optional[_StageProcedure]:
        if not concept_ids:
            return None
        ph = ",".join(["?"] * len(concept_ids))
        # Procedure that covers the most stage concepts; tie-break by
        # shorter (more cheatsheet-friendly) steps content.
        rows = conn.execute(
            f"""
            SELECT p.procedure_id, p.name, p.preconditions, p.steps,
                   p.postconditions, p.failure_modes,
                   COUNT(DISTINCT pc.concept_id) AS hits,
                   length(COALESCE(p.steps, '')) AS step_len
              FROM procedure p
              JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
             WHERE pc.concept_id IN ({ph})
               AND p.steps IS NOT NULL
               AND length(trim(p.steps)) > 0
             GROUP BY p.procedure_id, p.name, p.preconditions, p.steps,
                      p.postconditions, p.failure_modes
             ORDER BY hits DESC, step_len ASC
             LIMIT 1
            """,
            concept_ids,
        ).fetchone()
        if not rows:
            return None
        return _StageProcedure(
            procedure_id=int(rows[0]), name=rows[1] or "(unnamed)",
            preconditions=rows[2], steps_raw=rows[3],
            postconditions=rows[4], failure_modes=rows[5],
        )


# ---------------------------------------------------------------------------
# Step rendering helpers (parses procedure JSON)
# ---------------------------------------------------------------------------


def _parse_steps(raw: Optional[str], *, max_steps: int = DEFAULT_MAX_STEPS_PER_EXERCISE) -> list[dict]:
    """Return the parsed step list (capped). Falls back to a single
    pseudo-step for plain-text steps."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return [{"n": 1, "action": raw.strip()[:300]}]
    if not isinstance(parsed, list):
        return [{"n": 1, "action": str(parsed)[:300]}]
    return parsed[:max_steps]


def _render_exercise_steps(steps: list[dict]) -> str:
    """Render parsed steps as a numbered exercise. Each step's action
    is the prose; `command` (if present) shows as a code line."""
    if not steps:
        return "_No exercise steps for this stage; this is a conceptual checkpoint._\n"
    lines: list[str] = []
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        action = re.sub(r"\s+", " ", str(step.get("action") or "")).strip()
        cmd = re.sub(r"\s+", " ", str(step.get("command") or "")).strip()
        notes = re.sub(r"\s+", " ", str(step.get("notes") or "")).strip()
        if action:
            lines.append(f"{idx}. {action}")
        elif cmd:
            lines.append(f"{idx}. (run command below)")
        else:
            continue
        if cmd:
            lines.append("")
            lines.append("   ```")
            lines.append(f"   {cmd}")
            lines.append("   ```")
        if notes:
            lines.append(f"   _Note:_ {notes}")
        lines.append("")
    return "\n".join(lines)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "stage"


def _short(text: str, limit: int = 200) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_tutorial(d: _Decomposition) -> str:
    lines = [
        f"# Tutorial: {d.target_name}",
        "",
        f"**Level:** {d.level}  |  **Stages:** {len(d.stages)}",
        "",
        "## How this tutorial works",
        "",
        "Each stage walks a prerequisite step toward the target. Most "
        "stages have a hands-on exercise drawn from a procedure in the "
        "knowledge base. Run the exercises in order; check off each "
        "stage's checkpoint before moving on.",
        "",
    ]
    for s in d.stages:
        lines.append(f"## Stage {s.ordinal}: {s.title}")
        lines.append("")
        if s.other_concepts:
            lines.append(
                "_Concepts in this stage:_ "
                + ", ".join(f"**{n}**" for n in [s.title] + s.other_concepts)
            )
            lines.append("")
        if s.procedure:
            p = s.procedure
            lines.append(f"### Exercise: {p.name}")
            lines.append("")
            if p.preconditions and p.preconditions.strip():
                lines.append(f"_Preconditions:_ {_short(p.preconditions)}")
                lines.append("")
            steps = _parse_steps(p.steps_raw)
            lines.append(_render_exercise_steps(steps))
            if p.postconditions and p.postconditions.strip():
                lines.append(f"_When you finish:_ {_short(p.postconditions)}")
                lines.append("")
            if p.failure_modes and p.failure_modes.strip():
                lines.append(f"_Watch out for:_ {_short(p.failure_modes)}")
                lines.append("")
        else:
            lines.append(
                "_No backing procedure in the corpus for this stage. "
                "Treat this as a conceptual review of the listed concepts._"
            )
            lines.append("")
    return "\n".join(lines)


def _render_setup(d: _Decomposition) -> str:
    lines = [
        f"# Setup: {d.target_name}",
        "",
        "Before starting the tutorial, ensure these prerequisites are met.",
        "",
        "## Required knowledge",
        "",
    ]
    for s in d.stages:
        if s.ordinal == len(d.stages):
            continue  # last stage = target itself
        lines.append(f"- Familiar with **{s.title}**")
    lines.append("")
    lines.append("## Tooling preconditions")
    lines.append("")
    seen = set()
    for s in d.stages:
        if s.procedure and s.procedure.preconditions:
            pre = s.procedure.preconditions.strip()
            if pre and pre not in seen:
                seen.add(pre)
                lines.append(f"- {_short(pre, 220)}")
    if not seen:
        lines.append(
            "_No procedure-derived preconditions; standard development "
            "environment assumed._"
        )
    lines.append("")
    return "\n".join(lines)


def _render_checkpoints(d: _Decomposition) -> str:
    lines = [
        f"# Checkpoints: {d.target_name}",
        "",
        "After each stage, verify you can do the listed checks before "
        "moving on. Checkpoints are derived from procedure post-conditions.",
        "",
    ]
    for s in d.stages:
        lines.append(f"## Stage {s.ordinal}: {s.title}")
        lines.append("")
        if s.procedure and s.procedure.postconditions:
            for line in s.procedure.postconditions.splitlines():
                line = line.strip()
                if not line:
                    continue
                bullet = line.lstrip("-* •").strip()
                if bullet:
                    lines.append(f"- [ ] {_short(bullet, 220)}")
        else:
            lines.append(
                "- [ ] Can describe the concepts covered in this stage in your "
                "own words"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner / Validator / Materializer
# ---------------------------------------------------------------------------


class TutorialPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.target_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.target_name,
            source_query=d.target_name,
            package_metadata={
                "target_concept_id": d.target_concept_id,
                "level": d.level,
                "n_stages": len(d.stages),
                "n_unbacked_stages": sum(1 for s in d.stages if s.procedure is None),
            },
            notes=list(d.notes),
        )
        for s in d.stages:
            sources: list[tuple[str, int, float, float, Optional[str]]] = [
                ("concept", cid, 1.0, 1.0, None) for cid in s.concept_ids
            ]
            if s.procedure:
                sources.append(("procedure", s.procedure.procedure_id,
                                  1.0, 1.0, None))
            plan.units.append(GenUnit(
                unit_type="tutorial_stage",
                name=f"Stage {s.ordinal}: {s.title}",
                ordinal=s.ordinal,
                metadata={
                    "stage_slug": s.slug,
                    "concept_ids": s.concept_ids,
                    "procedure_id": s.procedure.procedure_id if s.procedure else None,
                    "has_exercise": s.procedure is not None,
                },
                logical_key=f"stage_{s.ordinal}",
                content_markdown=(
                    f"Stage with {len(s.concept_ids)} concept(s); "
                    + ("has exercise" if s.procedure else "conceptual only")
                ),
                sources=sources,
            ))
        plan.files.extend([
            GenFile(filename="tutorial.md", content=_render_tutorial(d), purpose="tutorial"),
            GenFile(filename="_setup.md", content=_render_setup(d), purpose="setup"),
            GenFile(filename="_checkpoints.md",
                    content=_render_checkpoints(d), purpose="checkpoints"),
        ])
        return plan


class TutorialValidator:
    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        target_id = plan.package_metadata.get("target_concept_id", -1)
        if target_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="target concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no stages produced",
            ))
            return issues

        # Stage ordinals contiguous
        ordinals = sorted(int(u.metadata.get("ordinal", u.ordinal))
                          for u in plan.units)
        if ordinals != list(range(1, len(ordinals) + 1)):
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"stage ordinals not contiguous: {ordinals}",
            ))

        # FK existence on concept_ids and procedure_ids
        cids: set[int] = set()
        pids: set[int] = set()
        for u in plan.units:
            for c in u.metadata.get("concept_ids", []):
                cids.add(int(c))
            pid = u.metadata.get("procedure_id")
            if pid is not None:
                pids.add(int(pid))

        if cids:
            ph = ",".join(["?"] * len(cids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(cids),
            ).fetchall()}
            missing = cids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"concept_ids missing: {sorted(missing)[:5]}",
                ))
        if pids:
            ph = ",".join(["?"] * len(pids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT procedure_id FROM procedure WHERE procedure_id IN ({ph})",
                list(pids),
            ).fetchall()}
            missing = pids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"procedure_ids missing: {sorted(missing)[:5]}",
                ))

        # Warning for stages without exercises
        n_unbacked = plan.package_metadata.get("n_unbacked_stages", 0)
        if n_unbacked >= len(plan.units) // 2:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"{n_unbacked} of {len(plan.units)} stages lack backing "
                        f"procedures — tutorial is mostly conceptual",
            ))
        return issues


class TutorialMaterializer:
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


def make_tutorial_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=ProcedureBackedDecomposer(),
        planner=TutorialPlanner(),
        ranking_mode="generation",
        validator=TutorialValidator(),
        materializer=TutorialMaterializer(),
    )
