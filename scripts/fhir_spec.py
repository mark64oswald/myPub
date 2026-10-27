#!/usr/bin/env python3
"""
fhir_spec.py — FHIR specification packages as a first-class doc source.

A FHIR NPM package (hl7.fhir.r4.core, hl7.fhir.us.core, any IG) is a
tarball of conformance resources. This turns its StructureDefinitions
into Markdown, one "## <Resource>" section each (description, scope,
base, maturity, key elements with cardinality / type / short), and feeds
that through the normal doc pipeline (sectionize → doc_section →
embeddings → FTS) by handing ``refresh_docs.refresh_one_source`` a local
fetcher. ``refresh_docs.py`` itself is not modified.

    render    --package-file <pkg.tgz> [--out spec.md] [--include-profiles]
              [--include-conceptmaps]
              No DB. Writes the Markdown so you can review it.
    register  --package-file <pkg.tgz> [--name "FHIR R4 core"]
              [--catalog data/catalog.ddb] [--include-profiles]
              Writer. Upserts doc_source (source_type 'fhir_package',
              identifier '<package>@<version>') and runs steps 1–6 of the
              doc pipeline. Re-running with the same package is a no-op
              (content hash unchanged). Then run refresh_docs.py prep /
              process as usual for concept + procedure extraction.

Get a package (on the Mac — the cloud container's proxy blocks it):
    curl -L -o r4-core.tgz https://packages.fhir.org/hl7.fhir.r4.core/4.0.1
    curl -L -o us-core.tgz https://packages.fhir.org/hl7.fhir.us.core/6.1.0
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MAX_ELEMENTS = 60
SOURCE_TYPE = "fhir_package"


@dataclass
class FetchResultLike:
    url: str
    content: str
    source_type: str


def _cell(s: Optional[str], limit: int = 120) -> str:
    s = " ".join((s or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _types(el: dict) -> str:
    out = []
    for t in el.get("type") or []:
        code = t.get("code", "")
        targets = [p.rsplit("/", 1)[-1] for p in t.get("targetProfile") or []]
        out.append(f"{code}({', '.join(targets)})" if targets else code)
    return ", ".join(out)


def structure_definition_markdown(sd: dict) -> str:
    name = sd.get("name") or sd.get("id") or "Unnamed"
    lines = [f"## {name}", ""]
    if sd.get("title") and sd["title"] != name:
        lines += [f"_{sd['title']}_", ""]
    if sd.get("description"):
        lines += [sd["description"].strip(), ""]
    if sd.get("purpose"):
        lines += [f"**Purpose:** {sd['purpose'].strip()}", ""]
    facts = [f"kind `{sd.get('kind')}`"]
    if sd.get("baseDefinition"):
        facts.append(f"base `{sd['baseDefinition'].rsplit('/', 1)[-1]}`")
    if sd.get("derivation"):
        facts.append(f"derivation `{sd['derivation']}`")
    for ext in sd.get("extension") or []:
        if ext.get("url", "").endswith("structuredefinition-fmm"):
            facts.append(f"maturity FMM{ext.get('valueInteger')}")
    if sd.get("url"):
        facts.append(f"canonical `{sd['url']}`")
    lines += ["**Definition:** " + ", ".join(facts), ""]

    # Profiles: the differential is what the profile changes. Core
    # resources: the snapshot's first two path levels are the shape.
    if sd.get("derivation") == "constraint" and sd.get("differential"):
        elements = sd["differential"].get("element") or []
        label = "Constrained elements"
    else:
        elements = (sd.get("snapshot") or sd.get("differential") or {}).get("element") or []
        elements = [e for e in elements if 1 <= e.get("path", "").count(".") <= 2]
        label = "Elements"
    rows = []
    for el in elements:
        path = el.get("path", "")
        if path.count(".") == 0:
            continue
        card = f"{el.get('min', '')}..{el.get('max', '')}" if "min" in el or "max" in el else ""
        flags = " MS" if el.get("mustSupport") else ""
        binding = el.get("binding") or {}
        short = el.get("short", "")
        if binding.get("valueSet"):
            short += f" (binding {binding.get('strength', '')}: {binding['valueSet'].rsplit('/', 1)[-1]})"
        rows.append(f"| `{path}` | {card}{flags} | {_cell(_types(el), 60)} | {_cell(short)} |")
    if rows:
        lines += [f"### {label}", "", "| Path | Card. | Type | Description |",
                  "|---|---|---|---|", *rows[:MAX_ELEMENTS]]
        if len(rows) > MAX_ELEMENTS:
            lines.append(f"| … | | | {len(rows) - MAX_ELEMENTS} more elements |")
        lines.append("")
    return "\n".join(lines)


MAX_MAP_ROWS = 80


def concept_map_markdown(cm: dict) -> str:
    """One ConceptMap as a section: what maps to what, as a table. The
    v2-to-FHIR IG ships its segment, datatype and vocabulary mappings
    this way (element code "PID-3" → target "Patient.identifier")."""
    name = cm.get("title") or cm.get("name") or cm.get("id") or "Unnamed"
    lines = [f"## {name}", ""]
    if cm.get("description"):
        lines += [cm["description"].strip(), ""]
    src = cm.get("sourceUri") or cm.get("sourceCanonical") or cm.get("sourceScope") or ""
    tgt = cm.get("targetUri") or cm.get("targetCanonical") or cm.get("targetScope") or ""
    if src or tgt:
        lines += [f"**Maps:** `{src.rsplit('/', 1)[-1]}` → `{tgt.rsplit('/', 1)[-1]}`", ""]
    rows = []
    for grp in cm.get("group") or []:
        for el in grp.get("element") or []:
            for t in el.get("target") or [{}]:
                rel = t.get("relationship") or t.get("equivalence") or ""
                cond = "; ".join(
                    f"{d.get('property') or d.get('attribute', '')}={d.get('value', '')}"
                    for d in t.get("dependsOn") or [])
                note = " ".join(x for x in (t.get("comment", ""), cond) if x)
                rows.append(f"| `{_cell(el.get('code'), 40)}` | {_cell(el.get('display'), 60)} "
                            f"| `{_cell(t.get('code'), 60)}` | {rel} | {_cell(note)} |")
    if rows:
        lines += ["| Source | Display | Target | Relationship | Notes |",
                  "|---|---|---|---|---|", *rows[:MAX_MAP_ROWS]]
        if len(rows) > MAX_MAP_ROWS:
            lines.append(f"| … | | | | {len(rows) - MAX_MAP_ROWS} more rows |")
        lines.append("")
    return "\n".join(lines)


def _iter_package(tgz: Path) -> Iterator[dict]:
    with tarfile.open(tgz, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".json"):
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            try:
                yield json.loads(f.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue


def package_manifest(tgz: Path) -> dict:
    for doc in _iter_package(tgz):
        if "name" in doc and "version" in doc and "resourceType" not in doc:
            return doc
    return {}


def package_markdown(tgz: Path, *, include_profiles: bool = False,
                     include_conceptmaps: bool = False) -> tuple[str, str]:
    """(identifier 'name@version', markdown) for one package."""
    pkg = package_manifest(tgz)
    ident = f"{pkg.get('name', tgz.stem)}@{pkg.get('version', '0')}"
    sds, cms = [], []
    for doc in _iter_package(tgz):
        if include_conceptmaps and doc.get("resourceType") == "ConceptMap":
            cms.append(doc)
            continue
        if doc.get("resourceType") != "StructureDefinition":
            continue
        kind, deriv = doc.get("kind"), doc.get("derivation")
        if kind == "resource" and deriv in ("specialization", None) and not doc.get("abstract"):
            sds.append(doc)
        elif include_profiles and deriv == "constraint" and kind in ("resource", "complex-type"):
            sds.append(doc)
    sds.sort(key=lambda d: (d.get("derivation") == "constraint", d.get("name") or ""))
    head = [f"# {pkg.get('title') or pkg.get('name', ident)} ({ident})", ""]
    if pkg.get("description"):
        head += [pkg["description"].strip(), ""]
    head += [f"{len(sds)} StructureDefinition(s)."
             + (f", {len(cms)} ConceptMap(s)." if cms else ""), ""]
    cms.sort(key=lambda d: d.get("title") or d.get("name") or "")
    return ident, ("\n".join(head)
                   + "\n".join(structure_definition_markdown(sd) for sd in sds)
                   + "\n".join(concept_map_markdown(cm) for cm in cms))


class FhirPackageFetcher:
    """Local fetcher with the refresh_docs fetcher interface."""

    def __init__(self, tgz: Path, include_profiles: bool = False,
                 include_conceptmaps: bool = False):
        self.tgz, self.include_profiles = tgz, include_profiles
        self.include_conceptmaps = include_conceptmaps

    def fetch(self, identifier: str) -> FetchResultLike:
        ident, md = package_markdown(self.tgz, include_profiles=self.include_profiles,
                                     include_conceptmaps=self.include_conceptmaps)
        name, _, version = ident.partition("@")
        return FetchResultLike(url=f"https://packages.fhir.org/{name}/{version}",
                               content=md, source_type="markdown")


def do_render(args: argparse.Namespace) -> int:
    ident, md = package_markdown(args.package_file, include_profiles=args.include_profiles,
                                 include_conceptmaps=args.include_conceptmaps)
    if args.out:
        args.out.write_text(md)
        print(f"{ident}: {md.count(chr(10) + '## ')} section(s) → {args.out}")
    else:
        print(md)
    return 0


def do_register(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import refresh_docs  # noqa: PLC0415 — heavy imports, writer path only

    ident, _ = package_markdown(args.package_file, include_profiles=args.include_profiles,
                                include_conceptmaps=args.include_conceptmaps)
    conn = refresh_docs.open_writer(args.catalog)
    try:
        row = conn.execute("SELECT doc_source_id FROM doc_source WHERE source_type = ? "
                           "AND identifier = ?", [SOURCE_TYPE, ident]).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO doc_source (name, source_type, mcp_server, identifier, "
                "authority_score, refresh_ttl_days, priority_tier, pinned) "
                "VALUES (?, ?, 'local', ?, 0.9, 365, 'cold', TRUE) RETURNING doc_source_id",
                [args.name or ident, SOURCE_TYPE, ident]).fetchone()
        result = refresh_docs.refresh_one_source(
            conn, int(row[0]),
            fetcher=FhirPackageFetcher(args.package_file, args.include_profiles,
                                       args.include_conceptmaps))
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
        p.add_argument("--package-file", type=Path, required=True)
        p.add_argument("--include-profiles", action="store_true")
        p.add_argument("--include-conceptmaps", action="store_true",
                       help="also render ConceptMaps (e.g. the v2-to-FHIR IG)")
        if name == "render":
            p.add_argument("--out", type=Path)
        else:
            p.add_argument("--name")
            p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)
    return do_render(args) if args.cmd == "render" else do_register(args)


if __name__ == "__main__":
    sys.exit(main())
