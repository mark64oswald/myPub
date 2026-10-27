#!/usr/bin/env python3
"""
prose_pass.py — Sub-agent prose layer for Dialog, Tutorial, Slide-deck
and Content Brief packages.

The deterministic generators get the structure right but the voice wrong:
every Dialog beat gives the responder the same templated sentence, and
Tutorial stages are bare headings over procedure steps. This pass hands
each package's main file to a sub-agent together with evidence pulled
from the catalog (chapter and doc-section excerpts for every beat or
stage concept) and asks for grounded prose — the same prep → sub-agent →
process shape Bootstrap and extraction already use.

    prep     --catalog <clone>.ddb
             --generator dialog|tutorial|slide_deck|content_brief
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
    # generator_type: (main file or glob, unit_type, how to read concept ids)
    "dialog": ("dialogue.md", "dialog_beat", lambda m: [m.get("concept_id")]),
    "tutorial": ("tutorial.md", "tutorial_stage", lambda m: list(m.get("concept_ids") or [])[:3]),
    "slide_deck": ("speaker-notes.md", "slide", lambda m: list(m.get("concept_ids") or [])[:3]),
    "content_brief": ("sections/*.md", "content_section", lambda m: [m.get("anchor_concept_id")]),
}

# Multi-file targets (content_brief sections) travel as one draft with a
# marker line before each file; process splits the result on the same markers.
_FILE_MARK = "<!-- file: {} -->"
_FILE_MARK_RE = re.compile(r"^<!-- file: (.+?) -->$", re.M)

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

SLIDES_BRIEF = """Rewrite these speaker notes so a presenter could deliver each slide from them.

The current notes are template output or empty. Keep the title and every "## Slide N — <title>" heading exactly, in order. Under each heading write 3-6 sentences the presenter would actually say: the point of the slide, one concrete detail or example, and a one-line bridge to the next slide.

Grounding:
  * Every factual claim cites its excerpt inline as [ch:ID] or [ds:ID], using only the ids listed for that slide.
  * A slide with no excerpts (title, agenda, Q&A) gets plain framing with no citations and no new claims.
  * No new slides, no bullet lists, no closing summary beyond the existing slides.
"""

BRIEF_BRIEF = """Turn each content-brief section into a first draft a writer can edit.

The draft below holds several files, each introduced by a "<!-- file: ... -->" line. Keep every marker line exactly, and keep each file's "# N. <title>" heading, its "**Thesis:**" line and its "## ..." headings and their lists.

For each section, directly after the "**Thesis:**" line (and the anchor line if there is one), add "## Draft" with 2-3 paragraphs that argue the thesis. Where the section has angle hints, take a position on one rather than listing them.

Grounding: cite excerpts inline as [ch:ID] or [ds:ID], only from the ids listed for that section. If the excerpts don't support the thesis, say so in one sentence instead of inventing support.
"""

BRIEFS = {"dialog": DIALOG_BRIEF, "tutorial": TUTORIAL_BRIEF,
          "slide_deck": SLIDES_BRIEF, "content_brief": BRIEF_BRIEF}


def _unit_heading(generator: str, ordinal: int, name: str) -> str:
    if generator == "dialog":
        return f"Beat {ordinal}: {name}"
    if generator == "slide_deck":
        return f"Slide {ordinal} — {name}"
    if generator == "content_brief":
        return f"{ordinal}. {name}"
    return name


def _target_files(pkg_dir: Path, pattern: str) -> list[Path]:
    if "*" not in pattern:
        return [pkg_dir / pattern]

    def order(p: Path):
        head = p.name.split("-", 1)[0]
        return (int(head) if head.isdigit() else 10**9, p.name)
    return sorted(pkg_dir.glob(pattern), key=order)


def _join_files(pkg_dir: Path, files: list[Path]) -> str:
    if len(files) == 1:
        return files[0].read_text()
    return "\n".join(_FILE_MARK.format(f.relative_to(pkg_dir).as_posix()) + "\n" + f.read_text()
                     for f in files)


def split_files(text: str) -> dict[str, str]:
    """{relative path: content} from a marked multi-file result."""
    marks = list(_FILE_MARK_RE.finditer(text))
    return {m.group(1): text[m.end(): (marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip("\n")
            for i, m in enumerate(marks)}


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
    brief = BRIEFS[generator]
    if generator == "dialog":
        brief = brief.format(char_a=chars[0], char_b=chars[1])
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
        heading = _unit_heading(args.generator, ordinal, name)
        units.append({"heading": heading, "evidence": evidence})
        allowed += [x["id"] for ev in evidence for k in ("book", "doc") for x in ev[k]]
    conn.close()

    pkg_dir = args.package_root / args.package
    files = _target_files(pkg_dir, main_file)
    if not files or not all(f.is_file() for f in files):
        print(f"{pkg_dir / main_file} not found — materialize the package first", file=sys.stderr)
        return 2
    draft = _join_files(pkg_dir, files)
    chars = (pkg_meta.get("char_a", "Architect"), pkg_meta.get("char_b", "Practitioner"))

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(build_prompt(args.generator, draft, units, chars))
    (out / "manifest.json").write_text(json.dumps({
        "generator": args.generator, "package": args.package,
        "package_dir": str(pkg_dir),
        "target_files": [f.relative_to(pkg_dir).as_posix() for f in files],
        "result_path": str(out / "result.md"),
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
    files = manifest.get("target_files") or []
    if len(files) > 1:
        missing = [f for f in files if f not in split_files(text)]
        if missing:
            problems.append("missing file marker(s): " + ", ".join(missing))
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
    if "target_file" in manifest:  # manifests written before multi-file support
        t = Path(manifest["target_file"])
        pkg_dir, files = t.parent, [t.name]
    else:
        pkg_dir, files = Path(manifest["package_dir"]), manifest["target_files"]
    parts = split_files(text) if len(files) > 1 else {files[0]: text}
    for rel in files:
        if rel not in parts:  # only reachable with --force
            print(f"skipped {rel}: no section for it in the result", file=sys.stderr)
            continue
        target = pkg_dir / rel
        backup = pkg_dir / "_deterministic" / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(target, backup)
        target.write_text(parts[rel].rstrip() + "\n")
    cites = len(_CITE_RE.findall(text))
    print(f"wrote {len(files)} file(s) under {pkg_dir} ({cites} citation(s)); "
          f"deterministic copies under {pkg_dir / '_deterministic'}")
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
