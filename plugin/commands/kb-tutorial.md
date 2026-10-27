---
description: Generate a Tutorial — sequenced exercise track from procedures, with per-stage checkpoints
---

You are generating a Tutorial for the target in `$ARGUMENTS`. The
tutorial walks prerequisites, attaches one backing procedure per
stage as a hands-on exercise, and emits per-stage checkpoints
derived from procedure post-conditions.

Fully deterministic — no sub-agents.

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

### Step 1 — Parse the request

- **Required:** target concept (the thing the learner builds toward)
- **Optional:** `--level beginner|intermediate|advanced` (default intermediate)
- **Optional:** `--max-depth N` (default 4) — prereq chain depth
- **Optional:** `--max-stages N` (default 5) — final stage cap

### Step 2 — Generate

Call `generate_tutorial` from `mypub-kb`.

### Step 3 — Surface the result

Response: `n_stages`, `n_unbacked_stages` (stages without a backing
procedure — those become conceptual stages), `level`, `file_paths`,
`validation_issues`.

- **`package_id == -1`** → target not resolved; suggest `/kb-discover`
- **`package_id > 0`** → surface:
  - `tutorial.md` (the deliverable — stages with exercises)
  - `_setup.md` (prerequisites checklist)
  - `_checkpoints.md` (per-stage "you can do X if..." checks)

## Guidance

- **A stage without a backing procedure is conceptual-only.** When
  `n_unbacked_stages` > 0 it's because the corpus doesn't have
  procedures for those concepts. The renderer flags this honestly;
  don't try to fabricate exercises.
- **Stage exercises are deterministic.** The renderer parses the
  procedure's JSON-encoded steps and shows numbered actions + commands
  in code blocks. No LLM prose.
- **Checkpoint items derive from procedure post-conditions.** When a
  procedure has detailed postconditions, checkpoints are sharp; when
  not, checkpoints fall back to "describe in your own words".
- **Re-running with the same target replaces in place.**