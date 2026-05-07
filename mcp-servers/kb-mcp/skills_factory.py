"""skills_factory.py — Phase 5.4 Skills Factory orchestration + materialization.

Glues Phase 5.1 (decomposition), 5.2 (package planning), and 5.3
(per-Skill generation prep/process) into one pipeline, plus the
materialization stage that writes ``SKILL.md`` files to disk.

Pipeline (driven by ``/kb-generate-skills``):

  1. ``decompose_and_plan(query)`` →
     ``decomposition.decompose_domain`` → ``package_planning.plan_package``
     → ``PackagePlan``.

  2. ``prep_package(plan, output_dir, search_fn)`` →
     ``skill_generation.prep_skill_generation``. Writes one prompt
     file per Skill plus a ``manifest.json`` to ``output_dir``.

  3. (External step) The ``/kb-generate-skills`` slash command
     dispatches one Claude Code sub-agent per prompt. Each writes a
     ``result_skill_<cluster_id>.json`` next to its prompt.

  4. ``process_package(output_dir, conn)`` →
     ``skill_generation.process_skill_generation``. Reads result
     JSONs, persists ``skill_package`` / ``skill`` / ``skill_source``
     / ``skill_relation`` rows.

  5. ``materialize_package(package_id, output_root, conn)`` walks the
     ``skill`` rows for the package and writes one ``SKILL.md`` file
     per skill to ``<output_root>/<package_folder>/<skill_folder>/``,
     plus ``_provenance.json`` with the audit trail. SKILL.md uses
     the standard Anthropic frontmatter:

         ---
         name: <skill name>
         description: <trigger description>
         ---
         <body>

  6. ``run_full_package(query, conn, ...)`` is a convenience wrapper
     that runs steps 1-2 in one call. Sub-agent dispatch (3) and
     post-dispatch ingest+materialize (4-5) live in the slash command
     because they require Task-tool access that's outside this
     module's scope.

This module makes NO direct API calls. All LLM work runs through
Claude Code sub-agents in the slash-command driver.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb

import decomposition
import package_planning
import skill_generation

LOG = logging.getLogger("mypub-skills-factory")


DEFAULT_OUTPUT_ROOT = "data/generated-packages"
DEFAULT_PROMPT_ROOT = "data/skill-runs"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PrepReport:
    """Returned by ``prep_package`` so callers know what to dispatch."""

    package_name: str
    domain: str
    output_dir: str
    n_skills: int
    skill_prompt_paths: list[str] = field(default_factory=list)
    skill_result_paths: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class MaterializeReport:
    """Returned by ``materialize_package`` listing what was written."""

    package_id: int
    package_name: str
    output_root: str
    skill_md_paths: list[str] = field(default_factory=list)
    provenance_paths: list[str] = field(default_factory=list)
    package_md_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 1+2: decompose → plan
# ---------------------------------------------------------------------------


def decompose_and_plan(
    conn: duckdb.DuckDBPyConnection,
    resolver: Any,
    query: str,
    *,
    package_name: Optional[str] = None,
    max_depth: int = decomposition.DEFAULT_MAX_DEPTH,
    max_neighborhood: int = decomposition.DEFAULT_MAX_NEIGHBORHOOD,
    min_cluster_size: int = decomposition.DEFAULT_MIN_CLUSTER_SIZE,
    folder_root_template: str = "data/generated-packages/{name}",
) -> package_planning.PackagePlan:
    """Run Phase 5.1 + 5.2 in one call.

    Returns a ``PackagePlan`` ready for ``prep_package``. The
    decomposition tunables flow through to ``decompose_domain``.
    """
    decomp = decomposition.decompose_domain(
        conn, resolver, query,
        max_depth=max_depth, max_neighborhood=max_neighborhood,
        min_cluster_size=min_cluster_size,
    )
    return package_planning.plan_package(
        decomp, conn,
        package_name=package_name,
        folder_root_template=folder_root_template,
    )


# ---------------------------------------------------------------------------
# Stage 3: prep (write prompts)
# ---------------------------------------------------------------------------


def prep_package(
    plan: package_planning.PackagePlan,
    conn: duckdb.DuckDBPyConnection,
    output_dir: Path,
    *,
    search_fn: skill_generation.SearchFn,
    retrieval_limit: int = skill_generation.DEFAULT_RETRIEVAL_LIMIT,
    excerpt_chars: int = skill_generation.DEFAULT_EXCERPT_CHARS,
) -> PrepReport:
    """Generate sub-agent prompts for every PlannedSkill in the package.

    Writes ``<output_dir>/prompts/prompt_skill_<cluster_id>.txt``
    files plus ``<output_dir>/manifest.json``. The slash command
    reads the manifest and dispatches one Task agent per prompt.
    """
    output_dir = Path(output_dir)
    manifest = skill_generation.prep_skill_generation(
        plan, conn, output_dir,
        search_fn=search_fn,
        retrieval_limit=retrieval_limit,
        excerpt_chars=excerpt_chars,
    )
    return PrepReport(
        package_name=manifest.package_name,
        domain=manifest.domain,
        output_dir=str(output_dir),
        n_skills=len(manifest.skills),
        skill_prompt_paths=[s.prompt_path for s in manifest.skills],
        skill_result_paths=[s.result_path for s in manifest.skills],
    )


# ---------------------------------------------------------------------------
# Stage 4: process (ingest sub-agent results)
# ---------------------------------------------------------------------------


def process_package(
    conn: duckdb.DuckDBPyConnection,
    output_dir: Path,
) -> skill_generation.SkillIngestSummary:
    """Re-export of skill_generation.process_skill_generation for symmetry."""
    return skill_generation.process_skill_generation(conn, Path(output_dir))


# ---------------------------------------------------------------------------
# Stage 5: materialize (write SKILL.md files)
# ---------------------------------------------------------------------------


_FRONTMATTER_ESCAPE_RE = re.compile(r'(["\\])')


def _yaml_escape(s: str) -> str:
    """Conservative YAML string escape for use in scalar values.

    We always wrap the value in double quotes and escape backslashes
    and double quotes. That's safe for the kind of single-line
    descriptions we generate; multi-line bodies live in the markdown
    body, not the frontmatter.
    """
    if s is None:
        return ""
    return _FRONTMATTER_ESCAPE_RE.sub(r"\\\1", s)


def _build_skill_md(name: str, description: str, body: str) -> str:
    """Compose the SKILL.md file content with Anthropic frontmatter."""
    name_clean = (name or "").strip()
    desc_clean = " ".join((description or "").split())  # collapse whitespace
    body_clean = body.rstrip() + "\n"
    return (
        "---\n"
        f'name: "{_yaml_escape(name_clean)}"\n'
        f'description: "{_yaml_escape(desc_clean)}"\n'
        "---\n\n"
        f"{body_clean}"
    )


def _build_package_md(
    package_name: str, domain: str, skills: list[dict[str, Any]],
) -> str:
    """Compose the package-level _package.md README listing every Skill."""
    lines = [
        f"# {package_name}",
        "",
        f"**Domain:** {domain}",
        "",
        f"This package contains {len(skills)} Skill(s) generated by the myPub Skills Factory.",
        "",
        "## Skills",
        "",
    ]
    for s in skills:
        lines.append(f"- **{s['name']}** — {s['description']}")
    lines.append("")
    return "\n".join(lines)


def _resolve_package_id(
    conn: duckdb.DuckDBPyConnection,
    package_id: Optional[int], package_name: Optional[str],
) -> int:
    """Look up package_id from name when caller passes name only."""
    if package_id is not None:
        return int(package_id)
    if not package_name:
        raise ValueError("must provide either package_id or package_name")
    row = conn.execute(
        "SELECT package_id FROM skill_package WHERE name = ?", [package_name],
    ).fetchone()
    if not row:
        raise ValueError(f"no skill_package found with name={package_name!r}")
    return int(row[0])


def materialize_package(
    conn: duckdb.DuckDBPyConnection,
    *,
    package_id: Optional[int] = None,
    package_name: Optional[str] = None,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    overwrite: bool = True,
) -> MaterializeReport:
    """Walk the ``skill`` rows for the package and write SKILL.md files.

    For each skill:
      * ``<output_root>/<package_folder>/<skill_folder>/SKILL.md`` —
        the markdown body with Anthropic-style YAML frontmatter
        (``name`` + ``description``).
      * ``<output_root>/<package_folder>/<skill_folder>/_provenance.json`` —
        the audit trail: which sources were selected, which were
        dropped, with reasons. Mirrors the §8.6 provenance contract.

    The package folder also gets a ``_package.md`` summary listing
    every Skill, useful as a README.

    ``overwrite=True`` rewrites existing files; ``False`` skips them
    and reports them in the result so the caller can verify nothing
    was clobbered accidentally.
    """
    pid = _resolve_package_id(conn, package_id, package_name)
    pkg_row = conn.execute(
        "SELECT name, domain FROM skill_package WHERE package_id = ?", [pid],
    ).fetchone()
    if not pkg_row:
        raise ValueError(f"package_id {pid} not found")
    pkg_name = pkg_row[0]
    domain = pkg_row[1]

    package_folder = Path(output_root) / package_planning._slugify(pkg_name)
    package_folder.mkdir(parents=True, exist_ok=True)

    report = MaterializeReport(
        package_id=pid,
        package_name=pkg_name,
        output_root=str(output_root),
    )

    # Pull skills + their generation strategy/notes for the package md.
    skills = conn.execute(
        """
        SELECT skill_id, name, description, content_markdown, strategy
          FROM skill WHERE package_id = ? ORDER BY skill_id
        """, [pid],
    ).fetchall()
    skill_summaries: list[dict[str, Any]] = []
    for skill_id, name, description, content_markdown, strategy in skills:
        skill_folder = package_folder / package_planning._slugify(name)
        skill_folder.mkdir(parents=True, exist_ok=True)

        skill_md_path = skill_folder / "SKILL.md"
        if skill_md_path.exists() and not overwrite:
            LOG.info("skip existing %s (overwrite=False)", skill_md_path)
        else:
            skill_md_path.write_text(_build_skill_md(
                name, description or "", content_markdown or "",
            ))
            report.skill_md_paths.append(str(skill_md_path))

        # Provenance file
        sources = conn.execute(
            "SELECT source_type, source_id, score, weight, drop_reason "
            "  FROM skill_source WHERE skill_id = ? ORDER BY drop_reason NULLS FIRST, score DESC",
            [skill_id],
        ).fetchall()
        prov = {
            "skill_id": skill_id,
            "skill_name": name,
            "strategy": strategy,
            "selected": [
                {"source_type": s[0], "source_id": s[1],
                 "score": float(s[2] or 0.0), "weight": float(s[3] or 0.0)}
                for s in sources if s[4] is None
            ],
            "dropped": [
                {"source_type": s[0], "source_id": s[1],
                 "score": float(s[2] or 0.0), "drop_reason": s[4]}
                for s in sources if s[4] is not None
            ],
        }
        prov_path = skill_folder / "_provenance.json"
        if prov_path.exists() and not overwrite:
            LOG.info("skip existing %s (overwrite=False)", prov_path)
        else:
            prov_path.write_text(json.dumps(prov, indent=2))
            report.provenance_paths.append(str(prov_path))

        skill_summaries.append({"name": name, "description": description})

    # Package README
    package_md_path = package_folder / "_package.md"
    if package_md_path.exists() and not overwrite:
        LOG.info("skip existing %s (overwrite=False)", package_md_path)
    else:
        package_md_path.write_text(_build_package_md(
            pkg_name, domain or "", skill_summaries,
        ))
        report.package_md_path = str(package_md_path)
    return report


# ---------------------------------------------------------------------------
# Convenience wrapper: full pipeline through prep
# ---------------------------------------------------------------------------


def run_full_package(
    conn: duckdb.DuckDBPyConnection,
    resolver: Any,
    query: str,
    *,
    search_fn: skill_generation.SearchFn,
    package_name: Optional[str] = None,
    output_dir: Optional[Path] = None,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    prompt_root: str = DEFAULT_PROMPT_ROOT,
    retrieval_limit: int = skill_generation.DEFAULT_RETRIEVAL_LIMIT,
    excerpt_chars: int = skill_generation.DEFAULT_EXCERPT_CHARS,
    **decomposition_kwargs: Any,
) -> tuple[package_planning.PackagePlan, PrepReport]:
    """End-to-end Phase 5.1 → 5.3 prep in one call.

    Returns ``(plan, prep_report)`` so callers (the slash command)
    have everything they need to dispatch sub-agents and call
    ``process_package`` afterward. Materialization is a separate
    third call once results are ingested.

    ``output_dir`` defaults to ``<prompt_root>/<package_name>``.
    """
    plan = decompose_and_plan(
        conn, resolver, query,
        package_name=package_name,
        folder_root_template=output_root + "/{name}",
        **decomposition_kwargs,
    )
    if output_dir is None:
        output_dir = Path(prompt_root) / plan.package_name
    output_dir = Path(output_dir)
    prep = prep_package(
        plan, conn, output_dir,
        search_fn=search_fn,
        retrieval_limit=retrieval_limit, excerpt_chars=excerpt_chars,
    )
    return plan, prep
