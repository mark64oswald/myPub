---
description: Generate an Author Panel — N≥2 characters debate per-topic positions; cross-character spread surfaces tension
---

You are generating an Author Panel for the panel name + topics in
`$ARGUMENTS`. Each topic gets scored across all characters; topics
with high spread are where the panel disagrees most.

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

Call `generate_author_panel` with:
- `panel_name`: title
- `topics`: list of concept names
- `characters` (optional): list of dicts with name/bio/
  preferred_relations/preferred_concept_types/preferred_era. Defaults
  to [Architect, Practitioner]; pass 3+ for richer debate.

Surface:
- `_panel.md` — position grid
- `authors/<slug>.md` — per-character positions across all topics
