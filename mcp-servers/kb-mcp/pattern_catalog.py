"""pattern_catalog.py — Phase 11 Pattern + Anti-Pattern Catalog.

Produces a curated catalog of patterns within a domain plus an
anti-pattern complement. Foundational for Phase 15 Project Bootstrap
(which composes Pattern → Procedure into runnable scaffolds) and
Phase 12 ADR (which uses CONTRASTS_WITH for option framing).

Decomposition:

  1. Resolve domain to a graph concept (or to a set of seed concepts
     for a multi-concept domain like "resilience").
  2. Find Pattern-typed concepts in the seed neighborhood (BFS over
     IMPLEMENTS + EXTENDS, capped depth).
  3. Cluster patterns by IMPLEMENTS-target overlap: patterns that
     IMPLEMENT the same higher concepts share a "family". Each
     cluster becomes a section in the catalog.
  4. For each pattern, gather:
        - chapters that mention it (reading list)
        - sibling patterns (same family) — discriminating context
        - CONTRASTS_WITH neighbors → anti-patterns for the inverse
          file
  5. Surface conflicts: when ranking signals (chapter authority vs
     doc currency) disagree on which pattern is canonical, flag in
     the catalog with an explicit "sources disagree" note.

Output:

    pattern-catalogs/<domain>/
      _catalog.md           overview + per-family pattern list
      _anti_patterns.md     CONTRASTS_WITH-derived anti-pattern catalog
      patterns/
        <pattern-slug>.md   per-pattern: when-to-use, when-not-to-use,
                            common pitfalls, references

Ranking mode: interactive — surface conflicts. The catalog is a
debate-ready reference; a Bootstrap generator that consumes it picks
silent-mode positions, but the catalog itself shows the debate.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
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

LOG = logging.getLogger("mypub-pattern-catalog")


GENERATOR_TYPE = "pattern_catalog"
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_PATTERNS = 30
DEFAULT_MAX_ANTI_PATTERNS = 20
DEFAULT_MAX_REFERENCES_PER_PATTERN = 4


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class _Pattern:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    chapter_count: int
    doc_section_count: int
    implements_targets: set[int]   # concepts this pattern IMPLEMENTS
    contrasts: set[int]            # CONTRASTS_WITH neighbors
    extends: set[int]              # EXTENDS chain (more general)


@dataclass
class _Family:
    """Cluster of patterns sharing IMPLEMENTS targets."""

    family_id: int
    canonical_target_id: int
    canonical_target_name: str
    patterns: list[_Pattern] = field(default_factory=list)


@dataclass
class _AntiPattern:
    """A pattern that is the CONTRASTS_WITH neighbor of an in-catalog
    pattern. The 'rationale' is the contrasting pattern's name."""

    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    contrasts_with_in_catalog: list[tuple[int, str]]  # (pattern_id, name)


