#!/usr/bin/env python3
"""
seed_aliases.py — Propose and register concept aliases.

Coordinator script for alias seeding. Follows the same pattern as
extract_entities.py: the Python code handles I/O and DB writes; the
LLM reasoning is delegated to a Claude Code sub-agent (Task tool).
All LLM work runs on the Max subscription — this script never calls
the Anthropic API.

Workflow:
    1. `seed_aliases.py --print-prompt [--limit N] [--output PATH]`
       emits a self-contained prompt listing up to N concepts that
       currently have no aliases. Feed this to a Claude Code
       sub-agent and have it write the JSON result to a file.

    2. `seed_aliases.py --json-file PATH` reads the sub-agent's
       JSON result and writes the proposed aliases to concept_alias.

Usage:
    .venv/bin/python3 scripts/seed_aliases.py --print-prompt --limit 50
    .venv/bin/python3 scripts/seed_aliases.py --json-file /tmp/aliases.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
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


# ----------------------------------------------------------------------------
# Queries + prompt building
# ----------------------------------------------------------------------------

def _concepts_needing_aliases(
    conn: duckdb.DuckDBPyConnection, limit: int | None
) -> list[tuple[int, str, str | None, str | None]]:
    """Return concepts that have no alias rows yet, ordered by concept_id."""
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
    return conn.execute(sql).fetchall()


def build_prompt(
    concepts: Iterable[tuple[int, str, str | None, str | None]],
) -> str:
    """Assemble the full self-contained sub-agent prompt for the given concepts."""
    payload = [
        {
            "concept_id": cid,
            "name": name,
            "concept_type": ctype,
            "description": (desc or "")[:400],
        }
        for cid, name, ctype, desc in concepts
    ]
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- CONCEPTS TO ALIAS ---\n\n"
        f"{json.dumps({'concepts': payload}, indent=2)}\n\n"
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


# ----------------------------------------------------------------------------
# DB writers
# ----------------------------------------------------------------------------

def register_aliases(
    conn: duckdb.DuckDBPyConnection, raw: dict
) -> tuple[int, int]:
    """Insert aliases from a parsed LLM result.

    Returns (concepts_processed, aliases_registered). Duplicate
    (concept_id, alias) rows are silently skipped via the UNIQUE
    constraint.
    """
    concepts_seen = 0
    registered = 0

    for result in raw.get("results", []):
        cid = result.get("concept_id")
        if cid is None:
            continue
        concepts_seen += 1
        for proposal in result.get("aliases", []):
            alias = (proposal.get("alias") or "").strip()
            alias_type = (proposal.get("alias_type") or "").strip().lower()
            if not alias:
                continue
            if alias_type not in VALID_ALIAS_TYPES:
                LOG.warning("skip alias %r with invalid type %r for concept %s",
                            alias, alias_type, cid)
                continue
            try:
                conn.execute(
                    "INSERT INTO concept_alias (concept_id, alias, alias_type) "
                    "VALUES (?, ?, ?)",
                    [cid, alias, alias_type],
                )
                registered += 1
            except duckdb.ConstraintException:
                LOG.debug("duplicate alias skipped: concept %s %r", cid, alias)
    conn.commit()
    return concepts_seen, registered


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Run --print-prompt or --json-file mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--print-prompt", action="store_true",
                        help="emit the sub-agent prompt for concepts lacking aliases")
    parser.add_argument("--limit", type=int, default=None,
                        help="with --print-prompt, cap how many concepts to include")
    parser.add_argument("--output", type=Path, default=None,
                        help="with --print-prompt, write to this path instead of stdout")
    parser.add_argument("--json-file", type=Path, default=None,
                        help="read sub-agent JSON result from this path and "
                             "register the proposed aliases")
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
        if args.print_prompt:
            concepts = _concepts_needing_aliases(conn, args.limit)
            if not concepts:
                LOG.info("no concepts need aliases — concept table empty or all have aliases")
                return 0
            prompt = build_prompt(concepts)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(prompt)
                LOG.info("wrote prompt for %d concepts to %s (%d chars)",
                         len(concepts), args.output, len(prompt))
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
        concepts_seen, registered = register_aliases(conn, raw)
        LOG.info("processed %d concepts, registered %d aliases",
                 concepts_seen, registered)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
