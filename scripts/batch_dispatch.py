#!/usr/bin/env python3
"""
batch_dispatch.py — Anthropic Batch API dispatch for entity extraction.

Replaces in-conversation Task() sub-agent dispatch (which billed against
the Max plan at Sonnet/Opus rates) with direct Batch API submission
using Claude Haiku 4.5 + prompt caching.

Reuses ``scripts/extract_batch.py``'s manifest format so the existing
``prep`` and ``process`` steps stay intact — this script only replaces
the *dispatch* step.

Workflow::

    # 1. Build the manifest (existing extract_batch.py)
    .venv/bin/python3 scripts/extract_batch.py prep \\
        --output-dir data/batch-runs/concepts-2026-05-07

    # 2. Submit batches via Batch API (this script)
    .venv/bin/python3 scripts/batch_dispatch.py submit \\
        --manifest data/batch-runs/concepts-2026-05-07/manifest.json

    # 3. Poll periodically (or wait until next morning)
    .venv/bin/python3 scripts/batch_dispatch.py poll \\
        --state data/batch-runs/concepts-2026-05-07/batch_state.json

    # 4. Fetch results when complete — writes
    #    data/batch-runs/concepts-2026-05-07/results/result_chapter_<id>.json
    .venv/bin/python3 scripts/batch_dispatch.py fetch \\
        --state data/batch-runs/concepts-2026-05-07/batch_state.json

    # 5. Ingest via existing extract_batch.py
    .venv/bin/python3 scripts/extract_batch.py process \\
        --output-dir data/batch-runs/concepts-2026-05-07

Cost model
----------
* Claude Haiku 4.5: $1/$5 per 1M input/output tokens
* Batch API: 50% discount on everything
* Prompt caching: cache reads cost 0.1× base input (90% off)
* Cache writes cost 1.25× base input (paid once per ~5min window)

Cache minimum on Haiku 4.5 is 4096 tokens. ``EXTENDED_SYSTEM_PROMPT``
below is padded with worked examples to safely exceed that.

Gotchas this script handles
---------------------------
* Cache minimum: builds + verifies a ≥4096-token prefix at startup.
* Silent invalidators: ``EXTENDED_SYSTEM_PROMPT`` is a module constant,
  built once. No ``datetime.now()`` / IDs / unsorted JSON in it.
* ``custom_id`` uniqueness: ``chapter-<id>`` is unique per row.
* Output verification: uses ``output_config.format`` with json_schema
  so the response is guaranteed to parse.
* Batch size limits: chunks the manifest into 15K-request batches.
* Errors are per-request: tracks succeeded/errored/expired in state.
* Re-runnable: ``submit`` skips chapters that already have a result
  file; partial failures recover by re-running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import tiktoken

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from extract_entities import (  # noqa: E402  # pylint: disable=wrong-import-position
    SYSTEM_PROMPT,
    _build_user_prompt,  # pylint: disable=protected-access
    _load_chapter,  # pylint: disable=protected-access
)

LOG = logging.getLogger("batch_dispatch")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_BATCH_SIZE = 15000
CACHE_MIN_HAIKU = 4096
DEFAULT_MAX_TOKENS = 2048
POLL_INTERVAL_SECONDS = 60

# Output schema — guarantees the response is valid JSON of the right shape.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["Concept", "Pattern", "Tool", "Framework",
                                 "Algorithm", "Technique"],
                    },
                    "description": {"type": "string"},
                },
                "required": ["name", "type", "description"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["REQUIRES", "EXTENDS", "CONTRASTS_WITH",
                                 "IMPLEMENTS", "CITES"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["from", "to", "type", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities", "relations"],
    "additionalProperties": False,
}


# Padded system prompt: original 503-token SYSTEM_PROMPT + worked examples
# + disambiguation guidance, hitting ~4200 tokens to clear the Haiku 4.5
# cache minimum. The padding is genuinely useful — quality should match
# or exceed the unpadded prompt because the worked examples anchor the
# extraction. Keep this constant exact-byte stable across runs; any byte
# change invalidates the cache.
EXTENDED_SYSTEM_PROMPT = SYSTEM_PROMPT + """


