---
description: Generate a Slide-deck Outline — talk skeleton with bullets, presenter notes, and visual suggestions for a 15-60 minute talk
---

You are generating a Slide-deck Outline for the topic in `$ARGUMENTS`.
The outline is a complete talk skeleton: title / agenda / N insights ×
3-6 slides each / takeaways / Q&A. Each slide has ≤5 bullets (≤10
words each) plus presenter notes.

This is a **fully deterministic** generator — no sub-agents, no LLM
prose. Single MCP call.

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

- **Required:** topic (e.g. "Change Data Capture", "Apache Kafka")
- **Optional:** `--minutes N` (default 30) — talk duration
- **Optional:** `--audience engineers|executives|mixed` (default engineers)
- **Optional:** `--insights N` (default 3) — number of major sections
- **Optional:** `--thesis "..."` — one-line thesis to anchor the talk

### Step 2 — Generate

Call `generate_slide_deck` from `mypub-kb` with the parsed args.

### Step 3 — Surface the result

Response: `package_id`, `package_name`, `n_slides`, `n_insights`,
`duration_min`, `audience`, `file_paths`, `validation_issues`, `notes`.

- **`package_id == -1`** — generation failed. The most common cause
  is "topic concept not resolved". Suggest `/kb-discover <topic>` first.
- **`package_id > 0`** — surface:
  - the package folder (`<output_root>/<package_name>`)
  - slide count vs target duration
  - the five files: `_outline.md` (the deliverable), `_abstract.md`
    (CFP-ready), `visuals.md`, `speaker-notes.md`, `sources.md`
  - any warnings (slide-count mismatch, presenter-notes overflow)

## Guidance

- **Duration heuristic: ~1 min/content slide + 20% buffer.** A 30-min
  talk lands around 24 slides; 60 min around 50. Short talks (≤15
  min) hit the fixed-structure floor (~18 slides for 3 insights).
- **Audience changes presenter notes tone, not slide count.**
- **Re-running with the same topic replaces in place** (idempotent).
- **The outline is a skeleton, not a finished deck.** Bullets are
  short by spec (≤10 words); the prose lives in the speaker notes
  and the talk itself.