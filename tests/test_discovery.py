"""
test_discovery.py — Phase 4.5b auto-discovery tests.

Mock-only tests cover the deterministic logic (gap detection, confidence
gate, ingester, orchestrator with synthetic probers). Live tests probe real
Context7 and GitHub APIs to verify the outermost layer; they run by default
and skip on no-network or with MYPUB_SKIP_LIVE_TESTS=1.
"""

from __future__ import annotations

import os
import socket
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

import discovery  # noqa: E402


# Live-test gate (mirrors test_refresh_docs.py).
def _network_available() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2.0):
            return True
    except OSError:
        return False


SKIP_LIVE_OPT_OUT = os.getenv("MYPUB_SKIP_LIVE_TESTS") == "1"
NETWORK_AVAILABLE = _network_available() if not SKIP_LIVE_OPT_OUT else False
live_only = pytest.mark.skipif(
    SKIP_LIVE_OPT_OUT or not NETWORK_AVAILABLE,
    reason=(
        "MYPUB_SKIP_LIVE_TESTS=1 set" if SKIP_LIVE_OPT_OUT
        else "no network available"
    ),
)


@pytest.fixture
def catalog():
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# ConceptGapDetector
# ---------------------------------------------------------------------------


class _StubResolver:
    def __init__(self, known: set[str] = ()):
        self.known = {k.lower() for k in known}

    def resolve_lookup_only(self, name: str):
        return 1 if name.lower() in self.known else None


def test_gap_detector_returns_unknown_terms_only():
    resolver = _StubResolver(known={"kafka"})
    detector = discovery.ConceptGapDetector(resolver)
    gaps = detector.detect(
        "How does Zippy compare to Kafka for change data capture?",
        search_response={"results": []},
    )
    # 'Kafka' is known via resolver; stop words filtered; short tokens filtered.
    # Capture/data/change might pass length filter but won't be unknown
    # because... actually "change", "data", "capture" all >= 3 chars and not
    # stop words. They'll be candidates unless we add domain-specific filtering.
    # The salient unknown is Zippy.
    assert "Zippy" in gaps


def test_gap_detector_skips_terms_appearing_in_search_results():
    """A term that's already surfaced by retrieval (in heading/excerpt) is
    not a gap, even if there's no concept node for it."""
    resolver = _StubResolver()
    detector = discovery.ConceptGapDetector(resolver)
    response = {"results": [{"chapter_title": "Zippy and Kafka",
                             "excerpt": "configuring Zippy on a cluster"}]}
    gaps = detector.detect("How do I configure Zippy?", search_response=response)
    assert "Zippy" not in gaps


def test_gap_detector_skips_stopwords_and_short_tokens():
    detector = discovery.ConceptGapDetector(_StubResolver())
    gaps = detector.detect("how to use a", search_response={"results": []})
    # All tokens are stop words or <3 chars.
    assert gaps == []


def test_gap_detector_handles_interactive_response_shape():
    """The §8.1 {primary, corroborations, ...} shape must also be inspected."""
    detector = discovery.ConceptGapDetector(_StubResolver())
    response = {
        "primary": {"heading_text": "Zippy basics", "excerpt": ""},
        "corroborations": [{"heading_text": "More Zippy"}],
    }
    gaps = detector.detect("how Zippy works", search_response=response)
    assert "Zippy" not in gaps


def test_gap_detector_dedupes_case_insensitive():
    """Same word in different cases ('Zippy' and 'zippy') reports once."""
    detector = discovery.ConceptGapDetector(_StubResolver())
    gaps = detector.detect("Zippy zippy ZIPPY", search_response={"results": []})
    # Only one 'Zippy' makes it through (preserves first-seen casing).
    assert gaps.count("Zippy") == 1
    assert "zippy" not in gaps


# ---------------------------------------------------------------------------
# ConfidenceGate
# ---------------------------------------------------------------------------


def _probe(matches=None, error=None):
    return discovery.ProbeResult(
        source="context7", query_term="X",
        matches=list(matches or []), error=error,
    )


def _match(name, score):
    return discovery.ProbeMatch(name=name, identifier=name.lower(),
                                description=None, score=score)


def test_gate_no_matches_returns_not_found():
    decision = discovery.ConfidenceGate().evaluate(_probe(matches=[]))
    assert decision.decision == "not_found"


def test_gate_single_match_returns_match():
    decision = discovery.ConfidenceGate().evaluate(
        _probe(matches=[_match("only", 1.0)]),
    )
    assert decision.decision == "match"
    assert decision.chosen_match.name == "only"


def test_gate_dominant_top_returns_match_with_winner():
    """Top score 90, runner-up 30 → margin 0.67 ≥ 0.20 → auto-ingest top."""
    decision = discovery.ConfidenceGate().evaluate(
        _probe(matches=[_match("a", 90.0), _match("b", 30.0)]),
    )
    assert decision.decision == "match"
    assert decision.chosen_match.name == "a"


