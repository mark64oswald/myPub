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
    SYSTEM_PROMPT as ENTITY_SYSTEM_PROMPT,
    _build_user_prompt,  # pylint: disable=protected-access
    _load_chapter,  # pylint: disable=protected-access
)
from extract_procedures import (  # noqa: E402  # pylint: disable=wrong-import-position
    SYSTEM_PROMPT as PROCEDURE_SYSTEM_PROMPT,
)
from refresh_docs import (  # noqa: E402  # pylint: disable=wrong-import-position
    ALIGNMENT_SYSTEM_PROMPT,
)

LOG = logging.getLogger("batch_dispatch")

DEFAULT_MODEL = "claude-haiku-4-5"
ALIGNMENT_MODEL = "claude-sonnet-4-6"
DEFAULT_BATCH_SIZE = 15000
CACHE_MIN_HAIKU = 4096   # Haiku 4.5 / Opus 4.6 / Opus 4.5 minimum
CACHE_MIN_SONNET = 2048  # Sonnet 4.6 minimum
DEFAULT_MAX_TOKENS = 2048
POLL_INTERVAL_SECONDS = 60

# Per-task cache minimum (depends on the model that task uses).
TASK_CACHE_MIN: dict[str, int] = {
    "concepts":   CACHE_MIN_HAIKU,
    "procedures": CACHE_MIN_HAIKU,
    "alignment":  CACHE_MIN_SONNET,
}

# Output schemas — guarantee the response is valid JSON of the right shape.
CONCEPT_OUTPUT_SCHEMA = {
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

# Procedure-extraction schema. Note: implements_pattern is type ["string", "null"]
# because the extractor returns null when no pattern is implemented.
PROCEDURE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "procedures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "preconditions": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "n": {"type": "integer"},
                                "action": {"type": "string"},
                                "command": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                            "required": ["n", "action"],
                            "additionalProperties": False,
                        },
                    },
                    "postconditions": {"type": "string"},
                    "failure_modes": {"type": "string"},
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "implements_pattern": {"type": ["string", "null"]},
                },
                "required": ["name", "preconditions", "steps",
                             "postconditions", "failure_modes",
                             "concepts", "implements_pattern"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["procedures"],
    "additionalProperties": False,
}


# Padded system prompt: original 503-token SYSTEM_PROMPT + worked examples
# + disambiguation guidance, hitting ~4300 tokens to clear the Haiku 4.5
# cache minimum. The padding is genuinely useful — quality should match
# or exceed the unpadded prompt because the worked examples anchor the
# extraction. Keep this constant exact-byte stable across runs; any byte
# change invalidates the cache.
EXTENDED_ENTITY_PROMPT = ENTITY_SYSTEM_PROMPT + """


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


# Procedure extraction prompt — same padding strategy. Base PROCEDURE_SYSTEM_PROMPT
# is ~620 tokens; we add disambiguation guidance + worked examples to clear
# the 4096-token Haiku cache minimum. As with the entity prompt, the padding
# is genuinely useful content that improves quality.
EXTENDED_PROCEDURE_PROMPT = PROCEDURE_SYSTEM_PROMPT + """


--- DISAMBIGUATION: PROCEDURE vs CONCEPTUAL CHAPTER ---

A procedure is "how to do X" content — concrete steps a reader could
literally follow. Most technical chapters do NOT contain procedures;
they explain concepts, motivate patterns, or compare approaches. Be
strict about this: when in doubt, return zero procedures.

Signals that a chapter HAS extractable procedures:
  * Numbered steps, bullet lists of actions, or "Do this, then this"
    sequencing
  * Code listings followed by "run this" / "execute this" instructions
  * Configuration walkthroughs ("Add the following to your settings…")
  * Setup / installation / migration sequences
  * Headings like "Setting up X", "Configuring Y", "How to Z",
    "Walkthrough", "Tutorial"

Signals that a chapter has NO extractable procedures (return []):
  * Pure exposition explaining what a thing IS, not how to use it
  * Comparative discussion of multiple approaches without committing
    to one
  * Front-matter, prefaces, summaries, indexes, references, glossaries
  * Theoretical chapters that introduce algorithms or patterns
    abstractly (those are concepts/algorithms; the implementation is
    elsewhere)
  * Chapters that summarize a previous chapter

A typical chapter has 0–3 procedures. More than 5 is suspicious — the
extractor is probably treating bullet points as procedures or splitting
one procedure into many. Consolidate.

--- STEP STRUCTURE GUIDANCE ---

