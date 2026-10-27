#!/usr/bin/env python3
"""
ingest_pmc.py — Bring PubMed Central open-access articles into the catalog.

Articles become ``book`` rows (source_path "pmc:PMC<id>") with one
``chapter`` per top-level section, so embeddings, concept / procedure
extraction, search and every generator treat them like book chapters.
See scripts/jats.py for the mapping and docs/healthcare-corpora-design.md
for why.

    fetch  (--query "<PubMed query>" [--max 50] | --pmcids PMC1,PMC2)
           --output-dir <dir>
           Downloads JATS XML from NCBI E-utilities, restricted to the
           open-access subset, one file per article. No catalog access.
    index  --xml-dir <dir> [--catalog data/catalog.ddb] [--replace]
           Writer. Parses each file and inserts book / chapter / author
           rows. Skips articles already present unless --replace, and
           abstract-only records unless --allow-abstract-only.

Then run the usual pipeline on the new rows:
    scripts/generate_embeddings.py            (only NULL embeddings)
    scripts/extract_batch.py prep --books <ids> …   (sub-agents)

NCBI etiquette: at most 3 requests/s without a key. Set NCBI_API_KEY
(10/s) and NCBI_EMAIL if you have them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import duckdb

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from jats import JatsArticle, parse_jats  # noqa: E402

DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
FETCH_BATCH = 20


def _count_tokens(text: str) -> int:
    try:
        import tiktoken  # noqa: PLC0415
        enc = _count_tokens.__dict__.setdefault("enc", tiktoken.get_encoding("cl100k_base"))
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 — offline / not installed: rough estimate
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _eutils(endpoint: str, params: dict) -> bytes:
    params = dict(params, tool="mypub")
    if os.environ.get("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    url = f"{EUTILS}/{endpoint}?" + urllib.parse.urlencode(params)
    time.sleep(0.12 if "api_key" in params else 0.35)
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def search_pmc(query: str, max_results: int) -> list[str]:
    term = f"({query}) AND open access[filter]"
    data = json.loads(_eutils("esearch.fcgi", {
        "db": "pmc", "term": term, "retmax": max_results, "retmode": "json",
        "sort": "relevance",
    }))
    return [f"PMC{i}" for i in data.get("esearchresult", {}).get("idlist", [])]


def split_articleset(xml: bytes) -> list[bytes]:
    """Split a <pmc-articleset> into one serialized <article> per item."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    root = ET.fromstring(xml)
    if root.tag.rsplit("}", 1)[-1] == "article":
        return [xml]
    return [ET.tostring(a, encoding="utf-8") for a in root
            if a.tag.rsplit("}", 1)[-1] == "article"]


def do_fetch(args: argparse.Namespace) -> int:
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    ids = ([i.strip() for i in args.pmcids.split(",") if i.strip()]
           if args.pmcids else search_pmc(args.query, args.max))
    todo = [i for i in ids if not (out / f"{i.upper()}.xml").exists()]
    print(f"{len(ids)} article(s); {len(ids) - len(todo)} already downloaded")
    saved = 0
    for start in range(0, len(todo), FETCH_BATCH):
        chunk = todo[start: start + FETCH_BATCH]
        xml = _eutils("efetch.fcgi", {
            "db": "pmc", "id": ",".join(c.upper().removeprefix("PMC") for c in chunk),
            "retmode": "xml",
        })
        for art_xml in split_articleset(xml):
            try:
                art = parse_jats(art_xml)
            except ValueError as e:
                print(f"  skip unparseable record: {e}", file=sys.stderr)
                continue
            name = (art.pmcid or hashlib.sha1(art_xml).hexdigest()[:12]).upper()
            (out / f"{name}.xml").write_bytes(art_xml)
            saved += 1
    print(f"saved {saved} article(s) to {out}")
    return 0


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def _upsert_author(conn: duckdb.DuckDBPyConnection, name: str) -> int:
    row = conn.execute("SELECT author_id FROM author WHERE name = ?", [name]).fetchone()
    if row:
        return int(row[0])
    return int(conn.execute("INSERT INTO author (name) VALUES (?) RETURNING author_id",
                            [name]).fetchone()[0])