--- DISAMBIGUATION GUIDANCE ---

Distinguishing entity types is the most common source of error. Use these heuristics:

Concept vs Pattern:
  A Concept is an idea or principle (e.g., "Idempotency", "Eventual Consistency").
  A Pattern is a *named, reusable structural solution* — usually capitalized in
  the source text (e.g., "Outbox Pattern", "Circuit Breaker", "Saga Pattern").
  If the source text never names the construct as a pattern, it's a Concept.

Tool vs Framework:
  A Tool is a *standalone software product* you run or interact with
  (e.g., "PostgreSQL", "Kafka", "Wireshark", "kubectl").
  A Framework is *something you write code against* (e.g., "Spring",
  "React", "Django", "FastAPI").
  When uncertain, prefer Tool for runtime services and Framework for
  developer libraries.

Algorithm vs Technique:
  An Algorithm has *defined inputs/outputs and a named procedure* —
  often citable in literature (e.g., "Raft", "HyperLogLog", "PageRank").
  A Technique is a less-formal method or practice
  (e.g., "memoization", "code review", "blue-green deployment").
  Algorithm names are usually capitalized; technique names usually aren't.

Things that are NOT entities (skip these):
  - Generic plain words: "data", "file", "system", "user", "request",
    "response" — unless the chapter assigns them a specific named meaning
  - Author names — those live in book_author, not concept
  - Book titles / chapter titles
  - Single-letter or single-character abbreviations alone
    ("X", "Y") — only meaningful in their named-form (e.g., "X.509")

--- WORKED EXAMPLES ---

EXAMPLE 1 — A clearly-bounded chapter on a single pattern.

Book: Designing Data-Intensive Applications
Chapter: Chapter 11 — Stream Processing
Excerpt: "Change data capture (CDC) is a pattern for observing all
changes written to a database and extracting them in a form that can be
replicated to other systems. CDC requires a *changelog* — typically the
write-ahead log of the source database, exposed as a stream. CDC is
usually paired with the *Outbox Pattern* when the application also
needs to publish derived events; outbox tables and CDC tail the same
log, avoiding dual-write hazards. CDC is contrasted with periodic
batch extraction (ETL polling), which can miss intermediate states."

Expected JSON:
{
  "entities": [
    {"name": "Change Data Capture", "type": "Pattern",
     "description": "Observing changes written to a database and exposing them as a stream for downstream consumers."},
    {"name": "Outbox Pattern", "type": "Pattern",
     "description": "Publishing derived events through an outbox table tailed by CDC, avoiding dual-write hazards."},
    {"name": "Write-Ahead Log", "type": "Concept",
     "description": "An append-only log of database mutations used as the source for CDC streams."},
    {"name": "ETL Polling", "type": "Technique",
     "description": "Periodic batch extraction; less timely than CDC and can miss intermediate states."}
  ],
  "relations": [
    {"from": "Change Data Capture", "to": "Write-Ahead Log",
     "type": "REQUIRES", "confidence": 0.9},
    {"from": "Outbox Pattern", "to": "Change Data Capture",
     "type": "REQUIRES", "confidence": 0.85},
    {"from": "Change Data Capture", "to": "ETL Polling",
     "type": "CONTRASTS_WITH", "confidence": 0.9}
  ]
}

EXAMPLE 2 — A chapter that names tools and frameworks.

Book: Kafka: The Definitive Guide
Chapter: Chapter 4 — Kafka Consumers
Excerpt: "The Kafka consumer client subscribes to topics and pulls
records. The high-level API uses consumer groups to partition work
across instances. For exactly-once semantics, set
`enable.idempotence=true` on the producer and use the transactions
API. The Kafka Streams library wraps these primitives in a Java DSL
similar to Apache Flink, but Streams runs as an embedded library in
your application rather than as a separate cluster like Flink does."

