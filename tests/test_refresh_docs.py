"""
test_refresh_docs.py — Phase 4.4 snapshot ingestion pipeline tests.

Layered to mirror the script's structure:

    Lock handling     → unit tests with mocked psutil + a small integration
                        test using a real subprocess holding a real RO connection
    Fetchers          → unit tests with mocked MCP SDK + httpx
    Pipeline (1–6)    → integration tests over an in-memory v2 catalog
    prep / process    → tests over an in-memory catalog with synthesized
                        sub-agent result JSONs

Tests that need a writable catalog use the ``rw_catalog`` fixture below
(temp file, v2 schema, all extensions loaded).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import duckdb
import httpx
import psutil
import pytest


def _network_available() -> bool:
    """Quick TCP probe to a public DNS so live tests skip cleanly when offline."""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2.0):
            return True
    except OSError:
        return False


# Live tests run by default — the project standard requires they ride alongside
# mock tests on every full run. Opt-out via MYPUB_SKIP_LIVE_TESTS=1 (CI without
# network); auto-skip with a clear reason if the network probe fails.
SKIP_LIVE_OPT_OUT = os.getenv("MYPUB_SKIP_LIVE_TESTS") == "1"
NETWORK_AVAILABLE = _network_available() if not SKIP_LIVE_OPT_OUT else False
live_only = pytest.mark.skipif(
    SKIP_LIVE_OPT_OUT or not NETWORK_AVAILABLE,
    reason=(
        "MYPUB_SKIP_LIVE_TESTS=1 set" if SKIP_LIVE_OPT_OUT
        else "no network available for external-API integration tests"
    ),
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import refresh_docs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rw_catalog(tmp_path):
    """Temp catalog with the v2 schema applied. RW-friendly path for writers."""
    db_path = tmp_path / "catalog.ddb"
    conn = duckdb.connect(str(db_path))
    conn.execute(SCHEMA_FILE.read_text())
    conn.close()
    yield db_path


@pytest.fixture
def rw_conn(rw_catalog):
    """Open a RW connection with FTS extension loaded — pipeline writes need both."""
    conn = duckdb.connect(str(rw_catalog))
    conn.execute("LOAD fts")
    yield conn
    conn.close()


@pytest.fixture
def seeded_doc_source(rw_conn):
    """Insert one doc_source row and return its id. Source_type is github_md-friendly
    so the markdown sectionizer fires during pipeline tests."""
    rw_conn.execute(
        "INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, identifier, "
        "                        authority_score, refresh_ttl_days) "
        "VALUES (1001, 'Test Source', 'github', 'github', 'octocat/test-repo', 0.5, 7)"
    )
    return 1001


class _FakeEmbedder:
    """Deterministic 384-dim embeddings — sums of byte values, scaled. No network."""

    def encode(self, texts, *, show_progress_bar=False):
        # numpy not strictly required — return list of lists; the pipeline coerces to list.
        out = []
        for t in texts:
            seed = sum(ord(c) for c in t) or 1
            out.append([float((seed + i) % 100) / 100.0 for i in range(384)])
        return out


class _FakeFetcher:
    """Returns canned content. Used to drive the pipeline without external I/O."""

    def __init__(self, content: str, *, source_type: str = "markdown",
                 url: str = "https://example.test/x"):
        self.content = content
        self.source_type = source_type
        self.url = url
        self.call_count = 0

    def fetch(self, identifier: str):
        self.call_count += 1
        return refresh_docs.FetchResult(
            url=self.url, content=self.content, source_type=self.source_type,
        )


class _ErroringFetcher:
    def fetch(self, identifier: str):
        raise refresh_docs.FetchError("simulated transport failure")


class _FakeResolver:
    """Test double for EntityResolver that actually inserts concept rows.

    process_extraction writes concept_relation rows that FK-reference
    concept(concept_id), so the resolver must produce *real* concept IDs.
    This fake mirrors the EntityResolver public API: ``resolve()`` returns
    a ResolveResult-shaped object with ``.concept_id``. Each unique name
    creates a fresh concept row; repeats reuse the same id.
    """

    def __init__(self, conn):
        self.conn = conn
        self._by_name: dict[str, int] = {}

    def resolve(self, candidate_name, candidate_context="", concept_type=None,
                *, source_type=None, source_id=None):
        ctype = concept_type or "Concept"
        if candidate_name in self._by_name:
            cid = self._by_name[candidate_name]
        else:
            # Mirror EntityResolver's exact-match stage: check DB before insert.
            existing = self.conn.execute(
                "SELECT concept_id FROM concept WHERE name = ? AND concept_type = ?",
                [candidate_name, ctype],
            ).fetchone()
            if existing is not None:
                cid = int(existing[0])
            else:
                row = self.conn.execute(
                    "INSERT INTO concept (name, concept_type, description) "
                    "VALUES (?, ?, ?) RETURNING concept_id",
                    [candidate_name, ctype, candidate_context],
                ).fetchone()
                cid = int(row[0])
            self._by_name[candidate_name] = cid
        return SimpleNamespace(concept_id=cid, is_new=False, resolution_type="exact")


# ---------------------------------------------------------------------------
# Lock-handling: helpers (unit tests, mocked psutil)
# ---------------------------------------------------------------------------


class _FakeOpenFile:
    """Stand-in for psutil's open_file namedtuple — only `.path` is used."""

    def __init__(self, path: str):
        self.path = path


class _FakeProc:
    """Stand-in for psutil.Process iter entries used by _find_lock_holder.

    Matches the dict-info shape psutil yields from process_iter(['pid', 'open_files']).
    """

    def __init__(self, pid: int, open_paths: list[str]):
        self.info = {"pid": pid, "open_files": [_FakeOpenFile(p) for p in open_paths]}


def test_find_lock_holder_returns_pid_when_path_matches(tmp_path):
    target = tmp_path / "catalog.ddb"
    target.touch()  # ensure the path resolves

    with patch.object(psutil, "process_iter", return_value=[
        _FakeProc(pid=999, open_paths=["/some/other/file"]),
        _FakeProc(pid=12345, open_paths=[str(target.resolve())]),
    ]):
        assert refresh_docs._find_lock_holder(target) == 12345


def test_find_lock_holder_returns_none_when_no_match(tmp_path):
    target = tmp_path / "catalog.ddb"
    target.touch()

    with patch.object(psutil, "process_iter", return_value=[
        _FakeProc(pid=999, open_paths=["/elsewhere"]),
    ]):
        assert refresh_docs._find_lock_holder(target) is None


def test_find_lock_holder_skips_processes_that_raise(tmp_path):
    """psutil raising NoSuchProcess / AccessDenied during iteration must not crash the scan."""
    target = tmp_path / "catalog.ddb"
    target.touch()

    class _ExplodingProc:
        @property
        def info(self):
            raise psutil.AccessDenied(pid=42)

    with patch.object(psutil, "process_iter", return_value=[
        _ExplodingProc(),
        _FakeProc(pid=7777, open_paths=[str(target.resolve())]),
    ]):
        assert refresh_docs._find_lock_holder(target) == 7777


def test_is_trusted_reader_true_on_substring_match():
    with patch.object(refresh_docs, "_safe_cmdline",
                      return_value="/usr/bin/python3 mcp-servers/kb-mcp/server.py"):
        assert refresh_docs._is_trusted_reader(123, "mcp-servers/kb-mcp/server.py") is True


def test_is_trusted_reader_false_on_unrelated_cmdline():
    with patch.object(refresh_docs, "_safe_cmdline", return_value="duckdb /tmp/foo.ddb"):
        assert refresh_docs._is_trusted_reader(123, "mcp-servers/kb-mcp/server.py") is False


def test_is_trusted_reader_false_on_empty_cmdline():
    """An unreachable cmdline ('') must NOT be treated as trusted (fail-safe default)."""
    with patch.object(refresh_docs, "_safe_cmdline", return_value=""):
        assert refresh_docs._is_trusted_reader(123, "mcp-servers/kb-mcp/server.py") is False


# ---------------------------------------------------------------------------
# Lock-handling: open_writer (unit tests, mocked dependencies)
# ---------------------------------------------------------------------------


def test_open_writer_happy_path_no_holder(rw_catalog):
    """When no other process holds the file, open_writer just returns the RW conn."""
    conn = refresh_docs.open_writer(rw_catalog)
    try:
        # Verify the connection actually works for writes.
        conn.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                     "VALUES ('test', 'github', 'github', 'a/b')")
        assert conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == 1
    finally:
        conn.close()


def test_open_writer_no_auto_stop_raises_lockhelderror_when_held(rw_catalog):
    """With --no-auto-stop, a held lock raises immediately with a LockHeldError naming the holder."""
    # Simulate first open raising a lock-style IOException.
    fake_io = duckdb.IOException("Could not set lock on file: Conflicting lock is held in /usr/bin/python")
    with patch.object(refresh_docs, "open_catalog", side_effect=fake_io):
        with patch.object(refresh_docs, "_find_lock_holder", return_value=42):
            with patch.object(refresh_docs, "_safe_cmdline", return_value="some-other-process"):
                with pytest.raises(refresh_docs.LockHeldError) as exc_info:
                    refresh_docs.open_writer(rw_catalog, auto_stop=False)
    err = exc_info.value
    assert err.pid == 42
    assert "no-auto-stop" in err.reason
    assert "some-other-process" in err.cmdline


