#!/usr/bin/env python3
"""
merge_concept_twins.py — Merge concepts that are the same idea under two names.

dedupe_concepts.py handles same-name twins split across concept_type.
This handles *different-name* twins, which split generators: "RAG" and
"Retrieval-Augmented Generation" became two Learning Path stages, two
Curriculum weeks and two slides in the 2026-10-27 overview run.

Candidate pairs come from three name rules, each confirmed by evidence:

  acronym        "RAG" ↔ "Retrieval-Augmented Generation"   (initials match)
  parenthetical  "Retrieval-Augmented Generation (RAG)" ↔ either part
  spelling       "Micro-services" ↔ "Microservices"          (same letters,
                 ignoring case, punctuation and a trailing plural "s")

Evidence: concept_embedding cosine ≥ ``--min-sim`` (0.60 for acronyms,
0.75 otherwise), or — when an embedding is missing — the long form's
description names the acronym or vice versa. Pairs are unioned into
groups. The canonical is the best-covered member (procedures + relations
+ alignment edges), ties to the lowest concept_id.

    find   --catalog <clone>.ddb --out twins.json [--min-sim …] [--limit N]
           Read-only. Writes every group with its evidence for review.
    merge  --plan twins.json [--catalog data/catalog.ddb] [--write]
           [--only 12,40]   (group indexes from the plan)
           Writer. For each twin: re-points concept_relation (dropping
           rows that would duplicate or become self-loops),
           alignment_edge, procedure_concept, concept_doc_link,
           concept_alias, concept_query_log and concept_resolution_queue
           to the canonical; registers the twin's name as an alias
           (alias_type 'merged'); then deletes the twin's embedding and
           concept row. Writes <plan>.merged.json first — every twin
           row and its embedding — as the recovery trail.

DuckDB 1.5 notes: child rows are moved with INSERT … SELECT + DELETE (an
UPDATE that hits a primary key collides), and the parent delete runs in
autocommit, after the children are gone. The concept_embedding HNSW
index is dropped before deleting and rebuilt after, as in
prune_stale_books.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
HNSW_INDEX = "concept_embedding_hnsw"
ACRONYM_MIN_SIM = 0.60
DEFAULT_MIN_SIM = 0.75
_INITIAL_SKIP = {"of", "and", "the", "for", "to", "in", "on", "a", "an", "with", "&"}
_ACRONYM = re.compile(r"^[A-Z][A-Za-z0-9]{1,7}$")
_PAREN = re.compile(r"^(.+?)\s*\(([^()]+)\)\s*$")


# ---------------------------------------------------------------------------
# Name rules
# ---------------------------------------------------------------------------


def spelling_key(name: str) -> str:
    k = "".join(ch for ch in name.lower() if ch.isalnum())
    return k[:-1] if len(k) > 4 and k.endswith("s") else k


def initials(name: str) -> Optional[str]:
    """'Retrieval-Augmented Generation' → 'RAG'. None for one-word names."""
    words = [w for w in re.split(r"[\s\-/]+", name.strip()) if w]
    words = [w for w in words if w.lower() not in _INITIAL_SKIP]
    if len(words) < 2:
        return None
    return "".join(w[0] for w in words if w[0].isalnum()).upper()


def acronym_key(name: str) -> Optional[str]:
    """'RAG' / 'RAGs' / 'CQRS' → key; None if the name isn't an acronym.
    Needs at least two capitals so 'Kafka' or 'Python' never qualify."""
    n = name.strip()
    if not _ACRONYM.match(n) or sum(ch.isupper() for ch in n) < 2:
        return None
    return (n[:-1] if n.endswith("s") and n[:-1].isupper() else n).upper()


def candidate_pairs(rows: list[tuple]) -> list[tuple[int, int, str]]:
    """(id_a, id_b, rule) from (concept_id, name) rows; no evidence check."""
    by_initials: dict[str, list[int]] = defaultdict(list)
    by_spelling: dict[str, list[int]] = defaultdict(list)
    by_lower: dict[str, list[int]] = defaultdict(list)
    acronyms: list[tuple[int, str]] = []
    for cid, name in rows:
        if not name:
            continue
        by_lower[name.strip().lower()].append(cid)
        by_spelling[spelling_key(name)].append(cid)
        ini = initials(name)
        if ini and len(ini) >= 2:
            by_initials[ini].append(cid)
        ak = acronym_key(name)
        if ak:
            acronyms.append((cid, ak))

    pairs: set[tuple[int, int, str]] = set()

    def add(a: int, b: int, rule: str) -> None:
        if a != b:
            pairs.add((min(a, b), max(a, b), rule))

    for cid, ak in acronyms:
        for other in by_initials.get(ak, []):
            add(cid, other, "acronym")
    for cid, name in rows:
        m = _PAREN.match(name or "")
        if m:
            for part in m.groups():
                for other in by_lower.get(part.strip().lower(), []):
                    add(cid, other, "parenthetical")
    for ids in by_spelling.values():
        if 1 < len(ids) <= 6:
            names = {cid: n for cid, n in rows if cid in ids}
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    # Same-name twins are dedupe_concepts.py's job.
                    if names[a].strip().lower() != names[b].strip().lower():
                        add(a, b, "spelling")
    return sorted(pairs)


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------


def _coverage(conn: duckdb.DuckDBPyConnection, ids: list[int]) -> dict[int, int]:
    conn.execute("CREATE OR REPLACE TEMP TABLE _ids (cid BIGINT)")
    conn.executemany("INSERT INTO _ids VALUES (?)", [[i] for i in ids])
    rows = conn.execute(
        """
        SELECT i.cid,
               (SELECT COUNT(*) FROM concept_relation r
                 WHERE r.from_concept_id = i.cid OR r.to_concept_id = i.cid)
             + (SELECT COUNT(*) FROM procedure_concept p WHERE p.concept_id = i.cid)
             + (SELECT COUNT(*) FROM alignment_edge a WHERE a.concept_id = i.cid)
          FROM _ids i
        """).fetchall()
    return {int(a): int(b) for a, b in rows}


def _evidence(conn, a: int, b: int, rule: str, min_sim: float) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT array_cosine_similarity(ea.embedding, eb.embedding)
          FROM concept_embedding ea, concept_embedding eb
         WHERE ea.concept_id = ? AND eb.concept_id = ?
        """, [a, b]).fetchone()
    threshold = ACRONYM_MIN_SIM if rule == "acronym" else min_sim
    if row is not None and row[0] is not None:
        sim = float(row[0])
        return {"cosine": round(sim, 3)} if sim >= threshold else None
    # No embedding on one side: fall back to a description cross-mention.
    na, da, nb, db = conn.execute(
        "SELECT a.name, COALESCE(a.description, ''), b.name, COALESCE(b.description, '') "
        "FROM concept a, concept b WHERE a.concept_id = ? AND b.concept_id = ?",
        [a, b]).fetchone()
    if na.lower() in db.lower() or nb.lower() in da.lower():
        return {"description_mentions": True}
    return None


