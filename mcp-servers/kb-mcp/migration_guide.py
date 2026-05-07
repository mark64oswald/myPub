"""migration_guide.py — Phase 13 Migration Guide generator.

Builds an "era diff" guide from CONTRADICTS edges between book content
and current doc content. The catalog currently has 0 CONTRADICTS edges
(alignment runs produced only CORROBORATES). The infrastructure ships;
the substrate signal grows when contradicting source material lands.

Output:
    migration-guides/<subject>/
      _migration.md     era-by-era diffs (may be empty)
      _superseded.md    deprecated patterns / approaches
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

LOG = logging.getLogger("mypub-migration-guide")

GENERATOR_TYPE = "migration_guide"


@dataclass
class _Diff:
    concept_id: int
    concept_name: str
    book_chapter_id: Optional[int]
    book_label: str
    doc_section_id: Optional[int]
    doc_label: str
    explanation: str


@dataclass
class _Decomposition:
    subject_concept_id: int
    subject_name: str
    diffs: list[_Diff]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "subject"


class ContradictsDecomposer:
    """Walks alignment_edge rows of relation_type=CONTRADICTS that
    involve concepts in the subject's neighborhood."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        max_depth: int = 1,
        **_: Any,
    ) -> _Decomposition:
        sid = resolver.resolve_lookup_only(query)
        if sid is None:
            return _Decomposition(
                subject_concept_id=-1, subject_name=query,
                diffs=[],
                notes=[f"subject {query!r} not found"],
            )
        row = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [sid],
        ).fetchone()
        subject_name = row[0]

        # Subject + its 1-hop neighbors are the candidate concept set
        from collections import deque
        seen = {sid}
        frontier = deque([(sid, 0)])
        while frontier:
            cid, d = frontier.popleft()
            if d >= max_depth:
                continue
            rows = conn.execute(
                """
                SELECT to_concept_id FROM concept_relation WHERE from_concept_id = ?
                UNION SELECT from_concept_id FROM concept_relation WHERE to_concept_id = ?
                """,
                [cid, cid],
            ).fetchall()
            for (n,) in rows:
                n = int(n)
                if n not in seen:
                    seen.add(n)
                    frontier.append((n, d + 1))

        # Walk CONTRADICTS edges referencing any of those concepts
        if not seen:
            return _Decomposition(
                subject_concept_id=sid, subject_name=subject_name,
                diffs=[], notes=["subject has no neighbors"],
            )
        ph = ",".join(["?"] * len(seen))
        rows = conn.execute(
            f"""
            SELECT ae.concept_id, c.name,
                   ae.from_doc_section_id, ae.to_chapter_id, ae.to_doc_section_id,
                   COALESCE(ae.explanation, '')
              FROM alignment_edge ae
              JOIN concept c ON c.concept_id = ae.concept_id
             WHERE ae.relation_type = 'CONTRADICTS'
               AND ae.concept_id IN ({ph})
            """,
            list(seen),
        ).fetchall()

        diffs: list[_Diff] = []
        for r in rows:
            cid, cname, from_sec, to_ch, to_sec, expl = (
                int(r[0]), r[1], r[2], r[3], r[4], r[5],
            )
            doc_label = "(doc)"
            if from_sec is not None:
                row2 = conn.execute(
                    """
                    SELECT ds.name, s.heading_text
                      FROM doc_section s
                      JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
                      JOIN doc_source ds ON sn.doc_source_id = ds.doc_source_id
                     WHERE s.doc_section_id = ?
                    """, [from_sec],
                ).fetchone()
                if row2:
                    doc_label = f"{row2[0]} — {row2[1] or '(no heading)'}"

            book_label = "(book)"
            book_chapter_id = None
            if to_ch is not None:
                row3 = conn.execute(
                    "SELECT b.title, c.title FROM chapter c "
                    "JOIN book b ON b.book_id = c.book_id WHERE c.chapter_id = ?",
                    [to_ch],
                ).fetchone()
                if row3:
                    book_label = f"{row3[0]} — {row3[1]}"
                book_chapter_id = int(to_ch)

            diffs.append(_Diff(
                concept_id=cid, concept_name=cname,
                book_chapter_id=book_chapter_id, book_label=book_label,
                doc_section_id=int(from_sec) if from_sec is not None else None,
                doc_label=doc_label,
                explanation=(expl or "").strip(),
            ))

        notes: list[str] = []
        if not diffs:
            notes.append(
                "no CONTRADICTS edges in the subject's neighborhood — the "
                "guide is data-starved. The infrastructure works; the "
                "substrate signal grows as alignment runs produce more "
                "contradictory edges."
            )

        return _Decomposition(
            subject_concept_id=sid, subject_name=subject_name,
            diffs=diffs, notes=notes,
        )


