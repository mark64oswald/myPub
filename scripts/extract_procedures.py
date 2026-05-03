#!/usr/bin/env python3
"""
extract_procedures.py — Coordinator for sub-agent-driven procedure extraction.

Mirrors scripts/extract_batch.py: same prep/process/status flow, same
manifest format, same dispatch model. The only differences are the
extraction prompt (targets step-by-step procedures, not concepts) and the
catalog writer (writes to `procedure` + `procedure_concept` and sets
`chapter.procedure_attempted_at`).

Architecture: this script does only I/O, DB reads/writes, and entity
resolution for concept references. Sub-agents invoked from Claude Code
(Task tool) do the LLM reasoning. The script never calls the Anthropic
API.

Workflow:

    1. `prep` writes prompt files and a manifest:
         scripts/extract_procedures.py prep \\
             --chapter-ids 12345,12346 \\
             --per-batch 5 \\
             --output-dir /tmp/mypub-procedures/session-YYYYMMDD

       Or for a small batch:
         scripts/extract_procedures.py prep \\
             --limit 50 \\
             --output-dir /tmp/mypub-procedures/session-YYYYMMDD

    2. Claude Code dispatches one sub-agent per batch with the list of
       (prompt_path, result_path) pairs. Each sub-agent writes
       result_<chapter_id>.json under <out>/results/.

    3. `process` reads the manifest, validates each result, links concept
       references via EntityResolver, and persists procedures:
         scripts/extract_procedures.py process \\
             --output-dir /tmp/mypub-procedures/session-YYYYMMDD

    4. `status` reports per-book procedure-extraction coverage.

Selection: a chapter is "done" once `procedure_attempted_at` is set OR a
sibling-by-content-hash has procedure rows. Front-matter chapters yield
empty procedure lists; the attempted_at marker keeps them from boomeranging
back into future sessions.
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

# Reuse chapter loading + JSON parsing helpers from extract_entities so the two
# coordinators stay aligned on payload shape and fence handling.
from extract_entities import (  # noqa: E402  # pylint: disable=wrong-import-position
    parse_llm_json,
    _load_chapter,  # pylint: disable=protected-access
)
from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("extract_procedures")

MAX_CONTENT_CHARS = 20000  # ~5K input tokens; matches extract_entities.


# ----------------------------------------------------------------------------
# Procedure-specific prompt
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You extract executable procedures from technical book chapters for a knowledge base. A procedure is step-by-step "how to do X" content — concrete actions a reader could follow, not explanations.

Return JSON only — no prose, no markdown fences.

For the chapter provided, identify zero or more PROCEDURES. Each procedure has:

  name              short imperative phrase, e.g., "Configure CDC for a Delta table"
  preconditions     prerequisites the reader needs in place (1-2 sentences)
  steps             ordered array of {n, action, command?, notes?} objects
                      action  — imperative sentence ("Enable change data feed on the source table")
                      command — literal command/code if the chapter shows one (optional, ≤200 chars)
                      notes   — short clarifying remark (optional)
  postconditions    what's true after the procedure succeeds (1-2 sentences)
  failure_modes     ways this can fail and how to recover (1-2 sentences); empty string if the chapter doesn't discuss failures
  concepts          list of concept names this procedure operates on, e.g., ["Change Data Capture", "Delta Lake"]
  implements_pattern  name of a single named Pattern this procedure realizes, or null

Rules:

  * Only extract concrete procedures the chapter actually walks through. Conceptual chapters that explain what something is — without telling you how to do it — produce zero procedures.
  * Steps must be ordered actions, not bullet points summarizing topics.
  * Never invent commands. If the chapter shows code, transcribe it verbatim (truncate to ~200 chars). If not, leave command absent.
  * A typical chapter has 0–3 procedures. More than 5 is suspicious — re-read and merge.
  * Skip front-matter (preface, TOC, index, copyright) — return {"procedures": []}.

Output JSON schema:

{
  "procedures": [
    {
      "name": "string",
      "preconditions": "string",
      "steps": [
        {"n": 1, "action": "string", "command": "optional string", "notes": "optional string"}
      ],
      "postconditions": "string",
      "failure_modes": "string",
      "concepts": ["string", "..."],
      "implements_pattern": "string or null"
    }
  ]
}"""