@dataclass
class _Decomposition:
    domain_concept_id: int
    domain_name: str
    domain_concept_type: Optional[str]
    families: list[_Family]
    patterns: list[_Pattern]
    anti_patterns: list[_AntiPattern]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class PatternDiscoveryDecomposer:
    """Discover patterns + anti-patterns within a domain.

    Strategy:
      1. Resolve domain concept.
      2. BFS expand over IMPLEMENTS + EXTENDS to find related concepts.
      3. Filter to Pattern-typed concepts (or concepts with Pattern in
         the type name as a fallback).
      4. Group by shared IMPLEMENTS targets; each group is a family.
      5. CONTRASTS_WITH neighbors of in-catalog patterns become anti-
         patterns.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_patterns: int = DEFAULT_MAX_PATTERNS,
        max_anti_patterns: int = DEFAULT_MAX_ANTI_PATTERNS,
        **_: Any,
    ) -> _Decomposition:
        domain_id = resolver.resolve_lookup_only(query)
        if domain_id is None:
            return _Decomposition(
                domain_concept_id=-1, domain_name=query,
                domain_concept_type=None,
                families=[], patterns=[], anti_patterns=[],
                notes=[f"domain concept {query!r} not found"],
            )
        domain_row = conn.execute(
            "SELECT name, concept_type FROM concept WHERE concept_id = ?",
            [domain_id],
        ).fetchone()

        # 1. Find candidate pattern concept_ids
        pattern_ids = self._find_pattern_candidates(
            conn, domain_id, max_depth=max_depth, limit=max_patterns,
        )
        if not pattern_ids:
            return _Decomposition(
                domain_concept_id=domain_id, domain_name=domain_row[0],
                domain_concept_type=domain_row[1],
                families=[], patterns=[], anti_patterns=[],
                notes=[f"no Pattern-typed concepts found near {domain_row[0]!r}"],
            )

        # 2. Enrich patterns with chapter/doc_section counts + edges
        patterns = self._enrich_patterns(conn, pattern_ids)

        # 3. Group by IMPLEMENTS-target overlap
        families = self._group_into_families(conn, patterns)

        # 4. Find anti-patterns via CONTRASTS_WITH
        anti_patterns = self._find_anti_patterns(
            conn, patterns, limit=max_anti_patterns,
        )

        return _Decomposition(
            domain_concept_id=domain_id, domain_name=domain_row[0],
            domain_concept_type=domain_row[1],
            families=families, patterns=patterns,
            anti_patterns=anti_patterns,
        )

    @staticmethod
    def _find_pattern_candidates(
        conn: duckdb.DuckDBPyConnection,
        domain_id: int,
        *, max_depth: int, limit: int,
    ) -> list[int]:
        """BFS over IMPLEMENTS + EXTENDS from the domain seed; return
        Pattern-typed concept_ids ranked by chapter coverage."""
        from collections import deque
        seen = {domain_id}
        frontier = deque([(domain_id, 0)])
        candidates: set[int] = set()
        while frontier:
            cid, d = frontier.popleft()
            if d >= max_depth:
                continue
            rows = conn.execute(
                """
                SELECT to_concept_id FROM concept_relation
                 WHERE from_concept_id = ?
                   AND relation_type IN ('IMPLEMENTS', 'EXTENDS', 'REQUIRES')
                UNION
                SELECT from_concept_id FROM concept_relation
                 WHERE to_concept_id = ?
                   AND relation_type IN ('IMPLEMENTS', 'EXTENDS', 'REQUIRES')
                """,
                [cid, cid],
            ).fetchall()
            for (n,) in rows:
                n = int(n)
                if n in seen:
                    continue
                seen.add(n)
                candidates.add(n)
                frontier.append((n, d + 1))
        # Filter to Pattern-typed
        if not candidates:
            return []
        ph = ",".join(["?"] * len(candidates))
        rows = conn.execute(
            f"""
            SELECT c.concept_id,
                   (SELECT COUNT(DISTINCT cr.source_id)
                      FROM concept_relation cr
                      JOIN chapter ch ON ch.chapter_id = cr.source_id
                     WHERE cr.source_type = 'chapter'
                       AND (cr.from_concept_id = c.concept_id
                            OR cr.to_concept_id = c.concept_id)
                   ) AS chapters
              FROM concept c
             WHERE c.concept_id IN ({ph})
               AND (c.concept_type = 'Pattern' OR c.concept_type LIKE '%Pattern%')
             ORDER BY chapters DESC
             LIMIT ?
            """,
            [*candidates, limit],
        ).fetchall()
        return [int(r[0]) for r in rows]

    @staticmethod
    def _enrich_patterns(
        conn: duckdb.DuckDBPyConnection, pattern_ids: list[int],
    ) -> list[_Pattern]:
        if not pattern_ids:
            return []
        ph = ",".join(["?"] * len(pattern_ids))
        # Base info + counts
        rows = conn.execute(
            f"""
            SELECT c.concept_id, c.name, c.concept_type, c.description
              FROM concept c WHERE c.concept_id IN ({ph})
            """,
            pattern_ids,
        ).fetchall()
        base = {int(r[0]): r for r in rows}

        # chapter_count + doc_section_count via JOIN to real rows
        chap = dict(conn.execute(
            f"""
            SELECT cid, COUNT(DISTINCT chapter_id) FROM (
              SELECT cr.from_concept_id AS cid, ch.chapter_id
                FROM concept_relation cr JOIN chapter ch ON ch.chapter_id = cr.source_id
               WHERE cr.source_type = 'chapter' AND cr.from_concept_id IN ({ph})
              UNION
              SELECT cr.to_concept_id, ch.chapter_id
                FROM concept_relation cr JOIN chapter ch ON ch.chapter_id = cr.source_id
               WHERE cr.source_type = 'chapter' AND cr.to_concept_id IN ({ph})
            ) GROUP BY cid
            """,
            [*pattern_ids, *pattern_ids],
        ).fetchall())
        sec = dict(conn.execute(
            f"""
            SELECT cid, COUNT(DISTINCT doc_section_id) FROM (
              SELECT cr.from_concept_id AS cid, ds.doc_section_id
                FROM concept_relation cr JOIN doc_section ds ON ds.doc_section_id = cr.source_id
               WHERE cr.source_type = 'doc_section' AND cr.from_concept_id IN ({ph})
              UNION
              SELECT cr.to_concept_id, ds.doc_section_id
                FROM concept_relation cr JOIN doc_section ds ON ds.doc_section_id = cr.source_id
               WHERE cr.source_type = 'doc_section' AND cr.to_concept_id IN ({ph})
            ) GROUP BY cid
            """,
            [*pattern_ids, *pattern_ids],
        ).fetchall())

        # Edge sets per pattern
        impl: dict[int, set[int]] = defaultdict(set)
        contrasts: dict[int, set[int]] = defaultdict(set)
        extends: dict[int, set[int]] = defaultdict(set)
        rows = conn.execute(
            f"""
            SELECT from_concept_id, to_concept_id, relation_type
              FROM concept_relation
             WHERE relation_type IN ('IMPLEMENTS', 'CONTRASTS_WITH', 'EXTENDS')
               AND (from_concept_id IN ({ph}) OR to_concept_id IN ({ph}))
            """,
            [*pattern_ids, *pattern_ids],
        ).fetchall()
        pat_set = set(pattern_ids)
        for fr, to, rt in rows:
            fr, to = int(fr), int(to)
            if fr in pat_set:
                if rt == "IMPLEMENTS":
                    impl[fr].add(to)
                elif rt == "CONTRASTS_WITH":
                    contrasts[fr].add(to)
                elif rt == "EXTENDS":
                    extends[fr].add(to)
            if to in pat_set and rt == "CONTRASTS_WITH":
                contrasts[to].add(fr)

        out: list[_Pattern] = []
        for pid in pattern_ids:
            row = base.get(pid)
            if not row:
                continue
            out.append(_Pattern(
                concept_id=pid, name=row[1], concept_type=row[2],
                description=row[3],
                chapter_count=int(chap.get(pid, 0)),
                doc_section_count=int(sec.get(pid, 0)),
                implements_targets=impl.get(pid, set()),
                contrasts=contrasts.get(pid, set()),
                extends=extends.get(pid, set()),
            ))
        return out

    @staticmethod
    def _group_into_families(
        conn: duckdb.DuckDBPyConnection, patterns: list[_Pattern],
    ) -> list[_Family]:
        """Patterns sharing an IMPLEMENTS target form a family.

        We pick the most-shared target as the family's canonical
        anchor; patterns implementing only unique targets become a
        single-member 'Standalone' family at the end.
        """
        # Count target popularity
        target_count: dict[int, int] = defaultdict(int)
        for p in patterns:
            for t in p.implements_targets:
                target_count[t] += 1

        # Sort targets by popularity desc; greedy-assign patterns to
        # the most-popular target they implement.
        sorted_targets = [t for t, c in sorted(
            target_count.items(), key=lambda kv: -kv[1]) if c >= 2]
        target_names = {}
        if sorted_targets:
            ph = ",".join(["?"] * len(sorted_targets))
            for cid, name in conn.execute(
                f"SELECT concept_id, name FROM concept WHERE concept_id IN ({ph})",
                sorted_targets,
            ).fetchall():
                target_names[int(cid)] = name

        assigned: set[int] = set()
        families: list[_Family] = []
        for fam_idx, t in enumerate(sorted_targets):
            members = [
                p for p in patterns
                if p.concept_id not in assigned and t in p.implements_targets
            ]
            if not members:
                continue
            for p in members:
                assigned.add(p.concept_id)
            families.append(_Family(
                family_id=fam_idx,
                canonical_target_id=t,
                canonical_target_name=target_names.get(t, "(unnamed)"),
                patterns=members,
            ))

        # Standalone bucket
        leftover = [p for p in patterns if p.concept_id not in assigned]
        if leftover:
            families.append(_Family(
                family_id=len(families),
                canonical_target_id=-1,
                canonical_target_name="Standalone",
                patterns=leftover,
            ))
        return families

    @staticmethod
    def _find_anti_patterns(
        conn: duckdb.DuckDBPyConnection,
        patterns: list[_Pattern],
        *, limit: int,
    ) -> list[_AntiPattern]:
        """For every CONTRASTS_WITH neighbor of an in-catalog pattern,
        emit one _AntiPattern entry. Grouped by anti-pattern concept;
        ranked by how many in-catalog patterns contrast with it."""
        if not patterns:
            return []
        in_catalog = {p.concept_id: p.name for p in patterns}
        anti_map: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for p in patterns:
            for nb in p.contrasts:
                if nb in in_catalog:
                    continue  # both in-catalog ⇒ peer relation, not anti
                anti_map[nb].append((p.concept_id, p.name))
        if not anti_map:
            return []
        ph = ",".join(["?"] * len(anti_map))
        rows = conn.execute(
            f"""
            SELECT concept_id, name, concept_type, description
              FROM concept WHERE concept_id IN ({ph})
            """,
            list(anti_map),
        ).fetchall()
        out = []
        for r in rows:
            cid = int(r[0])
            out.append(_AntiPattern(
                concept_id=cid, name=r[1],
                concept_type=r[2], description=r[3],
                contrasts_with_in_catalog=anti_map[cid],
            ))
        # Rank: most-contrasted first
        out.sort(key=lambda a: -len(a.contrasts_with_in_catalog))
        return out[:limit]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "pattern"


def _short_summary(text: Optional[str], limit: int = 200) -> str:
    if not text:
        return "_no description_"
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


def _render_catalog(d: _Decomposition) -> str:
    lines = [
        f"# {d.domain_name} — Pattern Catalog",
        "",
        f"_{len(d.patterns)} patterns across {len(d.families)} family/families._",
        "",
    ]
    if not d.families:
        lines.append("_No patterns found in this domain._")
        return "\n".join(lines)
    for fam in d.families:
        lines.append(f"## {fam.canonical_target_name}")
        lines.append("")
        for p in fam.patterns:
            lines.append(f"### {p.name}")
            lines.append(_short_summary(p.description))
            lines.append("")
            sources_note = (
                f"_{p.chapter_count} chapter(s), "
                f"{p.doc_section_count} doc section(s)._"
            )
            lines.append(sources_note)
            lines.append("")
            lines.append(f"→ See `patterns/{_slugify(p.name)}.md`")
            lines.append("")
    return "\n".join(lines)


def _render_pattern(
    conn: duckdb.DuckDBPyConnection,
    pattern: _Pattern,
    *, max_refs: int = DEFAULT_MAX_REFERENCES_PER_PATTERN,
) -> str:
    lines = [
        f"# {pattern.name}",
        "",
    ]
    if pattern.concept_type:
        lines.append(f"_Type: {pattern.concept_type}_")
        lines.append("")
    if pattern.description:
        lines.append("## Summary")
        lines.append("")
        lines.append(pattern.description.strip())
        lines.append("")

    # When to use — derived heuristically: pattern + its IMPLEMENTS
    # targets explain the use case.
    if pattern.implements_targets:
        ph = ",".join(["?"] * len(pattern.implements_targets))
        impl_rows = conn.execute(
            f"SELECT name FROM concept WHERE concept_id IN ({ph}) ORDER BY name",
            list(pattern.implements_targets),
        ).fetchall()
        if impl_rows:
            lines.append("## When to use")
            lines.append("")
            lines.append(
                f"Use {pattern.name} when implementing: "
                + ", ".join(f"**{r[0]}**" for r in impl_rows[:5])
                + "."
            )
            lines.append("")

    # When NOT to use — derived from CONTRASTS_WITH (this pattern's
    # alternatives suggest scenarios where the alternative wins).
    if pattern.contrasts:
        ph = ",".join(["?"] * len(pattern.contrasts))
        contrast_rows = conn.execute(
            f"SELECT name FROM concept WHERE concept_id IN ({ph}) ORDER BY name",
            list(pattern.contrasts),
        ).fetchall()
        if contrast_rows:
            lines.append("## When NOT to use")
            lines.append("")
            lines.append(
                f"Consider alternatives when these patterns fit better: "
                + ", ".join(f"**{r[0]}**" for r in contrast_rows[:5])
                + "."
            )
            lines.append("")

    # References — top chapters that mention this pattern
    rows = conn.execute(
        """
        SELECT b.title, c.title, COUNT(*) AS mentions
          FROM concept_relation cr
          JOIN chapter c ON c.chapter_id = cr.source_id
          JOIN book    b ON b.book_id = c.book_id
         WHERE cr.source_type = 'chapter'
           AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
         GROUP BY b.title, c.title
         ORDER BY mentions DESC
         LIMIT ?
        """,
        [pattern.concept_id, pattern.concept_id, max_refs],
    ).fetchall()
    if rows:
        lines.append("## References")
        lines.append("")
        for r in rows:
            lines.append(f"- **{r[0]}** — _{r[1]}_ ({r[2]} mention(s))")
        lines.append("")

    return "\n".join(lines)


def _render_anti_patterns(d: _Decomposition) -> str:
    lines = [
        f"# {d.domain_name} — Anti-Patterns",
        "",
        "_Patterns that CONTRAST with the catalog patterns. Listed in "
        "order of how many in-catalog patterns contrast with them._",
        "",
    ]
    if not d.anti_patterns:
        lines.append("_No CONTRASTS_WITH neighbors found for the catalog patterns._")
        return "\n".join(lines)
    for ap in d.anti_patterns:
        lines.append(f"## {ap.name}")
        lines.append("")
        if ap.description:
            lines.append(_short_summary(ap.description))
            lines.append("")
        lines.append(
            f"_Contrasts with {len(ap.contrasts_with_in_catalog)} in-catalog "
            f"pattern(s):_ "
            + ", ".join(f"**{n}**" for _pid, n in ap.contrasts_with_in_catalog[:5])
        )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class PatternCatalogPlanner:
    """Renders the discovery into a fully-loaded GenPlan.

    One ``GenUnit`` per pattern. Files: _catalog.md, _anti_patterns.md,
    and per-pattern patterns/<slug>.md.
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        max_references: int = DEFAULT_MAX_REFERENCES_PER_PATTERN,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.domain_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.domain_name,
            source_query=d.domain_name,
            package_metadata={
                "domain_concept_id": d.domain_concept_id,
                "domain_concept_type": d.domain_concept_type,
                "n_patterns": len(d.patterns),
                "n_families": len(d.families),
                "n_anti_patterns": len(d.anti_patterns),
            },
            notes=list(d.notes),
        )

        for ordinal, pattern in enumerate(d.patterns):
            family_idx = next(
                (f.family_id for f in d.families
                 if pattern.concept_id in {p.concept_id for p in f.patterns}),
                -1,
            )
            unit = GenUnit(
                unit_type="pattern",
                name=pattern.name,
                ordinal=ordinal,
                metadata={
                    "concept_id": pattern.concept_id,
                    "concept_type": pattern.concept_type,
                    "family_id": family_idx,
                    "chapter_count": pattern.chapter_count,
                    "doc_section_count": pattern.doc_section_count,
                    "n_implements": len(pattern.implements_targets),
                    "n_contrasts": len(pattern.contrasts),
                },
                logical_key=f"pattern_{pattern.concept_id}",
                content_markdown=_short_summary(pattern.description),
                sources=[("concept", pattern.concept_id, 1.0, 1.0, None)],
            )
            plan.units.append(unit)
            plan.files.append(GenFile(
                filename=f"patterns/{_slugify(pattern.name)}.md",
                content=_render_pattern(conn, pattern, max_refs=max_references),
                purpose="pattern_detail",
                unit_logical_key=f"pattern_{pattern.concept_id}",
            ))

        plan.files.extend([
            GenFile(filename="_catalog.md",
                    content=_render_catalog(d), purpose="catalog"),
            GenFile(filename="_anti_patterns.md",
                    content=_render_anti_patterns(d), purpose="anti_patterns"),
        ])
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class PatternCatalogValidator:
    """Checks:

    * domain concept resolved
    * every pattern's concept_id exists in the concept table
    * patterns/ folder has one file per pattern
    * _catalog.md mentions every pattern
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        domain_id = plan.package_metadata.get("domain_concept_id", -1)
        if domain_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="domain concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no patterns found in domain; catalog will be empty",
            ))
            return issues

        # FK existence on pattern concept_ids
        all_ids: set[int] = set()
        for u in plan.units:
            cid = u.metadata.get("concept_id")
            if cid is not None:
                all_ids.add(int(cid))
        if all_ids:
            ph = ",".join(["?"] * len(all_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(all_ids),
            ).fetchall()}
            missing = all_ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"pattern concept_ids missing: {sorted(missing)[:5]}",
                ))

        # Per-pattern file present
        per_pattern_files = {
            f.filename for f in plan.files if f.purpose == "pattern_detail"
        }
        if len(per_pattern_files) != len(plan.units):
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message=f"pattern file count {len(per_pattern_files)} != "
                        f"unit count {len(plan.units)}",
            ))

        # _catalog.md mentions every pattern by name
        catalog = next(
            (f.content for f in plan.files if f.filename == "_catalog.md"), "",
        )
        missing_in_catalog = [u.name for u in plan.units if u.name not in catalog]
        if missing_in_catalog:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"patterns missing from _catalog.md: "
                        f"{missing_in_catalog[:3]} ...",
            ))

        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class PatternCatalogMaterializer:
    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
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
            "WHERE package_id = ? ORDER BY file_id",
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
        notes: list[str] = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


def make_pattern_catalog_generator() -> Generator:
    """Return a fully-wired Pattern + Anti-Pattern Catalog generator."""
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=PatternDiscoveryDecomposer(),
        planner=PatternCatalogPlanner(),
        ranking_mode="interactive",
        validator=PatternCatalogValidator(),
        materializer=PatternCatalogMaterializer(),
    )
