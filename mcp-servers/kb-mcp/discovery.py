"""
discovery.py — Phase 4.5b auto-discovery (architecture §5.4).

When a hybrid-retrieval query returns no useful results AND contains terms
that don't resolve to any existing concept, we don't immediately give up:
we probe the live doc source stack for matching libraries/repos, gate on
confidence, and inline-ingest if a confident match exists. Then re-run
retrieval with the new content in the corpus.

Pipeline:

    GapDetector         → which query terms are completely unknown?
    Prober (per source) → does Context7 / DeepWiki / GitHub know this term?
    ConfidenceGate      → exactly one strong match? auto-ingest. Multiple? ask.
    InlineIngester      → register doc_source + run refresh_one_source pipeline.

Every probe attempt logs a discovery_log row regardless of outcome — the log
is the audit trail for confidence-gate tuning in Phase 4.6 eval.

Conservative authority scoring per arch §5.4 step 4:

    Context7: 0.60   (vs 0.90 for explicit registrations)
    DeepWiki: 0.50   (vs 0.75)
    GitHub:   0.40   (vs 0.65)

Lower defaults until the content proves its value through actual use.

Cost model: this module never calls the Anthropic API. All "LLM" reasoning
in auto-discovery is delegated to the source MCPs, which return structured
candidate lists; ConfidenceGate is rule-based.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

LOG = logging.getLogger("mypub-discovery")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# --- Source-specific defaults from arch §5.4 step 4 -------------------------

DISCOVERY_AUTHORITY_DEFAULTS: dict[str, float] = {
    "context7": 0.60,
    "deepwiki": 0.50,
    "github":   0.40,
}

DISCOVERY_REFRESH_TTL_DAYS: dict[str, int] = {
    "context7": 30,
    "deepwiki": 30,
    "github":   30,
}


# --- Data classes ------------------------------------------------------------


@dataclass
class ProbeMatch:
    """One candidate match returned by a Prober."""

    name: str                      # human-readable display name
    identifier: str                # source-specific ID (e.g., '/duckdb/duckdb')
    description: Optional[str]
    score: Optional[float]         # source-side relevance, if returned


@dataclass
class ProbeResult:
    """The outcome of probing one source for one query term."""

    source: str                    # 'context7' | 'deepwiki' | 'github'
    query_term: str
    matches: list[ProbeMatch] = field(default_factory=list)
    error: Optional[str] = None    # transport / parse failure if any


@dataclass
class GateDecision:
    """ConfidenceGate verdict for one ProbeResult."""

    decision: str                  # 'match' | 'ambiguous' | 'not_found'
    chosen_match: Optional[ProbeMatch] = None
    reason: str = ""


@dataclass
class IngestOutcome:
    """Final auto-discovery outcome surfaced to the caller."""

    query_term: str
    decision: str                  # 'ingested' | 'asked_user' | 'discarded'
    source: Optional[str] = None
    doc_source_id: Optional[int] = None
    chosen_match: Optional[ProbeMatch] = None
    candidates: list[ProbeMatch] = field(default_factory=list)
    note: str = ""


# --- Step 1: ConceptGapDetector ---------------------------------------------


# Tokens stripped before gap analysis: stop words and operator-like tokens that
# are never library names. Keep this list focused — over-aggressive filtering
# can hide real gaps.
_GAP_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for", "from",
    "has", "have", "how", "in", "into", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "use", "using", "what", "when", "where", "why",
    "with", "without", "would", "can", "could", "does", "i", "we", "you", "your",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]+")
# Library/repo names typically: start with uppercase OR contain a digit /
# dot / underscore / hyphen. Pure lowercase English words like "configure"
# or "install" almost never qualify, which keeps the discovery probes from
# burning rate limits on common verbs that show up in every query.
_LIBRARY_NAME_HINT_RE = re.compile(r"[A-Z]|[0-9._-]")


class ConceptGapDetector:
    """Identifies query terms with no matches in any retrieval modality.

    A gap is a token (or capitalized n-gram) from the query that:
      * doesn't resolve to an existing concept (lookup-only),
      * doesn't appear in any FTS hit's heading_text or chapter_title,
      * isn't a stop word.

    The detector takes the assembled search_chapters response so it can
    cross-reference the actual hits — terms that DO appear in retrieved
    content are treated as known even without a concept node.
    """

    def __init__(self, resolver: Any):
        # Resolver only needs `.resolve_lookup_only(name) -> Optional[int]`.
        self.resolver = resolver

    def detect(self, query: str, search_response: dict[str, Any]) -> list[str]:
        """Return the unknown terms from ``query`` not surfaced by retrieval."""
        candidates = self._candidates(query)
        if not candidates:
            return []

        known_terms = self._terms_in_results(search_response)

        unknown: list[str] = []
        seen: set[str] = set()
        for term in candidates:
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            # Already discoverable through retrieval → not a gap.
            if any(term.lower() in known.lower() for known in known_terms):
                continue
            # Already in the concept graph → not a gap.
            if self.resolver.resolve_lookup_only(term) is not None:
                continue
            unknown.append(term)
        return unknown

    @staticmethod
    def _candidates(query: str) -> list[str]:
        """Tokens that *look like* library/repo names — Capitalized or with
        digits / underscores / dots / hyphens. Lowercase English words are
        deliberately excluded; the resolver and FTS already handle them, and
        treating them as discovery candidates wastes rate-limited MCP calls."""
        tokens = _TOKEN_RE.findall(query)
        out: list[str] = []
        for t in tokens:
            if t.lower() in _GAP_STOPWORDS:
                continue
            if len(t) < 3:
                continue
            if not _LIBRARY_NAME_HINT_RE.search(t):
                continue
            out.append(t)
        return out

    @staticmethod
    def _terms_in_results(search_response: dict[str, Any]) -> list[str]:
        """Pull human-readable strings out of any of the result shapes
        search_chapters might return (legacy flat or §8.1 interactive)."""
        terms: list[str] = []
        for r in search_response.get("results") or []:
            terms.extend(_text_signals_from_row(r))
        primary = search_response.get("primary")
        if primary:
            terms.extend(_text_signals_from_row(primary))
        for r in search_response.get("corroborations") or []:
            terms.extend(_text_signals_from_row(r))
        return terms


def _text_signals_from_row(r: dict[str, Any]) -> list[str]:
    fields = ("book_title", "chapter_title", "doc_source_name", "heading_text",
              "excerpt")
    out: list[str] = []
    for f in fields:
        v = r.get(f)
        if isinstance(v, str) and v:
            out.append(v)
    return out


# --- Step 2: Probers ---------------------------------------------------------


class Context7Prober:
    """Probes Context7 via ``resolve-library-id`` to find matching libraries.

    If ``CONTEXT7_API_KEY`` is set in the environment, it's appended as
    ``--api-key <key>`` to the npx args. Anonymous usage hits a low
    monthly quota; an API key (free tier sufficient for our scale)
    raises that. The MCP server reads CONTEXT7_API_KEY natively too,
    but only if the environment is propagated through stdio_client —
    passing it as a CLI arg is the reliable path.
    """

    NPX_COMMAND = "npx"
    NPX_ARGS = ("-y", "@upstash/context7-mcp")
    TOOL_NAME = "resolve-library-id"

    def probe(self, query_term: str) -> ProbeResult:
        return asyncio.run(self._probe_async(query_term))

    async def _probe_async(self, query_term: str) -> ProbeResult:
        args = list(self.NPX_ARGS)
        api_key = os.environ.get("CONTEXT7_API_KEY")
        if api_key:
            args.extend(["--api-key", api_key])
        params = StdioServerParameters(
            command=self.NPX_COMMAND, args=args,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        self.TOOL_NAME,
                        {"libraryName": query_term, "query": query_term},
                    )
        except Exception as e:
            return ProbeResult(source="context7", query_term=query_term,
                               error=f"transport: {e}")

        text = _extract_mcp_text(result)
        matches = _parse_context7_libraries(text)
        return ProbeResult(source="context7", query_term=query_term, matches=matches)


class DeepWikiProber:
    """Probes DeepWiki via ``read_wiki_structure`` for owner/repo guesses.

    DeepWiki doesn't expose a search endpoint — its tool surface is
    repo-keyed. Strategy:

      1. If the term is repo-shaped (contains '/'), try it directly.
      2. Otherwise try the canonical "{term}/{term}" convention — many
         well-known projects use this pattern (redis/redis, facebook/facebook,
         prefecthq/prefecthq, langchain-ai/langchain). One extra round-trip
         per probe; usually catches the canonical home repo. If that misses,
         report not-found and let the orchestrator fall through to the next
         source (typically GitHub search, which CAN find by name).
    """

    HTTP_URL = "https://mcp.deepwiki.com/mcp"
    TOOL_NAME = "read_wiki_structure"

    def probe(self, query_term: str) -> ProbeResult:
        candidates = self._candidate_repos(query_term)
        for candidate in candidates:
            result = asyncio.run(self._probe_one(candidate))
            if result.error or result.matches:
                # Either we hit a real match, or a transport-level error worth
                # surfacing. Either way, stop probing further candidates.
                return result
        # Every candidate came back empty (no transport errors).
        return ProbeResult(source="deepwiki", query_term=query_term, matches=[])

    @staticmethod
    def _candidate_repos(query_term: str) -> list[str]:
        """Build the ordered list of owner/repo candidates to try."""
        if "/" in query_term:
            return [query_term]
        # Canonical "name/name" convention — try lowercase since GitHub repo
        # paths are case-insensitive but DeepWiki may return more relevant
        # content for the canonical owner casing. We try the original first
        # (handles cases like 'PrefectHQ' that may have a 'PrefectHQ/PrefectHQ')
        # then lowercase as a fallback.
        out = [f"{query_term}/{query_term}"]
        lower = query_term.lower()
        if lower != query_term:
            out.append(f"{lower}/{lower}")
        return out

    async def _probe_one(self, repo_name: str) -> ProbeResult:
        try:
            async with streamable_http_client(self.HTTP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        self.TOOL_NAME, {"repoName": repo_name},
                    )
        except Exception as e:
            return ProbeResult(source="deepwiki", query_term=repo_name,
                               error=f"transport: {e}")

        text = _extract_mcp_text(result)
        if not text or "not found" in text.lower() or "error" in text.lower():
            return ProbeResult(source="deepwiki", query_term=repo_name, matches=[])
        # DeepWiki returned a wiki structure → treat as a confident match.
        return ProbeResult(
            source="deepwiki", query_term=repo_name,
            matches=[ProbeMatch(
                name=repo_name, identifier=repo_name,
                description=_first_nonblank_line(text), score=1.0,
            )],
        )


class GitHubProber:
    """Probes GitHub via the public search API for repos matching the term.

    Last resort — only used when Context7 and DeepWiki both miss. The public
    search endpoint has aggressive rate limits for unauthenticated callers,
    so we issue at most one call per probe and cap the candidate list small.
    """

    SEARCH_URL = "https://api.github.com/search/repositories"
    HTTP_TIMEOUT_S = 30.0
    PER_PAGE = 5

    def probe(self, query_term: str) -> ProbeResult:
        try:
            response = httpx.get(
                self.SEARCH_URL,
                params={"q": query_term, "sort": "stars", "per_page": self.PER_PAGE},
                headers={"Accept": "application/vnd.github+json"},
                timeout=self.HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as e:
            return ProbeResult(source="github", query_term=query_term,
                               error=f"transport: {e}")
        if response.status_code >= 400:
            return ProbeResult(
                source="github", query_term=query_term,
                error=f"HTTP {response.status_code}",
            )

        try:
            payload = response.json()
        except ValueError as e:
            return ProbeResult(source="github", query_term=query_term,
                               error=f"json: {e}")

        matches = []
        for item in payload.get("items", [])[:self.PER_PAGE]:
            full_name = item.get("full_name") or ""
            stars = item.get("stargazers_count")
            description = item.get("description") or ""
            if full_name:
                matches.append(ProbeMatch(
                    name=full_name, identifier=full_name,
                    description=description,
                    score=float(stars) if stars else None,
                ))
        return ProbeResult(source="github", query_term=query_term, matches=matches)


def _extract_mcp_text(result: Any) -> str:
    pieces: list[str] = []
    for item in (getattr(result, "content", None) or []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "\n".join(pieces)


def _first_nonblank_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _parse_context7_libraries(text: str) -> list[ProbeMatch]:
    """Parse Context7 resolve-library-id text output into ProbeMatch entries.

    Context7 returns blocks separated by lines of dashes; each block has
    'Title:', 'Context7-compatible library ID:', 'Description:', and
    'Benchmark Score:' fields. Robust to missing fields and order changes.
    """
    matches: list[ProbeMatch] = []
    for block in (b.strip() for b in text.split("----------") if b.strip()):
        title: Optional[str] = None
        identifier: Optional[str] = None
        description: Optional[str] = None
        score: Optional[float] = None
        for line in block.splitlines():
            stripped = line.strip().lstrip("- ").strip()
            if stripped.startswith("Title:"):
                title = stripped[len("Title:"):].strip()
            elif stripped.startswith("Context7-compatible library ID:"):
                identifier = stripped[len("Context7-compatible library ID:"):].strip()
            elif stripped.startswith("Description:"):
                description = stripped[len("Description:"):].strip()
            elif stripped.startswith("Benchmark Score:"):
                try:
                    score = float(stripped[len("Benchmark Score:"):].strip())
                except ValueError:
                    pass
        if identifier and title:
            matches.append(ProbeMatch(
                name=title, identifier=identifier,
                description=description, score=score,
            ))
    return matches


# --- Step 3: ConfidenceGate --------------------------------------------------


# A "clear winner" needs to outscore the runner-up by this much (0..1 fraction).
DEFAULT_DOMINANCE_MARGIN = 0.20

# Per-source minimum score for the single-match auto-ingest path. Source
# scores are not on a comparable scale:
#   Context7 — 0–100 relevance score (vendor-curated; below 65 = weak match)
#   DeepWiki — 1.0 indicator (presence = high confidence by construction)
#   GitHub   — stargazer count (1000 stars ≈ established project)
# When the SOLE match returned by a probe falls below the source's floor,
# we downgrade to ``ambiguous`` rather than auto-ingest. The asked_user
# path lets the human override and accept the weak match deliberately.
#
# Why this exists: a nonsense query ("xyzzy_qwerty_nonsense_42") once
# triggered a Context7 single-match return of score 53.3, which the gate
# accepted blindly and ingested into the catalog. A score floor makes
# the single-match rule honest about what counts as confident.
SINGLE_MATCH_SCORE_FLOOR: dict[str, float] = {
    "context7": 65.0,
    "deepwiki": 0.5,
    "github":   1000.0,
}


def _meets_single_match_floor(match: "ProbeMatch", source: str) -> bool:
    """True if the single-match score clears the source's confidence floor."""
    floor = SINGLE_MATCH_SCORE_FLOOR.get(source)
    if floor is None:
        # Unknown source — accept (don't block on missing config).
        return True
    if match.score is None:
        # No score from the source — can't verify, defer to user.
        return False
    return float(match.score) >= floor


