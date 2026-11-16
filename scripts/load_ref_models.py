#!/usr/bin/env python3
"""
load_ref_models.py — Load and map reference data models (2026-11-16).

Loads industry data models into the ref_* tables of the catalog
(mcp-servers/kb-mcp/ref_models.py), classifies their entities against the
pattern map (data/ref_models/patterns.yaml), and prepares / applies the
subagent pass that confirms those pattern links.

Sources:
  * Databricks Lakehouse Industry Data Models — a local clone of
    github.com/databricks-industry-solutions/lakehouse-industry-data-models
    (Databricks License; loaded into the local catalog only).
  * Silverston, The Data Model Resource Book Vol. 2 — its entity/attribute
    appendices are page images, so ``silverston-pages`` extracts them from
    the ePub with one transcription prompt per page; subagents write one CSV
    per page; ``load-silverston`` loads them. Files stay in the gitignored
    data/ref_models/silverston/.
  * OMOP CDM 5.4 — the OHDSI field/table CSVs (Apache 2.0), fetched.
  * Any DDL file (``load-ddl``).

Typical run on the Mac (one writer at a time; LLM steps are Claude Code
subagents in Remote Control, never the paid API):

    git clone --depth 1 https://github.com/databricks-industry-solutions/lakehouse-industry-data-models ~/Developer/lakehouse-industry-data-models
    .venv/bin/python3 scripts/load_ref_models.py load-databricks --repo ~/Developer/lakehouse-industry-data-models
    .venv/bin/python3 scripts/load_ref_models.py load-omop
    .venv/bin/python3 scripts/load_ref_models.py silverston-pages --industry healthcare --industry insurance
        # subagents: one page image + prompt each -> pNN.csv
    .venv/bin/python3 scripts/load_ref_models.py load-silverston --industry healthcare --industry insurance
    .venv/bin/python3 scripts/load_ref_models.py classify --all
    .venv/bin/python3 scripts/load_ref_models.py embed --all
    .venv/bin/python3 scripts/load_ref_models.py map-prep --industry healthcare --industry health_insurance --out data/ref_models/mapping-runs/20261116
        # subagents: batch_NNN.prompt.md -> batch_NNN.json
    .venv/bin/python3 scripts/load_ref_models.py map-apply --dir data/ref_models/mapping-runs/20261116
    .venv/bin/python3 scripts/load_ref_models.py status
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import ref_models as rm  # noqa: E402

DEFAULT_CATALOG = ROOT / "data" / "catalog.ddb"
SILVERSTON_DIR = ROOT / "data" / "ref_models" / "silverston"
DMRB2_GLOB = "The Data Model Resource Book, Vol. 2*.epub"
EBOOKS = Path.home() / "Documents" / "eBooks"
LOG = logging.getLogger("ref_models")

# Vol. 2 appendix per industry: letter, page-image prefix in the ePub.
SILVERSTON_APPENDICES = {
    "manufacturing": ("A", "ap01-"),
    "telecommunications": ("B", "ap02-"),
    "healthcare": ("C", "ap03-"),
    "insurance": ("D", "ap04-"),
    "financial_services": ("E", "ap05-"),
    "professional_services": ("F", "ap06-"),
    "travel": ("G", "ap07-"),
    "ecommerce": ("H", "ap08-"),
}

TRANSCRIBE_PROMPT = """Transcribe one page of an entity/attribute listing from a data
modeling book into CSV. Read the image at:

    {image}

The page is a table with columns ENTITY NAME, ATTRIBUTE NAME, PK?, FK?, DOMAIN,
usually printed as TWO side-by-side column blocks: transcribe the LEFT block top
to bottom, then the RIGHT block top to bottom.

Rules:
- Write the CSV with the Write tool to: {out}
- Header line exactly: entity,attribute,pk,fk,domain
- One row per attribute. When the ENTITY NAME cell is blank (a continuation),
  leave entity empty. When an entity or attribute name wraps onto two printed
  lines, join them with one space ("CLAIM ITEM DIAGNOSIS CODE").
- Keep names in the book's upper case. pk and fk are Yes or No as printed.
  domain as printed (ID, Datetime, Description, Long varchar, Currency amount...).
