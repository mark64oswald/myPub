---
description: Generate one Agent Skill (agentskills.io spec) from a knowledge-base concept
argument-hint: "<topic> [--title T] [--purpose P]"
---

You are generating an Agent Skill for the topic in `$ARGUMENTS`. The skill
follows the Agent Skills specification: a `SKILL.md` with a spec-valid
`name` (lowercase slug that matches its folder) and a description that says
what it does and when to use it, plus `references/` files for progressive
disclosure. Content comes from the KB: the concept's description, its
procedures (and those of EXTENDS children), related concepts and the books
and live docs behind them.

Deterministic: one MCP call writes the package.

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out tools/`, "save it under
   plugins/"): resolve it against the current project
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

- **Required:** topic (a concept in the KB, e.g. "Event Sourcing", "Kafka Streams")
- **Optional:** `--title T` (human title; its slug becomes the skill name),
  `--purpose P` (extra "use when …" phrase), `--max-procedures N` (default 8)

### Step 2 — Generate

Call `generate_agent_skill` on `mypub-kb` with `topic`, `title`, `purpose`,
`max_procedures` and `output_root`.

### Step 3 — Report

- **`package_id == -1`**: the topic didn't resolve. Suggest `/kb-discover <topic>`
  or a broader concept name.
- Otherwise give the skill folder (`<output_root>/<package_name>/<skill-name>/`),
  the procedure count from `metadata.n_procedures`, and any warnings
  (a thin skill when there are no procedures). Point at `_validation.md`.

### Optional — refine with a subagent

The skill bodies are assembled from KB rows, so they read like notes. If the
user wants polished instructions, dispatch one Agent subagent per skill with
`_prompts/refine-skill.md` from the package (fill in the skill name and
path). It edits `SKILL.md` in place and must keep `name:` unchanged. Run it on
the subscription through Claude Code subagents, never the paid API.

## Guidance

- To use it in Claude Code, copy the skill folder into `.claude/skills/` (project)
  or `~/.claude/skills/` (personal), or bundle several with `/kb-plugin-scaffold`.
- For a set of skills from a whole domain, use `/kb-generate-skills` instead.
