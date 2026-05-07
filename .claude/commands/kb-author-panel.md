---
description: Generate an Author Panel — N≥2 characters debate per-topic positions; cross-character spread surfaces tension
---

You are generating an Author Panel for the panel name + topics in
`$ARGUMENTS`. Each topic gets scored across all characters; topics
with high spread are where the panel disagrees most.

## How to run

Call `generate_author_panel` with:
- `panel_name`: title
- `topics`: list of concept names
- `characters` (optional): list of dicts with name/bio/
  preferred_relations/preferred_concept_types/preferred_era. Defaults
  to [Architect, Practitioner]; pass 3+ for richer debate.

Surface:
- `_panel.md` — position grid
- `authors/<slug>.md` — per-character positions across all topics
