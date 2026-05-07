---
description: Run the Skills Factory end-to-end — decompose a domain, dispatch sub-agents, ingest results, materialize SKILL.md files, and run the routing eval
---

You are driving the Skills Factory for the domain in `$ARGUMENTS` (or in
the message body if `$ARGUMENTS` is empty). The pipeline has five stages:
**prep → dispatch → process → materialize → eval**. You are the
orchestrator — you call the MCP tools, you fan out the per-Skill
sub-agents in parallel, you do not do the writing yourself.

## Stage 1 — Prep

Call the `generate_skills_prep` MCP tool from `mypub-kb` with:

- `domain` — the user's input verbatim
- Leave `package_name`, `output_dir`, and decomposition tunables at
  defaults unless the user explicitly specified them

The response carries `package_name`, `output_dir`, `n_skills`,
`planned_skills` (per-Skill summary), `prompt_paths` and `result_paths`
(both absolute), plus `notes`.

### Branch on `n_skills`

- **`n_skills == 0`** — decomposition failed. The most common causes:
  * Topic is novel / not in the corpus → suggest `/kb-discover` first
    to grow the corpus, then re-run.
  * Query terms don't match any concept → suggest a more specific
    rephrasing.
  Surface the `notes` field verbatim (the planner explains *why* it
  bailed) and stop. Do not dispatch any sub-agents.

- **`n_skills > 15`** — confirm with the user before dispatching.
  Large packages thrash the user's parallel-agent budget; offer to
  re-run with a tighter `min_cluster_size` or a more focused domain.

- **`1 ≤ n_skills ≤ 15`** — show a compact table of the planned
  Skills, then proceed without confirmation:

  ```
  Package: <package_name>  (<n_skills> Skills)
  Output:  <output_dir>

    [cluster_id]  name              strategy            anchor
    [3]           Circuit Breaker   recent_doc_anchored Circuit Breaker
    [7]           Bulkhead          recent_doc_anchored Bulkhead
    ...
  ```

  Tell the user "Dispatching `n_skills` sub-agents in parallel."

If `notes` is non-empty, surface it before the table — it's where the
planner records partial-coverage warnings (e.g. "anchor concept has
no neighbors, falling back to single-cluster proposal").

## Stage 2 — Dispatch sub-agents (in parallel)

For every entry in `prompt_paths`, fan out **one Task agent per prompt**,
all in a single message with multiple tool calls. Use
`subagent_type="general-purpose"`. The prompt for each Task is:

```
Read the file at <absolute prompt_path>. It contains a complete brief
for generating one Claude Skill — system instructions, package context,
sibling Skills (so you can discriminate), the chosen strategy, and the
selected source excerpts.

Follow the brief exactly. Respond with JSON only — the schema is
{"trigger_description": "...", "skill_md": "..."} — and write the JSON
to <absolute result_path> using the Write tool. Do NOT print the JSON
to your output instead of writing the file. Do NOT wrap the JSON in
```json ``` fences (the orchestrator can recover from fences but
unwrapped JSON is preferred). Do NOT prepend a sentence before the
JSON in the file. After writing, reply with one sentence confirming
the absolute path you wrote.
```

Substitute `<absolute prompt_path>` and `<absolute result_path>` from
the corresponding indices of `prompt_paths` and `result_paths`. Pass a
short Task `description` like `"skill <cluster_id>: <name>"`.

Wait for all Task calls to return before moving on. Each sub-agent
should reply with one short confirmation sentence — if any sub-agent
errors out (Task tool returned an error rather than a result), note
which `cluster_id` failed and continue. Process can still ingest the
ones that succeeded; you'll re-dispatch failures in Stage 3.

## Stage 3 — Process

Call `generate_skills_process` with `output_dir` from Stage 1.

The response carries `package_id`, `package_name`, `total`, `processed`,
`missing`, `unparseable`, `skill_ids`.

### Branch on the counts

- **`missing == 0` and `unparseable == 0`** → clean ingest, proceed.
- **`missing > 0`** — sub-agents that didn't write the result file.
  Surface those `cluster_id`s. Re-dispatch a fresh Task for just those
  clusters (same prompt, same result_path), then re-run
  `generate_skills_process` (it's idempotent — clears prior skills
  for the package and re-ingests cleanly).
- **`unparseable > 0`** — sub-agent wrote something but it didn't
  validate (missing `trigger_description` / `skill_md`, or non-object).
  The processor already strips ```json fences and finds the first
  `{ … }` block before failing, so unparseable here means the JSON
  is genuinely malformed or missing required fields. Re-dispatch
  those cluster_ids.

After two failed attempts at the same cluster, stop and ask the user
how to proceed — don't loop indefinitely on a model that can't follow
the schema for that particular Skill.

## Stage 4 — Materialize

Call `generate_skills_materialize` with `package_id` from Stage 3.
Leave `output_root` and `overwrite` at defaults.

The response carries `output_root`, `skill_md_paths`,
`provenance_paths`, and `package_md_path`. Tell the user:

- where the package landed (`output_root`)
- how many SKILL.md files were written
- the path to `_package.md` so they can read the package overview

## Stage 5 — Eval

Call `eval_skills_routing` with `package_id` from Stage 3. Leave
`queries_per_skill` at the default (5).

The eval is a *proxy* — it embeds synthesized queries and ranks Skill
descriptions by cosine similarity. Real Claude Code uses an LLM
router, so high scores here are necessary but not sufficient. Treat
the metrics as a smoke test for description quality:

- **`overall.recall_at_1 ≥ 0.8`** → descriptions discriminate cleanly,
  report success and stop.
- **`0.5 ≤ overall.recall_at_1 < 0.8`** → some descriptions overlap.
  Walk `per_skill` and surface the 1–2 lowest-scoring Skills by name.
  Suggest the user inspect those SKILL.md files and re-dispatch with
  a tighter prompt if they want a clean run.
- **`overall.recall_at_1 < 0.5`** → bad. Either the descriptions are
  too generic or the package was over-decomposed (too many similar
  Skills). Surface the lowest 3 by name with their queries that
  mis-routed (`per_query[].rank > 1`). Recommend either re-running
  with a coarser `min_cluster_size` or rewriting the offending
  descriptions manually.

Always surface `notes` from the eval — they catch empty descriptions
and concept-budget shortfalls that would otherwise hide behind a
deceptively reasonable score.

## Guidance

- **Parallel fan-out is the whole point.** The prep stage does the
  expensive retrieval/ranking once; the sub-agents only write the
  prose. Send all Task calls in a single message — sequential
  dispatch defeats the design.
- **Don't open the prompt files yourself.** They're large and load
  context for no reason. The sub-agents read them.
- **Don't second-guess the strategy assignments.** Phase 5.2 picked
  `recent_doc_anchored` / `consensus_synthesis` / `authority_pick`
  based on the corpus signal. Surface the rationale; don't override.
- **Re-dispatch on failure, don't edit JSON by hand.** If a sub-agent
  writes malformed JSON or skips the write, dispatch a fresh Task for
  just that cluster and re-call `generate_skills_process`.
- **Materialization rewrites the same folder by default.** If the
  user wants to preserve a prior version, ask them to rename the
  output folder before re-running.
- **Eval is informational, not gating.** A weak eval doesn't block
  shipping the package — it's a signal that the descriptions could
  be tighter. The user decides whether to ship as-is.