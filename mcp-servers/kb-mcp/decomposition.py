"""decomposition.py — Phase 5.1 Skills Factory: domain decomposition.

Given a domain string like "CDC with Databricks", produce a structured
proposal of Skills (concept clusters) that together cover the domain.
Output is then fed to Phase 5.2 (package planning) and Phase 5.3
(per-Skill generation).

Pipeline (per architecture §8 / execution plan prompt 5.1):

  1. ``find_anchor_concepts(query)`` — resolve the query against the
     concept graph. Whole query first, then each significant token.
     The resolved set is the BFS frontier.

  2. ``expand_neighborhood(anchor_ids, max_depth=3)`` — BFS over
     ``concept_relation``. Both directions (from↔to). Caps the
     neighborhood to ``max_neighborhood_size`` to keep clustering
     tractable on large graphs (85K concepts, 126K edges).

  3. ``cluster_concepts(subgraph)`` — Louvain community detection
     via networkx. The subgraph is built from concept_relation edges
     among the neighborhood, weighted by edge frequency.

  4. ``cross_reference_chapters(cluster, conn, k)`` — for each
     cluster, find the top-k chapters most strongly associated with
     its concepts (concept_relation count + book authority).

  5. ``decompose_domain(...)`` — orchestrator returning a
     ``DecompositionResult`` with one ``ProposedSkill`` per cluster.
     Each ``ProposedSkill`` is ready to be named/scoped by an LLM
     sub-agent in the next phase.

Cost model: this module does NO LLM calls. It runs locally over the
DuckDB catalog using SQL + networkx. The sub-agent name/scope pass
is a separate concern (Phase 5.1 step 5 — LLM refine).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import duckdb

LOG = logging.getLogger("mypub-decomposition")


# Tunables. These are starting points; Phase 5.5 eval should
# re-validate with real Skills-Factory output quality.
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_NEIGHBORHOOD = 200       # concept count cap before clustering
DEFAULT_TOP_CHAPTERS_PER_SKILL = 5
DEFAULT_MIN_CLUSTER_SIZE = 3         # below this, fold into nearest cluster
DEFAULT_LOUVAIN_RESOLUTION = 1.0     # higher → smaller communities


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProposedSkill:
    """One cluster of concepts proposed as a Skill candidate.

    Anchor is the most central concept in the cluster (highest internal
    degree). The suggested_name is a placeholder; an LLM sub-agent
    pass refines it into a human-readable Skill name in Phase 5.1
    step 5.
    """

    cluster_id: int                       # 0-indexed within decomposition
    concept_ids: list[int] = field(default_factory=list)
    anchor_concept_id: Optional[int] = None
    anchor_concept_name: Optional[str] = None
    suggested_name: Optional[str] = None  # placeholder until LLM names it
    top_chapters: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DecompositionResult:
    """Top-level output of decompose_domain().

    Carries the original query, the anchor concepts that the BFS
    started from, the neighborhood after expansion, and the proposed
    Skill clusters. ``anchor_concept_ids_unresolved`` lists query
    tokens that didn't resolve to any concept — useful for surfacing
    "you may want to add coverage for X" hints.
    """

    query: str
    anchor_concept_ids: list[int] = field(default_factory=list)
    anchor_terms_unresolved: list[str] = field(default_factory=list)
    neighborhood_size: int = 0
    proposed_skills: list[ProposedSkill] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Step 1: anchor concept resolution
# ---------------------------------------------------------------------------


def find_anchor_concepts(
    resolver: Any, query: str,
) -> tuple[list[int], list[str]]:
    """Resolve the query against the concept graph using lookup-only.

    Tries the whole phrase first (for compound concepts like 'Change
    Data Capture'), then each whitespace-delimited token. Returns
    ``(resolved_ids, unresolved_tokens)`` so callers can report
    coverage gaps.

    Lookup-only by design: we don't want decomposition to *create*
    concepts; we want it to map the query into the existing concept
    graph. If a token doesn't resolve, that's a discovery opportunity
    handled elsewhere.
    """
    if not query or not query.strip():
        return [], []

    whole = query.strip()
    resolved: list[int] = []
    seen: set[int] = set()

    # Try the whole query first. If it resolves, we don't bother with
    # per-token resolution — the compound concept is what the user
    # asked about, and partial-token misses would be noise.
    cid = resolver.resolve_lookup_only(whole)
    if cid is not None:
        resolved.append(cid)
        return resolved, []

    # Whole query didn't resolve — fall back to per-token resolution.
    unresolved: list[str] = []
    for tok in (t for t in query.split() if t.strip()):
        cid = resolver.resolve_lookup_only(tok)
        if cid is not None and cid not in seen:
            seen.add(cid)
            resolved.append(cid)
        elif cid is None:
            unresolved.append(tok)

    return resolved, unresolved


# ---------------------------------------------------------------------------
# Step 2: neighborhood expansion (BFS over concept_relation)
# ---------------------------------------------------------------------------


def expand_neighborhood(
    conn: duckdb.DuckDBPyConnection,
    anchor_ids: Sequence[int],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_size: int = DEFAULT_MAX_NEIGHBORHOOD,
) -> dict[int, int]:
    """BFS expansion over ``concept_relation`` from anchor_ids.

    Returns a mapping ``concept_id → depth-from-nearest-anchor``.
    Anchors are at depth 0. Expansion stops when ``max_depth`` is
    reached OR the neighborhood reaches ``max_size``.

    Both directions of each edge are followed (from→to and to→from)
    since concept relations are conceptual, not strictly directional
    for traversal purposes.
    """
    if not anchor_ids:
        return {}

    depth_of: dict[int, int] = {int(a): 0 for a in anchor_ids}
    frontier: deque[int] = deque(depth_of.keys())

    while frontier and len(depth_of) < max_size:
        current = frontier.popleft()
        d = depth_of[current]
        if d >= max_depth:
            continue
        # Pull neighbors in one query (both directions).
        neighbors = conn.execute(
            """
            SELECT DISTINCT to_concept_id   FROM concept_relation WHERE from_concept_id = ?
            UNION
            SELECT DISTINCT from_concept_id FROM concept_relation WHERE to_concept_id   = ?
            """,
            [current, current],
        ).fetchall()
        for (nid,) in neighbors:
            if nid is None:
                continue
            nid = int(nid)
            if nid in depth_of:
                continue
            depth_of[nid] = d + 1
            frontier.append(nid)
            if len(depth_of) >= max_size:
                break

    return depth_of


# ---------------------------------------------------------------------------
# Step 3: clustering via Louvain community detection
# ---------------------------------------------------------------------------


def cluster_concepts(
    conn: duckdb.DuckDBPyConnection,
    concept_ids: Sequence[int],
    *,
    resolution: float = DEFAULT_LOUVAIN_RESOLUTION,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> list[set[int]]:
    """Run Louvain community detection over the induced subgraph.

    Builds a weighted networkx graph from concept_relation edges where
    both endpoints are in ``concept_ids``. Edge weight is the count of
    relations between the two concepts (sums across REQUIRES, EXTENDS,
    etc.).

    Clusters smaller than ``min_cluster_size`` are folded into the
    cluster with which they share the most edges (or kept as a
    standalone if no overlap). Empty input → empty output.
    """
    # pylint: disable=import-outside-toplevel
    import networkx as nx
    from networkx.algorithms.community import louvain_communities

    if not concept_ids:
        return []
    if len(concept_ids) <= min_cluster_size:
        return [set(int(c) for c in concept_ids)]

    cids = [int(c) for c in concept_ids]
    placeholders = ",".join(["?"] * len(cids))
    edges = conn.execute(
        f"""
        SELECT from_concept_id, to_concept_id, COUNT(*) AS w
          FROM concept_relation
         WHERE from_concept_id IN ({placeholders})
           AND to_concept_id   IN ({placeholders})
         GROUP BY from_concept_id, to_concept_id
        """,
        [*cids, *cids],
    ).fetchall()

    g = nx.Graph()
    g.add_nodes_from(cids)
    for f, t, w in edges:
        if f is None or t is None or f == t:
            continue
        # Symmetrize: networkx Graph stores undirected; if both
        # directions appear, sum the weights.
        if g.has_edge(f, t):
            g[f][t]["weight"] += w
        else:
            g.add_edge(f, t, weight=w)

    communities = louvain_communities(g, weight="weight",
                                       resolution=resolution, seed=42)
    communities = [{int(c) for c in com} for com in communities]

    # Fold small clusters into their best-connected neighbor.
    if min_cluster_size > 1:
        communities = _fold_small_clusters(g, communities, min_cluster_size)

    # Sort by size descending — largest clusters are usually the most
    # cohesive Skill candidates.
    communities.sort(key=len, reverse=True)
    return communities


def _fold_small_clusters(
    g: Any, communities: list[set[int]], min_size: int,
) -> list[set[int]]:
    """Merge clusters smaller than ``min_size`` into their best-connected sibling."""
    if not communities:
        return []
    keep: list[set[int]] = []
    smalls: list[set[int]] = []
    for com in communities:
        (keep if len(com) >= min_size else smalls).append(com)

    if not keep:
        # All clusters too small — return the union as one.
        merged: set[int] = set()
        for s in smalls:
            merged.update(s)
        return [merged] if merged else []

    for small in smalls:
        # Score each kept cluster by edge weight summing nodes-in-small to nodes-in-keep.
        best_idx, best_score = 0, -1.0
        for i, target in enumerate(keep):
            score = 0.0
            for u in small:
                for v in target:
                    if g.has_edge(u, v):
                        score += g[u][v].get("weight", 1.0)
            if score > best_score:
                best_score, best_idx = score, i
        keep[best_idx] = keep[best_idx] | small
    return keep


# ---------------------------------------------------------------------------
# Step 4: cross-reference clusters with chapters
# ---------------------------------------------------------------------------


def top_chapters_for_cluster(
    conn: duckdb.DuckDBPyConnection,
    concept_ids: Sequence[int],
    *,
    k: int = DEFAULT_TOP_CHAPTERS_PER_SKILL,
) -> list[dict[str, Any]]:
    """Return up to ``k`` chapters most strongly associated with the cluster.

    Ranks by distinct-concept hit count then total relation count.
    This is the same heuristic used by the graph modality in
    server.py, scoped to a specific cluster instead of a query.
    """
    if not concept_ids:
        return []
    cids = [int(c) for c in concept_ids]
    placeholders = ",".join(["?"] * len(cids))
    rows = conn.execute(
        f"""
        SELECT cr.source_id AS chapter_id,
               COUNT(DISTINCT
                   CASE WHEN cr.from_concept_id IN ({placeholders})
                        THEN cr.from_concept_id
                        WHEN cr.to_concept_id   IN ({placeholders})
                        THEN cr.to_concept_id
                   END
               ) AS concept_hits,
               COUNT(*) AS mention_count,
               b.title AS book_title,
               c.title AS chapter_title
          FROM concept_relation cr
          JOIN chapter c ON c.chapter_id = cr.source_id
          JOIN book    b ON b.book_id = c.book_id
         WHERE cr.source_type = 'chapter'
           AND (cr.from_concept_id IN ({placeholders})
                OR cr.to_concept_id IN ({placeholders}))
         GROUP BY cr.source_id, b.title, c.title
         ORDER BY concept_hits DESC, mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, k],
    ).fetchall()
    return [
        {
            "chapter_id": int(r[0]),
            "concept_hits": int(r[1]),
            "mention_count": int(r[2]),
            "book_title": r[3],
            "chapter_title": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helpers for picking a cluster's anchor + suggested name
# ---------------------------------------------------------------------------


def _pick_cluster_anchor(
    conn: duckdb.DuckDBPyConnection, cluster: Sequence[int],
) -> tuple[Optional[int], Optional[str]]:
    """Pick the most-central concept in the cluster as its anchor.

    Centrality = number of concept_relation edges to other concepts in
    the same cluster. Tie-breaks by total mention count across the
    chapter corpus, then by lexical concept name (deterministic).
    """
    if not cluster:
        return None, None
    cids = [int(c) for c in cluster]
    placeholders = ",".join(["?"] * len(cids))
    rows = conn.execute(
        f"""
        WITH internal_edges AS (
          SELECT from_concept_id AS c, COUNT(*) AS d
            FROM concept_relation
           WHERE from_concept_id IN ({placeholders})
             AND to_concept_id   IN ({placeholders})
           GROUP BY from_concept_id
          UNION ALL
          SELECT to_concept_id AS c, COUNT(*) AS d
            FROM concept_relation
           WHERE from_concept_id IN ({placeholders})
             AND to_concept_id   IN ({placeholders})
           GROUP BY to_concept_id
        ),
        degree AS (
          SELECT c, SUM(d) AS deg FROM internal_edges GROUP BY c
        ),
        mentions AS (
          SELECT cr.from_concept_id AS c, COUNT(*) AS m
            FROM concept_relation cr
           WHERE cr.from_concept_id IN ({placeholders})
           GROUP BY cr.from_concept_id
          UNION ALL
          SELECT cr.to_concept_id AS c, COUNT(*) AS m
            FROM concept_relation cr
           WHERE cr.to_concept_id IN ({placeholders})
           GROUP BY cr.to_concept_id
        ),
        total_mentions AS (
          SELECT c, SUM(m) AS total_m FROM mentions GROUP BY c
        )
        SELECT cn.concept_id, cn.name,
               COALESCE(d.deg, 0) AS deg,
               COALESCE(tm.total_m, 0) AS total_m
          FROM concept cn
          LEFT JOIN degree d ON cn.concept_id = d.c
          LEFT JOIN total_mentions tm ON cn.concept_id = tm.c
         WHERE cn.concept_id IN ({placeholders})
         ORDER BY deg DESC, total_m DESC, cn.name
         LIMIT 1
        """,
        [*cids, *cids, *cids, *cids, *cids, *cids, *cids],
    ).fetchall()
    if not rows:
        return None, None
    return int(rows[0][0]), rows[0][1]


# ---------------------------------------------------------------------------
# Step 5: orchestrator
# ---------------------------------------------------------------------------


def decompose_domain(
    conn: duckdb.DuckDBPyConnection,
    resolver: Any,
    query: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_neighborhood: int = DEFAULT_MAX_NEIGHBORHOOD,
    top_chapters_per_skill: int = DEFAULT_TOP_CHAPTERS_PER_SKILL,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    louvain_resolution: float = DEFAULT_LOUVAIN_RESOLUTION,
) -> DecompositionResult:
    """Decompose ``query`` into proposed Skill clusters.

    See module docstring for the pipeline. Returns a DecompositionResult
    with one ProposedSkill per detected cluster, ranked by size.
    """
    anchor_ids, unresolved = find_anchor_concepts(resolver, query)
    result = DecompositionResult(
        query=query,
        anchor_concept_ids=anchor_ids,
        anchor_terms_unresolved=unresolved,
    )
    if not anchor_ids:
        result.notes = (
            "No anchor concepts resolved from query. Either the topic is "
            "novel (try /kb-discover) or the query terms don't match "
            "existing concept names."
        )
        return result

    neighborhood = expand_neighborhood(
        conn, anchor_ids,
        max_depth=max_depth, max_size=max_neighborhood,
    )
    result.neighborhood_size = len(neighborhood)

    if not neighborhood:
        result.notes = "Anchor concepts have no neighbors in concept_relation."
        return result

    clusters = cluster_concepts(
        conn, list(neighborhood.keys()),
        resolution=louvain_resolution,
        min_cluster_size=min_cluster_size,
    )

    proposed: list[ProposedSkill] = []
    for i, cluster in enumerate(clusters):
        if not cluster:
            continue
        anchor_id, anchor_name = _pick_cluster_anchor(conn, list(cluster))
        top_chapters = top_chapters_for_cluster(
            conn, list(cluster), k=top_chapters_per_skill,
        )
        proposed.append(ProposedSkill(
            cluster_id=i,
            concept_ids=sorted(cluster),
            anchor_concept_id=anchor_id,
            anchor_concept_name=anchor_name,
            suggested_name=anchor_name,  # placeholder; LLM-named in step 5
            top_chapters=top_chapters,
        ))
    result.proposed_skills = proposed
    return result