class ConfidenceGate:
    """Decides whether a ProbeResult warrants auto-ingest, ask-user, or discard.

    Rules (per arch §5.4 step 3, updated 2026-05):
      * Zero matches → not_found.
      * Exactly one match clearing the source's confidence floor → match
        (auto-ingest). Below the floor → ambiguous (ask the user).
      * Multiple matches, top dominates by ≥ ``dominance_margin`` → match
        (auto-ingest the dominant one). Same floor applies — a dominant-
        but-weak match is still ambiguous.
      * Multiple matches with similar scores → ambiguous.

    The score-floor was added after a nonsense query inadvertently
    ingested a low-scoring single-match (Context7 score 53.3). The §5.4
    spec is explicit: "The default for ambiguity is to ask, not to
    ingest. This keeps the knowledge base clean."
    """

    def __init__(self, *, dominance_margin: float = DEFAULT_DOMINANCE_MARGIN):
        self.dominance_margin = dominance_margin

    def evaluate(self, probe: ProbeResult) -> GateDecision:
        if probe.error:
            return GateDecision(decision="not_found",
                                reason=f"probe error: {probe.error}")
        if not probe.matches:
            return GateDecision(decision="not_found", reason="no matches")
        if len(probe.matches) == 1:
            only = probe.matches[0]
            if not _meets_single_match_floor(only, probe.source):
                return GateDecision(
                    decision="ambiguous", chosen_match=None,
                    reason=(
                        f"single match but score "
                        f"{only.score!r} below "
                        f"{probe.source} floor "
                        f"{SINGLE_MATCH_SCORE_FLOOR.get(probe.source)!r}"
                    ),
                )
            return GateDecision(decision="match", chosen_match=only,
                                reason="single match")

        # Multiple matches: rank by score (None scores tie at -inf).
        ranked = sorted(
            probe.matches,
            key=lambda m: m.score if m.score is not None else float("-inf"),
            reverse=True,
        )
        top, runner_up = ranked[0], ranked[1]
        if top.score is None or runner_up.score is None:
            return GateDecision(decision="ambiguous",
                                reason="missing scores; cannot compare")
        if top.score <= 0:
            return GateDecision(decision="ambiguous", reason="top score not positive")
        margin = (top.score - runner_up.score) / top.score
        if margin >= self.dominance_margin:
            # Dominant — but the absolute score still has to clear the floor.
            # Otherwise we'd auto-ingest "the best of a bad lot" for a query
            # the corpus simply doesn't have.
            if not _meets_single_match_floor(top, probe.source):
                return GateDecision(
                    decision="ambiguous", chosen_match=None,
                    reason=(
                        f"dominant (margin={margin:.2f}) but top score "
                        f"{top.score!r} below {probe.source} floor "
                        f"{SINGLE_MATCH_SCORE_FLOOR.get(probe.source)!r}"
                    ),
                )
            return GateDecision(decision="match", chosen_match=top,
                                reason=f"dominant (margin={margin:.2f})")
        return GateDecision(
            decision="ambiguous",
            reason=f"top score too close to runner-up (margin={margin:.2f})",
        )


