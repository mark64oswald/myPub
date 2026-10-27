"""Broken-NCX recovery and pdf2htmlEX text extraction in index_books.py."""
from __future__ import annotations

import sys
import types
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

try:  # tiktoken downloads its encoding on first use; stub it when offline
    import tiktoken
    tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    fake = types.ModuleType("tiktoken")
    fake.get_encoding = lambda _n: types.SimpleNamespace(
        encode=lambda t, disallowed_special=(): t.split())
    sys.modules["tiktoken"] = fake

import index_books as ib  # noqa: E402

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0" unique-identifier="id">
<metadata><dc:identifier id="id">x</dc:identifier><dc:title>Broken Book</dc:title><dc:language>en</dc:language></metadata>
<manifest>
  <item id="ncx" media-type="application/x-dtbncx+xml" href="toc.ncx"/>
  <item id="c1" media-type="application/xhtml+xml" href="chapter-01.html"/>
  <item id="c2" media-type="application/xhtml+xml" href="chapter-02.html"/>
</manifest>
<spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""

PAGE = """<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Broken Book</title></head><body><div id="pdf-main">
<div class="t m0">Int<span class="_ _0"></span>ro<span class="_ _0"></span>duction to {w}</div>
<div class="t m0">second li<span class="_ _1"></span>ne</div></div></body></html>"""


def _broken_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:'
                   'tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/'
                   'content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/toc.ncx", '<?xml version="1.0"?><HTML><HEAD><TITLE>Access Denied'
                                    '</TITLE></HEAD><BODY>denied</BODY></HTML>')
        z.writestr("OEBPS/chapter-01.html", PAGE.format(w="alpha"))
        z.writestr("OEBPS/chapter-02.html", PAGE.format(w="beta"))
    return path


def test_broken_ncx_falls_back_to_spine(tmp_path):
    p = _broken_epub(tmp_path / "Broken Book-9781482229233.epub")
    book = ib._read_epub(p)
    toc = ib._flatten_toc(book)
    assert [e["title"] for e in toc] == ["Broken Book (1)", "Broken Book (2)"]
    cache = ib._content_cache_for_book(book, toc)
    text = cache["chapter-01.html"][""]
    assert text.splitlines() == ["Introduction to alpha", "second line"]


def test_pdf2htmlex_flatten_is_noop_for_normal_html():
    html = b"<html><body><p>one</p><div class='t'>two<span>three</span></div></body></html>"
    out = ib._slice_html_by_anchors(html, {""})[""]
    assert out.splitlines() == ["one", "two", "three"]
