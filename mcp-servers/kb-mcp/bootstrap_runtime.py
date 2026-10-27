"""bootstrap_runtime.py — Project Bootstrap v2: dispatch loop + runtime validation.

Bootstrap v1 (``project_bootstrap.py``) writes a stack-appropriate
skeleton plus one sub-agent prompt per file. v2 closes the loop
around that skeleton without touching the v1 generator:

  1. ``dispatch_manifest`` — which planned files still hold placeholder
     content, and the prompt that fills each one. Claude Code fans these
     out as Task sub-agents (subscription, never the paid API).
  2. ``validate_project`` — per-stack runtime checks:
       * placeholder scan (no "Fill in from _sub_agent_prompts/" left)
       * the stack's test command (pytest, cargo test, npm test,
         go test ./..., mvn verify, dotnet test, rspec)
       * a data-flow smoke check: run the entry point and require exit 0,
         plus ``expect_stdout`` from an optional ``_smoke.json``
  3. ``write_retry_prompts`` — for every failed step, a follow-up
     prompt carrying the original prompt and the failure output, so the
     next dispatch round fixes what broke.

Loop: manifest → dispatch → validate → retry prompts → dispatch …
until ``validate_project`` passes or the round budget runs out. The
CLI wrapper is ``scripts/bootstrap_v2.py``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

PROMPTS_DIR = "_sub_agent_prompts"
RETRY_DIR = "_sub_agent_prompts/retry"
SMOKE_FILE = "_smoke.json"
VALIDATION_JSON = "_validation.json"
VALIDATION_MD = "_validation.md"

# Strings the v1 placeholders carry. A file that still contains one has
# not been filled in by a sub-agent yet.
PLACEHOLDER_MARKERS = (
    "Fill in from _sub_agent_prompts/",
    "sub-agent fills this in",
    "TODO: populate from stack context",
    "replace with real assertions",
)

_PROMPT_TARGET_RE = re.compile(r"Generate the file at `([^`]+)`")
_OUTPUT_LIMIT = 4000


@dataclass
class StackRuntime:
    name: str
    manifest: str                       # file whose presence identifies the stack
    tool: str                           # binary that must be on PATH
    test: list[str]
    smoke: Optional[list[str]]          # entry-point run; None = no smoke step
    setup: Optional[list[str]] = None   # dependency install before tests


STACK_RUNTIMES: dict[str, StackRuntime] = {
    "python": StackRuntime(
        "python", "pyproject.toml", "python3",
        test=["python3", "-m", "pytest", "-q"],
        smoke=["python3", "-m", "src.main"],
    ),
    "rust": StackRuntime(
        "rust", "Cargo.toml", "cargo",
        test=["cargo", "test", "--quiet"],
        smoke=["cargo", "run", "--quiet"],
    ),
    "typescript": StackRuntime(
        "typescript", "tsconfig.json", "npm",
        setup=["npm", "install", "--no-audit", "--no-fund"],
        test=["npm", "test", "--silent"],
        smoke=["npx", "--yes", "tsc", "--noEmit"],
    ),
    "node": StackRuntime(
        "node", "package.json", "npm",
        setup=["npm", "install", "--no-audit", "--no-fund"],
        test=["npm", "test", "--silent"],
        smoke=["node", "src/index.js"],
    ),
    "java": StackRuntime(
        "java", "pom.xml", "mvn",
        test=["mvn", "-q", "verify"],
        smoke=["java", "-cp", "target/classes", "com.example.Main"],
    ),
    "go": StackRuntime(
        "go", "go.mod", "go",
        test=["go", "test", "./..."],
        smoke=["go", "run", "."],
    ),
    "csharp": StackRuntime(
        "csharp", "Project.csproj", "dotnet",
        test=["dotnet", "test"],
        smoke=["dotnet", "run"],
    ),
    "ruby": StackRuntime(
        "ruby", "Gemfile", "bundle",
        setup=["bundle", "install", "--quiet"],
        test=["bundle", "exec", "rspec"],
        smoke=["ruby", "lib/main.rb"],
    ),
}

# Detection order matters: tsconfig.json before package.json so a
# TypeScript project isn't treated as plain Node.
_DETECT_ORDER = ("typescript", "node", "python", "rust", "java", "go",
                 "csharp", "ruby")


@dataclass
class PendingFile:
    relative_path: str
    prompt_path: str
    reason: str                         # 'missing' | 'placeholder' | 'retry'


@dataclass
class StepResult:
    name: str                           # 'placeholders' | 'setup' | 'test' | 'smoke'
    status: str                         # 'pass' | 'fail' | 'skipped'
    command: Optional[list[str]] = None
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    output: str = ""
    detail: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    project_dir: str
    stack: Optional[str]
    passed: bool
    steps: list[StepResult]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


Runner = Callable[[list[str], Path, int], tuple[int, str]]


def _default_runner(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return 124, f"timed out after {timeout}s\n{out}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _tail(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------------------
# Stack + placeholder discovery
# ---------------------------------------------------------------------------


def detect_stack(project_dir: Path) -> Optional[str]:
    for name in _DETECT_ORDER:
        if (project_dir / STACK_RUNTIMES[name].manifest).exists():
            return name
    return None


def prompt_targets(project_dir: Path) -> dict[str, Path]:
    """Map each planned file's relative path to its sub-agent prompt."""
    out: dict[str, Path] = {}
    pdir = project_dir / PROMPTS_DIR
    if not pdir.is_dir():
        return out
    for p in sorted(pdir.glob("prompt_*.txt")):
        m = _PROMPT_TARGET_RE.search(p.read_text(errors="replace"))
        if m:
            out[m.group(1)] = p
    return out


