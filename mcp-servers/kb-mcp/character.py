"""character.py — Phase 14 character profile system.

A "character" is a view function over the ranking engine. Each
character has preferences over relations and concept_types that bias
which sources/concepts they prefer when ranking.

Used by:
  * Dialog generator — 2 characters; divergence between them produces
    dialogue beats.
  * Author Panel generator — N>2 characters; per-topic positions with
    cross-character tensions.

This is a deterministic-first model: characters score concepts via a
weighted sum over preference-aligned signals (relation type counts,
concept_type matches, era preference). The actual prose generation is
deferred to a future v2 sub-agent layer; v1 produces structured
position cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import duckdb


@dataclass
class Character:
    """One viewpoint over the ranking engine.

    Attributes:
        name: Display name (e.g. "Architect", "Practitioner").
        bio: One-line biographical seed used in rendered output.
        preferred_relations: Relation types this character favors.
        preferred_concept_types: Concept types this character favors.
        preferred_era: 'classical' | 'recent' | 'current' — which era
            of source the character anchors on. (v1 doesn't enforce
            this; reserved for v2 ranking-mode integration.)
    """

    name: str
    bio: str = ""
    preferred_relations: list[str] = field(default_factory=list)
    preferred_concept_types: list[str] = field(default_factory=list)
    preferred_era: str = "current"


# Default Dialog cast.
ARCHITECT = Character(
    name="Architect",
    bio="Pattern-oriented; values long-lived structural decisions.",
    preferred_relations=["IMPLEMENTS", "EXTENDS", "REQUIRES"],
    preferred_concept_types=["Pattern", "Concept"],
    preferred_era="classical",
)

PRACTITIONER = Character(
    name="Practitioner",
    bio="Implementation-oriented; trusts current vendor docs.",
    preferred_relations=["CITES"],
    preferred_concept_types=["Tool", "Framework", "Technique"],
    preferred_era="current",
)


def score_concept_for_character(
    conn: duckdb.DuckDBPyConnection,
    concept_id: int,
    character: Character,
) -> float:
    """Score how well ``concept_id`` aligns with ``character``'s view.

    Sum of:
      * +2 per outgoing relation matching preferred_relations
      * +3 if concept_type matches preferred_concept_types
      * +1 per chapter that mentions the concept (baseline coverage)
    """
    score = 0.0

    # Relation bonus
    if character.preferred_relations:
        ph = ",".join(["?"] * len(character.preferred_relations))
        rel_n = conn.execute(
            f"""
            SELECT COUNT(*) FROM concept_relation
             WHERE (from_concept_id = ? OR to_concept_id = ?)
               AND relation_type IN ({ph})
            """,
            [concept_id, concept_id, *character.preferred_relations],
        ).fetchone()[0] or 0
        score += rel_n * 2.0

    # Concept-type bonus
    if character.preferred_concept_types:
        row = conn.execute(
            "SELECT concept_type FROM concept WHERE concept_id = ?",
            [concept_id],
        ).fetchone()
        if row and row[0] in character.preferred_concept_types:
            score += 3.0

    # Coverage baseline
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
        [concept_id, concept_id],
    ).fetchone()[0] or 0
    score += float(chap_n)
    return score


def parse_character_json(payload: list[dict[str, Any]]) -> list[Character]:
    """Parse a list-of-dicts character spec into Character objects."""
    out: list[Character] = []
    for d in payload:
        out.append(Character(
            name=str(d.get("name") or "Anonymous"),
            bio=str(d.get("bio") or ""),
            preferred_relations=list(d.get("preferred_relations") or []),
            preferred_concept_types=list(d.get("preferred_concept_types") or []),
            preferred_era=str(d.get("preferred_era") or "current"),
        ))
    return out
