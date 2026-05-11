"""quickstart.py — Phase 18 Quickstart Generator.

A first-contact artifact for a single library: install + hello-world +
verification + pointers, on one page. Distinct from:

  - Cheatsheet: assumes you're already using the library; reference grid
  - Tutorial: multi-stage sequenced learning
  - Bootstrap: full project scaffold composing multiple components

Audience: "I want to try X for the first time."

Inputs:
  - library (str): library name; resolved against doc_source.name
  - language_hint (Optional[str]): nudges code block selection
    when the library has examples in multiple languages

Output: data/generated-packages/quickstart-<slug>/
  _quickstart.md       single-page artifact (start here)
  hello_world/<file>   minimal runnable code, when extractable

Source weighting:
  - Install instructions: doc_source FIRST (currency_critical_interactive)
  - Hello-world code: doc_source FIRST; book chapters as fallback
  - Framing ("what it is"): book chapter if available, else doc_source
"""
from __future__ import annotations

import logging
import re
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

LOG = logging.getLogger("mypub-quickstart")

GENERATOR_TYPE = "quickstart"

# Topic keyword sets — first match wins per section
INSTALL_KEYWORDS = [
    "pip install", "pip3 install",
    "npm install", "npm i ", "yarn add", "pnpm add",
    "cargo add", "cargo install",
    "go get", "go install",
    "gem install",
    "brew install", "apt install", "apt-get install",
    "docker pull",
    "mvn install", "gradle", "<dependency>",
    "nuget install", "dotnet add package",
    "composer require",
    "installation", "install ", "setup",
]

HELLO_KEYWORDS = [
    "hello world", "hello, world",
    "minimal example", "minimal program",
    "first example", "basic example", "simple example",
    "getting started", "quickstart", "quick start",
    "your first",
]

VERIFY_KEYWORDS = [
    "verify", "verify installation", "verify that",
    "smoke test", "sanity check", "test that",
    "should output", "should print", "expected output",
]

# Code block regex (triple-backtick fenced)
_CODE_BLOCK_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)

# Languages we EXCLUDE — these are diagrams/markup, not runnable code
_DIAGRAM_LANGS = {
    "mermaid", "plantuml", "puml", "dot", "graphviz", "asciiart",
    "ditaa", "blockdiag", "seqdiag", "math", "tex", "latex",
}

# Languages we ACCEPT as runnable code. Empty string is permitted only
# when language_hint is supplied (we trust the hint).
_RUNNABLE_LANGS = {
    "python", "py", "ipython", "pycon",
    "rust", "rs",
    "javascript", "js", "typescript", "ts", "jsx", "tsx",
    "java", "kotlin", "scala", "groovy",
    "go", "golang",
    "c", "cpp", "c++", "cc",
    "csharp", "cs", "fsharp", "fs",
    "ruby", "rb", "php", "perl", "pl",
    "swift", "objc", "objectivec",
    "bash", "sh", "shell", "zsh", "fish", "ps1", "powershell",
    "sql", "psql", "plpgsql",
    "html", "css", "yaml", "yml", "json", "toml", "xml",
    "dockerfile", "docker",
}


# ---------------------------------------------------------------------------
# Decomposition shape
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    doc_section_id: int
    snapshot_id: int
    content: str
    heading_level: Optional[int]
    parent_id: Optional[int]
    rank_score: float = 0.0      # length-based weighting after keyword match


@dataclass
class _CodeBlock:
    language: str                 # 'python', 'rust', 'bash', ''
    code: str
    source_section_id: int


@dataclass
class _Decomposition:
    library_name: str
    doc_source_id: int
    doc_source_name: str
    framing_text: Optional[str]
    framing_source: Optional[str]    # 'book' or 'doc_section'
    framing_source_id: Optional[int]
    install_sections: list[_Section] = field(default_factory=list)
    hello_sections: list[_Section] = field(default_factory=list)
    verify_sections: list[_Section] = field(default_factory=list)
    install_blocks: list[_CodeBlock] = field(default_factory=list)
    hello_blocks: list[_CodeBlock] = field(default_factory=list)
    verify_blocks: list[_CodeBlock] = field(default_factory=list)
    language_hint: Optional[str] = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-").replace("/", "-").replace("_", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-."
    out = "".join(c for c in s if c in keep).strip("-.")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "library"


