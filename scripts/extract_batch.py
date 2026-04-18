#!/usr/bin/env python3
"""
extract_batch.py — Batch coordinator for sub-agent-driven chapter extraction.

Designed for two use cases:
  (1) Sample runs — extract a specified set of chapters/books.
  (2) Full-corpus runs (Phase 2.4) — extract every chapter with content,
      resumable across many Claude Code sessions.

Architecture (same cost model as extract_entities.py): this script does
only I/O, DB reads/writes, and entity resolution. Sub-agents invoked
from Claude Code (Task tool) do the LLM reasoning. The script never
calls the Anthropic API.

Workflow (driven by Claude Code in the loop):

    1. `prep` writes prompt files and a manifest:
         scripts/extract_batch.py prep \\
             --books 348,389,361  \\
             --per-batch 5        \\
             --output-dir /tmp/mypub-extraction/session-YYYYMMDD
       Produces:
         <out>/prompts/prompt_<chapter_id>.txt     one per chapter
         <out>/manifest.json                         batches + metadata

    2. Claude Code dispatches one sub-agent per batch, handing it the
       list of (prompt_path, result_path) pairs. Each sub-agent writes
       result_<chapter_id>.json under <out>/results/.

    3. `process` reads the manifest, feeds each result through
       EntityResolver + concept_relation writes, and reports stats:
         scripts/extract_batch.py process \\
             --output-dir /tmp/mypub-extraction/session-YYYYMMDD

`status` reports per-book extraction progress; use this at session start
to know what's outstanding and at session end to know what's been
completed.

Modes:
  prep     — enumerate chapters, write prompts, emit manifest
  process  — ingest sub-agent results into the catalog
  status   — report extraction coverage (chapters extracted vs. total)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

# These are siblings under scripts/.
from extract_entities import (  # noqa: E402  # pylint: disable=wrong-import-position
    build_full_prompt,
    parse_llm_json,
    process_extraction_json,
    _load_chapter,  # pylint: disable=protected-access
)
from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("extract_batch")


# ----------------------------------------------------------------------------
# Manifest model
# ----------------------------------------------------------------------------

@dataclass
class ChapterEntry:
    chapter_id: int
    book_id: int
    book_title: str
    chapter_title: Optional[str]
    prompt_path: str
    result_path: str


@dataclass
class Manifest:
    output_dir: str
    created_at: str
    per_batch: int
    chapters: list[ChapterEntry]
    batches: list[list[int]]  # each batch is a list of chapter_ids

    def to_dict(self) -> dict:
        return {
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "per_batch": self.per_batch,
            "chapters": [asdict(c) for c in self.chapters],
            "batches": self.batches,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            output_dir=d["output_dir"],
            created_at=d["created_at"],
            per_batch=d["per_batch"],
            chapters=[ChapterEntry(**c) for c in d["chapters"]],
            batches=[list(b) for b in d["batches"]],
        )


# ----------------------------------------------------------------------------
# Target selection
# ----------------------------------------------------------------------------

def _select_chapters(
    conn: duckdb.DuckDBPyConnection,
    *,
    book_ids: Optional[list[int]],
    chapter_ids: Optional[list[int]],
    skip_extracted: bool,
    limit: Optional[int],
    min_content_chars: int,
) -> list[tuple[int, int, str, Optional[str]]]:
    """Return (chapter_id, book_id, book_title, chapter_title) for targets.

    Targets = chapters with content, matching --books or --chapter-ids if
    provided (else all), optionally excluding chapters that already have
    at least one concept_relation row.
    """
    where = ["ch.content IS NOT NULL", f"LENGTH(ch.content) >= {int(min_content_chars)}"]
    params: list = []
    if chapter_ids:
        placeholders = ",".join("?" * len(chapter_ids))
        where.append(f"ch.chapter_id IN ({placeholders})")
        params.extend(chapter_ids)
    if book_ids:
        placeholders = ",".join("?" * len(book_ids))
        where.append(f"ch.book_id IN ({placeholders})")
        params.extend(book_ids)
    if skip_extracted:
        where.append(
            "NOT EXISTS (SELECT 1 FROM concept_relation cr "
            "WHERE cr.source_type='chapter' AND cr.source_id = ch.chapter_id)"
        )
    where_sql = " AND ".join(where)
    sql = (
        "SELECT ch.chapter_id, ch.book_id, b.title, ch.title "
        "  FROM chapter ch JOIN book b ON ch.book_id = b.book_id "
        f" WHERE {where_sql} "
        " ORDER BY ch.book_id, ch.chapter_num, ch.chapter_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


# ----------------------------------------------------------------------------
# Prep mode
# ----------------------------------------------------------------------------

def do_prep(conn: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    """Write prompt files and the manifest for the selected chapters."""
    output_dir: Path = args.output_dir
    prompts_dir = output_dir / "prompts"
    results_dir = output_dir / "results"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    chapters = _select_chapters(
        conn,
        book_ids=args.books,
        chapter_ids=args.chapter_ids,
        skip_extracted=args.skip_extracted,
        limit=args.limit,
        min_content_chars=args.min_content_chars,
    )
    if not chapters:
        LOG.info("no chapters matched selection")
        return 0

    entries: list[ChapterEntry] = []
    for cid, bid, book_title, chap_title in chapters:
        prompt_path = prompts_dir / f"prompt_{cid}.txt"
        result_path = results_dir / f"result_{cid}.json"
        # Fetch the chapter record and build the prompt.
        chapter = _load_chapter(conn, cid)
        prompt_path.write_text(build_full_prompt(chapter))
        entries.append(ChapterEntry(
            chapter_id=cid,
            book_id=bid,
            book_title=book_title,
            chapter_title=chap_title,
            prompt_path=str(prompt_path),
            result_path=str(result_path),
        ))

    # Group into batches of N, preserving order.
    per_batch = max(1, int(args.per_batch))
    batches: list[list[int]] = []
    for i in range(0, len(entries), per_batch):
        batches.append([e.chapter_id for e in entries[i : i + per_batch]])

    import datetime  # pylint: disable=import-outside-toplevel
    manifest = Manifest(
        output_dir=str(output_dir),
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        per_batch=per_batch,
        chapters=entries,
        batches=batches,
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2))

    LOG.info("prepped %d chapters in %d batch(es) of up to %d",
             len(entries), len(batches), per_batch)
    LOG.info("manifest: %s", manifest_path)
    LOG.info("prompts : %s/ (one per chapter)", prompts_dir)
    LOG.info("results : %s/ (sub-agents write here)", results_dir)

    # Emit a compact batch-by-batch summary so a driver can dispatch them.
    print()
    print("=== batches ===")
    for i, batch in enumerate(batches, 1):
        chap_names = [
            f"{e.chapter_id} ({e.book_title[:25]}: {(e.chapter_title or '')[:25]})"
            for e in entries if e.chapter_id in batch
        ]
        print(f"batch {i} ({len(batch)}): {', '.join(chap_names)}")
    return 0


# ----------------------------------------------------------------------------
# Process mode
# ----------------------------------------------------------------------------

@dataclass
class BatchProcessSummary:
    processed: int
    skipped_missing: int
    entities_total: int
    relations_total: int
    by_resolution: dict[str, int]


def do_process(conn: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    """Process every result file referenced in the manifest."""
    manifest_path = args.manifest or (args.output_dir / "manifest.json")
    if not manifest_path.exists():
        LOG.error("manifest not found: %s", manifest_path)
        return 2
    manifest = Manifest.from_dict(json.loads(manifest_path.read_text()))
    LOG.info("manifest: %d chapters in %d batch(es)",
             len(manifest.chapters), len(manifest.batches))

    resolver = EntityResolver(conn)
    summary = BatchProcessSummary(
        processed=0,
        skipped_missing=0,
        entities_total=0,
        relations_total=0,
        by_resolution={},
    )

    for entry in manifest.chapters:
        result_path = Path(entry.result_path)
        if not result_path.exists():
            LOG.warning("pending (no result yet): chapter %d → %s",
                        entry.chapter_id, result_path.name)
            summary.skipped_missing += 1
            continue
        try:
            raw = parse_llm_json(result_path.read_text())
        except json.JSONDecodeError as exc:
            LOG.error("bad JSON in %s: %s", result_path, exc)
            summary.skipped_missing += 1
            continue
        s = process_extraction_json(conn, resolver, entry.chapter_id, raw)
        summary.processed += 1
        summary.entities_total += s.entities_extracted
        summary.relations_total += s.relations_written
        for rtype, n in s.entities_by_resolution.items():
            summary.by_resolution[rtype] = summary.by_resolution.get(rtype, 0) + n
        LOG.info("ch %d: %d entities, %d relations written",
                 entry.chapter_id, s.entities_extracted, s.relations_written)

    print()
    print("=== batch process summary ===")
    print(f"processed:            {summary.processed}")
    print(f"missing result files: {summary.skipped_missing}")
    print(f"entities total:       {summary.entities_total}")
    print(f"relations written:    {summary.relations_total}")
    if summary.by_resolution:
        print("resolution counts:")
        for rtype, n in sorted(summary.by_resolution.items()):
            print(f"  {rtype:<16} {n}")
    return 0 if summary.processed > 0 or summary.skipped_missing == 0 else 1


# ----------------------------------------------------------------------------
# Status mode
# ----------------------------------------------------------------------------

def do_status(conn: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    """Report per-book extraction coverage."""
    where = ["ch.content IS NOT NULL"]
    params: list = []
    if args.books:
        placeholders = ",".join("?" * len(args.books))
        where.append(f"ch.book_id IN ({placeholders})")
        params.extend(args.books)
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT b.book_id, b.title,
               COUNT(*) AS ch_with_content,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM concept_relation cr
                    WHERE cr.source_type='chapter' AND cr.source_id = ch.chapter_id
               ) THEN 1 ELSE 0 END) AS ch_extracted
          FROM chapter ch JOIN book b ON ch.book_id = b.book_id
         WHERE {where_sql}
         GROUP BY b.book_id, b.title
         ORDER BY ch_extracted DESC, b.book_id
        """,
        params,
    ).fetchall()

    total_ch = sum(r[2] for r in rows)
    total_done = sum(r[3] for r in rows)
    print(f"=== extraction status ===")
    print(f"books in scope:      {len(rows)}")
    print(f"chapters w/ content: {total_ch}")
    print(f"chapters extracted:  {total_done}  ({100 * total_done / total_ch if total_ch else 0:.1f}%)")
    if args.verbose:
        print()
        print(f"{'book_id':>7}  {'done/total':>12}  title")
        for bid, title, total, done in rows:
            print(f"{bid:>7}  {done:>6}/{total:<5}  {title[:70]}")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _parse_id_list(spec: Optional[str]) -> Optional[list[int]]:
    if not spec:
        return None
    return [int(x) for x in spec.split(",") if x.strip()]


