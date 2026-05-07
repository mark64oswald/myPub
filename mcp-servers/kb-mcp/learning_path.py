"""learning_path.py — Phase 8 Learning Path generator (deterministic v1).

Produces a sequenced curriculum from a target concept (e.g. "CDC
pipeline design") backward through prerequisites (REQUIRES + EXTENDS
edges), grouping concepts into ordered stages and recommending the
strongest book chapters per stage. Deliberately deterministic: no
sub-agent prose, no LLM calls. The framework supports adding
sub-agent prose layers later (per-stage "why this chapter" exposition,
checkpoint questions); v1 focuses on the path itself.

Pipeline:

  1. Resolve target concept (user's goal)
  2. Optionally resolve start concept (already-known) — used to clip
     the path
  3. BFS backward from target over (REQUIRES, EXTENDS), depth-limited
  4. Group concepts into stages by depth (deepest = first stage)
  5. Per-stage: pick top-K book chapters by concept-mention count
  6. Gap analysis: concepts with no book coverage are flagged
  7. Render: _path.md (overview) + stage-N-<name>/reading-list.md per stage

Output layout (matches §2.1 of the architecture spec):

    learning-paths/<path-name>/
      _path.md
      stage-1-<slug>/
        reading-list.md
      stage-2-<slug>/
        reading-list.md
      …

The "stage name" for v1 is derived heuristically from the highest-
authority concept in the stage. A future LLM-refined version can
replace the heuristic with sub-agent naming.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
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

LOG = logging.getLogger("mypub-learning-path")


GENERATOR_TYPE = "learning_path"
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_CONCEPTS = 30   # cap on total path length; densely-connected
                            # targets in a 100K-concept graph hit thousands
                            # of prereqs at depth 4 — most are noise. Pruning
                            # by chapter coverage keeps the learn-able subset.
DEFAULT_STAGE_SIZE = 5      # target concepts per stage; min 3, max 7
DEFAULT_MAX_CHAPTERS_PER_STAGE = 4
DEFAULT_PREREQ_RELATIONS = ("REQUIRES", "EXTENDS")


# ---------------------------------------------------------------------------
# Data shape from the decomposer
# ---------------------------------------------------------------------------


@dataclass
class _PathConcept:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    depth: int                       # 0 = target, 1 = direct prereq, …
    chapter_count: int
    doc_section_count: int


@dataclass
class _Decomposition:
    target_concept_id: int
    target_name: str
    start_concept_id: Optional[int]  # may be None if user only gave target
    start_name: Optional[str]
    max_depth: int
    relation_filter: tuple[str, ...]
    concepts: list[_PathConcept]     # ordered: deepest (foundations) first
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class PrerequisiteDecomposer:
    """Backward BFS from target_concept over REQUIRES + EXTENDS edges.

    Optionally clipped by ``start_concept`` — when supplied, only
    concepts on a path from start to target are kept. Without start,
    the full prerequisite tree to ``max_depth`` is returned.

    A "REQUIRES B" edge from concept A means A depends on B; to learn
    A, you need B first. So the prerequisite traversal walks from
    target to its REQUIRES, to their REQUIRES, etc. — the ``to_concept``
    of each edge.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        start: Optional[str] = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_concepts: int = DEFAULT_MAX_CONCEPTS,
        relation_filter: Optional[tuple[str, ...]] = None,
        **_: Any,
    ) -> _Decomposition:
        rels = tuple(relation_filter) if relation_filter else DEFAULT_PREREQ_RELATIONS

        target_id = resolver.resolve_lookup_only(query)
        if target_id is None:
            return _Decomposition(
                target_concept_id=-1, target_name=query,
                start_concept_id=None, start_name=None,
                max_depth=max_depth, relation_filter=rels, concepts=[],
                notes=[f"target concept {query!r} not found"],
            )
        target_name = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [target_id],
        ).fetchone()[0]

        start_id: Optional[int] = None
        start_name: Optional[str] = None
        if start:
            start_id = resolver.resolve_lookup_only(start)
            if start_id is not None:
                start_name = conn.execute(
                    "SELECT name FROM concept WHERE concept_id = ?", [start_id],
                ).fetchone()[0]
            else:
                # Don't fail; just note and proceed without clipping.
                pass

        # BFS over the prerequisite subgraph.
        depth_map: dict[int, int] = {target_id: 0}
        frontier = deque([target_id])
        while frontier:
            cid = frontier.popleft()
            d = depth_map[cid]
            if d >= max_depth:
                continue
            ph = ",".join(["?"] * len(rels))
            rows = conn.execute(
                f"""
                SELECT DISTINCT to_concept_id
                  FROM concept_relation
                 WHERE from_concept_id = ?
                   AND relation_type IN ({ph})
                """,
                [cid, *rels],
            ).fetchall()
            for (nb,) in rows:
                nb = int(nb)
                if nb in depth_map:
                    continue
                depth_map[nb] = d + 1
                frontier.append(nb)

        # If start was supplied & resolved, clip to concepts that lead
        # toward target. Heuristic: keep concepts at or above start's
        # depth. (The full reachability filter would be expensive; this
        # captures the user's intent that "I already know start, don't
        # show me material before that point in the chain".)
        if start_id is not None and start_id in depth_map:
            cutoff = depth_map[start_id]
            depth_map = {cid: d for cid, d in depth_map.items() if d <= cutoff}

        notes: list[str] = []
        if start and start_id is None:
            notes.append(
                f"start concept {start!r} not found — path returned without clipping"
            )
        elif start_id is not None and start_id not in depth_map:
            notes.append(
                f"start concept {start!r} not on a prerequisite path to target — "
                f"path returned without clipping"
            )

        concepts = self._enrich_concepts(conn, depth_map)

        # Cap by max_concepts: prune by chapter coverage (lowest first)
        # while keeping the target itself. Densely-connected targets
        # produce 100s of prereqs at depth 4; most have no chapter
        # coverage and add noise to the path.
        pruned_count = 0
        if len(concepts) > max_concepts:
            target_idx = next(
                (i for i, c in enumerate(concepts)
                 if c.concept_id == target_id), None,
            )
            target_concept = concepts.pop(target_idx) if target_idx is not None else None
            # Rank by (chapter_count desc, depth desc, name)
            concepts.sort(key=lambda c: (-c.chapter_count, -c.depth, c.name))
            kept = concepts[: max(0, max_concepts - 1)]
            pruned_count = len(concepts) - len(kept)
            concepts = kept
            if target_concept is not None:
                concepts.append(target_concept)
            notes.append(
                f"pruned {pruned_count} prerequisites with low book-chapter "
                f"coverage; max_concepts={max_concepts}"
            )

        # Order: deepest first (foundations) so stage 1 is foundational.
        concepts.sort(key=lambda c: (-c.depth, -c.chapter_count, c.name))

        return _Decomposition(
            target_concept_id=target_id, target_name=target_name,
            start_concept_id=start_id, start_name=start_name,
            max_depth=max_depth, relation_filter=rels,
            concepts=concepts, notes=notes,
        )

    @staticmethod
    def _enrich_concepts(
        conn: duckdb.DuckDBPyConnection,
        depth_map: dict[int, int],
    ) -> list[_PathConcept]:
        if not depth_map:
            return []
        ids = list(depth_map.keys())
        ph = ",".join(["?"] * len(ids))
        # Chapter / doc_section counts join through to the actual rows
        # so orphan source_ids in concept_relation can't inflate counts.
        # (The concept_relation polymorphic FK isn't enforced; defensive
        # joins are correct.)
        rows = conn.execute(
            f"""
            WITH chap AS (
              SELECT concept_id, COUNT(DISTINCT chapter_id) AS n
                FROM (
                  SELECT cr.from_concept_id AS concept_id, ch.chapter_id
                    FROM concept_relation cr
                    JOIN chapter ch ON ch.chapter_id = cr.source_id
                   WHERE cr.source_type = 'chapter'
                     AND cr.from_concept_id IN ({ph})
                  UNION
                  SELECT cr.to_concept_id, ch.chapter_id
                    FROM concept_relation cr
                    JOIN chapter ch ON ch.chapter_id = cr.source_id
                   WHERE cr.source_type = 'chapter'
                     AND cr.to_concept_id IN ({ph})
                )
                GROUP BY concept_id
            ),
            sec AS (
              SELECT concept_id, COUNT(DISTINCT doc_section_id) AS n
                FROM (
                  SELECT cr.from_concept_id AS concept_id, ds.doc_section_id
                    FROM concept_relation cr
                    JOIN doc_section ds ON ds.doc_section_id = cr.source_id
                   WHERE cr.source_type = 'doc_section'
                     AND cr.from_concept_id IN ({ph})
                  UNION
                  SELECT cr.to_concept_id, ds.doc_section_id
                    FROM concept_relation cr
                    JOIN doc_section ds ON ds.doc_section_id = cr.source_id
                   WHERE cr.source_type = 'doc_section'
                     AND cr.to_concept_id IN ({ph})
                )
                GROUP BY concept_id
            )
            SELECT c.concept_id, c.name, c.concept_type, c.description,
                   COALESCE(chap.n, 0), COALESCE(sec.n, 0)
              FROM concept c
              LEFT JOIN chap ON chap.concept_id = c.concept_id
              LEFT JOIN sec  ON sec.concept_id  = c.concept_id
             WHERE c.concept_id IN ({ph})
            """,
            [*ids, *ids, *ids, *ids, *ids],
        ).fetchall()
        out: list[_PathConcept] = []
        for r in rows:
            cid = int(r[0])
            out.append(_PathConcept(
                concept_id=cid, name=r[1], concept_type=r[2],
                description=r[3], depth=depth_map[cid],
                chapter_count=int(r[4]), doc_section_count=int(r[5]),
            ))
        return out


