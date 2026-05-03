#!/usr/bin/env python3
"""
dispatch_state.py — Streaming-dispatch state tracker for procedure-extraction sessions.

Replaces the wave-of-10 gating model with a "keep N in flight" pipeline:

    init   <session-dir> <total-batches> [--concurrency 10]
        Initialize state. Returns the initial set of batches to dispatch.

    next   <session-dir>
        Print the next batch index to dispatch, or "DONE" if all 50 already
        dispatched. Increments `next_pending` so subsequent calls return the
        next one.

    pairs  <session-dir> <batch-index>
        Print the (prompt_path, result_path) pairs for a given batch in the
        format expected by the sub-agent prompt (one pair per line).

    status <session-dir>
        Print active / pending / dispatched / total tallies.

    pending-count <session-dir>
        Print just the integer count of pending batches.

State file: <session-dir>/dispatch.state (JSON).
Concurrency control is the dispatcher's responsibility — this module only
tracks "what's been dispatched so far" and provides the next batch on demand.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _state_path(session_dir: Path) -> Path:
    return session_dir / "dispatch.state"


def _load(session_dir: Path) -> dict:
    return json.loads(_state_path(session_dir).read_text())


def _save(session_dir: Path, state: dict) -> None:
    _state_path(session_dir).write_text(json.dumps(state, indent=2))


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize state for a new session."""
    state = {
        "session_dir": str(args.session_dir),
        "total_batches": args.total_batches,
        "next_pending": args.concurrency + 1,  # batches 1..concurrency dispatched immediately
        "concurrency": args.concurrency,
        "completed_batches": [],
    }
    _save(args.session_dir, state)
    print(f"initialized: total={args.total_batches} concurrency={args.concurrency} "
          f"initial_dispatch=1..{args.concurrency} next_pending={state['next_pending']}")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """Atomically claim and print the next batch index to dispatch."""
    state = _load(args.session_dir)
    nb = state["next_pending"]
    if nb > state["total_batches"]:
        print("DONE")
        return 0
    state["next_pending"] = nb + 1
    _save(args.session_dir, state)
    print(nb)
    return 0


def cmd_pairs(args: argparse.Namespace) -> int:
    """Print the (prompt, result) pairs for a given batch from the manifest."""
    manifest = json.loads((args.session_dir / "manifest.json").read_text())
    chap_by_id = {c["chapter_id"]: c for c in manifest["chapters"]}
    if args.batch < 1 or args.batch > len(manifest["batches"]):
        print(f"ERROR: batch {args.batch} out of range 1..{len(manifest['batches'])}",
              file=sys.stderr)
        return 2
    cids = manifest["batches"][args.batch - 1]
    for cid in cids:
        c = chap_by_id[cid]
        print(f"  {c['prompt_path']} -> {c['result_path']}")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Record that a batch finished (best-effort, used for status only)."""
    state = _load(args.session_dir)
    if args.batch not in state["completed_batches"]:
        state["completed_batches"].append(args.batch)
    _save(args.session_dir, state)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = _load(args.session_dir)
    total = state["total_batches"]
    next_pending = state["next_pending"]
    dispatched = next_pending - 1
    completed = len(state["completed_batches"])
    print(f"total={total} dispatched={dispatched} completed={completed} "
          f"pending={total - dispatched} next_pending={next_pending}")
    return 0


def cmd_pending_count(args: argparse.Namespace) -> int:
    state = _load(args.session_dir)
    print(state["total_batches"] - (state["next_pending"] - 1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("session_dir", type=Path)
    p_init.add_argument("total_batches", type=int)
    p_init.add_argument("--concurrency", type=int, default=10)
    p_init.set_defaults(func=cmd_init)

    p_next = sub.add_parser("next")
    p_next.add_argument("session_dir", type=Path)
    p_next.set_defaults(func=cmd_next)

    p_pairs = sub.add_parser("pairs")
    p_pairs.add_argument("session_dir", type=Path)
    p_pairs.add_argument("batch", type=int)
    p_pairs.set_defaults(func=cmd_pairs)

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("session_dir", type=Path)
    p_complete.add_argument("batch", type=int)
    p_complete.set_defaults(func=cmd_complete)

    p_status = sub.add_parser("status")
    p_status.add_argument("session_dir", type=Path)
    p_status.set_defaults(func=cmd_status)

    p_pending = sub.add_parser("pending-count")
    p_pending.add_argument("session_dir", type=Path)
    p_pending.set_defaults(func=cmd_pending_count)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
