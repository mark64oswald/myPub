# myPub v2

This project is a knowledge base system indexing ~345 technical ePubs,
with a Skills Factory for generating Claude Skills packages.

## Key locations

- Local DuckDB: `data/catalog.ddb`
- ePub collection: `~/Documents/eBooks`
- Generated Skills packages: `data/generated-packages/`

## MCP servers (stdio + hosted)

- `mypub-kb` — hybrid retrieval, ranking, Skills Factory (local stdio)
- `context7` — primary doc source: vendor docs + well-documented OSS (local stdio)
- `deepwiki` — complementary doc source: AI-generated docs for any public GitHub repo (hosted HTTPS)
- `github` — long-tail fallback: raw file fetching from any public repo (local stdio)

## Common commands

- `/kb-search <topic>` — find chapters and concepts
- `/kb-compare <concept>` — compare author perspectives
- `/kb-prereqs <concept>` — show learning prerequisites
- `/kb-generate-skills <domain>` — run the Skills Factory
- `/kb-index <book-path>` — add new book to the corpus
- `/kb-refresh-docs [domain]` — refresh live doc snapshots and re-extract changed ones
- `/kb-review-concepts` — review borderline entity-resolution matches
- `/kb-focus <domain>` — elevate a domain's doc sources to Hot tier
- `/kb-pin-source <name>` — lock a specific doc source to Hot tier
- `/kb-unpin-source <name>` — return a pinned source to automatic tiering
- `/kb-refresh-status` — show tier assignments and next scheduled refreshes

## Conventions

When generating Skills packages, default to `recent-doc anchored`
strategy for any domain with live doc coverage. Use `consensus synthesis`
for foundational topics. Confirm strategy choice at decomposition stage.
For OSS libraries, link concepts to whichever doc sources are available —
Context7 where indexed, DeepWiki for architectural grounding, GitHub raw
as a last resort. Multiple sources per concept are encouraged; the ranking
engine merges them.