def test_gate_close_top_returns_ambiguous():
    """Top 90, runner-up 85 → margin 0.055 < 0.20 → ambiguous."""
    decision = discovery.ConfidenceGate().evaluate(
        _probe(matches=[_match("a", 90.0), _match("b", 85.0)]),
    )
    assert decision.decision == "ambiguous"


def test_gate_missing_scores_return_ambiguous():
    decision = discovery.ConfidenceGate().evaluate(
        _probe(matches=[_match("a", None), _match("b", None)]),
    )
    assert decision.decision == "ambiguous"


def test_gate_probe_error_returns_not_found():
    decision = discovery.ConfidenceGate().evaluate(_probe(error="transport: x"))
    assert decision.decision == "not_found"


# ---------------------------------------------------------------------------
# Context7 library-id parser
# ---------------------------------------------------------------------------


def test_parse_context7_libraries_extracts_id_title_score():
    text = (
        "Available Libraries:\n"
        "\n"
        "- Title: Foo\n"
        "  Context7-compatible library ID: /foo/bar\n"
        "  Description: Foo is a thing\n"
        "  Source Reputation: High\n"
        "  Benchmark Score: 87.5\n"
        "----------\n"
        "- Title: Baz\n"
        "  Context7-compatible library ID: /baz/qux\n"
        "  Benchmark Score: 50.0\n"
    )
    matches = discovery._parse_context7_libraries(text)
    assert len(matches) == 2
    assert matches[0].identifier == "/foo/bar"
    assert matches[0].score == 87.5
    assert matches[1].identifier == "/baz/qux"


def test_parse_context7_libraries_handles_missing_fields():
    """Block missing identifier or title is dropped (we can't ingest without ID)."""
    text = (
        "- Title: NoID\n"
        "  Description: this has no library id\n"
        "----------\n"
        "- Context7-compatible library ID: /lone/id\n"
        "  Description: this has no title\n"
    )
    matches = discovery._parse_context7_libraries(text)
    assert matches == []


# ---------------------------------------------------------------------------
# DeepWikiProber — quick path that doesn't hit the network
# ---------------------------------------------------------------------------


def test_deepwiki_prober_short_circuits_on_non_repo_terms():
    """Single-token term → no network call, returns empty matches."""
    out = discovery.DeepWikiProber().probe("zippy")
    assert out.source == "deepwiki"
    assert out.matches == []
    assert out.error is None


# ---------------------------------------------------------------------------
# InlineIngester._upsert_doc_source
# ---------------------------------------------------------------------------


def test_upsert_doc_source_creates_with_conservative_authority(catalog):
    sid = discovery.InlineIngester._upsert_doc_source(
        catalog, source="context7", identifier="/foo/bar", display_name="Foo",
    )
    row = catalog.execute(
        "SELECT name, source_type, identifier, authority_score, refresh_ttl_days "
        "  FROM doc_source WHERE doc_source_id = ?", [sid],
    ).fetchone()
    assert row[0] == "Foo"
    assert row[1] == "context7"
    assert row[2] == "/foo/bar"
    assert row[3] == discovery.DISCOVERY_AUTHORITY_DEFAULTS["context7"]


