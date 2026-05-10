"""edi_roundtrip.py — EDI Round-Trip Test Generator (healthcare interop).

Generates a complete X12 round-trip test package for a given transaction
set. The generated package is a thin consumer of ``healthcare_libs.x12``:
fixtures are produced at GENERATION time by calling the lib's per-txn
builders and persisted as static ``.x12`` files; the generated test
file imports the lib and asserts parse/validate/round-trip behavior;
a ``transformer.py`` CLI exposes build/validate/extract operations.

Supported transaction sets:
  270/271   eligibility benefit inquiry / response
  834       benefit enrollment + maintenance
  835       claim payment / advice (remittance)
  837       claim (professional — 837P)
  997       functional acknowledgement (envelope-only fallback build)
  999       implementation acknowledgement (envelope-only fallback build)

Substrate: pulls X12 concept citations from `pyx12`, `Ballerina EDI
Module`, and `Stedi (clearinghouse)` doc sections.

Output structure:
    edi-roundtrip-<txn>/
      README.md               — transaction set overview + how to run
      mapping.md              — segment/loop reference + citation list
      transformer.py          — CLI: build / validate / extract
      fixtures/
        <txn>_request.x12     — built by healthcare_libs.x12.build_<txn>()
        <paired>_response.x12 — paired response when applicable
      tests/test_roundtrip.py — imports healthcare_libs.x12 and asserts
                                parse_envelope, round_trip, validate
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)
from healthcare_libs import x12 as _x12

LOG = logging.getLogger("mypub-edi-roundtrip")

GENERATOR_TYPE = "edi_roundtrip"

# Each entry is (transaction_set_code, display_name, what_it_does, paired_response)
# Pairs are the request → response patterns specific to X12 healthcare.
TRANSACTION_SETS: dict[str, dict[str, Any]] = {
    "270": {
        "name": "Eligibility, Coverage or Benefit Inquiry",
        "purpose": "Provider asks payer whether a patient is eligible for "
                   "coverage on a given service date.",
        "paired_response": "271",
        "key_loops": ["2000A Information Source", "2000B Information Receiver",
                      "2000C Subscriber", "2000D Dependent (optional)"],
        "key_segments": ["BHT", "HL", "NM1", "DMG", "DTP", "EQ"],
    },
    "271": {
        "name": "Eligibility, Coverage or Benefit Information",
        "purpose": "Payer's response to a 270 — confirms eligibility and "
                   "describes benefits.",
        "paired_response": None,  # this IS a response
        "key_loops": ["2000A", "2000B", "2000C", "2000D",
                      "2110C Subscriber Eligibility/Benefit Information"],
        "key_segments": ["BHT", "HL", "NM1", "EB", "MSG"],
    },
    "834": {
        "name": "Benefit Enrollment and Maintenance",
        "purpose": "Sponsor (employer / govt) sends member enrollment, "
                   "maintenance, and termination data to a health plan.",
        "paired_response": "999",
        "key_loops": ["1000A Sponsor", "1000B Payer",
                      "2000 Member Level Detail", "2100A Member Name"],
        "key_segments": ["BGN", "INS", "REF", "DTP", "NM1", "HD"],
    },
    "835": {
        "name": "Health Care Claim Payment / Advice (Remittance)",
        "purpose": "Payer's electronic remittance — payment + claim "
                   "adjudication detail back to the provider.",
        "paired_response": None,  # this is itself a response to 837
        "key_loops": ["1000A Payer Identification", "1000B Payee Identification",
                      "2000 Header Number", "2100 Claim Payment Information"],
        "key_segments": ["BPR", "TRN", "CLP", "CAS", "SVC"],
    },
    "837": {
        "name": "Health Care Claim (Professional / Institutional / Dental)",
        "purpose": "Provider submits a claim for services rendered.",
        "paired_response": "835",
        "key_loops": ["1000A Submitter Name", "1000B Receiver Name",
                      "2000A Billing Provider HL", "2000B Subscriber HL",
                      "2300 Claim Information", "2400 Service Line"],
        "key_segments": ["BHT", "NM1", "CLM", "HI", "LX", "SV1"],
    },
    "997": {
        "name": "Functional Acknowledgement",
        "purpose": "Acknowledges receipt + syntactic validity of any X12 "
                   "transaction (legacy — superseded by 999 in HIPAA).",
        "paired_response": None,
        "key_loops": ["AK2 Transaction Set Response Header"],
        "key_segments": ["AK1", "AK2", "AK5", "AK9"],
    },
    "999": {
        "name": "Implementation Acknowledgement",
        "purpose": "HIPAA-mandated acknowledgement — reports syntactic + "
                   "implementation guide compliance.",
        "paired_response": None,
        "key_loops": ["AK2", "IK3 Implementation Data Element Note"],
        "key_segments": ["AK1", "AK2", "IK3", "IK4", "IK5", "AK9"],
    },
}

# Some signature-segment hints we use in the generated test as
# "this segment must be present in the body". Picking the most
# distinctive one or two per txn keeps the assertion specific without
# forcing us to ship a full IG validator.
SIGNATURE_BODY_SEGMENTS: dict[str, list[str]] = {
    "270": ["BHT*0022*13", "EQ*"],
    "271": ["BHT*0022*11", "EB*"],
    "834": ["BGN*", "INS*Y*"],
    "835": ["BPR*I*", "CLP*"],
    "837": ["BHT*0019*", "CLM*"],
    "997": ["AK1*", "AK9*"],
    "999": ["AK1*", "AK9*"],
}


@dataclass
class _Decomposition:
    txn_code: str
    txn_meta: dict[str, Any]
    paired_response_code: Optional[str]
    paired_response_meta: Optional[dict[str, Any]]
    # Healthcare doc-source signal: concepts found in the corpus that match
    # this transaction set (segments, loops, related concepts)
    concept_citations: list[tuple[int, str, int, str]]  # (concept_id, name, doc_section_id, source_name)
    notes: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "txn"


# ---------------------------------------------------------------------------
# Fixture builders — delegate to healthcare_libs.x12 wherever possible.
# 997 / 999 don't have dedicated lib builders (no per-txn IG fixture in
# pyx12 we'd want to re-implement); we use build_envelope with a known
# acknowledgement body. This is the ONLY hand-written X12 in this file.
# ---------------------------------------------------------------------------

def build_message(txn_code: str, ctrl_num: int = 1) -> str:
    """Build a fixture for ``txn_code`` using ``healthcare_libs.x12``.

    For 270/271/834/835/837 this calls the lib's dedicated builder. For
    997/999 we compose a minimal acknowledgement body and wrap with
    ``build_envelope`` (the lib doesn't ship dedicated ack builders).

    ``ctrl_num`` propagates to ISA-13 / IEA-02 (zero-padded 9 digits)
    AND to GS-06 / GE-02 (bare integer).
    """
    # Lib builders accept generic envelope kwargs via build_envelope but
    # not directly — we call the typed builders and pass the ICN/GCN
    # the same way build_envelope does. For pairs (271, 835) the ICN
    # kwarg is the request's ICN; we set it to ctrl_num so paired
    # builds stay aligned.
    if txn_code == "270":
        body = _extract_body(_x12.build_270())
        return _x12.build_envelope(
            txn_set="270", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "271":
        body = _extract_body(_x12.build_271(request_270_icn=ctrl_num))
        return _x12.build_envelope(
            txn_set="271", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "834":
        body = _extract_body(_x12.build_834())
        return _x12.build_envelope(
            txn_set="834", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "835":
        body = _extract_body(_x12.build_835(paired_837_icn=ctrl_num))
        return _x12.build_envelope(
            txn_set="835", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "837":
        body = _extract_body(_x12.build_837p())
        return _x12.build_envelope(
            txn_set="837", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "997":
        body = ["AK1*HS*1234", "AK2*270*0001", "AK5*A", "AK9*A*1*1*1"]
        return _x12.build_envelope(
            txn_set="997", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    if txn_code == "999":
        body = [
            "AK1*HS*1234*005010X279A1",
            "AK2*270*0001*005010X279A1",
            "IK5*A",
            "AK9*A*1*1*1",
        ]
        return _x12.build_envelope(
            txn_set="999", body_segments=body, icn=ctrl_num, gcn=ctrl_num,
        )
    raise ValueError(f"unsupported transaction set: {txn_code!r}")


def _extract_body(x12_msg: str) -> list[str]:
    """Round-trip helper: pull the ST-internal body out of a built msg.

    Lets us re-call build_envelope with custom envelope kwargs (icn, gcn)
    without having to copy the lib's per-txn body construction. The lib's
    typed builders don't take envelope kwargs directly, so we build once
    with defaults, peel the body, then wrap with the desired envelope.
    """
    _, body = _x12.parse_envelope(x12_msg)
    return body


def build_paired_messages(
    txn_code: str, paired_code: Optional[str],
) -> tuple[str, Optional[str]]:
    """Build (request, response) fixtures with matching ICNs when paired.

    Returns (request_x12, response_x12_or_None). The response reuses the
    request's ICN so paired-control-number tracking works downstream.
    """
    request = build_message(txn_code, ctrl_num=1)
    if not paired_code:
        return request, None
    req_env, _ = _x12.parse_envelope(request)
    response = build_message(paired_code, ctrl_num=req_env.icn)
    return request, response


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------

class EdiRoundTripDecomposer:
    """Look up the transaction set spec metadata + find supporting concept
    citations from the X12-related doc sources in the catalog."""

    # Doc source names that carry X12 / EDI knowledge
    X12_SOURCES = ("pyx12", "Ballerina EDI Module", "Stedi (clearinghouse)")

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,  # unused — txn code is the input, not a free-form query
        query: str,
        **kwargs: Any,
    ) -> _Decomposition:
        # Normalize: accept "270", "834", "270/271", "837P", etc.
        txn_code = query.strip().upper().split("/")[0].rstrip("PID")
        if txn_code not in TRANSACTION_SETS:
            return _Decomposition(
                txn_code=txn_code, txn_meta={},
                paired_response_code=None, paired_response_meta=None,
                concept_citations=[],
                notes=[f"unknown transaction set: {query!r} (supported: "
                       f"{sorted(TRANSACTION_SETS)})"],
            )
        meta = TRANSACTION_SETS[txn_code]
        paired = meta.get("paired_response")
        paired_meta = TRANSACTION_SETS.get(paired) if paired else None

        # Find concepts in the corpus that name X12 segments / loops / related
        # concepts for this txn — pull from doc_sources we ingested for X12.
        candidate_terms = (
            meta.get("key_segments", []) + meta.get("key_loops", []) +
            ["X12", "EDI", "HIPAA"] + [f"{txn_code} transaction"]
        )
        concept_citations: list[tuple[int, str, int, str]] = []
        for term in candidate_terms:
            rows = conn.execute(
                f"""
                SELECT DISTINCT c.concept_id, c.name, ds.doc_section_id, src.name
                  FROM concept c
                  JOIN concept_relation cr ON cr.from_concept_id = c.concept_id
                                           OR cr.to_concept_id = c.concept_id
                  JOIN doc_section ds ON cr.source_id = ds.doc_section_id
                                      AND cr.source_type = 'doc_section'
                  JOIN doc_snapshot sn USING (snapshot_id)
                  JOIN doc_source src ON sn.doc_source_id = src.doc_source_id
                 WHERE LOWER(c.name) LIKE LOWER(?)
                   AND src.name IN ({','.join(['?'] * len(self.X12_SOURCES))})
                 LIMIT 5
                """,
                [f"%{term}%"] + list(self.X12_SOURCES),
            ).fetchall()
            for r in rows:
                concept_citations.append((int(r[0]), r[1], int(r[2]), r[3]))

        # Dedupe by concept_id
        seen: set[int] = set()
        unique: list[tuple[int, str, int, str]] = []
        for c in concept_citations:
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)

        notes: list[str] = []
        if not unique:
            notes.append(
                "no X12 concepts found in catalog for this transaction set's "
                "segment/loop terms — fixtures will be generated from the "
                "spec metadata only, without per-segment citations."
            )
        return _Decomposition(
            txn_code=txn_code, txn_meta=meta,
            paired_response_code=paired, paired_response_meta=paired_meta,
            concept_citations=unique, notes=notes,
        )


# ---------------------------------------------------------------------------
# Renderers — README, mapping, transformer, test
# ---------------------------------------------------------------------------

def _render_readme(d: _Decomposition) -> str:
    meta = d.txn_meta
    lines = [
        f"# X12 {d.txn_code} — {meta.get('name', 'Transaction Set')}",
        "",
        f"**Purpose.** {meta.get('purpose', '')}",
        "",
    ]
    if d.paired_response_code:
        pr = d.paired_response_meta or {}
        lines.extend([
            f"**Paired response: {d.paired_response_code}** "
            f"({pr.get('name', '')}).",
            "",
            f"This package emits both the {d.txn_code} request and the "
            f"{d.paired_response_code} response for round-trip testing.",
            "",
        ])
    lines.extend([
        "## Key segments",
        "",
        ", ".join(meta.get("key_segments", [])) or "(none recorded)",
        "",
        "## Key loops",
        "",
        "- " + "\n- ".join(meta.get("key_loops", [])) if meta.get("key_loops") else "(flat structure — no loops)",
        "",
        "## How the package works",
        "",
        "This package is a **thin consumer** of the project's X12 reference",
        "library (`healthcare_libs.x12`) — it does not carry its own X12",
        "envelope / segment logic. The library exposes:",
        "",
        "- `build_envelope`, `parse_envelope` — ISA/GS/ST/SE/GE/IEA round-trip",
        "- `build_270`, `build_271`, `build_834`, `build_835`, `build_837p`",
        "  — minimal-but-conformant per-transaction builders",
        "- `validate` — wraps pyx12's structural validator + our own",
        "  SE-count / ST-set checks",
        "- `round_trip(x12)` — `True` iff parse(build(x)) preserves the body",
        "- `get_segments(x12, id)` — pull every segment of a given type",
        "",
        "The static fixtures under `fixtures/` were produced by calling the",
        "library's `build_<txn>()` functions at package-generation time. The",
        "generated tests in `tests/test_roundtrip.py` import the same library",
        "and assert that re-parsing each fixture round-trips and validates.",
        "",
        "## How to run",
        "",
        "```bash",
        "# 1. Make sure healthcare_libs is on PYTHONPATH:",
        "export PYTHONPATH=/path/to/myPub/mcp-servers/kb-mcp:$PYTHONPATH",
        "",
        "# 2. Run the round-trip tests:",
        "python -m pytest tests/test_roundtrip.py -v",
        "",
        "# 3. Use the transformer CLI:",
        f"python transformer.py build    --txn {d.txn_code} --output /tmp/{d.txn_code}.x12",
        f"python transformer.py validate --input /tmp/{d.txn_code}.x12",
        f"python transformer.py extract  --input /tmp/{d.txn_code}.x12 --segment NM1",
        "```",
        "",
        "Tests assert that:",
        "1. Each fixture parses cleanly with `healthcare_libs.x12.parse_envelope`.",
        "2. `healthcare_libs.x12.round_trip(fixture)` returns `True`.",
        "3. `healthcare_libs.x12.validate(fixture)` reports no errors.",
    ])
    if d.paired_response_code:
        lines.append(
            f"4. The {d.paired_response_code} response shares the {d.txn_code} "
            "request's ICN (paired control-number alignment)."
        )
    lines.append("")
    if d.concept_citations:
        lines.extend(["## Cited concepts from the corpus", ""])
        for cid, cname, _ds, src in d.concept_citations[:15]:
            lines.append(f"- **{cname}** — {src}")
        lines.append("")
    elif d.notes:
        lines.extend(["## Notes", ""])
        for n in d.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


def _render_mapping(d: _Decomposition) -> str:
    meta = d.txn_meta
    lines = [
        f"# {d.txn_code} — Segment + Loop Reference",
        "",
        f"_{meta.get('purpose', '')}_",
        "",
        "## Segments used in this fixture",
        "",
    ]
    for seg in meta.get("key_segments", []):
        lines.append(f"- `{seg}`")
    lines.extend(["", "## Loops used in this fixture", ""])
    for loop in meta.get("key_loops", []):
        lines.append(f"- {loop}")
    if d.concept_citations:
        lines.extend(["", "## Citations from the catalog", ""])
        for cid, cname, ds, src in d.concept_citations:
            lines.append(f"- {cname} (concept_id={cid}, doc_section={ds}, source={src})")
    return "\n".join(lines)


def _render_transformer(d: _Decomposition) -> str:
    """Generate a self-contained CLI script that uses healthcare_libs.x12."""
    txn = d.txn_code
    paired = d.paired_response_code or "None"
    return f'''"""transformer.py — CLI for X12 {txn} build / validate / extract.

This is a thin wrapper over ``healthcare_libs.x12``. Every operation
delegates to the library; no X12 envelope / segment logic lives here.

Supported operations
--------------------
- ``build``      — emit a synthetic spec-conformant {txn} fixture
- ``validate``   — run ``healthcare_libs.x12.validate`` on an X12 file
                   and print structured issues
- ``extract``    — print every occurrence of a given segment ID
                   (e.g. NM1, CLP, SVC) using ``get_segments``

Usage
-----
    python transformer.py build    --txn {txn} --output out.x12
    python transformer.py validate --input  out.x12
    python transformer.py extract  --input  out.x12 --segment NM1

The script assumes ``healthcare_libs`` is importable, i.e. either:
- the myPub project's ``mcp-servers/kb-mcp/`` is on ``PYTHONPATH``, or
- the project has been installed (``pip install -e .[healthcare]``).
"""
from __future__ import annotations

import argparse
import sys

from healthcare_libs import x12


# Map txn code → builder. We delegate entirely; no inline X12 here.
_BUILDERS = {{
    "270": lambda: x12.build_270(),
    "271": lambda: x12.build_271(request_270_icn=1),
    "834": lambda: x12.build_834(),
    "835": lambda: x12.build_835(paired_837_icn=1),
    "837": lambda: x12.build_837p(),
}}


def cmd_build(args: argparse.Namespace) -> int:
    txn = args.txn
    if txn not in _BUILDERS:
        # 997 / 999 use envelope-only fallback: the library's
        # build_envelope handles both via FG_CODES.
        if txn == "997":
            body = ["AK1*HS*1234", "AK2*270*0001", "AK5*A", "AK9*A*1*1*1"]
            msg = x12.build_envelope(txn_set="997", body_segments=body)
        elif txn == "999":
            body = [
                "AK1*HS*1234*005010X279A1",
                "AK2*270*0001*005010X279A1",
                "IK5*A",
                "AK9*A*1*1*1",
            ]
            msg = x12.build_envelope(txn_set="999", body_segments=body)
        else:
            print(f"unsupported txn: {{txn!r}}", file=sys.stderr)
            return 2
    else:
        msg = _BUILDERS[txn]()
    if args.output:
        with open(args.output, "w") as f:
            f.write(msg)
        print(f"wrote {{len(msg)}} bytes to {{args.output}}")
    else:
        sys.stdout.write(msg)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    with open(args.input) as f:
        data = f.read()
    issues = x12.validate(data)
    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        print(f"[{{issue.severity:7}}] {{issue.code:8}} {{issue.message}}")
        if issue.segment_context:
            print(f"           context: {{issue.segment_context}}")
    if errors:
        print(f"\\n{{len(errors)}} error(s)", file=sys.stderr)
        return 1
    print(f"\\nvalidation passed ({{len(issues)}} info/warning issue(s))")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    with open(args.input) as f:
        data = f.read()
    matches = x12.get_segments(data, args.segment)
    if not matches:
        print(f"no {{args.segment}} segments found")
        return 0
    for fields in matches:
        print("*".join(fields))
    print(f"\\n{{len(matches)}} {{args.segment}} segment(s) total")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="emit a synthetic X12 fixture")
    p_build.add_argument("--txn", required=True,
                         help="transaction set code (270/271/834/835/837/997/999)")
    p_build.add_argument("--output", default="",
                         help="write to this file (default: stdout)")
    p_build.set_defaults(func=cmd_build)

    p_val = sub.add_parser("validate", help="validate an X12 file")
    p_val.add_argument("--input", required=True, help="path to X12 file")
    p_val.set_defaults(func=cmd_validate)

    p_ext = sub.add_parser("extract", help="print all matches of a segment ID")
    p_ext.add_argument("--input", required=True, help="path to X12 file")
    p_ext.add_argument("--segment", required=True,
                       help="segment ID (e.g. NM1, CLP, SVC)")
    p_ext.set_defaults(func=cmd_extract)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _render_test(d: _Decomposition) -> str:
    """Generate the test file. Imports healthcare_libs.x12 directly."""
    txn = d.txn_code
    paired = d.paired_response_code
    sigs = SIGNATURE_BODY_SEGMENTS.get(txn, [])
    sig_assertions = "\n".join(
        f"    assert any({sig!r} in s for s in body), "
        f"f'{txn} body missing signature segment {sig}'"
        for sig in sigs
    ) or "    # (no signature segments declared for this txn)"

    paired_block = ""
    if paired:
        paired_sigs = SIGNATURE_BODY_SEGMENTS.get(paired, [])
        paired_sig_assertions = "\n".join(
            f"    assert any({sig!r} in s for s in body), "
            f"f'{paired} body missing signature segment {sig}'"
            for sig in paired_sigs
        ) or "    # (no signature segments declared for this txn)"
        paired_block = f'''

def test_{paired}_response_parses_cleanly():
    """The paired {paired} response parses with healthcare_libs.x12."""
    msg = _read("{paired}_response.x12")
    env, body = x12.parse_envelope(msg)
    assert env.txn_set == "{paired}", \\
        f"expected ST-01={paired}, got {{env.txn_set!r}}"
{paired_sig_assertions}


def test_{paired}_response_round_trips():
    """healthcare_libs.x12.round_trip returns True for the {paired} fixture."""
    msg = _read("{paired}_response.x12")
    assert x12.round_trip(msg)


def test_{paired}_response_validates_clean():
    """healthcare_libs.x12.validate reports no errors for the {paired} fixture."""
    msg = _read("{paired}_response.x12")
    issues = x12.validate(msg)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, \\
        f"validate() returned errors: {{[(i.code, i.message) for i in errors]}}"


def test_paired_icn_matches():
    """The paired {paired} response must reuse the {txn} request's ICN."""
    req = _read("{txn}_request.x12")
    resp = _read("{paired}_response.x12")
    req_env, _ = x12.parse_envelope(req)
    resp_env, _ = x12.parse_envelope(resp)
    assert req_env.icn == resp_env.icn, \\
        f"paired ICN mismatch: req={{req_env.icn}} resp={{resp_env.icn}}"
'''

    return f'''"""Round-trip parse + validate tests for X12 {txn}.

These tests exercise the static fixtures shipped under ``fixtures/``
through the project's X12 reference library. The fixtures themselves
were produced by calling ``healthcare_libs.x12.build_<txn>()`` at
package-generation time, so a passing test demonstrates that:

  1. parse_envelope can re-parse what build_<txn> emits
  2. round_trip(...) is True on the canonical fixture
  3. validate(...) returns no error-severity issues
  4. paired (request, response) fixtures share an ICN

Run with PYTHONPATH set so ``healthcare_libs`` resolves, e.g.

    PYTHONPATH=/path/to/myPub/mcp-servers/kb-mcp \\
        python -m pytest tests/test_roundtrip.py -v
"""
from __future__ import annotations

from pathlib import Path

from healthcare_libs import x12


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---- {txn} request -----------------------------------------------------

def test_{txn}_request_parses_cleanly():
    """The {txn} fixture parses with healthcare_libs.x12.parse_envelope."""
    msg = _read("{txn}_request.x12")
    env, body = x12.parse_envelope(msg)
    assert env.txn_set == "{txn}", \\
        f"expected ST-01={txn}, got {{env.txn_set!r}}"
    assert body, "body has no segments"
{sig_assertions}


def test_{txn}_request_round_trips():
    """healthcare_libs.x12.round_trip returns True for the {txn} fixture."""
    msg = _read("{txn}_request.x12")
    assert x12.round_trip(msg)


def test_{txn}_request_validates_clean():
    """healthcare_libs.x12.validate reports no errors for the {txn} fixture."""
    msg = _read("{txn}_request.x12")
    issues = x12.validate(msg)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, \\
        f"validate() returned errors: {{[(i.code, i.message) for i in errors]}}"
{paired_block}'''


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class EdiRoundTripPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        if not d.txn_meta:
            return GenPlan(
                generator_type=GENERATOR_TYPE,
                package_name=package_name or "edi-unknown",
                domain=d.txn_code,
                source_query=d.txn_code,
                package_metadata={"error": "unknown transaction"},
                notes=list(d.notes),
            )

        pkg_name = package_name or f"edi-roundtrip-{d.txn_code.lower()}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=f"X12 {d.txn_code} — {d.txn_meta.get('name', '')}",
            source_query=d.txn_code,
            package_metadata={
                "txn_code": d.txn_code,
                "paired_response": d.paired_response_code,
                "n_citations": len(d.concept_citations),
            },
            notes=list(d.notes),
        )

        # Unit per generated artifact (helps provenance + future regeneration)
        sources = [
            ("concept", cid, 1.0, 1.0, None)
            for cid, _, _, _ in d.concept_citations[:10]
        ]
        sources += [
            ("doc_section", ds, 1.0, 1.0, None)
            for _, _, ds, _ in d.concept_citations[:10]
        ]

        plan.units.append(GenUnit(
            unit_type="x12_transaction_set",
            name=d.txn_code,
            ordinal=1,
            metadata={"txn_code": d.txn_code,
                      "paired_response": d.paired_response_code},
            logical_key="txn_main",
            sources=sources,
        ))

        # Build the fixtures by calling healthcare_libs.x12 at GEN TIME.
        # The result lands as a static file in the package — consumers
        # don't need the lib to read fixtures, only to run the tests.
        request_x12, response_x12 = build_paired_messages(
            d.txn_code, d.paired_response_code,
        )

        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="mapping.md", content=_render_mapping(d), purpose="reference"),
            GenFile(filename="transformer.py",
                    content=_render_transformer(d), purpose="cli"),
            GenFile(filename=f"fixtures/{d.txn_code}_request.x12",
                    content=request_x12, purpose="fixture"),
            GenFile(filename="tests/test_roundtrip.py",
                    content=_render_test(d), purpose="test"),
        ])
        if response_x12 is not None:
            plan.files.append(GenFile(
                filename=f"fixtures/{d.paired_response_code}_response.x12",
                content=response_x12, purpose="fixture",
            ))
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class EdiRoundTripValidator:
    def validate(self, conn, plan: GenPlan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("error") == "unknown transaction":
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="unknown X12 transaction set",
            ))
            return issues
        # Every fixture file must round-trip + validate via the lib.
        for f in plan.files:
            if f.filename.endswith(".x12"):
                if "ISA*" not in f.content or "IEA*" not in f.content:
                    issues.append(ValidationIssue(
                        unit_logical_key="txn_main", severity="error",
                        message=f"fixture {f.filename} missing ISA/IEA envelope",
                    ))
                    continue
                # Validate via the lib — generator output should be clean.
                lib_issues = _x12.validate(f.content)
                lib_errors = [i for i in lib_issues if i.severity == "error"]
                for li in lib_errors:
                    issues.append(ValidationIssue(
                        unit_logical_key="txn_main", severity="error",
                        message=f"healthcare_libs.x12.validate({f.filename}): "
                                f"{li.code} {li.message}",
                    ))
                if not _x12.round_trip(f.content):
                    issues.append(ValidationIssue(
                        unit_logical_key="txn_main", severity="error",
                        message=f"fixture {f.filename} fails round_trip()",
                    ))
        if plan.package_metadata.get("n_citations", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="txn_main", severity="warning",
                message="no concept citations from X12 doc sources — "
                        "fixtures generated from spec metadata only",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------

class EdiRoundTripMaterializer:
    def materialize(self, conn, package_id, output_root, *, overwrite=True):
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?",
            [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = conn.execute(
            "SELECT filename, content FROM generated_file "
            "WHERE package_id = ? ORDER BY file_id",
            [package_id],
        ).fetchall()
        written: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                continue
            target.write_text(content)
            written.append(str(target))
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=[],
        )


def make_edi_roundtrip_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=EdiRoundTripDecomposer(),
        planner=EdiRoundTripPlanner(),
        ranking_mode="generation",
        validator=EdiRoundTripValidator(),
        materializer=EdiRoundTripMaterializer(),
    )