# ---------------------------------------------------------------------------
# Stage grouping (deterministic heuristic)
# ---------------------------------------------------------------------------


@dataclass
class _Stage:
    ordinal: int                     # 1-based
    name: str                        # heuristic from anchor concept
    slug: str                        # filename-safe form
    concepts: list[_PathConcept]     # ordered within the stage
    chapters: list["_StageChapter"] = field(default_factory=list)


@dataclass
class _StageChapter:
    chapter_id: int
    chapter_title: str
    book_id: int
    book_title: str
    concept_hits: int                # how many of this stage's concepts the chapter mentions
    mention_count: int               # total concept_relation rows in this chapter for stage concepts


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "stage"


def group_into_stages(
    concepts: list[_PathConcept],
    *,
    target_size: int = DEFAULT_STAGE_SIZE,
) -> list[_Stage]:
    """Group prerequisite concepts into ordered learning stages.

    Strategy:

    * Bucket by ``depth`` (each depth level becomes a candidate stage).
    * Merge tiny adjacent buckets so every stage has ≥ ``min(3, total/2)``
      concepts where possible.
    * Split a single huge bucket into chunks of ``target_size``.
    * Order: deepest depth = stage 1 (foundations); shallowest = final stage.

    Pure deterministic — no LLM. Stage names are derived from each
    stage's "anchor" (highest chapter_count concept).
    """
    if not concepts:
        return []

    by_depth: dict[int, list[_PathConcept]] = defaultdict(list)
    for c in concepts:
        by_depth[c.depth].append(c)
    # Within each depth, sort by chapter_count desc (best-covered first
    # so the anchor lands at index 0).
    for d in by_depth:
        by_depth[d].sort(key=lambda c: (-c.chapter_count, c.name))

    # Iterate depths deepest → shallowest.
    raw_stages: list[list[_PathConcept]] = []
    for d in sorted(by_depth.keys(), reverse=True):
        bucket = by_depth[d]
        # Split oversized buckets.
        if len(bucket) > 7:
            for i in range(0, len(bucket), target_size):
                raw_stages.append(bucket[i:i + target_size])
        else:
            raw_stages.append(bucket)

    # Merge tiny adjacent stages (size < 3) into the next stage.
    merged: list[list[_PathConcept]] = []
    pending: list[_PathConcept] = []
    for stage in raw_stages:
        combined = pending + stage
        if len(combined) < 3 and stage is not raw_stages[-1]:
            pending = combined
            continue
        merged.append(combined)
        pending = []
    if pending:
        # Fold trailing tiny pending into the previous stage.
        if merged:
            merged[-1].extend(pending)
        else:
            merged.append(pending)

    # Build stage objects with names + slugs.
    out: list[_Stage] = []
    for i, items in enumerate(merged, start=1):
        anchor = items[0]
        name = anchor.name
        # Disambiguate slug if multiple stages share an anchor (rare).
        slug = _slugify(name)
        out.append(_Stage(ordinal=i, name=name, slug=slug, concepts=items))
    # Disambiguate duplicate slugs.
    seen: dict[str, int] = {}
    for s in out:
        if s.slug in seen:
            seen[s.slug] += 1
            s.slug = f"{s.slug}-{seen[s.slug]}"
        else:
            seen[s.slug] = 0
    return out