- Quote any field that contains a comma.
- Transcribe every row on the page; don't summarize, skip or invent rows.
Reply with only the number of rows written.
"""


def _conn(path: Path, read_only: bool = False):
    import duckdb  # noqa: PLC0415
    return duckdb.connect(str(path), read_only=read_only)


def _resolver(conn):
    try:
        from resolution import EntityResolver  # noqa: PLC0415
        return EntityResolver(conn, model=None)
    except Exception as exc:  # pragma: no cover - catalog without concepts
        LOG.warning("no resolver (%s); pattern concept ids stay empty", exc)
        return None


def _model_ids(conn, args) -> list[int]:
    if getattr(args, "model_id", None):
        return [int(x) for x in args.model_id]
    # --industry and --source add models (union): "--industry healthcare
    # --source omop" means the healthcare models plus OMOP.
    sql = "SELECT ref_model_id FROM ref_model"
    params: list[Any] = []
    clauses = []
    if getattr(args, "industry", None):
        clauses.append(f"industry IN ({','.join('?' * len(args.industry))})")
        params += args.industry
    if getattr(args, "source", None):
        clauses.append(f"source IN ({','.join('?' * len(args.source))})")
        params += args.source
    if clauses:
        sql += " WHERE " + " OR ".join(clauses)
    if not (getattr(args, "all", False) or params):
        raise SystemExit("name models with --model-id, --industry/--source, or --all")
    return [int(r[0]) for r in conn.execute(sql + " ORDER BY ref_model_id", params).fetchall()]


# ---------------------------------------------------------------------------
# Databricks
# ---------------------------------------------------------------------------

def _version_key(v: str) -> tuple[int, str]:
    m = re.match(r"v(\d+)", v)
    return (int(m.group(1)) if m else 0, v)


def databricks_model_files(repo: Path, industries: Optional[list[str]], flavours: list[str],
                           all_versions: bool = False) -> list[Path]:
    """model.json files under data-models/<industry>/<vN>/<flavour>/, latest version only
    unless ``all_versions``."""
    base = Path(repo).expanduser() / "data-models"
    out: list[Path] = []
    for ind in sorted(p for p in base.iterdir() if p.is_dir()):
        if industries and ind.name not in industries:
            continue
        versions = sorted((v for v in ind.iterdir() if v.is_dir() and re.match(r"v\d+", v.name)),
                          key=lambda v: _version_key(v.name))
        for fl in flavours:
            cands = [v / fl / "model.json" for v in versions if (v / fl / "model.json").is_file()]
            out += cands if all_versions else cands[-1:]
    return out


def cmd_load_databricks(args) -> int:
    files = databricks_model_files(Path(args.repo), args.industry,
                                   ["mvm", "ecm"] if args.flavour == "both" else [args.flavour])
    if not files:
        LOG.error("no model.json under %s/data-models", args.repo)
        return 1
    if args.dry_run:
        for f in files:
            LOG.info("would load %s", f)
        return 0
    conn = _conn(args.catalog)
    try:
        for f in files:
            model = rm.parse_databricks_model(f)
            mid = rm.load_model(conn, model)
            LOG.info("#%d %s/%s %s: %d entities, %d attributes, %d metrics", mid, model.industry,
                     model.flavour, model.version, len(model.entities),
                     sum(len(e.attributes) for e in model.entities), len(model.metrics))
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# OMOP
# ---------------------------------------------------------------------------

def _read_or_fetch(path: Optional[str], url: str) -> str:
    if path:
        return Path(path).expanduser().read_text()
    import httpx  # noqa: PLC0415
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.text


def cmd_load_omop(args) -> int:
    model = rm.parse_omop(_read_or_fetch(args.field_csv, rm.OMOP_FIELD_CSV),
                          _read_or_fetch(args.table_csv, rm.OMOP_TABLE_CSV))
    if args.dry_run:
        LOG.info("OMOP: %d tables, %d fields", len(model.entities),
                 sum(len(e.attributes) for e in model.entities))
        return 0
    conn = _conn(args.catalog)
    try:
        mid = rm.load_model(conn, model)
        LOG.info("#%d OMOP CDM %s: %d tables, %d relationships", mid, model.version,
                 len(model.entities), len(model.relationships()))
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Silverston
# ---------------------------------------------------------------------------

def _find_dmrb2(path: Optional[str]) -> Path:
    if path:
        return Path(path).expanduser()
    hits = sorted(EBOOKS.glob(DMRB2_GLOB))
    if not hits:
        raise SystemExit(f"Data Model Resource Book Vol. 2 not found in {EBOOKS}; pass --epub")
    return hits[0]


def silverston_pages(epub: Path, industry: str, out_root: Path = SILVERSTON_DIR) -> list[dict]:
    """Extract one appendix's page images and write a transcription prompt per page."""
    if industry not in SILVERSTON_APPENDICES:
        raise SystemExit(f"unknown industry {industry!r}; one of {sorted(SILVERSTON_APPENDICES)}")
    letter, prefix = SILVERSTON_APPENDICES[industry]
    out = out_root / industry
    (out / "pages").mkdir(parents=True, exist_ok=True)
    pages = []
    with zipfile.ZipFile(epub) as z:
        names = sorted(n for n in z.namelist()
                       if n.rsplit("/", 1)[-1].startswith(prefix)
                       and n.lower().endswith((".jpg", ".jpeg", ".png")))
        for i, n in enumerate(names, start=1):
            img = out / "pages" / n.rsplit("/", 1)[-1]
            img.write_bytes(z.read(n))
            csv_path = out / f"p{i:02d}.csv"
            prompt = out / f"p{i:02d}.prompt.md"
            prompt.write_text(TRANSCRIBE_PROMPT.format(image=img.resolve(), out=csv_path.resolve()))
            pages.append({"page": i, "image": str(img), "prompt": str(prompt), "csv": str(csv_path)})
    (out / "manifest.json").write_text(json.dumps(
        {"industry": industry, "appendix": letter, "epub": str(epub), "pages": pages}, indent=2))
    return pages