Expected JSON:
{
  "entities": [
    {"name": "Apache Kafka", "type": "Tool",
     "description": "Distributed log-based messaging system."},
    {"name": "Kafka Streams", "type": "Framework",
     "description": "Java DSL embedded in applications for stream processing on top of Kafka."},
    {"name": "Apache Flink", "type": "Tool",
     "description": "Stream processing engine that runs as a separate cluster."},
    {"name": "Consumer Group", "type": "Concept",
     "description": "Mechanism for partitioning work across consumer instances reading the same topic."},
    {"name": "Idempotent Producer", "type": "Pattern",
     "description": "Configuration that prevents duplicate writes on retry; foundation for exactly-once delivery."}
  ],
  "relations": [
    {"from": "Kafka Streams", "to": "Apache Kafka",
     "type": "REQUIRES", "confidence": 0.95},
    {"from": "Kafka Streams", "to": "Apache Flink",
     "type": "CONTRASTS_WITH", "confidence": 0.85},
    {"from": "Idempotent Producer", "to": "Apache Kafka",
     "type": "IMPLEMENTS", "confidence": 0.7}
  ]
}

EXAMPLE 3 — A chapter that's mostly preface / table of contents.

Book: Any technical book
Chapter: Chapter 0 — Preface
Excerpt: "This book is the result of three years of teaching distributed
systems. The first part covers fundamentals; the second covers patterns;
the third covers operations. Acknowledgements to all reviewers."

Expected JSON:
{
  "entities": [],
  "relations": []
}

(Front-matter, table-of-contents, dedications, indices, and copyright
pages should produce empty results. Do not extract chapter titles like
"Distributed Systems" as a Concept just because the word appears.)

EXAMPLE 4 — A chapter that introduces an algorithm against a technique.

Book: Database Internals
Chapter: Chapter 8 — Distributed Transactions
Excerpt: "Two-Phase Commit (2PC) is a consensus protocol for atomically
committing a transaction across multiple participants. The coordinator
asks each participant to *prepare*; if all vote yes, it asks them to
*commit*. 2PC is blocking — if the coordinator fails between phases,
participants are stuck. Three-Phase Commit (3PC) adds a *pre-commit*
phase to address this, but introduces a network-partition vulnerability.
In practice, modern systems prefer Raft or Paxos for the same goal,
because both are non-blocking under coordinator failure."

Expected JSON:
{
  "entities": [
    {"name": "Two-Phase Commit", "type": "Algorithm",
     "description": "Consensus protocol for atomic commit across participants; blocks on coordinator failure."},
    {"name": "Three-Phase Commit", "type": "Algorithm",
     "description": "Variant of 2PC adding a pre-commit phase to avoid blocking; vulnerable to partitions."},
    {"name": "Raft", "type": "Algorithm",
     "description": "Non-blocking consensus protocol used as a modern alternative to 2PC/3PC."},
    {"name": "Paxos", "type": "Algorithm",
     "description": "Non-blocking consensus protocol used as a modern alternative to 2PC/3PC."}
  ],
  "relations": [
    {"from": "Three-Phase Commit", "to": "Two-Phase Commit",
     "type": "EXTENDS", "confidence": 0.9},
    {"from": "Raft", "to": "Two-Phase Commit",
     "type": "CONTRASTS_WITH", "confidence": 0.85},
    {"from": "Paxos", "to": "Two-Phase Commit",
     "type": "CONTRASTS_WITH", "confidence": 0.85}
  ]
}

--- COMMON PITFALLS ---

1. Speculating about relations not in the chapter. If the chapter
   names two patterns but doesn't relate them, do NOT emit a relation
   like CONTRASTS_WITH because they "feel" comparable. Confidence is
   for relations the chapter EXPLICITLY states.

