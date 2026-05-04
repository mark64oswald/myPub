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
# _title_token_coverage — fraction of significant query tokens in the title
# ---------------------------------------------------------------------------


def test_title_coverage_perfect_match_returns_one():
    """Title containing every significant query token → coverage 1.0."""
    out = server._title_token_coverage("Terraform state locking",
                                        "What Is Terraform State Locking")
    assert out == 1.0


def test_title_coverage_partial_match_returns_fraction():
    """2 of 3 significant tokens present → 0.67."""
    out = server._title_token_coverage("Terraform state locking",
                                        "What Is Terraform State?")
    assert out == pytest.approx(2 / 3, abs=1e-6)


def test_title_coverage_no_match_returns_zero():
    out = server._title_token_coverage("Terraform state locking",
                                        "Delta Lake benchmark notes")
    assert out == 0.0


def test_title_coverage_strips_stop_words():
    """Stop words don't count in either query or title; the coverage is
    fraction of NON-STOP query tokens that appear in the title."""
    # query → ["circuit", "breaker", "pattern"], title → "circuit breaker"
    out = server._title_token_coverage("a circuit breaker for the pattern",
                                        "Circuit Breaker")
    assert out == pytest.approx(2 / 3)


def test_title_coverage_handles_none_or_empty_title():
    assert server._title_token_coverage("anything", None) == 0.0
    assert server._title_token_coverage("anything", "") == 0.0


def test_title_coverage_handles_empty_query():
    assert server._title_token_coverage("", "anything") == 0.0
    # All-stopwords query → no significant tokens → coverage is 0
    assert server._title_token_coverage("the and or", "Title Here") == 0.0


def test_title_coverage_case_insensitive():
    out = server._title_token_coverage("CQRS read model", "cqrs Read Model")
    assert out == 1.0


def test_title_coverage_substring_match_for_partial_tokens():
    """Substring matching: 'compaction' in 'LSM Tree Compaction Strategies' counts."""
    out = server._title_token_coverage("LSM tree compaction",
                                        "LSM Tree Compaction Strategies")
    assert out == 1.0


def test_title_coverage_url_headings_score_zero():
    """Doc_section sectionizer falls back to GitHub URLs when source markdown
    lacks clean headings. URL-path substrings (.../terraform/README.md)
    shouldn't earn title boost — that's a filesystem artifact, not a
    meaningful section label."""
    url = "https://github.com/delta-io/delta/blob/master/benchmarks/infrastructure/gcp/terraform/README.md"
    assert server._title_token_coverage("Terraform state locking", url) == 0.0
    # http and www variants too
    assert server._title_token_coverage("kafka", "http://example.com/kafka") == 0.0
    assert server._title_token_coverage("kafka", "www.kafka.org") == 0.0


# ---------------------------------------------------------------------------
# _full_significant_title_match — exact-title-match floor signal
# ---------------------------------------------------------------------------


def test_full_title_match_true_when_all_significant_tokens_in_title():
    """Title contains every significant query token AND query has ≥2 → True."""
    assert server._full_significant_title_match(
        "circuit breaker pattern", "Circuit Breaker Pattern"
    ) is True


def test_full_title_match_partial_coverage_returns_false():
    """Missing one query token → no full match."""
    assert server._full_significant_title_match(
        "Terraform state locking", "What Is Terraform State?"  # missing 'locking'
    ) is False


def test_full_title_match_single_token_query_returns_false():
    """Generic single-word queries don't count: a chapter titled 'Data' shouldn't
    win every query that happens to be the word 'data' alone — too unspecific."""
    assert server._full_significant_title_match("data", "Data") is False
    assert server._full_significant_title_match("introduction", "Introduction") is False


def test_full_title_match_url_heading_returns_false():
    """URL-shaped headings can't earn the floor — filesystem-artifact substrings."""
    url = "https://github.com/delta-io/delta/blob/master/terraform/README.md"
    assert server._full_significant_title_match("Terraform state locking", url) is False


def test_full_title_match_strips_stop_words_from_query():
    """Stop words don't count — a query like 'a circuit breaker for the pattern'
    requires the title to contain 'circuit', 'breaker', 'pattern' (the
    significant tokens) but not 'a', 'for', 'the'."""
    assert server._full_significant_title_match(
        "a circuit breaker for the pattern", "Circuit Breaker Pattern"
    ) is True


