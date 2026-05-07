---
description: Run the Skills Factory end-to-end — decompose a domain, dispatch sub-agents, ingest results, and materialize SKILL.md files
---

You are driving the Skills Factory for the domain in `$ARGUMENTS` (or in
the message body if `$ARGUMENTS` is empty). The pipeline has four stages:
**prep → dispatch → process → materialize**. You are the orchestrator —
you call the MCP tools, you fan out the per-Skill sub-agents in parallel,
you do not do the writing yourself.

## Stage 1 — Prep

Call the `generate_skills_prep` MCP tool from `mypub-kb` with:

- `domain` — the user's input verbatim
- Leave `package_name`, `output_dir`, and decomposition tunables at
  defaults unless the user explicitly specified them

The response carries:

- `package_name`, `output_dir`, `n_skills`
- `planned_skills` — one entry per Skill with `cluster_id`, `name`,
  `anchor`, `strategy`, `strategy_rationale`, `requires_cluster_ids`,
  `references_cluster_ids`
- `prompt_paths` — absolute paths to per-Skill prompt files
- `result_paths` — absolute paths the sub-agents must write

Show the user a compact table of the planned Skills:

```
Package: <package_name>  (<n_skills> Skills)
Output:  <output_dir>

  [cluster_id]  name              strategy            anchor
  [3]           Circuit Breaker   recent_doc_anchored Circuit Breaker
  [7]           Bulkhead          recent_doc_anchored Bulkhead
  ...
```

Then say: "Dispatching <n> sub-agents in parallel." Do **not** ask for
confirmation unless `n_skills` is unusually large (>15) — in that case
confirm before dispatching.

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
to the file at <absolute result_path>. Do not write anywhere else.
Do not print the JSON to stdout instead of writing the file. After
writing, reply with one short sentence confirming the write.
```

Substitute `<absolute prompt_path>` and `<absolute result_path>` from
the corresponding indices of `prompt_paths` and `result_paths`. Pass a
short `description` like `"skill <cluster_id>: <name>"`.

Wait for all sub-agents to return before proceeding.

## Stage 3 — Process

Call `generate_skills_process` with `output_dir` from Stage 1.

The response carries `package_id`, `package_name`, `total`, `processed`,
`missing`, `unparseable`, `skill_ids`. If `missing` or `unparseable` is
non-empty, surface those clusters by id so the user can decide whether
to re-dispatch — do not silently swallow failures.

If everything processed cleanly, say so and move on.

## Stage 4 — Materialize

Call `generate_skills_materialize` with `package_id` from Stage 3.
Leave `output_root` and `overwrite` at defaults.

The response carries `output_root`, `skill_md_paths`,
`provenance_paths`, and `package_md_path`. Tell the user:

- where the package landed (`output_root`)
- how many SKILL.md files were written
- the path to `_package.md` so they can read the package overview

## Guidance

- **Parallel fan-out is the whole point.** The prep stage does the
  expensive retrieval/ranking once; the sub-agents only write the prose.
  Send all Task calls in a single message — sequential dispatch defeats
  the design.
- **Don't open the prompt files yourself.** The prompts are large and
  load up your context for no reason. The sub-agents read them.
- **Don't second-guess the strategy assignments.** Phase 5.2 picked
  `recent_doc_anchored` / `consensus_synthesis` / `authority_pick`
  based on the corpus signal. Surface the rationale to the user but
  don't override.
- **Re-dispatch on failure, don't edit JSON by hand.** If a sub-agent
  writes malformed JSON or skips the write, dispatch a fresh Task for
  just that cluster and re-call `generate_skills_process` — it's
  idempotent and clears prior skills for the package on each run.
- **Materialization is non-destructive by default in the sense that
  prior runs of the same package re-write the same folder.** If the
  user wants to preserve a prior version, ask them to rename the
  output folder before re-running.