# ---------------------------------------------------------------------------
# Reading list assembly (per-stage chapter ranking)
# ---------------------------------------------------------------------------


def assign_chapters_to_stage(
    conn: duckdb.DuckDBPyConnection,
    stage: _Stage,
    *,
    max_chapters: int = DEFAULT_MAX_CHAPTERS_PER_STAGE,
) -> list[_StageChapter]:
    """Pick the strongest chapters for a stage's concepts.

    A chapter is strong if it mentions many of the stage's concepts
    (high ``concept_hits``) and mentions them often (high
    ``mention_count``). Tie-break: prefer chapters from books with
    more total relations (a proxy for authority).

    Runs entirely against ``concept_relation`` — no LLM reasoning,
    no chapter-content reading. The reading-list rationale is
    derived from concept overlap.
    """
    cids = [c.concept_id for c in stage.concepts]
    if not cids:
        return []
    ph = ",".join(["?"] * len(cids))
    rows = conn.execute(
        f"""
        SELECT cr.source_id AS chapter_id,
               COUNT(DISTINCT
                   CASE WHEN cr.from_concept_id IN ({ph})
                        THEN cr.from_concept_id
                        WHEN cr.to_concept_id   IN ({ph})
                        THEN cr.to_concept_id
                   END
               ) AS concept_hits,
               COUNT(*) AS mention_count,
               c.title AS chapter_title,
               c.book_id, b.title AS book_title
          FROM concept_relation cr
          JOIN chapter c ON c.chapter_id = cr.source_id
          JOIN book    b ON b.book_id = c.book_id
         WHERE cr.source_type = 'chapter'
           AND (cr.from_concept_id IN ({ph}) OR cr.to_concept_id IN ({ph}))
         GROUP BY cr.source_id, c.title, c.book_id, b.title
        HAVING concept_hits >= 1
         ORDER BY concept_hits DESC, mention_count DESC
         LIMIT ?
        """,
        [*cids, *cids, *cids, *cids, max_chapters],
    ).fetchall()
    return [
        _StageChapter(
            chapter_id=int(r[0]), chapter_title=r[3] or "(untitled chapter)",
            book_id=int(r[4]), book_title=r[5] or "(unknown book)",
            concept_hits=int(r[1]), mention_count=int(r[2]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_stage_reading_list(stage: _Stage) -> str:
    lines = [
        f"# Stage {stage.ordinal} — {stage.name}",
        "",
        "## Concepts in this stage",
        "",
    ]
    for c in stage.concepts:
        kind = f" ({c.concept_type})" if c.concept_type else ""
        lines.append(f"- **{c.name}**{kind}")
        if c.description:
            desc = c.description.strip().splitlines()[0][:200]
            if desc:
                lines.append(f"  - {desc}")
    lines.append("")
    if stage.chapters:
        lines.append("## Recommended chapters")
        lines.append("")
        for ch in stage.chapters:
            lines.append(
                f"- **{ch.book_title}** — _{ch.chapter_title}_ "
                f"(covers {ch.concept_hits}/{len(stage.concepts)} stage concepts; "
                f"{ch.mention_count} mention(s))"
            )
        lines.append("")
    else:
        lines.append("## Recommended chapters")
        lines.append("")
        lines.append("_No book chapters in the corpus cover this stage's concepts. "
                     "Consider acquiring a book on this topic, or check "
                     "doc_section coverage via `/kb-discover`._")
        lines.append("")

    # Gap report inside the stage doc — concepts with zero chapter coverage.
    gaps = [c for c in stage.concepts if c.chapter_count == 0]
    if gaps:
        lines.append("## Coverage gaps in this stage")
        lines.append("")
        for c in gaps:
            doc_note = (f" (covered in {c.doc_section_count} doc section(s))"
                        if c.doc_section_count > 0 else "")
            lines.append(f"- **{c.name}** — no book chapter coverage{doc_note}")
        lines.append("")

    return "\n".join(lines)


def _render_path_overview(decomp: _Decomposition, stages: list[_Stage]) -> str:
    lines = [
        f"# Learning path: {decomp.target_name}",
        "",
        f"**Target concept:** {decomp.target_name} (concept_id={decomp.target_concept_id})",
    ]
    if decomp.start_name:
        lines.append(f"**Starting from:** {decomp.start_name}")
    lines.extend([
        f"**Max depth:** {decomp.max_depth} prerequisite hops",
        f"**Relations followed:** {', '.join(decomp.relation_filter)}",
        "",
        f"**Stages:** {len(stages)}",
        f"**Total concepts:** {sum(len(s.concepts) for s in stages)}",
        "",
        "## Path",
        "",
    ])
    for s in stages:
        anchor_count = len(s.concepts)
        chap_count = len(s.chapters)
        lines.append(
            f"{s.ordinal}. **{s.name}** — {anchor_count} concept(s), "
            f"{chap_count} chapter(s) → `stage-{s.ordinal}-{s.slug}/`"
        )
    lines.append("")
    # Whole-path gaps.
    all_gap_concepts = [c for s in stages for c in s.concepts if c.chapter_count == 0]
    if all_gap_concepts:
        lines.append("## Coverage gaps")
        lines.append("")
        lines.append(
            f"{len(all_gap_concepts)} concept(s) on this path have no book "
            f"chapter coverage in the corpus. The per-stage `reading-list.md` "
            f"files flag them inline; consider supplementing with current docs."
        )
        lines.append("")
    if decomp.notes:
        lines.append("## Notes")
        lines.append("")
        for n in decomp.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class LearningPathPlanner:
    """Renders the prerequisite decomposition into a fully-loaded GenPlan.

    One ``GenUnit`` per stage. Files: package-level ``_path.md`` and
    one ``stage-N-<slug>/reading-list.md`` per stage.
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        target_size: int = DEFAULT_STAGE_SIZE,
        max_chapters: int = DEFAULT_MAX_CHAPTERS_PER_STAGE,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.target_name)
        stages = group_into_stages(d.concepts, target_size=target_size)
        for s in stages:
            s.chapters = assign_chapters_to_stage(
                conn, s, max_chapters=max_chapters,
            )

        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.target_name,
            source_query=d.target_name,
            package_metadata={
                "target_concept_id": d.target_concept_id,
                "start_concept_id": d.start_concept_id,
                "max_depth": d.max_depth,
                "relation_filter": list(d.relation_filter),
                "n_stages": len(stages),
                "n_concepts": sum(len(s.concepts) for s in stages),
            },
            notes=list(d.notes),
        )

        for s in stages:
            unit = GenUnit(
                unit_type="learning_stage",
                name=f"Stage {s.ordinal}: {s.name}",
                ordinal=s.ordinal,
                content_markdown=_render_stage_reading_list(s),
                metadata={
                    "stage_ordinal": s.ordinal,
                    "stage_slug": s.slug,
                    "anchor_concept_id": s.concepts[0].concept_id,
                    "concept_ids": [c.concept_id for c in s.concepts],
                    "chapter_ids": [ch.chapter_id for ch in s.chapters],
                    "n_concepts": len(s.concepts),
                    "n_chapters": len(s.chapters),
                    "n_gaps": sum(1 for c in s.concepts if c.chapter_count == 0),
                },
                generation_notes=f"depth-{s.concepts[0].depth} grouping (deterministic)",
                logical_key=f"stage_{s.ordinal}",
                # Provenance: every concept in the stage + every chapter assigned.
                sources=(
                    [("concept", c.concept_id, 1.0, 1.0, None)
                     for c in s.concepts]
                    + [("chapter", ch.chapter_id,
                        float(ch.concept_hits) / max(1, len(s.concepts)),
                        1.0, None)
                       for ch in s.chapters]
                ),
            )
            plan.units.append(unit)
            plan.files.append(GenFile(
                filename=f"stage-{s.ordinal}-{s.slug}/reading-list.md",
                content=unit.content_markdown,
                purpose="reading_list",
                unit_logical_key=f"stage_{s.ordinal}",
            ))

        plan.files.append(GenFile(
            filename="_path.md",
            content=_render_path_overview(d, stages),
            purpose="overview",
        ))

        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class LearningPathValidator:
    """Checks:

    * target concept resolved
    * every stage has ≥ 1 concept
    * concept_ids in stage metadata exist in the concept table
    * chapter_ids in stage metadata exist in the chapter table
    * stages are ordered by ordinal (1..N) with no gaps
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        target_id = plan.package_metadata.get("target_concept_id", -1)
        if target_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="target concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no stages produced (empty prerequisite tree)",
            ))
            return issues

        # Stage ordinals 1..N with no gaps
        ordinals = sorted(int(u.metadata.get("stage_ordinal", -1)) for u in plan.units)
        expected = list(range(1, len(ordinals) + 1))
        if ordinals != expected:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message=f"stage ordinals not contiguous: got {ordinals}, expected {expected}",
            ))

        # Collect ids to check for FK existence
        all_concept_ids: set[int] = set()
        all_chapter_ids: set[int] = set()
        for u in plan.units:
            for cid in u.metadata.get("concept_ids", []):
                all_concept_ids.add(int(cid))
            for chid in u.metadata.get("chapter_ids", []):
                all_chapter_ids.add(int(chid))
            if not u.metadata.get("concept_ids"):
                issues.append(ValidationIssue(
                    unit_logical_key=u.logical_key, severity="error",
                    message=f"stage {u.name} has no concepts",
                ))

        if all_concept_ids:
            ph = ",".join(["?"] * len(all_concept_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(all_concept_ids),
            ).fetchall()}
            missing = all_concept_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"concept_ids referenced but not in concept table: {sorted(missing)[:5]}",
                ))

        if all_chapter_ids:
            ph = ",".join(["?"] * len(all_chapter_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT chapter_id FROM chapter WHERE chapter_id IN ({ph})",
                list(all_chapter_ids),
            ).fetchall()}
            missing = all_chapter_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"chapter_ids referenced but not in chapter table: {sorted(missing)[:5]}",
                ))

        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class LearningPathMaterializer:
    """Writes the package layout to disk.

    Layout:
        <output_root>/<package_name>/_path.md
        <output_root>/<package_name>/stage-1-<slug>/reading-list.md
        <output_root>/<package_name>/stage-2-<slug>/reading-list.md
        …

    Reads ``generated_file`` rows; uses the file's stored ``filename``
    verbatim so the relative-path layout (``stage-N-foo/reading-list.md``)
    encoded by the planner just falls out.
    """

    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?",
            [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)

        rows = conn.execute(
            """
            SELECT filename, content FROM generated_file
             WHERE package_id = ?
             ORDER BY file_id
            """,
            [package_id],
        ).fetchall()

        written: list[str] = []
        skipped: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                skipped.append(str(target))
                continue
            target.write_text(content)
            written.append(str(target))

        notes = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files (overwrite=False)")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


# ---------------------------------------------------------------------------
# Convenience constructor
# ---------------------------------------------------------------------------


def make_learning_path_generator() -> Generator:
    """Return a fully-wired Learning Path generator."""
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=PrerequisiteDecomposer(),
        planner=LearningPathPlanner(),
        ranking_mode="generation",
        validator=LearningPathValidator(),
        materializer=LearningPathMaterializer(),
    )