def test_full_title_match_handles_none_or_empty():
    assert server._full_significant_title_match("anything two", None) is False
    assert server._full_significant_title_match("anything two", "") is False
    assert server._full_significant_title_match("", "Anything Title") is False


# ---------------------------------------------------------------------------
# _required_acronyms — detects technical-acronym tokens in a query
# ---------------------------------------------------------------------------


def test_required_acronyms_finds_uppercase_tokens():
    assert server._required_acronyms("CQRS read model projection") == ["CQRS"]
    assert server._required_acronyms("FHIR resource versioning") == ["FHIR"]
    assert server._required_acronyms("Kafka HL7 v2 ingestion") == ["HL7"]


def test_required_acronyms_handles_multiple():
    """Multiple acronyms in one query — both must be present in candidates."""
    out = server._required_acronyms("REST API for FHIR")
    assert "REST" in out and "FHIR" in out
    # API is in the blocklist? No — API is a legitimate acronym, must be kept.
    assert "API" in out


def test_required_acronyms_filters_blocklist():
    """Common all-caps stop words must not be treated as acronyms."""
    assert server._required_acronyms("THE OR AND") == []
    # Mixed: real acronym + blocklist word
    assert server._required_acronyms("FOR CQRS THE design") == ["CQRS"]


def test_required_acronyms_natural_language_returns_empty():
    """Most queries have no acronyms — filter is a no-op."""
    assert server._required_acronyms("event sourcing") == []
    assert server._required_acronyms("circuit breaker pattern") == []
    assert server._required_acronyms("kafka consumer group rebalancing") == []


def test_required_acronyms_dedups_preserving_order():
    assert server._required_acronyms("CQRS for REST and CQRS again") == ["CQRS", "REST"]


def test_required_acronyms_ignores_lowercase_tokens():
    """Case matters: 'cqrs' (lowercase) should not be flagged. The user
    must explicitly capitalize for the constraint to apply."""
    assert server._required_acronyms("cqrs read model") == []


def test_acronym_filter_clauses_no_acronyms():
    sql, params = server._acronym_filter_clauses([], "c.content")
    assert sql == ""
    assert params == []


def test_acronym_filter_clauses_builds_ilike_per_acronym():
    sql, params = server._acronym_filter_clauses(["CQRS", "FHIR"], "x.content")
    # One AND clause per acronym, each parameterized
    assert sql.count("ILIKE ?") == 2
    assert "x.content" in sql
    assert params == ["%CQRS%", "%FHIR%"]


# ---------------------------------------------------------------------------
# _clean_excerpt — strips chapter heading boilerplate
# ---------------------------------------------------------------------------


def test_clean_excerpt_strips_chapter_n_heading():
    raw = "Chapter 2.\nFundamentals of Events and Event Streams\nEvent streams are the dominant mode for powerful event-driven systems and are served by an event broker."
    out = server._clean_excerpt(raw)
    assert out.startswith("Event streams")
    assert "Chapter 2." not in out
    assert "Fundamentals of Events" not in out


def test_clean_excerpt_strips_preface_heading():
    raw = "Preface\nDomain-Driven Design in PHP\nIn 2014, after two years of working with DDD, the authors decided to consolidate their learnings."
    out = server._clean_excerpt(raw)
    assert out.startswith("In 2014")


def test_clean_excerpt_strips_digit_only_heading():
    raw = "7\nPrompt Types\nThis chapter covers how systems process structured conversations with distinct message types."
    out = server._clean_excerpt(raw)
    assert out.startswith("This chapter covers")


def test_clean_excerpt_strips_roman_numeral_heading():
    raw = "II\nFrom Circuits to Networks\nIn this section we examine the topology and dynamics of biological circuits as networks."
    out = server._clean_excerpt(raw)
    assert out.startswith("In this section")


