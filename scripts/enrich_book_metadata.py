#!/usr/bin/env python3
"""
enrich_book_metadata.py — Fill missing book subjects / dates / publishers
from OpenLibrary, then Google Books.

As of 2026-10-26, 556 of 621 books have no subjects, 65 no usable
publication date and 50 no publisher: the ePubs' own DC metadata is thin.

Two steps, so the slow network part never holds the DB writer slot:

    lookup  --catalog <clone>.ddb --output-dir <out> [--limit N]
            Read-only. For each book missing something, find an ISBN
            (ePub DC:identifier, then the filename's ISBN suffix), look
            the book up and write <out>/enrichment.json. HTTP responses
            are cached in <out>/cache/, so a re-run resumes.

    apply   --output-dir <out> [--catalog data/catalog.ddb] [--write]
            Fill ONLY empty fields. Dry run unless --write (needs the
            single writer slot).

Subjects go to a side table, ``book_subject(book_id, subject, source)``,
not ``book.subjects``: DuckDB runs an UPDATE of a LIST column as
delete + insert, which trips the chapter → book foreign key. Scalar
publisher / publication_date are updated in place.

Matching is conservative: a title-search hit (no ISBN) must share most
title tokens with the catalog title and, when the catalog knows the
authors, at least one author surname.

Set GOOGLE_BOOKS_API_KEY to avoid Google's shared anonymous quota.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
USER_AGENT = "myPub-metadata-enrichment/1.0 (personal library catalog)"
MAX_SUBJECTS = 8
MIN_TITLE_OVERLAP = 0.6

# OpenLibrary subject strings that describe the copy, not the content.
_SUBJECT_NOISE = re.compile(
    r"accessible book|protected daisy|in library|lending library|overdrive|"
    r"internet archive|large type|electronic books?|ebooks?|^fiction$|"
    r"long now|^open library|^nyt:|^award:|^place:|^person:|^time:",
    re.I,
)
# Library call numbers (LC "QA76.9.D26 S53 2001", Dewey "005.13") — shelf
# locations, not topics.
_CALL_NUMBER = re.compile(
    r"^[a-z]{1,3}\s?\d{2,4}(\.\d+)?(\s?\.?[a-z]\d+)*(\s+[a-z]?\d+[a-z]*)*(\s+\d{4})?$"
    r"|^\d{3}(\.\d+)?(\s+\S+)?$",
    re.I,
)
# Whole-word markers of French / German / Spanish / Portuguese / Italian /
# Dutch headings ("Bases de données", "Programmierung und …").
_FOREIGN_WORDS = re.compile(
    r"\b(de|des|du|la|le|les|et|und|für|der|das|y|del|los|las|para|da|"
    r"di|della|het|een|voor|en)\b",
    re.I,
)


def _is_foreign(part: str) -> bool:
    # OpenLibrary's English headings are ASCII; an accented letter almost
    # always means a translated heading ("Informática").
    if any(ch.isalpha() and not ch.isascii() for ch in part):
        return True
    return bool(_FOREIGN_WORDS.search(part))


_ISBN_RE = re.compile(r"(97[89]\d{10}|\d{9}[\dXx])")
_STOPWORDS = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "with", "edition"}

Fetch = Callable[[str], Optional[dict]]


@dataclass
class BookGap:
    book_id: int
    title: str
    source_path: str
    authors: list[str]
    needs_subjects: bool
    needs_date: bool
    needs_publisher: bool


@dataclass
class Enrichment:
    book_id: int
    title: str
    matched_by: Optional[str] = None       # 'isbn' | 'title'
    source: Optional[str] = None           # 'openlibrary' | 'googlebooks'
    matched_title: Optional[str] = None
    isbn: Optional[str] = None
    subjects: list[str] = field(default_factory=list)
    publication_date: Optional[str] = None
    publisher: Optional[str] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def isbn_valid(isbn: str) -> bool:
    s = isbn.upper()
    if len(s) == 13 and s.isdigit():
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(s[:12]))
        return (10 - total % 10) % 10 == int(s[12])
    if len(s) == 10 and s[:9].isdigit() and (s[9].isdigit() or s[9] == "X"):
        total = sum((10 - i) * int(d) for i, d in enumerate(s[:9]))
        check = 10 if s[9] == "X" else int(s[9])
        return (total + check) % 11 == 0
    return False


def find_isbns(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in _ISBN_RE.finditer(re.sub(r"[-\s]", "", t or "")):
            isbn = m.group(1).upper()
            if isbn_valid(isbn) and isbn not in out:
                out.append(isbn)
    return out


def _tokens(s: str) -> set[str]:
    s = re.sub(r"\(.*?\)|:.*$", "", (s or "").lower())   # drop subtitles / parentheticals
    return {t for t in re.findall(r"[a-z0-9#+]+", s) if t not in _STOPWORDS}


def title_overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def authors_agree(ours: list[str], theirs: list[str]) -> bool:
    if not ours or not theirs:
        return True
    last = lambda n: (n.strip().split() or [""])[-1].lower()  # noqa: E731
    return bool({last(n) for n in ours} & {last(n) for n in theirs})


def clean_subjects(raw: list[str]) -> list[str]:
    """Drop copy-level noise, split Google's 'A / B / C' paths, dedupe."""
    out: list[str] = []
    seen: set[str] = set()
    for s in raw:
        for part in [p.strip() for p in re.split(r"\s+/\s+", s or "")]:
            if (not part or len(part) > 80 or _SUBJECT_NOISE.search(part)
                    or _CALL_NUMBER.match(part) or _is_foreign(part)):
                continue
            key = part.lower()
            if key in seen or key == "general":
                continue
            seen.add(key)
            out.append(part)
    return out[:MAX_SUBJECTS]


