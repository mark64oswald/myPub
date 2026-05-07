"""refactoring_playbook.py — Phase 15 Refactoring Playbook (deterministic).

Detects anti-patterns (concepts with concept_type='Pattern' or
'Anti-Pattern' that have CONTRASTS_WITH neighbors named in the
catalog) and produces refactor steps from the contrasting pattern's
procedures.

Output:
    refactor-playbooks/<topic>/
      _findings.md           anti-pattern findings + recommended refactors
      refactors/<slug>.md    per-finding refactor steps
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

LOG = logging.getLogger("mypub-refactoring-playbook")

GENERATOR_TYPE = "refactoring_playbook"


@dataclass
class _Finding:
    anti_concept_id: int
    anti_name: str
    anti_description: Optional[str]
    target_concept_id: int
    target_name: str
    target_description: Optional[str]
    refactor_procedure_ids: list[int] = field(default_factory=list)
    refactor_procedure_names: list[str] = field(default_factory=list)


@dataclass
class _Decomposition:
    topic_concept_id: int
    topic_name: str
    findings: list[_Finding]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "topic"


def _short(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


class AntiPatternDecomposer:
    """Walk the topic's neighborhood; for any Pattern-typed concept with
    CONTRASTS_WITH neighbors, record both as a refactor finding."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        max_findings: int = 12,
        **_: Any,
    ) -> _Decomposition:
        tid = resolver.resolve_lookup_only(query)
        if tid is None:
            return _Decomposition(
                topic_concept_id=-1, topic_name=query,
                findings=[],
                notes=[f"topic {query!r} not found"],
            )
        row = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [tid],
        ).fetchone()
        topic_name = row[0]

        # Find Pattern-typed neighbors, then walk their CONTRASTS_WITH edges
        rows = conn.execute(
            """
            WITH neighbors AS (
              SELECT to_concept_id AS cid FROM concept_relation WHERE from_concept_id = ?
              UNION SELECT from_concept_id FROM concept_relation WHERE to_concept_id = ?
            )
            SELECT c.concept_id FROM neighbors n JOIN concept c ON c.concept_id = n.cid
             WHERE c.concept_type IN ('Pattern', 'Anti-Pattern', 'Technique')
            """,
            [tid, tid],
        ).fetchall()
        pattern_ids = {int(r[0]) for r in rows}
        if not pattern_ids:
            pattern_ids = {tid}  # at least try the topic itself

        # For each pattern, find CONTRASTS_WITH partners that we'll suggest
        # as the refactor target.
        findings: list[_Finding] = []
        for pid in list(pattern_ids)[:max_findings * 2]:
            partners = conn.execute(
                """
                SELECT DISTINCT
                    CASE WHEN cr.from_concept_id = ? THEN cr.to_concept_id
                         ELSE cr.from_concept_id END AS pid
                  FROM concept_relation cr
                 WHERE cr.relation_type = 'CONTRASTS_WITH'
                   AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                """,
                [pid, pid, pid],
            ).fetchall()
            if not partners:
                continue
            partner_id = int(partners[0][0])
            anti_row = conn.execute(
                "SELECT name, description FROM concept WHERE concept_id = ?",
                [pid],
            ).fetchone()
            tgt_row = conn.execute(
                "SELECT name, description FROM concept WHERE concept_id = ?",
                [partner_id],
            ).fetchone()
            # Refactor procedures: from the target (recommended) pattern
            proc_rows = conn.execute(
                """
                SELECT p.procedure_id, p.name FROM procedure p
                 JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
                 WHERE pc.concept_id = ?
                   AND p.steps IS NOT NULL
                 ORDER BY length(p.steps) ASC
                 LIMIT 3
                """,
                [partner_id],
            ).fetchall()
            findings.append(_Finding(
                anti_concept_id=pid, anti_name=anti_row[0],
                anti_description=anti_row[1],
                target_concept_id=partner_id, target_name=tgt_row[0],
                target_description=tgt_row[1],
                refactor_procedure_ids=[int(r[0]) for r in proc_rows],
                refactor_procedure_names=[r[1] or "(unnamed)" for r in proc_rows],
            ))
            if len(findings) >= max_findings:
                break

        notes: list[str] = []
        if not findings:
            notes.append(
                "no anti-pattern findings — no Pattern-typed concept in the "
                "topic's neighborhood has CONTRASTS_WITH edges. Try a "
                "broader topic with more pattern coverage."
            )

        return _Decomposition(
            topic_concept_id=tid, topic_name=topic_name,
            findings=findings, notes=notes,
        )