def test_clean_excerpt_strips_early_release_preamble():
    raw = (
        "Chapter 1.\nIntroduction: Fundamental Patterns\n"
        "A Note for Early Release Readers\n"
        "With Early Release ebooks, you get books in their earliest form\n"
        "the author's raw and unedited content as they write\n"
        "so you can take advantage of these technologies long before the official release of these titles.\n"
        "Architecture is fundamentally about tradeoffs between competing concerns."
    )
    out = server._clean_excerpt(raw)
    assert out.startswith("Architecture is fundamentally"), \
        f"expected to skip early-release preamble, got: {out[:80]!r}"


def test_clean_excerpt_falls_back_when_cleaning_eats_too_much():
    """If after stripping there's almost nothing left, return the raw prefix.
    Better to show messy content than a useless sliver."""
    raw = "Chapter 1.\nFoo"  # only 2 lines, both heading-shaped
    out = server._clean_excerpt(raw)
    # Cleaning would leave "" — fallback returns the raw prefix.
    assert out  # non-empty
    assert "Chapter 1" in out or "Foo" in out


def test_clean_excerpt_handles_empty_input():
    assert server._clean_excerpt(None) == ""
    assert server._clean_excerpt("") == ""


def test_clean_excerpt_truncates_to_max_chars():
    long = "X" * 5000
    raw = "Chapter 1.\nTitle\n" + long
    out = server._clean_excerpt(raw, max_chars=100)
    assert len(out) == 100


def test_clean_excerpt_passthrough_when_no_heading():
    """Doc sections from the sectionizer don't have chapter headings — the
    cleaner should be a near-no-op for them."""
    raw = "Watermarks track the progress of event time and are used to handle late-arriving data in stream processing systems."
    out = server._clean_excerpt(raw)
    assert out.startswith("Watermarks track")


# ---------------------------------------------------------------------------
# _clean_book_title — strips '(for <name>)' personalization suffix
# ---------------------------------------------------------------------------


def test_clean_book_title_strips_for_name_suffix():
    assert server._clean_book_title("Building Event-Driven Microservices (for Mark Oswald)") \
        == "Building Event-Driven Microservices"


def test_clean_book_title_handles_unsuffixed():
    assert server._clean_book_title("Designing Data-Intensive Applications") \
        == "Designing Data-Intensive Applications"


def test_clean_book_title_handles_none_and_empty():
    assert server._clean_book_title(None) is None
    # Empty string passes through (no-op)
    assert server._clean_book_title("") == ""


def test_clean_book_title_only_strips_personalization_pattern():
    """Don't strip arbitrary parenthetical content — only the '(for <name>)' marker."""
    assert server._clean_book_title("Refactoring (2nd Edition)") \
        == "Refactoring (2nd Edition)"


# ---------------------------------------------------------------------------
# _dedupe_by_content — Phase 1 splitter bug mitigation
# ---------------------------------------------------------------------------


def test_dedupe_by_content_collapses_same_book_same_excerpt():
    """Multiple chapter rows with identical (book_title, excerpt) — symptom of
    the Phase 1 splitter bug — should collapse to one representative."""
    results = [
        {"kind": "chapter", "result_id": 1, "book_title": "B",
         "chapter_title": "Preface", "excerpt": "Lorem ipsum dolor sit amet…",
         "rrf_score": 0.020},
        {"kind": "chapter", "result_id": 2, "book_title": "B",
         "chapter_title": "Why This Book", "excerpt": "Lorem ipsum dolor sit amet…",
         "rrf_score": 0.025},
        {"kind": "chapter", "result_id": 3, "book_title": "B",
         "chapter_title": "Goals", "excerpt": "Lorem ipsum dolor sit amet…",
         "rrf_score": 0.018},
    ]
    out = server._dedupe_by_content(results)
    assert len(out) == 1
    # Highest-rrf representative wins.
    assert out[0]["result_id"] == 2


def test_dedupe_by_content_keeps_different_books_separate():
    results = [
        {"kind": "chapter", "result_id": 1, "book_title": "A",
         "chapter_title": "Intro", "excerpt": "shared text",
         "rrf_score": 0.020},
        {"kind": "chapter", "result_id": 2, "book_title": "B",
         "chapter_title": "Intro", "excerpt": "shared text",
         "rrf_score": 0.018},
    ]
    out = server._dedupe_by_content(results)
    # Two different books, same excerpt — both kept.
    assert len(out) == 2


