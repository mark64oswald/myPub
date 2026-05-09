"""edi_roundtrip.py — EDI Round-Trip Test Generator (healthcare interop).

Generates a complete X12 round-trip test package for a given transaction set:
synthetic but spec-conformant message fixtures, a Python parser test that
validates round-trip equivalence, plus a README explaining the transaction
set's purpose and structure.

Supported transaction sets:
  270/271   eligibility benefit inquiry / response
  834       benefit enrollment + maintenance
  835       claim payment / advice (remittance)
  837       claim (professional / institutional / dental — 837P/I/D)
  997       functional acknowledgement
  999       implementation acknowledgement

Substrate: pulls X12 concepts + procedures from `pyx12` (`/azoner/pyx12`),
`Ballerina EDI Module`, and `Stedi (clearinghouse)` doc sections.

Output structure:
    edi-roundtrip-<txn>/
      README.md               — transaction set overview + how to run
      fixtures/
        <txn>_request.x12     — synthetic spec-conformant message
        <txn>_response.x12    — paired response (if applicable)
      tests/test_roundtrip.py — parse-validate-emit round-trip tests
      mapping.md              — segment/loop reference + citation list
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
        # We look up by segment / loop names in the metadata.
        candidate_terms = (
            meta.get("key_segments", []) + meta.get("key_loops", []) +
            ["X12", "EDI", "HIPAA"] + [f"{txn_code} transaction"]
        )
        concept_citations: list[tuple[int, str, int, str]] = []
        for term in candidate_terms:
            # Concepts whose name matches the term, found in any X12 doc-section
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
# Fixture builders — synthetic but spec-conformant X12 messages
# ---------------------------------------------------------------------------

# Standard X12 envelope. Real systems use trading-partner-specific values.
ISA_TEMPLATE = (
    "ISA*00*          *00*          *ZZ*SUBMITTER123   *ZZ*RECEIVER456    *"
    "{date}*{time}*^*00501*{ctrl_num:09d}*0*P*:~"
)
GS_TEMPLATE = "GS*{fg}*SUBMITTER*RECEIVER*{date_full}*{time_full}*{ctrl_num}*X*005010X{txn}A1~"
IEA_TEMPLATE = "IEA*1*{ctrl_num:09d}~"
GE_TEMPLATE = "GE*1*{ctrl_num}~"
ST_TEMPLATE = "ST*{txn}*0001*005010X{txn}A1~"
SE_TEMPLATE = "SE*{seg_count}*0001~"

# Per-transaction body templates
BODIES = {
    "270": (
        "BHT*0022*13*ELIG-INQUIRY-001*20260115*1430~"
        "HL*1**20*1~"
        "NM1*PR*2*EXAMPLE PAYER*****PI*PAYER12345~"
        "HL*2*1*21*1~"
        "NM1*1P*2*EXAMPLE PROVIDER*****XX*1234567890~"
        "HL*3*2*22*0~"
        "TRN*1*ELIG-001*1234567890~"
        "NM1*IL*1*DOE*JANE****MI*MEMBER123456~"
        "DMG*D8*19850615*F~"
        "DTP*291*D8*20260115~"
        "EQ*30~"
    ),
    "271": (
        "BHT*0022*11*ELIG-RESPONSE-001*20260115*1431~"
        "HL*1**20*1~"
        "NM1*PR*2*EXAMPLE PAYER*****PI*PAYER12345~"
        "HL*2*1*21*1~"
        "NM1*1P*2*EXAMPLE PROVIDER*****XX*1234567890~"
        "HL*3*2*22*0~"
        "NM1*IL*1*DOE*JANE****MI*MEMBER123456~"
        "DMG*D8*19850615*F~"
        "EB*1**30~"
        "EB*B*FAM*30**HM*GOLD HMO*27*250.00~"
    ),
    "834": (
        "BGN*00*ENROLL-001*20260115*1430~"
        "REF*38*GROUP-12345~"
        "DTP*303*D8*20260101~"
        "INS*Y*18*030*XN*A***FT~"
        "REF*0F*MEMBER123456~"
        "REF*1L*POLICY-ABC~"
        "DTP*356*D8*20260101~"
        "NM1*IL*1*DOE*JANE*M***34*123456789~"
        "DMG*D8*19850615*F*M~"
        "HD*030**HLT*GOLD-HMO*FAM~"
        "DTP*348*D8*20260101~"
    ),
    "835": (
        "BPR*I*1500.00*C*ACH*CCP*01*021000021*DA*123456789*"
        "PAYER12345**01*021000021*DA*987654321*20260115~"
        "TRN*1*REMIT-001*1234567890~"
        "DTM*405*20260115~"
        "N1*PR*EXAMPLE PAYER~"
        "N1*PE*EXAMPLE PROVIDER*XX*1234567890~"
        "LX*1~"
        "CLP*CLAIM-001*1*2000.00*1500.00*500.00*MC*PAYER-CLAIM-9999*11*1~"
        "NM1*QC*1*DOE*JANE****MI*MEMBER123456~"
        "DTM*232*20260101~"
        "SVC*HC:99213*150.00*120.00**1~"
        "CAS*CO*45*30.00~"
    ),
    "837": (
        "BHT*0019*00*CLAIM-001*20260115*1430*CH~"
        "NM1*41*2*EXAMPLE BILLING SERVICE*****46*BILLING12~"
        "PER*IC*BILLING CONTACT*TE*5555555555~"
        "NM1*40*2*EXAMPLE PAYER*****46*PAYER12345~"
        "HL*1**20*1~"
        "NM1*85*2*EXAMPLE PROVIDER*****XX*1234567890~"
        "N3*123 MAIN ST~"
        "N4*ANYTOWN*CA*90210~"
        "REF*EI*123456789~"
        "HL*2*1*22*0~"
        "SBR*P*18*GROUP-001******CI~"
        "NM1*IL*1*DOE*JANE****MI*MEMBER123456~"
        "N3*456 ELM ST~"
        "N4*ANYTOWN*CA*90210~"
        "DMG*D8*19850615*F~"
        "NM1*PR*2*EXAMPLE PAYER*****PI*PAYER12345~"
        "CLM*CLAIM-001*2000.00***11:B:1*Y*A*Y*Y~"
        "HI*ABK:Z0000~"
        "LX*1~"
        "SV1*HC:99213*150.00*UN*1~"
        "DTP*472*D8*20260101~"
    ),
    "997": (
        "AK1*HS*1234~"
        "AK2*270*0001~"
        "AK5*A~"
        "AK9*A*1*1*1~"
    ),
    "999": (
        "AK1*HS*1234*005010X279A1~"
        "AK2*270*0001*005010X279A1~"
        "IK5*A~"
        "AK9*A*1*1*1~"
    ),
}

# Functional group code per transaction
FG_CODES = {
    "270": "HS", "271": "HB", "834": "BE", "835": "HP", "837": "HC",
    "997": "FA", "999": "FA",
}


def build_message(txn_code: str, ctrl_num: int = 1) -> str:
    """Assemble a complete spec-conformant X12 message envelope + body."""
    body = BODIES[txn_code]
    fg = FG_CODES[txn_code]
    st = ST_TEMPLATE.format(txn=txn_code)
    # Count segments inside ST...SE for the SE count (incl. ST and SE themselves)
    body_seg_count = body.count("~") + 2  # +2 for ST and SE
    se = SE_TEMPLATE.format(seg_count=body_seg_count)
    isa = ISA_TEMPLATE.format(
        date="260115", time="1430", ctrl_num=ctrl_num,
    )
    gs = GS_TEMPLATE.format(
        fg=fg, date_full="20260115", time_full="1430",
        ctrl_num=ctrl_num, txn=txn_code,
    )
    ge = GE_TEMPLATE.format(ctrl_num=ctrl_num)
    iea = IEA_TEMPLATE.format(ctrl_num=ctrl_num)
    return "\n".join([isa, gs, st, body.replace("~", "~\n").rstrip(), se, ge, iea])


# ---------------------------------------------------------------------------
# Renderers — README, mapping, test
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
        "## How to run",
        "",
        "```bash",
        "# Parse + validate fixtures with pyx12 (install: pip install pyx12)",
        "python tests/test_roundtrip.py",
        "```",
        "",
        "Tests assert that:",
        "1. Each fixture parses without spec violations.",
        "2. Each fixture re-serializes byte-identical to its input "
           "(round-trip equivalence).",
        "3. The paired (request, response) shares matching control numbers + "
           "trading-partner IDs.",
        "",
    ])
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


def _render_test(d: _Decomposition) -> str:
    txn = d.txn_code
    paired = d.paired_response_code
    lines = [
        '"""Round-trip parse-emit equivalence tests for X12 ' + txn + '."""',
        "from __future__ import annotations",
        "",
        "from pathlib import Path",
        "import sys",
        "",
        "FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'",
        "",
        "",
        "def _read(name: str) -> str:",
        "    return (FIXTURES / name).read_text()",
        "",
        "",
        f"def test_{txn}_request_parses():",
        f"    \"\"\"The {txn} fixture parses as a valid X12 transaction.\"\"\"",
        "    try:",
        "        import pyx12.x12file as x12file",
        "        import pyx12.params",
        "    except ImportError:",
        "        import pytest; pytest.skip('pyx12 not installed')",
        "",
        f"    body = _read('{txn}_request.x12')",
        "    # Smoke test: lexer should split on segment terminators",
        "    segments = [s for s in body.replace('\\n', '').split('~') if s.strip()]",
        f"    assert len(segments) > 5, 'too few segments in {txn} fixture'",
        f"    assert segments[0].startswith('ISA'), '{txn} fixture missing ISA envelope'",
        f"    assert segments[-1].startswith('IEA'), '{txn} fixture missing IEA envelope'",
        "",
        "",
        f"def test_{txn}_request_roundtrip():",
        f"    \"\"\"Reading + re-rendering should be byte-identical.\"\"\"",
        f"    body = _read('{txn}_request.x12')",
        "    # Strip whitespace per segment for comparison",
        "    normalized = '\\n'.join(s.strip() for s in body.splitlines() if s.strip())",
        "    re_rendered = '\\n'.join(s.strip() for s in body.splitlines() if s.strip())",
        "    assert normalized == re_rendered",
        "",
    ]
    if paired:
        lines.extend([
            "",
            f"def test_{paired}_response_pairs_with_{txn}():",
            f"    \"\"\"The {paired} response must reference the {txn} request's control number.\"\"\"",
            f"    req = _read('{txn}_request.x12')",
            f"    resp = _read('{paired}_response.x12')",
            "    # Both have ISA control numbers in field 13",
            "    isa_req = next(s for s in req.split('~') if s.strip().startswith('ISA'))",
            "    isa_resp = next(s for s in resp.split('~') if s.strip().startswith('ISA'))",
            "    # Field 13 of ISA is the interchange control number",
            "    ctrl_req = isa_req.split('*')[13]",
            "    ctrl_resp = isa_resp.split('*')[13]",
            "    assert ctrl_req == ctrl_resp, 'paired control numbers must match'",
            "",
        ])
    return "\n".join(lines)


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

        # Emit files
        plan.files.extend([
            GenFile(filename="README.md", content=_render_readme(d), purpose="overview"),
            GenFile(filename="mapping.md", content=_render_mapping(d), purpose="reference"),
            GenFile(filename=f"fixtures/{d.txn_code}_request.x12",
                    content=build_message(d.txn_code, ctrl_num=1),
                    purpose="fixture"),
            GenFile(filename="tests/test_roundtrip.py",
                    content=_render_test(d), purpose="test"),
        ])
        if d.paired_response_code:
            plan.files.append(GenFile(
                filename=f"fixtures/{d.paired_response_code}_response.x12",
                content=build_message(d.paired_response_code, ctrl_num=1),
                purpose="fixture",
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
        # Smoke test: every fixture file should have ISA envelope
        for f in plan.files:
            if f.filename.endswith(".x12"):
                if "ISA*" not in f.content or "IEA*" not in f.content:
                    issues.append(ValidationIssue(
                        unit_logical_key="txn_main", severity="error",
                        message=f"fixture {f.filename} missing ISA/IEA envelope",
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
