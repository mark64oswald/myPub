#!/usr/bin/env python3
"""
link_conceptmap_sections.py — MAPS_TO edges for ConceptMap doc sections.

fhir_spec.py --include-conceptmaps renders each ConceptMap as one section
titled like "Segment PID to Patient Map". Entity extraction finds nothing
relational in a mapping table, so those sections end up with no
concept_relation rows and the generators (which cite through
concept_relation) never reach them. This adds one deterministic edge per
section: <source concept> MAPS_TO <target concept>, source_type
'doc_section', when both names resolve to existing concepts.

    --source-id N   doc_source to scan (e.g. the v2-to-FHIR IG)
    --write         insert the edges (default: dry run, prints them)

Undo: DELETE FROM concept_relation WHERE relation_type = 'MAPS_TO'
      AND source_type = 'doc_section' AND source_id IN (<that source's sections>)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
RELATION = "MAPS_TO"

_HEADING = re.compile(
    r"^(?P<kind>segment|message|datatype|data type|vocabulary)\s+(?P<src>\S+)"
    r"\s+to\s+(?P<tgt>.+?)(?:\s+map)?$", re.I)
_SUFFIX = {"segment": " segment", "message": " message", "datatype": " data type",
           "data type": " data type", "vocabulary": ""}


def parse_heading(heading: str) -> Optional[tuple[list[str], list[str]]]:
    """Candidate concept names for (source, target), most specific first."""
    m = _HEADING.match((heading or "").strip())
    if not m:
        return None
    kind = m.group("kind").lower()
    src = m.group("src").replace("_", "^") if kind == "message" else m.group("src")
    tgt = m.group("tgt").strip()
    src_names = [f"{src}{_SUFFIX[kind]}".strip(), src, m.group("src")]
    tgt_names = [f"{tgt} resource", tgt, f"FHIR {tgt}"]
    return list(dict.fromkeys(src_names)), list(dict.fromkeys(tgt_names))


def _resolve(conn, names: list[str]) -> Optional[int]:
    for n in names:
        row = conn.execute(
            "SELECT concept_id FROM concept WHERE lower(name) = lower(?) "
            "ORDER BY concept_id LIMIT 1", [n]).fetchone()
        if row:
            return int(row[0])
    return None


def plan_edges(conn, source_id: int) -> list[tuple[int, int, int, str]]:
    """(from_concept_id, to_concept_id, doc_section_id, heading) for the
    latest snapshot of ``source_id``."""
    rows = conn.execute(
        """
        SELECT ds.doc_section_id, ds.heading_text
          FROM doc_section ds
         WHERE ds.snapshot_id = (SELECT max(snapshot_id) FROM doc_snapshot
                                  WHERE doc_source_id = ?)
        """, [source_id]).fetchall()
    edges = []
    for sec_id, heading in rows:
        parsed = parse_heading(heading)
        if not parsed:
            continue
        a, b = _resolve(conn, parsed[0]), _resolve(conn, parsed[1])
        if a and b and a != b:
            edges.append((a, b, int(sec_id), heading))
    return edges


def write_edges(conn, edges) -> int:
    n = 0
    for a, b, sec, _ in edges:
        exists = conn.execute(
            "SELECT 1 FROM concept_relation WHERE from_concept_id=? AND to_concept_id=? "
            "AND relation_type=? AND source_type='doc_section' AND source_id=?",
            [a, b, RELATION, sec]).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
                "relation_type, confidence, source_type, source_id) "
                "VALUES (?, ?, ?, 0.9, 'doc_section', ?)", [a, b, RELATION, sec])
            n += 1
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--source-id", type=int, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    conn = duckdb.connect(str(args.catalog), read_only=not args.write)
    try:
        edges = plan_edges(conn, args.source_id)
        for a, b, sec, heading in edges[:20]:
            print(f"  {heading!r}: {a} MAPS_TO {b} (section {sec})")
        print(f"{len(edges)} edge(s) planned")
        if args.write:
            print(f"{write_edges(conn, edges)} inserted")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