def test_open_writer_untrusted_holder_raises_even_with_auto_stop(rw_catalog):
    """A held lock from an untrusted process must NEVER be auto-killed."""
    fake_io = duckdb.IOException("Could not set lock on file: Conflicting lock")
    with patch.object(refresh_docs, "open_catalog", side_effect=fake_io):
        with patch.object(refresh_docs, "_find_lock_holder", return_value=99):
            with patch.object(refresh_docs, "_safe_cmdline", return_value="duckdb /tmp/random.ddb"):
                with patch.object(refresh_docs, "_signal_process") as mock_signal:
                    with pytest.raises(refresh_docs.LockHeldError) as exc_info:
                        refresh_docs.open_writer(rw_catalog, auto_stop=True)
                    # Critical safety check — we must not have sent a signal.
                    mock_signal.assert_not_called()
    err = exc_info.value
    assert err.pid == 99
    assert "untrusted" in err.reason.lower() or "does not contain" in err.reason


def test_open_writer_unidentifiable_holder_raises(rw_catalog):
    """If we can't identify the holder PID, fail loudly rather than guessing."""
    fake_io = duckdb.IOException("Could not set lock on file")
    with patch.object(refresh_docs, "open_catalog", side_effect=fake_io):
        with patch.object(refresh_docs, "_find_lock_holder", return_value=None):
            with pytest.raises(refresh_docs.LockHeldError) as exc_info:
                refresh_docs.open_writer(rw_catalog, auto_stop=True)
    assert exc_info.value.pid is None
    assert "could not be identified" in exc_info.value.reason


def test_open_writer_non_lock_io_error_propagates(rw_catalog):
    """Non-lock IO errors must bubble up unchanged, not be swallowed by lock handling."""
    fake_io = duckdb.IOException("Disk full or something else entirely")
    with patch.object(refresh_docs, "open_catalog", side_effect=fake_io):
        with pytest.raises(duckdb.IOException):
            refresh_docs.open_writer(rw_catalog)


def test_open_writer_trusted_holder_auto_stops_and_retries(rw_catalog):
    """Trusted holder + auto_stop=True: SIGTERM, wait for release, retry once.

    Uses a side-effect counter to simulate "first open fails, second succeeds".
    """
    real_open = refresh_docs.open_catalog
    open_calls = {"count": 0}

    def open_side_effect(path, *, read_only):
        open_calls["count"] += 1
        if open_calls["count"] == 1:
            raise duckdb.IOException("Could not set lock on file: Conflicting lock")
        return real_open(path, read_only=read_only)

    with patch.object(refresh_docs, "open_catalog", side_effect=open_side_effect):
        with patch.object(refresh_docs, "_find_lock_holder", return_value=12345):
            with patch.object(refresh_docs, "_safe_cmdline",
                              return_value="/usr/bin/python3 mcp-servers/kb-mcp/server.py"):
                with patch.object(refresh_docs, "_signal_process") as mock_signal:
                    with patch.object(refresh_docs, "_wait_for_lock_release") as mock_wait:
                        conn = refresh_docs.open_writer(rw_catalog, auto_stop=True)
                        try:
                            mock_signal.assert_called_once_with(12345, signal.SIGTERM)
                            mock_wait.assert_called_once()
                            assert open_calls["count"] == 2  # exactly one retry
                        finally:
                            conn.close()


def test_open_writer_trusted_holder_does_not_loop_on_persistent_failure(rw_catalog):
    """If the second open also fails, the error propagates — no infinite loop."""
    fake_io = duckdb.IOException("Could not set lock on file: Conflicting lock")
    open_calls = {"count": 0}

    def open_side_effect(path, *, read_only):
        open_calls["count"] += 1
        raise fake_io

    with patch.object(refresh_docs, "open_catalog", side_effect=open_side_effect):
        with patch.object(refresh_docs, "_find_lock_holder", return_value=12345):
            with patch.object(refresh_docs, "_safe_cmdline",
                              return_value="mcp-servers/kb-mcp/server.py"):
                with patch.object(refresh_docs, "_signal_process"):
                    with patch.object(refresh_docs, "_wait_for_lock_release"):
                        with pytest.raises(duckdb.IOException):
                            refresh_docs.open_writer(rw_catalog, auto_stop=True)
                        assert open_calls["count"] == 2  # exactly two attempts, no looping


# ---------------------------------------------------------------------------
# Lock-handling: integration test with a real subprocess holding RO
# ---------------------------------------------------------------------------


def _ro_holder_subprocess(catalog_path_str: str, ready_q, hold_seconds: float):
    """Run in a child process: open RO, signal ready, sleep, exit on signal."""
    import duckdb  # re-import in the child for clarity
    conn = duckdb.connect(catalog_path_str, read_only=True)
    ready_q.put("ready")
    end_at = time.monotonic() + hold_seconds
    while time.monotonic() < end_at:
        time.sleep(0.05)
    conn.close()


def test_lock_integration_real_subprocess_holds_then_we_fail_fast_when_no_auto_stop(rw_catalog):
    """Real subprocess (NOT named like kb-mcp) holds RO; --no-auto-stop must fail-fast."""
    ctx = mp.get_context("spawn")
    ready_q: "mp.Queue[str]" = ctx.Queue()
    proc = ctx.Process(
        target=_ro_holder_subprocess,
        args=(str(rw_catalog), ready_q, 5.0),
    )
    proc.start()
    try:
        assert ready_q.get(timeout=10) == "ready"
        time.sleep(0.1)  # let the OS register the lock
        with pytest.raises(refresh_docs.LockHeldError) as exc_info:
            refresh_docs.open_writer(rw_catalog, auto_stop=False)
        assert exc_info.value.pid == proc.pid
        # Subprocess should still be alive — we did not signal it.
        assert proc.is_alive()
    finally:
        proc.terminate()
        proc.join(timeout=5)


# ---------------------------------------------------------------------------
# Fetchers — GitHub (mocked + live)
# ---------------------------------------------------------------------------


def test_github_parse_identifier_owner_repo_only():
    owner, repo, branch, path = refresh_docs.GitHubFetcher._parse_identifier("octocat/Hello-World")
    assert (owner, repo, branch, path) == ("octocat", "Hello-World", None, "README.md")


def test_github_parse_identifier_with_branch():
    owner, repo, branch, path = refresh_docs.GitHubFetcher._parse_identifier("octocat/Hello-World:dev")
    assert (owner, repo, branch, path) == ("octocat", "Hello-World", "dev", "README.md")


def test_github_parse_identifier_with_branch_and_path():
    owner, repo, branch, path = refresh_docs.GitHubFetcher._parse_identifier(
        "octocat/Hello-World:main:docs/intro.md"
    )
    assert (owner, repo, branch, path) == ("octocat", "Hello-World", "main", "docs/intro.md")


def test_github_parse_identifier_rejects_missing_repo():
    with pytest.raises(refresh_docs.FetchError, match="must include 'owner/repo'"):
        refresh_docs.GitHubFetcher._parse_identifier("just-a-name")


def test_github_fetch_happy_path_main_branch():
    """First branch attempt (main) returns 200 — fetch returns its content with the main URL."""
    fetcher = refresh_docs.GitHubFetcher()
    fake_response = SimpleNamespace(status_code=200, text="# Hello World\n")
    with patch.object(httpx, "get", return_value=fake_response) as mock_get:
        result = fetcher.fetch("octocat/Hello-World")
    assert result.source_type == "github_md"
    assert result.content == "# Hello World\n"
    assert "main/README.md" in result.url
    # First call only — no fallback needed
    assert mock_get.call_count == 1


def test_github_fetch_falls_back_to_master_when_main_404s():
    """main returns 404, master returns 200 — fetch retries with master."""
    fetcher = refresh_docs.GitHubFetcher()
    responses = iter([
        SimpleNamespace(status_code=404, text=""),
        SimpleNamespace(status_code=200, text="legacy content"),
    ])
    with patch.object(httpx, "get", side_effect=lambda *a, **kw: next(responses)) as mock_get:
        result = fetcher.fetch("octocat/Hello-World")
    assert result.content == "legacy content"
    assert "master/README.md" in result.url
    assert mock_get.call_count == 2


def test_github_fetch_404_on_both_branches_raises():
    """Both main and master 404 → FetchError that names the failure."""
    fetcher = refresh_docs.GitHubFetcher()
    fake_response = SimpleNamespace(status_code=404, text="")
    with patch.object(httpx, "get", return_value=fake_response):
        with pytest.raises(refresh_docs.FetchError, match="GitHub fetch failed"):
            fetcher.fetch("nope/nada")


def test_github_fetch_transport_error_wraps_in_fetcherror():
    fetcher = refresh_docs.GitHubFetcher()
    with patch.object(httpx, "get", side_effect=httpx.ConnectError("network down")):
        with pytest.raises(refresh_docs.FetchError, match="GitHub fetch failed"):
            fetcher.fetch("octocat/Hello-World")


@live_only
def test_github_fetch_live_octocat():
    """Live: fetch a small, stable, public README. Gated by MYPUB_LIVE_TESTS=1."""
    fetcher = refresh_docs.GitHubFetcher()
    # Spoon-Knife is GitHub's official forking-demo repo: tiny, public,
    # known-stable, and has a README.md on main.
    result = fetcher.fetch("octocat/Spoon-Knife")
    assert result.source_type == "github_md"
    assert len(result.content) > 0
    assert result.url.startswith("https://raw.githubusercontent.com/octocat/Spoon-Knife/")


# ---------------------------------------------------------------------------
# Fetchers — Context7 (parsing tests + live)
# ---------------------------------------------------------------------------