def main() -> int:
    """Parse args and dispatch one of the three subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)

    sub = parser.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prep",
                            help="write prompt files and manifest for sub-agent dispatch")
    p_prep.add_argument("--books", type=str, default=None,
                        help="comma-separated book_ids to target")
    p_prep.add_argument("--chapter-ids", type=str, default=None,
                        help="comma-separated chapter_ids to target")
    p_prep.add_argument("--limit", type=int, default=None)
    p_prep.add_argument("--per-batch", type=int, default=5,
                        help="chapters per sub-agent batch (default 5)")
    p_prep.add_argument("--output-dir", type=Path, required=True,
                        help="session directory under which prompts/ results/ land")
    p_prep.add_argument("--skip-extracted", action="store_true", default=True,
                        help="omit chapters that already have concept_relation rows")
    p_prep.add_argument("--include-extracted", dest="skip_extracted",
                        action="store_false",
                        help="reprocess chapters even if already extracted")
    p_prep.add_argument("--min-content-chars", type=int, default=500,
                        help="skip chapters with content shorter than this (default 500)")

    p_proc = sub.add_parser("process",
                            help="process result JSON files into the catalog")
    p_proc.add_argument("--manifest", type=Path, default=None)
    p_proc.add_argument("--output-dir", type=Path, default=None,
                        help="session directory (looks for manifest.json inside)")

    p_stat = sub.add_parser("status",
                            help="report per-book extraction coverage")
    p_stat.add_argument("--books", type=str, default=None,
                        help="comma-separated book_ids (default: all)")
    p_stat.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Normalize id-list args.
    if hasattr(args, "books"):
        args.books = _parse_id_list(args.books)
    if hasattr(args, "chapter_ids"):
        args.chapter_ids = _parse_id_list(args.chapter_ids)

    conn = duckdb.connect(str(args.catalog))
    try:
        if args.command == "prep":
            return do_prep(conn, args)
        if args.command == "process":
            if args.manifest is None and args.output_dir is None:
                LOG.error("provide --manifest or --output-dir")
                return 2
            return do_process(conn, args)
        if args.command == "status":
            return do_status(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
