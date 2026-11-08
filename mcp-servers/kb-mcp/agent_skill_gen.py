"""
agent_skill_gen.py — ``/kb-skill-from-kb``: one Agent Skill built from the KB (2026-11).

Turns a concept the KB knows about into a skill folder that follows the
Agent Skills spec (agentskills.io):

    <package>/
      <skill-name>/
        SKILL.md                    frontmatter (spec-valid name + description) and a short body
        references/procedures.md    full steps, preconditions and failure modes
        references/concepts.md      related concepts by relation type
        references/sources.md       the books and live docs it came from
      _provenance.md               sources + the spec sections the layout follows
      _validation.md               spec check results
      _prompts/refine-skill.md     optional subagent pass that rewrites the body as instructions

Progressive disclosure, as the spec recommends: ``SKILL.md`` stays short and
points into ``references/``. The body is deterministic (assembled from KB
rows); the optional refine prompt lets a subagent turn it into polished
instructions without inventing facts.

:func:`build_skill_files` is reused by the MCP server and plugin generators.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import duckdb

import agent_skills
import kb_scaffold as ks
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

LOG = logging.getLogger("mypub-agent-skill")
GENERATOR_TYPE = "agent_skill"

_REL_LABELS = {
    "REQUIRES": "Requires",
    "IS_REQUIRED_BY": "Required by",
    "EXTENDS": "Extends",
    "SPECIALIZED_BY": "Specialized by",
    "IMPLEMENTS": "Implements",
    "CONTRASTS_WITH": "Contrasts with",
    "CITES": "Cites",
}


def skill_description(tm: ks.TopicMaterial, purpose: Optional[str] = None) -> str:
    """What the skill does and when to use it, per the spec's description guidance."""
    what = ks.first_sentence(tm.description, 280) or f"Guidance on {tm.name}."
    neighbours = [n for r, n, _ in tm.related if r in ("REQUIRES", "EXTENDS", "SPECIALIZED_BY")][:3]
    when = f"Use when the task involves {tm.name}"
    if neighbours:
        when += f" or related work with {', '.join(neighbours)}"
    if purpose:
        when += f", especially to {purpose.strip().rstrip('.')}"
    return agent_skills.fit_description(f"{what} {when}.")