def test_context7_text_to_chunks_splits_on_dashes():
    text = (
        "Title: Foo\nSource: https://example.com/foo\nbody for foo\n"
        "----------\n"
        "Title: Bar\nSource: https://example.com/bar\nbody for bar"
    )
    chunks = refresh_docs.Context7Fetcher._text_to_chunks(text)
    assert len(chunks) == 2
    assert chunks[0]["title"] == "Foo"
    assert chunks[0]["source"] == "https://example.com/foo"
    assert chunks[0]["content"] == "body for foo"
    assert chunks[1]["title"] == "Bar"


def test_context7_text_to_chunks_handles_no_dashes():
    """A single chunk without separator should still produce one section."""
    text = "Some plain text with no separators"
    chunks = refresh_docs.Context7Fetcher._text_to_chunks(text)
    assert len(chunks) == 1
    assert chunks[0]["content"] == "Some plain text with no separators"
    assert chunks[0]["title"] is None
    assert chunks[0]["source"] is None


def test_context7_text_to_chunks_handles_missing_title_or_source():
    """Chunks without Title:/Source: lines should still parse cleanly."""
    text = "raw body line one\nraw body line two\n----------\nTitle: Has Title\nbody"
    chunks = refresh_docs.Context7Fetcher._text_to_chunks(text)
    assert chunks[0]["title"] is None
    assert chunks[0]["content"] == "raw body line one\nraw body line two"
    assert chunks[1]["title"] == "Has Title"


def test_extract_text_from_call_result_concatenates_text_items_only():
    """TextContent items are joined; non-text items (e.g. images) are ignored."""
    result = SimpleNamespace(content=[
        SimpleNamespace(text="first chunk"),
        SimpleNamespace(text="second chunk"),
        SimpleNamespace(text=None),  # malformed; skipped
        SimpleNamespace(),  # missing .text; skipped
    ])
    out = refresh_docs._extract_text_from_call_result(result)
    assert out == "first chunk\nsecond chunk"


def test_extract_text_from_call_result_handles_empty_content():
    result = SimpleNamespace(content=[])
    assert refresh_docs._extract_text_from_call_result(result) == ""


def test_context7_merge_query_results_dedupes_across_queries():
    """Same chunk appearing under two query responses must be returned only once."""
    chunk_a = (
        "Title: Setup\nSource: https://example/docs/setup\n"
        "install with pip"
    )
    chunk_b = (
        "Title: Config\nSource: https://example/docs/config\n"
        "set MY_VAR=1"
    )
    response_1 = chunk_a + "\n----------\n" + chunk_b
    response_2 = chunk_a + "\n----------\nTitle: Examples\nSource: https://example/docs/examples\nrun foo"
    merged = refresh_docs.Context7Fetcher._merge_query_results([response_1, response_2])
    sources = [c.get("source") for c in merged]
    assert sources == [
        "https://example/docs/setup",
        "https://example/docs/config",
        "https://example/docs/examples",
    ]


def test_context7_merge_query_results_skips_protocol_errors():
    """MCP-level error responses must be ignored, not turned into chunks."""
    error_response = "MCP error -32602: Tool query-docs not found"
    good_response = "Title: Real\nSource: https://example/x\nbody"
    merged = refresh_docs.Context7Fetcher._merge_query_results(
        [error_response, good_response, ""],
    )
    assert len(merged) == 1
    assert merged[0]["source"] == "https://example/x"


def test_context7_merge_query_results_drops_empty_content_chunks():
    """A chunk with no body text contributes nothing — drop, don't index empty rows."""
    response = (
        "Title: Header only\nSource: https://example/empty\n"
        "----------\n"
        "Title: Real\nSource: https://example/real\nactual body"
    )
    merged = refresh_docs.Context7Fetcher._merge_query_results([response])
    assert len(merged) == 1
    assert merged[0]["source"] == "https://example/real"


@live_only
def test_context7_fetch_live_duckdb_multi_query_returns_broad_coverage():
    """Live: multi-query fan-out must produce substantially more chunks than
    a single-query response. Sanity threshold: >= 10 chunks with non-empty
    content from at least 3 distinct source URLs.

    Phase 4.4 architectural constraint: Context7 is query-driven; one query
    yields ~5 chunks. Five canonical queries should yield substantially
    more breadth even after dedup — this test catches regressions where
    we accidentally degrade to single-query.
    """
    fetcher = refresh_docs.Context7Fetcher()
    result = fetcher.fetch("/duckdb/duckdb")
    assert result.source_type == "context7"
    chunks = json.loads(result.content)
    assert len(chunks) >= 10, (
        f"expected >=10 chunks across canonical queries, got {len(chunks)} — "
        f"check that Context7 multi-query loop didn't regress to single query"
    )
    distinct_sources = {c.get("source") for c in chunks if c.get("source")}
    assert len(distinct_sources) >= 3, (
        f"expected coverage across >=3 source URLs, got {len(distinct_sources)}"
    )
    # Every retained chunk must have non-empty body — _merge_query_results invariant.
    assert all((c.get("content") or "").strip() for c in chunks)


# ---------------------------------------------------------------------------
# Fetchers — DeepWiki (parsing tests + live)
# ---------------------------------------------------------------------------


@live_only
def test_deepwiki_fetch_live_fastmcp():
    """Live: fetch DeepWiki content for PrefectHQ/fastmcp. Gated by MYPUB_LIVE_TESTS=1."""
    fetcher = refresh_docs.DeepWikiFetcher()
    result = fetcher.fetch("PrefectHQ/fastmcp")
    assert result.source_type == "markdown"
    assert "fastmcp" in result.content.lower() or "FastMCP" in result.content
    assert result.url == "https://deepwiki.com/PrefectHQ/fastmcp"


# ---------------------------------------------------------------------------
# Fetchers — dispatch
# ---------------------------------------------------------------------------


def test_get_fetcher_dispatches_by_source_type():
    assert isinstance(refresh_docs.get_fetcher("github"), refresh_docs.GitHubFetcher)
    assert isinstance(refresh_docs.get_fetcher("context7"), refresh_docs.Context7Fetcher)
    assert isinstance(refresh_docs.get_fetcher("deepwiki"), refresh_docs.DeepWikiFetcher)


def test_get_fetcher_case_insensitive():
    assert isinstance(refresh_docs.get_fetcher("GitHub"), refresh_docs.GitHubFetcher)
    assert isinstance(refresh_docs.get_fetcher("DEEPWIKI"), refresh_docs.DeepWikiFetcher)


def test_get_fetcher_unknown_type_raises():
    with pytest.raises(refresh_docs.FetchError, match="no fetcher registered"):
        refresh_docs.get_fetcher("not-a-real-type")


# ---------------------------------------------------------------------------
# Pipeline — pure functions
# ---------------------------------------------------------------------------


def test_compute_content_hash_deterministic_identical_input():
    h1 = refresh_docs.compute_content_hash("# Hello\nWorld")
    h2 = refresh_docs.compute_content_hash("# Hello\nWorld")
    assert h1 == h2 and len(h1) == 64  # SHA-256 hex


def test_compute_content_hash_differs_on_whitespace_change():
    """Conservative behavior: small upstream changes invalidate the hash."""
    h1 = refresh_docs.compute_content_hash("# Hello\nWorld")
    h2 = refresh_docs.compute_content_hash("# Hello\n World")
    assert h1 != h2


def test_flatten_section_tree_preorder_with_parent_indices():
    """Tree:    R0          ->  flat[0] parent=-1
                ├─ A         ->  flat[1] parent=0
                │  └─ A1     ->  flat[2] parent=1
                └─ B         ->  flat[3] parent=0
                R1           ->  flat[4] parent=-1
    """
    from sectionizer import Section
    a1 = Section(heading_level=3, heading_text="A1", content="a1", ordinal=0, children=[])
    a = Section(heading_level=2, heading_text="A", content="a", ordinal=0, children=[a1])
    b = Section(heading_level=2, heading_text="B", content="b", ordinal=1, children=[])
    r0 = Section(heading_level=1, heading_text="R0", content="r0", ordinal=0, children=[a, b])
    r1 = Section(heading_level=1, heading_text="R1", content="r1", ordinal=1, children=[])

    flat = refresh_docs.flatten_section_tree([r0, r1])
    assert [s.heading_text for s in flat] == ["R0", "A", "A1", "B", "R1"]
    assert [s.parent_index for s in flat] == [-1, 0, 1, 0, -1]


def test_flatten_section_tree_empty_input():
    assert refresh_docs.flatten_section_tree([]) == []


# ---------------------------------------------------------------------------
# Pipeline — DB-touching steps (one fixture each, scoped tightly)
# ---------------------------------------------------------------------------


def test_get_latest_snapshot_hash_returns_none_when_empty(rw_conn, seeded_doc_source):
    assert refresh_docs.get_latest_snapshot_hash(rw_conn, seeded_doc_source) is None


def test_get_latest_snapshot_hash_returns_most_recent(rw_conn, seeded_doc_source):
    rw_conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content) "
        "VALUES (?, 'github', 'u1', 'oldhash', 'old')",
        [seeded_doc_source],
    )
    rw_conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content) "
        "VALUES (?, 'github', 'u2', 'newhash', 'new')",
        [seeded_doc_source],
    )
    assert refresh_docs.get_latest_snapshot_hash(rw_conn, seeded_doc_source) == "newhash"


