"""generator.py — Phase 7 generalized generator framework.

The Skills Factory's seven-stage pipeline (decompose → plan → retrieve
→ rank → merge → generate → materialize) is the prototype. Other
generators (Concept Neighborhood Map, Learning Path, Tutorial, etc.)
share the same shape with different components plugged in. This module
defines the framework interfaces; concrete generators implement them.

Components — each generator provides its own:

* **Decomposer** — break the user's request into structured units
  (clusters of concepts, prerequisite paths, k-hop neighborhoods, …).
* **Planner** — order units, pick per-unit strategy, mint folder names.
* **OutputTemplate** — render units into the chosen output shape
  (SKILL.md, Mermaid diagram, learning-path stage, …).
* **Validator** — check each unit's structural correctness.
* **Materializer** — write the final artifacts to disk.

The retrieval/ranking infrastructure (search_chapters, weight profiles,
modality fan-out, alignment edges) is shared and accessed via
``search_fn``, not pluggable here. ``ranking_mode`` selects between
``"generation"`` (silent, decisive) and ``"interactive"`` (surface
conflicts) — see architecture spec §8.

The framework's job is to standardize:

* The **provenance trail** — every output unit traces back to source
  rows with a uniform score/weight/drop_reason structure
  (``generated_source``).
* The **ingest path** — sub-agent results land in well-known files,
  get parsed, validated, and committed to ``generated_unit`` /
  ``generated_file`` rows.
* The **idempotency contract** — re-running ``process`` for a package
  clears prior units and re-ingests fresh.

Skills Factory keeps using its existing ``skill_*`` tables for
backward compatibility; new generators land in ``generated_*``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import duckdb


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


RankingMode = str  # "generation" | "interactive"


@dataclass
class GenUnit:
    """One unit a generator produces. Maps 1:1 to a ``generated_unit`` row.

    ``unit_type`` is generator-specific ("concept_node", "learning_stage",
    "skill"). ``metadata_json`` carries per-generator fields not in the
    common columns (e.g., depth/relation_type for concept_map nodes).
    """

    unit_type: str
    name: str
    ordinal: int = 0
    parent_unit_key: Optional[str] = None  # logical key, resolved post-insert
    content_markdown: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    generation_notes: str = ""
    # Provenance: list of (source_type, source_id, score, weight, drop_reason)
    sources: list[tuple[str, int, float, float, Optional[str]]] = field(default_factory=list)
    # Logical key the planner uses to reference this unit (folder name,
    # stable id). Resolved to integer unit_id on insert.
    logical_key: str = ""


@dataclass
class GenFile:
    """One artifact file the generator produces.

    Bound either to a specific unit (``unit_logical_key``) or to the
    package as a whole (left unbound; ``unit_logical_key=""``).
    """

    filename: str
    content: str
    purpose: str = ""  # 'mermaid', 'dot', 'svg', 'overview', etc.
    unit_logical_key: str = ""


@dataclass
class GenPlan:
    """The plan + content emitted by a generator before persistence.

    A generator's ``plan_and_render`` produces this; ``persist`` writes
    it to ``generated_*`` tables and disk.
    """

    generator_type: str
    package_name: str
    domain: str
    target_audience: Optional[str] = None
    source_query: Optional[str] = None
    package_metadata: dict[str, Any] = field(default_factory=dict)
    units: list[GenUnit] = field(default_factory=list)
    files: list[GenFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationIssue:
    """One validation finding. Severity 'error' fails persistence."""

    unit_logical_key: str
    severity: str  # 'error' | 'warning'
    message: str


# ---------------------------------------------------------------------------
# Component protocols
# ---------------------------------------------------------------------------


class Decomposer(Protocol):
    """Break the user request into structured units.

    Returns whatever shape the matching ``Planner`` expects — concept
    clusters, prerequisite paths, neighborhoods, etc. The framework
    stays agnostic; the (Decomposer, Planner) pair is paired per
    generator.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        **kwargs: Any,
    ) -> Any:
        """Run the decomposition; return data shaped for the matching Planner."""
        raise NotImplementedError


