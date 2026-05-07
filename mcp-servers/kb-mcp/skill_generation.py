"""skill_generation.py — Phase 5.3 Skills Factory: per-Skill generation.

Given a ``PackagePlan`` from Phase 5.2, generate one Skill per
``PlannedSkill`` via the same prep/process pattern used by
``refresh_docs`` extraction. The prep stage:

  1. Builds a retrieval query from the Skill's anchor + concept names
  2. Calls ``search_chapters(mode='generation', selection_strategy=...)``
     so the §8.3 strategy gets applied (recent_doc_anchored drops
     chapters contradicted by current docs; consensus_synthesis filters
     to corroborated material; authority_pick keeps top-1).
  3. Conflicts per §8.4 are resolved by the strategy itself —
     ``recent_doc_anchored`` already drops CONTRADICTS-flagged chapters.
  4. Writes a sub-agent prompt that includes selected source material,
     the package's sibling Skills (for discrimination), and the output
     contract (JSON with trigger_description + skill_md).

Sub-agents (Task tool) read each prompt and write
``result_skill_<cluster_id>.json``. The process stage validates and
ingests into ``skill_package`` / ``skill`` / ``skill_source`` /
``skill_relation`` tables, with full §8.6 provenance — every selected
AND dropped source gets a ``skill_source`` row.

Cost model: this module never calls the Anthropic API. All LLM
content generation runs in Claude Code sub-agents.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import duckdb

LOG = logging.getLogger("mypub-skill-generation")


# Tunables. Phase 5.5 eval should validate against generated-Skill
# quality across multiple domains.
DEFAULT_RETRIEVAL_LIMIT = 12         # top-N candidates per Skill
DEFAULT_EXCERPT_CHARS = 800          # per-source excerpt in prompt
DEFAULT_MAX_SIBLINGS_LISTED = 12     # cap sibling summary list


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SkillSourceRecord:
    """One source candidate (selected or dropped) attached to a Skill."""

    source_type: str       # 'chapter' or 'doc_section'
    source_id: int
    score: float           # combined score from generation ranking
    weight: float = 0.0    # caller-assigned weight in synthesis
    drop_reason: Optional[str] = None   # None ⇒ selected


@dataclass
class SkillManifestEntry:
    """One Skill's prep manifest entry. Ready for sub-agent dispatch."""

    cluster_id: int
    skill_name: str                        # placeholder; LLM may rewrite
    anchor_concept_id: Optional[int]
    anchor_concept_name: Optional[str]
    concept_ids: list[int] = field(default_factory=list)
    strategy: str = ""
    strategy_rationale: str = ""
    folder_name: str = ""
    requires_cluster_ids: list[int] = field(default_factory=list)
    references_cluster_ids: list[int] = field(default_factory=list)
    selected_sources: list[SkillSourceRecord] = field(default_factory=list)
    dropped_sources: list[SkillSourceRecord] = field(default_factory=list)
    prompt_path: str = ""
    result_path: str = ""

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items()
               if k not in ("selected_sources", "dropped_sources")},
            "selected_sources": [asdict(s) for s in self.selected_sources],
            "dropped_sources": [asdict(s) for s in self.dropped_sources],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SkillManifestEntry":
        sel = [SkillSourceRecord(**s) for s in d.get("selected_sources", [])]
        drp = [SkillSourceRecord(**s) for s in d.get("dropped_sources", [])]
        kwargs = {k: v for k, v in d.items()
                  if k not in ("selected_sources", "dropped_sources")}
        kwargs["selected_sources"] = sel
        kwargs["dropped_sources"] = drp
        return cls(**kwargs)