def test_dedupe_by_content_keeps_different_excerpts():
    results = [
        {"kind": "chapter", "result_id": 1, "book_title": "B",
         "chapter_title": "Intro", "excerpt": "alpha content",
         "rrf_score": 0.020},
        {"kind": "chapter", "result_id": 2, "book_title": "B",
         "chapter_title": "Methods", "excerpt": "beta content",
         "rrf_score": 0.018},
    ]
    out = server._dedupe_by_content(results)
    assert len(out) == 2


def test_dedupe_by_content_passes_doc_sections_through():
    """Doc sections come from the sectionizer which doesn't have the splitter
    bug — they should pass through unchanged even with identical excerpts."""
    results = [
        {"kind": "doc_section", "result_id": 1, "doc_source_name": "DuckDB",
         "excerpt": "same text", "rrf_score": 0.020},
        {"kind": "doc_section", "result_id": 2, "doc_source_name": "DuckDB",
         "excerpt": "same text", "rrf_score": 0.018},
    ]
    out = server._dedupe_by_content(results)
    assert len(out) == 2


def test_dedupe_by_content_preserves_order():
    """Non-dup rows keep their relative order."""
    results = [
        {"kind": "chapter", "result_id": 1, "book_title": "A",
         "excerpt": "a", "rrf_score": 0.020},
        {"kind": "chapter", "result_id": 2, "book_title": "B",
         "excerpt": "b", "rrf_score": 0.018},
        {"kind": "chapter", "result_id": 3, "book_title": "C",
         "excerpt": "c", "rrf_score": 0.015},
    ]
    out = server._dedupe_by_content(results)
    assert [r["result_id"] for r in out] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _is_thin_retrieval
# ---------------------------------------------------------------------------


def test_is_thin_retrieval_true_when_all_keyword_buckets_empty():
    assert server._is_thin_retrieval([], [], [], []) is True


def test_is_thin_retrieval_false_when_chapter_fts_has_strong_hit():
    """Strong BM25 (>= threshold) should suppress thinness."""
    strong = [{"chapter_id": 1, "score": 5.0}]
    assert server._is_thin_retrieval(strong, [], [], []) is False


def test_is_thin_retrieval_false_when_doc_section_fts_has_strong_hit():
    strong = [{"doc_section_id": 1, "score": 4.0}]
    assert server._is_thin_retrieval([], strong, [], []) is False


def test_is_thin_retrieval_true_when_only_weak_fts_hits_and_no_graph():
    """Pattern B regression: novel terms (LangGraph, Marimo, Terraform) get
    weak BM25 matches because BM25 finds any chapter sharing one generic
    token. Without quality threshold, those weak hits would suppress
    discovery and the system would confidently present an irrelevant
    primary. With the threshold, retrieval is correctly classified as
    thin and discovery fires."""
    weak_chapter = [{"chapter_id": 1, "score": 1.5}]
    weak_section = [{"doc_section_id": 2, "score": 1.8}]
    # All-weak with no graph hits → thin.
    assert server._is_thin_retrieval(weak_chapter, weak_section, [], []) is True


def test_is_thin_retrieval_false_when_graph_carries_signal_even_with_weak_fts():
    """If the concept graph has any hit, treat as not-thin even if FTS is
    weak. The corpus knows about a related concept; further discovery
    would just create duplicates."""
    weak = [{"chapter_id": 1, "score": 1.0}]
    graph = [{"chapter_id": 99}]  # graph rows don't carry BM25, just presence
    assert server._is_thin_retrieval(weak, [], graph, []) is False


def test_is_thin_retrieval_false_when_only_graph_has_hits_no_fts():
    """Graph-only matches still suppress thinness."""
    assert server._is_thin_retrieval([], [], [{"chapter_id": 1}], []) is False
    assert server._is_thin_retrieval([], [], [], [{"doc_section_id": 1}]) is False


