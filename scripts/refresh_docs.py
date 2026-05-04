#!/usr/bin/env python3
"""
refresh_docs.py — Snapshot ingestion pipeline for live doc sources.

Implements the §6.2 ingestion workflow: fetch a snapshot from a doc-source
MCP, hash + persist it, sectionize, embed, FTS-index, then prepare prompts
for sub-agent-driven entity / procedure / alignment extraction.

Subcommands::

    refresh   --source-id N | --all | --tier hot
              steps 1–6 inline; emits a manifest under <out>/ for steps 7–9
              add --no-extract to skip the manifest (Phase 4b LaunchAgent path)

    prep      (rare; refresh emits this automatically) — write only the
              prompt manifest for an already-persisted snapshot

    process   --output-dir <path>
              read sub-agent result JSONs from <out>/results/, link concept
              references via EntityResolver, write entities, procedures, and
              CORROBORATES / CONTRADICTS edges with source_type='doc_section'

    status    --output-dir <path>
              report extraction coverage for the manifest

Concurrency
-----------
This script is a writer. The kb-mcp server keeps a read-only DuckDB
connection open for its lifetime, and DuckDB excludes RW from RO across
processes (see ~/Developer/notes/duckdb-concurrent-access.md, Topology B).
By default we detect a trusted reader (cmdline contains
``mcp-servers/kb-mcp/server.py``), SIGTERM it, run the write, and let the
MCP host respawn it on next call. Pass ``--no-auto-stop`` to bypass.
Non-trusted holders (random DuckDB CLI, forgotten test) never get
auto-killed — the script fails fast with a clear identification.

Cost model
----------
Per the project's deliberate constraint (pyproject.toml line 21), this
script never calls the Anthropic API directly. LLM reasoning happens in
Claude Code sub-agents dispatched after `refresh` writes the manifest;
`process` ingests their JSON results. Same pattern as
``extract_batch.py`` and ``extract_procedures.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb
import httpx
import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"

# Make kb-mcp modules importable for db.open_catalog and resolution.EntityResolver.
for _p in (str(MCP_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import open_catalog  # noqa: E402

DEFAULT_TRUSTED_READER_CMD = "mcp-servers/kb-mcp/server.py"
SIGTERM_RELEASE_TIMEOUT_S = 5.0
SIGTERM_POLL_INTERVAL_S = 0.05

log = logging.getLogger("refresh_docs")


# ---------------------------------------------------------------------------
# Lock-handling — Topology B from ~/Developer/notes/duckdb-concurrent-access.md
# ---------------------------------------------------------------------------


class LockHeldError(RuntimeError):
    """Raised when the catalog write lock is held and we won't (or can't) clear it.

    Carries the holder's PID and cmdline so callers can present an actionable
    message. ``reason`` distinguishes auto-stop-disabled, untrusted-holder,
    and unidentifiable-holder cases.
    """

    def __init__(self, *, pid: Optional[int], cmdline: str, reason: str):
        self.pid = pid
        self.cmdline = cmdline
        self.reason = reason
        pid_repr = f"PID {pid}" if pid is not None else "PID unknown"
        super().__init__(
            f"DuckDB write lock held ({pid_repr}, cmdline={cmdline!r}): {reason}"
        )


def _find_lock_holder(catalog_path: Path) -> Optional[int]:
    """Return the PID currently holding the catalog file open, or None.

    Iterates psutil.process_iter and matches on ``open_files`` paths. Skips
    processes we can't introspect (NoSuchProcess, AccessDenied) — losing
    visibility into one process is preferable to crashing on the scan.
    """
    target = str(Path(catalog_path).resolve())
    for proc in psutil.process_iter(["pid", "open_files"]):
        try:
            for f in proc.info.get("open_files") or []:
                if f.path == target:
                    return int(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _safe_cmdline(pid: int) -> str:
    """Return the process cmdline as one space-joined string, or '' on error."""
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def _is_trusted_reader(pid: int, expected_cmd_substring: str) -> bool:
    """True iff the process's cmdline contains the expected substring.

    Strict substring match — never auto-stop a holder we can't positively
    identify as the expected reader.
    """
    cmd = _safe_cmdline(pid)
    return bool(cmd) and expected_cmd_substring in cmd


def _signal_process(pid: int, sig: int) -> None:
    """Wrap psutil.Process(pid).send_signal so tests can patch the OS interaction."""
    psutil.Process(pid).send_signal(sig)


def _wait_for_lock_release(
    catalog_path: Path, pid: int, *, timeout_s: float, poll_s: float,
) -> None:
    """Block until ``pid`` no longer holds ``catalog_path``, or raise."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _find_lock_holder(catalog_path) != pid:
            return
        time.sleep(poll_s)
    raise RuntimeError(
        f"PID {pid} did not release the lock on {catalog_path} within {timeout_s}s"
    )


