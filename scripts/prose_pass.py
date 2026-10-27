#!/usr/bin/env python3
"""
prose_pass.py — Sub-agent prose layer for Dialog and Tutorial packages.

The deterministic generators get the structure right but the voice wrong:
every Dialog beat gives the responder the same templated sentence, and
Tutorial stages are bare headings over procedure steps. This pass hands
each package's main file to a sub-agent together with evidence pulled
from the catalog (chapter and doc-section excerpts for every beat or
stage concept) and asks for grounded prose — the same prep → sub-agent →
process shape Bootstrap and extraction already use.

    prep     --catalog <clone>.ddb --generator dialog|tutorial
             --package <name> --output-dir <out>
             [--package-root data/generated-packages]
             Read-only. Writes <out>/prompt.txt + manifest.json.
    (Claude Code: one Task sub-agent reads prompt.txt and writes
     <out>/result.md — subscription, never the paid API.)
    process  --output-dir <out>
             Checks the result (headings kept, citations only to the
             evidence ids it was given), saves the deterministic file
             as _deterministic/<file>, and writes the new one in place.

No catalog writes: generated_file keeps the deterministic version, so
re-materializing the package restores it.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
DEFAULT_PACKAGE_ROOT = PROJECT_ROOT / "data" / "generated-packages"
EXCERPT_CHARS = 700
EVIDENCE_PER_KIND = 2

TARGETS = {
    # generator_type: (main file, unit_type, how to read concept ids)
    "dialog": ("dialogue.md", "dialog_beat", lambda m: [m.get("concept_id")]),
    "tutorial": ("tutorial.md", "tutorial_stage", lambda m: list(m.get("concept_ids") or [])[:3]),
}

_CITE_RE = re.compile(r"\[(ch|ds):(\d+)\]")

DIALOG_BRIEF = """Rewrite this dialogue so each line sounds like a person making a specific point.

The current draft is template output: one character recites the concept's definition and the other answers with the same stock sentence every time. Keep the cast, the title, the bios and every "## Beat N: <concept>" heading exactly. Under each heading write 2-4 exchanges.