def _build_user_prompt(chapter) -> str:  # ChapterRecord; type omitted to avoid the import dance
    """Compose the per-chapter user payload (book/author/title + truncated content)."""
    content = chapter.content
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[...truncated for length]"
    authors_line = ", ".join(chapter.authors) if chapter.authors else "Unknown"
    return (
        f"Book: {chapter.book_title}\n"
        f"Authors: {authors_line}\n"
        f"Chapter: {chapter.title or '(untitled)'}\n"
        f"\n---\n\n"
        f"{content}"
    )


def build_full_prompt(chapter) -> str:
    """Self-contained sub-agent prompt: SYSTEM_PROMPT + chapter payload."""
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- CHAPTER TO EXTRACT ---\n\n"
        f"{_build_user_prompt(chapter)}\n\n"
        f"Respond with JSON only. No prose, no markdown fences."
    )


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def _coerce_step(raw_step) -> Optional[dict]:
    """Normalize one step entry; return None if the entry can't be salvaged."""
    if not isinstance(raw_step, dict):
        return None
    action = (raw_step.get("action") or "").strip()
    if not action:
        return None
    n = raw_step.get("n")
    try:
        n = int(n) if n is not None else None
    except (TypeError, ValueError):
        n = None
    out = {"n": n, "action": action}
    cmd = (raw_step.get("command") or "").strip()
    if cmd:
        out["command"] = cmd[:400]  # generous cap; prompt asks for ≤200
    notes = (raw_step.get("notes") or "").strip()
    if notes:
        out["notes"] = notes
    return out


def _validate_procedure(raw: dict) -> Optional[dict]:
    """Return a cleaned procedure dict, or None if it's unusable."""
    name = (raw.get("name") or "").strip()
    if not name:
        LOG.debug("skip procedure with empty name")
        return None

    raw_steps = raw.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        LOG.debug("skip procedure %r with no steps", name)
        return None
    steps: list[dict] = []
    for i, raw_step in enumerate(raw_steps, 1):
        step = _coerce_step(raw_step)
        if step is None:
            continue
        # Fill missing step numbers in stable order.
        if step.get("n") is None:
            step["n"] = i
        steps.append(step)
    if not steps:
        LOG.debug("skip procedure %r — all steps malformed", name)
        return None

    concepts_raw = raw.get("concepts") or []
    concepts: list[str] = []
    if isinstance(concepts_raw, list):
        for c in concepts_raw:
            if isinstance(c, str):
                cname = c.strip()
                if cname and cname not in concepts:
                    concepts.append(cname)

    pattern = raw.get("implements_pattern")
    if isinstance(pattern, str):
        pattern = pattern.strip() or None
    elif pattern is not None:
        pattern = None

    return {
        "name": name,
        "preconditions": (raw.get("preconditions") or "").strip(),
        "steps": steps,
        "postconditions": (raw.get("postconditions") or "").strip(),
        "failure_modes": (raw.get("failure_modes") or "").strip(),
        "concepts": concepts,
        "implements_pattern": pattern,
    }


def _validate_extraction(raw: dict) -> list[dict]:
    """Filter LLM output to well-formed procedures."""
    out: list[dict] = []
    for p in raw.get("procedures", []) or []:
        clean = _validate_procedure(p)
        if clean is not None:
            out.append(clean)
    return out


# ----------------------------------------------------------------------------
# DB writers
# ----------------------------------------------------------------------------

def _clear_prior_extraction(conn: duckdb.DuckDBPyConnection, chapter_id: int) -> int:
    """Remove `procedure_concept` rows then `procedure` rows from this chapter.

    Concepts and patterns themselves are left in place — they may be referenced
    by other chapters or by the concept graph.
    """
    proc_ids = [
        r[0] for r in conn.execute(
            "SELECT procedure_id FROM procedure "
            "WHERE source_type='chapter' AND source_id = ?",
            [chapter_id],
        ).fetchall()
    ]
    if not proc_ids:
        return 0
    placeholders = ",".join("?" * len(proc_ids))
    conn.execute(
        f"DELETE FROM procedure_concept WHERE procedure_id IN ({placeholders})",
        proc_ids,
    )
    conn.execute(
        f"DELETE FROM procedure WHERE procedure_id IN ({placeholders})",
        proc_ids,
    )
    return len(proc_ids)


