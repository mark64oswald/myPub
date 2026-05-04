"""
test_kb_server_search.py — kb-mcp/server.py search_chapters integration tests.

Covers:
    * _is_thin_retrieval — gate that fires auto-discovery
    * _temporarily_open_writer — Topology A close/reopen for in-process writes
    * search_chapters auto_discover behavior — disabled, not-thin (skipped),
      and thin (orchestrator runs, ingests, re-fans-out)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
SCRIPTS = PROJECT_ROOT / "scripts"

for p in (KB_MCP, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import server  # noqa: E402
import discovery  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_catalog(tmp_path):
    """Empty v2 catalog file. Tests that exercise the writer context need a
    real on-disk file because :memory: connections are tied to the process
    and don't recreate cleanly across close/open in this shape."""
    db_path = tmp_path / "catalog.ddb"
    conn = duckdb.connect(str(db_path))
    conn.execute(SCHEMA_FILE.read_text())
    conn.close()
    return db_path


@pytest.fixture
def server_state(fresh_catalog, monkeypatch):
    """Initialize server module-level globals against the temp catalog
    without paying the sentence-transformers load cost. Yields the catalog
    path; tests can call into server functions directly afterward."""
    # Point the bootstrap at the temp catalog.
    monkeypatch.setenv("MYPUB_CATALOG", str(fresh_catalog))

    # Replace SentenceTransformer with a stub that returns deterministic
    # 384-dim vectors. _bootstrap calls SentenceTransformer(...); avoid the
    # heavy network-fetch by patching the class reference inside server's
    # importable scope.
    class _StubModel:
        def encode(self, texts, *, convert_to_numpy=True):
            import numpy as np
            return np.array([[0.1] * 384 for _ in texts], dtype="float32")

    fake_st_module = SimpleNamespace(SentenceTransformer=lambda _name: _StubModel())
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_module)

    # Reset module globals so _bootstrap actually fires.
    monkeypatch.setattr(server, "_CONN", None, raising=False)
    monkeypatch.setattr(server, "_RESOLVER", None, raising=False)
    monkeypatch.setattr(server, "_MODEL", None, raising=False)

    server._bootstrap()
    yield fresh_catalog


# ---------------------------------------------------------------------------
# _is_thin_retrieval
# ---------------------------------------------------------------------------


def test_is_thin_retrieval_true_when_all_keyword_buckets_empty():
    assert server._is_thin_retrieval([], [], [], []) is True


def test_is_thin_retrieval_false_when_chapter_fts_has_hits():
    assert server._is_thin_retrieval([{"chapter_id": 1}], [], [], []) is False


def test_is_thin_retrieval_false_when_doc_section_fts_has_hits():
    assert server._is_thin_retrieval([], [{"doc_section_id": 1}], [], []) is False


def test_is_thin_retrieval_false_when_graph_has_hits():
    """Either chapter or doc_section graph hits should suppress thinness."""
    assert server._is_thin_retrieval([], [], [{"chapter_id": 1}], []) is False
    assert server._is_thin_retrieval([], [], [], [{"doc_section_id": 1}]) is False


def test_is_thin_retrieval_ignores_vss():
    """VSS isn't an argument — only FTS + graph signals contribute. (Sanity
    test for the documented behavior: VSS always returns near-neighbors so
    it can't be used as a thinness signal without false negatives.)"""
    # Same as the all-empty case — VSS doesn't appear in the call.
    assert server._is_thin_retrieval([], [], [], []) is True


# ---------------------------------------------------------------------------
# _temporarily_open_writer (Topology A close/reopen)
# ---------------------------------------------------------------------------


def test_temporarily_open_writer_yields_rw_connection(server_state):
    """Inside the context, a write to doc_source must succeed (RO would error)."""
    with server._temporarily_open_writer() as rw_conn:
        rw_conn.execute(
            "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
            "VALUES ('test', 'github', 'github', 'a/b')"
        )
        # Verify the insert is visible WITHIN the writer context.
        count = rw_conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0]
        assert count == 1


def test_temporarily_open_writer_reopens_ro_after_exit(server_state):
    """After the context exits, the global _CONN is a fresh RO connection,
    and the resolver is rebuilt against it."""
    pre_conn = server._CONN
    with server._temporarily_open_writer() as rw_conn:
        # During writer, _CONN is None.
        assert server._CONN is None
        assert server._RESOLVER is None
        rw_conn.execute(
            "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
            "VALUES ('post', 'github', 'github', 'x/y')"
        )
    # After exit, _CONN is reopened — fresh object, not the same as pre.
    assert server._CONN is not None
    assert server._CONN is not pre_conn
    assert server._RESOLVER is not None
    # The newly-inserted row is visible from the reopened RO connection.
    count = server._CONN.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0]
    assert count == 1


def test_temporarily_open_writer_reopens_ro_even_on_exception(server_state):
    """Exceptions inside the writer must not leave _CONN in a half-closed state."""
    with pytest.raises(RuntimeError, match="boom"):
        with server._temporarily_open_writer():
            raise RuntimeError("boom")
    assert server._CONN is not None
    assert server._RESOLVER is not None


# ---------------------------------------------------------------------------
# search_chapters auto-discovery integration
# ---------------------------------------------------------------------------


