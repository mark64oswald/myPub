"""author_panel.py — Phase 14 Author Panel Generator (deterministic v1).

N>2 characters debate per-topic positions. For each topic, every
character scores the topic; the spread across characters becomes the
"tension" surfaced in the panel grid.

Output:
    panels/<panel-name>/
      _panel.md             per-topic position grid
      authors/<slug>.md     each character's positions across all topics
"""
from __future__ import annotations

import logging
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
from character import (
    Character,
    ARCHITECT,
    PRACTITIONER,
    score_concept_for_character,
)

LOG = logging.getLogger("mypub-author-panel")

GENERATOR_TYPE = "author_panel"


@dataclass
class _TopicPosition:
    concept_id: int
    concept_name: str
    scores: dict[str, float]              # character_name -> score
    spread: float                         # max - min across characters


@dataclass
class _Decomposition:
    panel_name: str
    characters: list[Character]
    topics: list[_TopicPosition]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "panel"


class PanelDecomposer:
    """For each topic in the input list, score across N characters."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        topics: Optional[list[str]] = None,
        characters: Optional[list[Character]] = None,
        panel_name: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        if topics is None:
            topics = [t.strip() for t in (query or "").split(",") if t.strip()]
        chars = characters or [ARCHITECT, PRACTITIONER]
        if len(chars) < 2:
            chars = [ARCHITECT, PRACTITIONER]

        positions: list[_TopicPosition] = []
        notes: list[str] = []
        for topic in topics:
            tid = resolver.resolve_lookup_only(topic)
            if tid is None:
                notes.append(f"topic {topic!r} not found; skipped")
                continue
            row = conn.execute(
                "SELECT name FROM concept WHERE concept_id = ?", [tid],
            ).fetchone()
            scores: dict[str, float] = {}
            for c in chars:
                scores[c.name] = score_concept_for_character(conn, tid, c)
            spread = max(scores.values()) - min(scores.values())
            positions.append(_TopicPosition(
                concept_id=tid, concept_name=row[0],
                scores=scores, spread=spread,
            ))
        positions.sort(key=lambda p: -p.spread)
        return _Decomposition(
            panel_name=panel_name or query or "Panel",
            characters=chars, topics=positions, notes=notes,
        )


def _render_panel(d: _Decomposition) -> str:
    lines = [f"# Author Panel: {d.panel_name}", ""]
    if not d.topics:
        lines.append("_No topics resolved._")
        return "\n".join(lines)
    char_names = [c.name for c in d.characters]
    lines.append("## Cast")
    lines.append("")
    for c in d.characters:
        lines.append(f"- **{c.name}** — {c.bio}")
    lines.append("")
    lines.append("## Positions")
    lines.append("")
    header = "| Topic | " + " | ".join(char_names) + " | Spread |"
    sep = "|---|" + "|".join(["---:"] * (len(char_names) + 1)) + "|"
    lines.extend([header, sep])
    for p in d.topics:
        cells = [f"{p.scores.get(n, 0.0):.1f}" for n in char_names]
        lines.append(f"| **{p.concept_name}** | " + " | ".join(cells)
                     + f" | {p.spread:.1f} |")
    lines.append("")
    lines.append("_Higher score = topic aligns more with that character's "
                 "preferred relations and concept types. High-spread topics "
                 "are where the panel disagrees most._")
    return "\n".join(lines)


def _render_author(d: _Decomposition, character: Character) -> str:
    lines = [
        f"# {character.name}",
        "",
        f"_{character.bio}_",
        "",
        f"**Preferred relations:** {', '.join(character.preferred_relations) or '—'}  |  "
        f"**Preferred concept types:** {', '.join(character.preferred_concept_types) or '—'}  |  "
        f"**Preferred era:** {character.preferred_era}",
        "",
        f"## Positions on the panel topics",
        "",
    ]
    if not d.topics:
        lines.append("_No topics in the panel._")
        return "\n".join(lines)
    sorted_topics = sorted(
        d.topics, key=lambda p: -p.scores.get(character.name, 0.0),
    )
    for p in sorted_topics:
        my = p.scores.get(character.name, 0.0)
        avg = sum(p.scores.values()) / max(1, len(p.scores))
        rel = "above-avg" if my > avg else ("below-avg" if my < avg else "on-avg")
        lines.append(f"- **{p.concept_name}** — score {my:.1f} ({rel})")
    return "\n".join(lines)


class AuthorPanelPlanner:
    def plan(self, conn, decomposition, *, package_name=None, **_) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.panel_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.panel_name,
            source_query=d.panel_name,
            package_metadata={
                "n_characters": len(d.characters),
                "n_topics": len(d.topics),
                "max_spread": max((p.spread for p in d.topics), default=0.0),
            },
            notes=list(d.notes),
        )
        for i, p in enumerate(d.topics, start=1):
            plan.units.append(GenUnit(
                unit_type="panel_topic",
                name=p.concept_name,
                ordinal=i,
                metadata={
                    "concept_id": p.concept_id,
                    "spread": p.spread,
                    "scores": p.scores,
                },
                logical_key=f"topic_{p.concept_id}",
                content_markdown="",
                sources=[("concept", p.concept_id, p.spread, 1.0, None)],
            ))
        plan.files.append(GenFile(
            filename="_panel.md", content=_render_panel(d), purpose="panel",
        ))
        for c in d.characters:
            plan.files.append(GenFile(
                filename=f"authors/{_slugify(c.name)}.md",
                content=_render_author(d, c), purpose="author",
            ))
        return plan


class AuthorPanelValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("n_characters", 0) < 2:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="panel needs ≥2 characters",
            ))
        if plan.package_metadata.get("n_topics", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no topics resolved",
            ))
        return issues


class AuthorPanelMaterializer:
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


def make_author_panel_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=PanelDecomposer(),
        planner=AuthorPanelPlanner(),
        ranking_mode="interactive",
        validator=AuthorPanelValidator(),
        materializer=AuthorPanelMaterializer(),
    )