def parse_date(raw: Optional[str]) -> Optional[str]:
    """ISO date string from '2017', 'March 2017', '2017-03', '2017-03-16'."""
    if not raw:
        return None
    m = re.search(r"(1[89]\d{2}|20\d{2})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", str(raw))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return date(y, 1, 1).isoformat()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def from_openlibrary_isbn(isbn: str, fetch: Fetch) -> Optional[dict]:
    ed = fetch(f"https://openlibrary.org/isbn/{isbn}.json")
    if not ed or "title" not in ed:
        return None
    subjects = list(ed.get("subjects") or [])
    for w in (ed.get("works") or [])[:1]:
        work = fetch(f"https://openlibrary.org{w.get('key', '')}.json") or {}
        subjects += work.get("subjects") or []
    return {"title": ed.get("title"), "subjects": subjects,
            "date": ed.get("publish_date"),
            "publisher": (ed.get("publishers") or [None])[0], "authors": []}


def from_openlibrary_search(title: str, author: Optional[str], fetch: Fetch) -> list[dict]:
    q = {"title": title, "limit": "5",
         "fields": "title,author_name,publisher,first_publish_year,publish_date,subject"}
    if author:
        q["author"] = author
    res = fetch("https://openlibrary.org/search.json?" + urllib.parse.urlencode(q)) or {}
    return [{"title": d.get("title"), "subjects": d.get("subject") or [],
             "date": str(d.get("first_publish_year") or ""),
             "publisher": (d.get("publisher") or [None])[0],
             "authors": d.get("author_name") or []}
            for d in res.get("docs") or []]


