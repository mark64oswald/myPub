---
description: Generate a Pattern + Anti-Pattern Catalog — discover patterns within a domain, group by family, surface anti-patterns from CONTRASTS_WITH edges
---

You are generating a Pattern Catalog for the domain in `$ARGUMENTS`.
The catalog discovers Pattern-typed concepts in the domain
neighborhood, groups them by IMPLEMENTS-target overlap (families),
and surfaces CONTRASTS_WITH neighbors as anti-patterns.

Foundational for Phase 15 Project Bootstrap. Single MCP call,
fully deterministic.

## How to run

### Step 1 — Parse the request

- **Required:** domain (e.g. "resilience", "data ingestion", "domain modeling")
- **Optional:** `--max-depth N` (default 2) — BFS depth from the seed
- **Optional:** `--max-patterns N` (default 30)
- **Optional:** `--max-anti-patterns N` (default 20)

### Step 2 — Generate

Call `generate_pattern_catalog` from `mypub-kb`.

### Step 3 — Surface the result

Response: `package_id`, `package_name`, `n_patterns`, `n_families`,
`n_anti_patterns`, `file_paths`, `validation_issues`, `notes`.

- **`package_id == -1`** — generation failed. Most common: domain
  concept not resolved → suggest `/kb-discover <domain>` first.
- **`package_id > 0`** — surface:
  - the package folder
  - pattern, family, anti-pattern counts
  - `_catalog.md` (overview), `_anti_patterns.md`, and per-pattern
    files in `patterns/`

## Guidance

- **Trust the family grouping.** Patterns sharing an IMPLEMENTS
  target form a coherent family; standalones land in a "Standalone"
  bucket at the end.
- **Anti-patterns are honest CONTRASTS_WITH neighbors.** They're not
  always pejorative — a "Naive X" pattern is a real anti-pattern; a
  CONTRASTS_WITH between two equal alternatives appears here too.
  The user gets to read both files and decide.
- **Coverage gaps are honest.** Domains with few patterns just
  produce small catalogs. The substrate has 11.6K Pattern-typed
  concepts and 37K IMPLEMENTS edges; well-covered domains
  ("data ingestion", "domain modeling", "resilience") yield 10-30
  patterns. Niche domains may yield 1-5.
- **Re-running with the same domain replaces in place.**
- **The catalog is a foundation.** It feeds Phase 15 Project
  Bootstrap (which composes Pattern → Procedure into runnable
  scaffolds) and Phase 12 ADR (which uses the same CONTRASTS_WITH
  signal for option framing).