2. Splitting a single concept into multiple entities. If the chapter
   describes "the outbox pattern" and later "outbox table", these are
   the same concept — emit ONE entity ("Outbox Pattern") not two.

3. Extracting concepts from the chapter's METADATA (title, headings,
   chapter number) rather than its CONTENT. Headings are hints, not
   evidence; only emit entities the body of the chapter actually
   discusses.

4. Using a confidence of 1.0. Reserve 1.0 for relations stated as
   definitions or laws (e.g., "X is by definition Y"). Most explicit
   statements are 0.7–0.9.

5. Returning entities with empty descriptions. The description should
   be a concise 1–2 sentence summary IN YOUR OWN WORDS, not a copy of
   the chapter's wording. If you can't describe it in one sentence,
   it's probably not a real entity.

--- RELATION-TYPE GUIDANCE ---

REQUIRES: A depends on understanding/implementing B first.
  Use when the chapter says "before learning X you must understand Y",
  "X assumes you have Y in place", "X cannot be implemented without Y",
  or similar. Common for prerequisite chains in pedagogical material.
  Do NOT use REQUIRES for "is part of"; use IMPLEMENTS for that.

EXTENDS: A is a refinement, specialization, or build-on of B.
  Use when "X is a more specialized version of Y", "X improves on Y by
  adding Z", "X is Y with property P". The relation is hierarchical and
  monotonic — A inherits / improves on B's properties.

CONTRASTS_WITH: A and B are alternatives the chapter compares.
  Use when the chapter explicitly compares them, picks between them,
  or cites trade-offs. Two patterns can appear in the same chapter
  without being contrasted — only emit CONTRASTS_WITH if the source
  text discusses them as alternatives.

IMPLEMENTS: A is a concrete implementation of pattern B.
  Use for "X is an implementation of pattern Y", "Y is implemented by X".
  Common shapes: Tool IMPLEMENTS Pattern (Kafka IMPLEMENTS Log-Based
  Messaging), Algorithm IMPLEMENTS Concept (Raft IMPLEMENTS Consensus).

CITES: A's discussion mentions B for context, without dependency.
  Use as a fallback when the chapter mentions another concept but
  doesn't relate it as a prerequisite, refinement, contrast, or
  implementation. Loose citation only.

--- CONFIDENCE CALIBRATION ---

Confidence is your certainty the chapter EXPLICITLY states the relation:

  0.95–1.00  Definitional — "X is a Y", "by definition", or stated as
             a law / theorem. Rare.
  0.85–0.94  Strong explicit — "X requires Y", "X is built on Y",
             "X and Y are commonly contrasted". Most explicit relations
             land here.
  0.70–0.84  Implied but clearly intended — the chapter discusses both
             concepts together and the relation is obvious from context,
             but not stated in a single sentence.
  0.50–0.69  Inferred from structure — chapter sequences A before B in
             a section that establishes prerequisites. Use sparingly.
  Below 0.50 Skip the relation. Either the chapter doesn't really
             support it, or your reading is too speculative.

--- WORKED EXAMPLES (CONTINUED) ---

EXAMPLE 5 — A chapter on a framework with multiple implementing tools.

Book: Spring in Action
Chapter: Chapter 5 — Spring Boot Auto-Configuration
Excerpt: "Spring Boot's auto-configuration is built on the Spring
Framework's @Conditional pattern. When a starter dependency like
spring-boot-starter-data-jpa is on the classpath, Boot detects Hibernate
and configures a default DataSource. The DataSource itself can be
backed by HikariCP (default), Tomcat JDBC, or DBCP2 — Boot picks
HikariCP unless you exclude it. This contrasts with the older Spring
XML configuration approach, which required explicit bean wiring."

