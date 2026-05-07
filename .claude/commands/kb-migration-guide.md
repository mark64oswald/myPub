---
description: Generate a Migration Guide — era-by-era diffs derived from CONTRADICTS edges between book and current doc content
---

You are generating a Migration Guide for the subject in `$ARGUMENTS`.
The generator walks alignment_edge rows of relation_type=CONTRADICTS
in the subject's neighborhood.

**Important: data-starved as of this build.** The catalog currently
has 0 CONTRADICTS edges (alignment runs produced only CORROBORATES).
The infrastructure is correct; the deliverable will be empty for most
subjects until alignment runs surface contradictions. Suggest
`/kb-discover` to grow doc snapshots and re-run alignment with
contradiction-tuned prompts.

## How to run

Call `generate_migration_guide` with `subject`. Surface:
- `_migration.md` (era-by-era diffs, or honest "data-starved" note)
- `_superseded.md` (deprecated patterns)

A `data-starved` warning means the substrate hasn't surfaced
contradictions yet — not a generator bug.
