#!/usr/bin/env python3
"""
standards_registry.py — load and inspect the healthcare standards registry.

    check                      Validate data/standards/standards.yaml. No DB.
    load   [--write]           Replace the standard* tables from the YAML.
                               Without --write it validates and prints counts.
    status [--standard KEY]    Show each registered source and whether it
                               resolves to a loaded doc_source. Read-only, so
                               it can run against an APFS clone.

Usage:
    .venv/bin/python3 scripts/standards_registry.py check
    .venv/bin/python3 scripts/standards_registry.py load --write
    .venv/bin/python3 scripts/standards_registry.py status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import standards  # noqa: E402

DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"


def _connect(path: Path, read_only: bool):
    import duckdb
    return duckdb.connect(str(path), read_only=read_only)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--registry", type=Path, default=standards.REGISTRY_FILE)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p_load = sub.add_parser("load")
    p_load.add_argument("--write", action="store_true")
    p_status = sub.add_parser("status")
    p_status.add_argument("--standard", action="append")
    args = p.parse_args(argv)

    reg = standards.read_registry(args.registry)

    if args.cmd in ("check", "load"):
        errors = standards.validate(reg)
        for e in errors:
            print(f"ERROR {e}")
        n_std = len(reg.get("standards") or [])
        n_src = sum(len(s.get("sources") or []) for s in reg.get("standards") or [])
        print(f"{n_std} standards, {n_src} sources, "
              f"{len(reg.get('relations') or [])} relations")
        if errors:
            return 1
        if args.cmd == "check" or not args.write:
            if args.cmd == "load":
                print("dry run; pass --write to load")
            return 0
        conn = _connect(args.catalog, read_only=False)
        try:
            print(f"loaded {standards.load(conn, reg)}")
        finally:
            conn.close()
        return 0

    conn = _connect(args.catalog, read_only=True)
    try:
        rows = standards.status(conn)
    finally:
        conn.close()
    if args.standard:
        rows = [r for r in rows if r["standard_key"] in set(args.standard)]
    current = None
    for r in rows:
        if r["standard_key"] != current:
            current = r["standard_key"]
            print(f"\n{current}  [{r['license_tier']}]")
        target = (f"#{r['doc_source_id']} {r['doc_source_name']} "
                  f"({r['n_sections']} sections)" if r["doc_source_id"] else "")
        print(f"  {r['role']:<8} {r['name']:<34} {r['state']:<24} {target}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
