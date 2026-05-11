# myPub v2

This project is a knowledge base system indexing ~541 technical ePubs,
with live-doc augmentation (Context7 / DeepWiki / GitHub) and a
generator program covering 13+ output types built on a shared Phase 7
framework.

## Key locations

- Local DuckDB: `data/catalog.ddb`
- ePub collection: `~/Documents/eBooks`
- Generated packages (all generators): `data/generated-packages/`
- Architecture spec: `docs/mypub-v2-architecture.md`
- Generator spec: `docs/mypub-v2-generators.md`
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

**Search + admin:**
- **`/kb-discover <term>`** — hybrid search with the explicit discovery
  loop wired in. If discovery returns `asked_user`, presents candidates,
  waits for the user to pick, runs `disambiguate_discovery`, and
  re-runs the search.
- **`/kb-review-concepts`** — interactive review of borderline
  entity-resolution items in the EntityResolver queue.

**Generators (Phases 5, 7-16) — the substrate's product surface:**

All generators land in `data/generated-packages/<package_name>/` and
persist provenance to `generated_*` tables (or `skill_*` for the
original Skills Factory). Each is fully deterministic except where
noted; sub-agent dispatch is layered on top for those that need prose.

| Slash command | Generator | Notes |
|---|---|---|
| `/kb-generate-skills` | Skills Factory (Phase 5) | Sub-agent driven; ranking_mode=generation |
| `/kb-concept-map` | Concept Neighborhood Map (7.2) | Mermaid + Graphviz |
| `/kb-learning-path` | Learning Path (8) | Prereq traversal + reading list |
| `/kb-cheatsheet` | Cheatsheet (9.4) | One-page procedure reference |
| `/kb-slides` | Slide-deck Outline (9.5) | 15-60 min talk skeleton |
| `/kb-content-brief` | Content Brief (9.1-9.3) | Blog/talk/design-doc/chapter outline |
| `/kb-tutorial` | Tutorial (10) | Procedure-backed exercise track |
| `/kb-pattern-catalog` | Pattern + Anti-Pattern Catalog (11) | Foundational for Bootstrap |
| `/kb-adr` | Architecture Decision Record (12) | CONTRASTS_WITH-driven options |
| `/kb-tech-assessment` | Tech Assessment (12) | Comparison matrix + recommendation |
| `/kb-migration-guide` | Migration Guide (13) | CONTRADICTS-driven; data-starved until alignment surfaces contradictions |
| `/kb-currency-report` | Currency Report (13) | doc_snapshot history audit |
| `/kb-dialog` | Dialog (14) | Architect/Practitioner divergence |
| `/kb-author-panel` | Author Panel (14) | N≥2 characters, per-topic positions |
| `/kb-bootstrap` | Project Bootstrap (15) | **User #1.** Composes Concept→Pattern→Procedure into a project tree + sub-agent prompts. Stack-aware: detects target language (python/rust/node/typescript/java/go/csharp/ruby) from the request keywords; explicit `stack` param overrides; generic skeleton if no signal |
| `/kb-refactoring` | Refactoring Playbook (15) | Anti-pattern → refactor target |
| `/kb-curriculum` | Curriculum (16) | Multi-week composite |
| `/kb-landscape` | Library Landscape (17) | Multi-job × multi-candidate orientation; rarity-weighted scoring, keyword + vector job seeding |
| `/kb-quickstart` | Quickstart (18) | First-contact for one library: install + hello-world + verify |

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
