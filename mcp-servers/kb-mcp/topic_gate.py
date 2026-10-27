"""topic_gate.py — Keep generator picks on-topic via concept embeddings.

The Learning Path fix (learning_path.PrerequisiteDecomposer's
``min_similarity``) showed that a cosine floor against the target
concept stops hub concepts ("Machine Learning", "Kubernetes") from
taking over. Dialog beats, Tutorial procedures and Content Brief
anchors pick from the same graph and drift the same way, so they share
this helper.

Rules, matching the Learning Path gate:
  * a candidate below ``min_similarity`` to the topic is dropped;
  * a candidate with no embedding passes (no evidence either way);
  * if the topic itself has no embedding, nothing is gated;
  * ``min_similarity <= 0`` turns the gate off.
"""
from __future__ import annotations

from typing import Iterable, Optional

import duckdb

DEFAULT_MIN_SIMILARITY = 0.25


def _has_embedding(conn: duckdb.DuckDBPyConnection, concept_id: int) -> bool:
    return conn.execute("SELECT 1 FROM concept_embedding WHERE concept_id = ?",
                        [concept_id]).fetchone() is not None


def concept_similarities(
    conn: duckdb.DuckDBPyConnection, topic_id: int, ids: Iterable[int],
) -> dict[int, Optional[float]]:
    """Cosine of each concept to the topic; None when either has no embedding."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids or not _has_embedding(conn, topic_id):
        return {i: None for i in ids}
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT e.concept_id,
               array_cosine_similarity(e.embedding,
                   (SELECT embedding FROM concept_embedding WHERE concept_id = ?))
          FROM concept_embedding e
         WHERE e.concept_id IN ({ph})
        """, [topic_id, *ids]).fetchall()
    sims = {int(c): (float(s) if s is not None else None) for c, s in rows}
    return {i: sims.get(i) for i in ids}


def gate_concepts(
    conn: duckdb.DuckDBPyConnection, topic_id: int, ids: Iterable[int],
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> tuple[list[int], list[int], dict[int, Optional[float]]]:
    """(kept ids in input order, dropped ids, similarities)."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    sims = concept_similarities(conn, topic_id, ids)
    if min_similarity <= 0:
        return ids, [], sims
    kept = [i for i in ids if sims[i] is None or sims[i] >= min_similarity]
    dropped = [i for i in ids if i not in set(kept)]
    return kept, dropped, sims


def procedure_similarities(
    conn: duckdb.DuckDBPyConnection, topic_id: int, procedure_ids: Iterable[int],
) -> dict[int, Optional[float]]:
    """Mean cosine of each procedure's linked concepts to the topic."""
    pids = list(dict.fromkeys(int(p) for p in procedure_ids))
    if not pids or not _has_embedding(conn, topic_id):
        return {p: None for p in pids}
    ph = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""
        SELECT pc.procedure_id,
               avg(array_cosine_similarity(e.embedding,
                   (SELECT embedding FROM concept_embedding WHERE concept_id = ?)))
          FROM procedure_concept pc
          JOIN concept_embedding e ON e.concept_id = pc.concept_id
         WHERE pc.procedure_id IN ({ph})
         GROUP BY pc.procedure_id
        """, [topic_id, *pids]).fetchall()
    sims = {int(p): (float(s) if s is not None else None) for p, s in rows}
    return {p: sims.get(p) for p in pids}