Expected JSON:
{
  "entities": [
    {"name": "Spring Boot", "type": "Framework",
     "description": "Convention-over-configuration framework on top of Spring that auto-configures beans based on classpath."},
    {"name": "Spring Framework", "type": "Framework",
     "description": "Foundational dependency-injection and AOP framework for Java."},
    {"name": "Auto-Configuration", "type": "Pattern",
     "description": "Conditional bean registration based on classpath presence and configuration properties."},
    {"name": "HikariCP", "type": "Tool",
     "description": "JDBC connection pool used as Spring Boot's default DataSource backing."},
    {"name": "Hibernate", "type": "Framework",
     "description": "JPA implementation that Spring Boot detects and auto-wires when found on the classpath."}
  ],
  "relations": [
    {"from": "Spring Boot", "to": "Spring Framework",
     "type": "EXTENDS", "confidence": 0.95},
    {"from": "Auto-Configuration", "to": "Spring Boot",
     "type": "IMPLEMENTS", "confidence": 0.85},
    {"from": "Spring Boot", "to": "HikariCP",
     "type": "REQUIRES", "confidence": 0.7}
  ]
}

EXAMPLE 6 — A chapter about anti-patterns (these are still entities).

Book: Microservices Anti-Patterns
Chapter: Chapter 3 — The Distributed Monolith
Excerpt: "The distributed monolith is the anti-pattern that arises when
microservices are decomposed but maintain tight coupling — typically
through synchronous calls that fan out across services for a single
request, shared databases that prevent independent deployment, or
shared library versions that lock all services into a coordinated
release. The cure is to introduce asynchronous boundaries (event-driven
communication via Apache Kafka or AWS Kinesis) and database-per-service.
Bounded Contexts from Domain-Driven Design provide the conceptual
discipline."

Expected JSON:
{
  "entities": [
    {"name": "Distributed Monolith", "type": "Pattern",
     "description": "Anti-pattern where decomposed microservices remain tightly coupled, defeating the benefits of decomposition."},
    {"name": "Event-Driven Architecture", "type": "Pattern",
     "description": "Communication via asynchronous events as a cure for synchronous coupling between services."},
    {"name": "Database per Service", "type": "Pattern",
     "description": "Each microservice owns its data store privately, preventing schema-coupling across services."},
    {"name": "Bounded Context", "type": "Concept",
     "description": "From Domain-Driven Design — an explicit boundary around a model that gives terms unambiguous meaning."},
    {"name": "Apache Kafka", "type": "Tool",
     "description": "Distributed log-based messaging system used for asynchronous service communication."},
    {"name": "AWS Kinesis", "type": "Tool",
     "description": "Managed streaming service used for asynchronous service communication."}
  ],
  "relations": [
    {"from": "Distributed Monolith", "to": "Event-Driven Architecture",
     "type": "CONTRASTS_WITH", "confidence": 0.85},
    {"from": "Distributed Monolith", "to": "Database per Service",
     "type": "CONTRASTS_WITH", "confidence": 0.85},
    {"from": "Database per Service", "to": "Bounded Context",
     "type": "REQUIRES", "confidence": 0.7},
    {"from": "Apache Kafka", "to": "Event-Driven Architecture",
     "type": "IMPLEMENTS", "confidence": 0.85},
    {"from": "AWS Kinesis", "to": "Event-Driven Architecture",
     "type": "IMPLEMENTS", "confidence": 0.85}
  ]
}

EXAMPLE 7 — A chapter where most names are NOT entities.

Book: The Mythical Man-Month
Chapter: Chapter 11 — Plan to Throw One Away
Excerpt: "In most projects, the first system built is barely usable.
It may be too slow, too big, awkward to use, or all three. The user is
told that the system is wrong and a new one will replace it. The
question therefore is not whether to build a pilot system and throw it
away. The question is whether to plan in advance to build a throwaway,
or to promise to deliver the throwaway to customers."

Expected JSON:
{
  "entities": [
    {"name": "Throwaway Prototype", "type": "Technique",
     "description": "Building a first system explicitly intended to be discarded, accepting that the second system will be the deliverable."}
  ],
  "relations": []
}

