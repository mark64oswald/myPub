"""
kb_scaffold.py — shared pieces for the scaffolding generators (2026-11).

Used by agent_skill_gen (``/kb-skill-from-kb``), mcp_server_gen
(``/kb-mcp-server``) and plugin_scaffold (``/kb-plugin-scaffold``):

* :func:`doc_citations` — pick sections of named live-doc sources (the MCP
  spec, Agent Skills spec, Claude Code plugin docs seeded by
  scripts/seed_agent_standards.py) so every scaffold cites the standard it
  follows.
* :func:`topic_material` — what the KB knows about one concept: its
  description, related concepts, procedures and the books / docs they came
  from. Feeds a skill's body and ``references/``.
* :class:`TreeMaterializer` — writes ``generated_file`` rows whose
  filenames are relative paths (``skills/x/SKILL.md``), making parent
  folders and marking scripts executable.
"""

from __future__ import annotations

import keyword
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import duckdb

from generator import MaterializeReport

# Live-doc sources each scaffold cites, by doc_source.name (see
# scripts/seed_agent_standards.py). Missing names are skipped, so a catalog
# without these sources still generates, just with fewer citations.
SKILL_SPEC_SOURCES = (
    "Agent Skills specification",
    "Agent Skills guide: best practices",
    "Agent Skills guide: optimizing descriptions",
    "Claude Code: skills",
)
MCP_SPEC_SOURCES = (
    "MCP spec 2026-07-28: tools",
    "MCP spec 2026-07-28: resources",
    "MCP spec 2026-07-28: transports",
    "MCP spec 2026-07-28: Streamable HTTP transport",
    "MCP spec 2026-07-28: authorization",
    "MCP guide: security best practices",
    "MCP guide: build a server",
)
MCP_SDK_SOURCES = {
    "python": ("MCP Python SDK",),
    "typescript": ("MCP TypeScript SDK",),
}
MCP_APPS_SOURCES = (
    "MCP Apps spec (SEP-1865, 2026-01-26)",
    "MCP Apps guide: quickstart",
    "MCP Apps guide: CSP and CORS",
    "MCP Apps SDK (ext-apps)",
    "Claude MCP Apps: design guidelines",
)
MCP_SKILLS_EXT_SOURCES = (
    "MCP Skills extension spec (beta)",
    "MCP Skills extension overview",
)
MCP_REGISTRY_SOURCES = ("MCP Registry server.json reference", "MCP Registry quickstart")
PLUGIN_SOURCES = (
    "Claude Code: plugins reference",
    "Claude Code: plugins",
    "Claude Code: plugin marketplaces",
    "Claude Code: hooks",
    "Claude Code: MCP",
)


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    source_type: str          # 'doc_section' | 'chapter' | 'procedure'
    source_id: int
    label: str                # "MCP spec 2026-07-28: tools — Tool Result"
    url: Optional[str] = None

    def markdown(self) -> str:
        return f"[{self.label}]({self.url})" if self.url else self.label


def _kw_score(text: str, keywords: Sequence[str]) -> int:
    t = (text or "").lower()
    return sum(t.count(k.lower()) for k in keywords)


def doc_citations(
    conn: duckdb.DuckDBPyConnection,
    source_names: Iterable[str],
    *,
    keywords: Sequence[str] = (),
    per_source: int = 2,
) -> list[Citation]:
    """Pick up to ``per_source`` sections from each named doc_source's latest snapshot.

    With ``keywords``, sections are ranked by keyword hits (heading hits count
    triple); otherwise the first sections in document order win.
    """
    out: list[Citation] = []
    for name in source_names:
        row = conn.execute(
            """
            SELECT sn.snapshot_id, sn.url
              FROM doc_source ds JOIN doc_snapshot sn ON sn.doc_source_id = ds.doc_source_id
             WHERE ds.name = ?
             ORDER BY sn.retrieved_at DESC, sn.snapshot_id DESC
             LIMIT 1
            """,
            [name],
        ).fetchone()
        if not row:
            continue
        snapshot_id, url = row
        sections = conn.execute(
            """
            SELECT doc_section_id, heading_text, content, ordinal
              FROM doc_section WHERE snapshot_id = ?
             ORDER BY ordinal, doc_section_id
            """,
            [snapshot_id],
        ).fetchall()
        if keywords:
            ranked = sorted(
                sections,
                key=lambda s: (-(3 * _kw_score(s[1] or "", keywords)
                                 + _kw_score(s[2] or "", keywords)), s[3] or 0, s[0]),
            )
        else:
            ranked = [s for s in sections if (s[2] or "").strip()] or sections
        for sid, heading, _content, _ord in ranked[:per_source]:
            label = f"{name} — {heading}" if heading else name
            out.append(Citation("doc_section", int(sid), label, url))
    return out


