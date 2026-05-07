"""currency_report.py — Phase 13 Currency Report generator.

Audits doc_snapshot freshness across all (or one) doc_source. Surfaces
volatility signals: how recently each source was refreshed, hash
churn over time. Produces a per-source timeline + a top-level
volatility-ranked report.

Output:
    currency-reports/<scope>/
      _report.md             volatility-ranked subject list
      sources/<slug>.md      per-source timeline
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
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

LOG = logging.getLogger("mypub-currency-report")

GENERATOR_TYPE = "currency_report"
DEFAULT_SCOPE_NAME = "all-sources"


@dataclass
class _SnapshotEvent:
    snapshot_id: int
    retrieved_at: datetime
    content_hash: str
    section_count: int


@dataclass
class _SourceRow:
    doc_source_id: int
    name: str
    source_type: str
    identifier: str
    last_retrieved: Optional[datetime]
    snapshot_count: int
    distinct_hashes: int
    section_count: int
    volatility_score: float            # higher = more volatile
    snapshots: list[_SnapshotEvent] = field(default_factory=list)


@dataclass
class _Decomposition:
    scope_name: str
    sources: list[_SourceRow]
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "scope"


class SnapshotHistoryDecomposer:
    """Walks doc_snapshot rows for one or all sources, computes
    per-source volatility, and assembles the timeline."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        source_filter: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        if source_filter:
            scope_name = source_filter
            sources_q = conn.execute(
                "SELECT doc_source_id, name, source_type, identifier "
                "FROM doc_source WHERE name = ?",
                [source_filter],
            ).fetchall()
        else:
            scope_name = query or DEFAULT_SCOPE_NAME
            sources_q = conn.execute(
                "SELECT doc_source_id, name, source_type, identifier "
                "FROM doc_source ORDER BY name"
            ).fetchall()

        if not sources_q:
            return _Decomposition(
                scope_name=scope_name, sources=[],
                notes=[f"no doc_source rows match {source_filter or '(all)'}"],
            )

        rows: list[_SourceRow] = []
        for ds_id, name, stype, ident in sources_q:
            ds_id = int(ds_id)
            snap_rows = conn.execute(
                """
                SELECT sn.snapshot_id, sn.retrieved_at, sn.content_hash,
                       (SELECT COUNT(*) FROM doc_section s
                          WHERE s.snapshot_id = sn.snapshot_id) AS sec_n
                  FROM doc_snapshot sn
                 WHERE sn.doc_source_id = ?
                 ORDER BY sn.retrieved_at DESC
                """,
                [ds_id],
            ).fetchall()
            snapshots = [
                _SnapshotEvent(
                    snapshot_id=int(r[0]), retrieved_at=r[1],
                    content_hash=r[2], section_count=int(r[3] or 0),
                )
                for r in snap_rows
            ]
            distinct_hashes = len({s.content_hash for s in snapshots})
            section_count = max((s.section_count for s in snapshots), default=0)
            # Volatility = (distinct hashes) × log(snapshot count + 1).
            # Single-snapshot sources have volatility 0; sources with
            # frequent hash flips score higher.
            import math
            volatility = (
                distinct_hashes - 1
            ) * math.log(len(snapshots) + 1) if snapshots else 0.0
            rows.append(_SourceRow(
                doc_source_id=ds_id, name=name, source_type=stype,
                identifier=ident,
                last_retrieved=snapshots[0].retrieved_at if snapshots else None,
                snapshot_count=len(snapshots),
                distinct_hashes=distinct_hashes,
                section_count=section_count,
                volatility_score=float(volatility),
                snapshots=snapshots,
            ))
        rows.sort(key=lambda r: -r.volatility_score)

        notes: list[str] = []
        all_single = all(r.snapshot_count <= 1 for r in rows)
        if all_single:
            notes.append(
                "every source has only one snapshot — volatility cannot be "
                "measured. Currency Report becomes useful after multiple "
                "refresh runs accumulate snapshot history."
            )

        return _Decomposition(scope_name=scope_name, sources=rows, notes=notes)