def cmd_silverston_pages(args) -> int:
    epub = _find_dmrb2(args.epub)
    for ind in args.industry:
        pages = silverston_pages(epub, ind)
        LOG.info("%s: %d page images + prompts in %s", ind, len(pages), SILVERSTON_DIR / ind)
    return 0


def silverston_csv(industry: str, root: Path = SILVERSTON_DIR) -> tuple[str, list[str]]:
    """Concatenate the per-page CSVs in page order. Returns (csv text, missing pages)."""
    d = root / industry
    manifest = json.loads((d / "manifest.json").read_text())
    rows, missing = ["entity,attribute,pk,fk,domain"], []
    for p in manifest["pages"]:
        path = Path(p["csv"])
        if not path.is_file():
            path = d / Path(p["csv"]).name
        if not path.is_file():
            missing.append(path.name)
            continue
        lines = path.read_text().strip().splitlines()
        if lines and lines[0].lower().replace(" ", "").startswith("entity,"):
            lines = lines[1:]
        rows += lines
    return "\n".join(rows) + "\n", missing


def cmd_load_silverston(args) -> int:
    conn = None if args.dry_run else _conn(args.catalog)
    try:
        for ind in args.industry:
            text, missing = silverston_csv(ind)
            if missing and not args.allow_missing:
                LOG.error("%s: %d pages not transcribed yet: %s", ind, len(missing),
                          ", ".join(missing))
                return 1
            letter = SILVERSTON_APPENDICES[ind][0]
            model = rm.parse_silverston_csv(
                text, industry=ind, book_title="The Data Model Resource Book, Vol. 2",
                appendix=f"Appendix {letter}")
            LOG.info("%s: %d entities, %d attributes, %d relationships%s", ind,
                     len(model.entities), sum(len(e.attributes) for e in model.entities),
                     len(model.relationships()), " (dry run)" if args.dry_run else "")
            if conn is not None:
                LOG.info("  -> ref_model #%d", rm.load_model(conn, model))
    finally:
        if conn is not None:
            conn.close()
    return 0


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def cmd_load_ddl(args) -> int:
    ddl = "\n".join(Path(f).expanduser().read_text() for f in args.file)
    model = rm.parse_ddl_model(ddl, industry=args.industry, name=args.name or args.industry,
                               source=args.source, flavour=args.flavour)
    LOG.info("%d tables, %d columns, %d relationships", len(model.entities),
             sum(len(e.attributes) for e in model.entities), len(model.relationships()))
    if not args.dry_run:
        conn = _conn(args.catalog)
        try:
            LOG.info("-> ref_model #%d", rm.load_model(conn, model))
        finally:
            conn.close()
    return 0


# ---------------------------------------------------------------------------
# Classify / embed / map
# ---------------------------------------------------------------------------

def cmd_classify(args) -> int:
    conn = _conn(args.catalog)
    try:
        cat = rm.load_patterns()
        resolver = _resolver(conn)
        for mid in _model_ids(conn, args):
            LOG.info("#%d: %d heuristic links", mid, rm.classify_model(conn, mid, cat,
                                                                      resolver=resolver))
    finally:
        conn.close()
    return 0


def cmd_embed(args) -> int:
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    conn = _conn(args.catalog)
    try:
        ids = _model_ids(conn, args)
        LOG.info("embedded %d entity cards", rm.embed_entities(conn, ids, model))
    finally:
        conn.close()
    return 0