def from_googlebooks(query: str, fetch: Fetch) -> list[dict]:
    q = {"q": query, "maxResults": "5", "printType": "books"}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if key:
        q["key"] = key
    res = fetch("https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(q)) or {}
    out = []
    for item in res.get("items") or []:
        v = item.get("volumeInfo") or {}
        out.append({"title": v.get("title"), "subjects": v.get("categories") or [],
                    "date": v.get("publishedDate"), "publisher": v.get("publisher"),
                    "authors": v.get("authors") or []})
    return out


def _accept(cand: dict, gap: BookGap, by_isbn: bool) -> bool:
    if by_isbn:
        return True
    return (title_overlap(gap.title, cand.get("title") or "") >= MIN_TITLE_OVERLAP
            and authors_agree(gap.authors, cand.get("authors") or []))


def enrich_one(gap: BookGap, isbns: list[str], fetch: Fetch, *,
               use_google: bool = True) -> Enrichment:
    """Try ISBN lookups, then title searches; merge until the gaps close."""
    e = Enrichment(book_id=gap.book_id, title=gap.title)
    attempts: list[tuple[str, str, Callable[[], list[dict]]]] = []
    for isbn in isbns[:2]:
        attempts.append(("openlibrary", "isbn",
                         lambda i=isbn: [r for r in [from_openlibrary_isbn(i, fetch)] if r]))
        attempts.append(("googlebooks", "isbn", lambda i=isbn: from_googlebooks(f"isbn:{i}", fetch)))
    author = gap.authors[0] if gap.authors else None
    attempts.append(("openlibrary", "title", lambda: from_openlibrary_search(gap.title, author, fetch)))
    gq = f'intitle:"{gap.title}"' + (f' inauthor:"{author}"' if author else "")
    attempts.append(("googlebooks", "title", lambda: from_googlebooks(gq, fetch)))
    if not use_google:
        attempts = [a for a in attempts if a[0] != "googlebooks"]

    for source, by, run in attempts:
        cands = [c for c in run() if _accept(c, gap, by == "isbn")]
        if not cands:
            continue
        c = cands[0]
        took = False
        if gap.needs_subjects and not e.subjects:
            e.subjects = clean_subjects(c.get("subjects") or [])
            took |= bool(e.subjects)
        if gap.needs_date and not e.publication_date:
            e.publication_date = parse_date(c.get("date"))
            took |= bool(e.publication_date)
        if gap.needs_publisher and not e.publisher and c.get("publisher"):
            e.publisher = c["publisher"].strip()
            took = True
        if took and e.source is None:
            e.source, e.matched_by, e.matched_title = source, by, c.get("title")
            e.isbn = isbns[0] if by == "isbn" and isbns else None
        if ((not gap.needs_subjects or e.subjects)
                and (not gap.needs_date or e.publication_date)
                and (not gap.needs_publisher or e.publisher)):
            break
    if e.source is None:
        e.note = "no confident match"
    return e


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def make_fetch(cache_dir: Path, delay: float = 1.0) -> Fetch:
    cache_dir.mkdir(parents=True, exist_ok=True)
    last = [0.0]

    def fetch(url: str) -> Optional[dict]:
        f = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if f.exists():
            body = f.read_text()
            return json.loads(body) if body else None
        wait = delay - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as err:
            if err.code == 404:
                f.write_text("")       # cache the miss
                return None
            print(f"  HTTP {err.code} for {url}", file=sys.stderr)
            return None                # don't cache throttling / server errors
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            print(f"  {err} for {url}", file=sys.stderr)
            return None
        f.write_text(json.dumps(data))
        return data

    return fetch


def epub_identifiers(path: str) -> list[str]:
    try:
        from ebooklib import epub  # noqa: PLC0415
        book = epub.read_epub(path, options={"ignore_ncx": True})
    except Exception:  # noqa: BLE001 — missing file, bad zip, no ebooklib
        return []
    return [i[0] for i in book.get_metadata("DC", "identifier") if i and i[0]]


def select_gaps(conn: duckdb.DuckDBPyConnection, limit: Optional[int]) -> list[BookGap]:
    has_side = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'book_subject'"
    ).fetchone() is not None
    side = ("AND NOT EXISTS (SELECT 1 FROM book_subject bs WHERE bs.book_id = b.book_id)"
            if has_side else "")
    sql = f"""
        WITH gaps AS (
          SELECT b.book_id, b.title, b.source_path,
                 (COALESCE(len(b.subjects), 0) = 0 {side}) AS needs_subjects,
                 b.publication_date IS NULL AS needs_date,
                 NULLIF(trim(COALESCE(b.publisher, '')), '') IS NULL AS needs_publisher
            FROM book b
           WHERE COALESCE(b.status, 'active') = 'active'
        )
        SELECT g.book_id, g.title, g.source_path,
               list(a.name ORDER BY ba.position) FILTER (WHERE a.name IS NOT NULL),
               g.needs_subjects, g.needs_date, g.needs_publisher
          FROM gaps g
          LEFT JOIN book_author ba ON ba.book_id = g.book_id
          LEFT JOIN author a       ON a.author_id = ba.author_id
         WHERE g.needs_subjects OR g.needs_date OR g.needs_publisher
         GROUP BY ALL
         ORDER BY g.book_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [BookGap(int(r[0]), r[1], r[2], list(r[3] or []), bool(r[4]), bool(r[5]), bool(r[6]))
            for r in conn.execute(sql).fetchall()]


def do_lookup(args: argparse.Namespace) -> int:
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(args.catalog), read_only=True)
    gaps = select_gaps(conn, args.limit)
    conn.close()
    fetch = make_fetch(out / "cache", delay=args.delay)
    results = []
    for i, gap in enumerate(gaps, 1):
        isbns = find_isbns(*epub_identifiers(gap.source_path), Path(gap.source_path).stem)
        e = enrich_one(gap, isbns, fetch, use_google=not args.no_google)
        results.append(asdict(e))
        print(f"[{i}/{len(gaps)}] {gap.title[:60]!r}: "
              f"{e.source or '-'} ({e.matched_by or e.note}) "
              f"subj={len(e.subjects)} date={e.publication_date or '-'} pub={e.publisher or '-'}")
    (out / "enrichment.json").write_text(json.dumps(results, indent=2))
    hit = sum(1 for r in results if r["source"])
    print(f"\n{hit}/{len(results)} book(s) matched; review {out / 'enrichment.json'} before apply")
    return 0


def apply_enrichment(conn: duckdb.DuckDBPyConnection, rows: list[dict]) -> dict:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS book_subject (
            book_id    BIGINT  NOT NULL,  -- logical FK to book: a real one makes
                                          -- later UPDATEs of book fail in DuckDB 1.5
            subject    VARCHAR NOT NULL,
            source     VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (book_id, subject)
        )""")
    stats = {"subjects": 0, "dates": 0, "publishers": 0}
    conn.execute("BEGIN")
    try:
        for r in rows:
            if not r.get("source"):
                continue
            bid = r["book_id"]
            # One UPDATE per book: a second UPDATE of the same row in a
            # transaction trips DuckDB 1.5's FK checker (chapter → book).
            before = conn.execute(
                "SELECT publication_date IS NULL, "
                "       NULLIF(trim(COALESCE(publisher, '')), '') IS NULL "
                "  FROM book WHERE book_id = ?", [bid]).fetchone()
            if before is None:
                continue
            set_date = bool(r.get("publication_date")) and before[0]
            set_pub = bool(r.get("publisher")) and before[1]
            if set_date or set_pub:
                conn.execute(
                    "UPDATE book SET publication_date = CASE WHEN ? THEN ?::DATE ELSE publication_date END,"
                    "                publisher = CASE WHEN ? THEN ? ELSE publisher END,"
                    "                updated_at = now()"
                    " WHERE book_id = ?",
                    [set_date, r.get("publication_date"), set_pub, r.get("publisher"), bid])
                stats["dates"] += int(set_date)
                stats["publishers"] += int(set_pub)
            for s in r.get("subjects") or []:
                n = conn.execute(
                    "INSERT INTO book_subject (book_id, subject, source) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING RETURNING 1", [bid, s, r["source"]]).fetchall()
                stats["subjects"] += len(n)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats


