"""dialog.py — Phase 14 Dialog Generator (deterministic v1).

Two characters (default: Architect + Practitioner) have a scripted
exchange about a topic. The generator finds concepts in the topic's
neighborhood where the two characters score divergently; each
divergence becomes a dialogue beat.

Output:
    dialogues/<topic>/
      dialogue.md           script form
      _stage_directions.md  per-beat sourcing notes
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

LOG = logging.getLogger("mypub-dialog")

GENERATOR_TYPE = "dialog"
DEFAULT_MAX_BEATS = 6
DEFAULT_NEIGHBOR_LIMIT = 25


@dataclass
class _Beat:
    ordinal: int
    concept_id: int
    concept_name: str
    concept_description: Optional[str]
    score_a: float                     # character A's score
    score_b: float                     # character B's score
    favors: str                        # "A" | "B" | "neutral"


@dataclass
class _Decomposition:
    topic_concept_id: int
    topic_name: str
    char_a: Character
    char_b: Character
    beats: list[_Beat]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "topic"


class DivergenceDecomposer:
    """Find concepts where two characters score divergently."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        char_a: Optional[Character] = None,
        char_b: Optional[Character] = None,
        max_beats: int = DEFAULT_MAX_BEATS,
        neighbor_limit: int = DEFAULT_NEIGHBOR_LIMIT,
        **_: Any,
    ) -> _Decomposition:
        a = char_a or ARCHITECT
        b = char_b or PRACTITIONER

        topic_id = resolver.resolve_lookup_only(query)
        if topic_id is None:
            return _Decomposition(
                topic_concept_id=-1, topic_name=query,
                char_a=a, char_b=b, beats=[],
                notes=[f"topic concept {query!r} not found"],
            )
        topic_name = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [topic_id],
        ).fetchone()[0]

        # Neighbors of the topic
        rows = conn.execute(
            """
            SELECT DISTINCT to_concept_id AS cid FROM concept_relation
             WHERE from_concept_id = ?
            UNION SELECT from_concept_id FROM concept_relation
             WHERE to_concept_id = ?
            """,
            [topic_id, topic_id],
        ).fetchall()
        neighbors = [int(r[0]) for r in rows if int(r[0]) != topic_id][:neighbor_limit]

        # Score each by both characters
        beats: list[_Beat] = []
        for cid in neighbors:
            sa = score_concept_for_character(conn, cid, a)
            sb = score_concept_for_character(conn, cid, b)
            spread = abs(sa - sb)
            if spread < 1.0:
                continue
            row = conn.execute(
                "SELECT name, description FROM concept WHERE concept_id = ?",
                [cid],
            ).fetchone()
            favors = "A" if sa > sb else ("B" if sb > sa else "neutral")
            beats.append(_Beat(
                ordinal=0,
                concept_id=cid, concept_name=row[0],
                concept_description=row[1],
                score_a=sa, score_b=sb, favors=favors,
            ))
        beats.sort(key=lambda x: -abs(x.score_a - x.score_b))
        beats = beats[:max_beats]
        for i, beat in enumerate(beats, start=1):
            beat.ordinal = i

        notes: list[str] = []
        if not beats:
            notes.append(
                f"no divergent concepts found — both characters rank the "
                f"topic's neighbors similarly. Try different character "
                f"profiles or a topic with more polarized neighborhood."
            )

        return _Decomposition(
            topic_concept_id=topic_id, topic_name=topic_name,
            char_a=a, char_b=b, beats=beats, notes=notes,
        )


def _render_dialog(d: _Decomposition) -> str:
    lines = [
        f"# Dialogue: {d.topic_name}",
        "",
        f"_{d.char_a.name}: {d.char_a.bio}_",
        f"_{d.char_b.name}: {d.char_b.bio}_",
        "",
    ]
    if not d.beats:
        lines.append(
            "_The characters agree on every neighbor of this topic. "
            "No dialogue beats produced._"
        )
        return "\n".join(lines)
    for beat in d.beats:
        leader = d.char_a.name if beat.favors == "A" else d.char_b.name
        responder = d.char_b.name if beat.favors == "A" else d.char_a.name
        lines.append(f"## Beat {beat.ordinal}: {beat.concept_name}")
        lines.append("")
        desc = (beat.concept_description or "this concept").strip()
        lines.append(f"**{leader}:** {desc}")
        lines.append("")
        if beat.favors == "A":
            counter = (
                f"That framing under-weights what current docs surface. "
                f"From a {d.char_b.preferred_era}-era perspective, "
                f"{beat.concept_name} looks different."
            )
        else:
            counter = (
                f"That's the modern doc's framing. The architectural "
                f"history of {beat.concept_name} tells a different story."
            )
        lines.append(f"**{responder}:** {counter}")
        lines.append("")
    return "\n".join(lines)


def _render_stage_directions(d: _Decomposition) -> str:
    lines = [
        f"# {d.topic_name} — Stage Directions",
        "",
        f"For each beat, the score breakdown showing why each character "
        f"takes the position they do.",
        "",
    ]
    if not d.beats:
        lines.append("_No beats — see dialogue.md for the explanation._")
        return "\n".join(lines)
    lines.extend([
        "| Beat | Concept | "
        f"{d.char_a.name} score | {d.char_b.name} score | Spread | Favors |",
        "|---|---|---:|---:|---:|:---:|",
    ])
    for beat in d.beats:
        spread = abs(beat.score_a - beat.score_b)
        lines.append(
            f"| {beat.ordinal} | **{beat.concept_name}** | "
            f"{beat.score_a:.1f} | {beat.score_b:.1f} | "
            f"{spread:.1f} | {beat.favors} |"
        )
    return "\n".join(lines)


class DialogPlanner:
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
                "char_a": d.char_a.name,
                "char_b": d.char_b.name,
                "n_beats": len(d.beats),
            },
            notes=list(d.notes),
        )
        for beat in d.beats:
            plan.units.append(GenUnit(
                unit_type="dialog_beat",
                name=beat.concept_name,
                ordinal=beat.ordinal,
                metadata={
                    "concept_id": beat.concept_id,
                    "score_a": beat.score_a,
                    "score_b": beat.score_b,
                    "favors": beat.favors,
                },
                logical_key=f"beat_{beat.ordinal}",
                content_markdown=beat.concept_description or "",
                sources=[("concept", beat.concept_id, abs(beat.score_a - beat.score_b),
                           1.0, None)],
            ))
        plan.files.extend([
            GenFile(filename="dialogue.md", content=_render_dialog(d), purpose="dialogue"),
            GenFile(filename="_stage_directions.md",
                    content=_render_stage_directions(d), purpose="directions"),
        ])
        return plan


class DialogValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("topic_concept_id", -1) == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="topic concept not resolved",
            ))
            return issues
        if plan.package_metadata.get("n_beats", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no divergent beats; characters agree throughout",
            ))
        return issues


class DialogMaterializer:
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


def make_dialog_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=DivergenceDecomposer(),
        planner=DialogPlanner(),
        ranking_mode="interactive",
        validator=DialogValidator(),
        materializer=DialogMaterializer(),
    )