def cmd_map_prep(args) -> int:
    conn = _conn(args.catalog, read_only=True)
    try:
        batches = rm.map_batches(conn, _model_ids(conn, args), rm.load_patterns(),
                                 batch_size=args.batch_size)
    finally:
        conn.close()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for b in batches:
        stem = f"batch_{b['batch']:03d}"
        prompt = out / f"{stem}.prompt.md"
        result = out / f"{stem}.json"
        prompt.write_text(b["prompt"] + f"\nOutput file: {result.resolve()}\n")
        manifest.append({"batch": b["batch"], "prompt": str(prompt), "result": str(result),
                         "entity_ids": b["entity_ids"]})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    LOG.info("%d batches (%d entities) in %s", len(batches),
             sum(len(b["entity_ids"]) for b in batches), out)
    return 0


def cmd_map_apply(args) -> int:
    d = Path(args.dir)
    manifest = json.loads((d / "manifest.json").read_text())
    conn = _conn(args.catalog)
    total, missing = 0, []
    try:
        cat = rm.load_patterns()
        resolver = _resolver(conn)
        for b in manifest:
            path = Path(b["result"])
            if not path.is_file():
                path = d / Path(b["result"]).name
            if not path.is_file():
                missing.append(path.name)
                continue
            try:
                rows = json.loads(path.read_text())
            except ValueError as exc:
                LOG.error("%s: not JSON (%s)", path.name, exc)
                continue
            n, problems = rm.apply_mapping(conn, rows, cat, allowed_ids=set(b["entity_ids"]),
                                           resolver=resolver)
            total += n
            for p in problems[:5]:
                LOG.warning("%s: %s", path.name, p)
    finally:
        conn.close()
    LOG.info("applied %d links from %d batches; %d batches missing%s", total,
             len(manifest) - len(missing), len(missing),
             (": " + ", ".join(missing[:10])) if missing else "")
    return 0


def cmd_status(args) -> int:
    conn = _conn(args.catalog, read_only=True)
    try:
        rows = rm.list_models(conn)
        if not rows:
            LOG.info("no reference models loaded")
            return 0
        for r in rows:
            links = conn.execute(
                "SELECT method, count(*) FROM ref_entity_concept c JOIN ref_entity e "
                "USING (ref_entity_id) WHERE e.ref_model_id = ? GROUP BY 1 ORDER BY 1",
                [r["ref_model_id"]]).fetchall()
            LOG.info("#%-4d %-10s %-22s %-5s %-8s %5d entities %7d attrs  links: %s",
                     r["ref_model_id"], r["source"], r["industry"], r["flavour"], r["version"],
                     r["entities"], r["attributes"],
                     ", ".join(f"{m} {n}" for m, n in links) or "none")
    finally:
        conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load-databricks", help="load model.json files from a local clone")
    s.add_argument("--repo", required=True)
    s.add_argument("--industry", action="append")
    s.add_argument("--flavour", choices=["mvm", "ecm", "both"], default="mvm")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_load_databricks)

    s = sub.add_parser("load-omop", help="load the OMOP CDM 5.4 field/table CSVs")
    s.add_argument("--field-csv")
    s.add_argument("--table-csv")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_load_omop)

    s = sub.add_parser("silverston-pages", help="extract Vol. 2 appendix pages + prompts")
    s.add_argument("--industry", action="append", required=True)
    s.add_argument("--epub")
    s.set_defaults(fn=cmd_silverston_pages)

    s = sub.add_parser("load-silverston", help="load transcribed Vol. 2 appendix CSVs")
    s.add_argument("--industry", action="append", required=True)
    s.add_argument("--allow-missing", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_load_silverston)

    s = sub.add_parser("load-ddl", help="load CREATE TABLE / FK DDL as a model")
    s.add_argument("--file", action="append", required=True)
    s.add_argument("--industry", required=True)
    s.add_argument("--name")
    s.add_argument("--source", default="user", choices=list(rm.SOURCES))
    s.add_argument("--flavour", default="ddl")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_load_ddl)

    for name, fn, helptext in (("classify", cmd_classify, "heuristic pattern links"),
                               ("embed", cmd_embed, "embed entity cards"),
                               ("map-prep", cmd_map_prep, "write subagent mapping batches")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--model-id", action="append")
        s.add_argument("--industry", action="append")
        s.add_argument("--source", action="append")
        s.add_argument("--all", action="store_true")
        if name == "map-prep":
            s.add_argument("--out", required=True)
            s.add_argument("--batch-size", type=int, default=25)
        s.set_defaults(fn=fn)

    s = sub.add_parser("map-apply", help="apply subagent mapping results")
    s.add_argument("--dir", required=True)
    s.set_defaults(fn=cmd_map_apply)

    s = sub.add_parser("status", help="list loaded models and link counts")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
