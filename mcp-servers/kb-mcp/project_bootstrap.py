"""project_bootstrap.py — Phase 15 Project Bootstrap (deterministic v1).

The user's stated #1 generator. Composes Concept→Pattern→Procedure
into a runnable project scaffold reconciled with current doc snapshots.

The architecture spec calls for sub-agent-driven prose generation of
each project file. v1 ships the deterministic skeleton:

  1. Detect (or accept as a parameter) the **target language stack**
     — python / rust / node / typescript / java / go / csharp / ruby
     — and pick the right scaffolding template. Falls back to a
     minimal "generic" stack (README only) when no signal exists.
  2. Resolve named technologies + named patterns from the request
  3. Pull procedures + doc_sections relevant to each
  4. Render a project tree appropriate for the detected stack, with
     placeholder files carrying the accumulated context (procedures
     + pattern descriptions + doc excerpts)
  5. Write per-file sub-agent prompts to _sub_agent_prompts/ that a
     future v2 can dispatch to generate actual code

The skeleton is structural — every file expected by the chosen stack
appears, with substantial context for the sub-agent (or human) to
fill in. The user can either run the sub-agent prompts manually via
the Task tool or hand-edit each placeholder.

Output (paths shown for python stack; others are language-appropriate):
    bootstraps/<project-name>/
      README.md                project overview
      _build_plan.md           file-by-file build plan with metrics
      _sub_agent_prompts/      one prompt per planned file
      <manifest>               pyproject.toml | Cargo.toml | package.json | ...
      <entry-point>            src/main.py | src/main.rs | main.go | ...
      <test-stub>              tests/test_smoke.py | tests/integration_test.rs | ...
      .gitignore               stack-appropriate
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

LOG = logging.getLogger("mypub-project-bootstrap")

GENERATOR_TYPE = "project_bootstrap"


@dataclass
class _StackElement:
    concept_id: int
    name: str
    concept_type: str
    description: Optional[str]
    role: str                    # "pattern" | "tool" | "framework" | "technique"
    procedure_ids: list[int] = field(default_factory=list)
    chapter_ids: list[int] = field(default_factory=list)
    doc_section_ids: list[int] = field(default_factory=list)


@dataclass
class _PlannedFile:
    relative_path: str           # e.g. "src/handlers/command_handler.py"
    purpose: str                 # short description
    placeholder_content: str     # what gets written to disk
    prompt: str                  # sub-agent prompt content


@dataclass
class _Decomposition:
    project_name: str
    description: str
    stack: "_Stack"                          # forward ref; defined below
    elements: list[_StackElement]
    planned_files: list[_PlannedFile]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stack templates — one per supported language/ecosystem
# ---------------------------------------------------------------------------


@dataclass
class _Stack:
    """One language/ecosystem scaffolding template.

    ``files`` is the ordered list of (relative_path, purpose) tuples
    the planner emits. ``required_files`` is the subset the validator
    insists on — typically README + entry point + smoke test.
    """

    name: str                              # 'python', 'rust', ...
    description: str                       # human-readable
    files: list[tuple[str, str]]
    required_files: list[str]


# Each stack has its own canonical file set. The ``generic`` stack is
# the fallback when no language signal is detected — it ships only a
# README so callers get a useful skeleton without any
# language-specific assumptions.
_STACKS: dict[str, _Stack] = {
    "python": _Stack(
        name="python",
        description="Python project (pip + pytest)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("pyproject.toml", "Python package manifest"),
            ("src/__init__.py", "package marker"),
            ("src/main.py", "application entry point"),
            ("tests/__init__.py", "test package marker"),
            ("tests/test_smoke.py", "smoke tests verifying scaffold runs"),
            (".gitignore", "ignore Python build + venv artifacts"),
        ],
        required_files=["README.md", "src/main.py", "tests/test_smoke.py"],
    ),
    "rust": _Stack(
        name="rust",
        description="Rust project (cargo + integration tests)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("Cargo.toml", "Cargo manifest"),
            ("src/main.rs", "application entry point"),
            ("src/lib.rs", "library root for shared code"),
            ("tests/integration_test.rs", "integration test stub"),
            (".gitignore", "ignore target/ and IDE artifacts"),
        ],
        required_files=["README.md", "Cargo.toml", "src/main.rs",
                        "tests/integration_test.rs"],
    ),
    "node": _Stack(
        name="node",
        description="Node.js project (npm + plain JavaScript)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("package.json", "npm manifest"),
            ("src/index.js", "application entry point"),
            ("test/smoke.test.js", "smoke tests"),
            (".gitignore", "ignore node_modules and dist"),
        ],
        required_files=["README.md", "package.json", "src/index.js",
                        "test/smoke.test.js"],
    ),
    "typescript": _Stack(
        name="typescript",
        description="TypeScript project (npm + tsc + plain test runner)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("package.json", "npm manifest"),
            ("tsconfig.json", "TypeScript compiler config"),
            ("src/index.ts", "application entry point"),
            ("test/smoke.test.ts", "smoke tests"),
            (".gitignore", "ignore node_modules and dist"),
        ],
        required_files=["README.md", "package.json", "tsconfig.json",
                        "src/index.ts", "test/smoke.test.ts"],
    ),
    "java": _Stack(
        name="java",
        description="Java project (Maven layout)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("pom.xml", "Maven manifest"),
            ("src/main/java/com/example/Main.java", "application entry point"),
            ("src/test/java/com/example/SmokeTest.java", "smoke tests"),
            (".gitignore", "ignore target/ and IDE artifacts"),
        ],
        required_files=["README.md", "pom.xml",
                        "src/main/java/com/example/Main.java",
                        "src/test/java/com/example/SmokeTest.java"],
    ),
    "go": _Stack(
        name="go",
        description="Go module project",
        files=[
            ("README.md", "project overview + quickstart"),
            ("go.mod", "Go module manifest"),
            ("main.go", "application entry point"),
            ("main_test.go", "smoke test (same-package convention)"),
            (".gitignore", "ignore vendor/ and binaries"),
        ],
        required_files=["README.md", "go.mod", "main.go", "main_test.go"],
    ),
    "csharp": _Stack(
        name="csharp",
        description=".NET project (csproj + xUnit)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("Project.csproj", "csproj manifest"),
            ("Program.cs", "application entry point"),
            ("Tests/SmokeTest.cs", "smoke tests (xUnit)"),
            (".gitignore", "ignore bin/, obj/, IDE artifacts"),
        ],
        required_files=["README.md", "Project.csproj", "Program.cs",
                        "Tests/SmokeTest.cs"],
    ),
    "ruby": _Stack(
        name="ruby",
        description="Ruby project (Bundler + RSpec)",
        files=[
            ("README.md", "project overview + quickstart"),
            ("Gemfile", "Bundler manifest"),
            ("lib/main.rb", "application entry point"),
            ("spec/smoke_spec.rb", "smoke spec"),
            (".gitignore", "ignore bundler + coverage artifacts"),
        ],
        required_files=["README.md", "Gemfile", "lib/main.rb",
                        "spec/smoke_spec.rb"],
    ),
    "generic": _Stack(
        name="generic",
        description="Language-agnostic skeleton (README only)",
        files=[
            ("README.md", "project overview + quickstart"),
            (".gitignore", "minimal ignore patterns"),
        ],
        required_files=["README.md"],
    ),
}


# Keyword → canonical stack name. First match wins; order matters
# (more-specific frameworks beat language-generic terms). All
# matching is on lowercased substrings of the request.
_STACK_KEYWORDS: list[tuple[str, str]] = [
    # Python
    ("python", "python"), ("django", "python"), ("flask", "python"),
    ("fastapi", "python"), ("pydantic", "python"), ("pip ", "python"),
    ("pyproject", "python"), ("pytest", "python"),
    # Rust
    ("rust", "rust"), ("cargo", "rust"), ("tokio", "rust"),
    ("axum", "rust"), ("actix", "rust"), ("serde", "rust"),
    # TypeScript (before node so it wins for "TS + Node" requests)
    ("typescript", "typescript"), ("tsconfig", "typescript"),
    # Node / JavaScript
    ("node.js", "node"), ("nodejs", "node"), ("node ", "node"),
    ("express", "node"), ("javascript", "node"),
    ("react", "node"), ("vue", "node"), ("next.js", "node"),
    # Java
    ("java ", "java"), ("spring", "java"), ("spring boot", "java"),
    ("maven", "java"), ("gradle", "java"), ("kotlin", "java"),
    # Go
    ("golang", "go"), (" go ", "go"), ("go modules", "go"),
    # C# / .NET
    ("c#", "csharp"), ("csharp", "csharp"), (".net", "csharp"),
    ("dotnet", "csharp"), ("aspnet", "csharp"), ("asp.net", "csharp"),
    # Ruby
    ("ruby", "ruby"), ("rails", "ruby"), ("bundler", "ruby"),
]


# ---------------------------------------------------------------------------
# Per-stack placeholder content
# ---------------------------------------------------------------------------
# Each renderer is keyed by (stack_name, relative_path). README.md and
# .gitignore are handled separately. Renderers receive
# (project_name, purpose, ctx_summary) and return the file body.


def _py_manifest(project_name: str, _purpose: str, _ctx: str) -> str:
    return (
        f"[project]\nname = \"{_slugify(project_name)}\"\n"
        f"version = \"0.1.0\"\nrequires-python = \">=3.10\"\n"
        f"dependencies = [\n  # TODO: populate from stack context\n]\n\n"
        f"[build-system]\nrequires = [\"hatchling\"]\n"
        f"build-backend = \"hatchling.build\"\n"
    )


def _py_main(_pn: str, _p: str, _c: str) -> str:
    return (
        "\"\"\"Entry point — sub-agent fills this in.\"\"\"\n\n\n"
        "def main() -> None:\n"
        "    raise NotImplementedError(\n"
        "        \"Fill in from _sub_agent_prompts/.\"\n"
        "    )\n\n\n"
        "if __name__ == '__main__':\n    main()\n"
    )


def _py_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "\"\"\"Smoke test placeholder — sub-agent fills this in.\"\"\"\n\n\n"
        "def test_placeholder() -> None:\n"
        "    assert True, \"replace with real assertions\"\n"
    )


def _rs_cargo(project_name: str, _p: str, _c: str) -> str:
    return (
        f"[package]\nname = \"{_slugify(project_name)}\"\n"
        f"version = \"0.1.0\"\nedition = \"2021\"\n\n"
        f"[dependencies]\n# TODO: populate from stack context\n"
    )


def _rs_main(_pn: str, _p: str, _c: str) -> str:
    return (
        "// Entry point — sub-agent fills this in.\n\n"
        "fn main() {\n"
        "    todo!(\"Fill in from _sub_agent_prompts/.\");\n"
        "}\n"
    )


def _rs_lib(_pn: str, _p: str, _c: str) -> str:
    return (
        "//! Library root for shared code.\n//! Sub-agent populates "
        "modules + public API here.\n"
    )


def _rs_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "//! Integration test placeholder.\n\n"
        "#[test]\nfn smoke() {\n"
        "    assert!(true, \"replace with real assertions\");\n}\n"
    )


def _node_pkg(project_name: str, _p: str, _c: str) -> str:
    return (
        f"{{\n  \"name\": \"{_slugify(project_name)}\",\n"
        f"  \"version\": \"0.1.0\",\n"
        f"  \"type\": \"module\",\n"
        f"  \"main\": \"src/index.js\",\n"
        f"  \"scripts\": {{\n"
        f"    \"start\": \"node src/index.js\",\n"
        f"    \"test\": \"node --test test/\"\n"
        f"  }},\n"
        f"  \"dependencies\": {{}}\n}}\n"
    )


def _node_index(_pn: str, _p: str, _c: str) -> str:
    return (
        "// Entry point — sub-agent fills this in.\n\n"
        "function main() {\n"
        "  throw new Error(\"Fill in from _sub_agent_prompts/.\");\n"
        "}\n\nmain();\n"
    )


def _node_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "import { test } from 'node:test';\n"
        "import assert from 'node:assert';\n\n"
        "test('smoke', () => {\n"
        "  assert.ok(true, 'replace with real assertions');\n"
        "});\n"
    )


def _ts_pkg(project_name: str, _p: str, _c: str) -> str:
    return (
        f"{{\n  \"name\": \"{_slugify(project_name)}\",\n"
        f"  \"version\": \"0.1.0\",\n"
        f"  \"type\": \"module\",\n"
        f"  \"main\": \"dist/index.js\",\n"
        f"  \"scripts\": {{\n"
        f"    \"build\": \"tsc\",\n"
        f"    \"start\": \"node dist/index.js\"\n"
        f"  }},\n"
        f"  \"devDependencies\": {{\n"
        f"    \"typescript\": \"^5.0.0\",\n"
        f"    \"@types/node\": \"^20.0.0\"\n  }}\n}}\n"
    )


def _ts_tsconfig(_pn: str, _p: str, _c: str) -> str:
    return (
        "{\n  \"compilerOptions\": {\n"
        "    \"target\": \"ES2022\",\n"
        "    \"module\": \"ES2022\",\n"
        "    \"moduleResolution\": \"node\",\n"
        "    \"outDir\": \"./dist\",\n"
        "    \"strict\": true,\n"
        "    \"esModuleInterop\": true\n  },\n"
        "  \"include\": [\"src/**/*\"]\n}\n"
    )


def _ts_index(_pn: str, _p: str, _c: str) -> str:
    return (
        "// Entry point — sub-agent fills this in.\n\n"
        "function main(): void {\n"
        "  throw new Error(\"Fill in from _sub_agent_prompts/.\");\n"
        "}\n\nmain();\n"
    )


def _ts_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "import { test } from 'node:test';\n"
        "import assert from 'node:assert';\n\n"
        "test('smoke', () => {\n"
        "  assert.ok(true, 'replace with real assertions');\n"
        "});\n"
    )


def _java_pom(project_name: str, _p: str, _c: str) -> str:
    art = _slugify(project_name)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>com.example</groupId>\n"
        f"  <artifactId>{art}</artifactId>\n"
        "  <version>0.1.0-SNAPSHOT</version>\n"
        "  <properties>\n"
        "    <maven.compiler.source>17</maven.compiler.source>\n"
        "    <maven.compiler.target>17</maven.compiler.target>\n"
        "  </properties>\n"
        "  <dependencies>\n"
        "    <!-- TODO: populate from stack context -->\n"
        "    <dependency>\n"
        "      <groupId>org.junit.jupiter</groupId>\n"
        "      <artifactId>junit-jupiter</artifactId>\n"
        "      <version>5.10.0</version>\n"
        "      <scope>test</scope>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )


def _java_main(_pn: str, _p: str, _c: str) -> str:
    return (
        "package com.example;\n\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        throw new UnsupportedOperationException("
        "\"Fill in from _sub_agent_prompts/.\");\n"
        "    }\n}\n"
    )


def _java_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "package com.example;\n\n"
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertTrue;\n\n"
        "class SmokeTest {\n"
        "    @Test void smoke() {\n"
        "        assertTrue(true, \"replace with real assertions\");\n"
        "    }\n}\n"
    )


def _go_mod(project_name: str, _p: str, _c: str) -> str:
    return f"module example.com/{_slugify(project_name)}\n\ngo 1.22\n"


def _go_main(_pn: str, _p: str, _c: str) -> str:
    return (
        "package main\n\n"
        "import \"fmt\"\n\n"
        "func main() {\n"
        "    panic(\"Fill in from _sub_agent_prompts/.\")\n"
        "    _ = fmt.Println\n}\n"
    )


def _go_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "package main\n\nimport \"testing\"\n\n"
        "func TestSmoke(t *testing.T) {\n"
        "    if false {\n        t.Fatal(\"replace with real assertions\")\n"
        "    }\n}\n"
    )


def _cs_proj(project_name: str, _p: str, _c: str) -> str:
    return (
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
        "  <PropertyGroup>\n"
        "    <OutputType>Exe</OutputType>\n"
        "    <TargetFramework>net8.0</TargetFramework>\n"
        f"    <RootNamespace>{_slugify(project_name).replace('-','_')}</RootNamespace>\n"
        "  </PropertyGroup>\n"
        "</Project>\n"
    )


def _cs_program(_pn: str, _p: str, _c: str) -> str:
    return (
        "// Entry point — sub-agent fills this in.\n\n"
        "throw new System.NotImplementedException(\n"
        "    \"Fill in from _sub_agent_prompts/.\");\n"
    )


def _cs_test(_pn: str, _p: str, _c: str) -> str:
    return (
        "using Xunit;\n\n"
        "public class SmokeTest {\n"
        "    [Fact] public void Smoke() {\n"
        "        Assert.True(true, \"replace with real assertions\");\n"
        "    }\n}\n"
    )


def _ruby_gemfile(_pn: str, _p: str, _c: str) -> str:
    return (
        "source 'https://rubygems.org'\n\n"
        "gem 'rspec', '~> 3.13', group: :test\n"
        "# TODO: populate runtime gems from stack context\n"
    )


def _ruby_main(_pn: str, _p: str, _c: str) -> str:
    return (
        "# Entry point — sub-agent fills this in.\n\n"
        "module Main\n"
        "  def self.run\n"
        "    raise NotImplementedError, "
        "'Fill in from _sub_agent_prompts/.'\n"
        "  end\nend\n\n"
        "Main.run if __FILE__ == $PROGRAM_NAME\n"
    )


def _ruby_spec(_pn: str, _p: str, _c: str) -> str:
    return (
        "RSpec.describe 'smoke' do\n"
        "  it 'passes' do\n"
        "    expect(true).to be(true)  # replace with real assertions\n"
        "  end\nend\n"
    )


_PLACEHOLDER_RENDERERS: dict[tuple[str, str], Any] = {
    ("python", "pyproject.toml"):              _py_manifest,
    ("python", "src/main.py"):                 _py_main,
    ("python", "tests/test_smoke.py"):         _py_test,
    ("rust",   "Cargo.toml"):                  _rs_cargo,
    ("rust",   "src/main.rs"):                 _rs_main,
    ("rust",   "src/lib.rs"):                  _rs_lib,
    ("rust",   "tests/integration_test.rs"):   _rs_test,
    ("node",   "package.json"):                _node_pkg,
    ("node",   "src/index.js"):                _node_index,
    ("node",   "test/smoke.test.js"):          _node_test,
    ("typescript", "package.json"):            _ts_pkg,
    ("typescript", "tsconfig.json"):           _ts_tsconfig,
    ("typescript", "src/index.ts"):            _ts_index,
    ("typescript", "test/smoke.test.ts"):      _ts_test,
    ("java",   "pom.xml"):                                     _java_pom,
    ("java",   "src/main/java/com/example/Main.java"):         _java_main,
    ("java",   "src/test/java/com/example/SmokeTest.java"):    _java_test,
    ("go",     "go.mod"):                      _go_mod,
    ("go",     "main.go"):                     _go_main,
    ("go",     "main_test.go"):                _go_test,
    ("csharp", "Project.csproj"):              _cs_proj,
    ("csharp", "Program.cs"):                  _cs_program,
    ("csharp", "Tests/SmokeTest.cs"):          _cs_test,
    ("ruby",   "Gemfile"):                     _ruby_gemfile,
    ("ruby",   "lib/main.rb"):                 _ruby_main,
    ("ruby",   "spec/smoke_spec.rb"):          _ruby_spec,
}


_GITIGNORE_GENERIC = "# IDE\n.vscode/\n.idea/\n*.swp\n# OS\n.DS_Store\n"

_GITIGNORE_BY_STACK = {
    "python":     "__pycache__/\n*.pyc\n.venv/\n.env\nbuild/\ndist/\n*.egg-info/\n"
                  + _GITIGNORE_GENERIC,
    "rust":       "target/\nCargo.lock  # uncomment for libraries\n" + _GITIGNORE_GENERIC,
    "node":       "node_modules/\ndist/\n.env\nnpm-debug.log\n" + _GITIGNORE_GENERIC,
    "typescript": "node_modules/\ndist/\n.env\nnpm-debug.log\n*.tsbuildinfo\n"
                  + _GITIGNORE_GENERIC,
    "java":       "target/\n*.class\n*.jar\n*.war\n" + _GITIGNORE_GENERIC,
    "go":         "/vendor/\n*.exe\n*.test\n*.out\n" + _GITIGNORE_GENERIC,
    "csharp":     "bin/\nobj/\n*.user\n*.suo\n" + _GITIGNORE_GENERIC,
    "ruby":       ".bundle/\nvendor/bundle/\ncoverage/\n*.gem\n" + _GITIGNORE_GENERIC,
    "generic":    _GITIGNORE_GENERIC,
}


def _detect_stack(query: str, stack_hint: Optional[str]) -> _Stack:
    """Pick a stack template. Explicit hint > keyword match > generic.

    Returns the ``generic`` stack when nothing in the request points
    at a known language ecosystem — better an honest minimal skeleton
    than a confidently-wrong Python-by-default scaffold for a Rust
    project.
    """
    if stack_hint:
        key = stack_hint.lower().strip()
        # Common aliases
        alias = {"py": "python", "rs": "rust", "js": "node",
                 "ts": "typescript", "nodejs": "node", "node.js": "node",
                 "kotlin": "java", "dotnet": "csharp", "cs": "csharp",
                 "golang": "go"}
        key = alias.get(key, key)
        if key in _STACKS:
            return _STACKS[key]
    q = (query or "").lower()
    # Pad with spaces so word-boundary patterns like " go " catch
    # "for Go service" without misfiring on "google" / "argo" / etc.
    padded = f" {q} "
    for kw, stack_name in _STACK_KEYWORDS:
        if kw in padded:
            return _STACKS[stack_name]
    return _STACKS["generic"]


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return "".join(c for c in s if c in keep).strip("-") or "project"


def _short(text: str, limit: int = 300) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


class StackComposeDecomposer:
    """Resolve named technologies + patterns from the request and
    pull related procedures + doc_sections.
    """

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        technologies: Optional[list[str]] = None,
        patterns: Optional[list[str]] = None,
        project_name: Optional[str] = None,
        stack: Optional[str] = None,
        **_: Any,
    ) -> _Decomposition:
        techs = technologies or []
        pats = patterns or []
        all_named = list(techs) + list(pats)
        if not all_named:
            # Try to glean names from the description: any capitalized
            # word that resolves to a concept.
            tokens = re.findall(r"[A-Z][A-Za-z+#0-9.-]{2,}", query or "")
            all_named = list(set(tokens))

        elements: list[_StackElement] = []
        notes: list[str] = []
        for name in all_named:
            cid = resolver.resolve_lookup_only(name)
            if cid is None:
                notes.append(f"named element {name!r} not found; skipped")
                continue
            row = conn.execute(
                "SELECT name, concept_type, description FROM concept "
                "WHERE concept_id = ?", [cid],
            ).fetchone()
            ctype = row[1] or "Concept"
            role = ctype.lower() if ctype else "concept"

            # Procedures linked to this concept
            proc_ids = [
                int(r[0]) for r in conn.execute(
                    "SELECT procedure_id FROM procedure_concept "
                    "WHERE concept_id = ? LIMIT 5", [cid],
                ).fetchall()
            ]
            # Top chapters
            chap_ids = [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT cr.source_id FROM concept_relation cr
                     WHERE cr.source_type = 'chapter'
                       AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                     GROUP BY cr.source_id
                     ORDER BY COUNT(*) DESC LIMIT 3
                    """, [cid, cid],
                ).fetchall()
            ]
            # Top doc_sections
            sec_ids = [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT cr.source_id FROM concept_relation cr
                     WHERE cr.source_type = 'doc_section'
                       AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                     GROUP BY cr.source_id
                     ORDER BY COUNT(*) DESC LIMIT 3
                    """, [cid, cid],
                ).fetchall()
            ]
            elements.append(_StackElement(
                concept_id=cid, name=row[0], concept_type=ctype,
                description=row[2], role=role,
                procedure_ids=proc_ids,
                chapter_ids=chap_ids,
                doc_section_ids=sec_ids,
            ))

        # Detect (or honor explicit) target stack
        chosen_stack = _detect_stack(query, stack)
        if stack is None and chosen_stack.name == "generic":
            notes.append(
                "no language signal found in request; emitting a minimal "
                "generic skeleton. Pass `stack=python|rust|node|typescript|"
                "java|go|csharp|ruby` to force a specific scaffolding."
            )
        elif stack is None:
            notes.append(
                f"stack inferred from request: {chosen_stack.name} "
                f"({chosen_stack.description}). Override with the `stack` "
                "parameter if this is wrong."
            )

        # Plan files
        planned: list[_PlannedFile] = []
        ctx_summary = self._render_context_summary(conn, elements)
        for rel_path, purpose in chosen_stack.files:
            placeholder = self._render_placeholder(
                rel_path, purpose, project_name or "project",
                ctx_summary, chosen_stack,
            )
            prompt = self._render_subagent_prompt(
                rel_path, purpose, query, elements, ctx_summary,
            )
            planned.append(_PlannedFile(
                relative_path=rel_path, purpose=purpose,
                placeholder_content=placeholder, prompt=prompt,
            ))

        if not elements:
            notes.append(
                "no named technologies or patterns resolved; stack "
                "structure is present but file content is minimal. "
                "Try naming specific concepts in the request."
            )

        return _Decomposition(
            project_name=project_name or _slugify(query)[:50] or "project",
            description=query,
            stack=chosen_stack,
            elements=elements,
            planned_files=planned,
            notes=notes,
        )

    @staticmethod
    def _render_context_summary(
        conn: duckdb.DuckDBPyConnection,
        elements: list[_StackElement],
    ) -> str:
        if not elements:
            return "_(no named stack elements; sub-agent will scaffold a generic project)_"
        lines = []
        for el in elements:
            lines.append(f"- **{el.name}** ({el.concept_type}): "
                         f"{_short(el.description or 'no description', 160)}")
            if el.procedure_ids:
                lines.append(f"  Procedures: {len(el.procedure_ids)} available")
            if el.doc_section_ids:
                lines.append(f"  Doc sections: {len(el.doc_section_ids)} relevant")
        return "\n".join(lines)

    @staticmethod
    def _render_placeholder(
        rel_path: str, purpose: str, project_name: str,
        ctx_summary: str, stack: "_Stack",
    ) -> str:
        """Render the placeholder body for one planned file.

        README.md is universal; everything else dispatches by stack
        name. Any path the stack defines but this method doesn't have
        a template for falls through to a generic comment header.
        """
        if rel_path == "README.md":
            return (
                f"# {project_name}\n\n"
                f"_{purpose}_\n\n"
                f"## Stack\n\n"
                f"**{stack.name}** — {stack.description}\n\n"
                f"## Stack context (from corpus)\n\n"
                f"{ctx_summary}\n\n"
                f"## Quickstart\n\n"
                f"_(TODO — fill in once the entry point is implemented; "
                f"see `_sub_agent_prompts/` for per-file prompts.)_\n"
            )
        if rel_path == ".gitignore":
            return _GITIGNORE_BY_STACK.get(stack.name, _GITIGNORE_GENERIC)
        render = _PLACEHOLDER_RENDERERS.get((stack.name, rel_path))
        if render is not None:
            return render(project_name, purpose, ctx_summary)
        # Generic empty-file convention: empty __init__.py, empty marker files
        if rel_path.endswith("__init__.py"):
            return ""
        return (
            f"# {rel_path}\n# {purpose}\n\n"
            f"# TODO: sub-agent fills this in. See "
            f"`_sub_agent_prompts/` for the prompt.\n"
        )

    @staticmethod
    def _render_subagent_prompt(
        rel_path: str, purpose: str, request: str,
        elements: list[_StackElement], ctx_summary: str,
    ) -> str:
        return (
            f"Generate the file at `{rel_path}` for the following project request:\n\n"
            f"{request}\n\n"
            f"Purpose of this file: {purpose}\n\n"
            f"Stack context (resolved from the knowledge base):\n\n"
            f"{ctx_summary}\n\n"
            f"Constraints:\n"
            f"- Use the procedures and patterns named in the stack context\n"
            f"- Match current vendor doc semantics (the corpus tracks recent docs)\n"
            f"- Keep the file self-contained; cross-references go in the README\n"
            f"- Do NOT hallucinate APIs that aren't in the corpus\n"
            f"\nWrite ONLY the file content — no prose explanation, no fences.\n"
        )


def _render_build_plan(d: _Decomposition) -> str:
    lines = [
        f"# {d.project_name} — Build Plan",
        "",
        f"**Request:** {d.description}",
        "",
        f"**Target stack:** `{d.stack.name}` — {d.stack.description}",
        "",
        f"**Stack elements resolved:** {len(d.elements)}",
        "",
    ]
    if d.elements:
        lines.append("## Stack")
        lines.append("")
        for el in d.elements:
            lines.append(f"- **{el.name}** ({el.concept_type}, {el.role}) — "
                         f"{len(el.procedure_ids)} proc(s), "
                         f"{len(el.chapter_ids)} chapter(s), "
                         f"{len(el.doc_section_ids)} doc section(s)")
        lines.append("")
    lines.append("## Files planned")
    lines.append("")
    for pf in d.planned_files:
        lines.append(f"- `{pf.relative_path}` — {pf.purpose}")
    lines.append("")
    lines.append("## Sub-agent prompts")
    lines.append("")
    lines.append(
        "Each planned file has a corresponding prompt in "
        "`_sub_agent_prompts/`. Dispatch them via the Task tool to fill "
        "in the placeholders with real implementation."
    )
    if d.notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in d.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


class ProjectBootstrapPlanner:
    def plan(
        self, conn, decomposition, *, package_name=None, **_,
    ) -> GenPlan:
        d = decomposition
        pkg_name = package_name or _slugify(d.project_name)
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg_name,
            domain=d.description,
            source_query=d.description,
            package_metadata={
                "n_elements": len(d.elements),
                "n_files": len(d.planned_files),
                "element_concept_ids": [el.concept_id for el in d.elements],
                "stack": d.stack.name,
                "stack_description": d.stack.description,
                "required_files": list(d.stack.required_files),
            },
            notes=list(d.notes),
        )
        for i, pf in enumerate(d.planned_files, start=1):
            plan.units.append(GenUnit(
                unit_type="project_file",
                name=pf.relative_path,
                ordinal=i,
                metadata={
                    "purpose": pf.purpose,
                    "relative_path": pf.relative_path,
                },
                logical_key=f"file_{i}",
                content_markdown=pf.purpose,
                sources=[],
            ))
            # Placeholder content
            plan.files.append(GenFile(
                filename=pf.relative_path,
                content=pf.placeholder_content,
                purpose="placeholder",
            ))
            # Sub-agent prompt
            prompt_name = (
                f"_sub_agent_prompts/prompt_{i:02d}_"
                + _slugify(pf.relative_path) + ".txt"
            )
            plan.files.append(GenFile(
                filename=prompt_name,
                content=pf.prompt,
                purpose="subagent_prompt",
            ))
        plan.files.append(GenFile(
            filename="_build_plan.md",
            content=_render_build_plan(d),
            purpose="build_plan",
        ))
        return plan


class ProjectBootstrapValidator:
    def validate(self, conn, plan) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if plan.package_metadata.get("n_elements", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="no stack elements resolved; scaffold is generic",
            ))
        if plan.package_metadata.get("n_files", 0) == 0:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no files planned",
            ))
            return issues
        # Required structural files — read from the chosen stack's
        # required_files list (set by the decomposer/planner). Falls
        # back to README.md when the metadata is missing so older
        # packages still validate.
        names = {u.metadata.get("relative_path") for u in plan.units}
        required = plan.package_metadata.get("required_files") or ["README.md"]
        for req in required:
            if req not in names:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"missing required file in plan: {req}",
                ))
        # FK existence on element concept_ids
        ids = set(plan.package_metadata.get("element_concept_ids", []))
        if ids:
            ph = ",".join(["?"] * len(ids))
            existing = {int(r[0]) for r in conn.execute(
                f"SELECT concept_id FROM concept WHERE concept_id IN ({ph})",
                list(ids),
            ).fetchall()}
            missing = ids - existing
            if missing:
                issues.append(ValidationIssue(
                    unit_logical_key="", severity="error",
                    message=f"element concept_ids missing: {sorted(missing)[:5]}",
                ))
        return issues


class ProjectBootstrapMaterializer:
    def materialize(self, conn, package_id, output_root, *, overwrite=True):
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?", [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = conn.execute(
            "SELECT filename, content FROM generated_file "
            "WHERE package_id = ? ORDER BY file_id", [package_id],
        ).fetchall()
        written: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                continue
            target.write_text(content)
            written.append(str(target))
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=[],
        )


def make_project_bootstrap_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=StackComposeDecomposer(),
        planner=ProjectBootstrapPlanner(),
        ranking_mode="generation",
        validator=ProjectBootstrapValidator(),
        materializer=ProjectBootstrapMaterializer(),
    )