# --- Step 4: InlineIngester --------------------------------------------------


class InlineIngester:
    """Registers a doc_source row and runs the refresh_one_source pipeline.

    Caller passes a writable connection (RW) — the pipeline persists a snapshot,
    sections, and embeddings. Returns the new ``doc_source_id`` so callers can
    re-run retrieval with the new content immediately.
    """

    def ingest(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        source: str,                # 'context7' | 'deepwiki' | 'github'
        identifier: str,
        display_name: str,
        embedder: Optional[Any] = None,
    ) -> Optional[int]:
        if source not in DISCOVERY_AUTHORITY_DEFAULTS:
            LOG.warning("InlineIngester: unknown source %r", source)
            return None
        doc_source_id = self._upsert_doc_source(
            conn, source=source, identifier=identifier, display_name=display_name,
        )
        # Run the pipeline; lazy-import to avoid pulling refresh_docs at module load.
        from refresh_docs import refresh_one_source  # type: ignore[import-not-found]
        result = refresh_one_source(
            conn, doc_source_id=doc_source_id, embedder=embedder,
        )
        if result.status == "error":
            LOG.warning(
                "InlineIngester: refresh_one_source error for %s/%s: %s",
                source, identifier, result.error,
            )
            return doc_source_id
        return doc_source_id

    @staticmethod
    def _upsert_doc_source(
        conn: duckdb.DuckDBPyConnection,
        *, source: str, identifier: str, display_name: str,
    ) -> int:
        """Insert if absent, return existing id otherwise. Conservative authority."""
        existing = conn.execute(
            "SELECT doc_source_id FROM doc_source "
            " WHERE source_type = ? AND identifier = ?",
            [source, identifier],
        ).fetchone()
        if existing:
            return int(existing[0])
        row = conn.execute(
            """
            INSERT INTO doc_source
                (name, source_type, mcp_server, identifier,
                 authority_score, refresh_ttl_days)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING doc_source_id
            """,
            [
                display_name, source, source, identifier,
                DISCOVERY_AUTHORITY_DEFAULTS[source],
                DISCOVERY_REFRESH_TTL_DAYS[source],
            ],
        ).fetchone()
        assert row is not None, "RETURNING clause must yield a row"
        return int(row[0])


