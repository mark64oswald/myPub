---
description: Generate a Claude Code plugin with KB-built Agent Skills, workflow commands, hooks and an optional MCP server
argument-hint: "<purpose> [--name N] [--skills t1,t2] [--workflows w1,w2] [--mcp] [--hooks]"
---

You are scaffolding a Claude Code plugin for the purpose in `$ARGUMENTS`. The
package is a one-plugin local marketplace, so it installs right away:

```text
<package>/
  .claude-plugin/marketplace.json
  plugins/<name>/
    .claude-plugin/plugin.json
    skills/<topic>/SKILL.md + references/   one per --skills topic, built from the KB
    skills/<workflow>/SKILL.md              command-style skills (disable-model-invocation)
    hooks/hooks.json, scripts/after-edit.sh with --hooks
    .mcp.json + servers/<name>-server/      with --mcp
    README.md
  _provenance.md  _validation.md  _prompts/refine-skill.md
```

It uses skills rather than `commands/`: a skill with
`disable-model-invocation: true` is the slash-command form in current Claude Code.

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

- **Required:** purpose (becomes the manifest description)
- **Optional:**
  - `--name N` plugin name (kebab-case, not starting with `claude-`,
    `anthropic-` or `cc-plugin-`)
  - `--skills t1,t2` KB topics to turn into skills
  - `--workflows "Review design,Write ADR"` command-style skills to stub
  - `--mcp` include an MCP server; with it `--lang`, `--transport`, `--tools`,
    `--domain`, `--ui` as in `/kb-mcp-server`
  - `--hooks` include an example PostToolUse hook
  - `--author "Name"`

A plugin needs at least one of skills, workflows or an MCP server.

### Step 2 — Generate

Call `generate_plugin_scaffold` on `mypub-kb` with `purpose`, `plugin_name`,
`skill_topics`, `workflows`, `mcp_server`, `language`, `transport`, `tools`,
`domain`, `ui`, `hooks`, `author` and `output_root`.

### Step 3 — Validate and report

If the `claude` CLI is available, run
`claude plugin validate <package>/plugins/<name>` and `claude plugin validate <package>`
and include the result. Then tell the user:

- the package folder and what it contains (`metadata.skills`, `metadata.workflows`,
  `metadata.mcp_server`)
- how to try it: `/plugin marketplace add <package>` then
  `/plugin install <name>@<name>-local`
- topics that were skipped (in `notes`) and any warnings

### Optional — refine with a subagent

The skill bodies are assembled from KB rows, so they read like notes. If the
user wants polished instructions, dispatch one Agent subagent per skill with
`_prompts/refine-skill.md` from the package (fill in the skill name and
path). It edits `SKILL.md` in place and must keep `name:` unchanged. Run it on
the subscription through Claude Code subagents, never the paid API.
