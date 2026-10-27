"""
test_catalog_release.py — kb-mcp releases the catalog after each tool call.

Every Claude Code session with the myPub plugin runs its own kb-mcp
process, and even a read-only DuckDB handle blocks the catalog's writer.
The server must therefore open the catalog per call and close it when the
outermost call returns.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import server  # noqa: E402


@pytest.fixture
def fake_catalog(tmp_path, monkeypatch):
    """Patch open_catalog to a plain DuckDB file (no extensions needed)."""
    db_path = tmp_path / "catalog.ddb"
    duckdb.connect(str(db_path)).close()
    opened: list[duckdb.DuckDBPyConnection] = []

    def _open(_path, read_only=True):
        conn = duckdb.connect(str(db_path), read_only=read_only)
        opened.append(conn)
        return conn

    monkeypatch.setattr(server, "open_catalog", _open)
    monkeypatch.setattr(server, "_MODEL", object())
    monkeypatch.setattr(server, "_CONN", None)
    monkeypatch.setattr(server, "_RESOLVER", None)
    monkeypatch.setattr(server, "_CALL_DEPTH", 0)
    monkeypatch.delenv("MYPUB_KB_HOLD_CATALOG", raising=False)
    return db_path, opened


def test_catalog_closed_after_call_so_writer_can_open(fake_catalog):
    db_path, opened = fake_catalog

    @server._release_catalog
    def tool():
        server._bootstrap()
        return server._CONN.execute("SELECT 1").fetchone()[0]

    assert tool() == 1
    assert server._CONN is None and server._RESOLVER is None
    assert len(opened) == 1
    # A writer in another connection can now take the file.
    rw = duckdb.connect(str(db_path), read_only=False)
    rw.close()


def test_nested_calls_keep_connection_until_outermost_returns(fake_catalog):
    _, opened = fake_catalog

    @server._release_catalog
    def inner():
        server._bootstrap()
        return server._CONN

    @server._release_catalog
    def outer():
        server._bootstrap()
        first = server._CONN
        inner()
        assert server._CONN is first  # inner call did not close it
        return True

    assert outer()
    assert server._CONN is None
    assert len(opened) == 1


def test_catalog_closed_when_tool_raises(fake_catalog):
    @server._release_catalog
    def boom():
        server._bootstrap()
        raise ValueError("x")

    with pytest.raises(ValueError):
        boom()
    assert server._CONN is None
    assert server._CALL_DEPTH == 0


def test_hold_catalog_env_keeps_connection(fake_catalog, monkeypatch):
    monkeypatch.setenv("MYPUB_KB_HOLD_CATALOG", "1")

    @server._release_catalog
    def tool():
        server._bootstrap()

    tool()
    assert server._CONN is not None
    server._close_catalog()


def test_every_tool_is_wrapped():
    src = (KB_MCP / "server.py").read_text()
    assert src.count("@mcp.tool\n") == src.count("@mcp.tool\n@_release_catalog\n")
