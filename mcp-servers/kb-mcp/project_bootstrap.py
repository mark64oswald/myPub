"""project_bootstrap.py — Phase 15 Project Bootstrap (deterministic v1).

The user's stated #1 generator. Composes Concept→Pattern→Procedure
into a runnable project scaffold reconciled with current doc snapshots.

The architecture spec calls for sub-agent-driven prose generation of
each project file. v1 ships the deterministic skeleton:

  1. Resolve named technologies + named patterns from the request
  2. Pull procedures + doc_sections relevant to each
  3. Render a project tree with placeholder files containing the
     accumulated context (procedures + pattern descriptions + doc
     excerpts)
  4. Write per-file sub-agent prompts to _sub_agent_prompts/ that a
     future v2 can dispatch to generate actual code

The skeleton is structural — every file expected by the requested
stack appears, with substantial context for the sub-agent (or human)
to fill in. The user can either run the sub-agent prompts manually
via the Task tool or hand-edit each placeholder.

Output:
    bootstraps/<project-name>/
      README.md                project overview
      _build_plan.md           file-by-file build plan with metrics
      _sub_agent_prompts/      one prompt per planned file
        prompt_<n>_<file>.txt
      src/                     placeholder source files
      tests/                   placeholder test stubs
      docker-compose.yml       (when stack involves containers)
      requirements.txt         (when Python)
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

LOG = logging.getLogger("mypub-project-bootstrap")

GENERATOR_TYPE = "project_bootstrap"


@dataclass
class _StackElement:
    concept_id: int
    name: str
    concept_type: str
    description: Optional[str]
    role: str                    # "pattern" | "tool" | "framework" | "technique"
    procedure_ids: list[int] = field(default_factory=list)
    chapter_ids: list[int] = field(default_factory=list)
    doc_section_ids: list[int] = field(default_factory=list)


@dataclass
class _PlannedFile:
    relative_path: str           # e.g. "src/handlers/command_handler.py"
    purpose: str                 # short description
    placeholder_content: str     # what gets written to disk
    prompt: str                  # sub-agent prompt content


@dataclass
class _Decomposition:
    project_name: str
    description: str
    elements: list[_StackElement]
    planned_files: list[_PlannedFile]
    notes: list[str] = field(default_factory=list)


# Heuristic file plan based on the named patterns/technologies. The
# spec calls for a richer composition; v1 uses templates that match
# common stacks.
_DEFAULT_FILES = [
    ("README.md", "project overview + quickstart"),
    ("requirements.txt", "Python dependencies"),
    ("docker-compose.yml", "service topology for local dev"),
    ("src/__init__.py", "package marker"),
    ("src/main.py", "application entry point"),
    ("tests/__init__.py", "test package marker"),
    ("tests/test_smoke.py", "smoke tests verifying scaffold runs"),
    (".gitignore", "ignore generated artifacts"),
]


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "project"


def _short(text: str, limit: int = 300) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


class StackComposeDecomposer:
    """Resolve named technologies + patterns from the request and
    pull related procedures + doc_sections.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        technologies: Optional[list[str]] = None,
        patterns: Optional[list[str]] = None,
        project_name: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        techs = technologies or []
        pats = patterns or []
        all_named = list(techs) + list(pats)
        if not all_named:
            # Try to glean names from the description: any capitalized
            # word that resolves to a concept.
            tokens = re.findall(r"[A-Z][A-Za-z+#0-9.-]{2,}", query or "")
            all_named = list(set(tokens))

        elements: list[_StackElement] = []
        notes: list[str] = []
        for name in all_named:
            cid = resolver.resolve_lookup_only(name)
            if cid is None:
                notes.append(f"named element {name!r} not found; skipped")
                continue
            row = conn.execute(
                "SELECT name, concept_type, description FROM concept "
                "WHERE concept_id = ?", [cid],
            ).fetchone()
            ctype = row[1] or "Concept"
            role = ctype.lower() if ctype else "concept"

            # Procedures linked to this concept
            proc_ids = [
                int(r[0]) for r in conn.execute(
                    "SELECT procedure_id FROM procedure_concept "
                    "WHERE concept_id = ? LIMIT 5", [cid],
                ).fetchall()
            ]
            # Top chapters
            chap_ids = [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT cr.source_id FROM concept_relation cr
                     WHERE cr.source_type = 'chapter'
                       AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                     GROUP BY cr.source_id
                     ORDER BY COUNT(*) DESC LIMIT 3
                    """, [cid, cid],
                ).fetchall()
            ]
            # Top doc_sections
            sec_ids = [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT cr.source_id FROM concept_relation cr
                     WHERE cr.source_type = 'doc_section'
                       AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                     GROUP BY cr.source_id
                     ORDER BY COUNT(*) DESC LIMIT 3
                    """, [cid, cid],
                ).fetchall()
            ]
            elements.append(_StackElement(
                concept_id=cid, name=row[0], concept_type=ctype,
                description=row[2], role=role,
                procedure_ids=proc_ids,
                chapter_ids=chap_ids,
                doc_section_ids=sec_ids,
            ))

        # Plan files
        planned: list[_PlannedFile] = []
        ctx_summary = self._render_context_summary(conn, elements)
        for rel_path, purpose in _DEFAULT_FILES:
            placeholder = self._render_placeholder(
                rel_path, purpose, project_name or "project", ctx_summary,
            )
            prompt = self._render_subagent_prompt(
                rel_path, purpose, query, elements, ctx_summary,
            )
            planned.append(_PlannedFile(
                relative_path=rel_path, purpose=purpose,
                placeholder_content=placeholder, prompt=prompt,
            ))

        if not elements:
            notes.append(
                "no named technologies or patterns resolved; project "
                "scaffold is generic. Try naming specific concepts."
            )

        return _Decomposition(
            project_name=project_name or _slugify(query)[:50] or "project",
            description=query,
            elements=elements,
            planned_files=planned,
            notes=notes,
        )

    @staticmethod
    def _render_context_summary(
        conn: duckdb.DuckDBPyConnection,
        elements: list[_StackElement],
    ) -> str:
        if not elements:
            return "_(no named stack elements; sub-agent will scaffold a generic project)_"
        lines = []
        for el in elements:
            lines.append(f"- **{el.name}** ({el.concept_type}): "
                         f"{_short(el.description or 'no description', 160)}")
            if el.procedure_ids:
                lines.append(f"  Procedures: {len(el.procedure_ids)} available")
            if el.doc_section_ids:
                lines.append(f"  Doc sections: {len(el.doc_section_ids)} relevant")
        return "\n".join(lines)

    @staticmethod
    def _render_placeholder(
        rel_path: str, purpose: str, project_name: str, ctx_summary: str,
    ) -> str:
        if rel_path == "README.md":
            return (
                f"# {project_name}\n\n"
                f"_{purpose}_\n\n"
                f"## Stack context (from corpus)\n\n"
                f"{ctx_summary}\n\n"
                f"## Quickstart\n\n"
                f"_(TODO — fill in once src/main.py is implemented)_\n"
            )
        if rel_path == "requirements.txt":
            return "# Python dependencies — populate based on stack context above\n"
        if rel_path == "docker-compose.yml":
            return (
                "version: '3.9'\n"
                "services:\n"
                "  # TODO: list services per the stack context\n"
            )
        if rel_path == ".gitignore":
            return "__pycache__/\n*.pyc\n.venv/\n.env\ndata/\n"
        if rel_path.endswith("__init__.py"):
            return ""
        if rel_path == "src/main.py":
            return (
                "\"\"\"Entry point — sub-agent fills this in.\"\"\"\n\n\n"
                "def main() -> None:\n"
                "    raise NotImplementedError(\n"
                f"        \"Run /kb-bootstrap-dispatch to generate this file from the prompt in _sub_agent_prompts/\"\n"
                "    )\n\n\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            )
        if rel_path.startswith("tests/"):
            return (
                "\"\"\"Smoke test placeholder — sub-agent fills this in.\"\"\"\n\n\n"
                "def test_placeholder():\n"
                "    assert True, \"replace with real assertions\"\n"
            )
        return f"# {rel_path}\n# {purpose}\n\n# TODO: sub-agent fills this in\n"

    @staticmethod
    def _render_subagent_prompt(
        rel_path: str, purpose: str, request: str,
        elements: list[_StackElement], ctx_summary: str,
    ) -> str:
        return (
            f"Generate the file at `{rel_path}` for the following project request:\n\n"
            f"{request}\n\n"
            f"Purpose of this file: {purpose}\n\n"
            f"Stack context (resolved from the knowledge base):\n\n"
            f"{ctx_summary}\n\n"
            f"Constraints:\n"
            f"- Use the procedures and patterns named in the stack context\n"
            f"- Match current vendor doc semantics (the corpus tracks recent docs)\n"
            f"- Keep the file self-contained; cross-references go in the README\n"
            f"- Do NOT hallucinate APIs that aren't in the corpus\n"
            f"\nWrite ONLY the file content — no prose explanation, no fences.\n"
        )


def _render_build_plan(d: _Decomposition) -> str:
    lines = [
        f"# {d.project_name} — Build Plan",
        "",
        f"**Request:** {d.description}",
        "",
        f"**Stack elements resolved:** {len(d.elements)}",
        "",
    ]
    if d.elements:
        lines.append("## Stack")
        lines.append("")
        for el in d.elements:
            lines.append(f"- **{el.name}** ({el.concept_type}, {el.role}) — "
                         f"{len(el.procedure_ids)} proc(s), "
                         f"{len(el.chapter_ids)} chapter(s), "
                         f"{len(el.doc_section_ids)} doc section(s)")
        lines.append("")
    lines.append("## Files planned")
    lines.append("")
    for pf in d.planned_files:
        lines.append(f"- `{pf.relative_path}` — {pf.purpose}")
    lines.append("")
    lines.append("## Sub-agent prompts")
    lines.append("")
    lines.append(
        "Each planned file has a corresponding prompt in "
        "`_sub_agent_prompts/`. Dispatch them via the Task tool to fill "
        "in the placeholders with real implementation."
    )
    if d.notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in d.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


class ProjectBootstrapPlanner:
    def plan(
        self, conn, decomposition, *, package_name=None, **_,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.project_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.description,
            source_query=d.description,
            package_metadata={
                "n_elements": len(d.elements),
                "n_files": len(d.planned_files),
                "element_concept_ids": [el.concept_id for el in d.elements],
            },
            notes=list(d.notes),
        )
        for i, pf in enumerate(d.planned_files, start=1):
            plan.units.append(GenUnit(
                unit_type="project_file",
                name=pf.relative_path,
                ordinal=i,
                metadata={
                    "purpose": pf.purpose,
                    "relative_path": pf.relative_path,
                },
                logical_key=f"file_{i}",
                content_markdown=pf.purpose,
                sources=[],
            ))
            # Placeholder content
            plan.files.append(GenFile(
                filename=pf.relative_path,
                content=pf.placeholder_content,
                purpose="placeholder",
            ))
            # Sub-agent prompt
            prompt_name = (
                f"_sub_agent_prompts/prompt_{i:02d}_"
                + _slugify(pf.relative_path) + ".txt"
            )
            plan.files.append(GenFile(
                filename=prompt_name,
                content=pf.prompt,
                purpose="subagent_prompt",
            ))
        plan.files.append(GenFile(
            filename="_build_plan.md",
            content=_render_build_plan(d),
            purpose="build_plan",
        ))
        return plan


class ProjectBootstrapValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("n_elements", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no stack elements resolved; scaffold is generic",
            ))
        if plan.package_metadata.get("n_files", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no files planned",
            ))
            return issues
        # Required structural files
        names = {u.metadata.get("relative_path") for u in plan.units}
        for required in ("README.md", "src/main.py", "tests/test_smoke.py"):
            if required not in names:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"missing required file in plan: {required}",
                ))
        # FK existence on element concept_ids
        ids = set(plan.package_metadata.get("element_concept_ids", []))
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
                    message=f"element concept_ids missing: {sorted(missing)[:5]}",
                ))
        return issues


class ProjectBootstrapMaterializer:
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


def make_project_bootstrap_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=StackComposeDecomposer(),
        planner=ProjectBootstrapPlanner(),
        ranking_mode="generation",
        validator=ProjectBootstrapValidator(),
        materializer=ProjectBootstrapMaterializer(),
    )
