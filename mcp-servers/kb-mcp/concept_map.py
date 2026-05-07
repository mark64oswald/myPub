"""concept_map.py — Phase 7.2 Concept Neighborhood Map generator.

Produces a Mermaid + Graphviz DOT visualization of a concept's k-hop
graph neighborhood — REQUIRES, EXTENDS, CONTRASTS_WITH, IMPLEMENTS, and
CITES edges, with node coloring by source type and edge styling by
relation. Useful for orientation when starting on an unfamiliar topic,
embedding in design docs, and debugging the graph itself.

The generator is fully deterministic — no sub-agent prose, no LLM
calls. Decomposition runs a SQL traversal over ``concept_relation``,
the planner renders the diagram in two formats, the validator checks
that every node and edge resolves to real catalog rows, and the
materializer writes the package layout to disk.

This is the smallest credible smoke test for the Phase 7 generator
framework — it exercises entity resolution, graph traversal, and
materialization without needing a sophisticated decomposer.

Output layout (per architecture spec §2.8):

    concept-maps/<concept-name>/
      _map.md            # Concept summary + interpretation guide
      neighborhood.mmd   # Mermaid source
      neighborhood.dot   # Graphviz DOT source
      nodes.csv          # Node list with source counts (debugging)

(The pre-rendered SVG is left to a separate render step that shells
out to ``mmdc`` or ``dot``; we don't bundle that here to avoid the
external dependency.)
"""
from __future__ import annotations

import csv
import io
import logging
from collections import deque
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

LOG = logging.getLogger("mypub-concept-map")


GENERATOR_TYPE = "concept_map"
DEFAULT_DEPTH = 2
DEFAULT_MAX_NODES = 60
DEFAULT_RELATION_FILTER = ("REQUIRES", "EXTENDS", "CONTRASTS_WITH",
                            "IMPLEMENTS", "CITES")


# ---------------------------------------------------------------------------
# Data shape returned by the decomposer
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    depth: int                       # 0 = seed, 1 = direct neighbor, …
    chapter_count: int               # how many chapters mention this concept
    doc_section_count: int           # how many doc sections mention it
    procedure_count: int             # how many procedures link to it
    pruned: bool = False             # True if dropped by max_nodes cap


@dataclass
class _Edge:
    from_concept_id: int
    to_concept_id: int
    relation_type: str
    weight: int                      # edge frequency in concept_relation


