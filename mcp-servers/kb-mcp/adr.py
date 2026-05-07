"""adr.py — Phase 12 ADR Generator (deterministic; ranking_mode=interactive).

Produces an Architecture Decision Record from a decision question.
The decomposer finds the question's anchor concept + its CONTRASTS_WITH
neighbors → those become the candidate options. Per-option pros/cons
are heuristically derived from concept descriptions + ranked source
excerpts.

Output:
    adr/<slug>/
      adr.md            Context / Options / Pros & Cons / Decision template
      _options.md       Per-option deep dive
      _references.md    Source bibliography
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

LOG = logging.getLogger("mypub-adr")

GENERATOR_TYPE = "adr"
DEFAULT_MAX_OPTIONS = 5
DEFAULT_MAX_REFERENCES = 4


@dataclass
class _Option:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    chapter_count: int
    doc_section_count: int
    references: list[str] = field(default_factory=list)


@dataclass
class _Decomposition:
    anchor_concept_id: int
    anchor_concept_name: str
    question: str
    options: list[_Option]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "decision"


def _short(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


class ContrastsDecomposer:
    """Find candidate options by walking CONTRASTS_WITH from the anchor."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        max_options: int = DEFAULT_MAX_OPTIONS,
        max_references: int = DEFAULT_MAX_REFERENCES,
        **_: Any,
    ) -> _Decomposition:
        anchor_id = resolver.resolve_lookup_only(query)
        if anchor_id is None:
            return _Decomposition(
                anchor_concept_id=-1, anchor_concept_name=query,
                question=query, options=[],
                notes=[f"anchor concept {query!r} not found"],
            )
        row = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [anchor_id],
        ).fetchone()
        anchor_name = row[0]

        # Anchor + its CONTRASTS_WITH neighbors are the option candidates.
        # Always include the anchor itself as the first option ("status
        # quo / use the named approach as-is").
        candidates: list[int] = [anchor_id]
        rows = conn.execute(
            """
            SELECT DISTINCT
                CASE WHEN cr.from_concept_id = ? THEN cr.to_concept_id
                     ELSE cr.from_concept_id END AS other_id
              FROM concept_relation cr
             WHERE cr.relation_type = 'CONTRASTS_WITH'
               AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
            """,
            [anchor_id, anchor_id, anchor_id],
        ).fetchall()
        for r in rows:
            cid = int(r[0])
            if cid != anchor_id and cid not in candidates:
                candidates.append(cid)
            if len(candidates) >= max_options:
                break

        # Enrich each option
        options: list[_Option] = []
        for cid in candidates[:max_options]:
            opt_row = conn.execute(
                "SELECT name, concept_type, description FROM concept "
                "WHERE concept_id = ?", [cid],
            ).fetchone()
            chap_n = conn.execute(
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
            sec_n = conn.execute(
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
            ref_rows = conn.execute(
                """
                SELECT b.title, c.title, COUNT(*) AS m
                  FROM concept_relation cr
                  JOIN chapter c ON c.chapter_id = cr.source_id
                  JOIN book    b ON b.book_id = c.book_id
                 WHERE cr.source_type = 'chapter'
                   AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                 GROUP BY b.title, c.title
                 ORDER BY m DESC
                 LIMIT ?
                """,
                [cid, cid, max_references],
            ).fetchall()
            references = [
                f"{r[0]} — {r[1]} ({r[2]} mention(s))" for r in ref_rows
            ]
            options.append(_Option(
                concept_id=cid, name=opt_row[0],
                concept_type=opt_row[1], description=opt_row[2],
                chapter_count=int(chap_n), doc_section_count=int(sec_n),
                references=references,
            ))

        notes: list[str] = []
        if len(options) <= 1:
            notes.append(
                f"only one option candidate found ({anchor_name!r}); "
                f"no CONTRASTS_WITH alternatives in the corpus"
            )

        return _Decomposition(
            anchor_concept_id=anchor_id, anchor_concept_name=anchor_name,
            question=query, options=options,
        )


def _render_adr(d: _Decomposition) -> str:
    lines = [
        f"# ADR: {d.question}",
        "",
        "## Status",
        "",
        "Proposed",
        "",
        "## Context",
        "",
        f"This decision concerns **{d.anchor_concept_name}**.",
        "The corpus surfaces these candidate options based on "
        "CONTRASTS_WITH edges between concepts in the knowledge base.",
        "",
        "## Options",
        "",
    ]
    for i, opt in enumerate(d.options, start=1):
        lines.append(f"### Option {i}: {opt.name}")
        lines.append("")
        lines.append(_short(opt.description) or "_no description in corpus_")
        lines.append("")
        lines.append(
            f"_Coverage:_ {opt.chapter_count} chapter(s), "
            f"{opt.doc_section_count} doc section(s)"
        )
        lines.append("")
    lines.extend([
        "## Pros & Cons",
        "",
        "_(Use the per-option deep dives in `_options.md` to fill in. "
        "Coverage counts are a rough proxy for the corpus's confidence "
        "in each option.)_",
        "",
        "## Decision",
        "",
        "_TBD — choose an option above and document the rationale here._",
        "",
        "## Consequences",
        "",
        "_What changes after this decision? What gets harder; what gets easier?_",
        "",
    ])
    return "\n".join(lines)


def _render_options(d: _Decomposition) -> str:
    lines = [f"# {d.question} — Option Deep Dives", ""]
    for i, opt in enumerate(d.options, start=1):
        lines.append(f"## {i}. {opt.name}")
        lines.append("")
        if opt.description:
            lines.append(opt.description.strip())
            lines.append("")
        lines.append(
            f"**Type:** {opt.concept_type or 'concept'}  |  "
            f"**Chapters:** {opt.chapter_count}  |  "
            f"**Doc sections:** {opt.doc_section_count}"
        )
        lines.append("")
        if opt.references:
            lines.append("### References")
            lines.append("")
            for ref in opt.references:
                lines.append(f"- {ref}")
            lines.append("")
    return "\n".join(lines)


def _render_references(d: _Decomposition) -> str:
    lines = [f"# {d.question} — References", ""]
    for opt in d.options:
        if opt.references:
            lines.append(f"## {opt.name}")
            lines.append("")
            for ref in opt.references:
                lines.append(f"- {ref}")
            lines.append("")
    return "\n".join(lines)


class ADRPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.question)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.question,
            source_query=d.question,
            package_metadata={
                "anchor_concept_id": d.anchor_concept_id,
                "n_options": len(d.options),
            },
            notes=list(d.notes),
        )
        for i, opt in enumerate(d.options, start=1):
            plan.units.append(GenUnit(
                unit_type="adr_option",
                name=opt.name,
                ordinal=i,
                metadata={
                    "concept_id": opt.concept_id,
                    "chapter_count": opt.chapter_count,
                    "doc_section_count": opt.doc_section_count,
                },
                logical_key=f"option_{i}",
                content_markdown=_short(opt.description) or "",
                sources=[("concept", opt.concept_id, 1.0, 1.0, None)],
            ))
        plan.files.extend([
            GenFile(filename="adr.md", content=_render_adr(d), purpose="adr"),
            GenFile(filename="_options.md", content=_render_options(d),
                    purpose="options"),
            GenFile(filename="_references.md", content=_render_references(d),
                    purpose="references"),
        ])
        return plan


class ADRValidator:
    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        anchor = plan.package_metadata.get("anchor_concept_id", -1)
        if anchor == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="anchor concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no options produced",
            ))
            return issues
        if len(plan.units) <= 1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"only {len(plan.units)} option (no CONTRASTS_WITH "
                        f"alternatives in corpus); ADR is single-position",
            ))

        # FK existence
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
                    message=f"option concept_ids missing: {sorted(missing)[:5]}",
                ))
        return issues


class ADRMaterializer:
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


def make_adr_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=ContrastsDecomposer(),
        planner=ADRPlanner(),
        ranking_mode="interactive",
        validator=ADRValidator(),
        materializer=ADRMaterializer(),
    )
