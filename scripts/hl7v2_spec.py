#!/usr/bin/env python3
"""
hl7v2_spec.py — HL7 v2.x segment / message / data-type reference as a doc source.

HL7 publishes the v2 standard only behind a sign-in, and the free browsable
references (Caristix) render with JavaScript. hl7apy (MIT, already a
myPub dependency) ships the structure of every v2 version from 2.1 to
2.8.2: segments with their fields (data type, optionality, repetition,
table), message structures with their segment groups, composite data
types with their components, and the HL7 tables with their codes. This
renders one version of that as Markdown — one "## " section per segment,
message structure and data type — and feeds it through the normal doc
pipeline like fhir_spec.py does. No spec prose is reproduced, only
structure.

    render    [--version 2.5.1] [--include-tables] [--out spec.md]
              No DB. Writes the Markdown so you can review it.
    register  [--version 2.5.1] [--include-tables] [--name NAME]
              [--catalog data/catalog.ddb]
              Writer. Upserts doc_source (source_type 'hl7v2_spec',
              identifier 'hl7v2@<version>') and runs steps 1–6 of the doc
              pipeline. Then run refresh_docs.py prep / process as usual.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
SOURCE_TYPE = "hl7v2_spec"

# hl7apy carries no segment titles; these are the common ones.
SEGMENT_TITLES = {
    "ACC": "Accident", "AIG": "Appointment Information - General Resource",
    "AIL": "Appointment Information - Location Resource",
    "AIP": "Appointment Information - Personnel Resource",
    "AIS": "Appointment Information - Service", "AL1": "Patient Allergy Information",
    "BHS": "Batch Header", "BTS": "Batch Trailer", "DG1": "Diagnosis",
    "DRG": "Diagnosis Related Group", "ERR": "Error", "EVN": "Event Type",
    "FHS": "File Header", "FTS": "File Trailer", "GT1": "Guarantor",
    "IAM": "Patient Adverse Reaction Information", "IN1": "Insurance",
    "IN2": "Insurance Additional Information", "IN3": "Insurance Additional Information, Certification",
    "MRG": "Merge Patient Information", "MSA": "Message Acknowledgment",
    "MSH": "Message Header", "NK1": "Next of Kin / Associated Parties", "NTE": "Notes and Comments",
    "OBR": "Observation Request", "OBX": "Observation/Result", "ORC": "Common Order",
    "PD1": "Patient Additional Demographic", "PID": "Patient Identification",
    "PR1": "Procedures", "PRD": "Provider Data", "PV1": "Patient Visit",
    "PV2": "Patient Visit - Additional Information", "QAK": "Query Acknowledgment",
    "QPD": "Query Parameter Definition", "QRD": "Original-Style Query Definition",
    "RCP": "Response Control Parameter", "RGS": "Resource Group", "ROL": "Role",
    "RXA": "Pharmacy/Treatment Administration", "RXC": "Pharmacy/Treatment Component Order",
    "RXE": "Pharmacy/Treatment Encoded Order", "RXO": "Pharmacy/Treatment Order",
    "RXR": "Pharmacy/Treatment Route", "SCH": "Scheduling Activity Information",
    "SFT": "Software Segment", "SPM": "Specimen", "TQ1": "Timing/Quantity",
    "TQ2": "Timing/Quantity Relationship", "TXA": "Transcription Document Header",
    "UB1": "UB82", "UB2": "Uniform Billing Data", "PDA": "Patient Death and Autopsy",
    "DB1": "Disability", "ZXX": "Site-defined segment",
}


@dataclass
class FetchResultLike:
    url: str
    content: str
    source_type: str


def _label(long_name: Optional[str]) -> str:
    return (long_name or "").replace("_", " ").title()


def _card(rng: tuple[int, int]) -> str:
    lo, hi = rng
    return f"{lo}..{'*' if hi == -1 else hi}"


def _opt(rng: tuple[int, int]) -> str:
    lo, hi = rng
    if hi == 0:
        return "B (withdrawn)"
    return ("R" if lo >= 1 else "O") + (" repeating" if hi == -1 or hi > 1 else "")


def _field_parts(defn: tuple) -> tuple[str, str, Optional[str]]:
    """(data type, long name, table) for a field definition, leaf or composite."""
    # leaf:      ('leaf', None, DT, NAME, TABLE, LEN)
    # composite: ('sequence', (components...), DT, NAME, TABLE, LEN)
    # withdrawn: None
    if not defn:
        return "", "", None
    return defn[2] or "", defn[3] or "", defn[4]


def segment_markdown(code: str, seg: tuple) -> str:
    title = SEGMENT_TITLES.get(code, "")
    lines = [f"## {code} segment" + (f" — {title}" if title else ""), ""]
    lines += [f"The HL7 v2 {code} segment" + (f" ({title})" if title else "")
              + " has these fields:", "",
              "| Seq | Field | Data type | Usage | Card. | Table |", "|---|---|---|---|---|---|"]
    for name, defn, rng, _kind in seg[1]:
        dt, long_name, table = _field_parts(defn)
        seq = name.split("_", 1)[1] if "_" in name else name
        lines.append(f"| {code}-{seq} | {_label(long_name)} | {dt} | {_opt(rng)} | "
                     f"{_card(rng)} | {table or ''} |")
    lines.append("")
    return "\n".join(lines)


def _message_rows(children: tuple, depth: int = 0) -> list[str]:
    rows = []
    for name, defn, rng, kind in children:
        pad = "  " * depth
        if kind == "GRP":
            rows.append(f"{pad}- **{name}** group, {_opt(rng)} ({_card(rng)})")
            rows += _message_rows(defn[1], depth + 1)
        else:
            title = SEGMENT_TITLES.get(name, "")
            rows.append(f"{pad}- {name}" + (f" ({title})" if title else "")
                        + f", {_opt(rng)} ({_card(rng)})")
    return rows


def message_markdown(code: str, msg: tuple) -> str:
    event = code.replace("_", "^", 1)
    lines = [f"## {code} message structure", "",
             f"Segments of the HL7 v2 {event} message structure, in order "
             "(R = required, O = optional):", ""]
    lines += _message_rows(msg[1])
    lines.append("")
    return "\n".join(lines)


def datatype_markdown(code: str, comps: tuple) -> str:
    lines = [f"## {code} data type", "", f"Components of the HL7 v2 {code} data type:", "",
             "| Component | Name | Data type | Table |", "|---|---|---|---|"]
    for name, defn, _rng, _kind in comps:
        seq = name.rsplit("_", 1)[-1]
        dt, long_name, table = _field_parts(defn)
        lines.append(f"| {code}.{seq} | {_label(long_name)} | {dt} | {table or ''} |")
    lines.append("")
    return "\n".join(lines)


def table_markdown(code: str, table: tuple) -> str:
    title, values = table
    num = code.replace("HL7", "")
    return "\n".join([f"## HL7 table {num} — {title}", "",
                      f"Codes in HL7 v2 table {num} ({title}): "
                      + ", ".join(f"`{v}`" for v in values), ""])


def _composite_datatypes(lib: Any) -> dict[str, tuple]:
    """Composite data types, each with its component list, gathered from the
    field definitions (hl7apy keeps them inline)."""
    found: dict[str, tuple] = {}

    def walk(defn):
        if isinstance(defn, (tuple, list)) and defn and defn[0] == "sequence" \
                and isinstance(defn[1], tuple) and defn[2]:
            found.setdefault(defn[2], defn[1])
            for _n, sub, _r, _k in defn[1]:
                walk(sub)

    for _code, seg in lib.SEGMENTS.items():
        for _n, defn, _r, _k in seg[1]:
            walk(defn)
    return found


def spec_markdown(version: str = "2.5.1", *, include_tables: bool = False) -> tuple[str, str]:
    """(identifier 'hl7v2@<version>', markdown)."""
    from hl7apy import load_library

    lib = load_library(version)
    ident = f"hl7v2@{version}"
    segs = sorted(lib.SEGMENTS.items())
    msgs = sorted(lib.MESSAGES.items())
    dts = sorted(_composite_datatypes(lib).items())
    head = [f"# HL7 Version {version} structure reference ({ident})", "",
            f"Segment, message structure and data type definitions for HL7 v{version}, "
            "from the hl7apy reference tables. Structure only: field names, data "
            "types, usage, cardinality and table bindings.", "",
            f"{len(segs)} segments, {len(msgs)} message structures, {len(dts)} composite "
            "data types" + (f", {len(lib.TABLES)} tables." if include_tables else "."), ""]
    parts = [segment_markdown(c, s) for c, s in segs]
    parts += [message_markdown(c, m) for c, m in msgs]
    parts += [datatype_markdown(c, d) for c, d in dts]
    if include_tables:
        parts += [table_markdown(c, t) for c, t in sorted(lib.TABLES.items())]
    return ident, "\n".join(head) + "\n".join(parts)


class Hl7v2SpecFetcher:
    """Local fetcher with the refresh_docs fetcher interface."""

    def __init__(self, version: str, include_tables: bool = False):
        self.version, self.include_tables = version, include_tables

    def fetch(self, identifier: str) -> FetchResultLike:
        _, md = spec_markdown(self.version, include_tables=self.include_tables)
        return FetchResultLike(url=f"https://pypi.org/project/hl7apy/#hl7v{self.version}",
                               content=md, source_type="markdown")


def do_render(args: argparse.Namespace) -> int:
    ident, md = spec_markdown(args.version, include_tables=args.include_tables)
    if args.out:
        args.out.write_text(md)
        print(f"{ident}: {md.count(chr(10) + '## ')} section(s), "
              f"{len(md.split())} words → {args.out}")
    else:
        print(md)
    return 0


def do_register(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import refresh_docs  # noqa: PLC0415 — heavy imports, writer path only

    ident = f"hl7v2@{args.version}"
    conn = refresh_docs.open_writer(args.catalog)
    try:
        row = conn.execute("SELECT doc_source_id FROM doc_source WHERE source_type = ? "
                           "AND identifier = ?", [SOURCE_TYPE, ident]).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
                "authority_score, refresh_ttl_days, priority_tier, pinned) "
                "VALUES (?, ?, 'local', ?, 0.8, 365, 'cold', TRUE) RETURNING doc_source_id",
                [args.name or f"HL7 v{args.version} structure reference", SOURCE_TYPE,
                 ident]).fetchone()
        result = refresh_docs.refresh_one_source(
            conn, int(row[0]), fetcher=Hl7v2SpecFetcher(args.version, args.include_tables))
    finally:
        conn.close()
    print(f"{ident}: {result.status} (doc_source_id={row[0]}, "
          f"sections={getattr(result, 'section_count', None)}, error={result.error})")
    return 0 if result.status in ("refreshed", "no_change") else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("render", "register"):
        p = sub.add_parser(name)
        p.add_argument("--version", default="2.5.1")
        p.add_argument("--include-tables", action="store_true")
        if name == "render":
            p.add_argument("--out", type=Path)
        else:
            p.add_argument("--name")
            p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)
    return do_render(args) if args.cmd == "render" else do_register(args)


if __name__ == "__main__":
    sys.exit(main())