(Words like "system", "user", "customer", "project" are generic and
should not be extracted. The chapter discusses one named technique;
that's the only entity. There are no explicit relations.)

End of guidance. Now extract from the chapter provided below."""


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class BatchEntry:
    """One submitted batch in a multi-batch run."""

    batch_id: str
    custom_ids: list[str]   # the chapter-<id> custom_ids in this batch
    submitted_at: str
    status: str = "in_progress"   # 'in_progress' | 'ended' | 'fetched'


@dataclass
class DispatchState:
    """Sidecar state for a dispatch run."""

    manifest_path: str
    output_dir: str
    model: str
    batches: list[BatchEntry] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict."""
        return {
            "manifest_path": self.manifest_path,
            "output_dir": self.output_dir,
            "model": self.model,
            "created_at": self.created_at,
            "batches": [asdict(b) for b in self.batches],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DispatchState":
        """Rehydrate from JSON-friendly dict."""
        return cls(
            manifest_path=d["manifest_path"],
            output_dir=d["output_dir"],
            model=d["model"],
            created_at=d.get("created_at", ""),
            batches=[BatchEntry(**b) for b in d.get("batches", [])],
        )


def _state_path(manifest_path: Path) -> Path:
    """Sidecar state file lives next to the manifest."""
    return manifest_path.parent / "batch_state.json"


def _load_state(state_path: Path) -> DispatchState:
    return DispatchState.from_dict(json.loads(state_path.read_text()))


def _save_state(state: DispatchState, state_path: Path) -> None:
    state_path.write_text(json.dumps(state.to_dict(), indent=2))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_TOKENIZER = tiktoken.get_encoding("cl100k_base")


def _verify_cache_eligibility() -> None:
    """Hard-fail at startup if the padded system prompt isn't ≥4096 tokens."""
    n = len(_TOKENIZER.encode(EXTENDED_SYSTEM_PROMPT))
    LOG.info("EXTENDED_SYSTEM_PROMPT: %d tokens (Haiku cache min: %d)",
             n, CACHE_MIN_HAIKU)
    if n < CACHE_MIN_HAIKU:
        raise RuntimeError(
            f"EXTENDED_SYSTEM_PROMPT is {n} tokens; need ≥{CACHE_MIN_HAIKU} "
            f"to clear Haiku 4.5's cache minimum. Fix by extending the prompt."
        )


def _build_request(custom_id: str, user_text: str, model: str) -> dict:
    """Build one Batch API request with cache_control on the system prefix."""
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": EXTENDED_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}
            },
        },
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def do_submit(args: argparse.Namespace) -> int:
    """Read manifest, build requests, submit batches, save state.

    Skips chapters whose result_path already exists — re-running this
    after a partial failure resumes cleanly.
    """
    _verify_cache_eligibility()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        LOG.error("manifest not found: %s", manifest_path)
        return 1

    manifest = json.loads(manifest_path.read_text())
    chapters = manifest["chapters"]
    output_dir = Path(manifest["output_dir"]).resolve()
    if not output_dir.is_absolute():
        output_dir = manifest_path.parent / Path(manifest["output_dir"]).name
    results_dir = output_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    state_path = _state_path(manifest_path)
    if state_path.exists() and not args.force:
        LOG.error("state file already exists at %s; pass --force to override",
                  state_path)
        return 1

    # Load chapter content via existing helper.
    import duckdb  # pylint: disable=import-outside-toplevel
    catalog = PROJECT_ROOT / "data" / "catalog.ddb"
    conn = duckdb.connect(str(catalog), read_only=True)

    requests = []
    skipped_existing = 0
    skipped_no_content = 0
    custom_id_to_chapter: dict[str, int] = {}
    for entry in chapters:
        cid = entry["chapter_id"]
        result_path = Path(entry["result_path"])
        if not result_path.is_absolute():
            result_path = manifest_path.parent / result_path
        if result_path.exists() and not args.force:
            skipped_existing += 1
            continue
        try:
            chapter = _load_chapter(conn, cid)
        except ValueError as exc:
            LOG.warning("skip chapter_id=%d: %s", cid, exc)
            skipped_no_content += 1
            continue
        custom_id = f"chapter-{cid}"
        user_text = (
            f"--- CHAPTER TO EXTRACT ---\n\n"
            f"{_build_user_prompt(chapter)}\n\n"
            f"Respond with JSON only. No prose, no markdown fences."
        )
        requests.append(_build_request(custom_id, user_text, args.model))
        custom_id_to_chapter[custom_id] = cid
    conn.close()

    if skipped_existing:
        LOG.info("skipping %d chapters with existing results", skipped_existing)
    if skipped_no_content:
        LOG.info("skipped %d chapters with no content", skipped_no_content)
    if not requests:
        LOG.warning("no chapters to dispatch — nothing to do")
        return 0

    LOG.info("dispatching %d requests in batches of %d",
             len(requests), args.batch_size)

    client = anthropic.Anthropic()
    state = DispatchState(
        manifest_path=str(manifest_path),
        output_dir=str(output_dir),
        model=args.model,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    for i in range(0, len(requests), args.batch_size):
        chunk = requests[i:i + args.batch_size]
        chunk_custom_ids = [r["custom_id"] for r in chunk]
        LOG.info("submitting batch %d (%d requests, custom_ids %s..%s)",
                 i // args.batch_size + 1,
                 len(chunk),
                 chunk[0]["custom_id"],
                 chunk[-1]["custom_id"])
        if args.dry_run:
            LOG.info("  DRY-RUN — not submitting")
            continue
        batch = client.messages.batches.create(requests=chunk)
        LOG.info("  -> batch_id=%s status=%s",
                 batch.id, batch.processing_status)
        state.batches.append(BatchEntry(
            batch_id=batch.id,
            custom_ids=chunk_custom_ids,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        ))
        _save_state(state, state_path)

    if not args.dry_run:
        LOG.info("submitted %d batches; state saved to %s",
                 len(state.batches), state_path)
    return 0


def do_poll(args: argparse.Namespace) -> int:
    """Poll all in-flight batches in the state file until they end or once."""
    state_path = Path(args.state).resolve()
    state = _load_state(state_path)
    client = anthropic.Anthropic()

    last_status = ""
    while True:
        any_running = False
        lines = []
        for be in state.batches:
            if be.status == "fetched":
                lines.append(f"  {be.batch_id} fetched")
                continue
            batch = client.messages.batches.retrieve(be.batch_id)
            be.status = "ended" if batch.processing_status == "ended" else "in_progress"
            rc = batch.request_counts
            lines.append(
                f"  {be.batch_id} {batch.processing_status:>10} "
                f"succ={rc.succeeded:>5} err={rc.errored:>3} "
                f"proc={rc.processing:>5} canc={rc.canceled:>3}"
            )
            if batch.processing_status != "ended":
                any_running = True
        _save_state(state, state_path)

        status_msg = "\n".join(lines)
        if status_msg != last_status:
            LOG.info("\n" + status_msg)
            last_status = status_msg

        if not any_running:
            LOG.info("all batches ended")
            return 0
        if args.once:
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)


