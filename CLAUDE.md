# myPub v2

This project is a knowledge base system indexing ~345 technical ePubs,
with live-doc augmentation (Context7 / DeepWiki / GitHub) and a Skills
Factory for generating Claude Skills packages.

## Key locations

- Local DuckDB: `data/catalog.ddb`
- ePub collection: `~/Documents/eBooks`
- Generated Skills packages: `data/generated-packages/`
- Architecture spec: `docs/mypub-v2-architecture.md`
- Execution plan: `docs/mypub-v2-execution-plan.md`

## How to interact with the knowledge base

There are two surfaces. Most queries go through MCP tools — talk to
Claude in natural language and the tool routing happens automatically.
Slash commands are reserved for multi-step workflows that benefit from
deterministic orchestration.

### MCP tools (talk to Claude in natural language)

The `mypub-kb` server (registered in `.mcp.json`) exposes:

- **`search_chapters(query, mode, limit, weight_profile, auto_discover, selection_strategy)`** —
  hybrid retrieval over book chapters + live doc sections. `mode="interactive"`
  returns `{primary, corroborations, conflicts, all_scored, by_modality, discovery}`;
  `mode="generation"` returns a curated list under one of three §8.3
  selection strategies.
- **`compare_concept_across_authors(concept_name, limit_per_author)`** —
  per-author roll-up of chapters that discuss a concept.
- **`find_prerequisites(concept_name, max_depth)`** — recursive walk
  over `REQUIRES` edges, returning each prerequisite at its shortest
  depth.
- **`disambiguate_discovery(source, identifier, display_name, query_term)`** —
  closes the auto-discovery loop when search returns an `asked_user`
  outcome (multiple candidate libraries with similar scores). Idempotent.

Most natural-language queries — "search the kb for event sourcing",
"compare how my authors discuss CQRS", "what are the prerequisites for
Kafka Streams" — route automatically to the right tool. There's no
slash command needed for these.

### Slash commands (workflow shortcuts)

A slash command is here only when it composes multiple tools, drives an
interactive multi-step flow, or carries workflow context that natural
language wouldn't reliably reproduce.

- **`/kb-discover <term>`** — hybrid search with the explicit discovery
  loop wired in. If discovery returns `asked_user`, presents candidates,
  waits for the user to pick, runs `disambiguate_discovery`, and
  re-runs the search. Surfaces conflicts prominently.
- **`/kb-review-concepts`** — interactive review of borderline
  entity-resolution items in the EntityResolver queue.

### Planned slash commands (not yet wired)

These appear in the architecture spec but are scheduled for later phases.
Don't try to invoke them yet:

- `/kb-index <book-path>` — Phase 2/3 ingest workflow (drive ePub
  parse + extraction sub-agents)
- `/kb-refresh-docs [domain]`, `/kb-focus <domain>`,
  `/kb-pin-source <name>`, `/kb-unpin-source <name>`,
  `/kb-refresh-status` — Phase 4b proactive refresh + tier management
- `/kb-generate-skills <domain>` — Phase 5 Skills Factory

## Other MCP servers

- **`context7`** (local stdio) — primary live-doc source: vendor docs +
  well-documented OSS
- **`deepwiki`** (hosted HTTPS) — complementary doc source: AI-generated
  docs for any public GitHub repo
- **`github`** (raw fetch) — long-tail fallback for repos not in
  Context7 or DeepWiki

The auto-discovery probe order (Context7 → DeepWiki → GitHub) and
authority defaults (0.60 / 0.50 / 0.40 respectively) live in
`mcp-servers/kb-mcp/discovery.py`.

## Conventions

When generating Skills packages (Phase 5), default to **recent-doc
anchored** strategy for any domain with live doc coverage. Use
**consensus synthesis** for foundational topics. Confirm strategy choice
at the decomposition stage. For OSS libraries, link concepts to whichever
doc sources are available — Context7 where indexed, DeepWiki for
architectural grounding, GitHub raw as a last resort. Multiple sources
per concept are encouraged; the ranking engine merges them.

When in doubt about query weighting, leave `weight_profile` at the
`currency_critical_interactive` default. Use `foundational_interactive`
only when the query is explicitly about timeless concepts (algorithms,
classical CS, design pattern theory) where authority and corroboration
matter more than recency.