A "step" is one discrete action. Each step has:

  n         1-based ordinal in the procedure
  action    imperative sentence ("Enable change data feed on the
            source table")
  command   literal shell or code snippet from the chapter (≤200 chars,
            optional). Do NOT invent commands; if the chapter doesn't
            show one, omit this field.
  notes     short clarifying remark (optional). Use sparingly — only
            when the action sentence is ambiguous without it.

Steps are ORDERED actions, not bullet points summarizing topics. If a
chapter has a numbered list like:
    1. Why CDC matters for analytics
    2. How CDC compares to dual writes
    3. The trade-offs of log-based CDC
…that's an outline, not a procedure. The chapter is conceptual.

If a chapter has:
    1. Run `ALTER TABLE … SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`
    2. Verify with `DESCRIBE EXTENDED <table>`
    3. Read the change feed via the table_changes() function
…those are real steps. Extract.

--- CONFIDENCE & FAILURE MODES ---

failure_modes:
  Capture only what the chapter discusses. If the chapter says "this
  fails when X is misconfigured; recover by Y", record that. If the
  chapter is silent on failures, set failure_modes to "" — do not
  speculate about possible failures.

implements_pattern:
  Set this to the name of ONE Pattern this procedure realizes — only
  if the chapter explicitly names a pattern the procedure implements.
  E.g., "Outbox Pattern" for an outbox-implementation procedure.
  Set to null if the procedure is purely operational (configuration,
  setup, debugging) and doesn't realize a named pattern.

concepts:
  List the named concepts the procedure operates on, in the
  vocabulary the chapter uses. E.g., ["Change Data Capture",
  "Delta Lake"] for a CDC-on-Delta procedure. Generic words ("data",
  "system") are not concepts.

--- WORKED EXAMPLES ---

EXAMPLE 1 — A clear procedural chapter.

Book: Apache Kafka in Production
Chapter: 12.3 — Configuring Idempotent Producers
Excerpt: "To enable exactly-once delivery from a Kafka producer, set
`enable.idempotence=true` on the producer configuration. This requires
`acks=all` and `max.in.flight.requests.per.connection ≤ 5` — both are
default in modern Kafka clients but verify in your config. After
configuring, restart the producer; the broker will assign a producer ID
which you can verify in the broker logs (look for 'Initiated PID' for
the client). Failure mode: if the broker is on Kafka < 2.5, idempotent
mode falls back to at-least-once silently. Upgrade the broker."

Expected JSON:
{
  "procedures": [
    {
      "name": "Configure Kafka idempotent producer",
      "preconditions": "Kafka client 1.0+ and broker 2.5+. Producer configuration access.",
      "steps": [
        {"n": 1, "action": "Set enable.idempotence to true on the producer.",
         "command": "enable.idempotence=true"},
        {"n": 2, "action": "Verify acks is set to all.",
         "command": "acks=all"},
        {"n": 3, "action": "Verify max.in.flight.requests.per.connection is at most 5.",
         "command": "max.in.flight.requests.per.connection=5"},
        {"n": 4, "action": "Restart the producer."},
        {"n": 5, "action": "Confirm the broker assigned a producer ID by checking broker logs for the 'Initiated PID' message."}
      ],
      "postconditions": "Producer publishes with exactly-once delivery semantics.",
      "failure_modes": "On brokers older than Kafka 2.5, idempotent mode silently falls back to at-least-once delivery. Upgrade the broker.",
      "concepts": ["Idempotent Producer", "Apache Kafka"],
      "implements_pattern": "Idempotent Producer"
    }
  ]
}

EXAMPLE 2 — A purely conceptual chapter.

Book: Designing Data-Intensive Applications
Chapter: 9.1 — The CAP Theorem
Excerpt: "The CAP theorem states that any networked shared-data system
can have at most two of three desirable properties: consistency,
availability, and partition tolerance. Brewer's original conjecture
formalized this in 2000; Gilbert and Lynch proved it in 2002. The
theorem is sometimes misunderstood: 'consistency' here is linearizability
(strong consistency), not the C in ACID. Many real systems navigate
this trade-off dynamically — Cassandra defaults to eventual consistency
but allows tunable per-query strength."

Expected JSON:
{
  "procedures": []
}

(This chapter explains a theorem and its history; it doesn't tell the
reader how to do anything. Zero procedures.)

EXAMPLE 3 — A chapter with one setup procedure but mostly explanation.

Book: PostgreSQL: Up and Running
Chapter: 4.2 — Setting up Streaming Replication
Excerpt: "PostgreSQL streaming replication keeps a standby server in
near-real-time sync with a primary. Conceptually, the primary streams
its write-ahead log (WAL) entries to the standby, which replays them.
The setup is straightforward: on the primary, edit postgresql.conf to
set wal_level = replica and max_wal_senders = 5. Reload the config
with `pg_ctl reload`. Create a replication user with `CREATE ROLE
replicator WITH REPLICATION LOGIN`. On the standby, take a base backup
with `pg_basebackup -h primary-host -U replicator -D /var/lib/postgres/13/main`.
Configure standby.signal and primary_conninfo. Start the standby —
streaming replication begins automatically. Common failure: standby
falls behind during high write load; tune max_replication_slots and
add a physical replication slot to prevent WAL recycling."

Expected JSON:
{
  "procedures": [
    {
      "name": "Set up PostgreSQL streaming replication",
      "preconditions": "Two PostgreSQL instances (primary and standby). Network connectivity between them. Superuser access on the primary.",
      "steps": [
        {"n": 1, "action": "On the primary, set wal_level to replica in postgresql.conf.",
         "command": "wal_level = replica"},
        {"n": 2, "action": "Set max_wal_senders to at least 5.",
         "command": "max_wal_senders = 5"},
        {"n": 3, "action": "Reload the primary's configuration.",
         "command": "pg_ctl reload"},
        {"n": 4, "action": "Create a replication user on the primary.",
         "command": "CREATE ROLE replicator WITH REPLICATION LOGIN"},
        {"n": 5, "action": "On the standby, take a base backup from the primary.",
         "command": "pg_basebackup -h primary-host -U replicator -D /var/lib/postgres/13/main"},
        {"n": 6, "action": "Configure standby.signal and primary_conninfo on the standby."},
        {"n": 7, "action": "Start the standby. Streaming replication begins automatically."}
      ],
      "postconditions": "Standby server replays WAL entries from the primary in near-real-time.",
      "failure_modes": "Standby may fall behind during high write load. Tune max_replication_slots and add a physical replication slot to prevent WAL recycling.",
      "concepts": ["Streaming Replication", "Write-Ahead Log", "PostgreSQL"],
      "implements_pattern": null
    }
  ]
}

EXAMPLE 4 — A chapter with two related procedures.

Book: Kubernetes in Action
Chapter: 11.4 — Rolling Updates and Rollbacks
Excerpt: "To perform a rolling update of a Deployment, edit its image
reference (kubectl set image deployment/myapp myapp=myrepo/myapp:v2)
or apply an updated manifest. Kubernetes will incrementally replace old
pods with new ones, respecting maxSurge and maxUnavailable. Watch
progress with `kubectl rollout status deployment/myapp`. To abort and
return to the previous version, run `kubectl rollout undo
deployment/myapp` — this rolls back to the prior ReplicaSet. The
rollback is fast because the prior ReplicaSet still exists, scaled to
zero. If a deployment hangs (a pod fails its readiness probe), you'll
see 'Waiting for replicas to be available'. Either fix the new image
and re-deploy, or roll back."

Expected JSON:
{
  "procedures": [
    {
      "name": "Perform a rolling update on a Kubernetes Deployment",
      "preconditions": "kubectl access to the cluster. An existing Deployment to update.",
      "steps": [
        {"n": 1, "action": "Update the Deployment's image reference.",
         "command": "kubectl set image deployment/myapp myapp=myrepo/myapp:v2"},
        {"n": 2, "action": "Watch the rollout progress.",
         "command": "kubectl rollout status deployment/myapp"}
      ],
      "postconditions": "Old pods are incrementally replaced with new ones. The Deployment reports successful rollout.",
      "failure_modes": "If a pod fails its readiness probe, the rollout hangs at 'Waiting for replicas to be available'. Fix the new image and redeploy, or roll back.",
      "concepts": ["Rolling Update", "Deployment", "Kubernetes"],
      "implements_pattern": "Rolling Update"
    },
    {
      "name": "Roll back a Kubernetes Deployment to the previous version",
      "preconditions": "kubectl access. A Deployment with a prior revision still tracked.",
      "steps": [
        {"n": 1, "action": "Roll back to the previous revision.",
         "command": "kubectl rollout undo deployment/myapp"}
      ],
      "postconditions": "Deployment is scaled back to the prior ReplicaSet, which already exists scaled to zero so the rollback is fast.",
      "failure_modes": "",
      "concepts": ["Deployment", "ReplicaSet", "Kubernetes"],
      "implements_pattern": null
    }
  ]
}

EXAMPLE 5 — A debugging walkthrough.

Book: Linux Performance Tools
Chapter: 6.2 — Diagnosing High CPU Usage
Excerpt: "When a Linux server shows high CPU usage, work top-down.
First, run `top -b -n 1` and look at the COMMAND column for the
process consuming the most CPU. Confirm with `ps -eo pid,comm,%cpu
--sort=-%cpu | head`. If a Python process is the culprit, attach with
`py-spy dump --pid <pid>` to see what it's doing right now (this
requires py-spy installed and ptrace privileges). Sample for 30 seconds
with `py-spy record -o profile.svg --pid <pid> --duration 30` to
visualize. Common gotcha: the high-CPU process may be a shell wrapper
masking the actual workload; use `ps -ef --forest` to see the process
tree. Recovery: if it's runaway code you control, `kill -USR1 <pid>` for
a graceful shutdown if your code handles that signal; `kill -9 <pid>`
otherwise, accepting data loss for unflushed buffers."

