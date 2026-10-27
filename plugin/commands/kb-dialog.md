---
description: Generate a Dialog — scripted exchange between Architect and Practitioner where their rankings diverge
---

You are generating a Dialog for the topic in `$ARGUMENTS`. The
generator finds concepts in the topic's neighborhood where two
characters score divergently; each divergence becomes a dialogue beat.

Default characters: Architect (Pattern-oriented, classical-era) and
Practitioner (Tool-oriented, current-doc).

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

Call `generate_dialog` with `topic`. Surface:
- `dialogue.md` — script form
- `_stage_directions.md` — per-beat score breakdown showing why each
  character takes their position

Beats with low spread are filtered out — characters who agree don't
make for interesting dialogue.