def do_fetch(args: argparse.Namespace) -> int:
    """Download results for ended batches, write to manifest's result_paths."""
    state_path = Path(args.state).resolve()
    state = _load_state(state_path)
    client = anthropic.Anthropic()

    manifest_path = Path(state.manifest_path)
    manifest = json.loads(manifest_path.read_text())
    custom_id_to_result_path: dict[str, Path] = {}
    for entry in manifest["chapters"]:
        rp = Path(entry["result_path"])
        if not rp.is_absolute():
            rp = manifest_path.parent / rp
        custom_id_to_result_path[f"chapter-{entry['chapter_id']}"] = rp

    written = 0
    errored = 0
    for be in state.batches:
        if be.status == "fetched":
            continue
        batch = client.messages.batches.retrieve(be.batch_id)
        if batch.processing_status != "ended":
            LOG.info("batch %s still %s — skipping",
                     be.batch_id, batch.processing_status)
            continue
        first_result_logged = False
        for result in client.messages.batches.results(be.batch_id):
            cid = result.custom_id
            rp = custom_id_to_result_path.get(cid)
            if rp is None:
                LOG.warning("no manifest entry for custom_id=%s", cid)
                continue
            if result.result.type == "succeeded":
                msg = result.result.message
                text = next(
                    (b.text for b in msg.content if b.type == "text"), ""
                )
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(text)
                written += 1
                if not first_result_logged:
                    LOG.info(
                        "  cache check on first result: write=%s read=%s "
                        "uncached=%s",
                        msg.usage.cache_creation_input_tokens,
                        msg.usage.cache_read_input_tokens,
                        msg.usage.input_tokens,
                    )
                    first_result_logged = True
            elif result.result.type == "errored":
                err = result.result.error
                LOG.warning("error on %s: %s — %s",
                            cid, err.type, getattr(err, "message", ""))
                # Persist a marker so process step doesn't see it as missing.
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.with_suffix(".error.json").write_text(
                    json.dumps({"type": err.type,
                                "message": getattr(err, "message", "")})
                )
                errored += 1
            elif result.result.type == "expired":
                LOG.warning("expired: %s — needs resubmit", cid)
                errored += 1
            elif result.result.type == "canceled":
                LOG.warning("canceled: %s", cid)
                errored += 1
        be.status = "fetched"
        _save_state(state, state_path)

    LOG.info("fetched: %d succeeded, %d errored/expired", written, errored)
    return 0