def _render_findings(d: _Decomposition) -> str:
    lines = [f"# {d.topic_name} — Refactoring Playbook", ""]
    if not d.findings:
        lines.append("_No anti-pattern findings for this topic._")
        return "\n".join(lines)
    lines.append(f"_{len(d.findings)} finding(s) — each pairs an anti-pattern "
                 "with a recommended refactor target._")
    lines.append("")
    for i, f in enumerate(d.findings, start=1):
        lines.append(f"## Finding {i}: {f.anti_name} → {f.target_name}")
        lines.append("")
        if f.anti_description:
            lines.append(f"**Anti-pattern:** {_short(f.anti_description)}")
        if f.target_description:
            lines.append(f"**Refactor to:** {_short(f.target_description)}")
        lines.append(f"**Refactor procedures available:** "
                     f"{len(f.refactor_procedure_ids)}")
        lines.append(f"**See:** `refactors/{_slugify(f.anti_name)}-to-"
                     f"{_slugify(f.target_name)}.md`")
        lines.append("")
    return "\n".join(lines)


def _render_refactor(f: _Finding) -> str:
    lines = [
        f"# Refactor: {f.anti_name} → {f.target_name}",
        "",
        "## Anti-pattern context",
        "",
        f.anti_description or "_No description_",
        "",
        "## Target pattern",
        "",
        f.target_description or "_No description_",
        "",
        "## Refactor procedures",
        "",
    ]
    if f.refactor_procedure_names:
        for name in f.refactor_procedure_names:
            lines.append(f"- {name}")
    else:
        lines.append(
            "_No procedures linked to the target pattern in the corpus. "
            "Refactor steps would need to be authored manually._"
        )
    lines.append("")
    return "\n".join(lines)


class RefactoringPlanner:
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
                "n_findings": len(d.findings),
            },
            notes=list(d.notes),
        )
        for i, f in enumerate(d.findings, start=1):
            sources: list[tuple[str, int, float, float, Optional[str]]] = [
                ("concept", f.anti_concept_id, 1.0, 1.0, None),
                ("concept", f.target_concept_id, 1.0, 1.0, None),
            ] + [
                ("procedure", pid, 1.0, 1.0, None)
                for pid in f.refactor_procedure_ids
            ]
            plan.units.append(GenUnit(
                unit_type="refactor_finding",
                name=f"{f.anti_name} → {f.target_name}",
                ordinal=i,
                metadata={
                    "anti_concept_id": f.anti_concept_id,
                    "target_concept_id": f.target_concept_id,
                    "n_procedures": len(f.refactor_procedure_ids),
                },
                logical_key=f"finding_{i}",
                content_markdown="",
                sources=sources,
            ))
            plan.files.append(GenFile(
                filename=f"refactors/{_slugify(f.anti_name)}-to-"
                         f"{_slugify(f.target_name)}.md",
                content=_render_refactor(f),
                purpose="refactor",
                unit_logical_key=f"finding_{i}",
            ))
        plan.files.append(GenFile(
            filename="_findings.md", content=_render_findings(d), purpose="findings",
        ))
        return plan


class RefactoringValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("topic_concept_id", -1) == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="topic concept not resolved",
            ))
            return issues
        if plan.package_metadata.get("n_findings", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no anti-pattern findings; playbook is empty",
            ))
        return issues


class RefactoringMaterializer:
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


def make_refactoring_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=AntiPatternDecomposer(),
        planner=RefactoringPlanner(),
        ranking_mode="interactive",
        validator=RefactoringValidator(),
        materializer=RefactoringMaterializer(),
    )
