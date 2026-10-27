#!/usr/bin/env python3
"""
realign_contradictions.py — Contradiction-tuned re-alignment with multi-sample voting.

Why: the general alignment round (refresh_docs.py step 9) classifies many
(concept, chapter) pairs per prompt and treats CONTRADICTS as a side case.
CORROBORATES edges average ~0.72 confidence; CONTRADICTS edges average
~0.16 and are mostly degenerate. Migration Guide and Currency Report run
on disagreement, so they inherit that noise.

This pass re-judges one (doc_section, chapter, concept) pair per prompt,
with a prompt built only for disagreement, and asks for verbatim claim
quotes from both sides. Each pair is judged ``--samples`` times (default
3) under different framings; ``process`` checks the quotes really occur
in the source text and takes a majority vote.

Verdicts:
  CONTRADICTS   both sides make incompatible claims about the concept
  SUPERSEDED    the book's claim was true but the doc shows it has changed
                (stored as CONTRADICTS with an "[superseded]" explanation —
                the schema allows only CORROBORATES / CONTRADICTS)
  CORROBORATES  specific agreement
  UNRELATED     no comparable claim — the edge should not exist

Workflow (driven from Claude Code; sub-agents on the subscription, never
the paid API, Batch API or ``claude -p``)::

    1. prep     --catalog <clone>.ddb --output-dir <out>
                writes <out>/prompts/<edge>_s<k>.txt + manifest.json.
                Reads only — point it at an APFS clone (cp -c).
    2. Claude Code dispatches Task sub-agents over manifest["batches"];
       each writes <out>/results/<edge>_s<k>.json. A batch holds one
       sample index only, so a pair's votes come from different agents.
    3. process  --output-dir <out> [--catalog data/catalog.ddb --apply]
                verifies quotes, votes, writes <out>/votes.json and a
                summary. With --apply (needs the single writer slot) it
                records every vote in alignment_vote and updates or
                deletes the alignment_edge rows.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"

VERDICTS = ("CONTRADICTS", "SUPERSEDED", "CORROBORATES", "UNRELATED")
DEFAULT_SAMPLES = 3
DEFAULT_PER_BATCH = 10
DOC_CHARS = 3000
CHAPTER_WINDOW = 1800      # chars either side of the concept's first mention
MIN_QUOTE_CHARS = 12

SYSTEM_PROMPT = """You are checking whether current documentation and an older technical book DISAGREE about one concept.

Most pairs agree or are unrelated. Your job is to find real disagreement when it exists and to say so plainly when it does not.

Verdicts:
  CONTRADICTS  — the two texts make claims about {concept} that cannot both be true (different defaults, opposite recommendations, an API one side says exists and the other says does not).
  SUPERSEDED   — the book's claim was accurate when written, but the doc shows it has since changed (renamed API, new default, deprecated approach, removed feature).
  CORROBORATES — both texts make the same specific claim about {concept}.
  UNRELATED    — there is no pair of comparable claims about {concept}. Different emphasis, different level of detail, or one side simply not covering something is UNRELATED, not CONTRADICTS.

Rules:
  * Quote, don't paraphrase. book_claim and doc_claim must be copied VERBATIM from the texts below (12+ characters each). Claims that cannot be quoted are not evidence.
  * For UNRELATED, leave both claims empty.
  * confidence ∈ [0,1]: how sure you are of the verdict, not how important it is.
  * One sentence of explanation naming the specific point.