def _delete_article(conn: duckdb.DuckDBPyConnection, book_id: int) -> None:
    # prune_stale_books handles the chapter-sourced rows and drops / rebuilds
    # the chapter_embedding HNSW index around the delete, which otherwise
    # crawls for tens of minutes.
    from prune_stale_books import delete_books  # noqa: PLC0415
    delete_books(conn, [book_id])


def index_article(conn: duckdb.DuckDBPyConnection, art: JatsArticle) -> int:
    """Insert one article; returns book_id. Caller handles transactions."""
    sections = [s for s in art.sections if s.content.strip()]
    total = sum(_count_tokens(s.content) for s in sections)
    book_id = int(conn.execute(
        """
        INSERT INTO book (title, publisher, publication_date, source_path, description,
                          subjects, total_tokens, chapter_count, content_hash, last_indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        RETURNING book_id
        """,
        [art.title, art.journal, art.publication_date, art.source_path,
         art.abstract or None, art.keywords or None, total, len(sections),
         hashlib.sha256("\n".join(s.content for s in sections).encode()).hexdigest()],
    ).fetchone()[0])
    for pos, name in enumerate(dict.fromkeys(art.authors)):
        conn.execute("INSERT INTO book_author (book_id, author_id, position) VALUES (?, ?, ?)",
                     [book_id, _upsert_author(conn, name), pos])
    for n, s in enumerate(sections, 1):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_num, title, href, content, content_hash,"
            " token_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [book_id, n, s.title, f"{art.source_path}#{n}", s.content,
             hashlib.sha256(s.content.encode()).hexdigest(), _count_tokens(s.content)],
        )
    return book_id


def do_index(args: argparse.Namespace) -> int:
    files = sorted(args.xml_dir.glob("*.xml"))
    if not files:
        print(f"no .xml files in {args.xml_dir}", file=sys.stderr)
        return 2
    conn = duckdb.connect(str(args.catalog))
    added = skipped = failed = 0
    new_ids: list[int] = []
    for f in files:
        try:
            art = parse_jats(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            print(f"  {f.name}: parse failed: {e}", file=sys.stderr)
            failed += 1
            continue
        if len(art.sections) <= 1 and not args.allow_abstract_only:
            print(f"  {f.name}: abstract only, skipped")
            skipped += 1
            continue
        existing = conn.execute("SELECT book_id FROM book WHERE source_path = ?",
                                [art.source_path]).fetchone()
        if existing and not args.replace:
            skipped += 1
            continue
        if existing:
            # Autocommit on purpose: DuckDB 1.5 rejects deleting a parent
            # row after its children inside one explicit transaction. If
            # the insert below fails, re-running restores the article.
            _delete_article(conn, int(existing[0]))
        conn.execute("BEGIN")
        try:
            new_ids.append(index_article(conn, art))
            conn.execute("COMMIT")
            added += 1
        except Exception as e:  # noqa: BLE001
            conn.execute("ROLLBACK")
            print(f"  {f.name}: index failed: {e}", file=sys.stderr)
            failed += 1
    conn.close()
    print(f"indexed {added}, skipped {skipped}, failed {failed}")
    if new_ids:
        print("new book_ids (for extract_batch.py prep --books): "
              + ",".join(map(str, new_ids)))
    return 0 if not failed else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--query")
    g.add_argument("--pmcids")
    f.add_argument("--max", type=int, default=50)
    f.add_argument("--output-dir", type=Path, required=True)
    i = sub.add_parser("index")
    i.add_argument("--xml-dir", type=Path, required=True)
    i.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    i.add_argument("--replace", action="store_true")
    i.add_argument("--allow-abstract-only", action="store_true")
    args = parser.parse_args(argv)
    return do_fetch(args) if args.cmd == "fetch" else do_index(args)


if __name__ == "__main__":
    sys.exit(main())