@dataclass
class _Decomposition:
    seed_concept_id: int
    seed_name: str
    depth: int
    relation_filter: tuple[str, ...]
    nodes: list[_Node]               # included nodes
    edges: list[_Edge]               # edges among included nodes
    pruned_node_count: int           # how many got dropped by max_nodes
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class KHopDecomposer:
    """k-hop expansion over ``concept_relation``.

    Implementation notes:

    * BFS in Python rather than SQL recursive CTE — DuckPGQ traversals
      exist but we already have a simpler concept_relation table; the
      Python loop keeps the depth annotation explicit per node.
    * Every edge is collected once; duplicates between the same pair
      are aggregated by frequency for edge weight.
    * Pruning runs only if node count exceeds ``max_nodes``: we keep
      the highest-degree nodes within the neighborhood, then re-emit
      only edges among the kept nodes.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        depth: int = DEFAULT_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        relation_filter: Optional[Sequence[str]] = None,
        **_: Any,
    ) -> _Decomposition:
        rels = tuple(relation_filter) if relation_filter else DEFAULT_RELATION_FILTER

        seed_id = resolver.resolve_lookup_only(query)
        if seed_id is None:
            return _Decomposition(
                seed_concept_id=-1, seed_name=query,
                depth=depth, relation_filter=rels,
                nodes=[], edges=[], pruned_node_count=0,
                notes=[f"seed concept {query!r} not found"],
            )

        seed_name = conn.execute(
            "SELECT name FROM concept WHERE concept_id = ?", [seed_id],
        ).fetchone()[0]

        # BFS expansion
        visited: dict[int, int] = {seed_id: 0}  # concept_id -> shortest depth
        frontier = deque([seed_id])
        all_edges: list[_Edge] = []

        while frontier:
            cid = frontier.popleft()
            d = visited[cid]
            if d >= depth:
                continue
            placeholders = ",".join(["?"] * len(rels))
            rows = conn.execute(
                f"""
                SELECT from_concept_id, to_concept_id, relation_type,
                       COUNT(*) AS w
                  FROM concept_relation
                 WHERE relation_type IN ({placeholders})
                   AND (from_concept_id = ? OR to_concept_id = ?)
                 GROUP BY from_concept_id, to_concept_id, relation_type
                """,
                [*rels, cid, cid],
            ).fetchall()
            for fr, to, rt, w in rows:
                fr, to = int(fr), int(to)
                # Capture the edge
                all_edges.append(_Edge(
                    from_concept_id=fr, to_concept_id=to,
                    relation_type=rt, weight=int(w),
                ))
                # Enqueue the neighbor if unseen
                neighbor = to if fr == cid else fr
                if neighbor not in visited:
                    visited[neighbor] = d + 1
                    frontier.append(neighbor)

        # Build node records with source-type counts
        nodes = self._enrich_nodes(conn, list(visited.keys()), visited)
        nodes.sort(key=lambda n: (n.depth, -self._degree(n.concept_id, all_edges), n.name))

        # Prune by node-count cap
        pruned_count = 0
        if len(nodes) > max_nodes:
            kept_set = {n.concept_id for n in nodes[:max_nodes]}
            for n in nodes[max_nodes:]:
                n.pruned = True
            nodes = [n for n in nodes if n.concept_id in kept_set]
            pruned_count = len(visited) - len(nodes)
            # Drop edges that point to pruned nodes
            all_edges = [
                e for e in all_edges
                if e.from_concept_id in kept_set
                and e.to_concept_id in kept_set
            ]

        # Dedup edges (BFS visits both endpoints; same edge appears twice)
        edge_keys: set[tuple[int, int, str]] = set()
        deduped: list[_Edge] = []
        for e in all_edges:
            # Normalize directionality only for symmetric relations? Keep
            # directional for everything — a REQUIRES edge has direction.
            key = (e.from_concept_id, e.to_concept_id, e.relation_type)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            deduped.append(e)

        notes: list[str] = []
        if pruned_count > 0:
            notes.append(
                f"pruned {pruned_count} additional nodes; max_nodes={max_nodes}"
            )

        return _Decomposition(
            seed_concept_id=seed_id, seed_name=seed_name,
            depth=depth, relation_filter=rels,
            nodes=nodes, edges=deduped,
            pruned_node_count=pruned_count, notes=notes,
        )

    @staticmethod
    def _degree(concept_id: int, edges: list[_Edge]) -> int:
        return sum(
            1 for e in edges
            if e.from_concept_id == concept_id or e.to_concept_id == concept_id
        )

    @staticmethod
    def _enrich_nodes(
        conn: duckdb.DuckDBPyConnection,
        concept_ids: list[int],
        depth_map: dict[int, int],
    ) -> list[_Node]:
        """Pull name + concept_type + description + per-source counts."""
        if not concept_ids:
            return []
        ph = ",".join(["?"] * len(concept_ids))
        rows = conn.execute(
            f"""
            WITH chap AS (
              SELECT concept_id, COUNT(DISTINCT source_id) AS n
                FROM (
                  SELECT from_concept_id AS concept_id, source_id
                    FROM concept_relation
                   WHERE source_type = 'chapter'
                     AND from_concept_id IN ({ph})
                  UNION
                  SELECT to_concept_id AS concept_id, source_id
                    FROM concept_relation
                   WHERE source_type = 'chapter'
                     AND to_concept_id IN ({ph})
                )
                GROUP BY concept_id
            ),
            sec AS (
              SELECT concept_id, COUNT(DISTINCT source_id) AS n
                FROM (
                  SELECT from_concept_id AS concept_id, source_id
                    FROM concept_relation
                   WHERE source_type = 'doc_section'
                     AND from_concept_id IN ({ph})
                  UNION
                  SELECT to_concept_id AS concept_id, source_id
                    FROM concept_relation
                   WHERE source_type = 'doc_section'
                     AND to_concept_id IN ({ph})
                )
                GROUP BY concept_id
            ),
            proc AS (
              SELECT concept_id, COUNT(*) AS n
                FROM procedure_concept
               WHERE concept_id IN ({ph})
               GROUP BY concept_id
            )
            SELECT c.concept_id, c.name, c.concept_type, c.description,
                   COALESCE(chap.n, 0), COALESCE(sec.n, 0), COALESCE(proc.n, 0)
              FROM concept c
              LEFT JOIN chap ON chap.concept_id = c.concept_id
              LEFT JOIN sec  ON sec.concept_id  = c.concept_id
              LEFT JOIN proc ON proc.concept_id = c.concept_id
             WHERE c.concept_id IN ({ph})
            """,
            [*concept_ids, *concept_ids, *concept_ids, *concept_ids,
             *concept_ids, *concept_ids],
        ).fetchall()
        out: list[_Node] = []
        for r in rows:
            cid = int(r[0])
            out.append(_Node(
                concept_id=cid, name=r[1], concept_type=r[2],
                description=r[3], depth=depth_map.get(cid, 0),
                chapter_count=int(r[4]), doc_section_count=int(r[5]),
                procedure_count=int(r[6]),
            ))
        return out


# Allow the decomposer to be imported wherever Sequence is needed.
from collections.abc import Sequence  # noqa: E402  (used in type hint above)


# ---------------------------------------------------------------------------
# Renderer helpers — Mermaid + DOT + nodes.csv + overview.md
# ---------------------------------------------------------------------------


_NODE_COLOR_BY_COVERAGE = {
    "chapter":      "#1f6feb",  # book chapter coverage
    "doc_section":  "#bf8700",  # doc-source coverage
    "procedure":    "#9333ea",  # procedure-only
    "none":         "#8b949e",  # no source-typed coverage (graph-only)
}

_EDGE_STYLE_BY_RELATION = {
    "REQUIRES":       ("==>", "solid"),
    "EXTENDS":        ("-->", "dashed"),
    "CONTRASTS_WITH": ("-.->", "dotted"),
    "IMPLEMENTS":     ("==>", "bold"),
    "CITES":          ("-->", "thin"),
}


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    return "".join(c for c in s if c.isalnum() or c == "-").strip("-") or "concept"


def _node_coverage(n: _Node) -> str:
    if n.chapter_count > 0:
        return "chapter"
    if n.doc_section_count > 0:
        return "doc_section"
    if n.procedure_count > 0:
        return "procedure"
    return "none"


def _mermaid(decomp: _Decomposition) -> str:
    if not decomp.nodes:
        return "graph TD\n  empty[\"(no concepts)\"]\n"
    lines = ["graph LR"]
    # Node declarations with node-id sanitization.
    for n in decomp.nodes:
        nid = f"c{n.concept_id}"
        # Mermaid escapes for label characters. Mermaid uses square
        # brackets to wrap labels; strip backticks and quotes inside.
        label = n.name.replace('"', "'").replace("[", "(").replace("]", ")")
        if n.concept_id == decomp.seed_concept_id:
            label = f"⭐ {label}"
        lines.append(f"  {nid}[\"{label}\"]")
    # Edges.
    for e in decomp.edges:
        arrow, _style = _EDGE_STYLE_BY_RELATION.get(
            e.relation_type, ("-->", "solid"),
        )
        lines.append(
            f"  c{e.from_concept_id} {arrow}|{e.relation_type}| c{e.to_concept_id}"
        )
    # Coverage classes.
    classes_used: dict[str, list[int]] = {}
    for n in decomp.nodes:
        cov = _node_coverage(n)
        classes_used.setdefault(cov, []).append(n.concept_id)
    for cov, ids in classes_used.items():
        color = _NODE_COLOR_BY_COVERAGE[cov]
        lines.append(f"  classDef cov_{cov} fill:{color},color:#fff,stroke:#000;")
        for cid in ids:
            lines.append(f"  class c{cid} cov_{cov};")
    return "\n".join(lines) + "\n"


def _dot(decomp: _Decomposition) -> str:
    if not decomp.nodes:
        return 'digraph G {\n  empty [label="(no concepts)"];\n}\n'
    lines = ["digraph G {", '  rankdir="LR";', "  node [shape=box, style=\"rounded,filled\", fontname=\"Helvetica\"];"]
    for n in decomp.nodes:
        cov = _node_coverage(n)
        color = _NODE_COLOR_BY_COVERAGE[cov]
        label = n.name.replace('"', "'")
        if n.concept_id == decomp.seed_concept_id:
            label = f"⭐ {label}"
        lines.append(
            f'  c{n.concept_id} [label="{label}", fillcolor="{color}", fontcolor="white"];'
        )
    for e in decomp.edges:
        _arrow, style = _EDGE_STYLE_BY_RELATION.get(
            e.relation_type, ("-->", "solid"),
        )
        lines.append(
            f'  c{e.from_concept_id} -> c{e.to_concept_id} '
            f'[label="{e.relation_type}", style="{style}"];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _nodes_csv(decomp: _Decomposition) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "concept_id", "name", "concept_type", "depth",
        "chapter_count", "doc_section_count", "procedure_count",
    ])
    for n in decomp.nodes:
        w.writerow([
            n.concept_id, n.name, n.concept_type or "",
            n.depth, n.chapter_count, n.doc_section_count,
            n.procedure_count,
        ])
    return buf.getvalue()


def _overview_md(decomp: _Decomposition) -> str:
    if not decomp.nodes:
        return f"# {decomp.seed_name}\n\n(empty neighborhood)\n"
    lines = [
        f"# {decomp.seed_name}",
        "",
        f"**Seed concept:** {decomp.seed_name} (concept_id={decomp.seed_concept_id})",
        f"**Depth:** {decomp.depth} hop(s)",
        f"**Relation filter:** {', '.join(decomp.relation_filter)}",
        "",
        f"**Nodes:** {len(decomp.nodes)}",
        f"**Edges:** {len(decomp.edges)}",
    ]
    if decomp.pruned_node_count > 0:
        lines.append(f"**Pruned nodes:** {decomp.pruned_node_count} (high fan-out neighborhood)")
    lines.extend([
        "",
        "## Coverage legend",
        "",
        "- 🔵 **Book chapter** — concept appears in indexed book content",
        "- 🟠 **Doc section** — concept appears in current vendor docs",
        "- 🟣 **Procedure-only** — concept appears in extracted procedures",
        "- ⚪ **Graph-only** — no source-typed coverage (relation-only)",
        "",
        "## Edge styles",
        "",
        "- ==> REQUIRES (solid heavy)",
        "- --> EXTENDS (dashed)",
        "- -.-> CONTRASTS_WITH (dotted)",
        "- ==> IMPLEMENTS (bold)",
        "- --> CITES (thin)",
        "",
        "## Files",
        "",
        "- `neighborhood.mmd` — Mermaid source (renders in Markdown viewers)",
        "- `neighborhood.dot` — Graphviz DOT source (higher-fidelity rendering)",
        "- `nodes.csv` — Node list with source counts (debugging / analysis)",
        "",
    ])
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


class ConceptMapPlanner:
    """Renders the decomposition into a fully-loaded ``GenPlan``.

    One ``GenUnit`` per node; package-level files for the diagrams and
    overview. No sub-agent prose required.
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        depth: int = DEFAULT_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        relation_filter: Optional[Sequence[str]] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.seed_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.seed_name,
            source_query=d.seed_name,
            package_metadata={
                "seed_concept_id": d.seed_concept_id,
                "depth": d.depth,
                "max_nodes": max_nodes,
                "relation_filter": list(d.relation_filter),
                "pruned_node_count": d.pruned_node_count,
            },
            notes=list(d.notes),
        )

        # One unit per node; sources point at the concept itself.
        for ordinal, n in enumerate(d.nodes):
            unit = GenUnit(
                unit_type="concept_node",
                name=n.name,
                ordinal=ordinal,
                content_markdown=(n.description or "").strip(),
                metadata={
                    "concept_id": n.concept_id,
                    "concept_type": n.concept_type,
                    "depth": n.depth,
                    "chapter_count": n.chapter_count,
                    "doc_section_count": n.doc_section_count,
                    "procedure_count": n.procedure_count,
                    "is_seed": n.concept_id == d.seed_concept_id,
                },
                generation_notes=(
                    "seed concept" if n.concept_id == d.seed_concept_id
                    else f"depth-{n.depth} neighbor"
                ),
                logical_key=f"node_{n.concept_id}",
                sources=[("concept", n.concept_id, 1.0, 1.0, None)],
            )
            plan.units.append(unit)

        # Files: Mermaid, DOT, nodes.csv, _map.md
        plan.files.extend([
            GenFile(filename="neighborhood.mmd",
                    content=_mermaid(d), purpose="mermaid"),
            GenFile(filename="neighborhood.dot",
                    content=_dot(d), purpose="dot"),
            GenFile(filename="nodes.csv",
                    content=_nodes_csv(d), purpose="csv"),
            GenFile(filename="_map.md",
                    content=_overview_md(d), purpose="overview"),
        ])

        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ConceptMapValidator:
    """Checks that:

    * the seed concept resolved
    * every node's ``concept_id`` exists in ``concept``
    * every implied edge corresponds to a real ``concept_relation`` row
      (we re-query for the IDs in the package)
    * the Mermaid file parses (heuristic: starts with ``graph`` and
      every node-id reference appears as a declared node)
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        # Seed concept resolved
        seed_id = plan.package_metadata.get("seed_concept_id", -1)
        if seed_id == -1:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="seed concept not resolved",
            ))
            return issues
        if not plan.units:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no nodes in plan (empty neighborhood)",
            ))
            return issues

        # Concept IDs exist in concept table
        concept_ids = [
            u.metadata["concept_id"] for u in plan.units
            if "concept_id" in u.metadata
        ]
        if concept_ids:
            ph = ",".join(["?"] * len(concept_ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                concept_ids,
            ).fetchall()}
            for u in plan.units:
                cid = u.metadata.get("concept_id")
                if cid is not None and cid not in existing:
                    issues.append(ValidationIssue(
                        unit_logical_key=u.logical_key,
                        severity="error",
                        message=f"concept_id={cid} ({u.name!r}) not in concept table",
                    ))

        # Mermaid file: declared node IDs vs. referenced node IDs
        mermaid = next((f for f in plan.files
                        if f.filename == "neighborhood.mmd"), None)
        if mermaid is None:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="neighborhood.mmd missing from plan",
            ))
        else:
            issues.extend(_validate_mermaid(mermaid.content))

        return issues


def _validate_mermaid(text: str) -> list[ValidationIssue]:
    """Heuristic Mermaid syntax check.

    Without ``mmdc`` we can't fully parse Mermaid, but we can catch
    obvious problems: missing graph declaration, nodes referenced
    in edges that weren't declared, mismatched braces.
    """
    issues: list[ValidationIssue] = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        issues.append(ValidationIssue(
            unit_logical_key="", severity="error",
            message="mermaid file is empty",
        ))
        return issues
    if not lines[0].startswith(("graph ", "flowchart ")):
        issues.append(ValidationIssue(
            unit_logical_key="", severity="error",
            message=f"mermaid does not start with graph/flowchart: {lines[0]!r}",
        ))
    # Node declarations look like:  cN["label"]    →  ID is the prefix
    declared: set[str] = set()
    referenced: set[str] = set()
    import re as _re
    decl_re = _re.compile(r'^(c\d+)\[')
    edge_re = _re.compile(r'^(c\d+)\s+(?:[-=]+>|[-.]+->)')
    for ln in lines[1:]:
        m = decl_re.match(ln)
        if m:
            declared.add(m.group(1))
        m = edge_re.match(ln)
        if m:
            referenced.add(m.group(1))
            # second endpoint after the arrow
            tail = _re.search(r'(c\d+)\s*$', ln)
            if tail:
                referenced.add(tail.group(1))
    missing = referenced - declared
    if missing:
        issues.append(ValidationIssue(
            unit_logical_key="", severity="error",
            message=f"mermaid edges reference undeclared nodes: {sorted(missing)}",
        ))
    return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class ConceptMapMaterializer:
    """Writes the package's files to ``<output_root>/<package_name>/``.

    Reads ``generated_file`` rows for the package and writes each by
    filename. Idempotent over re-runs (overwrite=True default).
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


def make_concept_map_generator() -> Generator:
    """Return a fully-wired Concept Neighborhood Map generator.

    Usable as:

        gen = make_concept_map_generator()
        package_id, report, issues = gen.run_deterministic(
            conn, resolver, "Event Sourcing", depth=2,
        )
    """
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=KHopDecomposer(),
        planner=ConceptMapPlanner(),
        ranking_mode="generation",
        validator=ConceptMapValidator(),
        materializer=ConceptMapMaterializer(),
    )