def render_citations(citations: Sequence[Citation], title: str = "Sources") -> str:
    if not citations:
        return f"## {title}\n\n_No matching live-doc sources in this catalog. " \
               "Run scripts/seed_agent_standards.py and refresh to add them._\n"
    lines = [f"## {title}", ""]
    seen: set[tuple[str, int]] = set()
    for c in citations:
        if (c.source_type, c.source_id) in seen:
            continue
        seen.add((c.source_type, c.source_id))
        lines.append(f"- {c.markdown()} (`{c.source_type}:{c.source_id}`)")
    return "\n".join(lines) + "\n"


def source_label(conn: duckdb.DuckDBPyConnection, source_type: Optional[str],
                 source_id: Optional[int]) -> Optional[str]:
    if not source_type or source_id is None:
        return None
    if source_type == "chapter":
        row = conn.execute(
            "SELECT b.title, c.title FROM chapter c JOIN book b ON c.book_id = b.book_id "
            "WHERE c.chapter_id = ?", [source_id],
        ).fetchone()
        return f"{row[0]} — {row[1]}" if row else None
    if source_type == "doc_section":
        row = conn.execute(
            """
            SELECT ds.name, s.heading_text
              FROM doc_section s
              JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
              JOIN doc_source ds   ON sn.doc_source_id = ds.doc_source_id
             WHERE s.doc_section_id = ?
            """, [source_id],
        ).fetchone()
        return f"{row[0]} — {row[1] or '(no heading)'}" if row else None
    return None


# ---------------------------------------------------------------------------
# Topic material for a skill
# ---------------------------------------------------------------------------


@dataclass
class Procedure:
    procedure_id: int
    name: str
    preconditions: Optional[str]
    steps: str
    failure_modes: Optional[str]
    source: Optional[Citation]


@dataclass
class TopicMaterial:
    concept_id: int
    name: str
    concept_type: Optional[str]
    description: Optional[str]
    related: list[tuple[str, str, float]] = field(default_factory=list)  # (relation, name, conf)
    procedures: list[Procedure] = field(default_factory=list)
    sources: list[Citation] = field(default_factory=list)


_STOP = {"a", "an", "and", "the", "of", "to", "for", "in", "on", "with", "my", "our", "your",
         "from", "by", "it", "is", "this", "that", "using", "use", "how"}
# Housekeeping procedures that say little about the topic itself.
_TRIVIAL = {"version", "clipboard", "copy", "display", "view", "open", "close", "uninstall",
            "upgrade", "update", "changelog", "license", "contribute", "contributing"}


def tokens(text: Optional[str]) -> set[str]:
    """Content words, lower-cased and crudely singularised."""
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w
            for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 2}


def topic_tokens(name: str) -> set[str]:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    out = tokens(name)
    if len(words) >= 2:
        out.add("".join(w[0] for w in words).lower())  # "Model Context Protocol" -> "mcp"
    return out


