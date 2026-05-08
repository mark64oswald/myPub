#!/usr/bin/env python3
"""One-shot targeted ingestion for recovered chapters.

Reads each recovery state file, iterates the custom_ids, parses each
result_<id>.json, and calls process_extraction_json (concepts) or
its procedure equivalent. Skips chapters whose result still doesn't
parse — those need a second-pass recovery with higher max_tokens.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "kb-mcp"))

from extract_entities import (  # noqa: E402  # pylint: disable=wrong-import-position
    process_extraction_json,
    parse_llm_json,
)
from extract_procedures import (  # noqa: E402  # pylint: disable=wrong-import-position
    process_extraction_json as process_procedure_json,
)
from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position
from db import open_catalog  # noqa: E402  # pylint: disable=wrong-import-position


def ingest_recovery(label, recovery_state_path, processor, resolver, conn):
    """Iterate one recovery state file and ingest parseable results."""
    print(f"=== {label} recovery ingestion ===", flush=True)
    state = json.loads(Path(recovery_state_path).read_text())
    cids = [int(c.split("-", 1)[1]) for c in state["batches"][0]["custom_ids"]]
    results_dir = Path(recovery_state_path).parent / "results"
    ingested = parse_fail = 0
    totals = {}
    for cid in cids:
        rp = results_dir / f"result_{cid}.json"
        try:
            raw = parse_llm_json(rp.read_text())
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            parse_fail += 1
            continue
        s = processor(conn, resolver, cid, raw)
        ingested += 1
        for attr in ("entities_extracted", "relations_written",
                     "procedures_written", "concept_links_written",
                     "pattern_links_written"):
            v = getattr(s, attr, 0)
            if v:
                totals[attr] = totals.get(attr, 0) + v
    print(f"  ingested {ingested}/{len(cids)}  (parse_fail still: {parse_fail})",
          flush=True)
    for k, v in sorted(totals.items()):
        print(f"  +{v:,} {k}", flush=True)


def main() -> int:
    """Run targeted ingestion for both concept and procedure recoveries."""
    conn = open_catalog(read_only=False)
    resolver = EntityResolver(conn)
    try:
        ingest_recovery(
            "concepts",
            "data/batch-runs/concepts-full-2026-05-07/batch_state.recovery.json",
            process_extraction_json,
            resolver,
            conn,
        )
        print()
        ingest_recovery(
            "procedures",
            "data/batch-runs/procedures-full-2026-05-07/batch_state.recovery.json",
            process_procedure_json,
            resolver,
            conn,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