Grounding:
  * {char_a} argues from the BOOK excerpts, {char_b} from the DOC excerpts listed for that beat (if the roles in the draft are reversed, follow the draft's bios).
  * Every substantive claim cites its excerpt inline as [ch:ID] or [ds:ID], using only the ids listed for that beat.
  * If a beat has no evidence on one side, that character asks a question or concedes instead of asserting.
  * No claims beyond the excerpts. No new beats. No closing summary.
"""

TUTORIAL_BRIEF = """Add a prose layer to this tutorial without changing its exercises.

Keep the title, the "How this tutorial works" section and every "## Stage N: <title>" and "### Exercise: <name>" heading exactly, and keep each exercise's numbered steps verbatim.

For each stage, directly under its "## Stage" heading, write:
  1. "**Why this stage:**" — 2-3 sentences on what this stage's concepts are and how they lead toward the tutorial's target, drawn from the stage's excerpts.
  2. After the exercise (or the conceptual-review note), "**Check yourself:**" — one question a reader can answer only if they did the stage.

Grounding: cite excerpts inline as [ch:ID] or [ds:ID], only from the ids listed for that stage. If a stage's concepts look unrelated to the target, say so in one sentence rather than inventing a connection.
"""


def _excerpt(text: str, concept: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    i = text.lower().find((concept or "").lower())
    start = max(0, i - EXCERPT_CHARS // 3) if i >= 0 else 0
    return text[start: start + EXCERPT_CHARS]


def gather_evidence(conn: duckdb.DuckDBPyConnection, concept_id: int) -> dict:
    name = conn.execute("SELECT name FROM concept WHERE concept_id = ?", [concept_id]).fetchone()
    name = name[0] if name else ""
    chapters = conn.execute(
        """
        SELECT ch.chapter_id, b.title, ch.title, ch.content, COUNT(*) AS n
          FROM concept_relation cr
          JOIN chapter ch ON ch.chapter_id = cr.source_id
          JOIN book b     ON b.book_id = ch.book_id
         WHERE cr.source_type = 'chapter'
           AND ? IN (cr.from_concept_id, cr.to_concept_id)
         GROUP BY ALL ORDER BY n DESC, ch.chapter_id LIMIT ?
        """, [concept_id, EVIDENCE_PER_KIND]).fetchall()
    sections = conn.execute(
        """
        SELECT ds.doc_section_id, src.name, ds.heading_text, ds.content, COUNT(*) AS n
          FROM concept_relation cr
          JOIN doc_section ds  ON ds.doc_section_id = cr.source_id
          JOIN doc_snapshot sn ON sn.snapshot_id = ds.snapshot_id
          JOIN doc_source src  ON src.doc_source_id = sn.doc_source_id
         WHERE cr.source_type = 'doc_section'
           AND ? IN (cr.from_concept_id, cr.to_concept_id)
         GROUP BY ALL ORDER BY n DESC, ds.doc_section_id LIMIT ?
        """, [concept_id, EVIDENCE_PER_KIND]).fetchall()
    return {
        "concept_id": concept_id, "concept_name": name,
        "book": [{"id": f"ch:{r[0]}", "where": f"{r[1]} — {r[2] or 'chapter'}",
                  "text": _excerpt(r[3], name)} for r in chapters],
        "doc": [{"id": f"ds:{r[0]}", "where": f"{r[1]} — {r[2] or 'section'}",
                 "text": _excerpt(r[3], name)} for r in sections],
    }


def build_prompt(generator: str, draft: str, units: list[dict], chars: tuple[str, str]) -> str:
    brief = (DIALOG_BRIEF.format(char_a=chars[0], char_b=chars[1])
             if generator == "dialog" else TUTORIAL_BRIEF)
    parts = [brief, "", "=== EVIDENCE ==="]
    for u in units:
        parts.append(f"\n--- {u['heading']} ---")
        for ev in u["evidence"]:
            parts.append(f"Concept: {ev['concept_name']}")
            for kind in ("book", "doc"):
                for x in ev[kind]:
                    parts.append(f"[{x['id']}] ({kind.upper()}: {x['where']}) {x['text']}")
            if not ev["book"] and not ev["doc"]:
                parts.append("(no excerpts in the catalog)")
    parts += ["", "=== CURRENT DRAFT ===", draft, "",
              "Write ONLY the full revised markdown file — no preamble, no fences."]
    return "\n".join(parts)


def do_prep(args: argparse.Namespace) -> int:
    main_file, unit_type, ids_of = TARGETS[args.generator]
    conn = duckdb.connect(str(args.catalog), read_only=True)
    pkg = conn.execute(
        "SELECT package_id, metadata_json FROM generated_package "
        " WHERE generator_type = ? AND name = ?", [args.generator, args.package]).fetchone()
    if pkg is None:
        print(f"no {args.generator} package named {args.package!r}", file=sys.stderr)
        return 2
    pkg_meta = json.loads(pkg[1] or "{}")
    rows = conn.execute(
        "SELECT name, ordinal, metadata_json FROM generated_unit "
        " WHERE package_id = ? AND unit_type = ? ORDER BY ordinal",
        [pkg[0], unit_type]).fetchall()
    units = []
    allowed: list[str] = []
    for name, ordinal, meta_json in rows:
        meta = json.loads(meta_json or "{}")
        evidence = [gather_evidence(conn, int(c)) for c in ids_of(meta) if c is not None]
        heading = (f"Beat {ordinal}: {name}" if args.generator == "dialog" else name)
        units.append({"heading": heading, "evidence": evidence})
        allowed += [x["id"] for ev in evidence for k in ("book", "doc") for x in ev[k]]
    conn.close()

    pkg_dir = args.package_root / args.package
    target = pkg_dir / main_file
    if not target.is_file():
        print(f"{target} not found — materialize the package first", file=sys.stderr)
        return 2
    draft = target.read_text()
    chars = (pkg_meta.get("char_a", "Architect"), pkg_meta.get("char_b", "Practitioner"))

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(build_prompt(args.generator, draft, units, chars))
    (out / "manifest.json").write_text(json.dumps({
        "generator": args.generator, "package": args.package,
        "target_file": str(target), "result_path": str(out / "result.md"),
        "headings": re.findall(r"^#{1,3} .+$", draft, flags=re.M),
        "allowed_citations": sorted(set(allowed)),
    }, indent=2))
    n_ev = len(set(allowed))
    print(f"prompt for {len(units)} unit(s), {n_ev} evidence excerpt(s): {out / 'prompt.txt'}")
    print(f"sub-agent writes: {out / 'result.md'}")
    return 0


def check_result(text: str, manifest: dict) -> list[str]:
    problems = []
    for h in manifest["headings"]:
        if h.startswith("### Exercise") or h.startswith("## ") or h.startswith("# "):
            if h not in text:
                problems.append(f"missing heading: {h}")
    allowed = set(manifest["allowed_citations"])
    bad = sorted({f"{k}:{i}" for k, i in _CITE_RE.findall(text)} - allowed)
    if bad:
        problems.append("citations not in the evidence: " + ", ".join(bad))
    if text.lstrip().startswith("```"):
        problems.append("result is wrapped in a code fence")
    return problems


def do_process(args: argparse.Namespace) -> int:
    out: Path = args.output_dir
    manifest = json.loads((out / "manifest.json").read_text())
    result = Path(manifest["result_path"])
    if not result.is_file():
        print(f"no result yet at {result}", file=sys.stderr)
        return 1
    text = result.read_text()
    problems = check_result(text, manifest)
    if problems and not args.force:
        print("result rejected:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    target = Path(manifest["target_file"])
    backup = target.parent / "_deterministic" / target.name
    backup.parent.mkdir(exist_ok=True)
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(text.rstrip() + "\n")
    cites = len(_CITE_RE.findall(text))
    print(f"wrote {target} ({cites} citation(s)); deterministic copy at {backup}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--generator", choices=sorted(TARGETS), required=True)
    p.add_argument("--package", required=True)
    p.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    p.add_argument("--output-dir", type=Path, required=True)
    q = sub.add_parser("process")
    q.add_argument("--output-dir", type=Path, required=True)
    q.add_argument("--force", action="store_true", help="write even if checks fail")
    args = parser.parse_args(argv)
    return do_prep(args) if args.cmd == "prep" else do_process(args)


if __name__ == "__main__":
    sys.exit(main())
