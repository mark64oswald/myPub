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
    prompt_files = sorted((out_dir / "prompts").glob("prompt_section_*.txt"))
    assert len(prompt_files) == 3
    # Each prompt file is non-empty and references the section_id in its content.
    for pf in prompt_files:
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
