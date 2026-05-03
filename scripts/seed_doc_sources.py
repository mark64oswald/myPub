#!/usr/bin/env python3
"""
seed_doc_sources.py — Populate doc_source with curated technologies.

Phase 4.2 deliverable: register the initial set of technologies the KB
should track via live docs. Each entry is keyed by (source_type,
identifier) and carries an authority score per architecture §5.4 plus
a refresh TTL appropriate to how fast that technology moves.

Source-type contract (per arch §6.1, §5.4):
  - context7   — vendor docs and well-indexed OSS via Context7 MCP
  - deepwiki   — AI-generated docs for any public GitHub repo
  - github_raw — raw file fetching from a public repo (last-resort)

Authority scores (explicit, not auto-discovered):
  - context7   : 0.90
  - deepwiki   : 0.75
  - github_raw : 0.65

Idempotent: re-runs UPDATE name/authority/refresh_ttl_days but preserve
priority_tier, pinned, last_refresh_at, and last_content_changed_at —
those reflect runtime state, not seed data.

The Context7 identifiers below are best-guess paths following the
common /owner/repo convention. The first ingestion run (Phase 4.4)
will surface any that don't resolve; update them in place and re-run
the seed.

Note: databricks-solutions/ai-dev-kit is deliberately excluded so it
remains an unknown for the Phase 4.5b auto-discovery test.

Usage:
    .venv/bin/python3 scripts/seed_doc_sources.py
    .venv/bin/python3 scripts/seed_doc_sources.py --catalog PATH
    .venv/bin/python3 scripts/seed_doc_sources.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("seed_doc_sources")

VALID_SOURCE_TYPES = {"context7", "deepwiki", "github_raw"}

# Per arch §5.4. Auto-discovered sources (Phase 4.5b) get lower scores;
# these are the explicit/curated ceilings.
DEFAULT_AUTHORITY = {
    "context7": 0.90,
    "deepwiki": 0.75,
    "github_raw": 0.65,
}


SEEDS: list[dict[str, Any]] = [
    # Vendor + heavyweight platforms — Context7 has strong coverage.
    {
        "name": "Databricks",
        "source_type": "context7",
        "identifier": "/databricks/databricks",
        "refresh_ttl_days": 7,   # fast-moving, deeply represented in corpus
    },
    {
        "name": "PostgreSQL",
        "source_type": "context7",
        "identifier": "/postgres/postgres",
        "refresh_ttl_days": 30,
    },
    {
        "name": "DuckDB",
        "source_type": "context7",
        "identifier": "/duckdb/duckdb",
        "refresh_ttl_days": 14,  # core to this project itself
    },
    {
        "name": "Apache Spark",
        "source_type": "context7",
        "identifier": "/apache/spark",
        "refresh_ttl_days": 30,
    },
    {
        "name": "Delta Lake",
        "source_type": "context7",
        "identifier": "/delta-io/delta",
        "refresh_ttl_days": 14,
    },
    {
        "name": "MLflow",
        "source_type": "context7",
        "identifier": "/mlflow/mlflow",
        "refresh_ttl_days": 14,
    },
    {
        "name": "Apache Kafka",
        "source_type": "context7",
        "identifier": "/apache/kafka",
        "refresh_ttl_days": 30,
    },
    {
        "name": "LangChain",
        "source_type": "context7",
        "identifier": "/langchain-ai/langchain",
        "refresh_ttl_days": 7,   # very fast-moving GenAI stack
    },
    # OSS libraries that DeepWiki covers better than Context7.
    {
        "name": "FastMCP",
        "source_type": "deepwiki",
        "identifier": "jlowin/fastmcp",
        "refresh_ttl_days": 14,  # mypub itself depends on it
    },
    {
        "name": "DuckPGQ",
        "source_type": "deepwiki",
        "identifier": "cwida/duckpgq-extension",
        "refresh_ttl_days": 30,
    },
]


# ----------------------------------------------------------------------------
# DB ops
# ----------------------------------------------------------------------------

def _validate(seed: dict[str, Any]) -> None:
    """Sanity-check a seed entry before insert."""
    missing = [k for k in ("name", "source_type", "identifier", "refresh_ttl_days")
               if not seed.get(k)]
    if missing:
        raise ValueError(f"seed missing required keys {missing}: {seed}")
    if seed["source_type"] not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"unknown source_type {seed['source_type']!r}; "
            f"expected one of {sorted(VALID_SOURCE_TYPES)}"
        )


def _upsert(conn: duckdb.DuckDBPyConnection, seed: dict[str, Any]) -> str:
    """Insert or update one doc_source row. Returns 'inserted' or 'updated'."""
    _validate(seed)
    authority = seed.get("authority_score", DEFAULT_AUTHORITY[seed["source_type"]])

    existing = conn.execute(
        "SELECT doc_source_id, name, authority_score, refresh_ttl_days "
        "  FROM doc_source WHERE source_type = ? AND identifier = ?",
        [seed["source_type"], seed["identifier"]],
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO doc_source
                (name, source_type, mcp_server, identifier,
                 authority_score, refresh_ttl_days)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                seed["name"],
                seed["source_type"],
                seed["source_type"],   # mcp_server tracks which MCP serves it
                seed["identifier"],
                authority,
                seed["refresh_ttl_days"],
            ],
        )
        return "inserted"

    # Update mutable seed fields, but leave runtime state alone
    # (priority_tier, pinned, last_refresh_at, last_content_changed_at).
    conn.execute(
        """
        UPDATE doc_source
           SET name             = ?,
               authority_score  = ?,
               refresh_ttl_days = ?
         WHERE source_type = ? AND identifier = ?
        """,
        [
            seed["name"], authority, seed["refresh_ttl_days"],
            seed["source_type"], seed["identifier"],
        ],
    )
    return "updated"


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    try:
        if args.dry_run:
            for seed in SEEDS:
                _validate(seed)
                authority = seed.get(
                    "authority_score", DEFAULT_AUTHORITY[seed["source_type"]]
                )
                LOG.info(
                    "would seed %-12s %-30s  auth=%.2f  ttl=%dd",
                    seed["source_type"], seed["identifier"],
                    authority, seed["refresh_ttl_days"],
                )
            return 0

        conn.execute("BEGIN")
        counts = {"inserted": 0, "updated": 0}
        for seed in SEEDS:
            action = _upsert(conn, seed)
            counts[action] += 1
            LOG.info(
                "%s %-12s %s",
                action.upper(), seed["source_type"], seed["identifier"],
            )
        conn.execute("COMMIT")
        LOG.info(
            "done: %d inserted, %d updated  (%d total)",
            counts["inserted"], counts["updated"],
            counts["inserted"] + counts["updated"],
        )

        total = conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0]
        LOG.info("doc_source row count: %d", total)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
