"""Tests for Quickstart code-block selection (install / hello-world intent)."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import quickstart as qs  # noqa: E402


def _b(code, lang="python"):
    return qs._CodeBlock(language=lang, code=code, source_section_id=1)


HELLO = _b("import pymupdf\n\ndoc = pymupdf.open('a.pdf')\nprint(doc.page_count)")
CI_YAML = _b(
    "name: test\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n      - run: pip install pymupdf\n"
    + "\n".join(f"      - run: echo {i}" for i in range(40)),
    lang="yaml",
)
VENV = _b("python -m venv .venv\nsource .venv/bin/activate\n"
          "ls .venv/lib/python3.12/site-packages", lang="bash")
LONG_EXAMPLE = _b("import pymupdf\n" + "\n".join(
    f"x{i} = pymupdf.Matrix({i}, {i})" for i in range(150)))
PIP = _b("pip install pymupdf", lang="bash")


def test_noise_detection():
    assert qs._is_noise_block(CI_YAML)
    assert qs._is_noise_block(VENV)
    assert not qs._is_noise_block(HELLO)
    assert not qs._is_noise_block(PIP)
    assert not qs._is_noise_block(
        _b('[dependencies]\ntokio = "1"', lang="toml"))
    assert qs._is_noise_block(_b('[tool.black]\nline-length = 88', lang="toml"))


def test_hello_prefers_short_import_block_over_longest():
    picked = qs._pick_primary_block([LONG_EXAMPLE, CI_YAML, HELLO], "python")
    assert picked is HELLO


def test_hello_never_picks_ci_when_real_code_exists():
    assert qs._pick_primary_block([CI_YAML, VENV, HELLO], None) is HELLO


def test_install_ranking_puts_package_manager_first_and_drops_noise():
    ranked = qs._rank_install_blocks([VENV, CI_YAML, PIP])
    assert ranked[0] is PIP
    assert CI_YAML not in ranked and VENV not in ranked


def test_hello_fallback_rejects_install_command():
    d = qs._Decomposition(
        library_name="pymupdf", doc_source_id=1, doc_source_name="pymupdf",
        framing_text=None, framing_source=None, framing_source_id=None,
        install_blocks=[PIP, CI_YAML], hello_blocks=[],
    )
    assert qs._pick_hello_block(d) is None
    d.install_blocks.append(HELLO)
    assert qs._pick_hello_block(d) is HELLO


def test_python_from_import_is_not_noise():
    assert not qs._is_noise_block(_b("x = 1\nfrom foo import bar\nprint(bar)"))


def test_install_ranking_prefers_a_command_for_this_library():
    other = qs._CodeBlock("bash", "pip install google-cloud-bigquery", 1)
    own = qs._CodeBlock("bash", "pip install polars", 2)
    assert qs._rank_install_blocks([other, own], "Polars")[0] is own
    assert not qs._installs_library(other, "Polars")


def test_name_keys_cover_common_package_spellings():
    assert qs._installs_library(qs._CodeBlock("bash", "pip install delta-spark", 1), "Delta Lake")
    assert qs._installs_library(qs._CodeBlock("bash", "pip3 install torch torchvision", 1), "PyTorch")
    assert qs._installs_library(qs._CodeBlock("bash", "pip install pymupdf", 1), "PyMuPDF")
    assert qs._installs_library(qs._CodeBlock("bash", "cargo add polars", 1), "pola-rs/polars")
    assert not qs._installs_library(qs._CodeBlock("bash", "pip install apache-airflow", 1),
                                    "Apache Kafka")


def test_hello_block_must_use_the_library():
    other = qs._CodeBlock("python", "import pandas as pd\nprint(pd.DataFrame())", 1)
    own = qs._CodeBlock("python", "import polars as pl\n\ndf = pl.DataFrame({'a': [1]})\nprint(df)", 2)
    assert qs._pick_primary_block([other, own], "python", library_name="Polars") is own
    # With nothing that uses the library, still pick something.
    assert qs._pick_primary_block([other], "python", library_name="Polars") is other