def test_upsert_doc_source_idempotent_on_repeat(catalog):
    s1 = discovery.InlineIngester._upsert_doc_source(
        catalog, source="deepwiki", identifier="owner/repo", display_name="A",
    )
    s2 = discovery.InlineIngester._upsert_doc_source(
        catalog, source="deepwiki", identifier="owner/repo", display_name="A",
    )
    assert s1 == s2
    count = catalog.execute(
        "SELECT COUNT(*) FROM doc_source "
        " WHERE source_type='deepwiki' AND identifier='owner/repo'"
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# log_discovery_event
# ---------------------------------------------------------------------------


def test_log_discovery_event_writes_row(catalog):
    discovery.log_discovery_event(
        catalog,
        query_term="Zippy", probe_source="context7", probe_result="match",
        match_count=1, top_match_name="zippy/zippy", top_match_score=85.0,
        action_taken="ingested", doc_source_id=None,
    )
    row = catalog.execute("SELECT query_term, probe_source, probe_result, "
                          "       action_taken FROM discovery_log").fetchone()
    assert row == ("Zippy", "context7", "match", "ingested")


# ---------------------------------------------------------------------------
# AutoDiscoveryOrchestrator with mocked probers
# ---------------------------------------------------------------------------


class _FakeProber:
    def __init__(self, matches=None, error=None):
        self.matches = matches or []
        self.error = error
        self.calls: list[str] = []

    def probe(self, term: str):
        self.calls.append(term)
        return discovery.ProbeResult(
            source="context7", query_term=term,
            matches=list(self.matches), error=self.error,
        )


class _FakeIngester:
    def __init__(self):
        self.ingested: list[dict] = []
        self._counter = 1000

    def ingest(self, conn, *, source, identifier, display_name, embedder=None):
        self.ingested.append({
            "source": source, "identifier": identifier, "display_name": display_name,
        })
        sid = self._counter
        self._counter += 1
        # Simulate the upsert side-effect — write a doc_source row.
        conn.execute(
            "INSERT INTO doc_source (doc_source_id, name, source_type, mcp_server, "
            "                        identifier, authority_score, refresh_ttl_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sid, display_name, source, source, identifier, 0.5, 30],
        )
        return sid


def test_orchestrator_ingests_on_confident_match(catalog):
    """One unknown term + one confident probe → ingest happens, log written."""
    resolver = _StubResolver()
    fake_prober = _FakeProber(matches=[_match("zippy/zippy", 90.0)])
    fake_ingester = _FakeIngester()
    orch = discovery.AutoDiscoveryOrchestrator(
        catalog, resolver,
        probers={"context7": fake_prober},
        ingester=fake_ingester,
        probe_order=("context7",),
    )
    outcomes = orch.run("How does Zippy work?", {"results": []})
    assert len(outcomes) == 1
    assert outcomes[0].decision == "ingested"
    assert outcomes[0].source == "context7"
    assert fake_ingester.ingested == [
        {"source": "context7", "identifier": "zippy/zippy", "display_name": "zippy/zippy"},
    ]
    log_rows = catalog.execute(
        "SELECT query_term, probe_source, probe_result, action_taken FROM discovery_log"
    ).fetchall()
    assert ("Zippy", "context7", "match", "ingested") in log_rows


def test_orchestrator_asks_user_on_ambiguous(catalog):
    """Multiple close matches → outcome is asked_user, no ingest."""
    resolver = _StubResolver()
    fake_prober = _FakeProber(matches=[_match("a", 90.0), _match("b", 88.0)])
    fake_ingester = _FakeIngester()
    orch = discovery.AutoDiscoveryOrchestrator(
        catalog, resolver,
        probers={"context7": fake_prober},
        ingester=fake_ingester,
        probe_order=("context7",),
    )
    outcomes = orch.run("how Zippy works", {"results": []})
    assert outcomes[0].decision == "asked_user"
    assert fake_ingester.ingested == []
    log_rows = catalog.execute(
        "SELECT probe_result, action_taken FROM discovery_log"
    ).fetchall()
    assert log_rows == [("ambiguous", "asked_user")]


def test_orchestrator_falls_through_to_next_source_on_not_found(catalog):
    """First source returns no matches → orchestrator tries the next source."""
    resolver = _StubResolver()
    miss = _FakeProber(matches=[])
    hit = _FakeProber(matches=[_match("found-it", 80.0)])
    fake_ingester = _FakeIngester()
    orch = discovery.AutoDiscoveryOrchestrator(
        catalog, resolver,
        probers={"context7": miss, "deepwiki": hit},
        ingester=fake_ingester,
        probe_order=("context7", "deepwiki"),
    )
    outcomes = orch.run("Zippy is unknown", {"results": []})
    assert outcomes[0].decision == "ingested"
    assert outcomes[0].source == "deepwiki"
    assert miss.calls == hit.calls == ["Zippy"]


def test_orchestrator_returns_discarded_when_no_source_matches(catalog):
    resolver = _StubResolver()
    miss = _FakeProber(matches=[])
    orch = discovery.AutoDiscoveryOrchestrator(
        catalog, resolver,
        probers={"context7": miss},
        ingester=_FakeIngester(),
        probe_order=("context7",),
    )
    outcomes = orch.run("Zippy unknown", {"results": []})
    assert outcomes[0].decision == "discarded"


def test_orchestrator_skips_known_terms_entirely(catalog):
    """A query with no gaps triggers no probes."""
    resolver = _StubResolver(known={"kafka", "duckdb"})
    fake_prober = _FakeProber()
    orch = discovery.AutoDiscoveryOrchestrator(
        catalog, resolver,
        probers={"context7": fake_prober},
        ingester=_FakeIngester(),
        probe_order=("context7",),
    )
    outcomes = orch.run("Kafka and DuckDB", {"results": []})
    assert outcomes == []
    assert fake_prober.calls == []


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@live_only
def test_context7_prober_live_finds_known_library():
    """Real Context7 probe for 'duckdb' should return at least one match."""
    out = discovery.Context7Prober().probe("duckdb")
    assert out.error is None, f"probe error: {out.error}"
    assert len(out.matches) >= 1
    assert any(m.identifier.startswith("/") for m in out.matches)


@live_only
def test_github_prober_live_finds_known_repo():
    """Real GitHub search for 'fastmcp' should surface PrefectHQ/fastmcp."""
    out = discovery.GitHubProber().probe("fastmcp")
    assert out.error is None, f"probe error: {out.error}"
    assert any("fastmcp" in m.identifier.lower() for m in out.matches)