def test_is_thin_retrieval_threshold_boundary():
    """At BM25 == threshold, treat as not-thin (we have at-least-marginal
    signal); below threshold, thin. Boundary behavior pinned so tuning
    the threshold is observable."""
    just_below = [{"chapter_id": 1, "score": server.THIN_BM25_THRESHOLD - 0.01}]
    at_threshold = [{"chapter_id": 1, "score": server.THIN_BM25_THRESHOLD}]
    assert server._is_thin_retrieval(just_below, [], [], []) is True
    assert server._is_thin_retrieval(at_threshold, [], [], []) is False


def test_is_thin_retrieval_ignores_vss():
    """VSS isn't an argument — only FTS + graph signals contribute. VSS
    always returns near-neighbors so it can't be used as a thinness signal
    without false negatives."""
    assert server._is_thin_retrieval([], [], [], []) is True


# ---------------------------------------------------------------------------
# _has_unknown_library_terms — Pattern B: novel-library detection
# ---------------------------------------------------------------------------


def test_has_unknown_library_terms_true_when_novel_capitalized_token(server_state):
    """A query mentioning a novel library that doesn't resolve to any
    concept should fire discovery, even if other tokens (state / workflow /
    notebook) suppress thin-retrieval via tangential graph hits."""
    prelim = {"results": [
        {"chapter_title": "State Machines for ML Pipelines",
         "excerpt": "state machines and workflow orchestration..."},
    ]}
    # 'LangGraph' isn't a concept in the test catalog, isn't in the result.
    assert server._has_unknown_library_terms("LangGraph state machine workflow", prelim) is True


def test_has_unknown_library_terms_false_for_natural_language(server_state):
    """Natural-language queries with no library-shaped tokens should NOT
    fire discovery — there's nothing to probe."""
    prelim = {"results": []}
    assert server._has_unknown_library_terms("event sourcing", prelim) is False
    assert server._has_unknown_library_terms("circuit breaker pattern", prelim) is False


def test_has_unknown_library_terms_false_when_term_in_results(server_state):
    """If the supposedly-novel library term already appears in retrieved
    content, the corpus knows about it — don't probe."""
    prelim = {"results": [
        {"chapter_title": "Apache Spark structured streaming",
         "excerpt": "Spark DataFrames and the catalyst optimizer..."},
    ]}
    # 'Spark' is in the chapter title → not a gap.
    assert server._has_unknown_library_terms("Spark performance tuning", prelim) is False


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


def test_temporarily_open_writer_restores_ro_when_rw_open_fails(server_state, monkeypatch):
    """If the RW open itself raises, the server still has a working RO
    connection afterward — discovery is best-effort and must not poison
    the long-lived reader."""
    # Patch open_catalog so the RW call inside the context manager raises,
    # but the RO reopen at the end still succeeds.
    real_open = server.open_catalog

    def flaky_open(path, *, read_only):
        if not read_only:
            raise duckdb.IOException("simulated RW lock conflict")
        return real_open(path, read_only=read_only)

    monkeypatch.setattr(server, "open_catalog", flaky_open)
    with pytest.raises(RuntimeError, match="failed to open RW connection"):
        with server._temporarily_open_writer():
            pytest.fail("body should not run when RW open fails")
    # The RO connection must be back, so search still works.
    assert server._CONN is not None
    assert server._RESOLVER is not None
    server._CONN.execute("SELECT 1").fetchone()  # smoke


