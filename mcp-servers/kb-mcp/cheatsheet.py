"""cheatsheet.py — Phase 9.4 Cheatsheet generator (deterministic v1).

Produces a one-page distilled reference for a subject (library, tool,
or technology) — procedures grouped by category with their canonical
steps, plus a failure-modes "gotchas" section. The deliverable is a
single self-contained Markdown page optimized for fast lookup.

Pipeline:

  1. Resolve subject to a graph concept
  2. Pull every procedure linked to subject + EXTENDS descendants
     via procedure_concept
  3. Cluster procedures by keyword-derived category (CRUD,
     Configuration, Performance, Errors, Integration, General)
  4. Per cluster, distill: render the procedure's name + preconditions
     + the first 200 chars of steps (the "canonical command" /
     short example)
  5. Aggregate failure_modes across procedures into a Gotchas section
  6. Cap to ≤1200 words / ≤8 sections per the architecture spec

Deterministic v1 — no sub-agent dispatch, no LLM distillation. The
"canonical command" extraction from prose is LLM work; v1 surfaces
the procedure's own steps verbatim (truncated). Future v2 can layer
sub-agent prose to compress steps to a single command line.
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

LOG = logging.getLogger("mypub-cheatsheet")


GENERATOR_TYPE = "cheatsheet"
DEFAULT_MAX_WORDS = 1200
DEFAULT_MAX_SECTIONS = 8
DEFAULT_STEPS_EXCERPT = 200       # chars of procedure.steps to keep per entry
DEFAULT_MAX_PROCS_PER_SECTION = 6
DEFAULT_EXTENDS_DEPTH = 1         # how far to walk EXTENDS for descendant pull


# ---------------------------------------------------------------------------
# Category taxonomy
# ---------------------------------------------------------------------------


# Keyword → section. Order matters: first-match wins.
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("CRUD",          ["create", "drop", "delete", "insert", "update",
                       "select", "alter", "add column", "rename"]),
    ("Configuration", ["configure", "config", "set option", "set flag",
                       "set parameter", "settings", "environment variable",
                       "enable", "disable"]),
    ("Performance",   ["tune", "optimize", "performance", "index", "vacuum",
                       "compact", "partition", "cache", "parallel"]),
    ("Errors",        ["error", "exception", "fail", "retry", "rollback",
                       "recover", "debug", "troubleshoot"]),
    ("Integration",   ["connect", "integrate", "import", "export",
                       "load from", "write to", "register", "publish"]),
    ("Install",       ["install", "setup", "bootstrap", "initialize"]),
    ("Operations",    ["start", "stop", "restart", "deploy", "monitor"]),
]

DEFAULT_CATEGORY = "General"


def _classify_procedure(name: str, steps: str) -> str:
    """Pick the first category whose keywords appear in the procedure
    name (preferred) or the first 200 chars of steps."""
    haystack = (name or "").lower() + "\n" + (steps or "")[:200].lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return category
    return DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Data shape from the decomposer
# ---------------------------------------------------------------------------


@dataclass
class _Procedure:
    procedure_id: int
    name: str
    preconditions: Optional[str]
    steps: Optional[str]
    failure_modes: Optional[str]
    source_type: Optional[str]      # 'chapter' or 'doc_section'
    source_id: Optional[int]
    source_label: Optional[str]     # human-readable book/chapter or doc_source/heading


@dataclass
class _Cluster:
    category: str
    procedures: list[_Procedure] = field(default_factory=list)


@dataclass
class _Decomposition:
    subject_concept_id: int
    subject_name: str
    subject_concept_type: Optional[str]
    n_procedures_total: int          # before per-section cap
    clusters: list[_Cluster] = field(default_factory=list)
    failure_modes: list[tuple[str, str]] = field(default_factory=list)  # (procedure_name, failure_modes_text)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class TopicalCondenseDecomposer:
    """Topical-condensation decomposer.

    Pulls every procedure attached to the subject concept (and
    optional EXTENDS descendants), classifies each by keyword, and
    surfaces failure_modes for the gotchas section. Deterministic.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        extends_depth: int = DEFAULT_EXTENDS_DEPTH,
        max_per_section: int = DEFAULT_MAX_PROCS_PER_SECTION,
        **_: Any,
    ) -> _Decomposition:
        subject_id = resolver.resolve_lookup_only(query)
        if subject_id is None:
            return _Decomposition(
                subject_concept_id=-1, subject_name=query,
                subject_concept_type=None,
                n_procedures_total=0,
                notes=[f"subject concept {query!r} not found"],
            )
        subject_row = conn.execute(
            "SELECT name, concept_type FROM concept WHERE concept_id = ?",
            [subject_id],
        ).fetchone()

        # Walk EXTENDS to gather descendant concepts (subject + things
        # that EXTEND it; useful for tools like "Apache Spark" whose
        # specialty libraries also have procedures).
        concept_ids = self._extends_closure(conn, subject_id, extends_depth)

        ph = ",".join(["?"] * len(concept_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT p.procedure_id, p.name, p.preconditions, p.steps,
                            p.failure_modes, p.source_type, p.source_id
              FROM procedure p
              JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
             WHERE pc.concept_id IN ({ph})
               AND (p.steps IS NOT NULL AND length(trim(p.steps)) > 0)
            """,
            list(concept_ids),
        ).fetchall()

        procedures = [
            _Procedure(
                procedure_id=int(r[0]), name=r[1] or "(unnamed procedure)",
                preconditions=r[2], steps=r[3], failure_modes=r[4],
                source_type=r[5], source_id=r[6],
                source_label=self._render_source_label(conn, r[5], r[6]),
            )
            for r in rows
        ]

        # Cluster.
        by_category: dict[str, list[_Procedure]] = {}
        for p in procedures:
            cat = _classify_procedure(p.name, p.steps or "")
            by_category.setdefault(cat, []).append(p)

        # Order categories by the taxonomy order, with General last.
        category_order = [c for c, _ in _CATEGORY_KEYWORDS] + [DEFAULT_CATEGORY]
        clusters: list[_Cluster] = []
        for cat in category_order:
            if cat not in by_category:
                continue
            # Within a category: prefer procedures with shortest steps
            # (more cheatsheet-friendly), then by procedure_id for
            # determinism.
            by_category[cat].sort(
                key=lambda p: (len((p.steps or "")[:DEFAULT_STEPS_EXCERPT]),
                               p.procedure_id),
            )
            clusters.append(_Cluster(
                category=cat,
                procedures=by_category[cat][:max_per_section],
            ))

        # Failure modes — aggregate (name, text) for any procedure that
        # has them.
        failure_modes: list[tuple[str, str]] = [
            (p.name, p.failure_modes.strip())
            for p in procedures
            if p.failure_modes and p.failure_modes.strip()
        ]
        # Cap at 10 to avoid bloating the page.
        failure_modes = failure_modes[:10]

        notes: list[str] = []
        if not procedures:
            notes.append(
                f"no procedures linked to {subject_row[0]!r} or its "
                f"EXTENDS descendants"
            )

        return _Decomposition(
            subject_concept_id=subject_id,
            subject_name=subject_row[0],
            subject_concept_type=subject_row[1],
            n_procedures_total=len(procedures),
            clusters=clusters,
            failure_modes=failure_modes,
            notes=notes,
        )

    @staticmethod
    def _extends_closure(
        conn: duckdb.DuckDBPyConnection,
        seed_id: int,
        depth: int,
    ) -> set[int]:
        """Return seed_id plus everything that EXTENDS it within ``depth``.

        Note: an "A EXTENDS B" edge means A specializes B (e.g.
        Apache Spark Structured Streaming EXTENDS Apache Spark). For a
        cheatsheet on B, we want procedures from A too.
        """
        result = {seed_id}
        frontier = {seed_id}
        for _ in range(max(0, depth)):
            if not frontier:
                break
            ph = ",".join(["?"] * len(frontier))
            rows = conn.execute(
                f"""
                SELECT DISTINCT from_concept_id
                  FROM concept_relation
                 WHERE relation_type = 'EXTENDS'
                   AND to_concept_id IN ({ph})
                """,
                list(frontier),
            ).fetchall()
            new_frontier = {int(r[0]) for r in rows} - result
            result.update(new_frontier)
            frontier = new_frontier
        return result

    @staticmethod
    def _render_source_label(
        conn: duckdb.DuckDBPyConnection,
        source_type: Optional[str],
        source_id: Optional[int],
    ) -> Optional[str]:
        if not source_type or not source_id:
            return None
        if source_type == "chapter":
            row = conn.execute(
                """
                SELECT b.title, c.title
                  FROM chapter c JOIN book b ON c.book_id = b.book_id
                 WHERE c.chapter_id = ?
                """,
                [source_id],
            ).fetchone()
            if row:
                return f"{row[0]} — {row[1]}"
        elif source_type == "doc_section":
            row = conn.execute(
                """
                SELECT ds.name, s.heading_text
                  FROM doc_section s
                  JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
                  JOIN doc_source ds   ON sn.doc_source_id = ds.doc_source_id
                 WHERE s.doc_section_id = ?
                """,
                [source_id],
            ).fetchone()
            if row:
                return f"{row[0]} — {row[1] or '(no heading)'}"
        return None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "subject"


def _truncate_steps(text: str, limit: int = DEFAULT_STEPS_EXCERPT) -> str:
    """Trim to limit chars without breaking mid-word; collapse runs of whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


def _format_steps_for_cheatsheet(
    raw: Optional[str], *, max_steps: int = 4, max_chars_per_step: int = 140,
) -> str:
    """Render a procedure's ``steps`` field for cheatsheet display.

    Procedures are extracted as JSON lists of step objects shaped like
    ``{"n": int, "action": str, "command": str?, "notes": str?}``. A
    cheatsheet wants the *commands* — short, copy-pasteable. We extract
    them in order, fall back to a truncated action when no command is
    present, and cap to ``max_steps`` to keep entries compact.

    Plain-text or malformed-JSON steps fall through to the truncated-text
    behavior of ``_truncate_steps``.
    """
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _truncate_steps(raw, DEFAULT_STEPS_EXCERPT)

    if not isinstance(parsed, list):
        return _truncate_steps(raw, DEFAULT_STEPS_EXCERPT)

    lines: list[str] = []
    for step in parsed[:max_steps]:
        if not isinstance(step, dict):
            continue
        cmd = (step.get("command") or "").strip()
        action = (step.get("action") or "").strip()
        if cmd:
            line = cmd
        elif action:
            line = f"# {action}"  # comment-style for non-runnable narration
        else:
            continue
        # Compress whitespace to keep the cheatsheet dense.
        line = re.sub(r"\s+", " ", line)
        if len(line) > max_chars_per_step:
            line = line[:max_chars_per_step].rstrip() + "…"
        lines.append(line)
    if len(parsed) > max_steps:
        lines.append(f"# … (+{len(parsed) - max_steps} more steps)")
    return "\n".join(lines) if lines else _truncate_steps(raw, DEFAULT_STEPS_EXCERPT)


def _render_cheatsheet(decomp: _Decomposition) -> str:
    title = decomp.subject_name
    type_tag = f" — {decomp.subject_concept_type}" if decomp.subject_concept_type else ""
    lines = [
        f"# {title}{type_tag} cheatsheet",
        "",
        f"_Quick reference distilled from {decomp.n_procedures_total} "
        f"procedure(s) in the corpus._",
        "",
    ]
    if not decomp.clusters:
        lines.append("_No procedures available for this subject. "
                     "Try a different concept or run `/kb-discover` to grow "
                     "the corpus._")
        return "\n".join(lines)

    for cluster in decomp.clusters:
        lines.append(f"## {cluster.category}")
        lines.append("")
        for p in cluster.procedures:
            lines.append(f"### {p.name}")
            if p.preconditions and p.preconditions.strip():
                lines.append(f"_when:_ {_truncate_steps(p.preconditions, 120)}")
            if p.steps:
                snippet = _format_steps_for_cheatsheet(p.steps)
                if snippet:
                    lines.append("```")
                    lines.append(snippet)
                    lines.append("```")
            lines.append("")

    if decomp.failure_modes:
        lines.append("## Gotchas")
        lines.append("")
        for proc_name, fm in decomp.failure_modes:
            short_fm = _truncate_steps(fm, 200)
            lines.append(f"- **{proc_name}** — {short_fm}")
        lines.append("")

    return "\n".join(lines)


def _render_provenance(decomp: _Decomposition) -> str:
    lines = [
        f"# Provenance — {decomp.subject_name} cheatsheet",
        "",
        "Each cheatsheet entry's source. Use this to drill into the "
        "original chapter or doc section.",
        "",
    ]
    for cluster in decomp.clusters:
        lines.append(f"## {cluster.category}")
        lines.append("")
        for p in cluster.procedures:
            src = p.source_label or "(no source label)"
            lines.append(f"- procedure_id={p.procedure_id} — **{p.name}** ← {src}")
        lines.append("")
    return "\n".join(lines)


def _render_gotchas(decomp: _Decomposition) -> str:
    if not decomp.failure_modes:
        return f"# {decomp.subject_name} — Gotchas\n\n_No failure modes recorded.\n_"
    lines = [
        f"# {decomp.subject_name} — Gotchas",
        "",
        "Full failure-mode notes; the cheatsheet's Gotchas section is a "
        "truncated summary of these.",
        "",
    ]
    for proc_name, fm in decomp.failure_modes:
        lines.append(f"### {proc_name}")
        lines.append("")
        lines.append(fm)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class CheatsheetPlanner:
    """Renders the topical decomposition into a fully-loaded GenPlan.

    One ``GenUnit`` per category cluster. Three files: ``cheatsheet.md``
    (the deliverable), ``_provenance.md`` (per-line source pointer),
    and ``_gotchas.md`` (extended failure modes).
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        max_words: int = DEFAULT_MAX_WORDS,
        **_: Any,
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
                "subject_concept_type": d.subject_concept_type,
                "n_procedures_total": d.n_procedures_total,
                "n_clusters": len(d.clusters),
                "max_words": max_words,
            },
            notes=list(d.notes),
        )

        # One unit per cluster
        for ordinal, cluster in enumerate(d.clusters):
            unit = GenUnit(
                unit_type="cheatsheet_section",
                name=cluster.category,
                ordinal=ordinal,
                metadata={
                    "category": cluster.category,
                    "procedure_ids": [p.procedure_id for p in cluster.procedures],
                    "n_procedures": len(cluster.procedures),
                },
                logical_key=f"section_{ordinal}",
                sources=[("procedure", p.procedure_id, 1.0, 1.0, None)
                         for p in cluster.procedures],
            )
            plan.units.append(unit)

        plan.files.extend([
            GenFile(filename="cheatsheet.md",
                    content=_render_cheatsheet(d), purpose="cheatsheet"),
            GenFile(filename="_provenance.md",
                    content=_render_provenance(d), purpose="provenance"),
            GenFile(filename="_gotchas.md",
                    content=_render_gotchas(d), purpose="gotchas"),
        ])

        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    """Approximate word count for the page-fit heuristic."""
    return len(re.findall(r"\b\w[\w'-]*\b", text))


class CheatsheetValidator:
    """Checks page-fit + reference integrity:

    * subject concept resolved
    * cheatsheet.md fits the page heuristic (≤ max_words, ≤ 8 sections)
    * every procedure_id referenced exists in the procedure table
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        subject_id = plan.package_metadata.get("subject_concept_id", -1)
        if subject_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="subject concept not resolved",
            ))
            return issues
        if not plan.units:
            # Empty cheatsheet is a warning, not an error — the deliverable
            # still renders ("no procedures available") and is honest about it.
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no procedure clusters; cheatsheet will be empty",
            ))

        # Page fit
        ch = next((f for f in plan.files if f.filename == "cheatsheet.md"), None)
        if ch is None:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="cheatsheet.md missing from plan",
            ))
        else:
            wc = _word_count(ch.content)
            max_words = plan.package_metadata.get("max_words", DEFAULT_MAX_WORDS)
            if wc > max_words:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="warning",
                    message=f"cheatsheet exceeds page heuristic: {wc} words "
                            f"(max {max_words})",
                ))
            n_sections = len(plan.units)
            if n_sections > DEFAULT_MAX_SECTIONS:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="warning",
                    message=f"cheatsheet has {n_sections} sections "
                            f"(max {DEFAULT_MAX_SECTIONS})",
                ))

        # FK existence: procedure_ids
        all_proc_ids: set[int] = set()
        for u in plan.units:
            for pid in u.metadata.get("procedure_ids", []):
                all_proc_ids.add(int(pid))
        if all_proc_ids:
            ph = ",".join(["?"] * len(all_proc_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT procedure_id FROM procedure WHERE procedure_id IN ({ph})",
                list(all_proc_ids),
            ).fetchall()}
            missing = all_proc_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"procedure_ids referenced but not in procedure table: "
                            f"{sorted(missing)[:5]}",
                ))

        return issues


# ---------------------------------------------------------------------------
# Materializer (same shape as Concept Map)
# ---------------------------------------------------------------------------


class CheatsheetMaterializer:
    """Writes cheatsheet.md, _provenance.md, _gotchas.md to disk.

    Reads ``generated_file`` rows for the package and writes them by
    filename under ``<output_root>/<package_name>/``.
    """

    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?",
            [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)

        rows = conn.execute(
            """
            SELECT filename, content FROM generated_file
             WHERE package_id = ?
             ORDER BY file_id
            """,
            [package_id],
        ).fetchall()

        written: list[str] = []
        skipped: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
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


def make_cheatsheet_generator() -> Generator:
    """Return a fully-wired Cheatsheet generator."""
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=TopicalCondenseDecomposer(),
        planner=CheatsheetPlanner(),
        ranking_mode="generation",
        validator=CheatsheetValidator(),
        materializer=CheatsheetMaterializer(),
    )