def rank_procedures(rows: Sequence[tuple], topic: str, *, purpose: Optional[str] = None,
                    limit: int = 8, per_source: int = 2) -> list[tuple]:
    """Order procedure rows (id, name, pre, steps, failure, source_type, source_id) by how
    much they say about ``topic`` (and ``purpose``), with at most ``per_source`` from any
    one chapter or doc section so a single page can't fill the list."""
    want = topic_tokens(topic) | tokens(purpose)

    def score(r: tuple) -> float:
        name_t, steps = tokens(r[1]), (r[3] or "")
        s = 3.0 * len(want & name_t) + 1.0 * min(len(want & tokens(steps[:800])), 3)
        s += min(len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s", steps)), 6) * 0.5
        s -= 4.0 * len(_TRIVIAL & name_t)
        s += 0.5 if r[5] == "doc_section" else 0.0
        return s

    ranked = sorted(rows, key=lambda r: (-score(r), r[0]))
    out: list[tuple] = []
    per: Counter = Counter()
    for r in ranked:
        key = (r[5], r[6])
        if per[key] >= per_source:
            continue
        per[key] += 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def topic_material(
    conn: duckdb.DuckDBPyConnection,
    resolver: Any,
    topic: str,
    *,
    max_procedures: int = 8,
    max_related: int = 12,
    max_sources: int = 10,
    purpose: Optional[str] = None,
) -> Optional[TopicMaterial]:
    """Gather what the KB knows about ``topic``; None when it doesn't resolve."""
    cid = resolver.resolve_lookup_only(topic)
    if cid is None:
        return None
    row = conn.execute(
        "SELECT name, concept_type, description FROM concept WHERE concept_id = ?", [cid],
    ).fetchone()
    if not row:
        return None
    tm = TopicMaterial(concept_id=int(cid), name=row[0], concept_type=row[1], description=row[2])
    # Same-name rows split by concept_type (e.g. "Agent Skills" as Concept and as Tool)
    # are one topic here: pool their relations, procedures and sources.
    ids = [int(r[0]) for r in conn.execute(
        "SELECT concept_id FROM concept WHERE lower(name) = lower(?) ORDER BY concept_id",
        [row[0]]).fetchall()] or [int(cid)]
    if not tm.description:
        d = conn.execute(
            "SELECT description FROM concept WHERE lower(name) = lower(?) AND description IS NOT NULL "
            "AND length(trim(description)) > 0 ORDER BY length(description) DESC LIMIT 1",
            [row[0]]).fetchone()
        tm.description = d[0] if d else None
    q = ", ".join("?" * len(ids))

    rel_rows = conn.execute(
        """
        SELECT rel, name, max(conf) AS conf FROM (
            SELECT r.relation_type AS rel, c.name AS name, r.confidence AS conf
              FROM concept_relation r JOIN concept c ON c.concept_id = r.to_concept_id
             WHERE r.from_concept_id IN ({q}) AND r.to_concept_id NOT IN ({q})
            UNION ALL
            SELECT 'IS_REQUIRED_BY', c.name, r.confidence
              FROM concept_relation r JOIN concept c ON c.concept_id = r.from_concept_id
             WHERE r.to_concept_id IN ({q}) AND r.from_concept_id NOT IN ({q}) AND r.relation_type = 'REQUIRES'
            UNION ALL
            SELECT 'SPECIALIZED_BY', c.name, r.confidence
              FROM concept_relation r JOIN concept c ON c.concept_id = r.from_concept_id
             WHERE r.to_concept_id IN ({q}) AND r.from_concept_id NOT IN ({q}) AND r.relation_type = 'EXTENDS'
        ) GROUP BY rel, name ORDER BY conf DESC, name LIMIT ?
        """.format(q=q),
        ids * 6 + [max_related],
    ).fetchall()
    tm.related = [(r[0], r[1], float(r[2] or 0.0)) for r in rel_rows]

    # Procedures on the concept and on concepts that EXTEND it.
    proc_rows = conn.execute(
        """
        SELECT DISTINCT p.procedure_id, p.name, p.preconditions, p.steps,
                        p.failure_modes, p.source_type, p.source_id
          FROM procedure p JOIN procedure_concept pc ON pc.procedure_id = p.procedure_id
         WHERE (pc.concept_id IN ({q}) OR pc.concept_id IN (
                   SELECT from_concept_id FROM concept_relation
                    WHERE to_concept_id IN ({q}) AND relation_type = 'EXTENDS'))
           AND p.steps IS NOT NULL AND length(trim(p.steps)) > 0
        """.format(q=q),
        ids * 2,
    ).fetchall()
    proc_rows = rank_procedures(proc_rows, row[0], purpose=purpose, limit=max_procedures)
    for r in proc_rows:
        label = source_label(conn, r[5], r[6])
        tm.procedures.append(Procedure(
            procedure_id=int(r[0]), name=r[1] or "(unnamed procedure)",
            preconditions=r[2], steps=r[3] or "", failure_modes=r[4],
            source=Citation(r[5], int(r[6]), label) if label else None,
        ))

    # Sources: where the concept's relations and procedures were extracted from.
    counts: Counter[tuple[str, int]] = Counter()
    for st, sid in conn.execute(
        """
        SELECT source_type, source_id FROM concept_relation
         WHERE (from_concept_id IN ({q}) OR to_concept_id IN ({q}))
           AND source_type IN ('chapter', 'doc_section')
        """.format(q=q), ids * 2,
    ).fetchall():
        counts[(st, int(sid))] += 1
    for p in tm.procedures:
        if p.source:
            counts[(p.source.source_type, p.source.source_id)] += 2
    for (st, sid), _n in counts.most_common(max_sources * 2):
        label = source_label(conn, st, sid)
        if label:
            tm.sources.append(Citation(st, sid, label))
        if len(tm.sources) >= max_sources:
            break
    return tm


def first_sentence(text: Optional[str], max_chars: int = 300) -> str:
    t = " ".join((text or "").split())
    if not t:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", t)
    s = m.group(1) if m else t
    return s if len(s) <= max_chars else s[: max_chars - 1].rstrip() + "…"


def identifier(text: str, *, style: str = "snake") -> str:
    """'Search the catalog' → 'search_the_catalog' (snake) or 'search-the-catalog' (kebab)."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    sep = "_" if style == "snake" else "-"
    s = sep.join(words)
    if len(s) > 48:
        s = s[:49]
        s = s[: s.rfind(sep)] if sep in s else s[:48]
    s = s.strip(sep)
    if not s or s[0].isdigit():
        s = f"x{sep}{s}" if s else "item"
    if style == "snake" and keyword.iskeyword(s):
        s += "_"
    return s


# ---------------------------------------------------------------------------
# Materializer for nested trees
# ---------------------------------------------------------------------------


EXECUTABLE_HINTS = ("scripts/", "bin/")
MANIFEST = ".mypub-files"  # files this package wrote last time, for stale-file cleanup


class TreeMaterializer:
    """Write a package's ``generated_file`` rows under ``<output_root>/<package>/``.

    Filenames may contain ``/``. Files under ``scripts/`` or ``bin/`` and
    ``*.sh`` files are made executable. Paths that escape the package folder
    are refused.
    """

    def materialize(self, conn: duckdb.DuckDBPyConnection, package_id: int,
                    output_root: str, *, overwrite: bool = True) -> MaterializeReport:
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?", [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        out_dir = (Path(output_root) / row[0]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        notes: list[str] = []
        manifest = out_dir / MANIFEST
        previous = set(manifest.read_text().splitlines()) if manifest.exists() else set()
        current: set[str] = set()
        for filename, content in conn.execute(
            "SELECT filename, content FROM generated_file WHERE package_id = ? ORDER BY file_id",
            [package_id],
        ).fetchall():
            target = (out_dir / filename).resolve()
            if out_dir not in target.parents:
                notes.append(f"refused path outside package: {filename}")
                continue
            if target.exists() and not overwrite:
                notes.append(f"kept existing {filename}")
                continue
            current.add(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            if filename.endswith(".sh") or any(h in filename for h in EXECUTABLE_HINTS):
                target.chmod(0o755)
            written.append(str(target))
        # Remove files an earlier run of this package wrote that this run didn't (a renamed
        # tool's ui/*.html, a dropped skill). Only paths in our own manifest are touched.
        if overwrite:
            for stale in sorted(previous - current):
                target = (out_dir / stale).resolve()
                if out_dir in target.parents and target.is_file():
                    target.unlink()
                    notes.append(f"removed stale {stale}")
        else:
            current |= previous
        manifest.write_text("\n".join(sorted(current)) + "\n")
        return MaterializeReport(package_id=package_id, package_name=row[0],
                                 output_root=output_root, file_paths=written, notes=notes)
