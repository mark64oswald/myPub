#!/usr/bin/env python3
"""
bootstrap_v2.py — Project Bootstrap v2 loop around a /kb-bootstrap package.

Subcommands::

    status   <project_dir>   JSON list of files still needing a sub-agent
                             (missing, placeholder, or retry) + the prompt
                             to hand each one
    validate <project_dir>   placeholder scan + per-stack tests + entry-point
                             smoke run; writes _validation.json/.md and
                             retry prompts for whatever failed
                             (exit 0 = pass, 1 = fail)

Driving the loop from Claude Code (subscription sub-agents, never the
paid API)::

    for round in 1..N:
        status   → for each pending file, spawn a Task sub-agent with the
                   prompt file; it writes ONLY that file's content
        validate → stop on pass; otherwise retry prompts are in
                   _sub_agent_prompts/retry/ and the next status lists them

Optional ``_smoke.json`` in the project root: {"expect_stdout": "..."}
makes the smoke step check the entry point's output, not just exit 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "kb-mcp"))

import bootstrap_runtime as br  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status")
    p_status.add_argument("project_dir", type=Path)
    p_val = sub.add_parser("validate")
    p_val.add_argument("project_dir", type=Path)
    p_val.add_argument("--stack", choices=sorted(br.STACK_RUNTIMES))
    p_val.add_argument("--timeout", type=int, default=300)
    p_val.add_argument("--skip-setup", action="store_true",
                       help="don't run npm/bundle install first")
    args = parser.parse_args(argv)

    if not args.project_dir.is_dir():
        parser.error(f"{args.project_dir} is not a directory")

    if args.cmd == "status":
        pending = br.dispatch_manifest(args.project_dir)
        print(json.dumps({
            "project_dir": str(args.project_dir),
            "stack": br.detect_stack(args.project_dir),
            "pending": [asdict(p) for p in pending],
        }, indent=2))
        return 0

    report = br.validate_project(
        args.project_dir, stack=args.stack, timeout=args.timeout,
        skip_setup=args.skip_setup,
    )
    retries = br.write_retry_prompts(args.project_dir, report)
    print(br.render_report_md(report))
    if retries:
        print(f"{len(retries)} retry prompt(s) in {br.RETRY_DIR}/")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
