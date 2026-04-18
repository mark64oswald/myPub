#!/usr/bin/env python3
"""
extraction_eval.py — Phase 2.6 extraction + resolution quality eval.

Runs against the golden-set JSON at tests/eval/golden_extractions.json
(or --golden-set PATH) and the live catalog.

For each `extraction_pair` `{chapter_id, concept_name, must_appear}`,
checks whether the extractor has emitted that concept for that chapter
(looking for `concept_relation` rows with source_id=<chapter_id> that
reference a concept whose name or alias matches `concept_name`).

Reports:
  * **Precision** — of the concepts extracted for chapters in the golden
    set, how many were expected? (Penalizes over-extraction of bogus
    entities.)
  * **Recall** — of the expected (chapter, concept) pairs, how many were
    actually extracted? (Penalizes missed concepts.)
  * **F1** — harmonic mean.

For each `resolution_pair`:
  * **Same pairs**: expect exact-match, alias-match, or embedding_high.
  * **Different pairs**: expect `new`, `borderline`, or — at worst — no
    embedding_high.

Intended use (autoresearch loop):
  1. Record baseline metrics to `logs/extraction_eval_baseline.md`.
  2. Modify the extraction prompt (one variable at a time).
  3. Re-run the eval; compare to baseline.
  4. Keep the prompt change if F1 improves (or resolution accuracy
     improves without F1 dropping); revert otherwise.

Usage:
  .venv/bin/python3 scripts/extraction_eval.py
  .venv/bin/python3 scripts/extraction_eval.py --golden-set path/to/custom.json
  .venv/bin/python3 scripts/extraction_eval.py --baseline logs/baseline.md
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
DEFAULT_GOLDEN = PROJECT_ROOT / "tests" / "eval" / "golden_extractions.json"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

LOG = logging.getLogger("extraction_eval")


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class ExtractionReport:
    """Aggregated eval results for one run."""

    total_pairs: int = 0
    hits: int = 0
    misses: list[tuple[int, str]] = field(default_factory=list)
    extra_concepts_by_chapter: dict[int, set[str]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return self.hits / self.total_pairs if self.total_pairs else 0.0

    @property
    def precision(self) -> float:
        # Precision = expected hits / (expected hits + unexpected extractions
        # within the scoped chapters). We only count extras in chapters that
        # appeared in the golden set, to keep the metric bounded.
        extras = sum(len(v) for v in self.extra_concepts_by_chapter.values())
        denom = self.hits + extras
        return self.hits / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0


@dataclass
class ResolutionReport:
    """Separate aggregation for resolution pair outcomes."""

    same_pairs: int = 0
    same_correct: int = 0  # resolver said same (exact/alias/embedding_high)
    different_pairs: int = 0
    different_correct: int = 0  # resolver said new/borderline

    @property
    def accuracy(self) -> float:
        total = self.same_pairs + self.different_pairs
        correct = self.same_correct + self.different_correct
        return correct / total if total else 0.0


# ----------------------------------------------------------------------------
# Extraction eval
# ----------------------------------------------------------------------------

def _extracted_concepts_for_chapter(
    conn: duckdb.DuckDBPyConnection, chapter_id: int
) -> set[str]:
    """Return set of concept names (canonical + alias) touched by this chapter."""
    rows = conn.execute(
        """
        SELECT DISTINCT c.name FROM concept c
          JOIN concept_relation cr
            ON c.concept_id = cr.from_concept_id OR c.concept_id = cr.to_concept_id
         WHERE cr.source_type = 'chapter' AND cr.source_id = ?
        UNION
        SELECT DISTINCT a.alias FROM concept_alias a
          JOIN concept_relation cr
            ON a.concept_id = cr.from_concept_id OR a.concept_id = cr.to_concept_id
         WHERE cr.source_type = 'chapter' AND cr.source_id = ?
        """,
        [chapter_id, chapter_id],
    ).fetchall()
    return {r[0] for r in rows}


def evaluate_extraction(
    conn: duckdb.DuckDBPyConnection, pairs: list[dict]
) -> ExtractionReport:
    """Check each expected (chapter, concept) pair against the catalog."""
    report = ExtractionReport(total_pairs=len(pairs))
    by_chapter: dict[int, set[str]] = {}
    expected_by_chapter: dict[int, set[str]] = {}

    for pair in pairs:
        chapter_id = pair["chapter_id"]
        concept_name = pair["concept_name"]
        expected_by_chapter.setdefault(chapter_id, set()).add(concept_name.lower())

        if chapter_id not in by_chapter:
            by_chapter[chapter_id] = {
                n.lower() for n in _extracted_concepts_for_chapter(conn, chapter_id)
            }
        found = concept_name.lower() in by_chapter[chapter_id]
        if found:
            report.hits += 1
        else:
            report.misses.append((chapter_id, concept_name))

    # Extras = extracted concepts in each evaluated chapter that weren't in
    # the golden set for that chapter.
    for chapter_id, extracted in by_chapter.items():
        expected = expected_by_chapter.get(chapter_id, set())
        report.extra_concepts_by_chapter[chapter_id] = extracted - expected
    return report


# ----------------------------------------------------------------------------
# Resolution eval
# ----------------------------------------------------------------------------

def _concept_id_by_name(
    conn: duckdb.DuckDBPyConnection, name: str
) -> Optional[int]:
    """Find a concept id by exact or alias name match (case-insensitive)."""
    row = conn.execute(
        "SELECT concept_id FROM concept WHERE LOWER(name) = LOWER(?) LIMIT 1",
        [name],
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT concept_id FROM concept_alias WHERE LOWER(alias) = LOWER(?) LIMIT 1",
        [name],
    ).fetchone()
    return row[0] if row else None


def evaluate_resolution(
    conn: duckdb.DuckDBPyConnection, pairs: dict
) -> ResolutionReport:
    """Check same-pair / different-pair outcomes without doing re-extractions.

    Same-pair: candidate should resolve to existing (checked via name or
    alias lookup). If both map to the same concept, ✓.

    Different-pair: a and b should be distinct concepts in the catalog. If
    they map to the same concept, ✗ (merged when they shouldn't have been).
    """
    report = ResolutionReport()
    for same in pairs.get("same", []):
        report.same_pairs += 1
        cid_a = _concept_id_by_name(conn, same["candidate"])
        cid_b = _concept_id_by_name(conn, same["existing"])
        if cid_a is not None and cid_a == cid_b:
            report.same_correct += 1
        else:
            LOG.info(
                "same-pair miss: %r (id=%s) vs %r (id=%s)",
                same["candidate"], cid_a, same["existing"], cid_b,
            )
    for diff in pairs.get("different", []):
        report.different_pairs += 1
        cid_a = _concept_id_by_name(conn, diff["a"])
        cid_b = _concept_id_by_name(conn, diff["b"])
        if cid_a is not None and cid_b is not None and cid_a != cid_b:
            report.different_correct += 1
        elif cid_a is None or cid_b is None:
            LOG.info(
                "different-pair skipped (one side absent): %r (id=%s) vs %r (id=%s)",
                diff["a"], cid_a, diff["b"], cid_b,
            )
            # Skipping doesn't penalize — we just don't count it either way.
            report.different_pairs -= 1
        else:
            LOG.info("different-pair MERGED: %r and %r resolved to id=%s",
                     diff["a"], diff["b"], cid_a)
    return report


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _format_report(
    ext: ExtractionReport, res: ResolutionReport
) -> str:
    lines = ["=== extraction ==="]
    lines.append(f"golden pairs:   {ext.total_pairs}")
    lines.append(f"hits:           {ext.hits}")
    lines.append(f"misses:         {len(ext.misses)}")
    lines.append(f"precision:      {ext.precision:.3f}")
    lines.append(f"recall:         {ext.recall:.3f}")
    lines.append(f"f1:             {ext.f1:.3f}")
    if ext.misses:
        lines.append("")
        lines.append("Missed pairs (first 10):")
        for chapter_id, name in ext.misses[:10]:
            lines.append(f"  ch={chapter_id}  concept={name!r}")

    lines.append("")
    lines.append("=== resolution ===")
    lines.append(f"same-pair total:     {res.same_pairs}")
    lines.append(f"same-pair correct:   {res.same_correct}")
    lines.append(f"different-pair total:  {res.different_pairs}")
    lines.append(f"different-pair correct:{res.different_correct}")
    lines.append(f"accuracy:              {res.accuracy:.3f}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Run the extraction + resolution eval and print a report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--baseline", type=Path, default=None,
                        help="also write the report to this path (markdown)")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    golden = json.loads(args.golden_set.read_text())
    conn = duckdb.connect(str(args.catalog), read_only=True)
    try:
        ext = evaluate_extraction(conn, golden.get("extraction_pairs", []))
        res = evaluate_resolution(conn, golden.get("resolution_pairs", {}))
    finally:
        conn.close()

    report = _format_report(ext, res)
    print(report)
    if args.baseline is not None:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(report + "\n")
        print(f"\nwrote baseline: {args.baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