class Planner(Protocol):
    """Take a Decomposer's output and produce a fully-rendered ``GenPlan``.

    For a generator that needs sub-agent prose generation (Skills,
    Tutorial), this stage usually emits an empty plan; the
    ``OutputTemplate`` then prompts the sub-agent and ``persist`` is
    called on the post-process result. For a fully-deterministic
    generator (Concept Map), this stage renders everything in one pass.
    """

    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: Any,
        *,
        package_name: Optional[str],
        **kwargs: Any,
    ) -> GenPlan:
        """Render the decomposition into a ``GenPlan`` ready to persist."""
        raise NotImplementedError


class Validator(Protocol):
    """Check that a ``GenPlan``'s units pass generator-specific
    correctness rules — graph IDs resolve, file syntax parses, etc.
    """

    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        """Return validation issues; severity='error' fails persistence."""
        raise NotImplementedError


class Materializer(Protocol):
    """Write the persisted package to disk in whatever layout the
    generator produces. Reads ``generated_*`` rows + files; emits
    files under ``output_root/<package_name>/``."""

    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> "MaterializeReport":
        """Write the package's persisted artifacts to disk."""
        raise NotImplementedError


@dataclass
class MaterializeReport:
    """Common shape returned by every Materializer."""

    package_id: int
    package_name: str
    output_root: str
    file_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistence — generic, shared across all generators
# ---------------------------------------------------------------------------


def upsert_package(
    conn: duckdb.DuckDBPyConnection, plan: GenPlan,
) -> int:
    """Insert (or return existing id for) a ``generated_package`` row.

    Generators are uniquely keyed by (generator_type, name) per the
    schema. Re-running with the same identity returns the existing
    package_id; callers then call ``clear_prior_units`` before
    re-persisting.
    """
    import json as _json

    metadata_json = (
        _json.dumps(plan.package_metadata, sort_keys=True)
        if plan.package_metadata else None
    )
    existing = conn.execute(
        "SELECT package_id FROM generated_package "
        " WHERE generator_type = ? AND name = ?",
        [plan.generator_type, plan.package_name],
    ).fetchone()
    if existing:
        # Refresh metadata + domain on re-run; tunables may have changed
        # (depth, max_concepts, etc.) and the row's metadata_json should
        # reflect what's actually persisted now.
        pkg_id = int(existing[0])
        conn.execute(
            """
            UPDATE generated_package
               SET domain = ?, target_audience = ?, source_query = ?,
                   metadata_json = ?
             WHERE package_id = ?
            """,
            [plan.domain, plan.target_audience, plan.source_query,
             metadata_json, pkg_id],
        )
        return pkg_id

    row = conn.execute(
        """
        INSERT INTO generated_package
            (generator_type, name, domain, target_audience,
             source_query, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING package_id
        """,
        [
            plan.generator_type, plan.package_name, plan.domain,
            plan.target_audience, plan.source_query, metadata_json,
        ],
    ).fetchone()
    assert row is not None
    return int(row[0])


def clear_prior_units(
    conn: duckdb.DuckDBPyConnection, package_id: int,
) -> None:
    """Drop every unit + source + file row attached to ``package_id``.

    Called before re-persisting to make process steps idempotent. Does
    NOT delete the ``generated_package`` row itself — that's stable.
    """
    unit_ids = [r[0] for r in conn.execute(
        "SELECT unit_id FROM generated_unit WHERE package_id = ?",
        [package_id],
    ).fetchall()]
    if unit_ids:
        ph = ",".join(["?"] * len(unit_ids))
        conn.execute(
            f"DELETE FROM generated_source WHERE unit_id IN ({ph})",
            unit_ids,
        )
        conn.execute(
            f"DELETE FROM generated_file WHERE unit_id IN ({ph})",
            unit_ids,
        )
        conn.execute(
            f"DELETE FROM generated_unit WHERE unit_id IN ({ph})",
            unit_ids,
        )
    # Package-level files (unit_id IS NULL).
    conn.execute(
        "DELETE FROM generated_file WHERE package_id = ? AND unit_id IS NULL",
        [package_id],
    )