def test_search_chapters_skips_discovery_when_auto_discover_false(server_state):
    """auto_discover=False bypasses the orchestrator entirely."""
    fake_fanout = ([], [], [], [], [], [])
    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server, "_run_auto_discovery") as mock_run, \
         patch.object(server.ranking, "InteractiveRanker"):
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        result = server.search_chapters("Zippy", auto_discover=False)
    mock_run.assert_not_called()
    assert result["discovery"] == []


def test_search_chapters_skips_discovery_when_results_are_not_thin(server_state):
    """If FTS or graph found anything, don't waste a probe."""
    # Stub the modality fan-out to return one FTS hit (so retrieval isn't thin)
    # and patch _run_auto_discovery to track whether it was called.
    fake_fanout = (
        [{"kind": "chapter", "result_id": 1, "rrf_score": 0.5,
          "chapter_id": 1, "doc_section_id": None, "score": 0.5}],  # fts_chapter
        [], [], [], [], [],  # vss_chapter, graph_chapter, fts/vss/graph_section
    )
    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server, "_run_auto_discovery") as mock_run, \
         patch.object(server.ranking, "InteractiveRanker"):
        # Make ranker return empty-but-valid output so the rest of the function
        # finishes cleanly.
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        server.search_chapters("kafka watermark")
    mock_run.assert_not_called()


def test_search_chapters_runs_discovery_when_results_are_thin(server_state):
    """All-empty FTS + graph triggers _run_auto_discovery."""
    fake_fanout = ([], [], [], [], [], [])
    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server, "_run_auto_discovery", return_value=[]) as mock_run, \
         patch.object(server.ranking, "InteractiveRanker"):
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        server.search_chapters("Zippy", auto_discover=True)
    mock_run.assert_called_once()


def test_search_chapters_re_fans_out_after_successful_ingest(server_state):
    """If discovery returns at least one 'ingested' outcome, the modality
    fan-out runs a second time so the new content is included in ranking."""
    fake_fanout = ([], [], [], [], [], [])
    ingested_outcome = [{"query_term": "Zippy", "decision": "ingested",
                         "source": "context7", "doc_source_id": 42,
                         "chosen_match": None, "candidates": [], "note": ""}]

    with patch.object(server, "_run_modality_fanout",
                      return_value=fake_fanout) as mock_fanout, \
         patch.object(server, "_run_auto_discovery",
                      return_value=ingested_outcome), \
         patch.object(server.ranking, "InteractiveRanker"):
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        server.search_chapters("Zippy")
    assert mock_fanout.call_count == 2  # initial + post-ingest re-fanout


def test_search_chapters_does_not_re_fanout_when_only_asked_user(server_state):
    """An ambiguous outcome (asked_user) doesn't trigger re-fanout — there's
    nothing new in the corpus to find."""
    fake_fanout = ([], [], [], [], [], [])
    asked_outcome = [{"query_term": "spark", "decision": "asked_user",
                      "source": "context7", "doc_source_id": None,
                      "chosen_match": None, "candidates": [], "note": ""}]

    with patch.object(server, "_run_modality_fanout",
                      return_value=fake_fanout) as mock_fanout, \
         patch.object(server, "_run_auto_discovery",
                      return_value=asked_outcome), \
         patch.object(server.ranking, "InteractiveRanker"):
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        server.search_chapters("spark")
    assert mock_fanout.call_count == 1


def test_search_chapters_surfaces_discovery_outcomes_in_response(server_state):
    """The 'discovery' field in the response carries the orchestrator's
    per-term outcomes so callers (Claude Code) can show the user what
    happened."""
    fake_fanout = ([], [], [], [], [], [])
    outcomes = [{"query_term": "Zippy", "decision": "ingested",
                 "source": "context7", "doc_source_id": 99,
                 "chosen_match": {"name": "z", "identifier": "/z/z"},
                 "candidates": [], "note": "single match"}]

    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server, "_run_auto_discovery", return_value=outcomes), \
         patch.object(server.ranking, "InteractiveRanker"):
        server.ranking.InteractiveRanker.return_value.rank.return_value = (
            server.ranking.InteractiveOutput(primary=None)
        )
        result = server.search_chapters("Zippy")
    assert result["discovery"] == outcomes


# ---------------------------------------------------------------------------
# _run_auto_discovery — runs orchestrator inside writer context
# ---------------------------------------------------------------------------


def test_run_auto_discovery_uses_writer_context(server_state):
    """_run_auto_discovery must open a writer connection (so InlineIngester
    can write doc_source rows). Verified by stubbing the orchestrator and
    checking that a write succeeded."""
    captured = {"writer_used": False}

    class _StubOrchestrator:
        def __init__(self, conn, resolver, *, embedder=None):
            self.conn = conn

        def run(self, query, search_response):
            # Verify we got an RW conn: writes succeed.
            self.conn.execute(
                "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                "VALUES ('via_orch', 'github', 'github', 'a/b')"
            )
            captured["writer_used"] = True
            return []

    with patch.object(discovery, "AutoDiscoveryOrchestrator", _StubOrchestrator):
        server._run_auto_discovery("Zippy", {"results": []})

    assert captured["writer_used"]
    # Row persisted past writer-context exit.
    count = server._CONN.execute(
        "SELECT COUNT(*) FROM doc_source WHERE name = 'via_orch'"
    ).fetchone()[0]
    assert count == 1
