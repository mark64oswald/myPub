---
description: Generate a Learning Path — sequenced curriculum from prerequisites to a target concept, with chapter recommendations per stage
---

You are generating a Learning Path for the target concept in
`$ARGUMENTS` (or in the message body). The path walks REQUIRES +
EXTENDS edges backward from the target, groups concepts into ordered
learning stages by depth, and recommends the strongest book chapters
for each stage.

This is a **fully deterministic** generator (v1) — no sub-agents, no
LLM prose. Single MCP call produces the package. A future v2 will add
sub-agent prose for "why this chapter" rationale and checkpoint
questions.

## How to run

### Step 1 — Parse the request

The user's input is one or more of:

- **Required:** target concept (e.g. "CDC pipeline design", "CQRS")
- **Optional:** `--start <concept>` — already-known concept; clips
  the path to material the user hasn't seen yet
- **Optional:** `--max-depth N` (default 4) — prerequisite chain
  length cap
- **Optional:** `--max-concepts N` (default 30) — total path size
  cap; densely-connected targets get hundreds of prereqs at depth 4
  and most are noise. Pruning by chapter-coverage keeps the
  learn-able subset.
- **Optional:** `--stage-size N` (default 5) — target concepts per
  stage; sane range 3-7

### Step 2 — Generate

Call the `generate_learning_path` MCP tool from `mypub-kb` with:

- `target` — the user's input verbatim
- `start`, `max_depth`, `max_concepts`, `target_stage_size` per
  the user's flags
- Leave `output_root` at the default

### Step 3 — Surface the result

The response carries `package_id`, `package_name`, `n_stages`,
`n_concepts`, `file_paths`, `validation_issues`, `notes`.

Branch on the response:

- **`package_id == -1`** — generation failed. Walk
  `validation_issues` for the reason. The most common cause is
  "target concept not resolved" — suggest `/kb-discover <target>`
  first.
- **`package_id > 0`** — generation succeeded. Tell the user:
  - the package folder (`<output_root>/<package_name>`)
  - stage count, concept count
  - if `notes` includes a "pruned N prerequisites" line, surface
    that prominently — the path was capped at `max_concepts` to
    keep it focused. Suggest `--max-concepts` to widen if they want
    a deeper survey.
  - point them at `_path.md` (overview + stage list) and
    `stage-N-<slug>/reading-list.md` per stage

## Guidance

- **Default depth=4, max_concepts=30 is right for most queries.** A
  4-hop path with ~30 concepts produces 5-7 stages of 4-6 concepts
  each — the sweet spot for "I'd actually follow this curriculum".
- **Use `--start` when the user has prior knowledge.** If they
  already know SQL, pass `--start SQL` so the path doesn't waste
  stages on basics. Without `--start`, the path includes everything
  back to the deepest reachable foundation.
- **Trust the chapter ranking.** Chapters are scored by
  concept-hit count: a chapter that covers 4 of 5 stage concepts
  ranks above one that covers 1. The reading list won't be
  exhaustive, but it'll be focused.
- **Coverage gaps are honest signals.** When the per-stage
  reading-list flags concepts with no book coverage, that's the
  corpus telling you to look at docs (`/kb-discover <concept>`)
  or acquire a book on the topic. Don't dismiss the gaps.
- **Re-running with the same target replaces in place.** The
  catalog row stays at the same `package_id`; the disk folder is
  rewritten. The user doesn't need to delete first.