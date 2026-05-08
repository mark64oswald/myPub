#!/usr/bin/env python3
"""
index_books.py — Index ePub books into the v2 catalog.

Extracts each book's metadata, TOC, and full chapter text, and writes to
the v2 schema defined in schemas/catalog.sql:

    author ← book_author → book → chapter

Chapter text is stored in `chapter.content` so downstream steps (embedding
generation in prompt 1.3, FTS in 1.4) can run directly against the catalog
without re-reading the epubs.

Usage:
    .venv/bin/python3 scripts/index_books.py                       # all books
    .venv/bin/python3 scripts/index_books.py --limit 10            # first 10
    .venv/bin/python3 scripts/index_books.py --book "name.epub"    # one book
    .venv/bin/python3 scripts/index_books.py --source /path/to/dir
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import duckdb
import ebooklib
import tiktoken
from bs4 import BeautifulSoup, Comment, NavigableString
from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path(os.environ.get(
    "MYPUB_EBOOK_DIR",
    Path.home() / "Documents" / "eBooks",
))
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("index_books")

_ENCODER = tiktoken.get_encoding("cl100k_base")


# ----------------------------------------------------------------------------
# Content extraction
# ----------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """Return the token count using the cl100k_base encoding."""
    return len(_ENCODER.encode(text, disallowed_special=()))


def _sha256_file(path: Path) -> str:
    """Stream-hash a file and return its hex SHA-256 digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    """Return the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_text(html_bytes: bytes) -> str:
    """Render an ePub HTML blob down to whitespace-normalized plain text."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _href_path(href: Optional[str]) -> Optional[str]:
    """Strip any fragment (#section) from an ePub href."""
    if not href:
        return None
    return href.split("#", 1)[0] or None