def find_groups(conn: duckdb.DuckDBPyConnection, *, min_sim: float = DEFAULT_MIN_SIM,
                limit: Optional[int] = None) -> list[dict]:
    rows = [(int(a), b) for a, b in conn.execute(
        "SELECT concept_id, name FROM concept WHERE NOT COALESCE(pending_review, FALSE)"
    ).fetchall()]
    names = dict(rows)
    confirmed: list[tuple[int, int, str, dict]] = []
    for a, b, rule in candidate_pairs(rows):
        ev = _evidence(conn, a, b, rule, min_sim)
        if ev is not None:
            confirmed.append((a, b, rule, ev))

    parent: dict[int, int] = {}

    def root(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for a, b, _r, _e in confirmed:
        parent[root(a)] = root(b)
    members: dict[int, set[int]] = defaultdict(set)
    for a, b, _r, _e in confirmed:
        members[root(a)].update((a, b))

    cov = _coverage(conn, sorted({i for m in members.values() for i in m}))
    groups = []
    for ids in members.values():
        canonical = min(ids, key=lambda i: (-cov.get(i, 0), i))
        groups.append({
            "canonical": {"concept_id": canonical, "name": names[canonical],
                          "coverage": cov.get(canonical, 0)},
            "twins": [{"concept_id": i, "name": names[i], "coverage": cov.get(i, 0)}
                      for i in sorted(ids - {canonical})],
            "evidence": [{"a": names[a], "b": names[b], "rule": r, **e}
                         for a, b, r, e in confirmed if a in ids],
        })
    groups.sort(key=lambda g: -sum(t["coverage"] for t in g["twins"]))
    return groups[:limit] if limit else groups


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _move_keyed(conn, table: str, cols: list[str], key: str, twin: int, canon: int) -> None:
    """Copy rows from twin to canonical (skipping key collisions), drop the twin's."""
    sel = ", ".join(f"? AS {c}" if c == key else c for c in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {sel} FROM {table} "
                 f"WHERE {key} = ? ON CONFLICT DO NOTHING", [canon, twin])
    conn.execute(f"DELETE FROM {table} WHERE {key} = ?", [twin])


def merge_twin(conn: duckdb.DuckDBPyConnection, twin: int, canon: int) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type,
                                          confidence, source_type, source_id, created_at)
            SELECT CASE WHEN from_concept_id = ? THEN ? ELSE from_concept_id END,
                   CASE WHEN to_concept_id   = ? THEN ? ELSE to_concept_id   END,
                   relation_type, confidence, source_type, source_id, created_at
              FROM concept_relation
             WHERE (from_concept_id = ? OR to_concept_id = ?)
               AND (CASE WHEN from_concept_id = ? THEN ? ELSE from_concept_id END)
                <> (CASE WHEN to_concept_id   = ? THEN ? ELSE to_concept_id   END)
            ON CONFLICT DO NOTHING
            """, [twin, canon, twin, canon, twin, twin, twin, canon, twin, canon])
        conn.execute("DELETE FROM concept_relation WHERE from_concept_id = ? OR to_concept_id = ?",
                     [twin, twin])
        conn.execute("UPDATE alignment_edge SET concept_id = ? WHERE concept_id = ?", [canon, twin])
        _move_keyed(conn, "procedure_concept", ["procedure_id", "concept_id"], "concept_id",
                    twin, canon)
        _move_keyed(conn, "concept_doc_link",
                    ["concept_id", "doc_source_id", "notes", "created_at"], "concept_id",
                    twin, canon)
        conn.execute(
            "INSERT INTO concept_alias (concept_id, alias, alias_type) "
            "SELECT ?, alias, alias_type FROM concept_alias WHERE concept_id = ? "
            "ON CONFLICT DO NOTHING", [canon, twin])
        conn.execute("DELETE FROM concept_alias WHERE concept_id = ?", [twin])
        conn.execute(
            "INSERT INTO concept_alias (concept_id, alias, alias_type) "
            "SELECT ?, name, 'merged' FROM concept WHERE concept_id = ? "
            "ON CONFLICT DO NOTHING", [canon, twin])
        conn.execute("UPDATE concept_query_log SET concept_id = ? WHERE concept_id = ?",
                     [canon, twin])
        conn.execute("UPDATE concept_resolution_queue SET nearest_concept_id = ? "
                     "WHERE nearest_concept_id = ?", [canon, twin])
        conn.execute("UPDATE concept_resolution_queue SET provisional_concept_id = ? "
                     "WHERE provisional_concept_id = ?", [canon, twin])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # Parent rows last, in autocommit (see module docstring).
    conn.execute("DELETE FROM concept_embedding WHERE concept_id = ?", [twin])
    conn.execute("DELETE FROM concept WHERE concept_id = ?", [twin])


def do_find(args: argparse.Namespace) -> int:
    conn = duckdb.connect(str(args.catalog), read_only=True)
    try:
        groups = find_groups(conn, min_sim=args.min_sim, limit=args.limit)
    finally:
        conn.close()
    args.out.write_text(json.dumps(groups, indent=2))
    n_twins = sum(len(g["twins"]) for g in groups)
    print(f"{len(groups)} group(s), {n_twins} twin(s) to merge → {args.out}")
    for i, g in enumerate(groups[:25]):
        twins = ", ".join(f"{t['name']} ({t['coverage']})" for t in g["twins"])
        print(f"  [{i}] {g['canonical']['name']} ({g['canonical']['coverage']}) ← {twins}")
    return 0


def do_merge(args: argparse.Namespace) -> int:
    groups = json.loads(args.plan.read_text())
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        groups = [g for i, g in enumerate(groups) if i in keep]
    work = [(t["concept_id"], g["canonical"]["concept_id"]) for g in groups for t in g["twins"]]
    print(f"{len(work)} twin(s) in {len(groups)} group(s)")
    if not args.write:
        print("dry run — re-run with --write to merge")
        return 0
    conn = duckdb.connect(str(args.catalog))
    try:
        twin_ids = [t for t, _ in work]
        ph = ",".join("?" * len(twin_ids)) or "NULL"
        trail = {
            "concepts": [list(r) for r in conn.execute(
                f"SELECT * FROM concept WHERE concept_id IN ({ph})", twin_ids).fetchall()],
            "embeddings": [[r[0], list(r[1])] for r in conn.execute(
                f"SELECT concept_id, embedding FROM concept_embedding WHERE concept_id IN ({ph})",
                twin_ids).fetchall()],
            "merged_into": dict((str(t), c) for t, c in work),
        }
        trail_path = args.plan.with_suffix(".merged.json")
        trail_path.write_text(json.dumps(trail, default=str, indent=1))
        try:
            conn.execute("LOAD vss")
            conn.execute("SET hnsw_enable_experimental_persistence = true")
        except duckdb.Error:
            pass
        has_index = conn.execute("SELECT 1 FROM duckdb_indexes() WHERE index_name = ?",
                                 [HNSW_INDEX]).fetchone()
        if has_index:
            conn.execute(f"DROP INDEX {HNSW_INDEX}")
        done = 0
        for twin, canon in work:
            if conn.execute("SELECT 1 FROM concept WHERE concept_id = ?", [twin]).fetchone():
                merge_twin(conn, twin, canon)
                done += 1
        if has_index:
            conn.execute(f"CREATE INDEX {HNSW_INDEX} ON concept_embedding "
                         "USING HNSW (embedding) WITH (metric = 'cosine')")
    finally:
        conn.close()
    print(f"merged {done} twin(s); recovery trail → {trail_path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--min-sim", type=float, default=DEFAULT_MIN_SIM)
    f.add_argument("--limit", type=int)
    m = sub.add_parser("merge")
    m.add_argument("--plan", type=Path, required=True)
    m.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    m.add_argument("--only")
    m.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    return do_find(args) if args.cmd == "find" else do_merge(args)


if __name__ == "__main__":
    sys.exit(main())