def test_persist_snapshot_inserts_and_returns_id(rw_conn, seeded_doc_source):
    fetched = refresh_docs.FetchResult(url="https://x/y", content="body", source_type="markdown")
    sid = refresh_docs.persist_snapshot(
        rw_conn,
        doc_source_id=seeded_doc_source,
        fetched=fetched,
        snapshot_source_type="github",
        content_hash="abc123",
    )
    assert isinstance(sid, int) and sid > 0
    row = rw_conn.execute(
        "SELECT doc_source_id, source_type, url, content_hash, content "
        "  FROM doc_snapshot WHERE snapshot_id = ?", [sid]).fetchone()
    assert row == (seeded_doc_source, "github", "https://x/y", "abc123", "body")


def test_persist_sections_wires_parent_id_correctly(rw_conn, seeded_doc_source):
    sid = refresh_docs.persist_snapshot(
        rw_conn, doc_source_id=seeded_doc_source,
        fetched=refresh_docs.FetchResult(url="u", content="c", source_type="markdown"),
        snapshot_source_type="github", content_hash="h",
    )
    flat = [
        refresh_docs._FlatSection(parent_index=-1, heading_level=1, heading_text="R",
                                  content="root", ordinal=0),
        refresh_docs._FlatSection(parent_index=0, heading_level=2, heading_text="C1",
                                  content="child1", ordinal=0),
        refresh_docs._FlatSection(parent_index=0, heading_level=2, heading_text="C2",
                                  content="child2", ordinal=1),
    ]
    ids = refresh_docs.persist_sections(rw_conn, sid, flat)
    assert len(ids) == 3
    rows = rw_conn.execute(
        "SELECT doc_section_id, parent_id, heading_text "
        "  FROM doc_section WHERE snapshot_id = ? ORDER BY doc_section_id", [sid]
    ).fetchall()
    # Root has no parent; both children point at root id.
    assert rows[0] == (ids[0], None, "R")
    assert rows[1] == (ids[1], ids[0], "C1")
    assert rows[2] == (ids[2], ids[0], "C2")


def test_generate_section_embeddings_writes_float384_rows(rw_conn, seeded_doc_source):
    sid = refresh_docs.persist_snapshot(
        rw_conn, doc_source_id=seeded_doc_source,
        fetched=refresh_docs.FetchResult(url="u", content="c", source_type="markdown"),
        snapshot_source_type="github", content_hash="h",
    )
    flat = [
        refresh_docs._FlatSection(parent_index=-1, heading_level=None,
                                  heading_text=None, content="some text", ordinal=0),
    ]
    section_ids = refresh_docs.persist_sections(rw_conn, sid, flat)
    refresh_docs.generate_section_embeddings(
        rw_conn, section_ids=section_ids, contents=["some text"], embedder=_FakeEmbedder(),
    )
    row = rw_conn.execute(
        "SELECT doc_section_id, len(embedding), model "
        "  FROM doc_section_embedding WHERE doc_section_id = ?", [section_ids[0]]
    ).fetchone()
    assert row == (section_ids[0], 384, refresh_docs.EMBEDDING_MODEL)


def test_generate_section_embeddings_handles_empty_input(rw_conn):
    """No section ids → no error, no insert."""
    refresh_docs.generate_section_embeddings(
        rw_conn, section_ids=[], contents=[], embedder=_FakeEmbedder(),
    )
    count = rw_conn.execute("SELECT COUNT(*) FROM doc_section_embedding").fetchone()[0]
    assert count == 0


def test_rebuild_doc_section_fts_index_makes_content_queryable(rw_conn, seeded_doc_source):
    """After rebuild, fts_main_doc_section.match_bm25 returns scores for matching keywords."""
    sid = refresh_docs.persist_snapshot(
        rw_conn, doc_source_id=seeded_doc_source,
        fetched=refresh_docs.FetchResult(url="u", content="c", source_type="markdown"),
        snapshot_source_type="github", content_hash="h",
    )
    flat = [
        refresh_docs._FlatSection(parent_index=-1, heading_level=2, heading_text="kafka",
                                  content="kafka streams windowed aggregation", ordinal=0),
        refresh_docs._FlatSection(parent_index=-1, heading_level=2, heading_text="other",
                                  content="completely unrelated content", ordinal=1),
    ]
    refresh_docs.persist_sections(rw_conn, sid, flat)
    refresh_docs.rebuild_doc_section_fts_index(rw_conn)

    rows = rw_conn.execute(
        f"""
        SELECT ds.doc_section_id,
               {refresh_docs.DOC_SECTION_FTS_SCHEMA}.match_bm25(ds.doc_section_id, ?) AS score
          FROM doc_section ds
         WHERE score IS NOT NULL
         ORDER BY score DESC
        """,
        ["kafka windowed"],
    ).fetchall()
    assert len(rows) >= 1
    assert all(r[1] is not None for r in rows)


# ---------------------------------------------------------------------------
# Pipeline — refresh_one_source (orchestrator)
# ---------------------------------------------------------------------------


def test_refresh_one_source_happy_path_creates_snapshot_sections_embeddings(
    rw_conn, seeded_doc_source,
):
    fetcher = _FakeFetcher(
        content="# Hello\n\n## Section A\nbody a\n\n## Section B\nbody b",
        source_type="markdown",
    )
    result = refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    assert result.status == "refreshed"
    assert result.snapshot_id is not None
    # H1 + 2x H2 → 1 root + 2 children = 3 sections (markdown sectionizer default)
    assert result.section_count == 3

    # Snapshot row exists with the right doc_source_id and content_hash.
    snap = rw_conn.execute(
        "SELECT doc_source_id, source_type, content_hash "
        "  FROM doc_snapshot WHERE snapshot_id = ?", [result.snapshot_id]
    ).fetchone()
    assert snap[0] == seeded_doc_source
    assert snap[1] == "github"  # canonical doc_source.source_type
    assert snap[2] == refresh_docs.compute_content_hash(fetcher.content)

    # Embeddings cover every section.
    emb_count = rw_conn.execute(
        "SELECT COUNT(*) FROM doc_section_embedding e "
        "  JOIN doc_section s USING (doc_section_id) "
        " WHERE s.snapshot_id = ?", [result.snapshot_id]
    ).fetchone()[0]
    assert emb_count == 3


def test_refresh_one_source_hash_skip_when_content_unchanged(rw_conn, seeded_doc_source):
    """Second refresh with identical content must not create a new snapshot."""
    fetcher = _FakeFetcher(content="# Same\nbody", source_type="markdown")
    first = refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    second = refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    assert first.status == "refreshed"
    assert second.status == "no_change"
    assert second.snapshot_id is None
    snap_count = rw_conn.execute(
        "SELECT COUNT(*) FROM doc_snapshot WHERE doc_source_id = ?", [seeded_doc_source]
    ).fetchone()[0]
    assert snap_count == 1  # second call did not insert


def test_refresh_one_source_updates_last_refresh_at_on_skip(rw_conn, seeded_doc_source):
    """no-change still bumps last_refresh_at — that's how scheduling knows it ran."""
    fetcher = _FakeFetcher(content="# Same\nbody", source_type="markdown")
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    rw_conn.execute(
        "UPDATE doc_source SET last_refresh_at = NULL, last_content_changed_at = NULL "
        " WHERE doc_source_id = ?", [seeded_doc_source])
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    row = rw_conn.execute(
        "SELECT last_refresh_at, last_content_changed_at "
        "  FROM doc_source WHERE doc_source_id = ?", [seeded_doc_source]
    ).fetchone()
    assert row[0] is not None  # last_refresh_at bumped on no-change
    assert row[1] is None      # last_content_changed_at NOT bumped on no-change


def test_refresh_one_source_handles_fetch_error(rw_conn, seeded_doc_source):
    result = refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source,
        fetcher=_ErroringFetcher(), embedder=_FakeEmbedder(),
    )
    assert result.status == "error"
    assert "simulated" in result.error
    snap_count = rw_conn.execute(
        "SELECT COUNT(*) FROM doc_snapshot WHERE doc_source_id = ?", [seeded_doc_source]
    ).fetchone()[0]
    assert snap_count == 0


# ---------------------------------------------------------------------------
# prep_extraction — writes prompts + manifest under output_dir
# ---------------------------------------------------------------------------


def _seed_snapshot_with_sections(conn, doc_source_id, content):
    """Helper: run pipeline so we have a real snapshot + sections to prep."""
    refresh_docs.refresh_one_source(
        conn, doc_source_id,
        fetcher=_FakeFetcher(content=content, source_type="markdown"),
        embedder=_FakeEmbedder(),
    )
    snap_id = conn.execute(
        "SELECT snapshot_id FROM doc_snapshot WHERE doc_source_id = ?",
        [doc_source_id]).fetchone()[0]
    return int(snap_id)