def test_run_auto_discovery_returns_empty_when_writer_unavailable(server_state, monkeypatch):
    """RW unavailability should degrade gracefully — discovery returns [],
    search_chapters keeps working against the existing corpus."""
    real_open = server.open_catalog

    def flaky_open(path, *, read_only):
        if not read_only:
            raise duckdb.IOException("simulated RW lock conflict")
        return real_open(path, read_only=read_only)

    monkeypatch.setattr(server, "open_catalog", flaky_open)
    outcomes = server._run_auto_discovery("Zippy", {"results": []})
    assert outcomes == []
    # Server is still usable.
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
    """If FTS found something STRONG, don't waste a probe.

    Score must be above THIN_BM25_THRESHOLD — weak FTS matches no longer
    suppress discovery (Pattern B fix)."""
    fake_fanout = (
        [{"kind": "chapter", "result_id": 1, "rrf_score": 0.5,
          "chapter_id": 1, "doc_section_id": None, "score": 5.0}],  # strong fts_chapter
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


def test_search_chapters_feeds_wider_pool_to_ranker_than_final_limit(server_state):
    """The five-factor ranker must see more candidates than ``limit`` so that
    a candidate with mediocre RRF rank but excellent combined score (recent,
    high-authority, well-corroborated) can still surface as primary. We pass
    limit=3; the ranker must receive at least limit*SCORING_POOL_MULTIPLIER
    rows when that many exist."""
    pool_size = 3 * server.SCORING_POOL_MULTIPLIER
    # Build pool_size + 5 fake FTS hits so RRF has plenty to fuse.
    # Each row has a distinct book_title + excerpt so the content-dedup pass
    # doesn't collapse them.
    fts_hits = [
        {"kind": "chapter", "result_id": i, "rrf_score": 1.0 / (i + 1),
         "chapter_id": i, "doc_section_id": None, "score": 1.0 / (i + 1),
         "book_title": f"book {i}", "chapter_title": f"c{i}",
         "excerpt": f"unique content for chapter {i}"}
        for i in range(1, pool_size + 6)
    ]
    fake_fanout = (fts_hits, [], [], [], [], [])
    captured: dict = {}

    class _RecordingRanker:
        def __init__(self, conn, weights, **kwargs):
            pass

        def rank(self, results, **_kwargs):
            captured["pool_size_seen"] = len(results)
            return server.ranking.InteractiveOutput(primary=None)

    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server.ranking, "InteractiveRanker", _RecordingRanker):
        server.search_chapters("kafka", limit=3, auto_discover=False)

    # The ranker must have seen the wider pool, not the final limit.
    assert captured["pool_size_seen"] >= pool_size, (
        f"ranker saw only {captured['pool_size_seen']} candidates with limit=3; "
        f"expected at least {pool_size} (limit × SCORING_POOL_MULTIPLIER)"
    )


def test_search_chapters_generation_mode_with_strategy_calls_generation_ranker(
    server_state,
):
    """selection_strategy='consensus_synthesis' must route through
    GenerationRanker.select(...) and surface dropped-source provenance."""
    fake_fanout = ([], [], [], [], [], [])
    captured = {}

    class _RecordingGen:
        def __init__(self, conn, weights, **kwargs):
            pass

        def select(self, results, *, strategy):
            captured["strategy"] = strategy
            captured["pool_size"] = len(results)
            return server.ranking.GenerationOutput(strategy=strategy)

    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout), \
         patch.object(server.ranking, "GenerationRanker", _RecordingGen):
        result = server.search_chapters(
            "kafka", mode="generation",
            selection_strategy="consensus_synthesis",
            weight_profile="skill_consensus",
            auto_discover=False,
        )
    assert captured["strategy"] == "consensus_synthesis"
    assert result["mode"] == "generation"
    assert result["selection_strategy"] == "consensus_synthesis"
    assert "results" in result and "dropped" in result


def test_search_chapters_generation_mode_without_strategy_returns_sorted_view(
    server_state,
):
    """No strategy → return combined-score-sorted ranking; dropped is []."""
    fake_fanout = ([], [], [], [], [], [])
    with patch.object(server, "_run_modality_fanout", return_value=fake_fanout):
        result = server.search_chapters(
            "kafka", mode="generation", auto_discover=False,
        )
    assert result["mode"] == "generation"
    assert result["selection_strategy"] is None
    assert result["dropped"] == []


def test_search_chapters_rejects_unknown_strategy(server_state):
    """Unknown selection_strategy must raise before any DB work."""
    with pytest.raises(ValueError, match="selection_strategy"):
        server.search_chapters(
            "kafka", mode="generation", selection_strategy="bogus",
            auto_discover=False,
        )


def test_search_chapters_rejects_strategy_in_interactive_mode(server_state):
    """selection_strategy is meaningless without mode='generation'."""
    with pytest.raises(ValueError, match="only valid when mode='generation'"):
        server.search_chapters(
            "kafka", mode="interactive",
            selection_strategy="recent_doc_anchored",
            auto_discover=False,
        )


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


# ---------------------------------------------------------------------------
# disambiguate_discovery — closes the asked_user loop
# ---------------------------------------------------------------------------