def persist(
    conn: duckdb.DuckDBPyConnection, plan: GenPlan,
) -> int:
    """Write ``plan`` to ``generated_*`` tables. Returns ``package_id``.

    Idempotent: clears prior units for the same (generator_type, name)
    pair, then inserts the new content. Concrete generators call this
    after their ``Validator`` passes.
    """
    import json as _json

    package_id = upsert_package(conn, plan)
    clear_prior_units(conn, package_id)

    # Insert units; build logical_key → unit_id map.
    key_to_id: dict[str, int] = {}
    # First pass: insert all units (skip parent_unit_id wiring on first
    # pass; some parents may appear later in the list).
    for u in plan.units:
        meta_json = _json.dumps(u.metadata, sort_keys=True) if u.metadata else None
        row = conn.execute(
            """
            INSERT INTO generated_unit
                (package_id, unit_type, name, ordinal,
                 parent_unit_id, content_markdown, metadata_json,
                 generation_notes)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
            RETURNING unit_id
            """,
            [
                package_id, u.unit_type, u.name, u.ordinal,
                u.content_markdown, meta_json, u.generation_notes,
            ],
        ).fetchone()
        assert row is not None
        unit_id = int(row[0])
        if u.logical_key:
            key_to_id[u.logical_key] = unit_id
        # Sources
        for st, sid, score, weight, drop_reason in u.sources:
            conn.execute(
                """
                INSERT OR IGNORE INTO generated_source
                    (unit_id, source_type, source_id, score, weight, drop_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [unit_id, st, int(sid), score, weight, drop_reason],
            )

    # Second pass: wire parent_unit_id from logical keys.
    for u in plan.units:
        if not u.parent_unit_key:
            continue
        child_id = key_to_id.get(u.logical_key)
        parent_id = key_to_id.get(u.parent_unit_key)
        if child_id is not None and parent_id is not None:
            conn.execute(
                "UPDATE generated_unit SET parent_unit_id = ? WHERE unit_id = ?",
                [parent_id, child_id],
            )

    # Files
    for f in plan.files:
        unit_id_for_file = key_to_id.get(f.unit_logical_key) if f.unit_logical_key else None
        conn.execute(
            """
            INSERT INTO generated_file
                (package_id, unit_id, filename, purpose, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            [package_id, unit_id_for_file, f.filename, f.purpose, f.content],
        )

    return package_id


# ---------------------------------------------------------------------------
# Generator — wires components together
# ---------------------------------------------------------------------------


@dataclass
class Generator:
    """One concrete generator type, configured with pluggable components.

    A generator's flow:
      1. ``decomposer.decompose(query)`` — structure the request.
      2. ``planner.plan(decomposition)`` — render to a ``GenPlan``.
         Either fully-rendered (deterministic generators like
         Concept Map) or skeleton + sub-agent prompts (Skills).
      3. ``validator.validate(plan)`` — check structural correctness.
      4. ``persist(plan)`` — write to ``generated_*`` tables.
      5. ``materializer.materialize(package_id, output_root)`` — write
         artifacts to disk.

    Generators that need sub-agent prose (Skills) split steps 2/3/4
    around a sub-agent dispatch fence; the framework supports both.
    """

    generator_type: str  # 'concept_map', 'skills', 'learning_path', …
    decomposer: Decomposer
    planner: Planner
    ranking_mode: RankingMode  # 'generation' | 'interactive'
    validator: Validator
    materializer: Materializer

    def run_deterministic(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        package_name: Optional[str] = None,
        output_root: str = "data/generated-packages",
        overwrite: bool = True,
        **kwargs: Any,
    ) -> tuple[int, MaterializeReport, list[ValidationIssue]]:
        """Run a fully-deterministic generator end-to-end.

        Suitable for generators where the ``Planner`` produces the
        complete output without sub-agent intervention (Concept Map,
        Currency Report). For sub-agent-driven generators (Skills),
        callers split the flow at the prompt-emit fence.
        """
        decomposition = self.decomposer.decompose(
            conn, resolver, query, **kwargs,
        )
        plan = self.planner.plan(
            conn, decomposition, package_name=package_name, **kwargs,
        )
        issues = self.validator.validate(conn, plan)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            return -1, MaterializeReport(
                package_id=-1, package_name=plan.package_name,
                output_root=output_root,
                notes=[f"validation failed: {len(errors)} error(s)"],
            ), issues
        package_id = persist(conn, plan)
        report = self.materializer.materialize(
            conn, package_id, output_root, overwrite=overwrite,
        )
        report.notes.extend(plan.notes)
        return package_id, report, issues
