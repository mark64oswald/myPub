#!/usr/bin/env python3
"""
link_conceptmap_sections.py — MAPS_TO edges for ConceptMap doc sections.

fhir_spec.py --include-conceptmaps renders each ConceptMap as one section
titled like "Segment PID to Patient Map". Entity extraction finds nothing
relational in a mapping table, so those sections end up with no
concept_relation rows and the generators (which cite through
concept_relation) never reach them. This adds one deterministic edge per
section: <source concept> MAPS_TO <target concept>, source_type
'doc_section', when both names resolve to existing concepts. Only segment
and message maps are linked. A qualified name ("PID segment", "Patient
resource") resolves directly; a bare one ("PID", "Patient") only to a
concept that already appears in HL7 v2 / FHIR doc sections, so "CE" never
lands on "Combinatorial Extension".

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
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
RELATION = "MAPS_TO"

# Only segment and message maps. Datatype and vocabulary maps (CWE, CE,
# DR, ID → Coding, Period …) collide with unrelated concepts of the same
# name across the corpus, so they are not linked.
_HEADING = re.compile(
    r"^(?P<kind>segment|message)\s+(?P<src>\S+)\s+to\s+(?P<tgt>.+?)(?:\s+map)?$", re.I)

# Registry standards whose doc sections define the HL7 v2 / FHIR sense of
# a bare name ("PID", "Patient").
SCOPE_STANDARDS = ("hl7v2", "hl7v2-to-fhir", "fhir-r4", "us-core")


def parse_heading(heading: str) -> Optional[tuple[list[tuple[str, bool]], list[tuple[str, bool]]]]:
    """Candidate concept names for (source, target), most specific first.
    Each is (name, qualified): a qualified name ("PID segment") resolves on
    its own; a bare one ("PID") only to a concept tied to HL7/FHIR docs."""
    m = _HEADING.match((heading or "").strip())
    if not m:
        return None
    kind = m.group("kind").lower()
    raw = m.group("src")
    src = raw.replace("_", "^") if kind == "message" else raw
    tgt = m.group("tgt").strip()
    src_names = [(f"{src} {kind}", True), (f"HL7 v2 {src}", True), (src, False)]
    tgt_names = [(f"{tgt} resource", True), (f"FHIR {tgt}", True), (tgt, False)]
    return src_names, tgt_names


def _scope_source_ids(conn) -> list[int]:
    try:
        import standards  # noqa: PLC0415
        rows = standards.standard_sources(conn, keys=SCOPE_STANDARDS)
    except Exception:  # registry tables absent
        return []
    return sorted({int(r["doc_source_id"]) for r in rows})


def _resolve(conn, names: list[tuple[str, bool]], scope_ids: list[int]) -> Optional[int]:
    for n, qualified in names:
        if qualified:
            row = conn.execute(
                "SELECT concept_id FROM concept WHERE lower(name) = lower(?) "
                "ORDER BY concept_id LIMIT 1", [n]).fetchone()
        elif scope_ids:
            row = conn.execute(
                f"""
                SELECT c.concept_id FROM concept c
                 WHERE lower(c.name) = lower(?)
                   AND EXISTS (
                     SELECT 1 FROM concept_relation cr
                       JOIN doc_section ds ON cr.source_type = 'doc_section'
                                          AND cr.source_id = ds.doc_section_id
                       JOIN doc_snapshot sn USING (snapshot_id)
                      WHERE (cr.from_concept_id = c.concept_id OR cr.to_concept_id = c.concept_id)
                        AND sn.doc_source_id IN ({', '.join('?' * len(scope_ids))}))
                 ORDER BY c.concept_id LIMIT 1
                """, [n, *scope_ids]).fetchone()
        else:
            row = None
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
    scope_ids = _scope_source_ids(conn)
    edges = []
    for sec_id, heading in rows:
        parsed = parse_heading(heading)
        if not parsed:
            continue
        a, b = _resolve(conn, parsed[0], scope_ids), _resolve(conn, parsed[1], scope_ids)
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