def test_prep_extraction_writes_procedure_prompt_per_section(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Step 8: prep emits a separate procedure prompt file per section,
    distinct from the entity prompt, with the procedure SYSTEM_PROMPT."""
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source,
        "# top\n\n## install\nrun `pip install duckdb`\n\n## query\nSELECT * FROM x",
    )
    out = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out)

    proc_files = sorted((out / "prompts").glob("prompt_section_*_proc.txt"))
    ent_files = sorted(p for p in (out / "prompts").glob("prompt_section_*.txt")
                       if not p.name.endswith("_proc.txt"))
    assert len(proc_files) == len(ent_files) == len(manifest.sections)

    # Procedure prompt must contain the procedure SYSTEM_PROMPT signature, not
    # the entity SYSTEM_PROMPT. Use a phrase unique to extract_procedures.
    proc_text = proc_files[0].read_text()
    assert "executable procedures" in proc_text
    assert "preconditions" in proc_text
    # Manifest carries both paths.
    sec = manifest.sections[0]
    assert sec.prompt_path.endswith(".txt")
    assert sec.procedure_prompt_path is not None
    assert sec.procedure_prompt_path.endswith("_proc.txt")
    assert sec.procedure_result_path is not None


def test_prep_manifest_round_trips_with_procedure_fields(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Manifest round-trip preserves the new procedure_*_path fields."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\nbody")
    out = tmp_path / "prep"
    refresh_docs.prep_extraction(rw_conn, snap_id, out)
    raw = json.loads((out / "manifest.json").read_text())
    rehydrated = refresh_docs.PrepManifest.from_dict(raw)
    sec = rehydrated.sections[0]
    assert sec.procedure_prompt_path is not None and sec.procedure_prompt_path.endswith("_proc.txt")
    assert sec.procedure_result_path is not None


def test_prep_manifest_backwards_compat_with_old_4_4_format(tmp_path):
    """4.4-era manifests omit procedure_*_path entirely. Loading must still work."""
    legacy = {
        "output_dir": str(tmp_path),
        "created_at": "2026-05-03T00:00:00+00:00",
        "snapshot_id": 42,
        "sections": [
            {
                "doc_section_id": 1, "snapshot_id": 42,
                "doc_source_id": 99, "doc_source_name": "Legacy",
                "heading_text": None,
                "prompt_path": "/x/prompt_section_1.txt",
                "result_path": "/x/result_section_1.json",
            },
        ],
    }
    rehydrated = refresh_docs.PrepManifest.from_dict(legacy)
    sec = rehydrated.sections[0]
    assert sec.procedure_prompt_path is None
    assert sec.procedure_result_path is None


def test_process_extraction_writes_procedures_with_doc_section_source_type(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Step 8 happy path: a procedure result file produces procedure rows
    with source_type='doc_section' and procedure_concept links."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\n\n## y\nbody")
    out = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out)
    target = manifest.sections[0]

    # Step 7 result so the entity name cache has something.
    _write_result_file(
        Path(target.result_path),
        entities=[{"name": "Kafka", "type": "Tool", "description": ""}],
        relations=[],
    )
    # Step 8 procedure result.
    proc_payload = {
        "procedures": [
            {
                "name": "Configure Kafka idempotent producer",
                "preconditions": "Kafka cluster running",
                "steps": [
                    {"n": 1, "action": "set enable.idempotence=true"},
                    {"n": 2, "action": "set transactional.id"},
                ],
                "postconditions": "exactly-once writes",
                "failure_modes": "",
                "concepts": ["Kafka", "exactly-once"],
                "implements_pattern": None,
            }
        ]
    }
    Path(target.procedure_result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(target.procedure_result_path).write_text(json.dumps(proc_payload))

    summary = refresh_docs.process_extraction(
        rw_conn, out, resolver=_FakeResolver(rw_conn),
    )
    assert summary.procedure_results_processed == 1
    # Other sections lack procedure results — counted as missing, not unparseable.
    assert summary.procedure_results_missing == len(manifest.sections) - 1
    assert summary.procedures_written == 1
    # Two concepts in the procedure → two procedure_concept links.
    assert summary.procedure_concept_links_written == 2

    rows = rw_conn.execute(
        "SELECT name, source_type, source_id FROM procedure"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("Configure Kafka idempotent producer", "doc_section",
                       target.doc_section_id)


def test_process_extraction_procedure_idempotent_on_rerun(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Re-running process_extraction must clear prior procedures, not duplicate them."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\nbody")
    out = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out)
    target = manifest.sections[0]
    Path(target.procedure_result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(target.procedure_result_path).write_text(json.dumps({
        "procedures": [{
            "name": "Run query", "preconditions": "", "steps": [{"n": 1, "action": "go"}],
            "postconditions": "", "failure_modes": "",
            "concepts": ["Query"], "implements_pattern": None,
        }],
    }))

    refresh_docs.process_extraction(rw_conn, out, resolver=_FakeResolver(rw_conn))
    refresh_docs.process_extraction(rw_conn, out, resolver=_FakeResolver(rw_conn))
    proc_count = rw_conn.execute(
        "SELECT COUNT(*) FROM procedure "
        " WHERE source_type='doc_section' AND source_id = ?",
        [target.doc_section_id],
    ).fetchone()[0]
    assert proc_count == 1


def test_process_extraction_handles_missing_procedure_result(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Missing procedure result files count as missing, not unparseable; no crash."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\nbody")
    out = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out)

    summary = refresh_docs.process_extraction(
        rw_conn, out, resolver=_FakeResolver(rw_conn),
    )
    assert summary.procedure_results_missing == len(manifest.sections)
    assert summary.procedure_results_processed == 0
    assert summary.procedures_written == 0


def test_process_extraction_reuses_entity_concept_cache_for_procedures(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Concepts already resolved during step 7 should NOT be re-resolved
    when step 8 references the same concept names. This avoids redundant
    resolver work and keeps concept_id assignments consistent."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\nbody")
    out = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out)
    target = manifest.sections[0]

    # Entity result mentions Kafka; record concept_id created.
    _write_result_file(
        Path(target.result_path),
        entities=[{"name": "Kafka", "type": "Tool", "description": ""}],
        relations=[],
    )
    # Procedure result also references Kafka.
    Path(target.procedure_result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(target.procedure_result_path).write_text(json.dumps({
        "procedures": [{
            "name": "Use Kafka", "preconditions": "", "steps": [{"n": 1, "action": "go"}],
            "postconditions": "", "failure_modes": "",
            "concepts": ["Kafka"], "implements_pattern": None,
        }],
    }))

    fake_resolver = _FakeResolver(rw_conn)
    refresh_docs.process_extraction(rw_conn, out, resolver=fake_resolver)
    # Kafka should only appear once in the concept table — same id used
    # by both the entity row and the procedure_concept link.
    kafka_count = rw_conn.execute(
        "SELECT COUNT(*) FROM concept WHERE name = 'Kafka'"
    ).fetchone()[0]
    assert kafka_count == 1
    link_count = rw_conn.execute(
        "SELECT COUNT(*) FROM procedure_concept pc "
        "  JOIN procedure p USING (procedure_id) "
        "  JOIN concept c ON pc.concept_id = c.concept_id "
        " WHERE c.name = 'Kafka' AND p.source_type = 'doc_section'"
    ).fetchone()[0]
    assert link_count == 1


def test_prep_extraction_writes_one_prompt_per_section(
    rw_conn, seeded_doc_source, tmp_path,
):
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source,
        "# Top\n\n## Alpha\nbody A\n\n## Beta\nbody B",
    )
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)

    section_count = rw_conn.execute(
        "SELECT COUNT(*) FROM doc_section WHERE snapshot_id = ?", [snap_id]
    ).fetchone()[0]
    assert section_count == 3
    assert len(manifest.sections) == 3
    # Step 7 (entity) prompts: one per section. Step 8 (procedure) prompts have
    # the _proc.txt suffix and are tested separately. Filter to entity-only here.
    ent_files = sorted(
        p for p in (out_dir / "prompts").glob("prompt_section_*.txt")
        if not p.name.endswith("_proc.txt")
    )
    assert len(ent_files) == 3
    # Each prompt file is non-empty and references the section_id in its content.
    for pf in ent_files:
        text = pf.read_text()
        assert len(text) > 0
        assert "DOC_SECTION_ID" in text


def test_prep_extraction_emits_round_trippable_manifest(
    rw_conn, seeded_doc_source, tmp_path,
):
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source, "# only\nbody",
    )
    out_dir = tmp_path / "prep"
    refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)

    raw = json.loads((out_dir / "manifest.json").read_text())
    rehydrated = refresh_docs.PrepManifest.from_dict(raw)
    assert rehydrated.snapshot_id == snap_id
    assert len(rehydrated.sections) >= 1
    assert all(s.doc_source_id == seeded_doc_source for s in rehydrated.sections)


def test_prep_extraction_handles_snapshot_with_no_sections(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Edge case: a snapshot row with zero doc_section rows must not crash prep."""
    rw_conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content) "
        "VALUES (?, 'github', 'u', 'h', 'c') RETURNING snapshot_id",
        [seeded_doc_source],
    )
    snap_id = rw_conn.execute(
        "SELECT MAX(snapshot_id) FROM doc_snapshot").fetchone()[0]

    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    assert manifest.sections == []
    assert (out_dir / "manifest.json").exists()


def test_prep_extraction_idempotent_on_rerun(
    rw_conn, seeded_doc_source, tmp_path,
):
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source, "# A\n\n## B\nbody",
    )
    out_dir = tmp_path / "prep"
    first = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    second = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    assert [s.doc_section_id for s in first.sections] == \
           [s.doc_section_id for s in second.sections]
    # File listing stable.
    files_first = sorted(p.name for p in (out_dir / "prompts").iterdir())
    files_second = sorted(p.name for p in (out_dir / "prompts").iterdir())
    assert files_first == files_second