def open_writer(
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    auto_stop: bool = True,
    trusted_reader_cmd: str = DEFAULT_TRUSTED_READER_CMD,
) -> duckdb.DuckDBPyConnection:
    """Open the catalog in RW mode, auto-stopping a trusted reader if needed.

    Strategy:
      1. Try ``open_catalog(read_only=False)``.
      2. On lock conflict, identify the holder.
      3. If holder is a trusted reader and ``auto_stop`` is True, SIGTERM it,
         wait for the lock to release, retry the open once.
      4. Anything else (untrusted holder, unidentifiable holder, --no-auto-stop)
         raises ``LockHeldError`` with a message the caller can surface verbatim.

    The retry happens at most once. If the second open also fails, the error
    propagates — we don't loop indefinitely on a state that needs human
    intervention.
    """
    try:
        return open_catalog(catalog_path, read_only=False)
    except duckdb.IOException as e:
        msg = str(e).lower()
        if "lock" not in msg and "could not set lock" not in msg:
            raise

    # Lock conflict path.
    holder = _find_lock_holder(catalog_path)
    if holder is None:
        raise LockHeldError(
            pid=None,
            cmdline="",
            reason="lock holder could not be identified — try `lsof <catalog>` manually",
        )

    holder_cmd = _safe_cmdline(holder)

    if not auto_stop:
        raise LockHeldError(
            pid=holder, cmdline=holder_cmd,
            reason="--no-auto-stop set; close the holder manually and retry",
        )

    if not _is_trusted_reader(holder, trusted_reader_cmd):
        raise LockHeldError(
            pid=holder,
            cmdline=holder_cmd,
            reason=(
                f"holder cmdline does not contain {trusted_reader_cmd!r}; "
                f"refusing to auto-stop an untrusted process. Pass --no-auto-stop "
                f"and close it manually if this is the catalog you intended to write to."
            ),
        )

    log.info(
        "kb-mcp running (PID %d) — briefly stopping to refresh, will respawn on next call",
        holder,
    )
    _signal_process(holder, signal.SIGTERM)
    _wait_for_lock_release(
        catalog_path, holder,
        timeout_s=SIGTERM_RELEASE_TIMEOUT_S,
        poll_s=SIGTERM_POLL_INTERVAL_S,
    )

    # Retry open — exactly once, no looping.
    return open_catalog(catalog_path, read_only=False)


# ---------------------------------------------------------------------------
# Fetchers — one per source_type, return content shaped for the sectionizer
# ---------------------------------------------------------------------------


class FetchError(RuntimeError):
    """Any fetch-time failure: HTTP error, MCP transport error, missing content."""


@dataclass
class FetchResult:
    """Normalized fetch output ready for sectionizer + persistence.

    ``source_type`` is the *sectionizer dispatch key* — what shape the parser
    should expect ("markdown", "github_md", "context7", "deepwiki"). It is
    NOT necessarily the same as ``doc_source.source_type``; for example, a
    DeepWiki fetch returns markdown content and uses the markdown parser,
    while ``doc_source.source_type`` for that row remains ``"deepwiki"`` so
    the refresh layer can filter/schedule it correctly.
    """

    url: str
    content: str
    source_type: str  # for sectionize() dispatch


# ---- GitHub --------------------------------------------------------------


class GitHubFetcher:
    """Fetch raw file content from a public GitHub repo.

    Identifier formats:
      ``owner/repo``                   → README.md on default branch
      ``owner/repo:branch``            → README.md on the named branch
      ``owner/repo:branch:path/to/x``  → arbitrary path on the named branch

    No branch ⇒ probe ``main``, then ``master``. We avoid the GitHub API
    here; ``raw.githubusercontent.com`` is the canonical, documented URL
    for raw file access and doesn't count against unauthenticated rate
    limits the way the API does.
    """

    DEFAULT_BRANCHES = ("main", "master")
    DEFAULT_PATH = "README.md"
    HTTP_TIMEOUT_S = 30.0

    def fetch(self, identifier: str) -> FetchResult:
        owner, repo, branch, path = self._parse_identifier(identifier)
        candidates = (branch,) if branch else self.DEFAULT_BRANCHES
        last_error: Optional[Exception] = None
        for b in candidates:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/{path}"
            try:
                content = self._fetch_url(url)
                return FetchResult(url=url, content=content, source_type="github_md")
            except FetchError as e:
                last_error = e
                continue
        raise FetchError(
            f"GitHub fetch failed for {owner}/{repo} (path={path}, branches="
            f"{list(candidates)}): {last_error}"
        )

    @staticmethod
    def _parse_identifier(identifier: str) -> tuple[str, str, Optional[str], str]:
        """Return (owner, repo, branch_or_None, path)."""
        # Split on ':' to get up to three pieces: "owner/repo", "branch", "path"
        parts = identifier.split(":")
        repo_spec = parts[0].strip("/")
        if "/" not in repo_spec:
            raise FetchError(f"GitHub identifier {identifier!r} must include 'owner/repo'")
        owner, repo = repo_spec.split("/", 1)
        branch = parts[1] if len(parts) >= 2 and parts[1] else None
        path = parts[2] if len(parts) >= 3 and parts[2] else GitHubFetcher.DEFAULT_PATH
        return owner, repo, branch, path

    def _fetch_url(self, url: str) -> str:
        try:
            response = httpx.get(url, timeout=self.HTTP_TIMEOUT_S, follow_redirects=True)
        except httpx.HTTPError as e:
            raise FetchError(f"transport error fetching {url}: {e}") from e
        if response.status_code == 404:
            raise FetchError(f"404 Not Found: {url}")
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} fetching {url}")
        return response.text


# ---- MCP-SDK-based fetchers (Context7, DeepWiki) -------------------------


def _extract_text_from_call_result(result: Any) -> str:
    """Concatenate text from an MCP CallToolResult.

    The SDK returns ``CallToolResult`` with a ``.content`` list of items that
    may include ``TextContent`` (most common), ``ImageContent``, etc. We
    pull the text payload out of every TextContent entry and join them.
    Any non-text content is silently skipped — sectionizers are text-only.
    """
    pieces: list[str] = []
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "\n".join(pieces)