def _render_report(d: _Decomposition) -> str:
    lines = [f"# Currency Report: {d.scope_name}", ""]
    if not d.sources:
        lines.append("_No doc_source rows in scope._")
        return "\n".join(lines)
    lines.append(f"_Audited {len(d.sources)} doc_source(s)._")
    lines.append("")
    lines.extend([
        "| Source | Type | Snapshots | Hashes | Volatility | Last retrieved |",
        "|---|---|---:|---:|---:|---|",
    ])
    for r in d.sources:
        ts = r.last_retrieved.strftime("%Y-%m-%d") if r.last_retrieved else "—"
        lines.append(
            f"| **{r.name}** | {r.source_type} | {r.snapshot_count} | "
            f"{r.distinct_hashes} | {r.volatility_score:.2f} | {ts} |"
        )
    lines.append("")
    lines.append("_Volatility = (distinct hashes − 1) × log(snapshot_count + 1). "
                 "Higher = more churn between refreshes._")
    if d.notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in d.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


def _render_source(r: _SourceRow) -> str:
    lines = [
        f"# {r.name} — Currency Timeline",
        "",
        f"**Type:** {r.source_type}  |  **Identifier:** `{r.identifier}`",
        "",
        f"**Snapshots:** {r.snapshot_count}  |  **Distinct hashes:** {r.distinct_hashes}  |  "
        f"**Sections (latest):** {r.section_count}  |  "
        f"**Volatility:** {r.volatility_score:.2f}",
        "",
        "## Timeline",
        "",
    ]
    if not r.snapshots:
        lines.append("_No snapshots recorded._")
        return "\n".join(lines)
    prev_hash = None
    for snap in r.snapshots:
        ts = snap.retrieved_at.strftime("%Y-%m-%d %H:%M") if snap.retrieved_at else "—"
        change_marker = ""
        if prev_hash is not None and prev_hash != snap.content_hash:
            change_marker = " 🔀"
        lines.append(
            f"- {ts} — hash `{snap.content_hash[:8]}` "
            f"({snap.section_count} section(s)){change_marker}"
        )
        prev_hash = snap.content_hash
    lines.append("")
    return "\n".join(lines)


class CurrencyReportPlanner:
    def plan(self, conn, decomposition, *, package_name=None, **_) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.scope_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.scope_name,
            source_query=d.scope_name,
            package_metadata={
                "n_sources": len(d.sources),
                "n_snapshots": sum(r.snapshot_count for r in d.sources),
                "max_volatility": max((r.volatility_score for r in d.sources), default=0.0),
            },
            notes=list(d.notes),
        )
        for i, r in enumerate(d.sources, start=1):
            plan.units.append(GenUnit(
                unit_type="currency_source",
                name=r.name,
                ordinal=i,
                metadata={
                    "doc_source_id": r.doc_source_id,
                    "snapshot_count": r.snapshot_count,
                    "distinct_hashes": r.distinct_hashes,
                    "volatility_score": r.volatility_score,
                },
                logical_key=f"source_{r.doc_source_id}",
                content_markdown="",
                sources=[],
            ))
            plan.files.append(GenFile(
                filename=f"sources/{_slugify(r.name)}.md",
                content=_render_source(r),
                purpose="source_timeline",
                unit_logical_key=f"source_{r.doc_source_id}",
            ))
        plan.files.append(GenFile(
            filename="_report.md", content=_render_report(d), purpose="report",
        ))
        return plan


class CurrencyReportValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("n_sources", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no doc_source rows in scope",
            ))
        # FK existence on doc_source_ids
        ids = {int(u.metadata.get("doc_source_id"))
               for u in plan.units if u.metadata.get("doc_source_id") is not None}
        if ids:
            ph = ",".join(["?"] * len(ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT doc_source_id FROM doc_source WHERE doc_source_id IN ({ph})",
                list(ids),
            ).fetchall()}
            missing = ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"doc_source_ids missing: {sorted(missing)[:5]}",
                ))
        return issues


class CurrencyReportMaterializer:
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


def make_currency_report_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=SnapshotHistoryDecomposer(),
        planner=CurrencyReportPlanner(),
        ranking_mode="interactive",
        validator=CurrencyReportValidator(),
        materializer=CurrencyReportMaterializer(),
    )
