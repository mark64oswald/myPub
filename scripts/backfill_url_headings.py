#!/usr/bin/env python3
"""
backfill_url_headings.py — Replace URL-shaped doc_section headings.

Phase 1+ of the sectionizer URL-heading fix. The parser was leaving
hyperlinks-as-headings (e.g. ``# https://github.com/foo/bar``) in
``doc_section.heading_text``, which the title-coverage scorer has to
zero out at query time. The sectionizer is now smart enough to derive
a meaningful body heading at parse time, but pre-existing rows still
carry the URL strings.

This script walks ``doc_section`` for rows with URL-shaped
``heading_text``, runs the same ``_derive_body_heading`` logic against
the row's own ``content``, and updates ``heading_text`` to:

  * the derived heading (string), or
  * ``NULL`` if no body heading surfaces — None is what the
    title-coverage scorer treats as "no signal", which is strictly
    better than a URL string.

Idempotent: re-running is a no-op once headings are repaired.

Usage:
    .venv/bin/python3 scripts/backfill_url_headings.py [--dry-run]
                                                       [--catalog PATH]

The script opens the catalog read-write. If the MCP server is running
it will fail at connect time — close the server first and retry.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

from db import open_catalog  # noqa: E402
from sectionizer import _derive_body_heading, _is_url_heading  # noqa: E402

LOG = logging.getLogger("backfill_url_headings")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without writing.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process at most N rows (0 = all). Useful for spot-checks.",
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
    rows = conn.execute(
        """
        SELECT doc_section_id, heading_text, content
          FROM doc_section
         WHERE heading_text IS NOT NULL
        """
    ).fetchall()

    candidates: list[tuple[int, str, str]] = [
        (rid, heading, content or "")
        for rid, heading, content in rows
        if _is_url_heading(heading)
    ]
    LOG.info(
        "scanned %d sections; %d have URL-shaped headings",
        len(rows), len(candidates),
    )

    if args.limit:
        candidates = candidates[: args.limit]
        LOG.info("--limit %d; processing first %d", args.limit, len(candidates))

    derived_count = 0
    nulled_count = 0
    for rid, _old_heading, body in candidates:
        new_heading = _derive_body_heading(body)
        if args.dry_run:
            if new_heading:
                derived_count += 1
            else:
                nulled_count += 1
            continue
        conn.execute(
            "UPDATE doc_section SET heading_text = ? WHERE doc_section_id = ?",
            [new_heading, rid],
        )
        if new_heading:
            derived_count += 1
        else:
            nulled_count += 1

    if args.dry_run:
        LOG.info(
            "DRY RUN — would derive %d headings, set %d to NULL",
            derived_count, nulled_count,
        )
    else:
        LOG.info(
            "updated %d rows (%d derived, %d set to NULL)",
            derived_count + nulled_count, derived_count, nulled_count,
        )
        conn.execute("CHECKPOINT")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
