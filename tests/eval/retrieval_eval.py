"""retrieval_eval.py — diverse-query probe across 15 categories of search
intent. Designed to expose PATTERNS of retrieval failure rather than
fix specific queries.

This script is NOT part of the regular pytest suite — it runs against
the live catalog with the real sentence-transformers model (slow), and
the "expected signal class" tags reflect a calibration baseline that
will drift as the corpus grows. Run it deliberately when iterating on
ranking / retrieval changes:

    .venv/bin/python3 tests/eval/retrieval_eval.py

Each query is tagged with:
  cat       — category bucket (distrib, db, lang, ml, ops, sec, nl,
              comp, long, new, old, vendor, ambig, code, health)
  expected  — kind the result SHOULD be:
              B = book chapter (foundational/older topic, well-covered)
              D = doc_section (current API / fresh-content question)
              E = either kind is reasonable
              T = thin (corpus likely doesn't have it; tangential or
                  empty results expected)

What the script reports:
  * primary kind/source/title with scoring components
  * latency per query
  * tally of (chapter / doc_section) primaries
  * per-query expected-vs-actual verdict

Use the categorical breakdown to identify FAILURE PATTERNS — repeated
shapes of mismatch across many queries — rather than fixating on any
single bad result. Iteration is cheaper when you fix the pattern, not
the example.
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))
os.environ.setdefault("MYPUB_CATALOG", str(ROOT / "data" / "catalog.ddb"))

import server  # noqa: E402  - sys.path manipulation above
server._bootstrap()


# Diverse query set, organized by category. Each tuple is:
#   (category, query, expected_signal_class, notes)
QUERIES = [
    # ── 1. Distributed systems / consensus (book-leaning) ──
    ("distrib",  "Raft consensus leader election",      "B", ""),
    ("distrib",  "Paxos algorithm phases",              "B", ""),
    ("distrib",  "two-phase commit protocol",           "B", ""),
    ("distrib",  "vector clocks causality",             "B", ""),
    # ── 2. Database internals (book-leaning) ──
    ("db",       "B-tree index structure",              "B", ""),
    ("db",       "LSM tree compaction",                 "B", ""),
    ("db",       "write-ahead log durability",          "B", ""),
    ("db",       "MVCC snapshot isolation",             "B", ""),
    # ── 3. Programming languages (mixed) ──
    ("lang",     "Python GIL global interpreter lock",  "B", ""),
    ("lang",     "Rust ownership borrow checker",       "B", ""),
    ("lang",     "Go channels select goroutine",        "B", ""),
    # ── 4. Machine learning ──
    ("ml",       "gradient descent backpropagation",    "B", ""),
    ("ml",       "transformer attention mechanism",     "E", ""),
    ("ml",       "RAG retrieval augmented generation",  "E", "newer concept; doc may also have"),
    # ── 5. Cloud / DevOps (doc-leaning but books exist) ──
    ("ops",      "blue-green deployment strategy",      "B", ""),
    ("ops",      "Kubernetes pod lifecycle",            "E", "common topic; both should have"),
    ("ops",      "Terraform state locking",             "T", "no Terraform doc registered"),
    # ── 6. Security ──
    ("sec",      "OAuth 2.0 authorization code flow",   "E", ""),
    ("sec",      "JWT signature verification",          "E", ""),
    ("sec",      "TLS certificate pinning",             "B", ""),
    # ── 7. Question form / natural language ──
    ("nl",       "how does Kafka guarantee message ordering", "E", ""),
    ("nl",       "why use eventual consistency in distributed systems", "B", ""),
    ("nl",       "when should I use CQRS vs CRUD", "E", "comparative; CQRS is acronym so filter applies"),
    # ── 8. Comparative queries ──
    ("comp",     "ETL vs ELT trade-offs",               "B", "comparison style"),
    ("comp",     "REST vs gRPC performance",            "E", "REST/gRPC are acronyms"),
    # ── 9. Long natural-language queries ──
    ("long",     "how do I implement exactly-once semantics in a Kafka producer with idempotent writes",
                 "E", ""),
    # ── 10. Newer / trending (likely needs discovery) ──
    ("new",      "LangGraph state machine workflow",    "T", "novel; LangChain registered, LangGraph not"),
    ("new",      "Marimo reactive notebook",            "T", ""),
    # ── 11. Outdated tech (book-only) ──
    ("old",      "Hadoop MapReduce job tracker",        "B", "old; books only"),
    ("old",      "SOAP envelope WSDL",                  "B", ""),
    # ── 12. Vendor product (specific) ──
    ("vendor",   "Databricks Delta Lake time travel",   "D", "Delta + Databricks both registered"),
    ("vendor",   "Snowflake Snowpark Python",           "T", "no Snowflake doc registered"),
    # ── 13. Highly ambiguous tokens ──
    ("ambig",    "queue concurrency patterns",          "E", "queue means many things"),
    ("ambig",    "graph traversal algorithms",          "B", "graph theory book content"),
    # ── 14. Code-shape query ──
    ("code",     "useState hook React component",       "T", "no React doc registered"),
    # ── 15. Healthcare ──
    ("health",   "ICD-10 diagnostic coding",            "E", ""),
    ("health",   "claims adjudication workflow",        "E", "if covered"),
]


# Tags that map "expected" classes to the kind we'd expect to win primary.
EXPECTED_KIND_FOR = {"B": "chapter", "D": "doc_section"}


def short(s, n=55):
    return s if not s or len(s) <= n else s[:n] + "…"


def main() -> int:
    print(f"{'cat':<8s} {'time':>5s}  {'expected':<8s} {'kind':<11s}  "
          f"{'source':<37s}  {'title':<55s}  rel")
    print("─" * 140)
    stats = {"total": 0, "by_cat": {}, "by_kind": {"chapter": 0, "doc_section": 0}}
    results = []
    for cat, q, expected, notes in QUERIES:
        t0 = time.time()
        resp = server.search_chapters(q, mode="interactive", limit=5, auto_discover=False)
        dt = time.time() - t0
        p = resp.get("primary")
        stats["total"] += 1
        stats["by_cat"][cat] = stats["by_cat"].get(cat, 0) + 1
        if not p:
            results.append({"cat": cat, "q": q, "expected": expected, "kind": "—",
                            "src": "(empty)", "title": "no primary", "rel": 0.0, "time": dt})
            print(f"  {cat:<8s} {dt:>4.1f}s  exp={expected:<5s} EMPTY")
            continue
        kind = p.get("kind", "?")
        src = p.get("book_title") or p.get("doc_source_name") or "?"
        title = p.get("chapter_title") or p.get("heading_text") or "(no title)"
        rel = (p.get("components") or {}).get("relevance", 0)
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        results.append({"cat": cat, "q": q, "expected": expected, "kind": kind,
                        "src": src, "title": title, "rel": rel, "time": dt})
        print(f"  {cat:<8s} {dt:>4.1f}s  exp={expected:<5s} [{kind:<11s}] "
              f"{short(src, 35):<37s}  {short(title, 50):<52s}  {rel:.2f}")

    print()
    print(f"Total queries: {stats['total']}, "
          f"kind breakdown: {stats['by_kind']}")
    print()
    print("Per-query verdict (vs. expected signal class):")
    print()
    mismatches = 0
    for r in results:
        expected_kind = EXPECTED_KIND_FOR.get(r["expected"], "?")
        if r["kind"] == "—":
            verdict = "EMPTY"
            mismatches += 1
        elif r["expected"] == "E":
            verdict = "(any)"
        elif r["expected"] == "T" and r["kind"] != "—":
            verdict = "GOT-RESULT-EXPECTED-THIN"
        elif r["kind"] == expected_kind:
            verdict = "✓ kind"
        else:
            verdict = "✗ kind mismatch"
            mismatches += 1
        print(f"  [{r['cat']:<7s}] {short(r['q'], 50):<53s}  expected={r['expected']}  "
              f"got={r['kind']:<11s}  {verdict}")

    print()
    print(f"Kind mismatches (book-expected got doc, or doc-expected got book): {mismatches}")
    return 0 if mismatches < len(QUERIES) // 4 else 1


if __name__ == "__main__":
    sys.exit(main())