def _first_sentence(text: str, max_chars: int = 280) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    # Split on period+space, take first non-trivial
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    for p in parts:
        if len(p) > 20:
            if len(p) > max_chars:
                return p[:max_chars].rsplit(" ", 1)[0] + "…"
            return p
    return cleaned[:max_chars]


def _extract_code_blocks(
    section_id: int, content: str, language_hint: Optional[str] = None,
) -> list[_CodeBlock]:
    """Extract fenced code blocks, filtering out diagrams and prose.

    Rules:
      - Drop blocks tagged with a diagram language (mermaid, plantuml, dot…)
      - Require a recognized programming language OR a matching
        language_hint (an empty language tag is OK only when hint is set)
      - Drop blocks too short or that look like expected-output prose
        (no parens, no equals, no import/use/fn/def keywords)
    """
    blocks: list[_CodeBlock] = []
    hint_lc = (language_hint or "").lower()
    for m in _CODE_BLOCK_RE.finditer(content or ""):
        lang = (m.group(1) or "").lower()
        code = m.group(2).strip()
        if not code or len(code) < 5:
            continue
        if lang in _DIAGRAM_LANGS:
            continue
        # Need a runnable language tag, or an empty tag + hint
        if lang:
            if lang not in _RUNNABLE_LANGS:
                continue
        else:
            if not hint_lc:
                continue  # untagged + no hint → can't trust as code
        # Note: we do NOT filter by hint match here — install blocks
        # are typically shell even when the library is Rust/Python.
        # _pick_primary_block does hint-preference selection downstream.
        # Drop blocks that look like expected-output prose
        if not any(tok in code for tok in (
            "(", "=", "import ", "use ", "fn ", "def ", "let ", "var ",
            "$", "#!/", "<", ":", "//", "#",
        )):
            continue
        # Drop test functions — they're not hello-world examples
        first_line = code.lstrip().split("\n", 1)[0]
        if re.match(r"^\s*(def|fn|function|async\s+fn)\s+test[_A-Z]", first_line):
            continue
        # Drop pure JSON-shape literals (block starts with `{` and is
        # mostly key:value pairs — these are output-format docs, not
        # runnable code)
        stripped = code.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            # Heuristic: lots of `"key":` patterns and no function call
            colon_pairs = len(re.findall(r'"\w+"\s*:', stripped))
            calls = len(re.findall(r"\w+\([^)]*\)", stripped))
            if colon_pairs >= 3 and calls < 2:
                continue
        blocks.append(_CodeBlock(
            language=lang or hint_lc,
            code=code,
            source_section_id=section_id,
        ))
    return blocks


def _section_matches_keywords(content: str, keywords: list[str]) -> bool:
    haystack = (content or "").lower()
    return any(kw in haystack for kw in keywords)


# Package-manager templates keyed by LANGUAGE (not by library or domain).
# This is the only language-specific data the generator carries, and it's
# fully domain-agnostic: the same map applies to any library we've
# ingested today or might ingest tomorrow. The language used to pick a
# template is resolved at runtime from (1) caller-supplied hint, then
# (2) inferred from code-block language tags in the library's own docs.
_PACKAGE_MANAGER_BY_LANGUAGE: dict[str, tuple[str, str]] = {
    "python":     ("pip install {name}", "bash"),
    "rust":       ("cargo add {name}", "bash"),
    "javascript": ("npm install {name}", "bash"),
    "typescript": ("npm install {name}", "bash"),
    "go":         ("go get {name}", "bash"),
    "java":       ("# Maven dependency\n<dependency>\n"
                   "  <artifactId>{name}</artifactId>\n</dependency>", "xml"),
    "kotlin":     ("# Maven dependency\n<dependency>\n"
                   "  <artifactId>{name}</artifactId>\n</dependency>", "xml"),
    "scala":      ("# build.sbt\nlibraryDependencies += "
                   "\"<group>\" %% \"{name}\" % \"<version>\"", "scala"),
    "csharp":     ("dotnet add package {name}", "bash"),
    "fsharp":     ("dotnet add package {name}", "bash"),
    "ruby":       ("gem install {name}", "bash"),
    "php":        ("composer require {name}", "bash"),
    "swift":      ("# Package.swift\n.package(url: \"https://...\","
                   " from: \"1.0.0\")", "swift"),
    "elixir":     ("# mix.exs\ndeps: [{{:{name}, \"~> 1.0\"}}]", "elixir"),
    "haskell":    ("cabal install {name}", "bash"),
    "lua":        ("luarocks install {name}", "bash"),
    "perl":       ("cpan {name}", "bash"),
    "r":          ("install.packages(\"{name}\")", "r"),
    "c":          ("# Add as a library dependency in your build system "
                   "(CMake / Make / Bazel).", "bash"),
    "cpp":        ("# Add as a library dependency in your build system "
                   "(CMake / Make / Bazel) — or use vcpkg / Conan.", "bash"),
}