def _resolve_pattern_id(
    resolver: EntityResolver, name: Optional[str], chapter_id: int
) -> Optional[int]:
    """Resolve a Pattern reference via EntityResolver; return concept_id or None."""
    if not name:
        return None
    result = resolver.resolve(
        name,
        candidate_context="implements pattern",
        concept_type="Pattern",
        source_type="chapter",
        source_id=chapter_id,
    )
    return result.concept_id


def _write_procedure(
    conn: duckdb.DuckDBPyConnection,
    chapter_id: int,
    proc: dict,
    pattern_id: Optional[int],
) -> int:
    """Insert one procedure row and return the new procedure_id."""
    row = conn.execute(
        """
        INSERT INTO procedure
            (name, preconditions, steps, postconditions, failure_modes,
             source_type, source_id, implements_pattern)
        VALUES (?, ?, ?, ?, ?, 'chapter', ?, ?)
        RETURNING procedure_id
        """,
        [
            proc["name"],
            proc["preconditions"] or None,
            json.dumps(proc["steps"], ensure_ascii=False),
            proc["postconditions"] or None,
            proc["failure_modes"] or None,
            chapter_id,
            pattern_id,
        ],
    ).fetchone()
    return row[0]


def _link_procedure_concept(
    conn: duckdb.DuckDBPyConnection, procedure_id: int, concept_id: int
) -> bool:
    """Insert a procedure_concept link; duplicates fail silently."""
    try:
        conn.execute(
            "INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
            [procedure_id, concept_id],
        )
        return True
    except duckdb.ConstraintException:
        return False


# ----------------------------------------------------------------------------
# Top-level orchestration
# ----------------------------------------------------------------------------

@dataclass
class ProcedureExtractionSummary:
    """Per-chapter persistence summary."""

    chapter_id: int
    procedures_extracted: int
    procedures_written: int
    concept_links_written: int
    pattern_links_written: int
    by_resolution: dict[str, int]
    prior_procedures_cleared: int


def process_extraction_json(
    conn: duckdb.DuckDBPyConnection,
    resolver: EntityResolver,
    chapter_id: int,
    raw: dict,
) -> ProcedureExtractionSummary:
    """Resolve concept references and persist procedures for one chapter."""
    # Ensure the chapter exists; raises if not.
    _load_chapter(conn, chapter_id)

    procedures = _validate_extraction(raw)
    LOG.info("validated %d procedure(s) for chapter %d", len(procedures), chapter_id)

    cleared = _clear_prior_extraction(conn, chapter_id)

    by_resolution: dict[str, int] = {}
    procs_written = 0
    concept_links = 0
    pattern_links = 0

    for proc in procedures:
        # Resolve concept references first; the resolver writes new concepts
        # immediately, so the procedure row can reference them.
        concept_ids: list[int] = []
        for cname in proc["concepts"]:
            result = resolver.resolve(
                cname,
                candidate_context=f"operates_on procedure: {proc['name']}",
                source_type="chapter",
                source_id=chapter_id,
            )
            concept_ids.append(result.concept_id)
            by_resolution[result.resolution_type] = (
                by_resolution.get(result.resolution_type, 0) + 1
            )

        pattern_id = _resolve_pattern_id(resolver, proc["implements_pattern"], chapter_id)
        if pattern_id is not None:
            # The pattern resolution counts toward by_resolution too — fetch its
            # type tag by re-resolving wouldn't be efficient, so we just bucket
            # it as 'pattern_link' for visibility.
            by_resolution["pattern_link"] = by_resolution.get("pattern_link", 0) + 1

        proc_id = _write_procedure(conn, chapter_id, proc, pattern_id)
        procs_written += 1
        if pattern_id is not None:
            pattern_links += 1
        for cid in concept_ids:
            if _link_procedure_concept(conn, proc_id, cid):
                concept_links += 1

    # Mark attempt so prep can skip even when extraction produced zero procedures.
    conn.execute(
        "UPDATE chapter SET procedure_attempted_at = CURRENT_TIMESTAMP "
        "WHERE chapter_id = ?",
        [chapter_id],
    )
    conn.commit()

    return ProcedureExtractionSummary(
        chapter_id=chapter_id,
        procedures_extracted=len(procedures),
        procedures_written=procs_written,
        concept_links_written=concept_links,
        pattern_links_written=pattern_links,
        by_resolution=by_resolution,
        prior_procedures_cleared=cleared,
    )