Expected JSON:
{
  "procedures": [
    {
      "name": "Diagnose high CPU usage on a Linux server",
      "preconditions": "SSH access to the affected server. py-spy installed if Python is involved. ptrace privileges (root or CAP_SYS_PTRACE).",
      "steps": [
        {"n": 1, "action": "Run top to identify the highest-CPU process.",
         "command": "top -b -n 1"},
        {"n": 2, "action": "Confirm with ps sorted by CPU percentage.",
         "command": "ps -eo pid,comm,%cpu --sort=-%cpu | head"},
        {"n": 3, "action": "If the culprit is Python, attach py-spy to inspect what it's doing now.",
         "command": "py-spy dump --pid <pid>"},
        {"n": 4, "action": "Sample the process for 30 seconds to produce a flame graph.",
         "command": "py-spy record -o profile.svg --pid <pid> --duration 30"},
        {"n": 5, "action": "If the high-CPU process appears to be a wrapper, inspect the process tree to find the real workload.",
         "command": "ps -ef --forest"}
      ],
      "postconditions": "The CPU-consuming process is identified and its stack trace is visible.",
      "failure_modes": "py-spy needs ptrace privileges and may fail without root. The real workload may be hidden behind a shell wrapper; check the process tree.",
      "concepts": ["py-spy", "Linux", "Process Tree"],
      "implements_pattern": null
    }
  ]
}

EXAMPLE 6 — A chapter where a bullet list is NOT a procedure.

Book: System Design Interview
Chapter: 3 — A Framework for System Design Interviews
Excerpt: "When you're given a system-design interview question,
follow this framework:
  1. Understand the problem and establish design scope
  2. Propose high-level design
  3. Design deep dive
  4. Wrap up
Each phase is roughly 10 minutes in a 45-minute interview. The biggest
mistake candidates make is jumping straight to component diagrams
without scoping. Ask clarifying questions in phase 1: what's the daily
active user count? What's the read/write ratio?"

Expected JSON:
{
  "procedures": []
}

(This is structural advice for interview pacing, not a step-by-step
procedure to follow. The numbered list is an outline, not actions to
execute. Returning a "procedure" here would be fabrication — there's
no concrete action like "edit a config file" or "run a command.")

--- COMMON PITFALLS ---

1. Treating numbered outlines as procedures. If the "steps" are
   subjects to discuss rather than actions to take, return [].

2. Inventing commands. If the chapter doesn't show a command, omit
   the `command` field. Do NOT synthesize what the command "would
   probably be."

3. Splitting one procedure into many. A single workflow with 10
   steps is one procedure with 10 steps, not 10 procedures with one
   step each.

4. Merging two distinct procedures. If a chapter has both a setup
   procedure and a separate teardown procedure, those are TWO
   procedures with different names, preconditions, and postconditions.

5. Filling failure_modes with speculation. Only record failure modes
   the chapter explicitly discusses. Empty string is fine.

6. Setting implements_pattern when no pattern is named. The default
   is null — only set this when the chapter ties the procedure to
   a specific named Pattern.

7. Mistaking a comparison or trade-off discussion for a procedure.
   "Option A is X; option B is Y" sequences are exposition, not
   instructions to follow. Even when the chapter eventually picks an
   option ("we'll use Option A"), the discussion itself is conceptual.
   The procedure (if any) is in the *implementation* section that
   follows, not in the comparison.

8. Treating a results-section "we ran this experiment" narrative as
   a procedure. Procedures are forward-facing instructions for the
   reader. A retrospective "here's what we did" is exposition.

End of guidance. Now extract procedures from the chapter provided below."""


# Alignment uses Sonnet 4.6 (better at nuanced CORROBORATES/CONTRADICTS
# judgment) with a 2048-token cache minimum. Base ALIGNMENT_SYSTEM_PROMPT
# is ~445 tokens; padding adds disambiguation guidance + worked examples
# until the prompt is comfortably above 2048.
EXTENDED_ALIGNMENT_PROMPT = ALIGNMENT_SYSTEM_PROMPT + """


--- DISAMBIGUATION ---

CORROBORATES is the right call when the doc and the chapter are
saying *the same specific thing* about the concept. Two questions
disambiguate:

1. Is the agreement specific or generic?
   "Both mention concept X" is generic — every chapter that names X
   technically corroborates the doc. That's not useful. Look for
   agreement on a *mechanism*, *defaulted behavior*, *recommended
   approach*, or *exact API shape*.

2. Could the chapter conceivably contradict the doc on this point,
   even though it doesn't? If the chapter is too high-level to
   support OR contradict the doc's specific claim, skip — there's
   no real alignment to judge.

CONTRADICTS is the right call when the doc and the chapter make
substantively *different* claims:
  * Different mechanism for the same outcome (and they each describe
    why their mechanism is correct)
  * Different defaults / required configuration
  * The doc shows an API the chapter labels deprecated, or vice versa
  * Different recommendations for the same scenario

NOT contradictions:
  * Different vocabulary for the same idea ("idempotent producer" in
    the doc vs. "exactly-once producer" in the chapter)
  * The doc covers a feature the chapter doesn't mention
  * The chapter discusses a pre-version of the doc's feature

(skip) — the most-common correct answer. Use it when:
  * Confidence is below ~0.7
  * The chapter excerpt is too short to support the claim
  * The concept is generic ("data", "system", "process")
  * The chapter happens to mention the concept in passing without
    describing it

Specificity beats coverage. A handful of high-confidence edges is
worth more than a list of weak ones.

--- WORKED EXAMPLES ---

EXAMPLE 1 — strong corroboration on a specific mechanism.

DOC: Apache Kafka, "Producer Configuration" (2026 snapshot)
"To enable exactly-once delivery, set enable.idempotence=true on
the producer. The broker assigns a producer ID and a sequence
number; duplicate writes from a retry are dropped on the broker
side."

CHAPTER excerpt: Confluent's "Designing Event-Driven Systems" by
Stopford (chapter "Idempotent Producers"):
"The Kafka idempotent producer prevents duplicate writes on retry
by tagging each request with a producer ID and a per-partition
sequence number. The broker tracks the last sequence per producer
ID and rejects out-of-order or repeated writes."

Expected output:
{
  "alignments": [
    {
      "concept_id": <Idempotent Producer concept_id>,
      "to_chapter_id": <Stopford ch concept_id>,
      "relation_type": "CORROBORATES",
      "confidence": 0.92,
      "explanation": "Both attribute idempotency to producer-ID + sequence-number tracking on the broker side."
    }
  ]
}

