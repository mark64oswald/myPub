---
description: Generate a Guided Conversation — a workflow skill that drives a chat from a goal to a finished output, one question at a time
argument-hint: "<goal> --output <what you want at the end> [--type T] [--topics A,B]"
---

You are generating a Guided Conversation for the goal in `$ARGUMENTS`. The
result is one Agent Skill that Claude follows step by step: it frames the
goal, checks prerequisites, settles decisions, works through procedures and
ends with the output the user asked for. The steps come from the knowledge
base (REQUIRES edges, CONTRASTS_WITH alternatives, linked procedures), so
they are specific to the topic rather than a generic interview.

Deterministic: one MCP call writes the package.

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out .claude/skills`): resolve it
   against the current project (`${CLAUDE_PROJECT_DIR}`) and pass the
   **absolute** path.
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

- **Required:** the goal, and the output wanted at the end. If the output is
  missing, ask once ("What should you have at the end: a doc, a plan, a
  decision record, code, a checklist?").
- **Optional:** `--type document|plan|decision|code|checklist` (detected from
  the output otherwise), `--topics A,B` (KB concepts; found in the goal text
  otherwise), `--audience`, `--max-steps N` (default 8), `--name <slug>`,
  `--auto` (let Claude start the skill on its own; the default is
  slash-command only).

### Step 2 — Generate

Call `generate_guided_conversation` on `mypub-kb` with `goal`, `output`,
`output_type`, `topics`, `audience`, `max_steps`, `skill_name`,
`invocation` (`auto` with `--auto`, else `user`) and `output_root`.

### Step 3 — Report

- The skill folder (`<output_root>/<package_name>/<skill-name>/`) and how to
  use it: copy it into a project's `.claude/skills/` (or `~/.claude/skills/`)
  and type `/<skill-name>`.
- The step outline from `metadata.steps` and the topics from
  `metadata.topics`.
- Warnings: no topics resolved (the conversation is generic; suggest
  `--topics` or `/kb-discover`), or topics with no prerequisites,
  alternatives or procedures.

### Optional — refine the wording with a subagent

The questions are assembled from KB rows. If the user wants them to read
naturally, dispatch one Agent subagent with `_prompts/refine-conversation.md`
from the package. It rewrites only the questions; the steps, state and
"done when" checks stay as generated. Run it on the user's subscription,
never the paid API.
