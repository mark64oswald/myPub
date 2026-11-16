"""odcs_contract_gen.py — ``/kb-data-contract``: an ODCS data contract for one table (2026-11).

Turns a table's columns (a ``CREATE TABLE`` statement or short
``name type [pk|required]`` specs) into a Bitol ODCS v3.1.0 data contract:

    <package>/
      <name>.odcs.yaml           schema (logical + physical types, keys, restricted
                                 columns), quality rules, SLA properties, server,
                                 team and support
      README.md                  what to fill in and how to check it
      checklist.md               what the KB's books and docs say about data contracts
      _provenance.md             ODCS doc sections and KB sources used
      _validation.md             ODCS JSON Schema check (+ SDK alignment when installed)
      _prompts/fill-contract.md  optional subagent pass that replaces the TODOs

Deterministic. Quality rules come from the columns (row count, unique keys,
required columns not null); SLA values are marked TODO, never invented as
facts. Columns whose names suggest personal data get ``classification:
restricted`` for the owner to review.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import duckdb

import kb_scaffold as ks
import odp_gen as og
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "data_contract"


def _checklist(tm: Optional[ks.TopicMaterial], cites: Sequence[ks.Citation]) -> str:
    lines = ["# Data contract checklist", "",
             "What the knowledge base says to settle before the contract goes active.", ""]
    if tm and tm.description:
        lines += [f"**{tm.name}.** " + " ".join(tm.description.split()), ""]
    if tm and tm.procedures:
        lines += ["## Procedures from the library", ""]
        for p in tm.procedures:
            src = f" _({p.source.label})_" if p.source else ""
            lines.append(f"- **{p.name}.** {ks.first_sentence(p.steps, 240)}{src}")
            if (p.failure_modes or "").strip():
                lines.append(f"  - Watch out: {ks.first_sentence(p.failure_modes, 200)}")
        lines.append("")
    if tm and tm.related:
        lines += ["## Related concepts", "",
                  ", ".join(sorted({n for _, n, _ in tm.related})), ""]
    lines += [
        "## Before you mark it active", "",
        "- [ ] Every column has a real description and the right logical type.",
        "- [ ] Columns marked `classification: restricted` are reviewed; personal or health",
        "      data has an access policy, and no real values appear in examples.",
        "- [ ] SLA values (availability, frequency, latency, retention, time to repair) are",
        "      agreed with the consumers, not left at the draft defaults.",
        "- [ ] Quality rules match what the pipeline can actually check.",
        "- [ ] The server block points at the real location.",
        "- [ ] Owner, team and support channel are real.",
        "",
    ]
    lines.append(ks.render_citations(cites, "ODCS sections behind this checklist"))
    return "\n".join(lines)


def _readme(name: str, table: str, file: str, platform: str, n_cols: int,
            restricted: Sequence[str], purpose: str) -> str:
    restricted_line = (f"- {len(restricted)} column(s) look like personal data and are marked "
                       f"`classification: restricted`: {', '.join(restricted)}.\n"
                       if restricted else "")
    return f"""\
# Data contract: {og.title(name)}

{purpose}