def _steps_excerpt(steps: str, limit: int = 220) -> str:
    s = " ".join((steps or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def render_body(tm: ks.TopicMaterial, *, title: str) -> str:
    lines = [f"# {title}", ""]
    if tm.description:
        lines += [" ".join(tm.description.split()), ""]
    reqs = [n for r, n, _ in tm.related if r == "REQUIRES"]
    if reqs:
        lines += ["## Before you start", "", f"This builds on: {', '.join(reqs)}.", ""]
    if tm.procedures:
        lines += ["## How to apply it", ""]
        for i, p in enumerate(tm.procedures, 1):
            lines.append(f"{i}. **{p.name}.** {_steps_excerpt(p.steps)}")
        lines += ["", "Full steps, preconditions and failure modes are in "
                  "[references/procedures.md](references/procedures.md).", ""]
    gotchas = [p for p in tm.procedures if (p.failure_modes or "").strip()]
    if gotchas:
        lines += ["## Watch out for", ""]
        for p in gotchas[:5]:
            lines.append(f"- {ks.first_sentence(p.failure_modes, 200)} ({p.name})")
        lines.append("")
    lines += [
        "## More detail",
        "",
        "- [references/procedures.md](references/procedures.md): every procedure, step by step",
        "- [references/concepts.md](references/concepts.md): related concepts",
        "- [references/sources.md](references/sources.md): the books and docs behind this skill",
        "",
    ]
    return "\n".join(lines)


def render_procedures(tm: ks.TopicMaterial) -> str:
    lines = [f"# Procedures for {tm.name}", ""]
    if not tm.procedures:
        lines.append("_The knowledge base has no procedures linked to this concept._")
    for p in tm.procedures:
        lines += [f"## {p.name}", ""]
        if (p.preconditions or "").strip():
            lines += ["**Preconditions.** " + " ".join(p.preconditions.split()), ""]
        lines += ["**Steps.**", "", p.steps.strip(), ""]
        if (p.failure_modes or "").strip():
            lines += ["**Failure modes.** " + " ".join(p.failure_modes.split()), ""]
        if p.source:
            lines += [f"_Source: {p.source.label} (`{p.source.source_type}:{p.source.source_id}`)_", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_concepts(tm: ks.TopicMaterial) -> str:
    lines = [f"# Concepts related to {tm.name}", ""]
    if not tm.related:
        lines.append("_No related concepts in the knowledge base._")
    by_rel: dict[str, list[str]] = {}
    for rel, name, _ in tm.related:
        by_rel.setdefault(rel, []).append(name)
    for rel, names in by_rel.items():
        lines += [f"## {_REL_LABELS.get(rel, rel.title())}", ""]
        lines += [f"- {n}" for n in names] + [""]
    return "\n".join(lines).rstrip() + "\n"


def build_skill_files(
    tm: ks.TopicMaterial,
    *,
    title: Optional[str] = None,
    purpose: Optional[str] = None,
    prefix: str = "",
    extra_frontmatter: Optional[dict[str, str]] = None,
) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Return ``(skill_name, [(relative_path, content)], spec_problems)`` for one skill.

    ``prefix`` is the folder the skill sits in (``"skills/"`` in a plugin).
    ``extra_frontmatter`` adds Claude Code fields such as
    ``disable-model-invocation`` (written as raw YAML values).
    """
    title = title or tm.name
    name = agent_skills.skill_name(title)
    description = skill_description(tm, purpose)
    body = render_body(tm, title=title)
    md = agent_skills.build_skill_md(
        name, description, body,
        metadata={"generator": "mypub agent_skill", "concept": tm.name},
    )
    if extra_frontmatter:
        extra = "".join(f"{k}: {v}\n" for k, v in extra_frontmatter.items())
        md = md.replace("\n---\n\n", f"\n{extra}---\n\n", 1)
    base = f"{prefix}{name}/"
    files = [
        (f"{base}SKILL.md", md),
        (f"{base}references/procedures.md", render_procedures(tm)),
        (f"{base}references/concepts.md", render_concepts(tm)),
        (f"{base}references/sources.md",
         ks.render_citations(tm.sources, title=f"Sources for {tm.name}")),
    ]
    problems = agent_skills.validate(name, description, folder=name, body=body)
    return name, files, problems


REFINE_PROMPT = """\
# Refine the {name} skill

You are improving a generated Agent Skill. Edit `{path}SKILL.md` in place.

Rules:
- Keep the frontmatter exactly: `name: {name}` must not change (it must match the folder).
  You may rewrite `description` (max 1024 characters): say what the skill does and when to use it,
  with the trigger words a user would type.
- Rewrite the body as direct instructions to an agent: numbered steps, decision points, and
  what to check. Keep it under 500 lines; leave long detail in `references/`.
- Use only facts in `references/` and the current SKILL.md. Do not invent commands, flags or APIs.
  Where a reference is thin, say so in the body rather than filling the gap.
- Keep the links to `references/*.md`.

Spec: Agent Skills specification (agentskills.io/specification); see `_provenance.md` for the
sections cited.
"""


# ---------------------------------------------------------------------------
# Generator components
# ---------------------------------------------------------------------------


class SkillDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        tm = ks.topic_material(conn, resolver, query,
                               max_procedures=int(kw.get("max_procedures", 8)))
        cites = ks.doc_citations(conn, ks.SKILL_SPEC_SOURCES,
                                 keywords=("name", "description", "references"), per_source=2)
        return {"query": query, "topic": tm, "citations": cites,
                "title": kw.get("title"), "purpose": kw.get("purpose")}


class SkillPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        tm: Optional[ks.TopicMaterial] = d["topic"]
        plan = GenPlan(generator_type=GENERATOR_TYPE,
                       package_name=package_name or agent_skills.skill_name(d["title"] or d["query"]),
                       domain=tm.name if tm else d["query"], source_query=d["query"])
        if tm is None:
            plan.package_metadata = {"concept_id": -1}
            plan.notes.append(f"topic {d['query']!r} not found in the knowledge base")
            return plan
        name, files, problems = build_skill_files(tm, title=d["title"], purpose=d["purpose"])
        plan.package_metadata = {
            "concept_id": tm.concept_id, "skill_name": name,
            "n_procedures": len(tm.procedures), "n_related": len(tm.related),
            "n_sources": len(tm.sources), "spec_problems": problems,
        }
        plan.units.append(GenUnit(
            unit_type="agent_skill", name=name, logical_key=name,
            content_markdown=files[0][1],
            metadata={"concept_id": tm.concept_id, "title": d["title"] or tm.name},
            sources=[(c.source_type, c.source_id, 1.0, 1.0, None) for c in tm.sources]
            + [("procedure", p.procedure_id, 1.0, 1.0, None) for p in tm.procedures],
        ))
        for path, content in files:
            plan.files.append(GenFile(filename=path, content=content,
                                      purpose="skill", unit_logical_key=name))
        plan.files += [
            GenFile(filename="_provenance.md", purpose="provenance", content=(
                f"# Provenance: {name}\n\nBuilt from concept **{tm.name}** "
                f"(`concept:{tm.concept_id}`).\n\n"
                + ks.render_citations(tm.sources, "Knowledge-base sources") + "\n"
                + ks.render_citations(d["citations"], "Specs this layout follows"))),
            GenFile(filename="_validation.md", purpose="validation",
                    content=render_validation(name, problems)),
            GenFile(filename="_prompts/refine-skill.md", purpose="subagent_prompt",
                    content=REFINE_PROMPT.format(name=name, path=f"{name}/")),
        ]
        if not tm.procedures:
            plan.notes.append("no procedures linked to this concept; the skill body is thin")
        return plan


def render_validation(name: str, problems: Sequence[str]) -> str:
    lines = [f"# Agent Skills spec check: {name}", ""]
    if not problems:
        lines.append("Passes: `name` is a valid slug matching its folder, `description` is "
                     "present and within 1024 characters, body is within the recommended length.")
    else:
        lines += [f"- {p}" for p in problems]
    lines += ["", "Re-check after editing with `skills-ref validate <folder>` "
              "(github.com/agentskills/agentskills, skills-ref/) if you have it installed."]
    return "\n".join(lines) + "\n"


class SkillValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        if plan.package_metadata.get("concept_id", -1) == -1:
            return [ValidationIssue("", "error", "topic not found in the knowledge base")]
        out = []
        for p in plan.package_metadata.get("spec_problems", []):
            sev = "warning" if p.startswith("warning:") else "error"
            out.append(ValidationIssue(plan.package_metadata.get("skill_name", ""), sev, p))
        if not plan.package_metadata.get("n_procedures"):
            out.append(ValidationIssue("", "warning", "no procedures; body is description-only"))
        return out


def make_agent_skill_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=SkillDecomposer(),
                     planner=SkillPlanner(), ranking_mode="generation",
                     validator=SkillValidator(), materializer=ks.TreeMaterializer())