@dataclass
class SkillGenerationManifest:
    """Top-level manifest for one package's skill-generation prep pass."""

    output_dir: str
    package_name: str
    domain: str
    folder_root: str
    created_at: str
    sibling_summaries: list[dict] = field(default_factory=list)
    skills: list[SkillManifestEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "output_dir": self.output_dir,
            "package_name": self.package_name,
            "domain": self.domain,
            "folder_root": self.folder_root,
            "created_at": self.created_at,
            "sibling_summaries": list(self.sibling_summaries),
            "skills": [s.to_dict() for s in self.skills],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SkillGenerationManifest":
        return cls(
            output_dir=d["output_dir"],
            package_name=d["package_name"],
            domain=d["domain"],
            folder_root=d["folder_root"],
            created_at=d["created_at"],
            sibling_summaries=list(d.get("sibling_summaries", [])),
            skills=[SkillManifestEntry.from_dict(s) for s in d.get("skills", [])],
        )


# ---------------------------------------------------------------------------
# Retrieval query construction
# ---------------------------------------------------------------------------


def build_retrieval_query(
    conn: duckdb.DuckDBPyConnection,
    anchor_name: Optional[str],
    concept_ids: Sequence[int],
    *,
    max_supplementary: int = 3,
) -> str:
    """Compose a free-text retrieval query for one Skill.

    The anchor concept name is the primary signal. We add up to
    ``max_supplementary`` additional concept names from the cluster
    that have the most ``concept_relation`` edges (i.e., the
    most-central non-anchor concepts). Empty cluster ⇒ anchor alone.
    """
    if not anchor_name and not concept_ids:
        return ""
    cids = [int(c) for c in concept_ids]
    if not cids:
        return anchor_name or ""

    # Pull the names of all concepts in cluster, ordered by mention count.
    placeholders = ",".join(["?"] * len(cids))
    rows = conn.execute(
        f"""
        WITH mentions AS (
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
        SELECT cn.name, COALESCE(tm.total_m, 0) AS total_m
          FROM concept cn
          LEFT JOIN total_mentions tm ON cn.concept_id = tm.c
         WHERE cn.concept_id IN ({placeholders})
         ORDER BY total_m DESC, cn.name
        """,
        [*cids, *cids, *cids],
    ).fetchall()

    parts: list[str] = []
    if anchor_name:
        parts.append(anchor_name)
    seen = {anchor_name.lower()} if anchor_name else set()
    for name, _ in rows:
        if not name:
            continue
        if name.lower() in seen:
            continue
        parts.append(name)
        seen.add(name.lower())
        if len(parts) - (1 if anchor_name else 0) >= max_supplementary:
            break
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Sibling summary (for discrimination prompts)
# ---------------------------------------------------------------------------


def build_sibling_summaries(plan: Any) -> list[dict]:
    """Extract a compact list of sibling Skills for discrimination context.

    Each entry: ``{cluster_id, name, anchor, strategy, folder_name}``.
    Used in the prompt so the sub-agent can write a trigger description
    that doesn't overlap with sibling Skills.
    """
    out: list[dict] = []
    for ps in plan.planned_skills:
        out.append({
            "cluster_id": ps.proposed.cluster_id,
            "name": ps.proposed.suggested_name or ps.proposed.anchor_concept_name,
            "anchor": ps.proposed.anchor_concept_name,
            "strategy": ps.strategy,
            "folder_name": ps.folder_name,
        })
    return out


# ---------------------------------------------------------------------------
# Step 4: prompt construction
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You generate one Claude Skill in a multi-Skill package.

A "Skill" is a self-contained Markdown document (SKILL.md) that Claude
can load on demand to gain expertise on a focused topic. Every Skill
also has a one-line trigger description used to route queries to it.

Generation rules:

  * Be confident and actionable. NO hedging ("might", "could", "is
    typically"). State things as the source material states them.
  * Cite source material faithfully — if the source says X, the Skill
    says X. If sources disagree, present the most-corroborated view
    or call out the disagreement explicitly.
  * The trigger description must DISCRIMINATE from siblings — describe
    when THIS Skill should fire, not when adjacent ones should.
  * Mention sibling Skills when relevant ("for the read-side, see the
    Read Model Skill") instead of duplicating their coverage.
  * Length: SKILL.md should be 300-1500 words for most topics. Aim
    for actionable density, not encyclopedic breadth.
  * NO YAML frontmatter — that's added by the materialization step.

Strategy guidance (you've been given one of three §8.3 strategies):

  recent_doc_anchored — fresh vendor docs are the source of truth.
    Treat the doc_section excerpts as authoritative for current API
    surface. Book chapters provide context but defer to docs when
    they disagree.
  consensus_synthesis — multiple authors agree. Synthesize the
    consensus view; don't privilege any single source.
  authority_pick — one canonical source dominates. Lean on it as the
    primary voice; secondary sources are color or counterpoint only.

Output JSON only. Schema:

{
  "trigger_description": "1-2 sentences, present-tense. Describes
                          when Claude should invoke this Skill.",
  "skill_md": "Full SKILL.md body in Markdown. No frontmatter."
}
"""


def _format_source_excerpts(
    sources: list[SkillSourceRecord],
    *, conn: duckdb.DuckDBPyConnection,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> str:
    """Format selected sources into a readable bundle for the prompt.

    Pulls book/chapter/section metadata + content excerpts via the
    conn. Each block is delimited by ``--- source N ---`` so the
    sub-agent can cite by index.
    """
    if not sources:
        return "(no source material — this Skill has no candidates)"
    blocks: list[str] = []
    for i, src in enumerate(sources, start=1):
        if src.source_type == "chapter":
            row = conn.execute(
                """
                SELECT b.title, c.title, substring(c.content, 1, ?)
                  FROM chapter c JOIN book b ON c.book_id = b.book_id
                 WHERE c.chapter_id = ?
                """,
                [excerpt_chars, src.source_id],
            ).fetchone()
            if not row:
                continue
            blocks.append(
                f"--- source {i} (chapter, score={src.score:.3f}) ---\n"
                f"BOOK: {row[0]}\n"
                f"CHAPTER: {row[1]}\n\n"
                f"{row[2]}\n"
            )
        elif src.source_type == "doc_section":
            row = conn.execute(
                """
                SELECT ds.name, s.heading_text, substring(s.content, 1, ?)
                  FROM doc_section s
                  JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
                  JOIN doc_source   ds ON sn.doc_source_id = ds.doc_source_id
                 WHERE s.doc_section_id = ?
                """,
                [excerpt_chars, src.source_id],
            ).fetchone()
            if not row:
                continue
            blocks.append(
                f"--- source {i} (doc_section, score={src.score:.3f}) ---\n"
                f"DOC SOURCE: {row[0]}\n"
                f"HEADING: {row[1] or '(no heading)'}\n\n"
                f"{row[2]}\n"
            )
    return "\n".join(blocks) if blocks else "(no source material rendered)"


def _format_sibling_summary(
    siblings: list[dict], current_cluster_id: int,
    *, max_listed: int = DEFAULT_MAX_SIBLINGS_LISTED,
) -> str:
    """Compact list of sibling Skills for discrimination."""
    others = [s for s in siblings if s["cluster_id"] != current_cluster_id]
    if not others:
        return "(this is the only Skill in the package)"
    listed = others[:max_listed]
    lines = [
        f"  - {s['folder_name']} (anchor: {s['anchor']!r}, strategy: {s['strategy']})"
        for s in listed
    ]
    out = "\n".join(lines)
    if len(others) > max_listed:
        out += f"\n  ... and {len(others) - max_listed} more"
    return out


def build_skill_prompt(
    *,
    package_name: str,
    domain: str,
    entry: SkillManifestEntry,
    siblings: list[dict],
    conn: duckdb.DuckDBPyConnection,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> str:
    """Construct the full sub-agent prompt for one Skill.

    Includes: system instructions, package context, sibling list,
    strategy + rationale, source excerpts (selected only — dropped
    are recorded for §8.6 provenance but not shown to the sub-agent),
    and the output schema.
    """
    sibling_str = _format_sibling_summary(siblings, entry.cluster_id)
    source_str = _format_source_excerpts(
        entry.selected_sources, conn=conn, excerpt_chars=excerpt_chars,
    )
    return (
        f"{SYSTEM_PROMPT}\n"
        f"--- PACKAGE CONTEXT ---\n"
        f"PACKAGE: {package_name}\n"
        f"DOMAIN: {domain}\n\n"
        f"--- THIS SKILL ---\n"
        f"NAME: {entry.skill_name}\n"
        f"ANCHOR CONCEPT: {entry.anchor_concept_name or '(none)'}\n"
        f"FOLDER: {entry.folder_name}\n"
        f"STRATEGY: {entry.strategy}\n"
        f"STRATEGY RATIONALE: {entry.strategy_rationale}\n\n"
        f"--- SIBLING SKILLS (discriminate against these) ---\n"
        f"{sibling_str}\n\n"
        f"--- SELECTED SOURCE MATERIAL ---\n"
        f"{source_str}\n\n"
        f"Respond with JSON only. No prose, no markdown fences."
    )


# ---------------------------------------------------------------------------
# Prep stage: build manifest + write prompt files
# ---------------------------------------------------------------------------


SearchFn = Callable[..., dict[str, Any]]


def _scored_to_source_record(
    scored: dict[str, Any], *, drop_reason: Optional[str] = None,
) -> Optional[SkillSourceRecord]:
    """Convert a generation-mode result row to a SkillSourceRecord."""
    kind = scored.get("kind")
    if kind not in ("chapter", "doc_section"):
        return None
    rid = scored.get("result_id")
    if rid is None:
        return None
    return SkillSourceRecord(
        source_type=kind,
        source_id=int(rid),
        score=float(scored.get("combined_score") or 0.0),
        drop_reason=drop_reason,
    )


def prep_skill_generation(
    plan: Any,
    conn: duckdb.DuckDBPyConnection,
    output_dir: Path,
    *,
    search_fn: SearchFn,
    retrieval_limit: int = DEFAULT_RETRIEVAL_LIMIT,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> SkillGenerationManifest:
    """For each PlannedSkill, retrieve+rank candidates and write a sub-agent prompt.

    ``search_fn`` is a callable matching ``server.search_chapters``
    signature: ``(query, mode, limit, weight_profile, auto_discover,
    selection_strategy) → response_dict``. Tests inject a stub; the
    CLI driver passes the real ``server.search_chapters``.

    Skills with strategy ``recent_doc_anchored`` use the
    ``skill_recent_doc`` weight profile, ``consensus_synthesis`` uses
    ``skill_consensus``, and ``authority_pick`` uses ``skill_authority``.
    """
    # Always resolve to absolute. Sub-agents dispatched from another
    # session don't share our CWD assumptions; relative paths in the
    # manifest would break read/write across processes.
    output_dir = Path(output_dir).resolve()
    (output_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (output_dir / "results").mkdir(parents=True, exist_ok=True)

    siblings = build_sibling_summaries(plan)
    manifest = SkillGenerationManifest(
        output_dir=str(output_dir),
        package_name=plan.package_name,
        domain=plan.domain,
        folder_root=plan.folder_root,
        created_at=datetime.now(timezone.utc).isoformat(),
        sibling_summaries=siblings,
    )

    strategy_to_profile = {
        "recent_doc_anchored": "skill_recent_doc",
        "consensus_synthesis": "skill_consensus",
        "authority_pick":      "skill_authority",
    }

    for ps in plan.planned_skills:
        anchor = ps.proposed.anchor_concept_name
        query = build_retrieval_query(
            conn, anchor, ps.proposed.concept_ids, max_supplementary=3,
        ) or (anchor or f"cluster {ps.proposed.cluster_id}")

        # Retrieve candidates with the chosen strategy.
        weight_profile = strategy_to_profile.get(ps.strategy, "skill_consensus")
        try:
            resp = search_fn(
                query=query, mode="generation", limit=retrieval_limit,
                weight_profile=weight_profile,
                selection_strategy=ps.strategy,
                auto_discover=False,
            )
        except Exception as e:  # pragma: no cover - retrieval errors surface cleanly
            LOG.warning(
                "skill_generation: retrieval failed for cluster %d: %s",
                ps.proposed.cluster_id, e,
            )
            resp = {"results": [], "dropped": []}

        selected: list[SkillSourceRecord] = []
        for r in resp.get("results", []):
            rec = _scored_to_source_record(r)
            if rec is not None:
                selected.append(rec)

        dropped: list[SkillSourceRecord] = []
        for r in resp.get("dropped", []):
            rec = _scored_to_source_record(
                r, drop_reason=r.get("drop_reason") or "dropped by strategy",
            )
            if rec is not None:
                dropped.append(rec)

        prompt_path = output_dir / "prompts" / f"prompt_skill_{ps.proposed.cluster_id}.txt"
        result_path = output_dir / "results" / f"result_skill_{ps.proposed.cluster_id}.json"
        entry = SkillManifestEntry(
            cluster_id=ps.proposed.cluster_id,
            skill_name=(ps.proposed.suggested_name
                        or ps.proposed.anchor_concept_name
                        or f"skill-{ps.proposed.cluster_id}"),
            anchor_concept_id=ps.proposed.anchor_concept_id,
            anchor_concept_name=ps.proposed.anchor_concept_name,
            concept_ids=list(ps.proposed.concept_ids),
            strategy=ps.strategy,
            strategy_rationale=ps.strategy_rationale,
            folder_name=ps.folder_name,
            requires_cluster_ids=list(ps.requires_cluster_ids),
            references_cluster_ids=list(ps.references_cluster_ids),
            selected_sources=selected,
            dropped_sources=dropped,
            prompt_path=str(prompt_path),
            result_path=str(result_path),
        )

        prompt_text = build_skill_prompt(
            package_name=plan.package_name, domain=plan.domain,
            entry=entry, siblings=siblings, conn=conn,
            excerpt_chars=excerpt_chars,
        )
        prompt_path.write_text(prompt_text)
        manifest.skills.append(entry)

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2),
    )
    LOG.info(
        "prep_skill_generation: wrote %d skill prompts under %s",
        len(manifest.skills), output_dir,
    )
    return manifest


# ---------------------------------------------------------------------------
# Process stage: ingest sub-agent JSON results into skill tables
# ---------------------------------------------------------------------------


@dataclass
class SkillIngestSummary:
    """Aggregate counts emitted by ``process_skill_generation``."""

    total: int = 0
    processed: int = 0
    missing: int = 0
    unparseable: int = 0
    package_id: Optional[int] = None
    skill_ids: list[int] = field(default_factory=list)


_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    """Strip a single ```json ... ``` fence if the sub-agent wrapped its
    output in one. Sub-agents are instructed not to fence, but real
    models do it occasionally regardless. Robust over the strict path.

    Returns ``text`` unchanged if no fence pattern matches.
    """
    s = text.strip()
    m = _FENCE_RE.match(s)
    return m.group(1).strip() if m else s


def _parse_skill_result(text: str) -> Any:
    """Parse a sub-agent result file's text into a JSON object.

    1. Strip optional markdown fences.
    2. ``json.loads`` the result.
    3. If that fails, try to locate the first ``{ … }`` block and parse
       only that (handles the case where a sub-agent prepended a stray
       sentence before the JSON despite the prompt's instructions).
    Raises the original ``json.JSONDecodeError`` if all paths fail.
    """
    cleaned = _strip_json_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first != -1 and last > first:
            return json.loads(cleaned[first : last + 1])
        raise


def _validate_skill_payload(raw: Any) -> Optional[dict]:
    """Return a normalized payload or None on validation failure."""
    if not isinstance(raw, dict):
        return None
    trigger = (raw.get("trigger_description") or "").strip()
    body = (raw.get("skill_md") or "").strip()
    if not trigger or not body:
        return None
    return {"trigger_description": trigger, "skill_md": body}


def _upsert_package(
    conn: duckdb.DuckDBPyConnection, manifest: SkillGenerationManifest,
) -> int:
    """Insert a fresh skill_package row, or return the existing id by name."""
    existing = conn.execute(
        "SELECT package_id FROM skill_package WHERE name = ?",
        [manifest.package_name],
    ).fetchone()
    if existing:
        return int(existing[0])
    row = conn.execute(
        """
        INSERT INTO skill_package (name, domain, root_topic, source_query)
        VALUES (?, ?, ?, ?)
        RETURNING package_id
        """,
        [manifest.package_name, manifest.domain, manifest.domain, manifest.domain],
    ).fetchone()
    return int(row[0])


def _clear_prior_skills(
    conn: duckdb.DuckDBPyConnection, package_id: int,
) -> None:
    """Idempotent re-run: drop prior skill rows + their dependencies."""
    skill_ids = [r[0] for r in conn.execute(
        "SELECT skill_id FROM skill WHERE package_id = ?", [package_id],
    ).fetchall()]
    if not skill_ids:
        return
    placeholders = ",".join(["?"] * len(skill_ids))
    conn.execute(
        f"DELETE FROM skill_relation WHERE from_skill_id IN ({placeholders}) "
        f"OR to_skill_id IN ({placeholders})",
        [*skill_ids, *skill_ids],
    )
    conn.execute(
        f"DELETE FROM skill_source WHERE skill_id IN ({placeholders})",
        skill_ids,
    )
    conn.execute(
        f"DELETE FROM skill_file WHERE skill_id IN ({placeholders})",
        skill_ids,
    )
    conn.execute(
        f"DELETE FROM skill WHERE skill_id IN ({placeholders})",
        skill_ids,
    )


def process_skill_generation(
    conn: duckdb.DuckDBPyConnection, output_dir: Path,
) -> SkillIngestSummary:
    """Read sub-agent result JSONs and persist into the skill_* tables.

    For each manifest entry:
      1. Read ``result_path``. Missing → counted, skip.
      2. Validate payload shape. Unparseable → counted, skip.
      3. Insert/upsert ``skill_package``.
      4. Insert ``skill`` row with content_markdown + description.
      5. Insert ``skill_source`` rows for every selected and dropped
         candidate (full §8.6 provenance).
      6. After all skills are inserted, fill in ``skill_relation`` rows
         for the cross-references and prerequisites recorded in the
         manifest (using the cluster_id → skill_id map).

    Idempotent: re-running clears prior skills + their dependencies
    for the package and re-ingests fresh.
    """
    output_dir = Path(output_dir)
    manifest = SkillGenerationManifest.from_dict(
        json.loads((output_dir / "manifest.json").read_text())
    )
    summary = SkillIngestSummary(total=len(manifest.skills))

    package_id = _upsert_package(conn, manifest)
    summary.package_id = package_id
    _clear_prior_skills(conn, package_id)

    cluster_to_skill: dict[int, int] = {}
    for entry in manifest.skills:
        result_path = Path(entry.result_path)
        if not result_path.exists():
            summary.missing += 1
            LOG.info("skill cluster %d: no result file", entry.cluster_id)
            continue
        try:
            raw = _parse_skill_result(result_path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            summary.unparseable += 1
            LOG.warning("skill cluster %d: parse error: %s",
                        entry.cluster_id, e)
            continue
        payload = _validate_skill_payload(raw)
        if payload is None:
            summary.unparseable += 1
            LOG.warning("skill cluster %d: missing trigger/body",
                        entry.cluster_id)
            continue

        # Insert the skill row.
        skill_id = conn.execute(
            """
            INSERT INTO skill (package_id, name, description, scope_summary,
                               content_markdown, source_currency, strategy,
                               generation_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING skill_id
            """,
            [
                package_id, entry.skill_name,
                payload["trigger_description"],
                f"anchor: {entry.anchor_concept_name or '(none)'}",
                payload["skill_md"],
                _strategy_to_currency(entry.strategy),
                entry.strategy,
                entry.strategy_rationale,
            ],
        ).fetchone()[0]
        cluster_to_skill[entry.cluster_id] = int(skill_id)
        summary.skill_ids.append(int(skill_id))

        # Provenance: every selected and dropped source.
        for src in entry.selected_sources:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_source
                    (skill_id, source_type, source_id, score, weight, drop_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [skill_id, src.source_type, src.source_id, src.score,
                 src.weight, None],
            )
        for src in entry.dropped_sources:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_source
                    (skill_id, source_type, source_id, score, weight, drop_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [skill_id, src.source_type, src.source_id, src.score,
                 0.0, src.drop_reason or "dropped by strategy"],
            )
        summary.processed += 1

    # Second pass: skill_relation links (prerequisites + references) using
    # the cluster_id → skill_id map. We can only link skills that
    # successfully ingested.
    for entry in manifest.skills:
        from_id = cluster_to_skill.get(entry.cluster_id)
        if from_id is None:
            continue
        for cl in entry.requires_cluster_ids:
            to_id = cluster_to_skill.get(int(cl))
            if to_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO skill_relation "
                "(from_skill_id, to_skill_id, relation_type) VALUES (?, ?, 'REQUIRES')",
                [from_id, to_id],
            )
        for cl in entry.references_cluster_ids:
            to_id = cluster_to_skill.get(int(cl))
            if to_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO skill_relation "
                "(from_skill_id, to_skill_id, relation_type) VALUES (?, ?, 'REFERENCES')",
                [from_id, to_id],
            )

    LOG.info(
        "process_skill_generation: package %d | %d/%d processed "
        "(%d missing, %d unparseable)",
        package_id, summary.processed, summary.total,
        summary.missing, summary.unparseable,
    )
    return summary


def _strategy_to_currency(strategy: str) -> str:
    """Map a §8.3 strategy to the ``skill.source_currency`` enum.

    The schema column is free-form text, but we use a stable convention:
    'current'   for recent_doc_anchored (fresh vendor docs)
    'consensus' for consensus_synthesis
    'canonical' for authority_pick
    """
    return {
        "recent_doc_anchored": "current",
        "consensus_synthesis": "consensus",
        "authority_pick":      "canonical",
    }.get(strategy, "consensus")
