#!/usr/bin/env python3
"""
extract_entities.py — LLM-based concept/relation extraction.

For one chapter at a time (per the Phase 2.2 prompt — Phase 2.3 tunes on
10 books, 2.4 goes corpus-wide). Given a chapter_id, this script:

  1. Loads the chapter's content, title, and containing book metadata.
  2. Asks Claude Haiku for structured JSON: entities (with type +
     description) and relations (from, to, type, confidence).
  3. Runs each extracted entity through EntityResolver so it either
     resolves to an existing concept or creates a new one with
     source-provenance back to this chapter.
  4. Inserts each relation into concept_relation with
     source_type='chapter' and source_id=<chapter_id>. Duplicate
     relations from a re-run are handled by clearing the chapter's
     prior relations before inserting.

Prints a resolution summary (exact / alias / embedding_high / borderline
/ new) plus extraction counts.

Requires ANTHROPIC_API_KEY. For programmatic / Phase 2.4 batch use,
import `extract_chapter()` directly rather than shelling out.

Usage:
    .venv/bin/python3 scripts/extract_entities.py --chapter-id 12345
    .venv/bin/python3 scripts/extract_entities.py --book-id 42 --chapter-num 3
    .venv/bin/python3 scripts/extract_entities.py --chapter-id 12345 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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

MODEL = "claude-haiku-4-5"
MAX_CONTENT_CHARS = 20000       # ~5K input tokens; truncate to cap per-call cost
MAX_OUTPUT_TOKENS = 4000
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
# LLM call
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


def _call_llm(client, user_prompt: str) -> dict:
    """One Haiku call returning parsed JSON. Raises on parse or validation error."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = response.content[0].text.strip()
    # Strip ```json fences defensively.
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


def extract_chapter(
    conn: duckdb.DuckDBPyConnection,
    client,
    resolver: EntityResolver,
    chapter_id: int,
    *,
    dry_run: bool = False,
) -> ExtractionSummary:
    """Extract and (optionally) persist entities + relations for one chapter."""
    chapter = _load_chapter(conn, chapter_id)
    LOG.info("extracting: %s :: %s", chapter.book_title[:50], (chapter.title or "")[:50])

    raw = _call_llm(client, _build_user_prompt(chapter))
    entities, relations = _validate_extraction(raw)
    LOG.info(
        "LLM returned %d entities (valid), %d relations (valid)",
        len(entities), len(relations),
    )

    if dry_run:
        print(json.dumps({"entities": entities, "relations": relations}, indent=2))
        return ExtractionSummary(
            chapter_id=chapter_id,
            entities_extracted=len(entities),
            entities_by_resolution={},
            relations_extracted=len(relations),
            relations_written=0,
            prior_relations_cleared=0,
        )

    # Resolve entities → concept_id mapping.
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

    # Clear prior relations and write fresh ones.
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


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Parse args, extract one chapter, print a summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--chapter-id", type=int)
    parser.add_argument("--book-id", type=int)
    parser.add_argument("--chapter-num", type=int)
    parser.add_argument("--dry-run", action="store_true",
                        help="call the LLM and print JSON; skip DB writes")
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.api_key:
        LOG.error("ANTHROPIC_API_KEY not set.")
        return 2

    # pylint: disable=import-outside-toplevel
    import anthropic
    client = anthropic.Anthropic(api_key=args.api_key)

    conn = duckdb.connect(str(args.catalog))
    try:
        if args.chapter_id is None:
            if args.book_id is None or args.chapter_num is None:
                LOG.error("provide --chapter-id or (--book-id and --chapter-num)")
                return 2
            chapter_id = _resolve_chapter_id(conn, args.book_id, args.chapter_num)
        else:
            chapter_id = args.chapter_id

        resolver = EntityResolver(conn)  # loads embedder lazily
        summary = extract_chapter(
            conn, client, resolver, chapter_id, dry_run=args.dry_run,
        )

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
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