class Context7Fetcher:
    """Fetch documentation via Context7 MCP (stdio, npx-spawned).

    Identifier is a Context7-compatible library ID, e.g. ``/duckdb/duckdb``
    or ``/websites/databricks``.

    Multi-query fan-out for broad snapshot coverage
    -----------------------------------------------
    Context7's ``query-docs`` tool is query-driven: it takes a library ID
    *and* a free-text query and returns chunks relevant to that query,
    not the entire library. A single generic query gets us a thin slice
    (5–10 chunks); for broad doc coverage we issue several canonical
    queries within one MCP session and merge the results.

    The query set below was chosen to span the major dimensions a reader
    would care about (reference, onboarding, config, integration,
    patterns). All queries reuse the same long-lived ClientSession so we
    pay the npx subprocess cost once per source.

    Chunk deduplication is keyed on (source URL, first 100 chars of
    content) — Context7 returns the same chunk under multiple queries
    fairly often.
    """

    NPX_COMMAND = "npx"
    NPX_ARGS = ("-y", "@upstash/context7-mcp")
    TOOL_NAME = "query-docs"

    SNAPSHOT_QUERIES: tuple[str, ...] = (
        "API reference, function signatures, and complete syntax",
        "installation, getting started, and basic usage",
        "configuration options, environment variables, and CLI flags",
        "integrations, plugins, and ecosystem libraries",
        "common patterns, best practices, performance, and examples",
    )

    def fetch(
        self, identifier: str, *, queries: Optional[Sequence[str]] = None,
    ) -> FetchResult:
        qs = list(queries) if queries is not None else list(self.SNAPSHOT_QUERIES)
        return asyncio.run(self._fetch_async(identifier, qs))

    async def _fetch_async(self, identifier: str, queries: list[str]) -> FetchResult:
        params = StdioServerParameters(command=self.NPX_COMMAND, args=list(self.NPX_ARGS))
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    query_results: list[str] = []
                    for q in queries:
                        try:
                            result = await session.call_tool(
                                self.TOOL_NAME,
                                {"libraryId": identifier, "query": q},
                            )
                        except Exception as e:
                            log.warning(
                                "Context7 query %r for %s failed (continuing): %s",
                                q, identifier, e,
                            )
                            continue
                        query_results.append(_extract_text_from_call_result(result))
        except Exception as e:  # subprocess / transport / protocol failures
            raise FetchError(f"Context7 session failed for {identifier}: {e}") from e

        chunks = self._merge_query_results(query_results)
        if not chunks:
            raise FetchError(
                f"Context7 returned no usable content for {identifier} "
                f"across {len(queries)} queries"
            )
        return FetchResult(
            url=f"https://context7.com{identifier}",
            content=json.dumps(chunks),
            source_type="context7",
        )

    @staticmethod
    def _merge_query_results(query_results: Sequence[str]) -> list[dict[str, Any]]:
        """Parse each query response, dedupe across queries, return merged chunks.

        Pure function — testable without spawning Context7. Drops responses
        that are MCP-level errors (tool-not-found, transport failure) and
        skips chunks already seen by (source URL, content prefix).
        """
        merged: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for text in query_results:
            if not text or not text.strip():
                continue
            if text.startswith("MCP error") or "Tool not found" in text:
                log.warning("Context7 returned protocol error: %s", text[:160])
                continue
            for chunk in Context7Fetcher._text_to_chunks(text):
                source = (chunk.get("source") or "").strip()
                content = (chunk.get("content") or "").strip()
                if not content:
                    continue
                key = (source, content[:100])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(chunk)
        return merged

    @staticmethod
    def _text_to_chunks(text: str) -> list[dict[str, Any]]:
        """Split Context7's '----------'-delimited response into chunk dicts.

        Context7 returns multiple snippets joined by lines of dashes. Each
        snippet has lines like ``Source: <url>``, ``Title: <heading>``, and
        body text. We treat each as one chunk; the sectionizer keeps them
        as leaf sections without inferring further structure.
        """
        raw_chunks = [c.strip() for c in text.split("----------") if c.strip()]
        if not raw_chunks:
            return [{"content": text.strip(), "title": None, "source": None}]

        chunks: list[dict[str, Any]] = []
        for raw in raw_chunks:
            title: Optional[str] = None
            source: Optional[str] = None
            body_lines: list[str] = []
            for line in raw.splitlines():
                if line.startswith("Title:") and title is None:
                    title = line[len("Title:"):].strip()
                elif line.startswith("Source:") and source is None:
                    source = line[len("Source:"):].strip()
                else:
                    body_lines.append(line)
            chunks.append({
                "title": title,
                "source": source,
                "content": "\n".join(body_lines).strip(),
            })
        return chunks


class DeepWikiFetcher:
    """Fetch wiki content via DeepWiki MCP (HTTP-streamable).

    Identifier is ``owner/repo`` (case-preserving). DeepWiki's
    ``read_wiki_contents`` returns one Markdown blob covering the whole
    wiki — we hand that to the markdown sectionizer (``source_type =
    "markdown"``). We don't try to reconstruct the {structure, pages}
    shape from ``read_wiki_structure`` because the markdown body already
    has H1/H2 headings the markdown parser will split on, and that gives
    sectioning quality equivalent to walking the page tree manually.
    """

    HTTP_URL = "https://mcp.deepwiki.com/mcp"
    TOOL_NAME = "read_wiki_contents"

    def fetch(self, identifier: str) -> FetchResult:
        return asyncio.run(self._fetch_async(identifier))

    async def _fetch_async(self, identifier: str) -> FetchResult:
        try:
            async with streamable_http_client(self.HTTP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        self.TOOL_NAME, {"repoName": identifier},
                    )
        except Exception as e:
            raise FetchError(f"DeepWiki fetch failed for {identifier}: {e}") from e

        text = _extract_text_from_call_result(result)
        if not text.strip():
            raise FetchError(f"DeepWiki returned empty content for {identifier}")
        return FetchResult(
            url=f"https://deepwiki.com/{identifier}",
            content=text,
            source_type="markdown",  # markdown sectionizer handles DeepWiki's H1/H2 output
        )


