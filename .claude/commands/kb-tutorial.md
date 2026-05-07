---
description: Generate a Tutorial — sequenced exercise track from procedures, with per-stage checkpoints
---

You are generating a Tutorial for the target in `$ARGUMENTS`. The
tutorial walks prerequisites, attaches one backing procedure per
stage as a hands-on exercise, and emits per-stage checkpoints
derived from procedure post-conditions.

Fully deterministic — no sub-agents.

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