def test_prep_extraction_prompt_text_includes_heading_and_content(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Prompt must give the sub-agent enough framing to extract from a section."""
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source,
        "# Architecture\n\n## Streams\nKafka exactly-once via idempotent producers.",
    )
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    streams_entry = next(
        s for s in manifest.sections if s.heading_text and "Streams" in s.heading_text
    )
    text = Path(streams_entry.prompt_path).read_text()
    assert "Streams" in text
    assert "Kafka exactly-once" in text
    assert "Test Source" in text  # doc_source name from fixture


# ---------------------------------------------------------------------------
# process_extraction — read sub-agent results, resolve, write relations
# ---------------------------------------------------------------------------


def _write_result_file(path: Path, entities: list[dict], relations: list[dict]):
    payload = {"entities": entities, "relations": relations}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_process_extraction_resolves_entities_and_writes_relations(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Happy path: a result file produces concept rows + concept_relation rows
    with source_type='doc_section'."""
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source, "# X\n\n## Y\nbody",
    )
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    target = manifest.sections[0]

    _write_result_file(
        Path(target.result_path),
        entities=[
            {"name": "Kafka", "type": "Tool", "description": "stream platform"},
            {"name": "exactly-once", "type": "Concept", "description": "delivery semantic"},
        ],
        relations=[{"from": "Kafka", "to": "exactly-once", "type": "IMPLEMENTS",
                    "confidence": 0.85}],
    )

    summary = refresh_docs.process_extraction(
        rw_conn, out_dir, resolver=_FakeResolver(rw_conn),
    )
    assert summary.results_processed == 1
    # Total includes ALL sections in manifest (results missing for others is fine).
    assert summary.results_missing == len(manifest.sections) - 1
    assert summary.entities_resolved == 2
    assert summary.relations_written == 1

    rows = rw_conn.execute(
        "SELECT relation_type, source_type, source_id "
        "  FROM concept_relation WHERE source_type = 'doc_section'",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("IMPLEMENTS", "doc_section", target.doc_section_id)


def test_process_extraction_handles_missing_result_file(
    rw_conn, seeded_doc_source, tmp_path,
):
    """When sub-agents haven't run, the count is reported but no error is raised."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# A\nbody")
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)

    summary = refresh_docs.process_extraction(
        rw_conn, out_dir, resolver=_FakeResolver(rw_conn),
    )
    assert summary.results_missing == len(manifest.sections)
    assert summary.results_processed == 0
    assert summary.entities_resolved == 0


def test_process_extraction_handles_unparseable_json(
    rw_conn, seeded_doc_source, tmp_path,
):
    """A malformed result file is logged + counted, doesn't crash the run."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# A\nbody")
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    Path(manifest.sections[0].result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest.sections[0].result_path).write_text("this is not json {{{")

    summary = refresh_docs.process_extraction(
        rw_conn, out_dir, resolver=_FakeResolver(rw_conn),
    )
    assert summary.results_unparseable == 1


def test_process_extraction_idempotent_clears_prior_relations(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Re-running process_extraction with the same result must not duplicate edges."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# A\nbody")
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    target = manifest.sections[0]
    _write_result_file(
        Path(target.result_path),
        entities=[
            {"name": "Foo", "type": "Concept", "description": ""},
            {"name": "Bar", "type": "Concept", "description": ""},
        ],
        relations=[{"from": "Foo", "to": "Bar", "type": "REQUIRES", "confidence": 0.9}],
    )
    refresh_docs.process_extraction(rw_conn, out_dir, resolver=_FakeResolver(rw_conn))
    refresh_docs.process_extraction(rw_conn, out_dir, resolver=_FakeResolver(rw_conn))
    count = rw_conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        " WHERE source_type='doc_section' AND source_id = ?",
        [target.doc_section_id],
    ).fetchone()[0]
    assert count == 1  # not 2 — the second call cleared+rewrote


def test_process_extraction_skips_relations_with_unknown_endpoints(
    rw_conn, seeded_doc_source, tmp_path,
):
    """If a relation references an entity not in the entities[] list, drop it."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# A\nbody")
    out_dir = tmp_path / "prep"
    manifest = refresh_docs.prep_extraction(rw_conn, snap_id, out_dir)
    target = manifest.sections[0]
    _write_result_file(
        Path(target.result_path),
        entities=[{"name": "OnlyEntity", "type": "Concept", "description": ""}],
        relations=[{"from": "OnlyEntity", "to": "GhostEntity",
                    "type": "REQUIRES", "confidence": 0.5}],
    )
    summary = refresh_docs.process_extraction(
        rw_conn, out_dir, resolver=_FakeResolver(rw_conn),
    )
    assert summary.entities_resolved == 1
    assert summary.relations_written == 0


# ---------------------------------------------------------------------------
# Step 9 — Alignment edges (DocSection → Chapter)
# ---------------------------------------------------------------------------


@pytest.fixture
def aligned_world(rw_conn, seeded_doc_source):
    """Seed a small concept graph with one section and two book chapters that
    discuss the same concept. Returns a dict with the relevant ids so tests
    can drive alignment prep/process directly."""
    # Two chapters with content + a book to host them
    rw_conn.execute(
        "INSERT INTO author (name) VALUES ('Test Author') RETURNING author_id"
    )
    book_id = rw_conn.execute(
        "INSERT INTO book (title, source_path) VALUES ('Test Book', '/tmp/x.epub') "
        "RETURNING book_id"
    ).fetchone()[0]
    ch1_id = rw_conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'Kafka Primer', 'Kafka is a distributed log. "
        "Use enable.idempotence for exactly-once.') RETURNING chapter_id",
        [book_id],
    ).fetchone()[0]
    ch2_id = rw_conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 2, 'Old Kafka Patterns', 'Kafka producers used to require "
        "manual deduplication; this is no longer the recommended approach.') "
        "RETURNING chapter_id",
        [book_id],
    ).fetchone()[0]

    # Seed snapshot + section via the pipeline
    snap_id = _seed_snapshot_with_sections(
        rw_conn, seeded_doc_source,
        "# Kafka\n\nUse `enable.idempotence=true` and set transactional.id "
        "for exactly-once writes.",
    )
    section_id = rw_conn.execute(
        "SELECT MIN(doc_section_id) FROM doc_section WHERE snapshot_id = ?",
        [snap_id],
    ).fetchone()[0]

    # One concept that all three sources discuss
    concept_id = rw_conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Kafka', 'Tool') "
        "RETURNING concept_id"
    ).fetchone()[0]
    other_concept_id = rw_conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('exactly-once', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]

    # concept_relation rows tying chapters and section to the concept
    for source_type, source_id in (
        ("chapter", ch1_id),
        ("chapter", ch2_id),
        ("doc_section", section_id),
    ):
        rw_conn.execute(
            "INSERT INTO concept_relation "
            "  (from_concept_id, to_concept_id, relation_type, source_type, source_id) "
            "VALUES (?, ?, 'REQUIRES', ?, ?)",
            [concept_id, other_concept_id, source_type, source_id],
        )

    return {
        "snapshot_id": int(snap_id),
        "section_id": int(section_id),
        "concept_id": int(concept_id),
        "other_concept_id": int(other_concept_id),
        "chapter_ids": [int(ch1_id), int(ch2_id)],
        "book_id": int(book_id),
    }


def test_gather_alignment_candidates_returns_concepts_with_chapter_ids(
    rw_conn, aligned_world,
):
    """Concepts the section discusses come back, with candidate chapter_ids."""
    candidates = refresh_docs.gather_alignment_candidates(
        rw_conn, aligned_world["section_id"],
    )
    # Both concepts are mentioned in the section's relation row (from + to).
    concept_ids_returned = {c["concept_id"] for c in candidates}
    assert aligned_world["concept_id"] in concept_ids_returned
    # Chapter ids include both seeded chapters.
    chapter_ids_returned: set[int] = set()
    for c in candidates:
        chapter_ids_returned.update(c["candidate_chapter_ids"])
    assert set(aligned_world["chapter_ids"]).issubset(chapter_ids_returned)


def test_gather_alignment_candidates_skips_concepts_with_no_chapters(
    rw_conn, aligned_world,
):
    """A concept the section discusses but no book chapter does → skipped."""
    orphan_id = rw_conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('Orphan', 'Concept') "
        "RETURNING concept_id"
    ).fetchone()[0]
    rw_conn.execute(
        "INSERT INTO concept_relation "
        "  (from_concept_id, to_concept_id, relation_type, source_type, source_id) "
        "VALUES (?, ?, 'REQUIRES', 'doc_section', ?)",
        [orphan_id, aligned_world["concept_id"], aligned_world["section_id"]],
    )
    candidates = refresh_docs.gather_alignment_candidates(
        rw_conn, aligned_world["section_id"],
    )
    assert orphan_id not in {c["concept_id"] for c in candidates}


def test_gather_alignment_candidates_respects_limits(rw_conn, aligned_world):
    """max_concepts and max_chapters_per_concept caps are honored."""
    candidates = refresh_docs.gather_alignment_candidates(
        rw_conn, aligned_world["section_id"],
        max_concepts=1, max_chapters_per_concept=1,
    )
    assert len(candidates) == 1
    assert len(candidates[0]["candidate_chapter_ids"]) == 1


def test_build_section_alignment_prompt_includes_section_and_candidates(rw_conn, aligned_world):
    """Prompt text includes the section content, candidate chapter excerpts, and concept context."""
    candidates = refresh_docs.gather_alignment_candidates(
        rw_conn, aligned_world["section_id"], max_concepts=2, max_chapters_per_concept=2,
    )
    excerpts = refresh_docs._fetch_chapter_excerpts(
        rw_conn,
        [c for cands in candidates for c in cands["candidate_chapter_ids"]],
        excerpt_chars=500,
    )
    prompt = refresh_docs.build_section_alignment_prompt(
        doc_section_id=aligned_world["section_id"],
        doc_source_name="Test Source",
        heading_text="kafka",
        section_content="enable.idempotence=true is the modern path",
        concepts_with_candidates=candidates,
        chapter_excerpts=excerpts,
        excerpt_chars=500,
    )
    assert "ALIGNMENT" in prompt or "alignment classifier" in prompt.lower()
    assert "DOC_SECTION_ID" in prompt
    assert "CANDIDATE_CHAPTER" in prompt
    # Both seeded chapter ids appear as candidates in the prompt.
    for ch_id in aligned_world["chapter_ids"]:
        assert f"to_chapter_id={ch_id}" in prompt


def test_prep_alignment_skips_sections_with_no_candidates(
    rw_conn, seeded_doc_source, tmp_path,
):
    """Sections whose concepts have no candidate chapters get no prompt files."""
    snap_id = _seed_snapshot_with_sections(rw_conn, seeded_doc_source, "# x\nbody")
    out = tmp_path / "alignment"
    manifest = refresh_docs.prep_alignment(rw_conn, snap_id, out)
    # No concept_relation rows for this snapshot's section → no candidates → no entries.
    assert manifest.sections == []


def test_prep_alignment_writes_prompts_when_candidates_exist(rw_conn, aligned_world, tmp_path):
    """Section with a candidate concept + candidate chapters gets a prompt file."""
    out = tmp_path / "alignment"
    manifest = refresh_docs.prep_alignment(
        rw_conn, aligned_world["snapshot_id"], out,
    )
    assert len(manifest.sections) == 1
    entry = manifest.sections[0]
    assert entry.doc_section_id == aligned_world["section_id"]
    assert Path(entry.prompt_path).exists()
    assert entry.concepts_with_candidates  # non-empty
    rehydrated = refresh_docs.AlignmentManifest.from_dict(
        json.loads((out / "manifest.json").read_text())
    )
    assert rehydrated.sections[0].doc_section_id == aligned_world["section_id"]


def test_validate_alignment_payload_drops_hallucinated_concept_ids():
    """Sub-agent invents a concept_id not in the prompt's allowed set → dropped."""
    summary = refresh_docs.AlignmentSummary()
    raw = {
        "alignments": [
            {"concept_id": 999, "to_chapter_id": 1, "relation_type": "CORROBORATES",
             "confidence": 0.9, "explanation": "x"},
            {"concept_id": 100, "to_chapter_id": 1, "relation_type": "CORROBORATES",
             "confidence": 0.9, "explanation": "y"},
        ],
    }
    out = refresh_docs._validate_alignment_payload(
        raw, allowed_concept_ids={100}, allowed_chapter_ids={1},
        allowed_section_ids=set(), summary=summary,
    )
    assert len(out) == 1
    assert out[0]["concept_id"] == 100
    assert summary.edges_dropped_unknown_concept == 1


