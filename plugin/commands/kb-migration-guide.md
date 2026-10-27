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

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out docs/kb`, "save it under
   design/"): resolve it against the current project
   (`${CLAUDE_PROJECT_DIR}`) and pass the **absolute** path.
2. **No folder named, and the current project is not the myPub repo:**
   ask once, recommending the first option:
   - `${CLAUDE_PROJECT_DIR}/mypub-packages/` (keep it with this project)
   - myPub's own `data/generated-packages/` (the shared archive)
3. **Inside the myPub repo, or the user says "default":** omit
   `output_root`. The server then uses `$MYPUB_OUTPUT` if set, otherwise
   `<myPub>/data/generated-packages/`.

Always pass an absolute path. The server resolves relative paths against
the myPub repo, not against the project you are working in.

## How to run

Call `generate_migration_guide` with `subject`. Surface:
- `_migration.md` (era-by-era diffs, or honest "data-starved" note)
- `_superseded.md` (deprecated patterns)

A `data-starved` warning means the substrate hasn't surfaced
contradictions yet — not a generator bug.