`{file}` is an [ODCS](https://bitol-io.github.io/open-data-contract-standard/) {og.ODCS_VERSION}
data contract for `{table}` ({n_cols} columns, server type `{platform}`).

## What is filled in, and what isn't

- Schema: every column with its physical and logical type, keys and required flags.
- Quality: the table has rows, keys are unique, required columns are never null.
- SLA: availability, frequency, latency, retention and time to repair, at **draft values**.
{restricted_line}- Everything marked `TODO` needs a real answer: descriptions, SLA values, owner,
  support channel and server details.

## Check it

```bash
# ODCS JSON Schema (what _validation.md ran)
pip install datacontract-cli && datacontract lint {file}

# Wrap it as an ODPS 4.2 product draft (Open Data Product SDK)
pip install open-data-products
open-data-products product wrap-contract --input {file} --output products/{name}.odps.yaml
open-data-products product audit products/{name}.odps.yaml --contract {file}
```

See `checklist.md` for what the library says to settle first.
"""


FILL_PROMPT = """\
# Fill in the {name} data contract

Edit `{file}` in place. It is an ODCS {version} contract; keep it valid.

Rules:
- Replace every `TODO` with a real value from the table's metadata, the
  owning team or the request. If you can't find the answer, leave the TODO
  and add the question to the README under "Open questions".
- Never invent SLA numbers or quality thresholds; propose them as questions.
- Don't copy real rows into `examples`. Columns marked `restricted` may hold
  personal or health data: describe them, never sample them.
- Keep `apiVersion`, `kind`, `id`, the column names and the server `type`.
- Re-check with `datacontract lint {file}` when available.

Background: `checklist.md` (what the library says) and `_provenance.md`.
"""


class ContractDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        ddl_table, cols = og.parse_ddl(kw.get("ddl") or "")
        if not cols:
            cols = og.parse_columns(kw.get("columns") or [])
        location = kw.get("location") or ddl_table
        table = (location or query).split(".")[-1].strip() or "dataset"
        table = og.snake(table) if " " in table else table
        name = og.slug(kw.get("contract_name") or table.replace("_", "-"))
        text = " ".join(str(x) for x in (query, kw.get("platform") or "", kw.get("ddl") or ""))
        platform = (kw.get("platform") or og.detect_platform(text)).lower()
        tm = ks.topic_material(conn, resolver, "Data Contract", max_procedures=6,
                               purpose=kw.get("purpose"))
        cites = ks.doc_citations(conn, og.ODCS_SOURCES,
                                 keywords=("quality", "sla", "schema", "server"), per_source=2)
        return {"query": query, "name": name, "table": table, "columns": cols,
                "location": location, "platform": platform, "topic": tm, "citations": cites,
                "purpose": kw.get("purpose") or query, "domain": kw.get("domain"),
                "owner": kw.get("owner")}


class ContractPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        name, table = d["name"], d["table"]
        cols: list[og.Column] = d["columns"]
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or name,
                       domain=d.get("domain") or "data contracts", source_query=d["query"])
        if not cols:
            cols = [og.Column("id", "string", required=True, primary_key=True,
                              description="TODO: replace with the table's real columns.")]
            plan.notes.append("no columns or DDL given; wrote one placeholder column")
        file = f"{name}.odcs.yaml"
        doc = og.odcs_contract(contract_id=name, name=name, table=table, columns=cols,
                               purpose=og.first_sentence(d["purpose"], 300),
                               platform=d["platform"], location=d["location"],
                               domain=d.get("domain"), owner=d.get("owner"))
        d["platform"] = doc["servers"][0]["type"]       # "generic" is written as "local"
        content = og.dump_yaml(doc, (
            f"ODCS {og.ODCS_VERSION} data contract for {table}.\n"
            "Generated by myPub /kb-data-contract. Replace every TODO; see README.md."))
        check = og.check_odcs(doc, file)
        restricted = [c.name for c in cols if c.personal]
        tm: Optional[ks.TopicMaterial] = d["topic"]
        sources = (tm.sources if tm else []) + list(d["citations"])
        plan.package_metadata = {
            "contract_file": file, "table": table, "platform": d["platform"],
            "n_columns": len(cols), "n_quality_rules": sum(
                len(p.get("quality", [])) for p in doc["schema"][0]["properties"])
            + len(doc["schema"][0]["quality"]),
            "restricted_columns": restricted, "schema_ok": check.ok,
            "schema_errors": check.messages if check.ok is False else [],
        }
        plan.units.append(GenUnit(
            unit_type="data_contract", name=name, logical_key=name, content_markdown=content,
            metadata={"table": table, "columns": [c.name for c in cols]},
            sources=[(c.source_type, c.source_id, 1.0, 1.0, None) for c in sources]
            + [("procedure", p.procedure_id, 1.0, 1.0, None) for p in (tm.procedures if tm else [])],
        ))
        plan.files += [
            GenFile(filename=file, content=content, purpose="odcs", unit_logical_key=name),
            GenFile(filename="README.md", purpose="overview", content=_readme(
                name, table, file, d["platform"], len(cols), restricted, d["purpose"])),
            GenFile(filename="checklist.md", purpose="guidance",
                    content=_checklist(tm, d["citations"])),
            GenFile(filename="_provenance.md", purpose="provenance", content=(
                f"# Provenance: {name}\n\nODCS {og.ODCS_VERSION} contract for `{table}`, "
                "built from the columns given.\n\n"
                + ks.render_citations(d["citations"], "ODCS sections")
                + "\n" + ks.render_citations(tm.sources if tm else [],
                                             "Knowledge-base sources on data contracts"))),
            GenFile(filename="_validation.md", purpose="validation", content=og.render_checks(
                f"ODCS check: {name}", [check],
                "Schema: data/odp/odcs-json-schema-v3.1.0.json (bitol-io, Apache 2.0).")),
            GenFile(filename="_prompts/fill-contract.md", purpose="subagent_prompt",
                    content=FILL_PROMPT.format(name=name, file=file, version=og.ODCS_VERSION)),
        ]
        if tm is None:
            plan.notes.append("the KB has no 'Data Contract' concept; checklist is generic")
        return plan


class ContractValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        out = [ValidationIssue(meta["contract_file"], "error", m)
               for m in meta.get("schema_errors", [])]
        if meta.get("schema_ok") is None:
            out.append(ValidationIssue(meta["contract_file"], "warning",
                                       "jsonschema not installed; ODCS schema not fully checked"))
        if meta.get("restricted_columns"):
            out.append(ValidationIssue(meta["contract_file"], "warning",
                                       "columns that may hold personal data: "
                                       + ", ".join(meta["restricted_columns"])))
        return out


def make_data_contract_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=ContractDecomposer(),
                     planner=ContractPlanner(), ranking_mode="generation",
                     validator=ContractValidator(), materializer=ks.TreeMaterializer())
