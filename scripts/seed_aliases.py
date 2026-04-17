#!/usr/bin/env python3
"""
seed_aliases.py — Propose concept aliases with Claude Haiku and register them.

For each concept in the catalog, asks Claude Haiku to propose common
abbreviations, synonyms, casing variants, and plurals. Obviously
unambiguous ones only — the system prompt tells the model to refuse
speculation. Registered aliases land in `concept_alias` and start being
matched by EntityResolver immediately.

Requires `ANTHROPIC_API_KEY` in the environment.

Usage:
    .venv/bin/python3 scripts/seed_aliases.py                   # all concepts
    .venv/bin/python3 scripts/seed_aliases.py --limit 50        # first 50
    .venv/bin/python3 scripts/seed_aliases.py --batch 10        # per-LLM-call
    .venv/bin/python3 scripts/seed_aliases.py --dry-run         # print only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MODEL = "claude-haiku-4-5"
LOG = logging.getLogger("seed_aliases")

VALID_ALIAS_TYPES = {"abbreviation", "synonym", "casing", "plural"}

SYSTEM_PROMPT = """You propose aliases for technical concepts so a knowledge base can match different surface forms of the same idea.

For each concept you see, return only aliases that a domain expert would consider unambiguously correct. Refuse to speculate. Prefer fewer correct aliases over more uncertain ones. Never include the canonical name itself as an alias.

Valid alias types:
  - "abbreviation" — a shortened form (e.g., CDC for Change Data Capture)
  - "synonym"      — a distinct name for the same concept (e.g., "microservices" ↔ "microservice architecture")
  - "casing"       — a different case convention only (e.g., ETL vs etl)
  - "plural"       — plural/singular variant when the concept name is countable

Do NOT include:
  - narrower or broader concepts
  - related concepts that aren't the same thing
  - marketing names, product names, or vendor-specific branding for a generic concept
  - stylistic rewordings

If a concept has no obvious aliases, return an empty list for it.

Respond with JSON only, matching this schema:
{
  "results": [
    {
      "concept_id": <int>,
      "aliases": [
        {"alias": "<string>", "alias_type": "abbreviation|synonym|casing|plural"}
      ]
    }
  ]
}"""


def _iter_batches(
    conn: duckdb.DuckDBPyConnection, batch: int, limit: int | None
) -> Iterator[list[tuple[int, str, str | None, str | None]]]:
    """Yield batches of concepts that have no aliases yet.

    Concepts already carrying at least one alias are skipped so the script
    is cheap to re-run.
    """
    sql = (
        "SELECT c.concept_id, c.name, c.concept_type, c.description "
        "  FROM concept c "
        "  LEFT JOIN concept_alias a USING (concept_id) "
        " WHERE a.alias_id IS NULL "
        " GROUP BY c.concept_id, c.name, c.concept_type, c.description "
        " ORDER BY c.concept_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    for i in range(0, len(rows), batch):
        yield rows[i : i + batch]


def _propose_aliases(client, batch: list[tuple[int, str, str | None, str | None]]) -> dict:
    """Single LLM call proposing aliases for a batch of concepts."""
    concepts_payload = [
        {
            "concept_id": cid,
            "name": name,
            "concept_type": ctype,
            "description": (desc or "")[:400],  # cap description tokens
        }
        for cid, name, ctype, desc in batch
    ]
    user_prompt = (
        "Propose aliases for the following concepts. Respond with JSON only.\n\n"
        + json.dumps({"concepts": concepts_payload}, indent=2)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = response.content[0].text
    # Some models wrap JSON in ```json fences — strip defensively.
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    return json.loads(text)


def _register(
    conn: duckdb.DuckDBPyConnection, concept_id: int, proposals: list[dict], dry_run: bool
) -> int:
    """Insert the proposed aliases into concept_alias. Returns new-row count."""
    added = 0
    for proposal in proposals:
        alias = (proposal.get("alias") or "").strip()
        alias_type = (proposal.get("alias_type") or "").strip().lower()
        if not alias or alias_type not in VALID_ALIAS_TYPES:
            LOG.debug("skip invalid alias proposal for concept %d: %r", concept_id, proposal)
            continue
        if dry_run:
            LOG.info("  [dry] concept %d: alias=%r type=%s", concept_id, alias, alias_type)
            added += 1
            continue
        # UNIQUE(concept_id, alias) — duplicates fail quietly.
        try:
            conn.execute(
                "INSERT INTO concept_alias (concept_id, alias, alias_type) "
                "VALUES (?, ?, ?)",
                [concept_id, alias, alias_type],
            )
            added += 1
        except duckdb.ConstraintException:
            LOG.debug("duplicate alias skipped: concept %d %r", concept_id, alias)
    return added


def main() -> int:
    """Seed concept_alias from the current concept table via Haiku proposals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--batch", type=int, default=8,
                        help="concepts per LLM call (default 8)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap total concepts processed")
    parser.add_argument("--dry-run", action="store_true",
                        help="show proposals without writing to DB")
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.api_key and not args.dry_run:
        LOG.error("ANTHROPIC_API_KEY not set (and --dry-run not passed). "
                  "Export it or use --dry-run.")
        return 2

    # pylint: disable=import-outside-toplevel
    import anthropic
    client = anthropic.Anthropic(api_key=args.api_key) if args.api_key else None

    conn = duckdb.connect(str(args.catalog))
    try:
        total_concepts = conn.execute(
            "SELECT COUNT(*) FROM concept c "
            "LEFT JOIN concept_alias a USING (concept_id) "
            "WHERE a.alias_id IS NULL"
        ).fetchone()[0]
        LOG.info("concepts without aliases: %d", total_concepts)
        if total_concepts == 0:
            LOG.info("nothing to seed — concept table empty or all have aliases")
            return 0

        start = time.time()
        processed = 0
        registered = 0
        for batch in _iter_batches(conn, args.batch, args.limit):
            if client is None:
                LOG.info("[dry-run] would prompt for %d concepts", len(batch))
                processed += len(batch)
                continue
            try:
                response = _propose_aliases(client, batch)
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                LOG.error("LLM call failed for batch starting at concept %d: %s",
                          batch[0][0], exc)
                continue
            for result in response.get("results", []):
                cid = result.get("concept_id")
                if cid is None:
                    continue
                added = _register(conn, cid, result.get("aliases", []), args.dry_run)
                registered += added
            processed += len(batch)
            if processed % 40 < len(batch):
                elapsed = time.time() - start
                LOG.info("%d/%d concepts processed (%.1fs)", processed, total_concepts, elapsed)

        conn.commit()
        LOG.info("done: %d concepts processed, %d aliases %s in %.1fs",
                 processed, registered,
                 "proposed (dry-run)" if args.dry_run else "registered",
                 time.time() - start)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