# ----------------------------------------------------------------------------
# Manifest model
# ----------------------------------------------------------------------------

@dataclass
class ChapterEntry:
    """One chapter-level entry in the extraction manifest."""

    chapter_id: int
    book_id: int
    book_title: str
    chapter_title: Optional[str]
    prompt_path: str
    result_path: str


@dataclass
class Manifest:
    """Session-level manifest: chapters to extract and their batch groupings."""

    output_dir: str
    created_at: str
    per_batch: int
    chapters: list[ChapterEntry]
    batches: list[list[int]]

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict."""
        return {
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "per_batch": self.per_batch,
            "chapters": [asdict(c) for c in self.chapters],
            "batches": self.batches,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        """Rehydrate from the JSON-friendly dict produced by to_dict()."""
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

# Front-matter titles that aren't worth dispatching. Two layers:
#   1) FRONT_MATTER_FILTER — exact-match list (kept for extract_batch.py parity)
#   2) FRONT_MATTER_REGEX — regex of patterns we observed in s7-s9 producing
#      consistent zero-procedure dispatches (Part N intros, Glossary, References,
#      Epilogue, Packt-style back-matter, etc.). Adding this layer lets each
#      session reach more real procedural content within the same dispatch budget.
FRONT_MATTER_FILTER = (
    "  'copyright', 'contents', 'table of contents', 'foreword', 'preface',"
    "  'acknowledgments', 'acknowledgements', 'dedication', 'colophon',"
    "  'index', 'index (1/2)', 'index (2/2)',"
    "  'about this book', 'about the author', 'about the authors',"
    "  'about the cover', 'about the cover illustration',"
    "  'disclaimer', 'legal notice', 'notice', 'errata',"
    "  'contact us', 'o''reilly online learning', 'using the examples',"
    "  'conventions used in this book'"
)

# Anchored regex patterns matched against LOWER(TRIM(title)). Single combined
# pattern alternation; DuckDB's REGEXP_MATCHES treats it as a partial-match
# search, so we anchor each alternative with ^ where appropriate.
FRONT_MATTER_REGEX = (
    r'^(part [ivxlcdm0-9]'         # "Part 1", "Part I", "Part II", ...
    r'|appendix [a-z]:.*(reference|glossary|bibliography|acronym)'  # ref/gloss appendices
    r'|glossary'                   # "Glossary", "Glossary of ..."
    r'|.*\bglossary$'              # "Acronyms Glossary", "Key Terms Glossary"
    r'|references$'                # exact
    r'|bibliography'
    r'|webliography'
    r'|epilogue'
    r'|other books'                # "Other Books You May Enjoy"
    r'|free benefits'
    r'|why subscribe'
    r'|unlock your'                # "Unlock Your Exclusive Benefits"
    r'|series editor'              # "Series Editor Foreword"
    r'|forewords?$'                # bare "Foreword" / "Forewords" (already in exact list, defensive)
    r'|audience and prerequisite'
    r'|who.*this book.*for'        # "Who this book is for", "Who's this book for?"
    r'|how to use this book'
    r'|^acronyms'
    r'|^key terms'
    r'|^brief table of contents'
    r'|^objective and approach'
    r'|^audience'
    r')'
)


def _select_chapters(
    conn: duckdb.DuckDBPyConnection,
    *,
    book_ids: Optional[list[int]],
    chapter_ids: Optional[list[int]],
    skip_attempted: bool,
    dedup_by_hash: bool,
    order_by: str,
    limit: Optional[int],
    min_content_chars: int,
) -> list[tuple[int, int, str, Optional[str]]]:
    """Return (chapter_id, book_id, book_title, chapter_title) for targets.

    Selection mirrors extract_batch._select_chapters except the "done"
    predicate uses procedure-extraction state:
      - chapter.procedure_attempted_at IS NOT NULL, OR
      - any sibling-by-hash has procedure rows.

    With dedup_by_hash=True, skip_attempted=True skips a hash if ANY sibling
    is already attempted (the same content shouldn't be re-extracted via a
    different TOC entry).
    """
    if order_by not in {"book_id", "title"}:
        raise ValueError(f"order_by must be 'book_id' or 'title', got {order_by!r}")

    base_where = [
        "ch.content IS NOT NULL",
        "ch.content_hash IS NOT NULL",
        f"LENGTH(ch.content) >= {int(min_content_chars)}",
        f"LOWER(TRIM(ch.title)) NOT IN ({FRONT_MATTER_FILTER})",
        f"NOT REGEXP_MATCHES(LOWER(TRIM(ch.title)), '{FRONT_MATTER_REGEX}')",
    ]
    params: list = []
    if chapter_ids:
        placeholders = ",".join("?" * len(chapter_ids))
        base_where.append(f"ch.chapter_id IN ({placeholders})")
        params.extend(chapter_ids)
    if book_ids:
        placeholders = ",".join("?" * len(book_ids))
        base_where.append(f"ch.book_id IN ({placeholders})")
        params.extend(book_ids)

    order_sql = (
        "ORDER BY b.title, ch.book_id, ch.chapter_num, ch.chapter_id"
        if order_by == "title"
        else "ORDER BY ch.book_id, ch.chapter_num, ch.chapter_id"
    )

    if not dedup_by_hash:
        where = list(base_where)
        if skip_attempted:
            where.append("ch.procedure_attempted_at IS NULL")
            where.append(
                "NOT EXISTS (SELECT 1 FROM procedure pr "
                "WHERE pr.source_type='chapter' AND pr.source_id = ch.chapter_id)"
            )
        where_sql = " AND ".join(where)
        sql = (
            "SELECT ch.chapter_id, ch.book_id, b.title, ch.title "
            "  FROM chapter ch JOIN book b ON ch.book_id = b.book_id "
            f" WHERE {where_sql} "
            f" {order_sql}"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return conn.execute(sql, params).fetchall()

    # dedup_by_hash=True: skip a content_hash if any sibling has been attempted
    # OR has procedure rows.
    where = list(base_where)
    if skip_attempted:
        where.append(
            "NOT EXISTS ("
            "  SELECT 1 FROM chapter sib "
            "  WHERE sib.content_hash = ch.content_hash "
            "    AND (sib.procedure_attempted_at IS NOT NULL "
            "      OR EXISTS (SELECT 1 FROM procedure pr "
            "                   WHERE pr.source_type='chapter' "
            "                     AND pr.source_id = sib.chapter_id))"
            ")"
        )
    where_sql = " AND ".join(where)
    sql = (
        "WITH rep AS ("
        "  SELECT MIN(ch.chapter_id) AS chapter_id, ch.book_id, ch.content_hash "
        "    FROM chapter ch "
        f"   WHERE {where_sql} "
        "    GROUP BY ch.book_id, ch.content_hash "
        ") "
        "SELECT rep.chapter_id, rep.book_id, b.title, ch.title "
        "  FROM rep JOIN chapter ch USING (chapter_id) "
        "  JOIN book b ON rep.book_id = b.book_id "
        f" {order_sql}"
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
        skip_attempted=args.skip_attempted,
        dedup_by_hash=args.dedup_by_hash,
        order_by=args.order_by,
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
    LOG.info("prompts : %s/", prompts_dir)
    LOG.info("results : %s/", results_dir)

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
    """Aggregate results of one `process` invocation over a manifest."""

    processed: int
    skipped_missing: int
    procedures_total: int
    concept_links_total: int
    pattern_links_total: int
    chapters_with_zero: int
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
        procedures_total=0,
        concept_links_total=0,
        pattern_links_total=0,
        chapters_with_zero=0,
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
        summary.procedures_total += s.procedures_written
        summary.concept_links_total += s.concept_links_written
        summary.pattern_links_total += s.pattern_links_written
        if s.procedures_written == 0:
            summary.chapters_with_zero += 1
        for rtype, n in s.by_resolution.items():
            summary.by_resolution[rtype] = summary.by_resolution.get(rtype, 0) + n
        LOG.info("ch %d: %d procedures, %d concept links, %d pattern links",
                 entry.chapter_id, s.procedures_written,
                 s.concept_links_written, s.pattern_links_written)

    print()
    print("=== batch process summary ===")
    print(f"processed:            {summary.processed}")
    print(f"missing result files: {summary.skipped_missing}")
    print(f"procedures written:   {summary.procedures_total}")
    print(f"concept links:        {summary.concept_links_total}")
    print(f"pattern links:        {summary.pattern_links_total}")
    print(f"chapters w/ 0 procs:  {summary.chapters_with_zero}")
    if summary.by_resolution:
        print("resolution counts:")
        for rtype, n in sorted(summary.by_resolution.items()):
            print(f"  {rtype:<16} {n}")
    return 0 if summary.processed > 0 or summary.skipped_missing == 0 else 1


# ----------------------------------------------------------------------------
# Status mode
# ----------------------------------------------------------------------------

def do_status(conn: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    """Report per-book procedure-extraction coverage."""
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
               SUM(CASE WHEN ch.procedure_attempted_at IS NOT NULL
                         OR EXISTS (
                             SELECT 1 FROM procedure pr
                              WHERE pr.source_type='chapter'
                                AND pr.source_id = ch.chapter_id)
                        THEN 1 ELSE 0 END) AS ch_attempted,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM procedure pr
                    WHERE pr.source_type='chapter' AND pr.source_id = ch.chapter_id
               ) THEN 1 ELSE 0 END) AS ch_with_procedures
          FROM chapter ch JOIN book b ON ch.book_id = b.book_id
         WHERE {where_sql}
         GROUP BY b.book_id, b.title
         ORDER BY ch_attempted DESC, b.book_id
        """,
        params,
    ).fetchall()

    total_ch = sum(r[2] for r in rows)
    total_attempted = sum(r[3] for r in rows)
    total_with_proc = sum(r[4] for r in rows)
    pct_attempted = (100 * total_attempted / total_ch) if total_ch else 0.0
    total_procs = conn.execute("SELECT COUNT(*) FROM procedure").fetchone()[0]
    total_links = conn.execute("SELECT COUNT(*) FROM procedure_concept").fetchone()[0]

    print("=== procedure extraction status ===")
    print(f"books in scope:           {len(rows)}")
    print(f"chapters w/ content:      {total_ch}")
    print(f"chapters attempted:       {total_attempted}  ({pct_attempted:.1f}%)")
    print(f"chapters w/ procedures:   {total_with_proc}")
    print(f"total procedures:         {total_procs}")
    print(f"total concept links:      {total_links}")
    if args.verbose:
        print()
        print(f"{'book_id':>7}  {'attempted/total':>16}  {'w/ procs':>8}  title")
        for bid, title, total, attempted, with_p in rows:
            print(f"{bid:>7}  {attempted:>7}/{total:<8}  {with_p:>8}  {title[:60]}")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _parse_id_list(spec: Optional[str]) -> Optional[list[int]]:
    """Parse a comma-separated id list into ints; None if empty."""
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
    p_prep.add_argument("--skip-attempted", action="store_true", default=True,
                        help="omit chapters already attempted (default on)")
    p_prep.add_argument("--include-attempted", dest="skip_attempted",
                        action="store_false",
                        help="reprocess chapters even if already attempted")
    p_prep.add_argument("--min-content-chars", type=int, default=500,
                        help="skip chapters with content shorter than this (default 500)")
    p_prep.add_argument("--dedup-by-hash", action="store_true", default=True,
                        help="pick one chapter per (book_id, content_hash) (default on)")
    p_prep.add_argument("--no-dedup", dest="dedup_by_hash", action="store_false",
                        help="emit one row per chapter_id (useful with --chapter-ids)")
    p_prep.add_argument("--order-by", choices=("book_id", "title"), default="title",
                        help="book ordering (default title)")

    p_proc = sub.add_parser("process",
                            help="process result JSON files into the catalog")
    p_proc.add_argument("--manifest", type=Path, default=None)
    p_proc.add_argument("--output-dir", type=Path, default=None,
                        help="session directory (looks for manifest.json inside)")

    p_stat = sub.add_parser("status",
                            help="report per-book procedure-extraction coverage")
    p_stat.add_argument("--books", type=str, default=None,
                        help="comma-separated book_ids (default: all)")
    p_stat.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if hasattr(args, "books"):
        args.books = _parse_id_list(args.books)
    if hasattr(args, "chapter_ids"):
        args.chapter_ids = _parse_id_list(args.chapter_ids)

    conn = duckdb.connect(str(args.catalog))
    # VSS must be loaded before INSERT/DELETE on concept_embedding because the
    # HNSW index isn't auto-loaded with the extension on a fresh connection.
    conn.execute("LOAD vss")
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