# Common alternate spellings → canonical key in the map above.
_LANGUAGE_ALIASES = {
    "py": "python", "rs": "rust", "js": "javascript", "ts": "typescript",
    "jsx": "javascript", "tsx": "typescript",
    "golang": "go", "cs": "csharp", "fs": "fsharp",
    "rb": "ruby", "c++": "cpp", "cxx": "cpp", "cc": "cpp",
    "kt": "kotlin", "ex": "elixir", "exs": "elixir",
    "hs": "haskell",
}


_PM_COMMAND_TOKENS = (
    "pip install", "pip3 install", "pipx install",
    "npm install", "npm i ", "yarn add", "pnpm add",
    "cargo add", "cargo install",
    "go get", "go install",
    "gem install",
    "brew install", "apt install", "apt-get install",
    "dotnet add package", "nuget install",
    "composer require",
)


def _has_package_manager_command(blocks: list[_CodeBlock]) -> bool:
    """True if any block looks like a real package-manager install command."""
    for b in blocks:
        head = b.code[:200].lower()
        if any(tok in head for tok in _PM_COMMAND_TOKENS):
            return True
        # Maven/Gradle dependency-fragment markers
        if "<artifactid>" in head or "groupid" in head:
            return True
    return False


# Shell-family tags are meta-language: they show up for install commands
# and CI scripts, not for the library's actual code. They dominate when
# a library's docs are mostly setup-and-run examples (e.g., a frontend
# package or a CLI tool with shell invocation examples), which crowds out
# the *real* primary language. Filtering them out yields a cleaner signal.
_META_LANGUAGE_TAGS = {
    "bash", "sh", "shell", "zsh", "fish", "ps1", "powershell",
    "console", "text", "txt", "plaintext", "diff", "log",
    "yaml", "yml", "toml", "json", "xml", "html", "css",
    "dockerfile", "docker", "makefile",
}