class _StubIngester:
    """Drop-in for InlineIngester that writes a doc_source row + a stub
    snapshot/section without invoking refresh_one_source's network pipeline."""

    def __init__(self):
        self.calls = []

    def ingest(self, conn, *, source, identifier, display_name, embedder=None):
        self.calls.append((source, identifier, display_name))
        existing = conn.execute(
            "SELECT doc_source_id FROM doc_source "
            " WHERE source_type = ? AND identifier = ?",
            [source, identifier],
        ).fetchone()
        if existing:
            doc_source_id = int(existing[0])
        else:
            row = conn.execute(
                """
                INSERT INTO doc_source
                    (name, source_type, mcp_server, identifier,
                     authority_score, refresh_ttl_days)
                VALUES (?, ?, ?, ?, 0.5, 30)
                RETURNING doc_source_id
                """,
                [display_name, source, source, identifier],
            ).fetchone()
            doc_source_id = int(row[0])
            snap = conn.execute(
                """
                INSERT INTO doc_snapshot (doc_source_id, source_type, url,
                                          content_hash, content)
                VALUES (?, ?, ?, ?, ?)
                RETURNING snapshot_id
                """,
                [doc_source_id, source, f"https://x/{identifier}", "h", "body"],
            ).fetchone()
            conn.execute(
                "INSERT INTO doc_section (snapshot_id, ordinal, content) "
                "VALUES (?, 0, 'body')",
                [int(snap[0])],
            )
        return doc_source_id


def test_disambiguate_discovery_ingests_user_pick(server_state, monkeypatch):
    """A confirmed pick from an asked_user outcome must persist a doc_source row,
    create a snapshot, and return ingested status with the new IDs."""
    stub = _StubIngester()
    monkeypatch.setattr(discovery, "InlineIngester", lambda: stub)

    result = server.disambiguate_discovery(
        source="context7", identifier="/duckdb/duckdb",
        display_name="DuckDB", query_term="duckdb",
    )
    assert result["status"] == "ingested"
    assert result["source"] == "context7"
    assert result["identifier"] == "/duckdb/duckdb"
    assert isinstance(result["doc_source_id"], int)
    assert result["section_count"] >= 1
    assert stub.calls == [("context7", "/duckdb/duckdb", "DuckDB")]
    # A discovery_log row was written.
    log_count = server._CONN.execute(
        "SELECT COUNT(*) FROM discovery_log WHERE query_term = 'duckdb'"
    ).fetchone()[0]
    assert log_count == 1


def test_disambiguate_discovery_idempotent_on_repeat(server_state, monkeypatch):
    """Calling disambiguate twice with the same source/identifier must report
    'already_present' on the second call and not create a duplicate row."""
    stub = _StubIngester()
    monkeypatch.setattr(discovery, "InlineIngester", lambda: stub)

    first = server.disambiguate_discovery(
        source="deepwiki", identifier="owner/repo",
    )
    assert first["status"] == "ingested"
    second = server.disambiguate_discovery(
        source="deepwiki", identifier="owner/repo",
    )
    assert second["status"] == "already_present"
    assert second["doc_source_id"] == first["doc_source_id"]
    count = server._CONN.execute(
        "SELECT COUNT(*) FROM doc_source "
        " WHERE source_type='deepwiki' AND identifier='owner/repo'"
    ).fetchone()[0]
    assert count == 1


def test_disambiguate_discovery_rejects_unknown_source(server_state):
    with pytest.raises(ValueError, match="source must be one of"):
        server.disambiguate_discovery(source="not_a_source", identifier="x")


def test_disambiguate_discovery_rejects_blank_identifier(server_state):
    with pytest.raises(ValueError, match="identifier must be non-empty"):
        server.disambiguate_discovery(source="context7", identifier="   ")


def test_disambiguate_discovery_defaults_display_name_to_identifier(
    server_state, monkeypatch,
):
    stub = _StubIngester()
    monkeypatch.setattr(discovery, "InlineIngester", lambda: stub)

    result = server.disambiguate_discovery(
        source="github", identifier="redis/redis",
    )
    assert result["display_name"] == "redis/redis"
    assert stub.calls == [("github", "redis/redis", "redis/redis")]


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