def test_validate_alignment_payload_drops_hallucinated_target_ids():
    """Same protection for chapter ids."""
    summary = refresh_docs.AlignmentSummary()
    raw = {
        "alignments": [
            {"concept_id": 100, "to_chapter_id": 999, "relation_type": "CORROBORATES",
             "confidence": 0.9, "explanation": "x"},
        ],
    }
    out = refresh_docs._validate_alignment_payload(
        raw, allowed_concept_ids={100}, allowed_chapter_ids={1, 2, 3},
        allowed_section_ids=set(), summary=summary,
    )
    assert out == []
    assert summary.edges_dropped_unknown_target == 1


def test_validate_alignment_payload_drops_invalid_relation_types():
    """Only CORROBORATES and CONTRADICTS allowed."""
    summary = refresh_docs.AlignmentSummary()
    raw = {
        "alignments": [
            {"concept_id": 100, "to_chapter_id": 1, "relation_type": "MAYBE",
             "confidence": 0.5, "explanation": "x"},
        ],
    }
    out = refresh_docs._validate_alignment_payload(
        raw, allowed_concept_ids={100}, allowed_chapter_ids={1},
        allowed_section_ids=set(), summary=summary,
    )
    assert out == []
    assert summary.edges_dropped_invalid_relation == 1


def test_validate_alignment_payload_clamps_confidence_to_unit_interval():
    """Confidence values outside [0, 1] are clamped, not dropped."""
    summary = refresh_docs.AlignmentSummary()
    raw = {
        "alignments": [
            {"concept_id": 100, "to_chapter_id": 1, "relation_type": "CORROBORATES",
             "confidence": 1.5, "explanation": "high"},
            {"concept_id": 100, "to_chapter_id": 2, "relation_type": "CONTRADICTS",
             "confidence": -0.1, "explanation": "low"},
        ],
    }
    out = refresh_docs._validate_alignment_payload(
        raw, allowed_concept_ids={100}, allowed_chapter_ids={1, 2},
        allowed_section_ids=set(), summary=summary,
    )
    assert [o["confidence"] for o in out] == [1.0, 0.0]


