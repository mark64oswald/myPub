"""
test_output_root.py — generator output_root resolution in kb-mcp/server.py.

The server can be launched from any project (via the myPub plugin), so
output paths must not depend on the process cwd.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import server  # noqa: E402


def test_default_is_repo_generated_packages(monkeypatch, tmp_path):
    monkeypatch.delenv("MYPUB_OUTPUT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert server._resolve_output_root(None) == str(
        PROJECT_ROOT / "data" / "generated-packages"
    )


def test_empty_string_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("MYPUB_OUTPUT", raising=False)
    assert server._resolve_output_root("") == str(
        PROJECT_ROOT / "data" / "generated-packages"
    )


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MYPUB_OUTPUT", str(tmp_path / "out"))
    assert server._resolve_output_root(None) == str(tmp_path / "out")


def test_absolute_path_used_as_is(monkeypatch, tmp_path):
    monkeypatch.setenv("MYPUB_OUTPUT", "/ignored")
    assert server._resolve_output_root(str(tmp_path)) == str(tmp_path)


def test_relative_path_anchored_at_repo_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert server._resolve_output_root("data/generated-packages") == str(
        PROJECT_ROOT / "data" / "generated-packages"
    )