Output JSON only — no prose, no fences:
{{"verdict": "CONTRADICTS|SUPERSEDED|CORROBORATES|UNRELATED", "confidence": 0.0, "book_claim": "", "doc_claim": "", "explanation": ""}}"""

# Each sample sees the pair under a different framing so the votes are
# less correlated: order swap, and a skeptical pass that argues for
# agreement before ruling disagreement.
FRAMINGS = (
    "Read the DOC first, then the BOOK.",
    "Read the BOOK first, then the DOC.",
    "Before ruling CONTRADICTS or SUPERSEDED, state to yourself the strongest case that the texts actually agree or are about different things; only rule disagreement if that case fails.",
)


@dataclass
class PairEntry:
    alignment_edge_id: int
    concept_id: int
    concept_name: str
    doc_section_id: int
    chapter_id: int
    current_relation: str
    current_confidence: Optional[float]
    doc_excerpt: str
    chapter_excerpt: str
    book_title: str
    book_date: Optional[str]
    doc_source_name: str
    samples: list[dict] = field(default_factory=list)   # [{prompt_path, result_path}]


# ---------------------------------------------------------------------------
# Prep
# ---------------------------------------------------------------------------


def concept_window(text: str, concept: str, window: int = CHAPTER_WINDOW) -> str:
    """Excerpt of ``text`` centred on the first mention of ``concept``."""
    text = text or ""
    idx = text.lower().find(concept.lower())
    if idx < 0:
        return text[: 2 * window]
    start = max(0, idx - window)
    return text[start: idx + window]


def select_pairs(
    conn: duckdb.DuckDBPyConnection, *, max_corroborates_conf: float, limit: Optional[int],
) -> list[tuple]:
    """Every doc→chapter CONTRADICTS edge, plus CORROBORATES edges at or
    below ``max_corroborates_conf`` (weak agreements are where hidden
    disagreements tend to sit)."""
    sql = """
        SELECT ae.alignment_edge_id, ae.concept_id, c.name,
               ae.from_doc_section_id, ae.to_chapter_id,
               ae.relation_type, ae.confidence,
               ds.content, ch.content, b.title,
               CAST(b.publication_date AS VARCHAR), src.name
          FROM alignment_edge ae
          JOIN concept c      ON c.concept_id = ae.concept_id
          JOIN doc_section ds ON ds.doc_section_id = ae.from_doc_section_id
          JOIN doc_snapshot sn ON sn.snapshot_id = ds.snapshot_id
          JOIN doc_source src ON src.doc_source_id = sn.doc_source_id
          JOIN chapter ch     ON ch.chapter_id = ae.to_chapter_id
          JOIN book b         ON b.book_id = ch.book_id
         WHERE ae.to_chapter_id IS NOT NULL
           AND (ae.relation_type = 'CONTRADICTS'
                OR (ae.relation_type = 'CORROBORATES'
                    AND COALESCE(ae.confidence, 0) <= ?))
         ORDER BY (ae.relation_type = 'CONTRADICTS') DESC, ae.alignment_edge_id
    """
    params: list[Any] = [max_corroborates_conf]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def build_prompt(p: PairEntry, framing: str) -> str:
    book = (f"BOOK: \"{p.book_title}\""
            + (f" (published {p.book_date})" if p.book_date else ""))
    doc = f"DOC: {p.doc_source_name} (current documentation)"
    doc_block = f"{doc}\n<<<\n{p.doc_excerpt}\n>>>"
    book_block = f"{book}\n<<<\n{p.chapter_excerpt}\n>>>"
    first, second = (book_block, doc_block) if "BOOK first" in framing else (doc_block, book_block)
    return (
        SYSTEM_PROMPT.format(concept=p.concept_name)
        + f"\n\nCONCEPT: {p.concept_name}\n\n{framing}\n\n{first}\n\n{second}\n"
    )


def batch_by_sample(pairs: list[PairEntry], samples: int, per_batch: int) -> list[list[dict]]:
    """Batches that each hold a single sample index (all s0, all s1, …).

    A sub-agent that sees one pair's three framings in a row mostly copies
    its first answer, which makes the vote meaningless. Batching by sample
    index means each of a pair's votes comes from a different sub-agent.
    """
    batches: list[list[dict]] = []
    for k in range(samples):
        jobs = [p.samples[k] for p in pairs if len(p.samples) > k]
        batches += [jobs[i: i + per_batch] for i in range(0, len(jobs), per_batch)]
    return batches


def do_prep(args: argparse.Namespace) -> int:
    out: Path = args.output_dir
    (out / "prompts").mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(args.catalog), read_only=True)
    rows = select_pairs(conn, max_corroborates_conf=args.max_corroborates_conf,
                        limit=args.limit)
    conn.close()

    pairs: list[PairEntry] = []
    for r in rows:
        p = PairEntry(
            alignment_edge_id=int(r[0]), concept_id=int(r[1]), concept_name=r[2],
            doc_section_id=int(r[3]), chapter_id=int(r[4]),
            current_relation=r[5], current_confidence=r[6],
            doc_excerpt=(r[7] or "")[:DOC_CHARS],
            chapter_excerpt=concept_window(r[8] or "", r[2]),
            book_title=r[9] or "(unknown)", book_date=r[10], doc_source_name=r[11],
        )
        for k in range(args.samples):
            stem = f"{p.alignment_edge_id}_s{k}"
            prompt_path = out / "prompts" / f"{stem}.txt"
            prompt_path.write_text(build_prompt(p, FRAMINGS[k % len(FRAMINGS)]))
            p.samples.append({"prompt_path": str(prompt_path),
                              "result_path": str(out / "results" / f"{stem}.json")})
        pairs.append(p)

    jobs = [s for p in pairs for s in p.samples]
    batches = batch_by_sample(pairs, args.samples, args.per_batch)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "catalog": str(args.catalog),
        "samples": args.samples,
        "pairs": [asdict(p) for p in pairs],
        "batches": batches,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    n_contra = sum(1 for p in pairs if p.current_relation == "CONTRADICTS")
    print(f"{len(pairs)} pair(s) ({n_contra} CONTRADICTS, {len(pairs) - n_contra} weak "
          f"CORROBORATES) x {args.samples} samples = {len(jobs)} prompts in "
          f"{len(batches)} batch(es) under {out}")
    return 0


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def quote_in_source(quote: str, source: str) -> bool:
    q = _norm(quote).strip("\"'“”…. ")
    return len(q) >= MIN_QUOTE_CHARS and q in _norm(source)


def check_vote(raw: dict, pair: dict) -> dict:
    """Normalise one sub-agent answer; demote it to UNRELATED when its
    evidence quotes aren't in the texts it was shown."""
    verdict = str(raw.get("verdict", "")).upper().strip()
    if verdict not in VERDICTS:
        return {"verdict": "INVALID", "confidence": 0.0, "reason": f"bad verdict {verdict!r}"}
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    vote = {"verdict": verdict, "confidence": conf,
            "book_claim": raw.get("book_claim", ""), "doc_claim": raw.get("doc_claim", ""),
            "explanation": raw.get("explanation", "")}
    if verdict != "UNRELATED":
        ok_book = quote_in_source(vote["book_claim"], pair["chapter_excerpt"])
        ok_doc = quote_in_source(vote["doc_claim"], pair["doc_excerpt"])
        if not (ok_book and ok_doc):
            vote["reason"] = "quote not found in " + ", ".join(
                n for n, ok in (("book", ok_book), ("doc", ok_doc)) if not ok)
            vote["claimed_verdict"] = verdict
            vote["verdict"] = "UNRELATED"
    return vote


