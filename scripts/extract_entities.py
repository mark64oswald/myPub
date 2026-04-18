#!/usr/bin/env python3
"""
extract_entities.py — Coordinator for LLM-based concept/relation extraction.

Architecture (Phase 2 cost model): this script is the Python coordinator
that handles DB reads, entity resolution, and DB writes. The LLM reasoning
runs in Claude Code sub-agents via the Task tool, never in this script.
All LLM work stays on the Max subscription — this module imports only
duckdb and the local EntityResolver; no `anthropic` SDK.

The coordinator is callable in two modes, glued together by Claude Code
driving the sub-agents between them:

    --chapter-id N --print-prompt
       Emits the full self-contained extraction prompt (system instructions
       + chapter payload) to stdout or --output PATH. Feed that to a
       sub-agent with instructions to write JSON to a known path.

    --chapter-id N --json-file PATH
       Reads the sub-agent's JSON output, validates it, resolves entities
       via EntityResolver, and writes concept / concept_embedding /
       concept_relation rows. Idempotent per-chapter: re-running a chapter
       clears its prior concept_relation rows and writes the new ones.
       Concept nodes are left in place — they may be referenced by other
       chapters.

The programmatic entry points — build_full_prompt(chapter),
parse_llm_json(text), process_extraction_json(conn, resolver, chapter_id,
raw) — are also exported so a future batch driver can orchestrate many
chapters in one process.

Usage:
    # Step 1: build the prompt for a sub-agent.
    .venv/bin/python3 scripts/extract_entities.py \\
        --chapter-id 12345 --print-prompt --output /tmp/prompt_12345.txt

    # Step 2: (driven by Claude Code) have a sub-agent read that prompt,
    # run the extraction, and write JSON to /tmp/result_12345.json.

    # Step 3: write the extraction to the catalog.
    .venv/bin/python3 scripts/extract_entities.py \\
        --chapter-id 12345 --json-file /tmp/result_12345.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
sys.path.insert(0, str(MCP_DIR))

from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position

MAX_CONTENT_CHARS = 20000       # ~5K input tokens; cap per-call payload size
LOG = logging.getLogger("extract_entities")

ENTITY_TYPES = {"Concept", "Pattern", "Tool", "Framework", "Algorithm", "Technique"}
RELATION_TYPES = {"REQUIRES", "EXTENDS", "CONTRASTS_WITH", "IMPLEMENTS", "CITES"}


SYSTEM_PROMPT = """You extract structured knowledge from technical book chapters for a concept graph. Return JSON only — no prose, no markdown fences.

For the chapter provided, identify:

ENTITIES — the specific concepts, patterns, tools, frameworks, algorithms, or techniques the chapter discusses.

Allowed entity types:
  Concept    — a general idea, theory, or principle (e.g., "eventual consistency")
  Pattern    — a reusable structural solution, usually named (e.g., "Circuit Breaker", "Outbox Pattern")
  Tool       — a named software tool or product (e.g., "Kafka", "PostgreSQL")
  Framework  — a library, SDK, or platform (e.g., "Spring", "React")
  Algorithm  — a named procedure with defined inputs/outputs (e.g., "HyperLogLog", "Raft")
  Technique  — a less-formal method or approach (e.g., "memoization", "code review")

RELATIONS — directed connections the chapter EXPLICITLY makes between those entities.

Allowed relation types:
  REQUIRES        A depends on B being understood first.
  EXTENDS         A builds on or generalizes B.
  CONTRASTS_WITH  A is discussed against B as an alternative.
  IMPLEMENTS      A is a concrete implementation of B.
  CITES           A references B without being derived from it.

Rules:
  * Extract only what the chapter actually says. Do not speculate about relations that aren't there.
  * Skip generic words like "data", "file", "system", "user" — they aren't concepts unless the chapter gives them a specific, named meaning.
  * Every relation's `from` and `to` must exactly match the `name` of an entity you extracted.
  * Confidence is your certainty the chapter explicitly states the relation, on 0.0–1.0.
  * If a chapter is mostly front-matter (preface, TOC, index), return empty lists.