def placeholder_files(project_dir: Path, paths: Optional[list[str]] = None) -> list[str]:
    candidates = paths if paths is not None else list(prompt_targets(project_dir))
    hits = []
    for rel in candidates:
        f = project_dir / rel
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        if any(m in text for m in PLACEHOLDER_MARKERS):
            hits.append(rel)
    return hits


def dispatch_manifest(project_dir: Path) -> list[PendingFile]:
    """Files that still need a sub-agent: missing, placeholder, or with a
    retry prompt from the last failed validation (retry wins)."""
    targets = prompt_targets(project_dir)
    retries = {}
    rdir = project_dir / RETRY_DIR
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.txt")):
            m = _PROMPT_TARGET_RE.search(p.read_text(errors="replace"))
            if m:
                retries[m.group(1)] = p
    placeholders = set(placeholder_files(project_dir, list(targets)))
    pending: list[PendingFile] = []
    for rel, prompt in targets.items():
        rel_prompt = str(prompt.relative_to(project_dir))
        if rel in retries:
            pending.append(PendingFile(
                rel, str(retries[rel].relative_to(project_dir)), "retry"))
        elif not (project_dir / rel).exists():
            pending.append(PendingFile(rel, rel_prompt, "missing"))
        elif rel in placeholders:
            pending.append(PendingFile(rel, rel_prompt, "placeholder"))
    return pending


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _run_step(
    name: str, cmd: list[str], project_dir: Path, timeout: int, runner: Runner,
) -> StepResult:
    t0 = time.monotonic()
    code, out = runner(cmd, project_dir, timeout)
    return StepResult(
        name=name, status="pass" if code == 0 else "fail", command=cmd,
        exit_code=code, duration_s=round(time.monotonic() - t0, 2),
        output=_tail(out),
    )


def validate_project(
    project_dir: Path,
    *,
    stack: Optional[str] = None,
    timeout: int = 300,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    skip_setup: bool = False,
) -> ValidationReport:
    project_dir = Path(project_dir)
    stack = stack or detect_stack(project_dir)
    steps: list[StepResult] = []

    left = placeholder_files(project_dir)
    steps.append(StepResult(
        name="placeholders", status="fail" if left else "pass",
        detail=(f"{len(left)} file(s) still hold placeholder content"
                if left else "no placeholder markers left"),
        files=left,
    ))

    rt = STACK_RUNTIMES.get(stack or "")
    if rt is None:
        steps.append(StepResult(
            name="test", status="skipped",
            detail="no recognised stack manifest — nothing to run",
        ))
        return _finish(project_dir, stack, steps)

    if which(rt.tool) is None:
        steps.append(StepResult(
            name="test", status="skipped",
            detail=f"`{rt.tool}` not on PATH — install it to run {rt.name} checks",
        ))
        return _finish(project_dir, stack, steps)

    if rt.setup and not skip_setup:
        setup = _run_step("setup", rt.setup, project_dir, timeout, runner)
        steps.append(setup)
        if setup.status == "fail":
            return _finish(project_dir, stack, steps)

    steps.append(_run_step("test", rt.test, project_dir, timeout, runner))

    if rt.smoke:
        smoke = _run_step("smoke", rt.smoke, project_dir, timeout, runner)
        expect = _smoke_expectation(project_dir)
        if smoke.status == "pass" and expect and expect not in smoke.output:
            smoke.status = "fail"
            smoke.detail = f"exit 0 but stdout lacks expected {expect!r}"
        steps.append(smoke)

    return _finish(project_dir, stack, steps)