def _detect_language_from_sections(
    sections: list[_Section],
) -> Optional[str]:
    """Infer the library's primary language from its doc-section code blocks.

    Counts fenced code-block language tags across all latest-snapshot
    sections, then picks the most-frequent **runnable application
    language** — skipping shell/markup/config tags that show up as
    install commands or CI snippets rather than the library's own code.

    Domain-agnostic by construction: this looks only at what languages
    dominate the library's own docs, never at the library name or its
    GitHub identifier path.

    Returns ``None`` if no runnable application language appears.
    """
    counts: dict[str, int] = {}
    for s in sections:
        for m in _CODE_BLOCK_RE.finditer(s.content or ""):
            lang = (m.group(1) or "").lower()
            if not lang or lang in _DIAGRAM_LANGS:
                continue
            canonical = _LANGUAGE_ALIASES.get(lang, lang)
            if canonical in _META_LANGUAGE_TAGS:
                continue
            if (canonical not in _RUNNABLE_LANGS
                    and canonical not in _PACKAGE_MANAGER_BY_LANGUAGE):
                continue
            counts[canonical] = counts.get(canonical, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _default_install_command(
    library_name: str, language_hint: Optional[str],
    sections: list[_Section],
) -> Optional[_CodeBlock]:
    """Synthesize an install command for ANY library, ANY domain.

    Resolution order for the language used to pick the package manager:
      1. Caller-supplied ``language_hint`` (after alias normalization)
      2. Most-frequent runnable code-block language inferred across
         the library's own doc sections (corpus signal, never the
         library name or its repo identifier)

    Returns ``None`` only if both signals are absent or the detected
    language has no package-manager entry. ``source_section_id == -1``
    flags the block as synthesized rather than corpus-derived.
    """
    pkg = re.sub(r"\s+\(.*\)$", "", library_name).strip()
    pkg_norm = pkg.lower().replace(" ", "-")

    # 1. Honor explicit hint first
    lang: Optional[str] = None
    if language_hint:
        h = language_hint.lower()
        lang = _LANGUAGE_ALIASES.get(h, h)
    # 2. Otherwise infer from the corpus
    if lang is None or lang not in _PACKAGE_MANAGER_BY_LANGUAGE:
        inferred = _detect_language_from_sections(sections)
        if inferred and inferred in _PACKAGE_MANAGER_BY_LANGUAGE:
            lang = inferred

    if lang is None or lang not in _PACKAGE_MANAGER_BY_LANGUAGE:
        return None
    template, fence = _PACKAGE_MANAGER_BY_LANGUAGE[lang]
    return _CodeBlock(
        language=fence,
        code=template.format(name=pkg_norm),
        source_section_id=-1,  # synthesized marker
    )


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class QuickstartDecomposer:
    """Resolve the library to its doc_source, pull install/hello/verify
    sections by keyword, and extract code blocks per topic.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        library: Optional[str] = None,
        language_hint: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        library_name = library or query
        notes: list[str] = []

        # 1. Resolve library to doc_source
        ds = self._resolve_doc_source(conn, library_name)
        if ds is None:
            # Build a minimal "not found" decomposition so the validator
            # surfaces a clean error.
            return _Decomposition(
                library_name=library_name,
                doc_source_id=-1,
                doc_source_name=library_name,
                framing_text=None,
                framing_source=None,
                framing_source_id=None,
                language_hint=language_hint,
                notes=[
                    f"library {library_name!r} not found in doc_source — "
                    "try /kb-discover first to seed it",
                ],
            )
        doc_source_id, doc_source_name = ds

        # 2. Pull all sections for the latest snapshot
        sections = self._latest_sections(conn, doc_source_id)
        if not sections:
            notes.append(f"doc_source {doc_source_name} has no sections; "
                         "try /kb-discover or refresh_docs first")

        # 3. Bucket sections by topic
        install_sec = self._rank_filter(sections, INSTALL_KEYWORDS, top_k=3)
        hello_sec = self._rank_filter(sections, HELLO_KEYWORDS, top_k=3)
        verify_sec = self._rank_filter(sections, VERIFY_KEYWORDS, top_k=2)

        # 4. Extract code blocks per topic
        install_blocks: list[_CodeBlock] = []
        for s in install_sec:
            install_blocks.extend(_extract_code_blocks(
                s.doc_section_id, s.content, language_hint))
        hello_blocks: list[_CodeBlock] = []
        for s in hello_sec:
            hello_blocks.extend(_extract_code_blocks(
                s.doc_section_id, s.content, language_hint))
        verify_blocks: list[_CodeBlock] = []
        for s in verify_sec:
            verify_blocks.extend(_extract_code_blocks(
                s.doc_section_id, s.content, language_hint))

        # 5. Framing — prefer a book chapter; fall back to first doc_section
        framing_text, framing_source, framing_source_id = self._find_framing(
            conn, library_name, doc_source_id, sections,
        )

        # 6a. Install fallback: synthesize a default install command if
        # (a) no code blocks were extracted, OR (b) none of the
        # extracted blocks contain a recognized package-manager
        # command. The latter case catches sections that match the
        # "install" keyword but actually quote internal source code
        # rather than installation commands.
        # If any extracted block IS a package-manager command (or Maven
        # dependency fragment), float it to the front so the renderer's
        # by-language dedup picks the install command — not whichever
        # block happened to appear earliest in the section list.
        if _has_package_manager_command(install_blocks):
            install_blocks = sorted(
                install_blocks,
                key=lambda b: 0 if _has_package_manager_command([b]) else 1,
            )
        if not _has_package_manager_command(install_blocks):
            default_block = _default_install_command(
                library_name, language_hint, sections,
            )
            if default_block is not None:
                # Prepend the synthesized command so it leads the install
                # section; keep any extracted blocks as supplemental context.
                install_blocks = [default_block] + install_blocks
                if install_blocks[1:]:
                    notes.append(
                        "extracted install blocks did not contain a package-"
                        "manager command; synthesized default leads the section "
                        "(language inferred from doc-section code blocks)")
                else:
                    notes.append(
                        "corpus had no install block; synthesized default "
                        "from package-manager heuristic (language inferred from "
                        "doc-section code blocks)")
            elif not install_blocks:
                notes.append("no install code blocks extracted; falling back to "
                             "section text only — install commands may be inline prose")

        # 6b. Hello-world fallback: scan ALL sections for the first
        # import-bearing code block matching the language hint. Catches
        # libraries whose docs have hello-world examples but not under a
        # "Hello World" or "Getting Started" heading.
        if not hello_blocks:
            hello_blocks = self._scan_for_import_block(
                sections, language_hint,
            )
            if hello_blocks:
                notes.append(
                    "no hello-world keyword section found; surfaced the first "
                    "import-bearing code block from the corpus")
            else:
                notes.append("no hello-world code blocks extracted; "
                             "the artifact will surface the section text instead")

        return _Decomposition(
            library_name=library_name,
            doc_source_id=doc_source_id,
            doc_source_name=doc_source_name,
            framing_text=framing_text,
            framing_source=framing_source,
            framing_source_id=framing_source_id,
            install_sections=install_sec,
            hello_sections=hello_sec,
            verify_sections=verify_sec,
            install_blocks=install_blocks,
            hello_blocks=hello_blocks,
            verify_blocks=verify_blocks,
            language_hint=language_hint,
            notes=notes,
        )

    def _scan_for_import_block(
        self, sections: list[_Section], language_hint: Optional[str],
    ) -> list[_CodeBlock]:
        """Scan every section for code blocks containing imports.

        Returns the import-bearing blocks across all sections — the
        caller picks the canonical one. Honors the language hint when
        present; falls back to any runnable block with an import.
        """
        out: list[_CodeBlock] = []
        for s in sections:
            for b in _extract_code_blocks(
                s.doc_section_id, s.content, language_hint,
            ):
                head = b.code[:200].lower()
                if any(tok in head for tok in (
                    "import ", "from ", "use ", "require(", "#include", "using ",
                )):
                    out.append(b)
        return out

    def _resolve_doc_source(
        self, conn: duckdb.DuckDBPyConnection, library: str,
    ) -> Optional[tuple[int, str]]:
        # Exact (case-insensitive) match preferred
        row = conn.execute(
            "SELECT doc_source_id, name FROM doc_source "
            " WHERE LOWER(name) = LOWER(?)",
            [library],
        ).fetchone()
        if row:
            return int(row[0]), row[1]
        # Fuzzy contains as fallback
        row = conn.execute(
            "SELECT doc_source_id, name FROM doc_source "
            " WHERE LOWER(name) LIKE ? ORDER BY LENGTH(name) ASC LIMIT 1",
            [f"%{library.lower()}%"],
        ).fetchone()
        if row:
            return int(row[0]), row[1]
        return None

    def _latest_sections(
        self, conn: duckdb.DuckDBPyConnection, doc_source_id: int,
    ) -> list[_Section]:
        rows = conn.execute(
            """
            SELECT s.doc_section_id, s.snapshot_id, s.content,
                   s.heading_level, s.parent_id
              FROM doc_section s
              JOIN doc_snapshot sn USING(snapshot_id)
             WHERE sn.doc_source_id = ?
             ORDER BY sn.snapshot_id DESC, s.doc_section_id ASC
            """,
            [doc_source_id],
        ).fetchall()
        out = [_Section(
            doc_section_id=int(r[0]), snapshot_id=int(r[1]),
            content=r[2] or "",
            heading_level=int(r[3]) if r[3] is not None else None,
            parent_id=int(r[4]) if r[4] is not None else None,
        ) for r in rows]
        # Limit to the most-recent snapshot only
        if not out:
            return out
        latest_snap = out[0].snapshot_id
        return [s for s in out if s.snapshot_id == latest_snap]

    def _rank_filter(
        self, sections: list[_Section], keywords: list[str],
        top_k: int = 3,
    ) -> list[_Section]:
        """Filter sections by keyword match; rank by content length."""
        matched: list[_Section] = []
        for s in sections:
            if _section_matches_keywords(s.content, keywords):
                # Length-based rank, but apply a tie-breaker bonus for
                # sections that have a code block (more substantive)
                bonus = 50 if "```" in s.content else 0
                s.rank_score = float(len(s.content) + bonus)
                matched.append(s)
        matched.sort(key=lambda s: -s.rank_score)
        return matched[:top_k]

    def _find_framing(
        self,
        conn: duckdb.DuckDBPyConnection, library_name: str,
        doc_source_id: int, sections: list[_Section],
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """Pick a framing snippet.

        Priority order:
          1. Book chapter whose title is an "Introduction"/"Overview"/
             "What is" + library, OR whose title centers the library.
          2. The first substantive doc_section that introduces the
             library (skipping mermaid-heavy / "Relevant source files"
             boilerplate that DeepWiki prepends).
          3. None.

        We avoid the prior "any chapter mentioning the library" rule
        which surfaced chapters where the library was incidental.
        """
        lib_lc = library_name.lower()

        # 1a. Strong title match: "introducing/intro/what is/overview/getting started" + library
        intro_pat = (
            f"%introduc%{lib_lc}%",
            f"%what is %{lib_lc}%",
            f"%overview%{lib_lc}%",
            f"%getting started%{lib_lc}%",
            f"%intro to %{lib_lc}%",
        )
        for pat in intro_pat:
            row = conn.execute(
                "SELECT chapter_id, content FROM chapter "
                " WHERE LOWER(title) LIKE ? "
                "   AND content IS NOT NULL AND LENGTH(content) > 200 "
                " ORDER BY LENGTH(content) ASC LIMIT 1",
                [pat],
            ).fetchone()
            if row:
                return (_first_sentence(row[1], max_chars=400),
                        "chapter", int(row[0]))

        # 1b. Title that STARTS WITH the library name (e.g. "Tokio Basics")
        row = conn.execute(
            "SELECT chapter_id, content FROM chapter "
            " WHERE LOWER(title) LIKE ? "
            "   AND content IS NOT NULL AND LENGTH(content) > 200 "
            " ORDER BY LENGTH(content) DESC LIMIT 1",
            [f"{lib_lc}%"],
        ).fetchone()
        if row:
            return (_first_sentence(row[1], max_chars=400),
                    "chapter", int(row[0]))

        # 2. First substantive doc_section, skipping DeepWiki boilerplate
        for s in sections:
            txt = s.content
            # Strip DeepWiki "Relevant source files" preamble
            txt = re.sub(r"<details>.*?</details>", "", txt, flags=re.DOTALL)
            # Strip code/mermaid blocks
            txt = re.sub(r"```.*?```", "", txt, flags=re.DOTALL)
            sent = _first_sentence(txt, max_chars=400)
            if len(sent) > 60:
                return sent, "doc_section", s.doc_section_id
        return None, None, None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _pick_primary_block(
    blocks: list[_CodeBlock], language_hint: Optional[str],
) -> Optional[_CodeBlock]:
    """Pick the canonical block for an artifact.

    Preference order:
      1. Language-hint-matched blocks containing an import/use/require
         (canonical hello-world shape)
      2. Language-hint-matched blocks, longest first
      3. Any blocks containing an import statement
      4. Longest block
    """
    if not blocks:
        return None
    hint = (language_hint or "").lower()
    matched = []
    if hint:
        matched = [b for b in blocks
                   if hint in b.language or b.language in hint]

    def _has_import(b: _CodeBlock) -> bool:
        head = b.code[:200].lower()
        return any(tok in head for tok in (
            "import ", "from ", "use ", "require(", "#include", "using ",
        ))

    # Tier 1: matched + has-import
    matched_imports = [b for b in matched if _has_import(b)]
    if matched_imports:
        return max(matched_imports, key=lambda b: len(b.code))
    # Tier 2: matched (any)
    if matched:
        return max(matched, key=lambda b: len(b.code))
    # Tier 3: any block with import
    with_imports = [b for b in blocks if _has_import(b)]
    if with_imports:
        return max(with_imports, key=lambda b: len(b.code))
    # Tier 4: longest
    return max(blocks, key=lambda b: len(b.code))


def _render_install(d: _Decomposition) -> str:
    lines = ["## Install", ""]
    if d.install_blocks:
        # Show up to 3 install variants
        seen_lang: set[str] = set()
        n_shown = 0
        for b in d.install_blocks:
            if b.language in seen_lang and b.language:
                continue
            seen_lang.add(b.language)
            fence_lang = b.language or "bash"
            lines.append(f"```{fence_lang}")
            lines.append(b.code)
            lines.append("```")
            lines.append("")
            n_shown += 1
            if n_shown >= 3:
                break
    elif d.install_sections:
        # No code blocks — surface a paragraph from the highest-ranked
        # install section
        sec = d.install_sections[0]
        snippet = re.sub(r"<details>.*?</details>", "", sec.content, flags=re.DOTALL)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rsplit(" ", 1)[0] + "…"
        lines.append(snippet)
        lines.append("")
    else:
        lines.append(f"_No install instructions found in the corpus for "
                     f"`{d.library_name}`. Check the upstream docs._")
        lines.append("")
    return "\n".join(lines)


def _render_hello(d: _Decomposition) -> str:
    lines = ["## Hello World", ""]
    block = _pick_primary_block(d.hello_blocks, d.language_hint)
    if block is None:
        # Fall back to extracting any code block from install sections —
        # often the first install example is also a hello-world
        block = _pick_primary_block(d.install_blocks, d.language_hint)
    if block:
        fence_lang = block.language or (d.language_hint or "")
        lines.append(f"```{fence_lang}")
        lines.append(block.code)
        lines.append("```")
        lines.append("")
    elif d.hello_sections:
        sec = d.hello_sections[0]
        snippet = re.sub(r"<details>.*?</details>", "", sec.content, flags=re.DOTALL)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:800]
        lines.append(snippet)
        lines.append("")
    else:
        lines.append(f"_No hello-world example found in the corpus for "
                     f"`{d.library_name}`. Check the upstream docs._")
        lines.append("")
    return "\n".join(lines)


def _render_verify(d: _Decomposition) -> str:
    lines = ["## Verify it works", ""]
    block = _pick_primary_block(d.verify_blocks, d.language_hint)
    if block:
        fence_lang = block.language or (d.language_hint or "")
        lines.append(f"```{fence_lang}")
        lines.append(block.code)
        lines.append("```")
        lines.append("")
    elif d.verify_sections:
        sec = d.verify_sections[0]
        snippet = re.sub(r"<details>.*?</details>", "", sec.content, flags=re.DOTALL)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:400]
        lines.append(snippet)
        lines.append("")
    else:
        lines.append(
            f"_If the Hello World above runs without errors, "
            f"`{d.library_name}` is installed and working._"
        )
        lines.append("")
    return "\n".join(lines)


def _render_quickstart(d: _Decomposition) -> str:
    lines = [f"# {d.library_name} — Quickstart", ""]
    lines.append("_First-contact artifact. Install, run something minimal, "
                 "verify, then move on to deeper material._")
    lines.append("")

    lines.append("## What it is")
    lines.append("")
    if d.framing_text:
        src_kind = "book chapter" if d.framing_source == "chapter" else "doc section"
        lines.append(d.framing_text)
        lines.append("")
        lines.append(f"_(framing from {src_kind})_")
    else:
        lines.append("_(no framing found in corpus; "
                     f"`{d.doc_source_name}` doc_source covered)_")
    lines.append("")

    lines.append(_render_install(d))
    lines.append(_render_hello(d))
    lines.append(_render_verify(d))

    lines.append("## Next steps")
    lines.append("")
    lines.append(f"- `/kb-cheatsheet {d.library_name}` — one-page reference")
    lines.append(f"- `/kb-tutorial {d.library_name}` — sequenced exercise track")
    lines.append(f"- `/kb-landscape <domain>` — orient against alternatives")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class QuickstartPlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg = package_name or f"quickstart-{_slugify(d.library_name)}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg,
            domain=d.library_name,
            source_query=d.library_name,
            package_metadata={
                "doc_source_id": d.doc_source_id,
                "doc_source_name": d.doc_source_name,
                "n_install_blocks": len(d.install_blocks),
                "n_hello_blocks": len(d.hello_blocks),
                "n_verify_blocks": len(d.verify_blocks),
                "has_framing": d.framing_text is not None,
            },
            notes=list(d.notes),
        )

        # Units
        sources_framing: list[tuple[str, int, float, float, Optional[str]]] = []
        if d.framing_source and d.framing_source_id is not None:
            sources_framing.append(
                (d.framing_source, d.framing_source_id, 1.0, 1.0, None)
            )
        plan.units.append(GenUnit(
            unit_type="whatitis",
            name=f"What is {d.library_name}",
            ordinal=1,
            logical_key="whatitis",
            content_markdown=d.framing_text or "",
            sources=sources_framing,
        ))

        plan.units.append(GenUnit(
            unit_type="install",
            name="Install",
            ordinal=2,
            logical_key="install",
            metadata={"n_blocks": len(d.install_blocks)},
            sources=[
                ("doc_section", s.doc_section_id, s.rank_score, 1.0, None)
                for s in d.install_sections
            ],
        ))

        plan.units.append(GenUnit(
            unit_type="hello_world",
            name="Hello World",
            ordinal=3,
            logical_key="hello_world",
            metadata={"n_blocks": len(d.hello_blocks)},
            sources=[
                ("doc_section", s.doc_section_id, s.rank_score, 1.0, None)
                for s in d.hello_sections
            ],
        ))

        plan.units.append(GenUnit(
            unit_type="verify",
            name="Verify",
            ordinal=4,
            logical_key="verify",
            metadata={"n_blocks": len(d.verify_blocks)},
            sources=[
                ("doc_section", s.doc_section_id, s.rank_score, 1.0, None)
                for s in d.verify_sections
            ],
        ))

        plan.units.append(GenUnit(
            unit_type="next_steps",
            name="Next steps",
            ordinal=5,
            logical_key="next_steps",
        ))

        # The main artifact
        plan.files.append(GenFile(
            filename="_quickstart.md",
            content=_render_quickstart(d),
            purpose="quickstart",
        ))

        # If we have a hello-world code block, also write it as a runnable file
        hello_block = _pick_primary_block(
            d.hello_blocks or d.install_blocks, d.language_hint,
        )
        if hello_block:
            ext = _file_extension(hello_block.language, d.language_hint)
            plan.files.append(GenFile(
                filename=f"hello_world/main{ext}",
                content=hello_block.code + "\n",
                purpose="hello_world",
                unit_logical_key="hello_world",
            ))
        return plan


_LANG_EXTENSIONS = {
    "python": ".py", "py": ".py",
    "rust": ".rs", "rs": ".rs",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "java": ".java",
    "go": ".go",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "csharp": ".cs", "cs": ".cs",
    "ruby": ".rb",
    "php": ".php",
    "bash": ".sh", "sh": ".sh", "shell": ".sh",
}


def _file_extension(language: str, hint: Optional[str]) -> str:
    lang = (language or "").lower()
    if lang in _LANG_EXTENSIONS:
        return _LANG_EXTENSIONS[lang]
    if hint:
        return _LANG_EXTENSIONS.get(hint.lower(), ".txt")
    return ".txt"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class QuickstartValidator:
    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        meta = plan.package_metadata or {}
        if meta.get("doc_source_id", -1) < 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="library not found in doc_source registry — "
                        "run /kb-discover first",
            ))
            return issues
        if not meta.get("n_install_blocks") and not meta.get("n_hello_blocks"):
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no code blocks extracted for install OR hello-world; "
                        "the artifact will surface section text only",
            ))
        if not meta.get("has_framing"):
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no framing paragraph found; 'What it is' will be empty",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class QuickstartMaterializer:
    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
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
        skipped: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                skipped.append(str(target))
                continue
            target.write_text(content)
            written.append(str(target))
        notes: list[str] = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files (overwrite=False)")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_quickstart_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=QuickstartDecomposer(),
        planner=QuickstartPlanner(),
        ranking_mode="generation",
        validator=QuickstartValidator(),
        materializer=QuickstartMaterializer(),
    )