def _render_migration(d: _Decomposition) -> str:
    lines = [f"# Migration Guide: {d.subject_name}", ""]
    if not d.diffs:
        lines.extend([
            "_No CONTRADICTS edges found for this subject._",
            "",
            "**Why this is empty:** Migration guides depend on CONTRADICTS "
            "edges in the alignment graph. As of this generation, the "
            "catalog has only CORROBORATES edges between books and current "
            "docs. The infrastructure works; the signal grows as alignment "
            "runs detect contradictions between book-era content and "
            "doc-era content.",
            "",
            "**To grow the substrate:** run `/kb-discover` for fresh "
            "doc_sources and re-run alignment with prompts tuned to "
            "surface contradictions explicitly.",
            "",
        ])
        return "\n".join(lines)
    lines.append(f"_{len(d.diffs)} contradiction(s) found between book and doc content._")
    lines.append("")
    by_concept: dict[str, list[_Diff]] = {}
    for diff in d.diffs:
        by_concept.setdefault(diff.concept_name, []).append(diff)
    for concept_name, diffs in by_concept.items():
        lines.append(f"## {concept_name}")
        lines.append("")
        for diff in diffs:
            lines.append(f"- **Book:** {diff.book_label}")
            lines.append(f"  **Docs:** {diff.doc_label}")
            if diff.explanation:
                lines.append(f"  {diff.explanation}")
        lines.append("")
    return "\n".join(lines)


def _render_superseded(d: _Decomposition) -> str:
    lines = [f"# {d.subject_name} — Superseded Patterns", ""]
    if not d.diffs:
        lines.append("_No superseded patterns recorded — see `_migration.md` for context._")
        return "\n".join(lines)
    seen: set[int] = set()
    for diff in d.diffs:
        if diff.concept_id in seen:
            continue
        seen.add(diff.concept_id)
        lines.append(f"## {diff.concept_name}")
        lines.append(f"- Book era: {diff.book_label}")
        lines.append(f"- Doc era:  {diff.doc_label}")
        lines.append("")
    return "\n".join(lines)


class MigrationPlanner:
    def plan(
        self, conn, decomposition, *, package_name=None, **_,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.subject_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.subject_name,
            source_query=d.subject_name,
            package_metadata={
                "subject_concept_id": d.subject_concept_id,
                "n_diffs": len(d.diffs),
            },
            notes=list(d.notes),
        )
        for i, diff in enumerate(d.diffs, start=1):
            sources: list[tuple[str, int, float, float, Optional[str]]] = [
                ("concept", diff.concept_id, 1.0, 1.0, None),
            ]
            if diff.book_chapter_id is not None:
                sources.append(("chapter", diff.book_chapter_id, 1.0, 1.0, None))
            if diff.doc_section_id is not None:
                sources.append(("doc_section", diff.doc_section_id, 1.0, 1.0, None))
            plan.units.append(GenUnit(
                unit_type="migration_diff",
                name=diff.concept_name,
                ordinal=i,
                metadata={
                    "concept_id": diff.concept_id,
                    "book_chapter_id": diff.book_chapter_id,
                    "doc_section_id": diff.doc_section_id,
                },
                logical_key=f"diff_{i}",
                content_markdown=diff.explanation,
                sources=sources,
            ))
        plan.files.extend([
            GenFile(filename="_migration.md", content=_render_migration(d), purpose="migration"),
            GenFile(filename="_superseded.md", content=_render_superseded(d), purpose="superseded"),
        ])
        return plan


class MigrationValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("subject_concept_id", -1) == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="subject concept not resolved",
            ))
            return issues
        if plan.package_metadata.get("n_diffs", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="data-starved: no CONTRADICTS edges for this subject",
            ))
        return issues


class MigrationMaterializer:
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


def make_migration_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=ContradictsDecomposer(),
        planner=MigrationPlanner(),
        ranking_mode="interactive",
        validator=MigrationValidator(),
        materializer=MigrationMaterializer(),
    )
