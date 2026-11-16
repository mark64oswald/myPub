#!/usr/bin/env python3
"""
seed_vibe_modeling.py — Seed Databricks' Vibe Data Modeling docs as doc_sources (2026-11-16).

Databricks' Lakehouse Industry Data Models repo
(github.com/databricks-industry-solutions/lakehouse-industry-data-models)
holds an agent that generates Silver-layer models for a business, a set of
Genie Code skills that fit a model onto real bronze data, and a gallery of
80 generated industry models. This seeds the *knowledge* in that repo, not
the models: the agent's design docs and whitepaper, its rule catalog (one
section per rule, via refresh_docs.csv_to_markdown), the app's guidance on
describing a business and on conventions, and the Genie skills with their
developer docs (procedure-rich: assess, build, validate, document).

The 80 generated models are loaded separately as structured metadata by
scripts/load_ref_models.py, never as doc sections.

License: the repo is under the Databricks License (use only with Databricks
services). Indexing it in the local catalog for Mark's own use alongside
his Databricks work is the intended use; no repo content is copied into
myPub's git tree, only these identifiers.

Authority: 0.70 for the agent's docs and rules (vendor-authored method
docs), 0.65 for the skills and their how-tos (operational guidance), both
below the Databricks product docs.

One github_raw source per file, pinned to ``main``. Idempotent: reuses
``seed_doc_sources._upsert``.

Usage:
    .venv/bin/python3 scripts/seed_vibe_modeling.py --dry-run
    .venv/bin/python3 scripts/seed_vibe_modeling.py
    .venv/bin/python3 scripts/refresh_docs.py refresh --source-id <id>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("seed_vibe_modeling")

REPO = "databricks-industry-solutions/lakehouse-industry-data-models"
BRANCH = "main"
DOC_AUTHORITY = 0.70
SKILL_AUTHORITY = 0.65
TTL_DAYS = 14   # the repo moves weekly (last commit 2026-11-12)


def _seed(name: str, path: str, authority: float) -> dict[str, Any]:
    return {
        "name": f"Vibe Data Modeling: {name}",
        "source_type": "github_raw",
        "identifier": f"{REPO}:{BRANCH}:{path}",
        "authority_score": authority,
        "refresh_ttl_days": TTL_DAYS,
    }


def _doc(name: str, path: str) -> dict[str, Any]:
    return _seed(name, path, DOC_AUTHORITY)


def _skill(name: str, path: str) -> dict[str, Any]:
    return _seed(name, path, SKILL_AUTHORITY)


_GS = "model-genie-skills"
_SK = f"{_GS}/skills"

VIBE_SEEDS: list[dict[str, Any]] = [
    # The agent: method, rules and quality gates.
    _doc("repository overview",            "README.md"),
    _doc("agent readme",                   "model-agent/readme.md"),
    _doc("whitepaper",                     "model-agent/docs/whitepaper.md"),
    _doc("design guide",                   "model-agent/docs/design-guide.md"),
    _doc("integration guide",              "model-agent/docs/integration-guide.md"),
    _doc("quality gates",                  "model-agent/docs/quality-gates.md"),
    _doc("rule catalog",                   "model-agent/rules/vibe-data-modelling-rules.csv"),

    # The app's modeling guidance (setup pages are left out).
    _doc("describing a business",          "model-app/docs/Describing-a-business.md"),
    _doc("model conventions",              "model-app/docs/conventions.md"),

    # Data Integration Process: Genie Code skills that fit a model to real data.
    _skill("Genie skills overview",                 f"{_GS}/README.md"),
    _skill("developer docs index",                  f"{_GS}/docs/developer/index.md"),
    _skill("developer explanation",                 f"{_GS}/docs/developer/explanation.md"),
    _skill("developer tutorial",                    f"{_GS}/docs/developer/tutorial.md"),
    _skill("developer workflow",                    f"{_GS}/docs/developer/workflow.md"),
    _skill("developer reference",                   f"{_GS}/docs/developer/reference.md"),
    _skill("how to add a table",                    f"{_GS}/docs/developer/how-to/add-a-table.md"),
    _skill("how to decide before the build",        f"{_GS}/docs/developer/how-to/decide-before-the-build.md"),
    _skill("how to extend to gold",                 f"{_GS}/docs/developer/how-to/extend-to-gold.md"),
    _skill("how to fill out conventions",           f"{_GS}/docs/developer/how-to/fill-out-conventions.md"),
    _skill("how to prepare assessment inputs",      f"{_GS}/docs/developer/how-to/prepare-assessment-inputs.md"),
    _skill("how to query the model",                f"{_GS}/docs/developer/how-to/query-the-model.md"),

    _skill("domain model assessment skill",         f"{_SK}/domain-model-assessment/SKILL.md"),
    _skill("discovery protocol",                    f"{_SK}/domain-model-assessment/discovery-protocol.md"),
    _skill("source-to-target mapping",              f"{_SK}/domain-model-assessment/source-to-target-mapping.md"),
    _skill("model completeness protocol",           f"{_SK}/domain-model-assessment/model-completeness-protocol.md"),
    _skill("existing model assessment",             f"{_SK}/domain-model-assessment/existing-model-assessment.md"),
    _skill("gold derivation protocol",              f"{_SK}/domain-model-assessment/gold-derivation-protocol.md"),
    _skill("ETL development framework skill",       f"{_SK}/etl-development-framework/SKILL.md"),
    _skill("DDL and modeling",                      f"{_SK}/etl-development-framework/ddl-and-modeling.md"),
    _skill("naming standards",                      f"{_SK}/etl-development-framework/naming-standards.md"),
    _skill("merge and defensive coding",            f"{_SK}/etl-development-framework/merge-and-defensive-coding.md"),
    _skill("gold and metrics",                      f"{_SK}/etl-development-framework/gold-and-metrics.md"),
    _skill("gold star schema in SDP",               f"{_SK}/etl-development-framework/sdp-gold-star.md"),
    _skill("domain model validation skill",         f"{_SK}/domain-model-validation/SKILL.md"),
    _skill("regression and drift",                  f"{_SK}/domain-model-validation/regression-and-drift.md"),
    _skill("domain documentation skill",            f"{_SK}/domain-documentation/SKILL.md"),
    _skill("Genie space configuration",             f"{_SK}/domain-documentation/genie-space-config.md"),
    _skill("domain sync skill",                     f"{_SK}/domain-sync/SKILL.md"),
    _skill("autonomous validation skill",           f"{_SK}/autonomous-validation/SKILL.md"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.dry_run:
        for seed in VIBE_SEEDS:
            LOG.info("would seed %-10s %-90s auth=%.2f ttl=%dd",
                     seed["source_type"], seed["identifier"],
                     seed["authority_score"], seed["refresh_ttl_days"])
        LOG.info("dry-run: %d seeds", len(VIBE_SEEDS))
        return 0

    import duckdb
    from seed_doc_sources import _upsert

    conn = duckdb.connect(str(args.catalog))
    try:
        conn.execute("BEGIN")
        counts = {"inserted": 0, "updated": 0}
        for seed in VIBE_SEEDS:
            action = _upsert(conn, seed)
            counts[action] += 1
            LOG.info("%s %-10s %s", action.upper(), seed["source_type"], seed["identifier"])
        conn.execute("COMMIT")
        LOG.info("done: %d inserted, %d updated (%d total)",
                 counts["inserted"], counts["updated"],
                 counts["inserted"] + counts["updated"])
        rows = conn.execute(
            "SELECT doc_source_id, name FROM doc_source "
            "WHERE source_type = 'github_raw' AND identifier LIKE ? "
            "ORDER BY doc_source_id", [f"{REPO}:%"]).fetchall()
        for sid, name in rows:
            LOG.info("  #%d %s", sid, name)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