def tally(votes: list[dict], samples: int) -> dict:
    """Majority vote. Needs a strict majority of the *planned* samples, so
    a missing result can't let one vote decide. SUPERSEDED and CONTRADICTS
    pool as 'disagree' for the majority, then the larger of the two names
    the verdict."""
    valid = [v for v in votes if v["verdict"] != "INVALID"]
    need = samples // 2 + 1
    groups = Counter(
        "DISAGREE" if v["verdict"] in ("CONTRADICTS", "SUPERSEDED") else v["verdict"]
        for v in valid
    )
    if not groups:
        return {"decision": "NO_DATA", "votes": len(valid), "confidence": 0.0}
    top, n = groups.most_common(1)[0]
    if n < need:
        return {"decision": "NO_MAJORITY", "votes": len(valid), "confidence": 0.0,
                "split": dict(groups)}
    if top == "DISAGREE":
        sub = Counter(v["verdict"] for v in valid if v["verdict"] in ("CONTRADICTS", "SUPERSEDED"))
        decision = "SUPERSEDED" if sub["SUPERSEDED"] > sub["CONTRADICTS"] else "CONTRADICTS"
        winners = [v for v in valid if v["verdict"] in ("CONTRADICTS", "SUPERSEDED")]
    else:
        decision = top
        winners = [v for v in valid if v["verdict"] == top]
    mean_conf = sum(v["confidence"] for v in winners) / len(winners)
    best = max(winners, key=lambda v: v["confidence"])
    return {
        "decision": decision, "votes": len(valid), "agree": n,
        "confidence": round(mean_conf * n / samples, 3),
        "explanation": best.get("explanation", ""),
        "book_claim": best.get("book_claim", ""), "doc_claim": best.get("doc_claim", ""),
        "split": dict(groups),
    }


def edge_action(decision: dict) -> tuple[str, Optional[str]]:
    """('update', relation) | ('delete', None) | ('keep', None)."""
    d = decision["decision"]
    if d in ("CONTRADICTS", "SUPERSEDED"):
        return "update", "CONTRADICTS"
    if d == "CORROBORATES":
        return "update", "CORROBORATES"
    if d == "UNRELATED":
        return "delete", None
    return "keep", None