EXAMPLE 2 — contradiction on a default-behavior claim.

DOC: Apache Kafka 3.x, "Producer Configuration" (2026 snapshot)
"enable.idempotence is true by default starting Kafka 3.0. No
explicit configuration is needed for the basic exactly-once
producer."

CHAPTER excerpt: "Kafka: The Definitive Guide" 1st ed. (2017),
chapter on producer configs:
"By default, Kafka producers do not enable idempotence. To opt in,
set enable.idempotence=true and verify acks=all. Without this
configuration, the producer will silently duplicate messages on
retry."

Expected output:
{
  "alignments": [
    {
      "concept_id": <Idempotent Producer concept_id>,
      "to_chapter_id": <Definitive Guide 1st ed ch>,
      "relation_type": "CONTRADICTS",
      "confidence": 0.88,
      "explanation": "Doc states idempotence is default in Kafka 3.0+; the 2017 chapter states it is off by default and must be opted in."
    }
  ]
}

EXAMPLE 3 — generic mention, no real alignment.

DOC: PostgreSQL, "Backup and Restore" (2026 snapshot)
"PostgreSQL supports several backup strategies including pg_dump
for logical backups, pg_basebackup for physical backups, and PITR
via WAL archiving."

CHAPTER excerpt: A general "Database Internals" book chapter
that says: "Most databases offer multiple backup strategies; the
choice depends on RTO/RPO requirements." (no specifics about
PostgreSQL)

Expected output:
{
  "alignments": []
}

(The chapter mentions backup strategies generically. There's
nothing specific to corroborate or contradict.)

EXAMPLE 4 — multiple alignments per section.

DOC: Apache Spark, "Structured Streaming" (2026 snapshot, fragment)
"Structured Streaming uses micro-batches by default; switch to
continuous processing mode with .trigger(continuous='1 second')
for sub-second latency. Watermarks must be specified to drop
late events. Output mode 'append' is the default; use 'update'
for in-place updates and 'complete' to replay the entire result
table."

CHAPTER excerpts:
  * Karau et al., "Learning Spark" 2nd ed., ch. on Structured
    Streaming: "Structured Streaming defaults to micro-batch
    processing. The trigger() method controls batch frequency..."
    (matches micro-batch claim, no continuous-mode discussion)
  * Damji et al., "Learning Spark" 2nd ed. (different chapter):
    "Watermarks tell Spark when to drop late-arriving data. Without
    a watermark, Spark must keep all state indefinitely."
    (matches watermark claim)

Expected output:
{
  "alignments": [
    {"concept_id": <Structured Streaming>, "to_chapter_id": <Karau ch>,
     "relation_type": "CORROBORATES", "confidence": 0.85,
     "explanation": "Both describe Structured Streaming as micro-batch by default."},
    {"concept_id": <Watermark>, "to_chapter_id": <Damji ch>,
     "relation_type": "CORROBORATES", "confidence": 0.90,
     "explanation": "Both explain watermarks as the mechanism that lets Spark drop late-arriving data and bound state."}
  ]
}

EXAMPLE 5 — passing-mention false candidate.

DOC: LangChain, "Memory" (2026 snapshot)
"LangChain provides several memory implementations:
ConversationBufferMemory stores the entire conversation;
ConversationSummaryMemory keeps an LLM-generated summary;
VectorStoreRetrieverMemory pulls relevant context from a
vector store via semantic search."

CHAPTER excerpt: A general LLM book that has a paragraph in an
intro chapter saying: "LangChain offers a memory abstraction
that we'll explore in chapter 12." (no actual content about
memory mechanisms)

Expected output:
{
  "alignments": []
}

(The chapter only mentions LangChain memory in passing. Nothing
substantive to corroborate.)

EXAMPLE 6 — corroboration that's lower-confidence due to
overlap-not-equality.

DOC: Delta Lake, "Time Travel" (2026 snapshot)
"Delta Lake lets you query a table as of a specific version or
timestamp. Use VERSION AS OF or TIMESTAMP AS OF in SQL, or the
.option('versionAsOf', N) on the DataFrame reader."

CHAPTER excerpt: "Lakehouse" book, ch. on Delta Lake:
"Delta Lake's transaction log enables time-travel queries by
keeping all historical commits. Older versions can be queried
either by version number (VERSION AS OF) or timestamp."

Expected output:
{
  "alignments": [
    {
      "concept_id": <Time Travel concept_id>,
      "to_chapter_id": <Lakehouse ch>,
      "relation_type": "CORROBORATES",
      "confidence": 0.78,
      "explanation": "Both describe time travel as version- or timestamp-based queries enabled by the Delta transaction log; chapter doesn't include the DataFrame reader option but covers the SQL form."
    }
  ]
}

