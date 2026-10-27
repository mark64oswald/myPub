#!/usr/bin/env python3
"""
seed_healthsim.py — Seed HealthSim's format and reference guides as doc_sources (2026-10-27).

HealthSim (github.com/mark64oswald/healthsim-workspace) is Mark's synthetic
healthcare data project. myPub does not generate synthetic data, but
HealthSim's guides are hands-on companions to the standards the
scaffold/translate generators cite: loop and segment layouts with worked
examples for X12, HL7 v2, NCPDP D.0, FHIR R4, C-CDA and CDISC. For X12 and
NCPDP, which are licensed and reference-only here, they are the only
structural text the KB has.

One github_raw source per file, so the repo's plans, archives and demos
stay out of the index. Authority 0.55 sits below the official specs and
vendor docs: these are secondary summaries written for HealthSim, not
the standards themselves. data/standards/standards.yaml pins the format
guides under the matching standard with role ``guide``.

Idempotent: reuses ``seed_doc_sources._upsert``.

Usage:
    .venv/bin/python3 scripts/seed_healthsim.py --dry-run
    .venv/bin/python3 scripts/seed_healthsim.py
    .venv/bin/python3 scripts/refresh_docs.py refresh --source-id <id>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_doc_sources import _upsert  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("seed_healthsim")

REPO = "mark64oswald/healthsim-workspace"
BRANCH = "main"
AUTHORITY = 0.55
TTL_DAYS = 30


def _seed(name: str, path: str) -> dict[str, Any]:
    return {
        "name": f"HealthSim {name}",
        "source_type": "github_raw",
        "identifier": f"{REPO}:{BRANCH}:{path}",
        "authority_score": AUTHORITY,
        "refresh_ttl_days": TTL_DAYS,
    }


HEALTHSIM_SEEDS: list[dict[str, Any]] = [
    # Format guides: one per output standard HealthSim emits.
    _seed("X12 837 claim format guide",          "formats/x12-837.md"),
    _seed("X12 835 remittance format guide",     "formats/x12-835.md"),
    _seed("X12 834 enrollment format guide",     "formats/x12-834.md"),
    _seed("X12 270/271 eligibility format guide", "formats/x12-270-271.md"),
    _seed("HL7 v2 ADT format guide",             "formats/hl7v2-adt.md"),
    _seed("HL7 v2 ORM format guide",             "formats/hl7v2-orm.md"),
    _seed("HL7 v2 ORU format guide",             "formats/hl7v2-oru.md"),
    _seed("FHIR R4 format guide",                "formats/fhir-r4.md"),
    _seed("C-CDA format guide",                  "formats/ccda-format.md"),
    _seed("NCPDP D.0 format guide",              "formats/ncpdp-d0.md"),
    _seed("CDISC SDTM format guide",             "formats/cdisc-sdtm.md"),
    _seed("CDISC ADaM format guide",             "formats/cdisc-adam.md"),
    _seed("dimensional analytics format guide",  "formats/dimensional-analytics.md"),

    # Cross-standard references.
    _seed("code systems reference",              "references/code-systems.md"),
    _seed("terminology reference",               "references/terminology.md"),
    _seed("HL7 v2 segments reference",           "references/hl7v2-segments.md"),
    _seed("healthcare data models reference",    "references/data-models.md"),
    _seed("validation rules reference",          "references/validation-rules.md"),

    # How to consume HealthSim output (bootstrap / quickstart).
    _seed("integration guide",                   "docs/integration-guide.md"),
    _seed("DuckDB schema",                       "docs/healthsim-duckdb-schema.md"),
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
        for seed in HEALTHSIM_SEEDS:
            LOG.info("would seed %-10s %-60s  auth=%.2f  ttl=%dd",
                     seed["source_type"], seed["identifier"],
                     seed["authority_score"], seed["refresh_ttl_days"])
        LOG.info("dry-run: %d seeds", len(HEALTHSIM_SEEDS))
        return 0

    conn = duckdb.connect(str(args.catalog))
    try:
        conn.execute("BEGIN")
        counts = {"inserted": 0, "updated": 0}
        for seed in HEALTHSIM_SEEDS:
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
