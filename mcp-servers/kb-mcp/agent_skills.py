"""
agent_skills.py — the Agent Skills format (agentskills.io/specification), shared by every
generator that writes a ``SKILL.md``.

The spec's rules for the frontmatter, as of 2026-11:

* ``name`` (required): 1-64 characters of ``a-z``, ``0-9`` and ``-``; must not start
  or end with ``-`` or contain ``--``; must equal the skill's folder name.
* ``description`` (required): 1-1024 characters; says what the skill does and when
  to use it.
* ``license``, ``compatibility`` (max 500 characters), ``metadata`` (string → string
  map) and ``allowed-tools`` (space-separated, experimental) are optional.

The body is free Markdown. The spec recommends keeping ``SKILL.md`` under about 500
lines and moving detail into ``references/``, ``scripts/`` and ``assets/``
(progressive disclosure), which :func:`write_skill` supports.

The spec text itself is in the catalog as doc_source "Agent Skills specification".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
BODY_LINES_RECOMMENDED = 500

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def skill_name(title: str) -> str:
    """A spec-valid ``name`` for a human title ("Event Sourcing Basics" → "event-sourcing-basics")."""
    s = _NON_ALNUM.sub("-", (title or "").lower()).strip("-")
    s = s[:NAME_MAX].rstrip("-")
    return s or "skill"


def fit_description(description: str) -> str:
    """Collapse whitespace and cut to the spec's 1024-character limit at a word boundary."""
    d = " ".join((description or "").split())
    if len(d) <= DESCRIPTION_MAX:
        return d
    cut = d[: DESCRIPTION_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


def validate(name: str, description: str, *, folder: Optional[str] = None,
             compatibility: Optional[str] = None, body: Optional[str] = None) -> list[str]:
    """Return the spec violations (errors) and advisories ("warning: …") for a skill."""
    problems: list[str] = []
    if not name:
        problems.append("name is required")
    else:
        if len(name) > NAME_MAX:
            problems.append(f"name is {len(name)} characters; max {NAME_MAX}")
        if not _NAME_RE.match(name):
            problems.append(
                f"name {name!r} must be lowercase letters, digits and single hyphens, "
                "not starting or ending with a hyphen"
            )
        if folder is not None and folder != name:
            problems.append(f"name {name!r} must match its folder {folder!r}")
    if not (description or "").strip():
        problems.append("description is required")
    elif len(description) > DESCRIPTION_MAX:
        problems.append(f"description is {len(description)} characters; max {DESCRIPTION_MAX}")
    if compatibility is not None and len(compatibility) > COMPATIBILITY_MAX:
        problems.append(f"compatibility is {len(compatibility)} characters; max {COMPATIBILITY_MAX}")
    if body is not None and body.count("\n") + 1 > BODY_LINES_RECOMMENDED:
        problems.append(
            f"warning: SKILL.md body is {body.count(chr(10)) + 1} lines; the spec recommends "
            f"under {BODY_LINES_RECOMMENDED} and moving detail to references/"
        )
    return problems


def errors(problems: list[str]) -> list[str]:
    return [p for p in problems if not p.startswith("warning:")]


def _q(s: str) -> str:
    """Double-quoted YAML scalar."""
    return json.dumps(s, ensure_ascii=False)


def build_skill_md(
    name: str,
    description: str,
    body: str,
    *,
    title: Optional[str] = None,
    license: Optional[str] = None,
    compatibility: Optional[str] = None,
    metadata: Optional[Mapping[str, str]] = None,
    allowed_tools: Optional[str] = None,
) -> str:
    """Compose ``SKILL.md``: spec frontmatter, then the body.

    ``title`` (the human name) becomes an ``# H1`` when the body has none, since
    ``name`` is now a slug and no longer reads as a title.
    """
    lines = ["---", f"name: {name}", f"description: {_q(fit_description(description))}"]
    if license:
        lines.append(f"license: {_q(license)}")
    if compatibility:
        lines.append(f"compatibility: {_q(' '.join(compatibility.split()))}")
    if metadata:
        lines.append("metadata:")
        for k, v in metadata.items():
            lines.append(f"  {k}: {_q(str(v))}")
    if allowed_tools:
        lines.append(f"allowed-tools: {_q(allowed_tools)}")
    lines.append("---")
    body_clean = (body or "").strip("\n")
    if title and not re.match(r"^#\s", body_clean):
        body_clean = f"# {title.strip()}\n\n{body_clean}" if body_clean else f"# {title.strip()}"
    return "\n".join(lines) + "\n\n" + body_clean.rstrip() + "\n"


@dataclass
class WrittenSkill:
    folder: Path
    name: str
    files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def write_skill(
    parent: Path,
    title: str,
    description: str,
    body: str,
    *,
    references: Optional[Mapping[str, str]] = None,
    scripts: Optional[Mapping[str, str]] = None,
    metadata: Optional[Mapping[str, str]] = None,
    license: Optional[str] = None,
    compatibility: Optional[str] = None,
    allowed_tools: Optional[str] = None,
) -> WrittenSkill:
    """Write ``<parent>/<name>/SKILL.md`` plus optional ``references/`` and ``scripts/`` files.

    ``references`` / ``scripts`` map a relative file name to its content.
    """
    name = skill_name(title)
    folder = Path(parent) / name
    folder.mkdir(parents=True, exist_ok=True)
    md = build_skill_md(name, description, body, title=title, license=license,
                        compatibility=compatibility, metadata=metadata,
                        allowed_tools=allowed_tools)
    out = WrittenSkill(folder=folder, name=name)
    (folder / "SKILL.md").write_text(md)
    out.files.append("SKILL.md")
    for sub, files in (("references", references), ("scripts", scripts)):
        for rel, content in (files or {}).items():
            p = folder / sub / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            if sub == "scripts":
                p.chmod(0o755)
            out.files.append(f"{sub}/{rel}")
    out.problems = validate(name, fit_description(description), folder=folder.name,
                            compatibility=compatibility, body=md.split("---", 2)[2])
    return out
