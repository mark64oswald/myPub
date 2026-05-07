#!/usr/bin/env python3
"""
dedupe_concepts.py — Collapse strictly-orphan duplicates of named concepts.

The catalog carries 5,597 same-name concept groups split across
``concept_type`` variants (e.g. "Event Sourcing" exists as both
``Concept`` and ``Pattern``). The schema's UNIQUE(name, concept_type)
constraint allows this by design, but in practice a duplicate is
usually an extractor artifact: one twin gets all the edges and the
other is a name-only orphan.

This script deletes the orphan twins under a strict safety filter.
A duplicate is deleted only if **all** of the following are zero:

  * concept_relation rows           (no edges in or out)
  * alignment_edge rows             (no doc-section alignment)
  * concept_doc_link rows           (no doc_source linking)
  * concept_query_log rows          (no historical user queries)
  * procedure_concept rows          (no procedure references)
  * concept_alias rows              (no aliases)
  * concept_resolution_queue rows   (no pending review pointing at it)

A duplicate that's referenced anywhere is left in place — the resolver
fix in ecc74f4 already routes lookups to the richer twin, so leaving
the loaded duplicate alone is no worse than deleting it would be.

For each surviving duplicate-name group, the **canonical** is the
concept with the most edges (ties broken by lowest concept_id) — the
same ranking the resolver uses post-ecc74f4. The script never touches
the canonical.

What gets removed per orphan:
  * concept_embedding (1:1 with concept)
  * concept (the row itself)

Idempotent: re-running once orphans are gone is a no-op.

Usage:
    .venv/bin/python3 scripts/dedupe_concepts.py [--dry-run]
                                                 [--catalog PATH]
                                                 [--limit N]
                                                 [--report-file PATH]

A --report-file path receives a JSON dump of every deleted concept
(id, name, concept_type, description, dedupe_canonical_id) before
deletion — keep this if you want a recovery trail. Default: no report.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

from db import open_catalog  # noqa: E402

LOG = logging.getLogger("dedupe_concepts")

DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"


def find_orphan_duplicates(
    conn: duckdb.DuckDBPyConnection,
) -> list[dict]:
    """Return every duplicate concept that's safe to delete.

    Each entry includes the canonical's concept_id so callers can log
    a recovery trail or register an alias if desired.
    """
    rows = conn.execute(
        """
        WITH grp AS (
          SELECT name
            FROM concept
           GROUP BY name
          HAVING COUNT(*) > 1
        ),
        ranked AS (
          SELECT
            c.concept_id,
            c.name,
            c.concept_type,
            c.description,
            COALESCE((SELECT COUNT(*) FROM concept_relation cr
                       WHERE cr.from_concept_id = c.concept_id
                          OR cr.to_concept_id   = c.concept_id), 0)
              AS rel_count,
            ROW_NUMBER() OVER (
              PARTITION BY c.name
              ORDER BY (
                COALESCE((SELECT COUNT(*) FROM concept_relation cr
                           WHERE cr.from_concept_id = c.concept_id
                              OR cr.to_concept_id   = c.concept_id), 0)
              ) DESC, c.concept_id ASC
            ) AS rank_in_group
            FROM concept c JOIN grp ON grp.name = c.name
        )
        SELECT r.concept_id, r.name, r.concept_type, r.description,
               r.rel_count,
               canonical.concept_id AS canonical_id
          FROM ranked r
          JOIN ranked canonical
            ON canonical.name = r.name AND canonical.rank_in_group = 1
         WHERE r.rank_in_group > 1
           AND r.rel_count = 0
           AND NOT EXISTS (
                 SELECT 1 FROM alignment_edge ae
                  WHERE ae.concept_id = r.concept_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM concept_doc_link cdl
                  WHERE cdl.concept_id = r.concept_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM concept_query_log cql
                  WHERE cql.concept_id = r.concept_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM procedure_concept pc
                  WHERE pc.concept_id = r.concept_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM concept_alias a
                  WHERE a.concept_id = r.concept_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM concept_resolution_queue q
                  WHERE q.nearest_concept_id = r.concept_id
                     OR q.provisional_concept_id = r.concept_id
               )
         ORDER BY r.name, r.concept_id
        """,
    ).fetchall()
    return [
        {
            "concept_id": int(r[0]),
            "name": r[1],
            "concept_type": r[2],
            "description": r[3],
            "rel_count": int(r[4]),
            "canonical_id": int(r[5]),
        }
        for r in rows
    ]


def delete_orphans(
    conn: duckdb.DuckDBPyConnection, orphans: list[dict],
) -> int:
    """Delete every orphan and its concept_embedding row. Returns count."""
    if not orphans:
        return 0
    ids = [o["concept_id"] for o in orphans]
    placeholders = ",".join(["?"] * len(ids))
    conn.execute(
        f"DELETE FROM concept_embedding WHERE concept_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM concept WHERE concept_id IN ({placeholders})",
        ids,
    )
    return len(ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process at most N orphans (0 = all).",
    )
    parser.add_argument(
        "--report-file", type=Path, default=None,
        help="Optional path to dump JSON of deleted concepts (recovery trail).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.catalog.exists():
        LOG.error("catalog not found: %s", args.catalog)
        return 2

    conn = open_catalog(args.catalog, read_only=args.dry_run)
    try:
        orphans = find_orphan_duplicates(conn)
        LOG.info(
            "scanned catalog; found %d strictly-orphan duplicate concepts",
            len(orphans),
        )

        if args.limit:
            orphans = orphans[: args.limit]
            LOG.info("--limit %d; processing first %d", args.limit, len(orphans))

        # Type breakdown
        type_counts: dict[str | None, int] = {}
        for o in orphans:
            type_counts[o["concept_type"]] = type_counts.get(o["concept_type"], 0) + 1
        LOG.info("by concept_type: %s",
                 ", ".join(f"{k or 'NULL'}={v}"
                           for k, v in sorted(type_counts.items(),
                                              key=lambda kv: -kv[1])))

        if args.report_file is not None and not args.dry_run:
            args.report_file.parent.mkdir(parents=True, exist_ok=True)
            args.report_file.write_text(json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "catalog": str(args.catalog),
                    "orphans": orphans,
                },
                indent=2, default=str,
            ))
            LOG.info("wrote recovery trail to %s", args.report_file)

        if args.dry_run:
            LOG.info("DRY RUN — would delete %d orphan rows", len(orphans))
            return 0

        deleted = delete_orphans(conn, orphans)
        LOG.info("deleted %d orphan concepts (+ their embeddings)", deleted)
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