def clean_existing(conn: duckdb.DuckDBPyConnection, *, write: bool) -> list[tuple[int, str]]:
    """book_subject rows that clean_subjects would now reject; deleted when write."""
    rows = conn.execute("SELECT book_id, subject FROM book_subject ORDER BY book_id").fetchall()
    bad = [(int(b), subj) for b, subj in rows if not clean_subjects([subj])]
    if write and bad:
        conn.executemany("DELETE FROM book_subject WHERE book_id = ? AND subject = ?", bad)
    return bad


def do_clean(args: argparse.Namespace) -> int:
    conn = duckdb.connect(str(args.catalog), read_only=not args.write)
    try:
        bad = clean_existing(conn, write=args.write)
    finally:
        conn.close()
    for b, subj in bad[:20]:
        print(f"  {b}: {subj}")
    print(f"{len(bad)} noisy subject row(s)"
          + (" deleted" if args.write else " — dry run, re-run with --write to delete"))
    return 0


def do_apply(args: argparse.Namespace) -> int:
    rows = json.loads((args.output_dir / "enrichment.json").read_text())
    matched = [r for r in rows if r.get("source")]
    print(f"{len(matched)} matched book(s): "
          f"{sum(len(r['subjects']) for r in matched)} subjects, "
          f"{sum(1 for r in matched if r['publication_date'])} dates, "
          f"{sum(1 for r in matched if r['publisher'])} publishers")
    if not args.write:
        print("dry run — re-run with --write to update the catalog")
        return 0
    conn = duckdb.connect(str(args.catalog))
    stats = apply_enrichment(conn, rows)
    conn.close()
    print("written:", stats)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lookup")
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--delay", type=float, default=1.0, help="seconds between HTTP calls")
    p.add_argument("--no-google", action="store_true",
                   help="skip Google Books (e.g. when its anonymous quota is spent)")
    a = sub.add_parser("apply")
    a.add_argument("--output-dir", type=Path, required=True)
    a.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    a.add_argument("--write", action="store_true")
    c = sub.add_parser("clean", help="remove book_subject rows the current filters reject")
    c.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    c.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    return {"lookup": do_lookup, "apply": do_apply, "clean": do_clean}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
