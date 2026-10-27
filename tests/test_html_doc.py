from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import html_doc  # noqa: E402

PAGE = """<html><head><title>PS3.15</title><script>x()</script></head><body>
<nav>menu</nav>
<h1>E Attribute Confidentiality Profiles</h1>
<p>The Basic Profile removes identifying attributes.</p>
<h2>E.1 Application Level Confidentiality Profile</h2>
<ul><li>Remove <b>Patient Name</b></li><li>Replace UIDs</li></ul>
<table><caption>Table E.1-1</caption>
<tr><th>Attribute</th><th>Tag</th><th>Basic</th></tr>
<tr><td>Patient's Name</td><td>(0010,0010)</td><td>Z</td></tr>
<tr><td>Note | pipe</td><td>(0010,4000)</td><td>X</td></tr>
</table>
<pre>ISA*00*</pre>
</body></html>"""


def test_html_to_markdown_keeps_structure_and_drops_chrome():
    title, md = html_doc.html_to_markdown(PAGE)
    assert title == "PS3.15"
    assert "# E Attribute Confidentiality Profiles" in md
    assert "## E.1 Application Level Confidentiality Profile" in md
    assert "- Remove Patient Name" in md
    assert "**Table E.1-1**" in md
    assert "| Patient's Name | (0010,0010) | Z |" in md
    assert "Note \\| pipe" in md
    assert "```\nISA*00*\n```" in md
    assert "menu" not in md and "x()" not in md


def test_site_markdown_from_files(tmp_path):
    f = tmp_path / "p.html"
    f.write_text(PAGE)
    md = html_doc.site_markdown(html_doc._read_pages([], [f]), "DICOM PS3.15")
    assert md.startswith("# DICOM PS3.15")
    assert f"Source: {f.as_uri()}" in md