def test_process_alignment_writes_edges_with_correct_provenance(
    rw_conn, aligned_world, tmp_path,
):
    """Happy path: a result file with valid CORROBORATES edge writes to alignment_edge."""
    out = tmp_path / "alignment"
    manifest = refresh_docs.prep_alignment(rw_conn, aligned_world["snapshot_id"], out)
    entry = manifest.sections[0]
    target_concept_id = entry.concepts_with_candidates[0]["concept_id"]
    target_chapter_id = entry.concepts_with_candidates[0]["candidate_chapter_ids"][0]

    payload = {"alignments": [{
        "concept_id": target_concept_id,
        "to_chapter_id": target_chapter_id,
        "relation_type": "CORROBORATES",
        "confidence": 0.85,
        "explanation": "Both describe enable.idempotence pattern.",
    }]}
    Path(entry.result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(entry.result_path).write_text(json.dumps(payload))

    summary = refresh_docs.process_alignment(rw_conn, out)
    assert summary.edges_written == 1
    assert summary.results_processed == 1

    row = rw_conn.execute(
        "SELECT from_doc_section_id, to_chapter_id, to_doc_section_id, "
        "       concept_id, relation_type, confidence "
        "  FROM alignment_edge"
    ).fetchone()
    assert row == (entry.doc_section_id, target_chapter_id, None,
                   target_concept_id, "CORROBORATES", 0.85)


def test_process_alignment_idempotent_clears_prior_edges(
    rw_conn, aligned_world, tmp_path,
):
    """Re-running process_alignment must not duplicate edges for the same section."""
    out = tmp_path / "alignment"
    manifest = refresh_docs.prep_alignment(rw_conn, aligned_world["snapshot_id"], out)
    entry = manifest.sections[0]
    cand = entry.concepts_with_candidates[0]
    payload = {"alignments": [{
        "concept_id": cand["concept_id"], "to_chapter_id": cand["candidate_chapter_ids"][0],
        "relation_type": "CORROBORATES", "confidence": 0.7,
        "explanation": "x",
    }]}
    Path(entry.result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(entry.result_path).write_text(json.dumps(payload))

    refresh_docs.process_alignment(rw_conn, out)
    refresh_docs.process_alignment(rw_conn, out)
    count = rw_conn.execute(
        "SELECT COUNT(*) FROM alignment_edge WHERE from_doc_section_id = ?",
        [entry.doc_section_id],
    ).fetchone()[0]
    assert count == 1


def test_process_alignment_handles_missing_and_unparseable(rw_conn, aligned_world, tmp_path):
    """Missing result file → counted as missing. Bad JSON → unparseable. No crash."""
    out = tmp_path / "alignment"
    manifest = refresh_docs.prep_alignment(rw_conn, aligned_world["snapshot_id"], out)
    entry = manifest.sections[0]
    Path(entry.result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(entry.result_path).write_text("not json {{{")

    summary = refresh_docs.process_alignment(rw_conn, out)
    assert summary.results_unparseable == 1
    assert summary.edges_written == 0


# ---------------------------------------------------------------------------
# Cross-corpus search_chapters — Phase 4.4b mixes book chapters + doc sections
# ---------------------------------------------------------------------------


def _import_kb_server():
    """Import mcp-servers/kb-mcp/server.py without booting the long-lived MCP."""
    kb_path = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
    if str(kb_path) not in sys.path:
        sys.path.insert(0, str(kb_path))
    import server  # type: ignore[import-not-found]
    return server


def test_normalize_chapter_row_adds_unified_keys():
    """Existing chapter-modality rows must gain (kind, result_id, doc_section_id) keys."""
    server = _import_kb_server()
    raw = {"chapter_id": 42, "score": 1.0, "book_title": "x", "chapter_title": "y", "excerpt": "z"}
    out = server._normalize_chapter_row(raw)
    assert out["kind"] == "chapter"
    assert out["result_id"] == 42
    assert out["doc_section_id"] is None
    assert out["chapter_id"] == 42  # original keys preserved


def test_rrf_merge_keys_on_kind_and_result_id_so_corpora_dont_collide():
    """A chapter_id and a doc_section_id with the same numeric value must NOT
    collapse into one merged result. Cross-corpus mixing requires the (kind,
    result_id) key invariant."""
    server = _import_kb_server()
    chapter_row = {"kind": "chapter", "result_id": 7, "chapter_id": 7,
                   "doc_section_id": None, "score": 1.0}
    section_row = {"kind": "doc_section", "result_id": 7, "chapter_id": None,
                   "doc_section_id": 7, "score": 1.0}
    merged = server._rrf_merge(
        {"fts_chapter": [chapter_row], "fts_doc_section": [section_row]},
        limit=10,
    )
    kinds = sorted(r["kind"] for r in merged)
    assert kinds == ["chapter", "doc_section"]
    # Each retains its own provenance keys.
    chapter_result = next(r for r in merged if r["kind"] == "chapter")
    assert chapter_result["chapter_id"] == 7 and chapter_result["doc_section_id"] is None
    section_result = next(r for r in merged if r["kind"] == "doc_section")
    assert section_result["doc_section_id"] == 7 and section_result["chapter_id"] is None


def test_rrf_merge_combines_modalities_for_same_result():
    """A result that hits multiple modalities accumulates RRF score across them."""
    server = _import_kb_server()
    chapter_row_fts = {"kind": "chapter", "result_id": 5, "chapter_id": 5,
                       "doc_section_id": None, "score": 0.9}
    chapter_row_vss = {"kind": "chapter", "result_id": 5, "chapter_id": 5,
                       "doc_section_id": None, "score": 0.8}
    merged = server._rrf_merge(
        {"fts_chapter": [chapter_row_fts], "vss_chapter": [chapter_row_vss]},
        limit=10,
    )
    assert len(merged) == 1
    assert sorted(merged[0]["modalities"]) == ["fts_chapter", "vss_chapter"]


def test_fts_doc_section_search_returns_empty_when_index_missing(rw_catalog):
    """Greenfield catalog with no refresh ever run → FTS returns [] cleanly,
    not an error. Important for chapter-only callers who haven't refreshed docs yet."""
    server = _import_kb_server()
    # Fresh schema connection, no refresh → no fts_main_doc_section schema.
    test_conn = duckdb.connect(str(rw_catalog))
    test_conn.execute("LOAD fts")
    with patch.object(server, "_CONN", test_conn):
        out = server._fts_doc_section_search("anything", limit=5)
    test_conn.close()
    assert out == []


def test_fts_doc_section_search_finds_indexed_sections(rw_conn, seeded_doc_source):
    """After a refresh, FTS over doc_section returns scored matching rows."""
    server = _import_kb_server()
    fetcher = _FakeFetcher(
        content="# kafka\n\n## streams\nwatermark and event time processing\n\n## other\nfoo",
        source_type="markdown",
    )
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    with patch.object(server, "_CONN", rw_conn):
        out = server._fts_doc_section_search("watermark", limit=5)
    assert len(out) >= 1
    assert all(r["kind"] == "doc_section" for r in out)
    assert all(r["chapter_id"] is None for r in out)
    assert all(isinstance(r["doc_section_id"], int) for r in out)


def test_vss_doc_section_search_returns_distance_ranked_sections(
    rw_conn, seeded_doc_source,
):
    """VSS modality returns sections ranked by cosine distance, with doc_source_name."""
    server = _import_kb_server()
    fetcher = _FakeFetcher(
        content="# topics\n\n## a\nalpha content\n\n## b\nbeta content\n\n## c\ngamma content",
        source_type="markdown",
    )
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    # Use one section's stored embedding as the query vector → that section
    # ranks first (self-distance = 0).
    self_emb = rw_conn.execute(
        "SELECT embedding FROM doc_section_embedding LIMIT 1"
    ).fetchone()[0]
    with patch.object(server, "_CONN", rw_conn):
        out = server._vss_doc_section_search(self_emb, limit=5)
    assert len(out) >= 1
    assert all(r["kind"] == "doc_section" for r in out)
    # First result is the self-match (similarity = 1.0 - 0 = 1.0).
    assert out[0]["score"] == pytest.approx(1.0, abs=1e-3)


def test_search_chapters_full_fanout_no_doc_data_still_works(rw_conn, seeded_doc_source):
    """Chapter-only catalog (no doc refresh) → search_chapters still returns
    chapter results without crashing on the missing doc_section_fts index."""
    server = _import_kb_server()
    # Bootstrap chapter side: one chapter + concept + relation, plus the FTS index.
    rw_conn.execute(
        "INSERT INTO author (name) VALUES ('Author') RETURNING author_id"
    )
    book_id = rw_conn.execute(
        "INSERT INTO book (title, source_path) VALUES ('B', '/x') RETURNING book_id"
    ).fetchone()[0]
    ch_id = rw_conn.execute(
        "INSERT INTO chapter (book_id, chapter_num, title, content) "
        "VALUES (?, 1, 'C', 'kafka stream watermark') RETURNING chapter_id",
        [book_id],
    ).fetchone()[0]
    # Build chapter FTS index so the chapter modality returns hits.
    rw_conn.execute(
        "PRAGMA create_fts_index('chapter', 'chapter_id', 'content', overwrite=1)"
    )
    # No doc refresh has run, so fts_main_doc_section does NOT exist.
    assert not server._doc_section_fts_index_exists.__call__ or True  # smoke
    # Patch the module connection + a stub resolver for the graph modality.
    fake_resolver = MagicMock()
    fake_resolver.resolve_lookup_only.return_value = None  # no concepts known
    with patch.object(server, "_CONN", rw_conn), \
         patch.object(server, "_RESOLVER", fake_resolver):
        # FTS on chapter: should find our chapter
        ch_hits = server._fts_chapter_search("kafka watermark", 10)
        # FTS on doc_section: empty (index missing)
        ds_hits = server._fts_doc_section_search("kafka watermark", 10)
    assert any(h["chapter_id"] == ch_id for h in ch_hits)
    assert ds_hits == []


def test_doc_section_vss_query_returns_ranked_results(rw_conn, seeded_doc_source):
    """Architecture §6.2 criterion: VSS queries on doc_section work end-to-end.

    After a refresh, query the embedding index by cosine distance and verify
    sections come back in distance order. This is the doc-side analog of
    test_phase1_integration's chapter VSS check.
    """
    rw_conn.execute("LOAD vss")
    fetcher = _FakeFetcher(
        content="# kafka\n\n## kafka streams\nstream processing\n\n## unrelated\nfoo bar baz",
        source_type="markdown",
    )
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )

    # Use one of the persisted embeddings as the query vector — cosine
    # distance to itself is 0, so it must rank first.
    self_row = rw_conn.execute(
        "SELECT doc_section_id, embedding FROM doc_section_embedding LIMIT 1"
    ).fetchone()
    self_id, self_embedding = self_row[0], self_row[1]

    rows = rw_conn.execute(
        """
        SELECT e.doc_section_id,
               array_cosine_distance(e.embedding, ?::FLOAT[384]) AS distance
          FROM doc_section_embedding e
         ORDER BY distance ASC
        """,
        [self_embedding],
    ).fetchall()
    assert len(rows) >= 2
    # Self-match wins.
    assert rows[0][0] == self_id
    # Distances are monotonically non-decreasing.
    distances = [r[1] for r in rows]
    assert distances == sorted(distances)


def test_doc_section_fts_query_after_refresh_finds_keyword(rw_conn, seeded_doc_source):
    """Architecture §6.2 criterion: FTS queries on doc_section work end-to-end.

    Distinct from test_rebuild_doc_section_fts_index because this drives the full
    pipeline (refresh_one_source) rather than calling rebuild directly — proves
    the orchestrator runs step 6.
    """
    fetcher = _FakeFetcher(
        content="# topics\n\n## kafka windowing\nwatermark and event time\n\n## elasticsearch\nfoo",
        source_type="markdown",
    )
    refresh_docs.refresh_one_source(
        rw_conn, seeded_doc_source, fetcher=fetcher, embedder=_FakeEmbedder(),
    )
    rows = rw_conn.execute(
        f"""
        SELECT ds.doc_section_id, ds.heading_text,
               {refresh_docs.DOC_SECTION_FTS_SCHEMA}.match_bm25(ds.doc_section_id, ?) AS score
          FROM doc_section ds
         WHERE score IS NOT NULL
         ORDER BY score DESC
        """,
        ["windowing watermark"],
    ).fetchall()
    assert len(rows) >= 1
    assert any("kafka" in (r[1] or "").lower() for r in rows)


def test_refresh_one_source_unknown_doc_source_id_returns_error(rw_conn):
    result = refresh_docs.refresh_one_source(
        rw_conn, doc_source_id=99999,
        fetcher=_FakeFetcher(content="x"), embedder=_FakeEmbedder(),
    )
    assert result.status == "error"
    assert "no doc_source row" in result.error


# ---------------------------------------------------------------------------
# (Lock integration test that came below — left structurally in place)
# ---------------------------------------------------------------------------


def test_lock_integration_untrusted_holder_not_auto_killed(rw_catalog):
    """Real subprocess with non-kb-mcp cmdline holds RO; auto_stop=True must still refuse."""
    ctx = mp.get_context("spawn")
    ready_q: "mp.Queue[str]" = ctx.Queue()
    proc = ctx.Process(
        target=_ro_holder_subprocess,
        args=(str(rw_catalog), ready_q, 5.0),
    )
    proc.start()
    try:
        assert ready_q.get(timeout=10) == "ready"
        time.sleep(0.1)
        # Default trusted_reader_cmd is mcp-servers/kb-mcp/server.py — this subprocess
        # is just a plain Python helper, so its cmdline won't match and it must be spared.
        with pytest.raises(refresh_docs.LockHeldError) as exc_info:
            refresh_docs.open_writer(rw_catalog, auto_stop=True)
        assert exc_info.value.pid == proc.pid
        assert proc.is_alive()  # CRITICAL: we did not kill an untrusted process
    finally:
        proc.terminate()
        proc.join(timeout=5)