# ---- Dispatch ------------------------------------------------------------


def get_fetcher(source_type: str):
    """Return a fetcher instance for ``doc_source.source_type``.

    Caller can also pass an instance directly into the pipeline for tests.
    """
    st = source_type.lower()
    if st == "github":
        return GitHubFetcher()
    if st == "context7":
        return Context7Fetcher()
    if st == "deepwiki":
        return DeepWikiFetcher()
    raise FetchError(f"no fetcher registered for source_type={source_type!r}")


# ---------------------------------------------------------------------------
# Pipeline — steps 1–6 of §6.2 (pure Python, no LLM)
# ---------------------------------------------------------------------------


EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
DOC_SECTION_FTS_SCHEMA = "fts_main_doc_section"


def compute_content_hash(content: str) -> str:
    """SHA-256 of the UTF-8 bytes — used to detect unchanged snapshots cheaply.

    No normalization beyond UTF-8 encoding: small whitespace changes upstream
    will invalidate the hash, which is the conservative behavior we want
    (false negatives on "this is the same content" cost us a re-embed; false
    positives lose new content silently).
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class RefreshResult:
    """Structured return from ``refresh_one_source`` for caller introspection."""

    doc_source_id: int
    status: str  # "no_change" | "refreshed" | "error"
    snapshot_id: Optional[int] = None
    section_count: int = 0
    error: Optional[str] = None


@dataclass
class _FlatSection:
    """A doc_section row in flattened form, with parent given as list-index.

    ``parent_index`` is the position (in this same list) of the parent
    section, or ``-1`` for roots. After insert, the caller maps indexes →
    section_ids and writes ``parent_id`` accordingly.
    """

    parent_index: int
    heading_level: Optional[int]
    heading_text: Optional[str]
    content: str
    ordinal: int


def flatten_section_tree(sections: Sequence[Any]) -> list[_FlatSection]:
    """Flatten a Section tree from sectionizer.py into insertion order.

    Parent rows always precede their children so that a single sequential
    INSERT batch can wire ``parent_id`` from the previously-assigned
    ``doc_section_id``. Tree-walk is depth-first, preorder.
    """
    flat: list[_FlatSection] = []

    def visit(node: Any, parent_index: int) -> None:
        idx = len(flat)
        flat.append(_FlatSection(
            parent_index=parent_index,
            heading_level=node.heading_level,
            heading_text=node.heading_text,
            content=node.content,
            ordinal=node.ordinal,
        ))
        for child in (node.children or []):
            visit(child, parent_index=idx)

    for root in sections:
        visit(root, parent_index=-1)
    return flat


def get_latest_snapshot_hash(
    conn: duckdb.DuckDBPyConnection, doc_source_id: int,
) -> Optional[str]:
    """Return the content_hash of the most-recent snapshot for the source, or None."""
    row = conn.execute(
        """
        SELECT content_hash
          FROM doc_snapshot
         WHERE doc_source_id = ?
         ORDER BY retrieved_at DESC, snapshot_id DESC
         LIMIT 1
        """,
        [doc_source_id],
    ).fetchone()
    return row[0] if row else None


def persist_snapshot(
    conn: duckdb.DuckDBPyConnection,
    *,
    doc_source_id: int,
    fetched: FetchResult,
    snapshot_source_type: str,
    content_hash: str,
) -> int:
    """Insert a doc_snapshot row and return its snapshot_id.

    ``snapshot_source_type`` is the canonical ``doc_source.source_type``
    (github / context7 / deepwiki) — distinct from ``fetched.source_type``,
    which is the sectionizer dispatch key.
    """
    row = conn.execute(
        """
        INSERT INTO doc_snapshot (doc_source_id, source_type, url, content_hash, content)
        VALUES (?, ?, ?, ?, ?)
        RETURNING snapshot_id
        """,
        [doc_source_id, snapshot_source_type, fetched.url, content_hash, fetched.content],
    ).fetchone()
    return int(row[0])


def persist_sections(
    conn: duckdb.DuckDBPyConnection,
    snapshot_id: int,
    flat_sections: list[_FlatSection],
) -> list[int]:
    """Insert flattened sections, wiring parent_id from prior inserts.

    Returns section_ids in the same order as ``flat_sections``. The caller
    can use that to look up parent IDs for embedding writes / sub-agent
    prompt naming.
    """
    section_ids: list[int] = []
    for fs in flat_sections:
        parent_id = section_ids[fs.parent_index] if fs.parent_index >= 0 else None
        row = conn.execute(
            """
            INSERT INTO doc_section
                (snapshot_id, parent_id, heading_level, heading_text, ordinal, content)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING doc_section_id
            """,
            [snapshot_id, parent_id, fs.heading_level, fs.heading_text, fs.ordinal, fs.content],
        ).fetchone()
        section_ids.append(int(row[0]))
    return section_ids


def generate_section_embeddings(
    conn: duckdb.DuckDBPyConnection,
    *,
    section_ids: list[int],
    contents: list[str],
    embedder: Any,
) -> None:
    """Embed each section's content and write to doc_section_embedding.

    ``embedder`` is a SentenceTransformer-like object exposing ``.encode(texts)``
    that returns a 2-D array of shape (len(texts), 384). Empty content embeds
    fine (the model returns a degenerate but valid vector); we still index it
    rather than skip, because section presence carries signal even if body is
    sparse (e.g., placeholder DeepWiki pages).
    """
    if not section_ids:
        return
    vectors = embedder.encode(contents, show_progress_bar=False)
    for sid, vec in zip(section_ids, vectors):
        # numpy → python list of floats so duckdb can bind to FLOAT[384]
        as_list = [float(x) for x in vec]
        if len(as_list) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding dimension mismatch: got {len(as_list)}, expected {EMBEDDING_DIM}"
            )
        conn.execute(
            """
            INSERT INTO doc_section_embedding (doc_section_id, embedding, model)
            VALUES (?, ?, ?)
            """,
            [sid, as_list, EMBEDDING_MODEL],
        )


def rebuild_doc_section_fts_index(conn: duckdb.DuckDBPyConnection) -> None:
    """(Re)build the BM25 index on doc_section.content.

    DuckDB's FTS index doesn't auto-update on inserts — every refresh
    rebuilds the index over the full doc_section table. Cost is roughly
    O(total sections); fine at our scale.
    """
    conn.execute(
        "PRAGMA create_fts_index('doc_section', 'doc_section_id', 'content', "
        "stemmer='porter', stopwords='english', ignore='(\\\\.|[^a-z])+', "
        "strip_accents=1, lower=1, overwrite=1)"
    )


def _update_source_metadata(
    conn: duckdb.DuckDBPyConnection,
    doc_source_id: int,
    *,
    content_changed: bool,
) -> None:
    """Bump last_refresh_at always; bump last_content_changed_at only on change."""
    if content_changed:
        conn.execute(
            "UPDATE doc_source "
            "   SET last_refresh_at = CURRENT_TIMESTAMP, "
            "       last_content_changed_at = CURRENT_TIMESTAMP "
            " WHERE doc_source_id = ?",
            [doc_source_id],
        )
    else:
        conn.execute(
            "UPDATE doc_source SET last_refresh_at = CURRENT_TIMESTAMP "
            " WHERE doc_source_id = ?",
            [doc_source_id],
        )


def refresh_one_source(
    conn: duckdb.DuckDBPyConnection,
    doc_source_id: int,
    *,
    fetcher: Optional[Any] = None,
    embedder: Optional[Any] = None,
) -> RefreshResult:
    """End-to-end pipeline for one doc_source: steps 1–6 of §6.2.

    Steps 7–9 (entity / procedure / alignment extraction) are NOT run here
    — that's the prep/process subcommand pair, kept separate so the Phase
    4b LaunchAgent can run this function without needing Claude Code.

    ``fetcher`` and ``embedder`` are injectable for tests; defaults load
    the real implementations.
    """
    # Steps 0a/0b: load doc_source + lazy-load shared resources.
    src = conn.execute(
        "SELECT name, source_type, identifier "
        "  FROM doc_source WHERE doc_source_id = ?",
        [doc_source_id],
    ).fetchone()
    if src is None:
        return RefreshResult(doc_source_id=doc_source_id, status="error",
                             error=f"no doc_source row with id {doc_source_id}")
    name, source_type, identifier = src

    fetcher = fetcher or get_fetcher(source_type)
    embedder = embedder or _load_default_embedder()

    # Step 1: fetch.
    try:
        fetched = fetcher.fetch(identifier)
    except FetchError as e:
        return RefreshResult(doc_source_id=doc_source_id, status="error", error=str(e))

    # Step 2: hash + skip-if-unchanged.
    content_hash = compute_content_hash(fetched.content)
    prior = get_latest_snapshot_hash(conn, doc_source_id)
    if prior == content_hash:
        _update_source_metadata(conn, doc_source_id, content_changed=False)
        log.info("doc_source %d (%s): unchanged (hash %s); refresh metadata only",
                 doc_source_id, name, content_hash[:8])
        return RefreshResult(
            doc_source_id=doc_source_id, status="no_change", snapshot_id=None,
        )

    # Step 3: persist snapshot.
    snapshot_id = persist_snapshot(
        conn,
        doc_source_id=doc_source_id,
        fetched=fetched,
        snapshot_source_type=source_type,
        content_hash=content_hash,
    )

    # Step 4: sectionize. We import lazily to keep the module import-time light.
    from sectionizer import sectionize  # type: ignore[import-not-found]

    sections = sectionize({
        "source_type": fetched.source_type,
        "content": fetched.content,
    })
    flat = flatten_section_tree(sections)

    # Step 4b: persist doc_section rows.
    section_ids = persist_sections(conn, snapshot_id, flat)

    # Step 5: per-section embeddings.
    contents = [fs.content for fs in flat]
    generate_section_embeddings(
        conn, section_ids=section_ids, contents=contents, embedder=embedder,
    )

    # Step 6: rebuild FTS index over doc_section.
    rebuild_doc_section_fts_index(conn)

    _update_source_metadata(conn, doc_source_id, content_changed=True)
    log.info(
        "doc_source %d (%s): refreshed; snapshot_id=%d sections=%d hash=%s",
        doc_source_id, name, snapshot_id, len(section_ids), content_hash[:8],
    )
    return RefreshResult(
        doc_source_id=doc_source_id, status="refreshed",
        snapshot_id=snapshot_id, section_count=len(section_ids),
    )


def _load_default_embedder() -> Any:
    """Lazy-load sentence-transformers — the import is heavy."""
    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    return SentenceTransformer(EMBEDDING_MODEL)


# ---------------------------------------------------------------------------
# prep / process — steps 7–9 of §6.2 via Claude Code sub-agents
# ---------------------------------------------------------------------------
#
# Same pattern as scripts/extract_batch.py and scripts/extract_procedures.py:
# refresh_docs.py never calls the Anthropic API. ``prep_extraction`` writes
# one prompt per doc_section under <out>/prompts/ plus a manifest under
# <out>/manifest.json. Claude Code dispatches sub-agents per the manifest;
# each writes <out>/results/result_section_<id>.json. ``process_extraction``
# reads the results, validates, runs EntityResolver, and writes
# concept / concept_relation rows with source_type='doc_section'.
#
# Scope note: this commit implements *entity extraction* (architecture step 7)
# only. Procedure extraction (step 8) and alignment edges (step 9) hook into
# the same framework via additional prompt builders + result handlers — see
# the TODO comments at ``BUILDERS`` / ``RESULT_HANDLERS`` below. The Phase
# 4b LaunchAgent (cron-driven, no Claude Code present) skips this stage by
# passing ``--no-extract`` to the ``refresh`` subcommand.


@dataclass
class SectionEntry:
    """One doc_section's entry in the prep manifest."""

    doc_section_id: int
    snapshot_id: int
    doc_source_id: int
    doc_source_name: str
    heading_text: Optional[str]
    prompt_path: str
    result_path: str


