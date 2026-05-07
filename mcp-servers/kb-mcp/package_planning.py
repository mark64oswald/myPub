"""package_planning.py — Phase 5.2 Skills Factory: package planning.

Given a ``DecompositionResult`` from Phase 5.1, decide:

  * **Order** — Skills are emitted in dependency-respecting order using
    ``concept_relation`` REQUIRES edges between cluster anchors. A
    Skill whose anchor depends on another Skill's anchor lands later.
  * **Strategy** per Skill — one of three §8.3 selection strategies:
      - ``recent_doc_anchored``: the Skill's anchor maps to a registered
        ``doc_source`` (Apache Spark, PostgreSQL, Delta Lake, etc.).
        Fresh vendor docs are the source of truth.
      - ``consensus_synthesis``: multiple book authors discuss this
        Skill (≥3 distinct ``concept_relation`` source chapters from
        different books). Synthesize across consensus.
      - ``authority_pick``: a single high-authority book chapter
        dominates (top chapter has ``authority ≥ 0.85`` and clearly
        outscores siblings).
  * **Folder layout** — slugified per-Skill directory names ready for
    Phase 5.4 materialization.
  * **Cross-references** — Skills whose anchor concept appears in
    *another* Skill's concept set get a reference link rather than
    duplicating coverage.

This module makes NO LLM calls and writes nothing to disk. The
``PackagePlan`` it returns is the input to Phase 5.3 (per-Skill
generation) and Phase 5.4 (package materialization).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import duckdb

from decomposition import DecompositionResult, ProposedSkill

LOG = logging.getLogger("mypub-package-planning")


# Selection-strategy thresholds. Tunable in Phase 5.5 eval against
# real Skills-Factory output.

# Authority floor for a single top-chapter to count as "dominant".
AUTHORITY_DOMINANT_FLOOR = 0.85

# Margin between top and second-place authority for authority_pick.
AUTHORITY_DOMINANCE_MARGIN = 0.15

# Minimum distinct books contributing to a Skill for consensus_synthesis.
CONSENSUS_MIN_BOOKS = 3

# Strategy names — must match ranking.SELECTION_STRATEGIES.
STRATEGY_RECENT_DOC = "recent_doc_anchored"
STRATEGY_CONSENSUS = "consensus_synthesis"
STRATEGY_AUTHORITY = "authority_pick"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PlannedSkill:
    """A ProposedSkill with planning decisions attached."""

    proposed: ProposedSkill
    order: int                                       # 0-indexed in topo order
    strategy: str                                    # one of STRATEGY_*
    strategy_rationale: str                          # one-line explanation
    folder_name: str                                 # filesystem-safe slug
    requires_cluster_ids: list[int] = field(default_factory=list)
    references_cluster_ids: list[int] = field(default_factory=list)


@dataclass
class PackagePlan:
    """Top-level output of plan_package()."""

    package_name: str
    domain: str
    folder_root: str
    planned_skills: list[PlannedSkill] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Slugification
# ---------------------------------------------------------------------------


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify(name: str, *, max_len: int = 50) -> str:
    """Filesystem-safe slug: lowercase, alphanumerics + hyphens, capped length."""
    if not name:
        return "skill"
    s = _SLUG_NON_ALNUM.sub("-", name.lower()).strip("-")
    if not s:
        return "skill"
    return s[:max_len].rstrip("-")


# ---------------------------------------------------------------------------
# Step 1: dependency ordering (topological sort)
# ---------------------------------------------------------------------------


def build_dependency_edges(
    conn: duckdb.DuckDBPyConnection,
    skills: list[ProposedSkill],
) -> list[tuple[int, int]]:
    """Return list of (from_cluster_id, to_cluster_id) REQUIRES edges.

    A Skill A REQUIRES Skill B if any concept in A's cluster has a
    REQUIRES edge to any concept in B's cluster (and the two clusters
    are different). Self-edges within a cluster are ignored.

    Edge direction: ``A REQUIRES B`` means B is a prerequisite — B
    should be emitted before A in topological order.
    """
    if len(skills) < 2:
        return []

    cluster_of: dict[int, int] = {}
    for sk in skills:
        for cid in sk.concept_ids:
            cluster_of[int(cid)] = sk.cluster_id

    if not cluster_of:
        return []

    placeholders = ",".join(["?"] * len(cluster_of))
    rows = conn.execute(
        f"""
        SELECT DISTINCT from_concept_id, to_concept_id
          FROM concept_relation
         WHERE relation_type = 'REQUIRES'
           AND from_concept_id IN ({placeholders})
           AND to_concept_id   IN ({placeholders})
        """,
        [*cluster_of.keys(), *cluster_of.keys()],
    ).fetchall()

    edges: set[tuple[int, int]] = set()
    for f, t in rows:
        if f is None or t is None:
            continue
        cf = cluster_of.get(int(f))
        ct = cluster_of.get(int(t))
        if cf is None or ct is None or cf == ct:
            continue
        edges.add((cf, ct))
    return list(edges)


def topo_order(
    skills: list[ProposedSkill],
    edges: list[tuple[int, int]],
) -> list[int]:
    """Return cluster_ids in dependency-respecting order (prerequisites first).

    Uses Kahn's algorithm on the dependency graph. ``edges`` are
    interpreted as A → B meaning "A REQUIRES B" (B is prerequisite of
    A), so the topological sort places B before A.

    On cycles (rare for concept graphs but possible), break by
    largest cluster first as a heuristic, log a warning, and continue.
    """
    cluster_ids = [sk.cluster_id for sk in skills]
    if not cluster_ids:
        return []

    # Reverse edge direction so toposort emits prerequisites first.
    # edge (a, b) "a REQUIRES b" → b must come before a → in toposort
    # we treat b as having a dependent a, edge b → a.
    in_degree: dict[int, int] = {c: 0 for c in cluster_ids}
    forward: dict[int, list[int]] = {c: [] for c in cluster_ids}
    for a, b in edges:
        if a not in in_degree or b not in in_degree:
            continue
        forward[b].append(a)
        in_degree[a] += 1

    # Stable starting set: clusters with no requirements, ordered by
    # original cluster_id (which DecompositionResult sorts by size desc).
    ready = sorted([c for c, d in in_degree.items() if d == 0])
    out: list[int] = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for next_node in forward[node]:
            in_degree[next_node] -= 1
            if in_degree[next_node] == 0:
                ready.append(next_node)
                ready.sort()

    if len(out) != len(cluster_ids):
        # Cycle present — append remaining clusters by largest-first heuristic.
        remaining = [c for c in cluster_ids if c not in out]
        size = {sk.cluster_id: len(sk.concept_ids) for sk in skills}
        remaining.sort(key=lambda c: -size.get(c, 0))
        LOG.warning(
            "package_planning: cycle in REQUIRES graph; appending %d "
            "remaining clusters by size: %s",
            len(remaining), remaining,
        )
        out.extend(remaining)
    return out


# ---------------------------------------------------------------------------
# Step 2: strategy selection
# ---------------------------------------------------------------------------


def _has_doc_source_match(
    conn: duckdb.DuckDBPyConnection, skill: ProposedSkill,
) -> Optional[str]:
    """Check whether the Skill's anchor concept matches a registered doc_source.

    Returns the matched doc_source name, or None. Match is case-
    insensitive substring on either side: anchor name in doc_source
    name, OR doc_source name in anchor name. Catches "Apache Kafka"
    matching "Kafka" anchor and vice versa.
    """
    if not skill.anchor_concept_name:
        return None
    rows = conn.execute(
        """
        SELECT name FROM doc_source
         WHERE LOWER(?) LIKE '%' || LOWER(name) || '%'
            OR LOWER(name) LIKE '%' || LOWER(?) || '%'
        """,
        [skill.anchor_concept_name, skill.anchor_concept_name],
    ).fetchall()
    return rows[0][0] if rows else None


def _book_authority_stats(
    conn: duckdb.DuckDBPyConnection, skill: ProposedSkill,
) -> dict[str, Any]:
    """Compute authority + book-diversity stats for a Skill.

    Returns ``{
        'top_authority': float | None,        # highest book authority among top chapters
        'second_authority': float | None,     # second-highest (for margin check)
        'distinct_books': int,                # number of distinct books in top chapters
        'top_book_title': str | None,
    }``
    Used by strategy selection to decide between consensus_synthesis
    and authority_pick.
    """
    if not skill.top_chapters:
        return {
            "top_authority": None, "second_authority": None,
            "distinct_books": 0, "top_book_title": None,
        }

    chapter_ids = [c["chapter_id"] for c in skill.top_chapters]
    placeholders = ",".join(["?"] * len(chapter_ids))
    rows = conn.execute(
        f"""
        SELECT b.publisher, b.title, c.chapter_id
          FROM chapter c
          JOIN book b ON c.book_id = b.book_id
         WHERE c.chapter_id IN ({placeholders})
        """,
        chapter_ids,
    ).fetchall()

    # Compute authority per chapter via the publisher table.
    # pylint: disable=import-outside-toplevel
    from ranking import authority_score_from_publisher

    auths: list[tuple[float, str]] = []  # (authority, book_title)
    distinct_books: set[str] = set()
    for publisher, title, _chid in rows:
        a = authority_score_from_publisher(publisher)
        auths.append((a, title))
        if title:
            distinct_books.add(title)

    auths.sort(key=lambda x: x[0], reverse=True)
    return {
        "top_authority": auths[0][0] if auths else None,
        "second_authority": auths[1][0] if len(auths) > 1 else None,
        "distinct_books": len(distinct_books),
        "top_book_title": auths[0][1] if auths else None,
    }


def select_strategy(
    conn: duckdb.DuckDBPyConnection, skill: ProposedSkill,
) -> tuple[str, str]:
    """Choose one of the three §8.3 strategies + a one-line rationale.

    Selection rules (first-match wins):

      1. Anchor matches a registered doc_source → ``recent_doc_anchored``.
         Vendor-curated current docs are the source of truth.
      2. Top chapter has authority ≥ AUTHORITY_DOMINANT_FLOOR AND
         margin to second-place ≥ AUTHORITY_DOMINANCE_MARGIN AND
         only 1 book in the top chapters → ``authority_pick``.
         A canonical book dominates and there's no consensus pool
         to synthesize against.
      3. ≥CONSENSUS_MIN_BOOKS distinct books contribute → ``consensus_synthesis``.
         Multi-author corpus coverage is genuine consensus material.
      4. Default → ``consensus_synthesis``. The safest choice when
         strategy isn't clearly one of the others.
    """
    # Rule 1: registered doc_source coverage
    doc_match = _has_doc_source_match(conn, skill)
    if doc_match:
        return STRATEGY_RECENT_DOC, f"anchor matches doc_source {doc_match!r}"

    stats = _book_authority_stats(conn, skill)

    # Rule 2: single-book authority dominance
    top_a = stats["top_authority"]
    second_a = stats["second_authority"]
    if (top_a is not None and top_a >= AUTHORITY_DOMINANT_FLOOR
            and stats["distinct_books"] == 1):
        return (
            STRATEGY_AUTHORITY,
            f"single canonical source: {stats['top_book_title']!r} "
            f"(authority={top_a:.2f})",
        )
    if (top_a is not None and top_a >= AUTHORITY_DOMINANT_FLOOR
            and second_a is not None
            and (top_a - second_a) >= AUTHORITY_DOMINANCE_MARGIN):
        return (
            STRATEGY_AUTHORITY,
            f"dominant source: {stats['top_book_title']!r} "
            f"(authority={top_a:.2f}, margin={top_a - second_a:.2f})",
        )

    # Rule 3: book-diversity consensus
    if stats["distinct_books"] >= CONSENSUS_MIN_BOOKS:
        return (
            STRATEGY_CONSENSUS,
            f"{stats['distinct_books']} distinct books contribute",
        )

    # Rule 4: default
    return STRATEGY_CONSENSUS, "default (no clear signal for other strategies)"


# ---------------------------------------------------------------------------
# Step 3: cross-references between Skills
# ---------------------------------------------------------------------------


def find_cross_references(
    skills: list[ProposedSkill],
) -> dict[int, list[int]]:
    """For each Skill, find sibling Skills it should reference.

    A Skill A references Skill B when B's anchor_concept appears in
    A's concept_ids (i.e., A's neighborhood touches B's central
    topic). This means A's content can hyperlink to B rather than
    duplicating coverage.

    Returns ``{cluster_id: [referenced_cluster_ids]}``. Each Skill
    points to siblings whose anchors appear in its own concept set.
    """
    refs: dict[int, list[int]] = defaultdict(list)
    by_anchor: dict[int, int] = {}
    for sk in skills:
        if sk.anchor_concept_id is not None:
            by_anchor[sk.anchor_concept_id] = sk.cluster_id

    for sk in skills:
        cluster_id = sk.cluster_id
        for cid in sk.concept_ids:
            other = by_anchor.get(int(cid))
            if other is not None and other != cluster_id:
                if other not in refs[cluster_id]:
                    refs[cluster_id].append(other)
    return dict(refs)


# ---------------------------------------------------------------------------
# Step 4: orchestrator
# ---------------------------------------------------------------------------


def _default_package_name(domain: str) -> str:
    """Slug from the user's domain query, used as the package's name."""
    slug = _slugify(domain, max_len=60)
    return slug or "package"


def plan_package(
    decomposition: DecompositionResult,
    conn: duckdb.DuckDBPyConnection,
    *,
    package_name: Optional[str] = None,
    folder_root_template: str = "data/generated-packages/{name}",
) -> PackagePlan:
    """Plan a Skill package from a decomposition.

    Steps (mirror the module docstring):
      1. Build REQUIRES edges between Skill clusters → topo order.
      2. Per-Skill: pick selection strategy + rationale.
      3. Per-Skill: list sibling Skills it should cross-reference.
      4. Per-Skill: slugify a folder name from suggested_name or anchor.
      5. Wrap into a PackagePlan ready for Phase 5.4 materialization.
    """
    skills = decomposition.proposed_skills
    name = package_name or _default_package_name(decomposition.query)
    folder_root = folder_root_template.format(name=name)
    plan = PackagePlan(
        package_name=name, domain=decomposition.query, folder_root=folder_root,
    )
    if not skills:
        plan.notes = "Empty decomposition — no Skills to plan."
        return plan

    # Step 1: dependency order
    edges = build_dependency_edges(conn, skills)
    order = topo_order(skills, edges)
    skill_by_cid: dict[int, ProposedSkill] = {sk.cluster_id: sk for sk in skills}

    # Reverse-lookup for "what does cluster X require"
    requires_of: dict[int, list[int]] = defaultdict(list)
    for a, b in edges:
        if a in skill_by_cid and b in skill_by_cid:
            requires_of[a].append(b)

    # Step 3: cross-references
    refs = find_cross_references(skills)

    # Steps 2 + 4: per-Skill strategy + folder
    used_slugs: set[str] = set()
    planned: list[PlannedSkill] = []
    for idx, cluster_id in enumerate(order):
        sk = skill_by_cid.get(cluster_id)
        if sk is None:
            continue
        strategy, rationale = select_strategy(conn, sk)
        # Slug from the suggested name (which is the anchor name post-Phase
        # 5.1; will be the LLM-named version after that pass runs).
        base = sk.suggested_name or sk.anchor_concept_name or f"skill-{cluster_id}"
        slug = _slugify(base)
        # Avoid filename collisions — append cluster_id if needed.
        if slug in used_slugs:
            slug = f"{slug}-{cluster_id}"
        used_slugs.add(slug)
        planned.append(PlannedSkill(
            proposed=sk,
            order=idx,
            strategy=strategy,
            strategy_rationale=rationale,
            folder_name=slug,
            requires_cluster_ids=requires_of.get(cluster_id, []),
            references_cluster_ids=refs.get(cluster_id, []),
        ))
    plan.planned_skills = planned
    return plan