def _split_href(href: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split an ePub href into ``(path, fragment)``. Either may be None."""
    if not href:
        return None, None
    path, _, frag = href.partition("#")
    return (path or None), (frag or None)


def _toc_fragments_per_file(toc: list[dict]) -> dict[str, set[str]]:
    """Map each href path → the set of fragment ids referenced by the TOC.

    A bare reference (no fragment) is recorded under ``""``. Files
    appearing only once with no fragment thus get ``{""}`` and the
    slicer returns the whole-file text under that key.
    """
    out: dict[str, set[str]] = {}
    for entry in toc:
        path, frag = _split_href(entry.get("href"))
        if not path:
            continue
        out.setdefault(path, set()).add(frag or "")
    return out


def _slice_html_by_anchors(
    html_bytes: bytes, fragments: set[str]
) -> dict[str, str]:
    """Slice an xhtml file's text at fragment anchor boundaries.

    Returns a dict keyed by fragment id (or ``""`` for the preamble /
    whole-file case) mapping to the plain text from that anchor up to
    (but not including) the next anchor in document order.

    Behavior:
      * If ``fragments`` contains no real (non-empty) ids, returns
        ``{"": <whole-file text>}``. This is the common "TOC entry
        with no fragment" case.
      * If a fragment in ``fragments`` doesn't resolve to any element
        in the document, that key still appears in the result mapped
        to the whole-file text. This is a *deliberate* fallback so a
        TOC entry with a stale anchor still gets searchable content
        rather than an empty row.
      * If real fragments resolve, ``""`` maps to the preamble (text
        before the first anchor, may be empty if the file starts at
        an anchor).
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    real_fragments = {f for f in fragments if f}

    if not real_fragments:
        # No fragmenting requested — whole-file text under "".
        return {"": soup.get_text(separator="\n", strip=True)}

    # Resolve each fragment to an element by id (modern) or
    # <a name="..."> (legacy). Fragments that don't resolve will fall
    # back to whole-file text below.
    anchor_elements: dict[str, object] = {}
    for frag in real_fragments:
        elem = soup.find(id=frag) or soup.find("a", attrs={"name": frag})
        if elem is not None:
            anchor_elements[frag] = elem
    resolved = set(anchor_elements)
    unresolved = real_fragments - resolved

    body = soup.body or soup

    # If NO requested fragments resolve, every requested key falls
    # back to whole-file text. Don't bother walking the doc.
    if not resolved:
        whole = soup.get_text(separator="\n", strip=True)
        out = {frag: whole for frag in real_fragments}
        if "" in fragments:
            out[""] = whole
        return out

    # Walk descendants in document order. Each time we cross one of
    # the resolved-anchor elements, switch to a new segment keyed by
    # that fragment id. Text before the first anchor goes into "".
    segments: dict[str, list[str]] = {"": []}
    current_key = ""
    for desc in body.descendants:
        # Anchor switch — must run before text collection because
        # the anchor element itself is also iterated as a descendant.
        if hasattr(desc, "get"):
            elem_id = desc.get("id")
            if elem_id and elem_id in resolved:
                current_key = elem_id
                segments.setdefault(current_key, [])
            elif getattr(desc, "name", None) == "a":
                a_name = desc.get("name")
                if a_name and a_name in resolved:
                    current_key = a_name
                    segments.setdefault(current_key, [])
        # Text collection.
        if isinstance(desc, NavigableString) and not isinstance(desc, Comment):
            text = str(desc).strip()
            if text:
                segments[current_key].append(text)

    out = {key: "\n".join(parts) for key, parts in segments.items()}

    # Unresolved fragments fall back to whole-file text so a stale
    # anchor still points at something searchable.
    if unresolved:
        whole = soup.get_text(separator="\n", strip=True)
        for frag in unresolved:
            out[frag] = whole

    return out


def _content_cache_for_book(
    book: epub.EpubBook, toc: list[dict]
) -> dict[str, dict[str, str]]:
    """Return ``{href_path: {fragment_id: plain_text}}`` for the book.

    Where the inner dict's ``""`` key is the preamble (text before
    the first anchor) — or, when the TOC references the file with no
    fragment, the whole-file text.

    This is the post-bug-fix replacement for the old ``href_path →
    full_text`` map. Slicing happens here; ``_insert_chapters`` only
    looks up the right slice per TOC entry.
    """
    fragments_per_file = _toc_fragments_per_file(toc)
    cache: dict[str, dict[str, str]] = {}
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        href = _href_path(item.get_name())
        if not href or href in cache:
            continue
        wanted = fragments_per_file.get(href)
        if wanted is None:
            # File isn't referenced by the TOC at all — skip.
            continue
        try:
            cache[href] = _slice_html_by_anchors(item.get_content(), wanted)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            LOG.warning("slicing failed for %s: %s", href, exc)
            cache[href] = {}
    return cache


# ----------------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------------

def _parse_pub_date(raw: str) -> Optional[date]:
    """Parse a DC date string into a date, trying common epub formats."""
    for fmt, width in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(raw[:width], fmt).date()
        except ValueError:
            continue
    return None


def _clean_filename_title(filepath: Path) -> str:
    """Derive a reasonable title from the ePub filename.

    Drops a trailing ISBN-like token (10 or 13 digits, optionally with an
    'X' check char) and the hyphen before it, so
    "Foo Bar-9781234567890.epub" → "Foo Bar".
    """
    stem = filepath.stem
    # Trailing "-<digits>[X]" pattern captures 10/13-char ISBNs.
    import re  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    stem = re.sub(r"-\d{9,13}[Xx]?$", "", stem).strip()
    return stem or filepath.stem


def _book_metadata(book: epub.EpubBook, filepath: Path) -> dict:
    """Pull the metadata we care about out of an ePub.

    Falls back to the filename (minus ISBN suffix) when DC:title is
    missing, empty, or the sentinel "(blank)" that some tooling emits
    for books with no title metadata.
    """
    title_md = book.get_metadata("DC", "title")
    raw_title = title_md[0][0].strip() if title_md and title_md[0] and title_md[0][0] else ""
    if not raw_title or raw_title.lower() == "(blank)":
        title = _clean_filename_title(filepath)
    else:
        title = raw_title

    creators = book.get_metadata("DC", "creator")
    authors = [c[0] for c in creators] if creators else []

    publisher_md = book.get_metadata("DC", "publisher")
    publisher = publisher_md[0][0] if publisher_md else None

    pub_date: Optional[date] = None
    date_md = book.get_metadata("DC", "date")
    if date_md:
        pub_date = _parse_pub_date(date_md[0][0])

    desc_md = book.get_metadata("DC", "description")
    description = desc_md[0][0] if desc_md else None

    subject_md = book.get_metadata("DC", "subject")
    subjects = [s[0] for s in subject_md] if subject_md else []

    return {
        "title": title,
        "authors": authors,
        "publisher": publisher,
        "publication_date": pub_date,
        "description": description,
        "subjects": subjects,
    }


# ----------------------------------------------------------------------------
# TOC flattening
# ----------------------------------------------------------------------------

def _flatten_toc(book: epub.EpubBook) -> list[dict]:
    """Flatten the TOC into an ordered list of chapters with parent refs.

    Each element: {sequence, depth, parent_seq, title, href}
    """
    flat: list[dict] = []
    counter = [0]  # mutable counter for closure

    def walk(items, depth: int, parent_seq: Optional[int]) -> None:
        for item in items:
            counter[0] += 1
            seq = counter[0]
            if isinstance(item, tuple):
                section, children = item
                title = getattr(section, "title", str(section))
                href = getattr(section, "href", None)
                flat.append({
                    "sequence": seq,
                    "depth": depth,
                    "parent_seq": parent_seq,
                    "title": title,
                    "href": href,
                })
                walk(children, depth + 1, seq)
            elif isinstance(item, epub.Link):
                flat.append({
                    "sequence": seq,
                    "depth": depth,
                    "parent_seq": parent_seq,
                    "title": item.title,
                    "href": item.href,
                })
            # Unknown TOC element types are silently skipped.

    walk(book.toc, depth=0, parent_seq=None)
    return flat


# ----------------------------------------------------------------------------
# DB writers
# ----------------------------------------------------------------------------

def _upsert_author(conn: duckdb.DuckDBPyConnection, name: str) -> int:
    """Return author_id, creating the row if the name isn't already stored."""
    row = conn.execute("SELECT author_id FROM author WHERE name = ?", [name]).fetchone()
    if row:
        return row[0]
    new_id = conn.execute(
        "INSERT INTO author (name) VALUES (?) RETURNING author_id",
        [name],
    ).fetchone()[0]
    return new_id


def _insert_book(conn: duckdb.DuckDBPyConnection, filepath: Path, meta: dict) -> int:
    """Insert a book row and return its new book_id."""
    file_hash = _sha256_file(filepath)
    row = conn.execute(
        """
        INSERT INTO book (title, publisher, publication_date, source_path,
                          description, subjects, total_tokens, chapter_count,
                          content_hash, last_indexed_at, status,
                          indexed_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL,
                ?, CURRENT_TIMESTAMP, 'active',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING book_id
        """,
        [
            meta["title"],
            meta["publisher"],
            meta["publication_date"],
            str(filepath),
            meta["description"],
            meta["subjects"],
            file_hash,
        ],
    ).fetchone()
    return row[0]


def _delete_existing_book(conn: duckdb.DuckDBPyConnection, filepath: Path) -> None:
    """Remove any previously indexed rows for the given source path.

    chapter.parent_chapter_id is a logical self-reference without an FK
    (see schema notes), so a bulk DELETE works without ordering games.
    """
    row = conn.execute(
        "SELECT book_id FROM book WHERE source_path = ?", [str(filepath)]
    ).fetchone()
    if not row:
        return
    book_id = row[0]
    # Delete anything that FKs into chapter first; currently only
    # chapter_embedding does. Ordered so children land before parents.
    conn.execute(
        "DELETE FROM chapter_embedding WHERE chapter_id IN "
        "(SELECT chapter_id FROM chapter WHERE book_id = ?)",
        [book_id],
    )
    conn.execute("DELETE FROM chapter WHERE book_id = ?", [book_id])
    conn.execute("DELETE FROM book_author WHERE book_id = ?", [book_id])
    conn.execute("DELETE FROM book WHERE book_id = ?", [book_id])


def _insert_chapters(
    conn: duckdb.DuckDBPyConnection,
    book_id: int,
    toc: list[dict],
    content_cache: dict[str, dict[str, str]],
) -> tuple[int, int]:
    """Insert chapter rows and return (chapter_count, total_tokens).

    Parent-chapter resolution uses the sequence field: rows are inserted in
    TOC order, and we track sequence → chapter_id as we go.

    The ``content_cache`` maps each href path to a per-fragment slice
    dict produced by ``_content_cache_for_book``. This lookup honors
    the fragment, which fixes the historical bug where every TOC
    entry pointing at the same xhtml file received the full file's
    content.
    """
    seq_to_id: dict[int, int] = {}
    total_tokens = 0

    for entry in toc:
        path, frag = _split_href(entry.get("href"))
        slices = content_cache.get(path, {}) if path else {}
        # Lookup order: the entry's own fragment first; fall back to
        # the whole-file / preamble slot under ""; finally empty.
        content = slices.get(frag or "", "") if slices else ""
        tokens = _count_tokens(content) if content else 0
        total_tokens += tokens

        parent_id = seq_to_id.get(entry["parent_seq"]) if entry["parent_seq"] else None

        content_for_db = content or None
        content_hash = _sha256_text(content) if content else None
        new_id = conn.execute(
            """
            INSERT INTO chapter (book_id, chapter_num, parent_chapter_id,
                                 title, href, content, content_hash,
                                 token_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING chapter_id
            """,
            [
                book_id,
                entry["sequence"],
                parent_id,
                entry["title"],
                entry["href"],
                content_for_db,
                content_hash,
                tokens or None,
            ],
        ).fetchone()[0]
        seq_to_id[entry["sequence"]] = new_id

    return len(toc), total_tokens


def _link_authors(
    conn: duckdb.DuckDBPyConnection, book_id: int, author_names: list[str]
) -> None:
    """Create book_author rows for each distinct author in order."""
    seen: set[str] = set()
    position = 0
    for name in author_names:
        if not name or name in seen:
            continue
        seen.add(name)
        author_id = _upsert_author(conn, name)
        conn.execute(
            "INSERT INTO book_author (book_id, author_id, position) VALUES (?, ?, ?)",
            [book_id, author_id, position],
        )
        position += 1


def _finalize_book(
    conn: duckdb.DuckDBPyConnection,
    book_id: int,
    chapter_count: int,
    total_tokens: int,
) -> None:
    """Write chapter_count and total_tokens back onto the book row."""
    conn.execute(
        "UPDATE book SET chapter_count = ?, total_tokens = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE book_id = ?",
        [chapter_count, total_tokens, book_id],
    )


# ----------------------------------------------------------------------------
# Top-level per-book driver
# ----------------------------------------------------------------------------

def index_book(conn: duckdb.DuckDBPyConnection, filepath: Path) -> bool:
    """Index one ePub file. Returns True on success."""
    try:
        book = epub.read_epub(str(filepath))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.error("cannot read %s: %s", filepath, exc)
        return False

    try:
        meta = _book_metadata(book, filepath)
        toc = _flatten_toc(book)
        content_cache = _content_cache_for_book(book, toc)

        _delete_existing_book(conn, filepath)
        book_id = _insert_book(conn, filepath, meta)
        _link_authors(conn, book_id, meta["authors"])
        chapter_count, total_tokens = _insert_chapters(conn, book_id, toc, content_cache)
        _finalize_book(conn, book_id, chapter_count, total_tokens)

        LOG.info(
            "indexed %s (id=%d, %d chapters, %d tokens)",
            filepath.name, book_id, chapter_count, total_tokens,
        )
        return True
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.exception("indexing failed for %s: %s", filepath, exc)
        return False


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Parse CLI args and index the requested ePub files into the catalog."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--book", type=str, default=None, help="Index one book by filename")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.source.exists():
        LOG.error("source directory not found: %s", args.source)
        return 2

    if args.book:
        files = [args.source / args.book]
        if not files[0].exists():
            LOG.error("book not found: %s", files[0])
            return 2
    else:
        files = sorted(args.source.glob("*.epub"))

    if args.limit:
        files = files[: args.limit]

    if not files:
        LOG.error("no epub files found")
        return 2

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(args.catalog))
    try:
        start = time.time()
        ok = 0
        for i, filepath in enumerate(files, 1):
            LOG.info("[%d/%d] %s", i, len(files), filepath.name)
            if index_book(conn, filepath):
                ok += 1
            if i % 25 == 0:
                conn.commit()
        conn.commit()
        elapsed = time.time() - start
        LOG.info("done: %d/%d books indexed in %.1fs", ok, len(files), elapsed)
    finally:
        conn.close()

    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