# --- Logging -----------------------------------------------------------------


def log_discovery_event(
    conn: duckdb.DuckDBPyConnection,
    *,
    query_term: str,
    probe_source: str,
    probe_result: str,
    match_count: int,
    top_match_name: Optional[str],
    top_match_score: Optional[float],
    action_taken: str,
    doc_source_id: Optional[int],
) -> None:
    """Append one discovery_log row. Independent of the probe/gate logic so
    callers can log even on early exits (transport error, etc.)."""
    conn.execute(
        """
        INSERT INTO discovery_log
            (query_term, probe_source, probe_result, match_count,
             top_match_name, top_match_score, action_taken, doc_source_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            query_term, probe_source, probe_result, match_count,
            top_match_name, top_match_score, action_taken, doc_source_id,
        ],
    )


# --- Top-level orchestrator --------------------------------------------------


# Probe order — Context7 first (broadest catalog), DeepWiki second
# (any public GitHub), GitHub last (raw search, rate-limited).
DEFAULT_PROBE_ORDER: tuple[str, ...] = ("context7", "deepwiki", "github")


class AutoDiscoveryOrchestrator:
    """Run the full GapDetect → Probe → Gate → Ingest pipeline for one query.

    The orchestrator is the integration point for kb-mcp/server.py:search_chapters
    when the initial retrieval comes back thin. It logs every probe attempt to
    discovery_log so the eval set can drive confidence-gate tuning.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        *,
        probers: Optional[dict[str, Any]] = None,
        gate: Optional[ConfidenceGate] = None,
        ingester: Optional[InlineIngester] = None,
        embedder: Optional[Any] = None,
        probe_order: Sequence[str] = DEFAULT_PROBE_ORDER,
    ):
        self.conn = conn
        self.resolver = resolver
        self.gate = gate or ConfidenceGate()
        self.ingester = ingester or InlineIngester()
        self.embedder = embedder
        self.probers: dict[str, Any] = probers or {
            "context7": Context7Prober(),
            "deepwiki": DeepWikiProber(),
            "github":   GitHubProber(),
        }
        self.probe_order = tuple(probe_order)

    def run(self, query: str, search_response: dict[str, Any]) -> list[IngestOutcome]:
        """Detect gaps, probe each in source order, return per-term outcomes."""
        gaps = ConceptGapDetector(self.resolver).detect(query, search_response)
        outcomes: list[IngestOutcome] = []
        for term in gaps:
            outcomes.append(self._discover_term(term))
        return outcomes

    def _discover_term(self, term: str) -> IngestOutcome:
        for source in self.probe_order:
            prober = self.probers.get(source)
            if prober is None:
                continue
            probe = prober.probe(term)
            decision = self.gate.evaluate(probe)
            top = decision.chosen_match or (probe.matches[0] if probe.matches else None)
            top_name = top.name if top else None
            top_score = top.score if top else None

            if decision.decision == "match" and decision.chosen_match is not None:
                doc_source_id = self.ingester.ingest(
                    self.conn,
                    source=source,
                    identifier=decision.chosen_match.identifier,
                    display_name=decision.chosen_match.name,
                    embedder=self.embedder,
                )
                log_discovery_event(
                    self.conn, query_term=term, probe_source=source,
                    probe_result="match", match_count=len(probe.matches),
                    top_match_name=top_name, top_match_score=top_score,
                    action_taken="ingested" if doc_source_id else "discarded",
                    doc_source_id=doc_source_id,
                )
                return IngestOutcome(
                    query_term=term, decision="ingested",
                    source=source, doc_source_id=doc_source_id,
                    chosen_match=decision.chosen_match,
                    candidates=list(probe.matches),
                    note=decision.reason,
                )

            if decision.decision == "ambiguous":
                log_discovery_event(
                    self.conn, query_term=term, probe_source=source,
                    probe_result="ambiguous", match_count=len(probe.matches),
                    top_match_name=top_name, top_match_score=top_score,
                    action_taken="asked_user", doc_source_id=None,
                )
                return IngestOutcome(
                    query_term=term, decision="asked_user",
                    source=source, candidates=list(probe.matches),
                    note=decision.reason,
                )

            # not_found — log and try the next source
            log_discovery_event(
                self.conn, query_term=term, probe_source=source,
                probe_result="not_found", match_count=len(probe.matches),
                top_match_name=top_name, top_match_score=top_score,
                action_taken="discarded", doc_source_id=None,
            )

        # Every source returned not_found.
        return IngestOutcome(
            query_term=term, decision="discarded",
            note="no source had a confident match",
        )