(Confidence at 0.78 because the chapter covers the SQL form but
not the .option() form — partial overlap. Still CORROBORATES on
the SQL claim, which is the bulk of the doc's content.)

--- CONFIDENCE CALIBRATION ---

0.95–1.00  Identical claim, almost-identical wording. Rare.
0.85–0.94  Same mechanism / default / recommendation, different
           phrasing. Most explicit alignments land here.
0.70–0.84  Same mechanism but partial overlap (chapter covers the
           main case, doc covers a few extras the chapter doesn't
           mention). Or: clear alignment but the chapter excerpt
           is shorter than ideal.
0.60–0.69  Plausible alignment but the chapter is somewhat tangential.
           Use sparingly.
< 0.60     Skip — confidence isn't there.

For CONTRADICTS specifically, lean on the higher end. A
weak-confidence contradiction is usually better skipped than
recorded as low-confidence — false-positive contradictions
mislead the Migration Guide and Currency Report generators.

EXAMPLE 7 — a near-miss that should NOT contradict.

DOC: PostgreSQL, "Vacuuming Basics" (2026 snapshot)
"PostgreSQL automatically runs autovacuum to reclaim storage and
update statistics. The default schedule triggers autovacuum on a
table when more than 20% of its rows have been updated/deleted.
You can tune autovacuum_vacuum_scale_factor to adjust this."

CHAPTER excerpt: "PostgreSQL: Up and Running" 3rd ed., ch. on
maintenance:
"VACUUM in PostgreSQL reclaims storage from dead tuples. You can
run it manually with VACUUM FULL or rely on the autovacuum daemon
to keep tables in good shape."

Expected output:
{
  "alignments": [
    {
      "concept_id": <Autovacuum concept_id>,
      "to_chapter_id": <PUR ch>,
      "relation_type": "CORROBORATES",
      "confidence": 0.72,
      "explanation": "Both describe autovacuum as the daemon that reclaims dead-tuple storage; chapter doesn't cover the 20% threshold detail but corroborates the general mechanism."
    }
  ]
}

(Lower confidence 0.72 because the chapter is at a higher level
than the doc — they agree, but the chapter doesn't engage with
the specific tuning detail.)

EXAMPLE 8 — a concept that is too generic to align.

DOC: Apache Kafka, "Reliability" (2026 snapshot)
"Kafka achieves reliable message delivery via replication, acks,
and idempotent producers."

CHAPTER excerpt: A book chapter that has the word "reliability"
in its title but discusses general distributed-systems theory:
"Reliability in distributed systems is achieved through
redundancy, consensus, and failure detection."

Expected output:
{
  "alignments": []
}

("Reliability" is too generic for an alignment edge here. Both
mention it but they're operating on different levels — Kafka's
specific implementation vs. general theory. Skip.)

--- COMMON ALIGNMENT TRAPS ---

Trap 1: Treating any topical overlap as CORROBORATES.
  If two passages happen to discuss the same broad topic but
  don't make matching specific claims, that's not corroboration.
  Skip it.

Trap 2: Calling something a CONTRADICTS when it's just an
  evolution/version difference that the doc itself acknowledges.
  If the doc says "since version X, behavior changed" and the
  chapter describes the pre-X behavior accurately, the chapter
  isn't wrong — it's just dated. This case CAN be CONTRADICTS
  if the chapter still presents itself as current best-practice
  for the user; otherwise skip.

Trap 3: Creating duplicate edges for the same (concept,
  chapter) pair. Emit at most one alignment per pair.

Trap 4: Hallucinating a concept_id or chapter_id. Use ONLY
  the ids the prompt provided.

Trap 5: Padding with skip-grade alignments to "look thorough."
  Empty alignments is a valid, common, correct answer.

EXAMPLE 9 — handling deprecated / replaced APIs.

DOC: Apache Spark, "DataFrame API" (2026 snapshot)
"For row counting, use df.count() or df.cache().count() if you'll
reuse the DataFrame. The deprecated df.persist().count() pattern
should be replaced with df.cache().count()."

CHAPTER excerpt: "Learning Spark" 1st ed. (2015):
"To force a DataFrame into memory before counting, use
df.persist().count(). This both materializes the DataFrame and
returns the row count."

Expected output:
{
  "alignments": [
    {
      "concept_id": <DataFrame concept_id>,
      "to_chapter_id": <Learning Spark 1st ed ch>,
      "relation_type": "CONTRADICTS",
      "confidence": 0.85,
      "explanation": "Doc deprecates df.persist().count() in favor of df.cache().count(); the 2015 chapter still recommends df.persist().count() as the canonical pattern."
    }
  ]
}

(This is a genuine CONTRADICTS — the doc explicitly labels the
chapter's recommendation as deprecated. The 0.85 confidence is
warranted because the doc is unambiguous.)

EXAMPLE 10 — a section discussing concept the chapter introduces
but doesn't elaborate.

DOC: MLflow, "Model Registry" (2026 snapshot)
"The Model Registry tracks model versions, manages stage
transitions (Staging → Production → Archived), and supports
webhooks for CI/CD integration."

CHAPTER excerpt: An MLOps overview book that lists Model Registry
in a table of MLflow features but only describes it in one
sentence: "MLflow Model Registry — a centralized model store for
managing the model lifecycle."

Expected output:
{
  "alignments": []
}

(The chapter mentions Model Registry but doesn't engage with any
specific claim from the doc — stage transitions, webhooks, CI/CD
integration. Skip.)

--- WHEN TO LEAVE alignments EMPTY ---

It's correct (and common) to return an empty alignments array.
Emit alignments: [] when:

  * No candidate chapter contains substantive content matching
    the doc's specific claims.
  * The chapter excerpts only mention the concept generically.
  * Confidence on every potential alignment is below 0.70.
  * The concept itself is too generic ("data", "process", "system",
    "user").

Doing fewer high-confidence edges is far more useful than padding
the response with weak ones.

--- FINAL CHECKS BEFORE EMITTING ---

Before adding an alignment to the output, verify:
  1. The concept_id you're attaching the edge to is one of the
     concept_ids in CONCEPTS_WITH_CANDIDATES — don't synthesize one.
  2. The to_chapter_id is one of the candidate chapter ids the
     prompt provided for that concept.
  3. The relation_type is exactly "CORROBORATES" or "CONTRADICTS"
     (case-sensitive). Skip = emit no row, not a different relation.
  4. The explanation cites the specific point of agreement /
     disagreement (one sentence, not a summary of the chapter).

End of guidance. Now classify each (concept, candidate_chapter) pair below."""

ALIGNMENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "integer"},
                    "to_chapter_id": {"type": "integer"},
                    "relation_type": {
                        "type": "string",
                        "enum": ["CORROBORATES", "CONTRADICTS"],
                    },
                    "confidence": {"type": "number"},
                    "explanation": {"type": "string"},
                },
                "required": ["concept_id", "to_chapter_id",
                             "relation_type", "confidence", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["alignments"],
    "additionalProperties": False,
}


# Task spec — pairs a (padded) system prompt with its output schema. Add a
# new entry here when adding a new task type.
TASKS: dict[str, tuple[str, dict]] = {
    "concepts":   (EXTENDED_ENTITY_PROMPT,    CONCEPT_OUTPUT_SCHEMA),
    "procedures": (EXTENDED_PROCEDURE_PROMPT, PROCEDURE_OUTPUT_SCHEMA),
    "alignment":  (EXTENDED_ALIGNMENT_PROMPT, ALIGNMENT_OUTPUT_SCHEMA),
}


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


def _verify_cache_eligibility(task: str) -> None:
    """Hard-fail at startup if the padded system prompt is below the
    cache minimum for the model this task uses."""
    prompt, _ = TASKS[task]
    n = len(_TOKENIZER.encode(prompt))
    threshold = TASK_CACHE_MIN[task]
    LOG.info("EXTENDED %s prompt: %d tokens (cache min: %d)",
             task.upper(), n, threshold)
    if n < threshold:
        raise RuntimeError(
            f"EXTENDED {task.upper()} prompt is {n} tokens; "
            f"need ≥{threshold} to clear the cache minimum. "
            f"Fix by extending the prompt."
        )


def _build_request(custom_id: str, user_text: str, model: str,
                    task: str) -> dict:
    """Build one Batch API request with cache_control on the system prefix."""
    prompt, schema = TASKS[task]
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema}
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
    _verify_cache_eligibility(args.task)

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
        requests.append(_build_request(custom_id, user_text, args.model,
                                       args.task))
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
    # extract_batch.py prep writes result_path as project-root-relative
    # (e.g., "data/batch-runs/<dir>/results/result_<id>.json"). Resolve
    # relative to PROJECT_ROOT, not to manifest_path.parent — otherwise
    # the path doubles up.
    #
    # Two manifest shapes: extract_batch / extract_procedures use
    # ``chapters`` keyed by ``chapter_id``; refresh_docs alignment uses
    # ``sections`` keyed by ``doc_section_id``.
    custom_id_to_result_path: dict[str, Path] = {}
    if "chapters" in manifest:
        for entry in manifest["chapters"]:
            rp = Path(entry["result_path"])
            if not rp.is_absolute():
                rp = PROJECT_ROOT / rp
            custom_id_to_result_path[f"chapter-{entry['chapter_id']}"] = rp
    elif "sections" in manifest:
        # Two flavors of section manifest:
        #   - alignment manifest: one prompt/result per section
        #   - refresh-extraction manifest: each section has both an
        #     entity prompt (result_path) and a procedure prompt
        #     (procedure_result_path). Register both custom_ids so the
        #     same fetch handles either run type.
        for entry in manifest["sections"]:
            sid = entry["doc_section_id"]
            rp = Path(entry["result_path"])
            if not rp.is_absolute():
                rp = PROJECT_ROOT / rp
            custom_id_to_result_path[f"section-{sid}-align"] = rp
            custom_id_to_result_path[f"section-{sid}-concept"] = rp
            proc_rp_str = entry.get("procedure_result_path")
            if proc_rp_str:
                proc_rp = Path(proc_rp_str)
                if not proc_rp.is_absolute():
                    proc_rp = PROJECT_ROOT / proc_rp
                custom_id_to_result_path[f"section-{sid}-proc"] = proc_rp
    else:
        LOG.error("manifest has neither 'chapters' nor 'sections' key")
        return 1

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


def _build_request_with_max_tokens(custom_id: str, user_text: str,
                                    model: str, task: str,
                                    max_tokens: int) -> dict:
    """Like _build_request but caller specifies max_tokens.

    Used by recovery to bump the per-request output budget — the
    most common failure mode at scale is truncated JSON when the
    model hits 2048 max_tokens before finishing the entities/
    relations/procedures list.
    """
    prompt, schema = TASKS[task]
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema}
            },
        },
    }


def _build_request_no_strict_schema(custom_id: str, user_text: str,
                                     model: str, task: str) -> dict:
    """Like _build_request but drops output_config.format.

    Used as a fallback for chapters that hit ``Grammar compilation
    timed out`` errors — Anthropic's structured-output schema
    compiler can't process those particular inputs in its time
    budget. The system prompt already instructs JSON-only output;
    parse_llm_json on the receiving side handles markdown fences.
    """
    prompt, _schema = TASKS[task]
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_text}],
            # NO output_config.format — relax structured output
        },
    }


_ALIGNMENT_BOUNDARY = "--- DOC SECTION TO CLASSIFY ---"


def _build_alignment_request(custom_id: str, dynamic_user_text: str,
                              model: str) -> dict:
    """Build one Batch API request for alignment.

    The on-disk prompt files were written by ``refresh_docs.py
    align-prep`` and contain the *unpadded* ALIGNMENT_SYSTEM_PROMPT
    as a prefix. We discard that prefix and use the padded
    EXTENDED_ALIGNMENT_PROMPT as the system block instead — gives
    cache eligibility plus richer disambiguation guidance.
    """
    prompt, schema = TASKS["alignment"]
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": 4096,  # alignment outputs can be longer
            "system": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": dynamic_user_text}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema}
            },
        },
    }


def do_submit_alignment(args: argparse.Namespace) -> int:
    """Submit alignment requests via Batch API.

    Reads an alignment manifest (the ``manifest.json`` that
    ``refresh_docs.py align-prep`` writes — different shape from
    the chapter-extraction manifest: ``sections`` not ``chapters``,
    and each entry has a pre-built ``prompt_path`` on disk).
    Splits each prompt at the static/dynamic boundary, uses the
    padded EXTENDED_ALIGNMENT_PROMPT as the cached system block,
    and submits.

    Custom_id format: ``section-<doc_section_id>-align``.
    """
    _verify_cache_eligibility("alignment")

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        LOG.error("manifest not found: %s", manifest_path)
        return 1
    manifest = json.loads(manifest_path.read_text())
    sections = manifest.get("sections", [])
    output_dir = manifest_path.parent
    LOG.info("alignment manifest: %d sections", len(sections))

    state_path = manifest_path.parent / "batch_state.json"
    if state_path.exists() and not args.force:
        LOG.error("state file already exists at %s; pass --force",
                  state_path)
        return 1

    requests = []
    skipped_existing = 0
    for entry in sections:
        section_id = entry["doc_section_id"]
        rp_str = entry["result_path"]
        rp = Path(rp_str)
        if not rp.is_absolute():
            rp = PROJECT_ROOT / rp
        if rp.exists() and not args.force:
            skipped_existing += 1
            continue
        prompt_path = Path(entry["prompt_path"])
        if not prompt_path.is_absolute():
            prompt_path = PROJECT_ROOT / prompt_path
        if not prompt_path.exists():
            LOG.warning("prompt file missing for section %d: %s",
                        section_id, prompt_path)
            continue
        full_prompt = prompt_path.read_text()
        # Split out the dynamic per-section content.
        if _ALIGNMENT_BOUNDARY in full_prompt:
            _static, dynamic = full_prompt.split(_ALIGNMENT_BOUNDARY, 1)
            dynamic_text = _ALIGNMENT_BOUNDARY + dynamic
        else:
            # Fallback: use the whole prompt as user content.
            dynamic_text = full_prompt
        custom_id = f"section-{section_id}-align"
        requests.append(_build_alignment_request(
            custom_id, dynamic_text, args.model
        ))

    if skipped_existing:
        LOG.info("skipping %d sections with existing results", skipped_existing)
    if not requests:
        LOG.warning("no requests built — nothing to dispatch")
        return 0
    LOG.info("submitting alignment: %d requests with model=%s",
             len(requests), args.model)

    if args.dry_run:
        LOG.info("DRY-RUN — not submitting")
        return 0

    client = anthropic.Anthropic()
    state = DispatchState(
        manifest_path=str(manifest_path),
        output_dir=str(output_dir),
        model=args.model,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    chunk_size = args.batch_size
    for i in range(0, len(requests), chunk_size):
        chunk = requests[i:i + chunk_size]
        chunk_cids = [r["custom_id"] for r in chunk]
        LOG.info("submitting batch %d (%d requests)",
                 i // chunk_size + 1, len(chunk))
        batch = client.messages.batches.create(requests=chunk)
        LOG.info("  -> batch_id=%s status=%s",
                 batch.id, batch.processing_status)
        state.batches.append(BatchEntry(
            batch_id=batch.id,
            custom_ids=chunk_cids,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        ))
        _save_state(state, state_path)

    LOG.info("submitted %d batches; state saved to %s",
             len(state.batches), state_path)
    return 0


# Boundaries used by refresh_docs.py when it writes per-section prompt files.
# Both the entity prompt and the procedure prompt embed a payload header
# whose first line is ``DOC_SECTION_ID:`` — that's our reliable split point
# (the unpadded SYSTEM_PROMPT prefix above never contains that token).
_SECTION_PAYLOAD_MARKER = "DOC_SECTION_ID:"


def _strip_unpadded_prefix(prompt_text: str) -> str:
    """Return the dynamic per-section payload from a refresh-written prompt.

    refresh_docs.py writes prompts as
        ``{UNPADDED_SYSTEM_PROMPT}\\n\\n[boundary]{header}\\n...``
    where the header always begins with ``DOC_SECTION_ID:``. We discard
    everything up to (but not including) that marker so we can substitute
    the padded EXTENDED_* prompt as the cached system block instead.
    """
    idx = prompt_text.find(_SECTION_PAYLOAD_MARKER)
    if idx < 0:
        raise ValueError(
            f"malformed section prompt: no {_SECTION_PAYLOAD_MARKER!r} marker"
        )
    return prompt_text[idx:]


def do_submit_doc_extraction(args: argparse.Namespace) -> int:
    """Submit per-section concept + procedure extraction via Batch API.

    Reads a refresh-style manifest (``sections[]`` shape with both
    ``prompt_path`` for entity extraction and ``procedure_prompt_path``
    for procedure extraction) and dispatches up to two requests per
    section:
        custom_id ``section-<id>-concept`` (concepts task, Haiku 4.5)
        custom_id ``section-<id>-proc``    (procedures task, Haiku 4.5)

    The on-disk prompt files contain the *unpadded* SYSTEM_PROMPT plus the
    per-section payload. We strip the prefix and use the padded
    ``EXTENDED_ENTITY_PROMPT`` / ``EXTENDED_PROCEDURE_PROMPT`` as the cached
    system block — same trick as ``submit-alignment``.

    Both tasks share the same model (Haiku 4.5) and 4096-token cache
    minimum, so they go in a single batch_state.json.

    Skips entries whose result_path / procedure_result_path already
    exists, so re-running this after a partial fetch resumes cleanly.
    """
    _verify_cache_eligibility("concepts")
    _verify_cache_eligibility("procedures")

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        LOG.error("manifest not found: %s", manifest_path)
        return 1
    manifest = json.loads(manifest_path.read_text())
    sections = manifest.get("sections", [])
    if not sections:
        LOG.error("manifest has no 'sections' entries: %s", manifest_path)
        return 1
    output_dir = manifest_path.parent
    LOG.info("doc-extraction manifest: %d sections", len(sections))

    state_path = manifest_path.parent / "batch_state.json"
    if state_path.exists() and not args.force:
        LOG.error("state file already exists at %s; pass --force to override",
                  state_path)
        return 1

    requests: list[dict] = []
    skipped_existing_concept = 0
    skipped_existing_proc = 0
    skipped_no_proc = 0
    for entry in sections:
        section_id = entry["doc_section_id"]

        # Concept request
        ent_rp_str = entry.get("result_path")
        ent_pp_str = entry.get("prompt_path")
        if ent_rp_str and ent_pp_str:
            ent_rp = Path(ent_rp_str)
            if not ent_rp.is_absolute():
                ent_rp = PROJECT_ROOT / ent_rp
            if ent_rp.exists() and not args.force:
                skipped_existing_concept += 1
            else:
                ent_pp = Path(ent_pp_str)
                if not ent_pp.is_absolute():
                    ent_pp = PROJECT_ROOT / ent_pp
                if not ent_pp.exists():
                    LOG.warning("concept prompt missing for section %d: %s",
                                section_id, ent_pp)
                else:
                    dynamic = _strip_unpadded_prefix(ent_pp.read_text())
                    user_text = (
                        f"--- DOC SECTION TO EXTRACT ---\n\n{dynamic}"
                    )
                    requests.append(_build_request(
                        f"section-{section_id}-concept",
                        user_text, args.model, "concepts",
                    ))

        # Procedure request
        proc_rp_str = entry.get("procedure_result_path")
        proc_pp_str = entry.get("procedure_prompt_path")
        if not (proc_rp_str and proc_pp_str):
            skipped_no_proc += 1
            continue
        proc_rp = Path(proc_rp_str)
        if not proc_rp.is_absolute():
            proc_rp = PROJECT_ROOT / proc_rp
        if proc_rp.exists() and not args.force:
            skipped_existing_proc += 1
            continue
        proc_pp = Path(proc_pp_str)
        if not proc_pp.is_absolute():
            proc_pp = PROJECT_ROOT / proc_pp
        if not proc_pp.exists():
            LOG.warning("procedure prompt missing for section %d: %s",
                        section_id, proc_pp)
            continue
        dynamic = _strip_unpadded_prefix(proc_pp.read_text())
        user_text = f"--- DOC SECTION TO EXTRACT ---\n\n{dynamic}"
        requests.append(_build_request(
            f"section-{section_id}-proc",
            user_text, args.model, "procedures",
        ))

    if skipped_existing_concept:
        LOG.info("skipping %d sections with existing concept results",
                 skipped_existing_concept)
    if skipped_existing_proc:
        LOG.info("skipping %d sections with existing procedure results",
                 skipped_existing_proc)
    if skipped_no_proc:
        LOG.info("%d sections lack procedure prompt entries (older manifest)",
                 skipped_no_proc)
    if not requests:
        LOG.warning("no requests built — nothing to dispatch")
        return 0
    LOG.info("dispatching %d requests with model=%s, batch-size=%d",
             len(requests), args.model, args.batch_size)

    if args.dry_run:
        LOG.info("DRY-RUN — not submitting")
        return 0

    client = anthropic.Anthropic()
    state = DispatchState(
        manifest_path=str(manifest_path),
        output_dir=str(output_dir),
        model=args.model,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    for i in range(0, len(requests), args.batch_size):
        chunk = requests[i:i + args.batch_size]
        chunk_cids = [r["custom_id"] for r in chunk]
        LOG.info("submitting batch %d (%d requests, custom_ids %s..%s)",
                 i // args.batch_size + 1, len(chunk),
                 chunk[0]["custom_id"], chunk[-1]["custom_id"])
        batch = client.messages.batches.create(requests=chunk)
        LOG.info("  -> batch_id=%s status=%s",
                 batch.id, batch.processing_status)
        state.batches.append(BatchEntry(
            batch_id=batch.id,
            custom_ids=chunk_cids,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        ))
        _save_state(state, state_path)

    LOG.info("submitted %d batches; state saved to %s",
             len(state.batches), state_path)
    return 0


def do_recover(args: argparse.Namespace) -> int:
    """Resubmit chapters that need recovery.

    Recovery scope = chapters in the source manifest whose result
    file is (a) missing, (b) replaced by a ``.error.json`` sidecar,
    or (c) present but doesn't parse as JSON. The third case is the
    common one at scale: requests that hit ``max_tokens`` produce
    truncated output that fails JSON parse downstream.

    The recovery batch resubmits with ``max_tokens=4096`` by default
    (vs the 2048 production default), giving the model headroom to
    finish the entity / relation / procedure list. ``--no-strict-schema``
    drops ``output_config.format`` for cases where Anthropic's FSM
    compilation times out.

    Writes a new state file at ``<state>.recovery[.no-schema].json``
    so the original state is preserved. Result files land in the
    same results/ directory used by the original run, overwriting
    any prior ``.error.json`` sidecars.
    """
    _verify_cache_eligibility(args.task)

    state_path = Path(args.state).resolve()
    state = _load_state(state_path)
    client = anthropic.Anthropic()

    # Audit which chapters need recovery — work from disk, not the API.
    # Catches the common case (parse failures from max_tokens
    # truncation) that the API doesn't flag as errors.
    manifest_path = Path(state.manifest_path)
    manifest = json.loads(manifest_path.read_text())
    LOG.info("auditing %d chapters in manifest...", len(manifest["chapters"]))

    needs_recovery: list[int] = []  # chapter_ids
    parse_fail = error_sidecar = missing = 0
    for entry in manifest["chapters"]:
        cid = entry["chapter_id"]
        rp_str = entry["result_path"]
        rp = Path(rp_str)
        if not rp.is_absolute():
            rp = PROJECT_ROOT / rp
        ep = rp.with_suffix(".error.json")
        if rp.exists():
            try:
                json.loads(rp.read_text())  # plain parse — no markdown stripping
            except json.JSONDecodeError:
                parse_fail += 1
                needs_recovery.append(cid)
        elif ep.exists():
            error_sidecar += 1
            needs_recovery.append(cid)
        else:
            missing += 1
            needs_recovery.append(cid)
    LOG.info("recovery scope: %d chapters (%d parse-fail, "
             "%d error-sidecar, %d missing)",
             len(needs_recovery), parse_fail, error_sidecar, missing)

    if not needs_recovery:
        LOG.info("no chapters to recover")
        return 0

    # Load chapter content + build requests. RO catalog access —
    # must not run while another process holds the RW lock.
    import duckdb  # pylint: disable=import-outside-toplevel
    catalog = PROJECT_ROOT / "data" / "catalog.ddb"
    conn = duckdb.connect(str(catalog), read_only=True)

    requests = []
    for chapter_id in needs_recovery:
        try:
            chapter = _load_chapter(conn, chapter_id)
        except ValueError as exc:
            LOG.warning("skip chapter_id=%d: %s", chapter_id, exc)
            continue
        custom_id = f"chapter-{chapter_id}"
        user_text = (
            f"--- CHAPTER TO EXTRACT ---\n\n"
            f"{_build_user_prompt(chapter)}\n\n"
            f"Respond with JSON only. No prose, no markdown fences."
        )
        if args.no_strict_schema:
            req = _build_request_no_strict_schema(
                custom_id, user_text, args.model, args.task
            )
            req["params"]["max_tokens"] = args.max_tokens
        else:
            req = _build_request_with_max_tokens(
                custom_id, user_text, args.model, args.task, args.max_tokens
            )
        requests.append(req)
    conn.close()

    if not requests:
        LOG.warning("no requests built — nothing to dispatch")
        return 0
    LOG.info("submitting recovery: %d requests, max_tokens=%d, "
             "no_strict_schema=%s",
             len(requests), args.max_tokens, args.no_strict_schema)

    if args.dry_run:
        LOG.info("DRY-RUN — not submitting")
        return 0

    # Chunk into batches of <= 5000 to stay under 256MB-per-batch limit.
    suffix = ".no-schema" if args.no_strict_schema else ""
    recovery_state_path = state_path.with_suffix(
        f".recovery{suffix}.json"
    )
    recovery_state = DispatchState(
        manifest_path=state.manifest_path,
        output_dir=state.output_dir,
        model=state.model,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    chunk_size = args.batch_size
    for i in range(0, len(requests), chunk_size):
        chunk = requests[i:i + chunk_size]
        chunk_cids = [r["custom_id"] for r in chunk]
        LOG.info("submitting recovery batch %d (%d requests)",
                 i // chunk_size + 1, len(chunk))
        batch = client.messages.batches.create(requests=chunk)
        LOG.info("  -> batch_id=%s status=%s",
                 batch.id, batch.processing_status)
        recovery_state.batches.append(BatchEntry(
            batch_id=batch.id,
            custom_ids=chunk_cids,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        ))
        _save_state(recovery_state, recovery_state_path)

    LOG.info("recovery state saved to %s", recovery_state_path)
    LOG.info("next: poll + fetch:")
    LOG.info("  batch_dispatch.py poll  --state %s", recovery_state_path)
    LOG.info("  batch_dispatch.py fetch --state %s", recovery_state_path)
    return 0


def do_check_prefix(args: argparse.Namespace) -> int:
    """Sanity-check each task's EXTENDED prompt length and cache-readiness."""
    all_ok = True
    for name, (prompt, _schema) in TASKS.items():
        n = len(_TOKENIZER.encode(prompt))
        threshold = TASK_CACHE_MIN[name]
        ok = n >= threshold
        print(f"  {name:<12}  {n:>5} tokens  min={threshold}  "
              f"cache eligible: {ok}")
        if not ok:
            all_ok = False
    return 0 if all_ok else 1


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
    p_submit.add_argument("--task", choices=list(TASKS.keys()),
                          default="concepts",
                          help="Which extraction task: concepts | procedures")
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

    p_align = sub.add_parser(
        "submit-alignment",
        help="Submit alignment requests (different manifest format)",
    )
    p_align.add_argument("--manifest", required=True,
                         help="alignment manifest written by align-prep")
    p_align.add_argument("--model", default=ALIGNMENT_MODEL)
    p_align.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p_align.add_argument("--dry-run", action="store_true")
    p_align.add_argument("--force", action="store_true",
                         help="Overwrite existing state file and resubmit")
    p_align.set_defaults(func=do_submit_alignment)

    p_doc_ext = sub.add_parser(
        "submit-doc-extraction",
        help="Submit per-section concept + procedure extraction "
             "(refresh_docs sections[] manifest)",
    )
    p_doc_ext.add_argument("--manifest", required=True,
                           help="refresh manifest at "
                                "data/refresh/<ts>/snapshot_<id>/manifest.json")
    p_doc_ext.add_argument("--model", default=DEFAULT_MODEL)
    p_doc_ext.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p_doc_ext.add_argument("--dry-run", action="store_true")
    p_doc_ext.add_argument("--force", action="store_true",
                           help="Overwrite existing state file and resubmit")
    p_doc_ext.set_defaults(func=do_submit_doc_extraction)

    p_recover = sub.add_parser(
        "recover",
        help="Resubmit chapters that errored in a prior run",
    )
    p_recover.add_argument("--state", required=True,
                           help="Source state file from the original run")
    p_recover.add_argument("--task", choices=list(TASKS.keys()),
                           required=True)
    p_recover.add_argument("--model", default=DEFAULT_MODEL)
    p_recover.add_argument(
        "--max-tokens", type=int, default=4096,
        help="Per-request output budget (default 4096; production "
             "default was 2048; truncation is the common failure).",
    )
    p_recover.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help="Chunk requests into batches of this size",
    )
    p_recover.add_argument(
        "--no-strict-schema", action="store_true",
        help="Drop output_config.format; use this as a follow-up "
             "for any chapters that still fail with grammar timeouts.",
    )
    p_recover.add_argument("--dry-run", action="store_true")
    p_recover.set_defaults(func=do_recover)

    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
