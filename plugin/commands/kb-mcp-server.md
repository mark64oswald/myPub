---
description: Generate an MCP server skeleton (Python or TypeScript, optional MCP Apps UI and skills) grounded in the current MCP spec
argument-hint: "<purpose> [--lang python|typescript] [--ui] [--domain C] [--tools a,b]"
---

You are scaffolding an MCP server for the purpose in `$ARGUMENTS`. The output
targets MCP revision 2026-07-28 and the v2 SDKs (Python `MCPServer`,
TypeScript `McpServer` + zod v4). Every tool is a stub with a TODO; when a
KB concept is given, each of its procedures becomes one stub with the
procedure's steps as the TODO.

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

- **Required:** purpose (one sentence; it becomes the server description)
- **Optional:**
  - `--name N` server name (kebab-case; default: slug of the purpose)
  - `--lang python|typescript` (default python)
  - `--transport stdio|http` (http = Streamable HTTP, python only)
  - `--tools a,b,c` tool names to stub, or `--domain <concept>` to stub one tool
    per KB procedure of that concept
  - `--ui` make the first tool an MCP App (interactive `ui://` view)
  - `--skills t1,t2` KB topics to serve as `skill://` resources (python only;
    adds the MCP Skills extension when the installed SDK has it)
  - `--namespace io.github.<user>` for `server.json`

If the user asks for TypeScript with HTTP, or TypeScript with skills, say the
TypeScript scaffold is stdio-only without the Skills extension, and offer Python.

### Step 2 — Generate

Call `generate_mcp_server` on `mypub-kb` with `purpose`, `server_name`,
`language`, `transport`, `tools`, `domain`, `ui`, `skill_topics`,
`registry_namespace` and `output_root`.

### Step 3 — Report

- **`package_id == -1`**: list `validation_issues` (bad option combination).
- Otherwise give the folder, the tool names, and the next steps from
  `_validation.md`: run the smoke test (`uv run pytest` or `npm run check`),
  open it in the MCP Inspector, replace the TODOs.

## Guidance

- `_provenance.md` lists the spec and SDK doc sections the scaffold follows,
  pulled from Live Docs. Check them if an SDK call has moved.
- HTTP servers need authorization before they leave localhost; the scaffold
  doesn't add it.
- To ship the server inside a Claude Code plugin, use `/kb-plugin-scaffold --mcp`.