def do_status(args: argparse.Namespace) -> int:
    """Print summary of state file (no API calls)."""
    state_path = Path(args.state).resolve()
    state = _load_state(state_path)
    print(f"Run: {state.manifest_path}")
    print(f"Model: {state.model}")
    print(f"Created: {state.created_at}")
    print(f"Batches: {len(state.batches)}")
    for be in state.batches:
        print(f"  {be.batch_id}  {be.status:>12}  "
              f"({len(be.custom_ids)} custom_ids, submitted {be.submitted_at})")
    return 0


def do_check_prefix(args: argparse.Namespace) -> int:
    """Sanity-check the EXTENDED_SYSTEM_PROMPT length and cache-readiness."""
    n = len(_TOKENIZER.encode(EXTENDED_SYSTEM_PROMPT))
    print(f"EXTENDED_SYSTEM_PROMPT: {n} tokens")
    print(f"Haiku 4.5 cache min:    {CACHE_MIN_HAIKU} tokens")
    print(f"Cache eligible:         {n >= CACHE_MIN_HAIKU}")
    return 0 if n >= CACHE_MIN_HAIKU else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    """Entry point dispatching to subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="Submit batches to Batch API")
    p_submit.add_argument("--manifest", required=True)
    p_submit.add_argument("--model", default=DEFAULT_MODEL)
    p_submit.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p_submit.add_argument("--dry-run", action="store_true")
    p_submit.add_argument("--force", action="store_true",
                          help="Overwrite existing state file and resubmit")
    p_submit.set_defaults(func=do_submit)

    p_poll = sub.add_parser("poll", help="Poll batch status")
    p_poll.add_argument("--state", required=True)
    p_poll.add_argument("--once", action="store_true",
                        help="Print once and exit instead of looping")
    p_poll.set_defaults(func=do_poll)

    p_fetch = sub.add_parser("fetch", help="Download results")
    p_fetch.add_argument("--state", required=True)
    p_fetch.set_defaults(func=do_fetch)

    p_status = sub.add_parser("status", help="Show state without API calls")
    p_status.add_argument("--state", required=True)
    p_status.set_defaults(func=do_status)

    p_check = sub.add_parser("check-prefix",
                             help="Verify cache-eligible prefix length")
    p_check.set_defaults(func=do_check_prefix)

    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