def _explanation(decision: dict) -> str:
    prefix = "[superseded] " if decision["decision"] == "SUPERSEDED" else ""
    parts = [prefix + (decision.get("explanation") or "").strip()]
    if decision.get("book_claim"):
        parts.append(f'Book: "{decision["book_claim"]}"')
    if decision.get("doc_claim"):
        parts.append(f'Doc: "{decision["doc_claim"]}"')
    return " | ".join(p for p in parts if p)


def apply_decisions(conn: duckdb.DuckDBPyConnection, decided: list[dict]) -> Counter:
    conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_alignment_vote_id START 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alignment_vote (
            alignment_vote_id BIGINT PRIMARY KEY DEFAULT nextval('seq_alignment_vote_id'),
            alignment_edge_id BIGINT NOT NULL,
            sample            INTEGER NOT NULL,
            verdict           VARCHAR NOT NULL,
            confidence        DOUBLE,
            book_claim        TEXT,
            doc_claim         TEXT,
            explanation       TEXT,
            note              VARCHAR,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    stats: Counter = Counter()
    conn.execute("BEGIN")
    try:
        for item in decided:
            eid = item["alignment_edge_id"]
            for k, v in enumerate(item["votes"]):
                conn.execute(
                    "INSERT INTO alignment_vote (alignment_edge_id, sample, verdict, confidence,"
                    " book_claim, doc_claim, explanation, note) VALUES (?,?,?,?,?,?,?,?)",
                    [eid, k, v["verdict"], v.get("confidence"), v.get("book_claim"),
                     v.get("doc_claim"), v.get("explanation"), v.get("reason")],
                )
            action, relation = edge_action(item["decision"])
            if action == "update":
                conn.execute(
                    "UPDATE alignment_edge SET relation_type = ?, confidence = ?, explanation = ?"
                    " WHERE alignment_edge_id = ?",
                    [relation, item["decision"]["confidence"], _explanation(item["decision"]), eid],
                )
                was = item["current_relation"]
                stats[f"{was}->{relation}"] += 1
            elif action == "delete":
                conn.execute("DELETE FROM alignment_edge WHERE alignment_edge_id = ?", [eid])
                stats["deleted_unrelated"] += 1
            else:
                stats["kept_" + item["decision"]["decision"].lower()] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats


def do_process(args: argparse.Namespace) -> int:
    out: Path = args.output_dir
    manifest = json.loads((out / "manifest.json").read_text())
    samples = int(manifest["samples"])
    decided = []
    missing = unparseable = 0
    for pair in manifest["pairs"]:
        votes = []
        for s in pair["samples"]:
            rp = Path(s["result_path"])
            if not rp.exists():
                missing += 1
                continue
            try:
                raw = json.loads(rp.read_text())
            except json.JSONDecodeError:
                unparseable += 1
                continue
            votes.append(check_vote(raw, pair))
        decided.append({
            "alignment_edge_id": pair["alignment_edge_id"],
            "concept_name": pair["concept_name"],
            "current_relation": pair["current_relation"],
            "current_confidence": pair["current_confidence"],
            "votes": votes,
            "decision": tally(votes, samples),
        })
    (out / "votes.json").write_text(json.dumps(decided, indent=2))

    summary = Counter(d["decision"]["decision"] for d in decided)
    demoted = sum(1 for d in decided for v in d["votes"] if "claimed_verdict" in v)
    contra = [d["decision"]["confidence"] for d in decided
              if d["decision"]["decision"] in ("CONTRADICTS", "SUPERSEDED")]
    print(f"pairs: {len(decided)}  missing results: {missing}  unparseable: {unparseable}")
    print(f"votes demoted for unverifiable quotes: {demoted}")
    for k, v in sorted(summary.items()):
        print(f"  {k:<13} {v}")
    if contra:
        print(f"mean confidence of disagreement edges: {sum(contra) / len(contra):.2f}")

    if args.apply:
        conn = duckdb.connect(str(args.catalog))
        stats = apply_decisions(conn, decided)
        conn.close()
        print("applied:", dict(stats))
    else:
        print("dry run — re-run with --apply --catalog data/catalog.ddb to write")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    p.add_argument("--per-batch", type=int, default=DEFAULT_PER_BATCH)
    p.add_argument("--max-corroborates-conf", type=float, default=0.5)
    p.add_argument("--limit", type=int)
    q = sub.add_parser("process")
    q.add_argument("--output-dir", type=Path, required=True)
    q.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    q.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return do_prep(args) if args.cmd == "prep" else do_process(args)


if __name__ == "__main__":
    sys.exit(main())