@dataclass
class PrepManifest:
    """Top-level manifest produced by ``prep_extraction``."""

    output_dir: str
    created_at: str
    snapshot_id: int
    sections: list[SectionEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "snapshot_id": self.snapshot_id,
            "sections": [asdict(s) for s in self.sections],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PrepManifest":
        return cls(
            output_dir=d["output_dir"],
            created_at=d["created_at"],
            snapshot_id=int(d["snapshot_id"]),
            sections=[SectionEntry(**s) for s in d.get("sections", [])],
        )


def build_section_extraction_prompt(
    *,
    doc_section_id: int,
    doc_source_name: str,
    heading_text: Optional[str],
    content: str,
) -> str:
    """Self-contained sub-agent prompt for one doc_section.

    Reuses the entity-extraction SYSTEM_PROMPT from ``extract_entities.py``
    so the JSON schema sub-agents emit is identical to chapter extraction.
    The doc-source name and heading replace the book/chapter framing in the
    payload header so the model has accurate context.
    """
    # Local import — extract_entities sits next to us in scripts/.
    import extract_entities  # type: ignore[import-not-found]

    payload_header = (
        f"DOC_SECTION_ID: {doc_section_id}\n"
        f"DOC_SOURCE: {doc_source_name}\n"
        f"HEADING: {heading_text or '(none — shapeless or top-level section)'}\n"
        f"\n"
        f"CONTENT:\n"
        f"{content}\n"
    )
    return (
        f"{extract_entities.SYSTEM_PROMPT}\n\n"
        f"{payload_header}\n"
        f"Respond with JSON only. No prose, no markdown fences."
    )


def prep_extraction(
    conn: duckdb.DuckDBPyConnection,
    snapshot_id: int,
    output_dir: Path,
) -> PrepManifest:
    """Emit one prompt per doc_section + a manifest, ready for sub-agent dispatch.

    Idempotent on repeat calls: existing files are overwritten, so a
    re-prep of the same snapshot reproduces the same manifest deterministically.
    """
    output_dir = Path(output_dir)
    (output_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (output_dir / "results").mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT s.doc_section_id, s.snapshot_id, s.heading_text, s.content,
               sn.doc_source_id, src.name
          FROM doc_section s
          JOIN doc_snapshot sn ON s.snapshot_id = sn.snapshot_id
          JOIN doc_source   src ON sn.doc_source_id = src.doc_source_id
         WHERE s.snapshot_id = ?
         ORDER BY s.doc_section_id
        """,
        [snapshot_id],
    ).fetchall()

    sections: list[SectionEntry] = []
    for sid, snap_id, heading, content, src_id, src_name in rows:
        prompt_path = output_dir / "prompts" / f"prompt_section_{sid}.txt"
        result_path = output_dir / "results" / f"result_section_{sid}.json"
        prompt_text = build_section_extraction_prompt(
            doc_section_id=int(sid),
            doc_source_name=src_name,
            heading_text=heading,
            content=content or "",
        )
        prompt_path.write_text(prompt_text)
        sections.append(SectionEntry(
            doc_section_id=int(sid),
            snapshot_id=int(snap_id),
            doc_source_id=int(src_id),
            doc_source_name=src_name,
            heading_text=heading,
            prompt_path=str(prompt_path),
            result_path=str(result_path),
        ))

    manifest = PrepManifest(
        output_dir=str(output_dir),
        created_at=datetime.now(timezone.utc).isoformat(),
        snapshot_id=int(snapshot_id),
        sections=sections,
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2),
    )
    log.info(
        "prep_extraction: wrote %d prompts under %s for snapshot_id=%d",
        len(sections), output_dir, snapshot_id,
    )
    return manifest


# ---- Result ingestion ---------------------------------------------------


@dataclass
class ProcessSummary:
    """Outcome of ``process_extraction`` for caller introspection / logging."""

    total_sections: int = 0
    results_processed: int = 0
    results_missing: int = 0
    results_unparseable: int = 0
    entities_resolved: int = 0
    relations_written: int = 0


def _validate_section_extraction(raw: dict) -> tuple[list[dict], list[dict]]:
    """Filter sub-agent output to well-formed entities + relations.

    Mirrors ``extract_entities._validate_extraction`` (private over there;
    duplicated here so future maintainers see the doc-section validation
    path explicitly). Cross-tagged TODO: once both call sites stabilize,
    promote a shared helper.
    """
    import extract_entities  # type: ignore[import-not-found]

    entity_types = extract_entities.ENTITY_TYPES
    relation_types = extract_entities.RELATION_TYPES

    entities: list[dict] = []
    for e in raw.get("entities", []) or []:
        name = (e.get("name") or "").strip()
        etype = (e.get("type") or "").strip()
        if not name or etype not in entity_types:
            continue
        entities.append({
            "name": name, "type": etype,
            "description": (e.get("description") or "").strip(),
        })
    names = {e["name"] for e in entities}

    relations: list[dict] = []
    for r in raw.get("relations", []) or []:
        src = (r.get("from") or "").strip()
        dst = (r.get("to") or "").strip()
        rtype = (r.get("type") or "").strip()
        if rtype not in relation_types or src not in names or dst not in names or src == dst:
            continue
        try:
            conf = float(r.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        relations.append({
            "from": src, "to": dst, "type": rtype,
            "confidence": max(0.0, min(1.0, conf)),
        })
    return entities, relations


def _clear_prior_doc_section_relations(
    conn: duckdb.DuckDBPyConnection, doc_section_id: int,
) -> int:
    """Remove prior concept_relation rows from this section (idempotent re-runs)."""
    before = conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE source_type='doc_section' AND source_id = ?",
        [doc_section_id],
    ).fetchone()[0]
    if before:
        conn.execute(
            "DELETE FROM concept_relation "
            "WHERE source_type='doc_section' AND source_id = ?",
            [doc_section_id],
        )
    return int(before)


def _write_doc_section_relation(
    conn: duckdb.DuckDBPyConnection,
    *,
    from_id: int, to_id: int, rtype: str, confidence: float, doc_section_id: int,
) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO concept_relation
                (from_concept_id, to_concept_id, relation_type, confidence,
                 source_type, source_id)
            VALUES (?, ?, ?, ?, 'doc_section', ?)
            """,
            [from_id, to_id, rtype, confidence, doc_section_id],
        )
        return True
    except duckdb.ConstraintException:
        return False


def process_extraction(
    conn: duckdb.DuckDBPyConnection,
    output_dir: Path,
    *,
    resolver: Optional[Any] = None,
) -> ProcessSummary:
    """Read sub-agent result JSONs and write doc_section entities + relations.

    For each section in the manifest:
      1. Read result_section_<id>.json (skip if missing — sub-agent didn't run)
      2. Parse + validate (skip individual section on parse error; log)
      3. EntityResolver maps entity names → concept_ids (creates new nodes
         for genuinely-new entities, links to existing for resolved matches)
      4. Clear prior doc_section relations + write new ones with
         source_type='doc_section', source_id=doc_section_id

    Returns aggregate counts for status reporting.
    """
    output_dir = Path(output_dir)
    manifest = PrepManifest.from_dict(
        json.loads((output_dir / "manifest.json").read_text())
    )

    if resolver is None:
        from resolution import EntityResolver  # type: ignore[import-not-found]
        resolver = EntityResolver(conn)

    import extract_entities  # type: ignore[import-not-found]
    parse_llm_json = extract_entities.parse_llm_json

    summary = ProcessSummary(total_sections=len(manifest.sections))

    for entry in manifest.sections:
        result_path = Path(entry.result_path)
        if not result_path.exists():
            summary.results_missing += 1
            log.info("section %d: no result file (sub-agent didn't run)",
                     entry.doc_section_id)
            continue

        try:
            raw = parse_llm_json(result_path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            summary.results_unparseable += 1
            log.warning("section %d: result parse error: %s",
                        entry.doc_section_id, e)
            continue

        entities, relations = _validate_section_extraction(raw)

        # Resolve entity names → concept_ids (creates new nodes as needed).
        # Mirrors scripts/extract_entities.py:348 — same EntityResolver,
        # different source_type tag for provenance.
        name_to_cid: dict[str, int] = {}
        for ent in entities:
            result = resolver.resolve(
                ent["name"],
                candidate_context=ent["description"],
                concept_type=ent["type"],
                source_type="doc_section",
                source_id=entry.doc_section_id,
            )
            if result is not None and result.concept_id is not None:
                name_to_cid[ent["name"]] = result.concept_id
                summary.entities_resolved += 1

        _clear_prior_doc_section_relations(conn, entry.doc_section_id)
        for rel in relations:
            from_id = name_to_cid.get(rel["from"])
            to_id = name_to_cid.get(rel["to"])
            if from_id is None or to_id is None:
                continue
            if _write_doc_section_relation(
                conn,
                from_id=from_id, to_id=to_id,
                rtype=rel["type"], confidence=rel["confidence"],
                doc_section_id=entry.doc_section_id,
            ):
                summary.relations_written += 1

        # TODO Phase 4.4b: procedure extraction (step 8) — write procedure +
        # procedure_concept rows with source_type='doc_section'. Mirrors
        # scripts/extract_procedures.py but at section granularity.
        #
        # TODO Phase 4.4b: alignment pass (step 9) — for each concept this
        # section discusses, compare against existing book content and emit
        # CORROBORATES / CONTRADICTS edges in concept_relation. Needs a
        # different prompt template that loads book-content snippets per
        # concept; the framework here is ready to accept the second pass.

        summary.results_processed += 1

    log.info(
        "process_extraction: %d/%d sections processed (%d missing, %d unparseable); "
        "entities_resolved=%d relations_written=%d",
        summary.results_processed, summary.total_sections,
        summary.results_missing, summary.results_unparseable,
        summary.entities_resolved, summary.relations_written,
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


import argparse  # noqa: E402  (kept here so top-of-file imports stay logical)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                        help="Path to the DuckDB catalog (default: data/catalog.ddb).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser(
        "refresh",
        help="Run the snapshot ingestion pipeline (steps 1-6, optional 7-9 prep).",
    )
    target = p_refresh.add_mutually_exclusive_group(required=True)
    target.add_argument("--source-id", type=int,
                        help="Refresh exactly one doc_source by id.")
    target.add_argument("--all", action="store_true",
                        help="Refresh every doc_source row.")
    target.add_argument("--tier", choices=("hot", "warm", "cool"),
                        help="Refresh sources with the given priority_tier.")
    p_refresh.add_argument("--output-dir", type=Path, default=None,
                           help="Where to emit the extraction manifest "
                                "(default: data/refresh/<UTC-timestamp>).")
    p_refresh.add_argument("--no-extract", action="store_true",
                           help="Skip writing prompts for steps 7-9 "
                                "(Phase 4b LaunchAgent path: cron at 3am).")
    p_refresh.add_argument("--no-auto-stop", action="store_true",
                           help="Fail fast on lock conflict instead of "
                                "auto-stopping a trusted reader.")

    p_prep = sub.add_parser("prep",
                            help="Write extraction prompts + manifest for a "
                                 "previously-persisted snapshot.")
    p_prep.add_argument("--snapshot-id", type=int, required=True)
    p_prep.add_argument("--output-dir", type=Path, required=True)
    p_prep.add_argument("--no-auto-stop", action="store_true")

    p_process = sub.add_parser("process",
                               help="Ingest sub-agent extraction results.")
    p_process.add_argument("--output-dir", type=Path, required=True)
    p_process.add_argument("--no-auto-stop", action="store_true")

    p_status = sub.add_parser("status",
                              help="Report extraction coverage for an output directory.")
    p_status.add_argument("--output-dir", type=Path, required=True)

    return parser.parse_args(argv)


def _select_doc_source_ids(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: Optional[int], all_sources: bool, tier: Optional[str],
) -> list[int]:
    if source_id is not None:
        return [source_id]
    if all_sources:
        rows = conn.execute(
            "SELECT doc_source_id FROM doc_source ORDER BY doc_source_id"
        ).fetchall()
        return [int(r[0]) for r in rows]
    if tier:
        rows = conn.execute(
            "SELECT doc_source_id FROM doc_source WHERE priority_tier = ? "
            "ORDER BY doc_source_id", [tier],
        ).fetchall()
        return [int(r[0]) for r in rows]
    return []


def _default_output_dir() -> Path:
    """Default <repo>/data/refresh/<utc-timestamp>/ — keeps runs side-by-side."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "data" / "refresh" / ts


def _cmd_refresh(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or _default_output_dir()
    conn = open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        ids = _select_doc_source_ids(
            conn, source_id=args.source_id, all_sources=args.all, tier=args.tier,
        )
        if not ids:
            log.warning("no doc_source rows match the selection — nothing to do")
            return 0

        embedder = _load_default_embedder()
        refreshed_snapshot_ids: list[int] = []
        for sid in ids:
            res = refresh_one_source(conn, sid, embedder=embedder)
            if res.status == "refreshed" and res.snapshot_id is not None:
                refreshed_snapshot_ids.append(res.snapshot_id)
            elif res.status == "error":
                log.warning("doc_source %d error: %s", sid, res.error)

        if args.no_extract:
            log.info("--no-extract: skipping manifest emission "
                     "(refreshed %d sources, %d new snapshots)",
                     len(ids), len(refreshed_snapshot_ids))
            return 0

        # Emit one combined manifest covering every newly-refreshed snapshot.
        # Each snapshot writes its own subdir so process can ingest one snapshot
        # at a time if desired.
        for snap_id in refreshed_snapshot_ids:
            sub_dir = Path(output_dir) / f"snapshot_{snap_id}"
            prep_extraction(conn, snap_id, sub_dir)
        log.info("manifests written under %s", output_dir)
        return 0
    finally:
        conn.close()


def _cmd_prep(args: argparse.Namespace) -> int:
    conn = open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        prep_extraction(conn, args.snapshot_id, args.output_dir)
        return 0
    finally:
        conn.close()


def _cmd_process(args: argparse.Namespace) -> int:
    conn = open_writer(args.catalog, auto_stop=not args.no_auto_stop)
    try:
        process_extraction(conn, args.output_dir)
        return 0
    finally:
        conn.close()


def _cmd_status(args: argparse.Namespace) -> int:
    """Print per-section extraction coverage for an output directory."""
    out = Path(args.output_dir)
    # Output dir from `refresh` may contain snapshot_<id>/ subdirs; from `prep`
    # it's the manifest root directly. Handle both shapes.
    manifests: list[Path] = []
    if (out / "manifest.json").exists():
        manifests.append(out / "manifest.json")
    else:
        manifests.extend(sorted(out.glob("snapshot_*/manifest.json")))

    if not manifests:
        print(f"no manifests found under {out}")
        return 1

    for mf in manifests:
        manifest = PrepManifest.from_dict(json.loads(mf.read_text()))
        present = sum(1 for s in manifest.sections if Path(s.result_path).exists())
        total = len(manifest.sections)
        pct = (100 * present / total) if total else 0.0
        print(f"{mf.parent.name}: {present}/{total} results ({pct:.0f}%) "
              f"snapshot_id={manifest.snapshot_id}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv)
    handlers = {
        "refresh": _cmd_refresh,
        "prep": _cmd_prep,
        "process": _cmd_process,
        "status": _cmd_status,
    }
    try:
        return handlers[args.cmd](args)
    except LockHeldError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
