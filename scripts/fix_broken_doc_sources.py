#!/usr/bin/env python3
"""
fix_broken_doc_sources.py — Repair doc_source rows whose Context7 fetch
returned an error stub.

When the catalog was first seeded, two doc_source rows landed with
identifiers that Context7 couldn't resolve to real content:

  * Databricks  /databricks/databricks  → "Library not found"
  * LangChain   /langchain-ai/langchain → "redirected to ..."

These appear as 1-section, ~100-byte stubs in doc_section, contributing
nothing to retrieval. Re-probed Context7 yields the real identifiers:

  * Databricks  /websites/databricks      (score 86.92)
  * LangChain   /websites/langchain       (score 82.13)

This script:

  1. Deletes the existing stub snapshot + its sections + their embeddings.
  2. Updates ``doc_source.identifier`` to the corrected value.
  3. Calls ``refresh_one_source`` to fetch + persist real content.

Idempotent: if the doc_source already points at the corrected identifier
and has substantive content, this no-ops (refresh will see hash match).

Usage:
    .venv/bin/python3 scripts/fix_broken_doc_sources.py [--dry-run]

Catalog must be writable (close any running MCP server first).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
SCRIPTS = PROJECT_ROOT / "scripts"
for p in (str(KB_MCP), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from db import open_catalog  # noqa: E402
from refresh_docs import refresh_one_source  # noqa: E402

LOG = logging.getLogger("fix_broken_doc_sources")


# (display_name, current_identifier, corrected_identifier)
FIXES = [
    ("Databricks", "/databricks/databricks", "/websites/databricks"),
    ("LangChain",  "/langchain-ai/langchain", "/websites/langchain"),
]


def _delete_existing_snapshots(
    conn: duckdb.DuckDBPyConnection, doc_source_id: int,
) -> tuple[int, int]:
    """Delete every snapshot + section + embeddings for ``doc_source_id``.

    Returns ``(snapshot_count, section_count)`` deleted.
    """
    section_ids = [r[0] for r in conn.execute(
        """
        SELECT sec.doc_section_id
          FROM doc_section sec
          JOIN doc_snapshot sn ON sn.snapshot_id = sec.snapshot_id
         WHERE sn.doc_source_id = ?
        """,
        [doc_source_id],
    ).fetchall()]
    snapshot_ids = [r[0] for r in conn.execute(
        "SELECT snapshot_id FROM doc_snapshot WHERE doc_source_id = ?",
        [doc_source_id],
    ).fetchall()]

    if section_ids:
        ph = ",".join(["?"] * len(section_ids))
        conn.execute(
            f"DELETE FROM doc_section_embedding WHERE doc_section_id IN ({ph})",
            section_ids,
        )
        # alignment_edge has FK to doc_section; clean orphans first.
        conn.execute(
            f"DELETE FROM alignment_edge WHERE from_doc_section_id IN ({ph}) "
            f"OR to_doc_section_id IN ({ph})",
            [*section_ids, *section_ids],
        )
        conn.execute(
            f"DELETE FROM doc_section WHERE doc_section_id IN ({ph})",
            section_ids,
        )

    if snapshot_ids:
        ph = ",".join(["?"] * len(snapshot_ids))
        conn.execute(
            f"DELETE FROM doc_snapshot_embedding WHERE snapshot_id IN ({ph})",
            snapshot_ids,
        )
        conn.execute(
            f"DELETE FROM doc_snapshot WHERE snapshot_id IN ({ph})",
            snapshot_ids,
        )

    return len(snapshot_ids), len(section_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = open_catalog(read_only=args.dry_run)

    for display_name, old_id, new_id in FIXES:
        row = conn.execute(
            """
            SELECT doc_source_id, identifier
              FROM doc_source
             WHERE name = ? AND source_type = 'context7'
            """,
            [display_name],
        ).fetchone()
        if row is None:
            LOG.warning("no doc_source row for %s — skip", display_name)
            continue
        doc_source_id, current_id = int(row[0]), row[1]

        if current_id == new_id:
            LOG.info("%s already on %s; checking content", display_name, new_id)
        elif current_id != old_id:
            LOG.warning(
                "%s identifier is %r (expected %r) — skipping to avoid "
                "clobbering manual changes",
                display_name, current_id, old_id,
            )
            continue

        sec_count = conn.execute(
            """
            SELECT COUNT(*) FROM doc_section sec
             JOIN doc_snapshot sn ON sn.snapshot_id = sec.snapshot_id
            WHERE sn.doc_source_id = ?
            """,
            [doc_source_id],
        ).fetchone()[0]
        LOG.info(
            "%s (id=%d): current sections=%d, identifier=%s → %s",
            display_name, doc_source_id, sec_count, current_id, new_id,
        )

        if args.dry_run:
            continue

        snap_n, sec_n = _delete_existing_snapshots(conn, doc_source_id)
        LOG.info(
            "%s: deleted %d snapshot(s) + %d section(s)",
            display_name, snap_n, sec_n,
        )
        conn.execute(
            "UPDATE doc_source SET identifier = ? WHERE doc_source_id = ?",
            [new_id, doc_source_id],
        )

        result = refresh_one_source(conn, doc_source_id=doc_source_id)
        if result.status == "error":
            LOG.error("%s: refresh failed: %s", display_name, result.error)
            continue
        new_sec_count = conn.execute(
            """
            SELECT COUNT(*) FROM doc_section sec
             JOIN doc_snapshot sn ON sn.snapshot_id = sec.snapshot_id
            WHERE sn.doc_source_id = ?
            """,
            [doc_source_id],
        ).fetchone()[0]
        LOG.info(
            "%s: refresh status=%s; new sections=%d",
            display_name, result.status, new_sec_count,
        )

    if not args.dry_run:
        conn.execute("CHECKPOINT")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
