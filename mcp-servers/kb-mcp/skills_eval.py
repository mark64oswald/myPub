"""Phase 5.5 — Skills Factory routing eval.

Evaluates whether a generated package's SKILL.md ``description`` lines
discriminate well — given a query that should land on Skill X, does
Skill X's description rank #1 against its siblings under cosine
similarity?

The eval is a **proxy** for what Claude actually does at trigger time
(the live router uses an LLM, not embedding similarity). But the proxy
is fast, deterministic, and catches the failure modes the Factory is
most likely to produce: descriptions that are too generic, descriptions
that overlap with siblings, descriptions that fail to mention the
Skill's own anchor concept.

## Known limitation: proxy disagrees with intent matching

Empirically (cdc package regen, 2026-05-07), tightening the generation
prompt to produce more discriminative descriptions made the eval scores
WORSE, not better — even though the new descriptions read clearly
better and do their real job (LLM intent matching) better.

The proxy rewards token overlap. So a description that says "fires when
the user asks about X; for Y see the Y Skill" matches queries about Y
because Y appears in its text — the proxy can't tell that "see Y" is a
*delegation*, not an *invocation*. A tightened description that uses
niche vocabulary (function names, parameter names) instead of sibling
references scores LOWER on the proxy because queries don't share that
niche vocabulary.

Treat scores as a smoke test for empty/generic descriptions, not as a
quality ranking. R@1 < 0.3 means something is broken (empty
description, total mis-routing). Higher scores reward the wrong thing
for the routing layer Claude actually uses.

## Query synthesis

Queries come from two non-circular signals (neither is derived from the
generated ``description`` itself):

* **Skill name** — the cluster's chosen name. Always a positive.
* **Source-chapter concepts** — concepts that appear in the chapters
  / sections this Skill's ``skill_source`` points at, via
  ``concept_relation``. We rank them by how *exclusive* they are to
  this Skill (i.e. concepts that appear in this Skill's sources but
  not in the siblings' sources are stronger discriminators).

If a Skill has no concept-bearing source chapters, we fall back to the
name-only query and flag the skill in ``notes``.

## Metrics

Per query: rank of the correct skill (1-based) under cosine similarity
against every Skill description in the package.

Per package:
* ``recall_at_1`` — fraction of queries where the correct Skill ranked #1.
* ``recall_at_3`` — fraction at top-3.
* ``mrr`` — mean reciprocal rank.

Per Skill: same metrics restricted to queries with that Skill as
ground truth — useful for spotting one bad description in an otherwise
healthy package.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import duckdb


DEFAULT_QUERIES_PER_SKILL = 5
DEFAULT_MIN_DISCRIMINATIVE_CONCEPTS = 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalQuery:
    """One eval query with its ground-truth target skill."""

    skill_id: int
    query: str
    source: str  # "name" | "concept_exclusive" | "concept_shared" | "user"


@dataclass
class QueryResult:
    """Result of routing one query against the package."""

    query: EvalQuery
    rank: int  # 1-based; package-size+1 means total miss (shouldn't happen
               # since ground-truth skill is always in the candidate set)
    top_skill_id: int
    top_similarity: float
    correct_similarity: float


@dataclass
class RoutingMetrics:
    """Aggregate routing metrics for a query set."""

    n_queries: int
    recall_at_1: float
    recall_at_3: float
    mrr: float


@dataclass
class EvalReport:
    """Full eval output for one package."""

    package_id: int
    package_name: str
    n_skills: int
    overall: RoutingMetrics
    per_skill: dict[int, RoutingMetrics] = field(default_factory=dict)
    per_query: list[QueryResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------


@dataclass
class _SkillRow:
    skill_id: int
    name: str
    description: str


def _load_skills(
    conn: duckdb.DuckDBPyConnection, package_id: int,
) -> list[_SkillRow]:
    rows = conn.execute(
        """
        SELECT skill_id, name, COALESCE(description, '')
          FROM skill
         WHERE package_id = ?
         ORDER BY skill_id
        """,
        [package_id],
    ).fetchall()
    return [_SkillRow(int(r[0]), r[1], r[2]) for r in rows]


def _load_package_meta(
    conn: duckdb.DuckDBPyConnection, package_id: int,
) -> Optional[tuple[int, str]]:
    row = conn.execute(
        "SELECT package_id, name FROM skill_package WHERE package_id = ?",
        [package_id],
    ).fetchone()
    if not row:
        return None
    return int(row[0]), row[1]


# ---------------------------------------------------------------------------
# Query synthesis
# ---------------------------------------------------------------------------


def _concepts_for_skill(
    conn: duckdb.DuckDBPyConnection, skill_id: int,
) -> dict[int, tuple[str, int]]:
    """Return ``{concept_id: (name, mention_count)}`` for concepts that
    appear in the chapters / doc sections this Skill's
    ``skill_source`` rows point at, with ``drop_reason IS NULL``
    (i.e. selected, not dropped)."""
    rows = conn.execute(
        """
        WITH chap_sources AS (
          SELECT source_id AS chapter_id
            FROM skill_source
           WHERE skill_id = ?
             AND source_type = 'chapter'
             AND drop_reason IS NULL
        ),
        section_sources AS (
          SELECT source_id AS section_id
            FROM skill_source
           WHERE skill_id = ?
             AND source_type = 'doc_section'
             AND drop_reason IS NULL
        ),
        concept_hits AS (
          SELECT cr.from_concept_id AS concept_id
            FROM concept_relation cr
            JOIN chap_sources cs ON cs.chapter_id = cr.source_id
           WHERE cr.source_type = 'chapter'
          UNION ALL
          SELECT cr.to_concept_id AS concept_id
            FROM concept_relation cr
            JOIN chap_sources cs ON cs.chapter_id = cr.source_id
           WHERE cr.source_type = 'chapter'
          UNION ALL
          SELECT cr.from_concept_id
            FROM concept_relation cr
            JOIN section_sources ss ON ss.section_id = cr.source_id
           WHERE cr.source_type = 'doc_section'
          UNION ALL
          SELECT cr.to_concept_id
            FROM concept_relation cr
            JOIN section_sources ss ON ss.section_id = cr.source_id
           WHERE cr.source_type = 'doc_section'
        )
        SELECT ch.concept_id,
               c.name,
               COUNT(*) AS mentions
          FROM concept_hits ch
          JOIN concept c ON c.concept_id = ch.concept_id
         GROUP BY ch.concept_id, c.name
         ORDER BY mentions DESC
        """,
        [skill_id, skill_id],
    ).fetchall()
    return {int(r[0]): (r[1], int(r[2])) for r in rows}


def synthesize_queries(
    conn: duckdb.DuckDBPyConnection,
    package_id: int,
    *,
    queries_per_skill: int = DEFAULT_QUERIES_PER_SKILL,
) -> tuple[list[EvalQuery], list[str]]:
    """Synthesize eval queries for every Skill in the package.

    Returns ``(queries, notes)``. ``notes`` carries warnings about
    Skills that yielded fewer queries than requested (typically because
    they have no concept-bearing source chapters).

    For each Skill we emit:

    1. The Skill name as a query (always).
    2. Up to ``queries_per_skill - 1`` concept names drawn from the
       Skill's source chapters/sections, ranked by *exclusivity* — a
       concept that appears in this Skill's sources but not in any
       sibling's sources is a stronger discriminator than one shared
       across siblings.
    """
    skills = _load_skills(conn, package_id)
    if not skills:
        return [], [f"package {package_id} has no skills"]

    # Concept set per skill
    per_skill_concepts: dict[int, dict[int, tuple[str, int]]] = {
        s.skill_id: _concepts_for_skill(conn, s.skill_id) for s in skills
    }
    # Sibling concept set per skill (concepts in *other* skills)
    sibling_concepts: dict[int, set[int]] = {}
    for s in skills:
        sib: set[int] = set()
        for other in skills:
            if other.skill_id == s.skill_id:
                continue
            sib.update(per_skill_concepts[other.skill_id].keys())
        sibling_concepts[s.skill_id] = sib

    notes: list[str] = []
    queries: list[EvalQuery] = []

    for s in skills:
        # Query 1: skill name verbatim
        queries.append(EvalQuery(
            skill_id=s.skill_id, query=s.name, source="name"))

        budget = queries_per_skill - 1
        if budget <= 0:
            continue

        my_concepts = per_skill_concepts[s.skill_id]
        sibs = sibling_concepts[s.skill_id]
        exclusive = [
            (cid, nm, m) for cid, (nm, m) in my_concepts.items()
            if cid not in sibs
        ]
        shared = [
            (cid, nm, m) for cid, (nm, m) in my_concepts.items()
            if cid in sibs
        ]
        # Sort each by mention count desc.
        exclusive.sort(key=lambda r: -r[2])
        shared.sort(key=lambda r: -r[2])

        added = 0
        # Prefer exclusive concepts; they're the stronger discriminators.
        for cid, nm, _ in exclusive:
            if added >= budget:
                break
            if nm.strip().lower() == s.name.strip().lower():
                continue  # don't duplicate the name query
            queries.append(EvalQuery(
                skill_id=s.skill_id, query=nm, source="concept_exclusive"))
            added += 1
        # Backfill with shared concepts if exclusive ran out.
        for cid, nm, _ in shared:
            if added >= budget:
                break
            if nm.strip().lower() == s.name.strip().lower():
                continue
            queries.append(EvalQuery(
                skill_id=s.skill_id, query=nm, source="concept_shared"))
            added += 1

        if added < budget:
            notes.append(
                f"skill_id={s.skill_id} ({s.name!r}) yielded {added}/{budget} "
                f"concept queries — source chapters lack concept relations"
            )

    return queries, notes


# ---------------------------------------------------------------------------
# Embedding + scoring
# ---------------------------------------------------------------------------


EmbedFn = Callable[[list[str]], list[list[float]]]
"""Signature: takes a batch of strings, returns a batch of float vectors.
``score_routing`` injects this so tests can pass a deterministic fake."""


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _aggregate_metrics(results: list[QueryResult]) -> RoutingMetrics:
    n = len(results)
    if n == 0:
        return RoutingMetrics(0, 0.0, 0.0, 0.0)
    r1 = sum(1 for r in results if r.rank == 1) / n
    r3 = sum(1 for r in results if r.rank <= 3) / n
    mrr = sum(1.0 / r.rank for r in results) / n
    return RoutingMetrics(n_queries=n, recall_at_1=r1, recall_at_3=r3, mrr=mrr)


def score_routing(
    conn: duckdb.DuckDBPyConnection,
    package_id: int,
    queries: list[EvalQuery],
    *,
    embed_fn: EmbedFn,
) -> EvalReport:
    """Embed each Skill description and each query, rank skills by
    cosine similarity for each query, and roll up metrics.

    The ground-truth Skill is always in the candidate set, so a query
    can never be a total miss — only mis-ranked.
    """
    meta = _load_package_meta(conn, package_id)
    if meta is None:
        return EvalReport(
            package_id=package_id, package_name="(missing)",
            n_skills=0,
            overall=RoutingMetrics(0, 0.0, 0.0, 0.0),
            notes=[f"package {package_id} not found"],
        )
    pkg_id, pkg_name = meta

    skills = _load_skills(conn, package_id)
    if not skills:
        return EvalReport(
            package_id=pkg_id, package_name=pkg_name, n_skills=0,
            overall=RoutingMetrics(0, 0.0, 0.0, 0.0),
            notes=[f"package {pkg_id} has no skills"],
        )

    # Embed descriptions once (use the skill name as a fallback when the
    # description is empty — empty descriptions are a Factory bug worth
    # surfacing, but we still want a usable embedding so the eval can run).
    desc_texts = [s.description if s.description.strip() else s.name for s in skills]
    desc_vecs = embed_fn(desc_texts)
    if len(desc_vecs) != len(skills):
        raise ValueError(
            f"embed_fn returned {len(desc_vecs)} vectors for {len(skills)} skills"
        )

    if not queries:
        return EvalReport(
            package_id=pkg_id, package_name=pkg_name, n_skills=len(skills),
            overall=RoutingMetrics(0, 0.0, 0.0, 0.0),
            notes=["no queries supplied"],
        )

    # Embed all queries in one batch.
    query_vecs = embed_fn([q.query for q in queries])
    if len(query_vecs) != len(queries):
        raise ValueError(
            f"embed_fn returned {len(query_vecs)} vectors for {len(queries)} queries"
        )

    skill_index = {s.skill_id: i for i, s in enumerate(skills)}
    notes: list[str] = []
    if any(not s.description.strip() for s in skills):
        empties = [s.skill_id for s in skills if not s.description.strip()]
        notes.append(
            f"skill descriptions empty for skill_ids={empties}; "
            f"fell back to skill name for embedding"
        )

    per_query: list[QueryResult] = []
    for q, qv in zip(queries, query_vecs):
        sims = [(s.skill_id, _cosine(qv, dv))
                for s, dv in zip(skills, desc_vecs)]
        # Higher cosine = more similar; sort descending.
        sims.sort(key=lambda t: -t[1])
        rank = next(
            (i + 1 for i, (sid, _) in enumerate(sims) if sid == q.skill_id),
            len(skills) + 1,
        )
        correct_idx = skill_index[q.skill_id]
        correct_sim = _cosine(qv, desc_vecs[correct_idx])
        per_query.append(QueryResult(
            query=q,
            rank=rank,
            top_skill_id=sims[0][0],
            top_similarity=sims[0][1],
            correct_similarity=correct_sim,
        ))

    overall = _aggregate_metrics(per_query)
    per_skill: dict[int, RoutingMetrics] = {}
    for s in skills:
        per_skill[s.skill_id] = _aggregate_metrics(
            [r for r in per_query if r.query.skill_id == s.skill_id]
        )

    return EvalReport(
        package_id=pkg_id, package_name=pkg_name, n_skills=len(skills),
        overall=overall, per_skill=per_skill, per_query=per_query,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_routing_eval(
    conn: duckdb.DuckDBPyConnection,
    package_id: int,
    *,
    queries_per_skill: int = DEFAULT_QUERIES_PER_SKILL,
    embed_fn: EmbedFn,
    extra_queries: Optional[list[EvalQuery]] = None,
) -> EvalReport:
    """One-shot: synthesize queries + score routing.

    ``extra_queries`` (optional) lets callers append hand-curated queries
    (``source="user"``) on top of the synthesized set. They're scored
    the same way and folded into the same metrics.
    """
    queries, synth_notes = synthesize_queries(
        conn, package_id, queries_per_skill=queries_per_skill,
    )
    if extra_queries:
        queries.extend(extra_queries)
    report = score_routing(conn, package_id, queries, embed_fn=embed_fn)
    report.notes = synth_notes + report.notes
    return report


# ---------------------------------------------------------------------------
# Convenience: serialize report to a JSON-friendly dict
# ---------------------------------------------------------------------------


def report_to_dict(report: EvalReport) -> dict[str, Any]:
    """Flatten an ``EvalReport`` for JSON serialization (MCP tool returns)."""
    return {
        "package_id": report.package_id,
        "package_name": report.package_name,
        "n_skills": report.n_skills,
        "overall": {
            "n_queries": report.overall.n_queries,
            "recall_at_1": report.overall.recall_at_1,
            "recall_at_3": report.overall.recall_at_3,
            "mrr": report.overall.mrr,
        },
        "per_skill": {
            str(sid): {
                "n_queries": m.n_queries,
                "recall_at_1": m.recall_at_1,
                "recall_at_3": m.recall_at_3,
                "mrr": m.mrr,
            }
            for sid, m in report.per_skill.items()
        },
        "per_query": [
            {
                "skill_id": r.query.skill_id,
                "query": r.query.query,
                "source": r.query.source,
                "rank": r.rank,
                "top_skill_id": r.top_skill_id,
                "top_similarity": r.top_similarity,
                "correct_similarity": r.correct_similarity,
            }
            for r in report.per_query
        ],
        "notes": list(report.notes),
    }