def _smoke_expectation(project_dir: Path) -> Optional[str]:
    f = project_dir / SMOKE_FILE
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text()).get("expect_stdout") or None
    except (json.JSONDecodeError, AttributeError):
        return None


def _finish(project_dir: Path, stack: Optional[str], steps: list[StepResult]) -> ValidationReport:
    ran = [s for s in steps if s.status != "skipped" and s.name != "placeholders"]
    passed = bool(ran) and all(s.status != "fail" for s in steps)
    report = ValidationReport(str(project_dir), stack, passed, steps)
    (project_dir / VALIDATION_JSON).write_text(report.to_json() + "\n")
    (project_dir / VALIDATION_MD).write_text(render_report_md(report))
    return report


def render_report_md(report: ValidationReport) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}
    lines = [
        "# Bootstrap validation",
        "",
        f"**Stack:** `{report.stack or 'unknown'}`  ",
        f"**Result:** {'PASS' if report.passed else 'FAIL'}",
        "",
        "| Step | Result | Command | Detail |",
        "|---|---|---|---|",
    ]
    for s in report.steps:
        cmd = f"`{' '.join(s.command)}`" if s.command else ""
        detail = s.detail or (f"exit {s.exit_code}" if s.exit_code is not None else "")
        lines.append(f"| {s.name} | {icon[s.status]} | {cmd} | {detail} |")
    for s in report.steps:
        if s.status == "fail" and s.output:
            lines += ["", f"## {s.name} output", "", "```", s.output.rstrip(), "```"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Retry prompts
# ---------------------------------------------------------------------------


def _files_for_failure(step: StepResult, project_dir: Path, targets: dict[str, Path]) -> list[str]:
    """Which planned files a failed step most likely implicates."""
    if step.name == "placeholders":
        return step.files
    named = [rel for rel in targets if rel in step.output or Path(rel).name in step.output]
    if named:
        return named
    # No file named in the output: blame the entry point for smoke, the
    # test files for test, the manifest for setup.
    def pick(pred: Callable[[str], bool]) -> list[str]:
        return [rel for rel in targets if pred(rel.lower())]
    if step.name == "smoke":
        return pick(lambda r: any(k in r for k in ("main", "index", "program")))
    if step.name == "test":
        return pick(lambda r: "test" in r or "spec" in r)
    if step.name == "setup":
        return pick(lambda r: r.endswith((".toml", ".json", ".xml", "gemfile", ".csproj", ".mod")))
    return []


def write_retry_prompts(project_dir: Path, report: ValidationReport) -> list[Path]:
    """Write one retry prompt per implicated file; clear stale ones first."""
    project_dir = Path(project_dir)
    rdir = project_dir / RETRY_DIR
    if rdir.is_dir():
        for old in rdir.glob("*.txt"):
            old.unlink()
    if report.passed:
        return []
    targets = prompt_targets(project_dir)
    reasons: dict[str, list[StepResult]] = {}
    for step in report.steps:
        if step.status != "fail":
            continue
        for rel in _files_for_failure(step, project_dir, targets):
            reasons.setdefault(rel, []).append(step)
    rdir.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, failed in reasons.items():
        original = targets[rel].read_text(errors="replace") if rel in targets else (
            f"Generate the file at `{rel}`.\n")
        current = project_dir / rel
        body = [original.rstrip(), "", "---", "",
                "The previous version of this file failed validation. Fix it.", ""]
        if current.is_file():
            body += ["Current content:", "", current.read_text(errors="replace").rstrip(), ""]
        for s in failed:
            what = " ".join(s.command) if s.command else s.name
            body += [f"Failed step: {s.name} ({what})",
                     s.detail or f"exit {s.exit_code}", ""]
            if s.output:
                body += ["Output:", "", s.output.rstrip(), ""]
        body.append("Write ONLY the corrected file content — no prose, no fences.")
        out = rdir / (re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-") + ".txt")
        out.write_text("\n".join(body) + "\n")
        written.append(out)
    return written