Output JSON schema:
{
  "entities": [
    {"name": "string", "type": "Concept|Pattern|Tool|Framework|Algorithm|Technique", "description": "1-2 sentences"}
  ],
  "relations": [
    {"from": "string", "to": "string", "type": "REQUIRES|EXTENDS|CONTRASTS_WITH|IMPLEMENTS|CITES", "confidence": 0.0}
  ]
}"""


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------

@dataclass
class ChapterRecord:
    chapter_id: int
    book_id: int
    chapter_num: Optional[int]
    title: Optional[str]
    content: str
    book_title: str
    authors: list[str]


def _load_chapter(conn: duckdb.DuckDBPyConnection, chapter_id: int) -> ChapterRecord:
    """Fetch the chapter plus its book title and author list."""
    row = conn.execute(
        """
        SELECT ch.chapter_id, ch.book_id, ch.chapter_num, ch.title, ch.content,
               b.title AS book_title
          FROM chapter ch
          JOIN book b ON ch.book_id = b.book_id
         WHERE ch.chapter_id = ?
        """,
        [chapter_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"chapter_id {chapter_id} not found")
    cid, bid, cnum, title, content, book_title = row
    if not content:
        raise ValueError(f"chapter_id {chapter_id} has no content")

    authors = [
        r[0]
        for r in conn.execute(
            """
            SELECT a.name
              FROM book_author ba JOIN author a USING (author_id)
             WHERE ba.book_id = ? ORDER BY ba.position
            """,
            [bid],
        ).fetchall()
    ]
    return ChapterRecord(cid, bid, cnum, title, content, book_title, authors)


def _resolve_chapter_id(
    conn: duckdb.DuckDBPyConnection, book_id: int, chapter_num: int
) -> int:
    """Translate (book_id, chapter_num) into a chapter_id."""
    row = conn.execute(
        "SELECT chapter_id FROM chapter WHERE book_id = ? AND chapter_num = ?",
        [book_id, chapter_num],
    ).fetchone()
    if row is None:
        raise ValueError(f"no chapter_num {chapter_num} in book_id {book_id}")
    return row[0]


# ----------------------------------------------------------------------------
# Prompt builders — exported so sub-agent drivers can share them
# ----------------------------------------------------------------------------

def _build_user_prompt(chapter: ChapterRecord) -> str:
    content = chapter.content
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[...truncated for length]"
    authors_line = ", ".join(chapter.authors) if chapter.authors else "Unknown"
    return (
        f"Book: {chapter.book_title}\n"
        f"Authors: {authors_line}\n"
        f"Chapter: {chapter.title or '(untitled)'}\n"
        f"\n---\n\n"
        f"{content}"
    )


def build_full_prompt(chapter: ChapterRecord) -> str:
    """Build a self-contained prompt combining SYSTEM_PROMPT + chapter payload.

    Used when the caller invokes the LLM through a channel that takes a single
    prompt string — e.g. Claude Code's Task tool dispatching a sub-agent.
    """
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- CHAPTER TO EXTRACT ---\n\n"
        f"{_build_user_prompt(chapter)}\n\n"
        f"Respond with JSON only. No prose, no markdown fences."
    )


def parse_llm_json(text: str) -> dict:
    """Parse LLM output into a dict, stripping ```json fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    return json.loads(text)


def _validate_extraction(raw: dict) -> tuple[list[dict], list[dict]]:
    """Filter the raw LLM output to well-formed entities/relations."""
    entities = []
    for e in raw.get("entities", []):
        name = (e.get("name") or "").strip()
        etype = (e.get("type") or "").strip()
        if not name:
            LOG.debug("skip entity with empty name: %r", e)
            continue
        if etype not in ENTITY_TYPES:
            LOG.warning("skip entity %r with invalid type %r", name, etype)
            continue
        entities.append({
            "name": name,
            "type": etype,
            "description": (e.get("description") or "").strip(),
        })

    names = {e["name"] for e in entities}

    relations = []
    for r in raw.get("relations", []):
        src = (r.get("from") or "").strip()
        dst = (r.get("to") or "").strip()
        rtype = (r.get("type") or "").strip()
        conf = r.get("confidence", 0.0)
        if rtype not in RELATION_TYPES:
            LOG.warning("skip relation with invalid type %r", rtype)
            continue
        if src not in names or dst not in names:
            LOG.warning("skip relation with unknown endpoint: %r → %r (%s)",
                        src, dst, rtype)
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        if src == dst:
            LOG.debug("skip self-relation on %r", src)
            continue
        relations.append(
            {"from": src, "to": dst, "type": rtype, "confidence": conf}
        )
    return entities, relations


# ----------------------------------------------------------------------------
# DB writers
# ----------------------------------------------------------------------------

def _clear_prior_extraction(conn: duckdb.DuckDBPyConnection, chapter_id: int) -> int:
    """Remove concept_relation rows previously written from this chapter.

    Concepts themselves are left in place — they may be referenced by other
    chapters or by humans via the review queue. Re-running produces a fresh
    relation set for this source without touching nodes.
    """
    before = conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE source_type='chapter' AND source_id = ?",
        [chapter_id],
    ).fetchone()[0]
    if before:
        conn.execute(
            "DELETE FROM concept_relation "
            "WHERE source_type='chapter' AND source_id = ?",
            [chapter_id],
        )
    return before


def _write_relation(
    conn: duckdb.DuckDBPyConnection,
    from_id: int,
    to_id: int,
    rtype: str,
    confidence: float,
    chapter_id: int,
) -> bool:
    """Insert one concept_relation row. Duplicates fail silently."""
    try:
        conn.execute(
            """
            INSERT INTO concept_relation
                (from_concept_id, to_concept_id, relation_type, confidence,
                 source_type, source_id)
            VALUES (?, ?, ?, ?, 'chapter', ?)
            """,
            [from_id, to_id, rtype, confidence, chapter_id],
        )
        return True
    except duckdb.ConstraintException:
        return False


# ----------------------------------------------------------------------------
# Top-level orchestration
# ----------------------------------------------------------------------------

@dataclass
class ExtractionSummary:
    chapter_id: int
    entities_extracted: int
    entities_by_resolution: dict[str, int]
    relations_extracted: int
    relations_written: int
    prior_relations_cleared: int


def process_extraction_json(
    conn: duckdb.DuckDBPyConnection,
    resolver: EntityResolver,
    chapter_id: int,
    raw: dict,
) -> ExtractionSummary:
    """Resolve entities and persist relations given already-parsed LLM output.

    This is the shared back-end for any caller that already has the
    structured JSON — whether produced by a sub-agent, copy-pasted from a
    chat, or synthesized by a future batch driver.
    """
    # Ensure the chapter exists; raises if not.
    _load_chapter(conn, chapter_id)

    entities, relations = _validate_extraction(raw)
    LOG.info(
        "validated %d entities, %d relations", len(entities), len(relations),
    )

    name_to_id: dict[str, int] = {}
    counts: dict[str, int] = {}
    for e in entities:
        result = resolver.resolve(
            e["name"],
            candidate_context=e["description"],
            concept_type=e["type"],
            source_type="chapter",
            source_id=chapter_id,
        )
        name_to_id[e["name"]] = result.concept_id
        counts[result.resolution_type] = counts.get(result.resolution_type, 0) + 1

    cleared = _clear_prior_extraction(conn, chapter_id)
    written = 0
    for r in relations:
        from_id = name_to_id[r["from"]]
        to_id = name_to_id[r["to"]]
        if _write_relation(conn, from_id, to_id, r["type"], r["confidence"], chapter_id):
            written += 1

    conn.commit()
    return ExtractionSummary(
        chapter_id=chapter_id,
        entities_extracted=len(entities),
        entities_by_resolution=counts,
        relations_extracted=len(relations),
        relations_written=written,
        prior_relations_cleared=cleared,
    )


def _print_summary(summary: ExtractionSummary) -> None:
    """Human-readable run stats to stdout."""
    print("\n=== extraction summary ===")
    print(f"chapter_id:        {summary.chapter_id}")
    print(f"entities:          {summary.entities_extracted}")
    if summary.entities_by_resolution:
        for rtype, n in sorted(summary.entities_by_resolution.items()):
            print(f"  {rtype:<16} {n}")
    print(f"relations written: {summary.relations_written} "
          f"(of {summary.relations_extracted} extracted)")
    if summary.prior_relations_cleared:
        print(f"prior relations cleared: {summary.prior_relations_cleared}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Parse args and run one of the two modes.

    Modes:
      --chapter-id N --print-prompt [--output PATH]
          Build the sub-agent prompt for this chapter; write it to stdout or
          PATH. No LLM call, no DB write.
      --chapter-id N --json-file PATH
          Read pre-parsed LLM JSON from PATH and process it (resolve +
          write DB). No LLM call.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--chapter-id", type=int)
    parser.add_argument("--book-id", type=int)
    parser.add_argument("--chapter-num", type=int)
    parser.add_argument("--print-prompt", action="store_true",
                        help="emit the sub-agent prompt for this chapter")
    parser.add_argument("--output", type=Path, default=None,
                        help="with --print-prompt, write to this path instead of stdout")
    parser.add_argument("--json-file", type=Path, default=None,
                        help="process LLM output already stored at this path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.print_prompt and args.json_file is None:
        LOG.error("provide --print-prompt or --json-file")
        return 2
    if args.print_prompt and args.json_file is not None:
        LOG.error("--print-prompt and --json-file are mutually exclusive")
        return 2

    conn = duckdb.connect(str(args.catalog))
    try:
        if args.chapter_id is None:
            if args.book_id is None or args.chapter_num is None:
                LOG.error("provide --chapter-id or (--book-id and --chapter-num)")
                return 2
            chapter_id = _resolve_chapter_id(conn, args.book_id, args.chapter_num)
        else:
            chapter_id = args.chapter_id

        if args.print_prompt:
            chapter = _load_chapter(conn, chapter_id)
            prompt = build_full_prompt(chapter)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(prompt)
                LOG.info("wrote prompt for chapter %d to %s (%d chars)",
                         chapter_id, args.output, len(prompt))
            else:
                sys.stdout.write(prompt)
            return 0

        # --json-file mode
        raw_text = args.json_file.read_text()
        try:
            raw = parse_llm_json(raw_text)
        except json.JSONDecodeError as exc:
            LOG.error("JSON parse failed in %s: %s", args.json_file, exc)
            return 3
        resolver = EntityResolver(conn)
        summary = process_extraction_json(conn, resolver, chapter_id, raw)
        _print_summary(summary)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
