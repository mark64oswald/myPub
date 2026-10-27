"""Tests for bootstrap_runtime.py — Project Bootstrap v2 loop + validation."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import bootstrap_runtime as br  # noqa: E402

PLACEHOLDER_MAIN = (
    'def main():\n    raise NotImplementedError("Fill in from _sub_agent_prompts/.")\n'
    "\n\nif __name__ == '__main__':\n    main()\n"
)


def _python_project(root: Path, main_body: str, test_body: str) -> Path:
    files = {
        "README.md": "# demo\n",
        "pyproject.toml": "[project]\nname = 'demo'\nversion = '0.1.0'\n",
        "src/__init__.py": "",
        "src/main.py": main_body,
        "tests/__init__.py": "",
        "tests/test_smoke.py": test_body,
    }
    for i, (rel, body) in enumerate(files.items()):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
        prompt = root / br.PROMPTS_DIR / f"prompt_{i:02d}_{rel.replace('/', '-')}.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(f"Generate the file at `{rel}` for the following project request:\n\nx\n")
    return root


GOOD_MAIN = "def main():\n    print('pipeline ok')\n\n\nif __name__ == '__main__':\n    main()\n"
GOOD_TEST = "from src.main import main\n\n\ndef test_main(capsys):\n    main()\n    assert 'ok' in capsys.readouterr().out\n"


def test_detect_stack_prefers_typescript_over_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert br.detect_stack(tmp_path) == "node"
    (tmp_path / "tsconfig.json").write_text("{}")
    assert br.detect_stack(tmp_path) == "typescript"


def test_manifest_lists_placeholder_and_missing_files(tmp_path):
    _python_project(tmp_path, PLACEHOLDER_MAIN, GOOD_TEST)
    (tmp_path / "README.md").unlink()
    pending = {p.relative_path: p.reason for p in br.dispatch_manifest(tmp_path)}
    assert pending == {"src/main.py": "placeholder", "README.md": "missing"}


def test_validate_python_project_passes_end_to_end(tmp_path):
    pytest.importorskip("pytest")
    _python_project(tmp_path, GOOD_MAIN, GOOD_TEST)
    (tmp_path / br.SMOKE_FILE).write_text(json.dumps({"expect_stdout": "pipeline ok"}))
    report = br.validate_project(tmp_path)
    assert report.stack == "python"
    assert [(s.name, s.status) for s in report.steps] == [
        ("placeholders", "pass"), ("test", "pass"), ("smoke", "pass")]
    assert report.passed
    assert (tmp_path / br.VALIDATION_JSON).exists()
    assert br.write_retry_prompts(tmp_path, report) == []


def test_validate_fails_on_placeholder_and_writes_retry_prompt(tmp_path):
    _python_project(tmp_path, PLACEHOLDER_MAIN, GOOD_TEST)
    report = br.validate_project(tmp_path)
    assert not report.passed
    by_name = {s.name: s for s in report.steps}
    assert by_name["placeholders"].files == ["src/main.py"]
    assert by_name["smoke"].status == "fail"
    retries = br.write_retry_prompts(tmp_path, report)
    names = {p.name: p for p in retries}
    assert "src-main-py.txt" in names
    text = names["src-main-py.txt"].read_text()
    assert "Generate the file at `src/main.py`" in text
    assert "NotImplementedError" in text
    # Next status round hands out the retry prompt instead of the original.
    pending = {p.relative_path: p for p in br.dispatch_manifest(tmp_path)}
    assert pending["src/main.py"].reason == "retry"
    assert pending["src/main.py"].prompt_path.startswith(br.RETRY_DIR)


def test_smoke_expectation_mismatch_fails(tmp_path):
    _python_project(tmp_path, GOOD_MAIN, GOOD_TEST)
    (tmp_path / br.SMOKE_FILE).write_text(json.dumps({"expect_stdout": "42 rows"}))
    report = br.validate_project(tmp_path)
    smoke = next(s for s in report.steps if s.name == "smoke")
    assert smoke.status == "fail" and "42 rows" in smoke.detail


@pytest.mark.parametrize("stack,manifest,expected", [
    ("rust", "Cargo.toml", ["cargo", "test", "--quiet"]),
    ("go", "go.mod", ["go", "test", "./..."]),
    ("java", "pom.xml", ["mvn", "-q", "verify"]),
    ("csharp", "Project.csproj", ["dotnet", "test"]),
    ("ruby", "Gemfile", ["bundle", "exec", "rspec"]),
    ("node", "package.json", ["npm", "test", "--silent"]),
])
def test_each_stack_runs_its_test_command(tmp_path, stack, manifest, expected):
    (tmp_path / manifest).write_text("")
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        return 0, ""

    report = br.validate_project(tmp_path, runner=runner, which=lambda _: "/bin/x")
    assert report.stack == stack
    assert expected in calls
    assert report.passed


def test_missing_toolchain_is_skipped_not_passed(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    report = br.validate_project(tmp_path, which=lambda _: None)
    assert not report.passed
    assert any(s.status == "skipped" and "cargo" in s.detail for s in report.steps)


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_validate_real_go_project(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hi") }\n')
    (tmp_path / "main_test.go").write_text(
        'package main\n\nimport "testing"\n\nfunc TestSmoke(t *testing.T) {}\n')
    report = br.validate_project(tmp_path, timeout=120)
    assert report.passed, br.render_report_md(report)
