---
description: Generate a Concept Neighborhood Map — Mermaid + Graphviz visualization of a concept's k-hop graph neighborhood
---

You are generating a Concept Neighborhood Map for the seed concept in
`$ARGUMENTS` (or in the message body). The map shows the concept and
its k-hop neighborhood in the concept graph, colored by source-type
coverage and styled by relation type.

This is a **fully deterministic** generator — no sub-agents, no LLM
prose. Single MCP call produces the package.

## How to run

### Step 1 — Parse the request

The user's input is one or more of:

- **Required:** seed concept name (e.g. "Event Sourcing", "CQRS")
- **Optional:** `--depth N` (default 2) — BFS hop count
- **Optional:** `--max-nodes N` (default 60) — neighborhood cap
- **Optional:** `--edges A,B,C` — restrict to relation types
  (default: all of REQUIRES, EXTENDS, CONTRASTS_WITH, IMPLEMENTS, CITES;
  pass e.g. `--edges REQUIRES,EXTENDS` for a learning-prerequisites view)

### Step 2 — Generate

Call the `generate_concept_map` MCP tool from `mypub-kb` with:

- `concept` — the seed name verbatim
- `depth`, `max_nodes`, `relation_filter` per the user's flags
- Leave `output_root` at the default

### Step 3 — Surface the result

The response carries `package_id`, `package_name`, `n_nodes`,
`pruned_node_count`, `file_paths`, `validation_issues`, `notes`.

Branch on the response:

- **`package_id == -1`** — generation failed. Walk
  `validation_issues` for the reason. The most common cause is
  "seed concept not resolved" (the seed name doesn't match an existing
  concept). Suggest the user run `/kb-discover <seed>` first to either
  surface a near match or grow the corpus, then retry.
- **`package_id > 0`** — generation succeeded. Tell the user:
  - the package folder (`<output_root>/<package_name>`)
  - node count, edge filter, depth
  - if `pruned_node_count > 0`: surface it prominently — the
    visualization is truncated to the highest-degree nodes within the
    radius. Suggest `--max-nodes` to widen or `--depth 1` to narrow.
  - the four files: `_map.md` (overview + interpretation guide),
    `neighborhood.mmd` (Mermaid — renders inline in most Markdown
    viewers), `neighborhood.dot` (Graphviz DOT for higher-fidelity
    rendering), `nodes.csv` (debugging/analysis data)

If `validation_issues` has any `severity="warning"` entries, surface
them — they catch e.g. mermaid syntax oddities the validator's
heuristic flagged.

## Guidance

- **Default depth=2 is right for most queries.** Depth 1 is too
  shallow to be useful for orienting; depth 3+ usually triggers
  pruning and produces a cluttered map. If the user says "just the
  neighbors" lean depth=1; if they say "broader picture" lean
  depth=2 with a generous `max_nodes`.
- **Trust the pruning.** When `pruned_node_count` is large, the
  package shows the most-connected core. Don't try to visualize 500
  concepts — that's not a map, it's a hairball.
- **Edge filters are powerful for focus.** A learning-prerequisites
  view (`--edges REQUIRES,EXTENDS`) is much cleaner than the default
  for "what do I need to know first?". An anti-pattern view
  (`--edges CONTRASTS_WITH`) shows debate/tension surface.
- **The Mermaid file renders in most Markdown viewers** (GitHub,
  VS Code, Obsidian) — point the user to that first. The DOT file
  is a fallback for layouts the user wants to render through Graphviz.
- **Re-running with the same seed** updates the existing package
  in place (idempotent). The user doesn't need to delete first.