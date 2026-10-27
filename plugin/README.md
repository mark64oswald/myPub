# myPub plugin

Makes the myPub knowledge base usable from any Claude Code project on
this Mac: the `mypub-kb` MCP server, the 24 `/kb-*` generator commands
(as `/mypub:kb-*`), and a `kb-usage` skill that teaches Claude which
search tool to call.

The plugin lives inside the myPub repo and loads **in place**: the MCP
server runs `../.venv/bin/python3 ../mcp-servers/kb-mcp/server.py`
relative to this folder, and reads `../data/catalog.ddb`. Nothing is
copied, so it only works on the machine that holds the catalog.

## Install (once)

```text
/plugin marketplace add ~/Developer/projects/myPub
/plugin install mypub@mypub-local
```

Then restart Claude Code (or run `/reload-plugins`). Edits to files in
`plugin/` take effect at the next session start or `/reload-plugins`;
no version bump needed.

## Configuration

| Env var | Effect |
|---|---|
| `MYPUB_OUTPUT` | Default folder for generator packages when a command doesn't pass one. Falls back to `<myPub>/data/generated-packages/`. |
| `MYPUB_CATALOG` | Use a different catalog file (e.g. an APFS clone). |
| `MYPUB_KB_HOLD_CATALOG` | Set to `1` to keep the catalog open between tool calls. Faster repeat queries, but it blocks writers while any session is open. |
| `CONTEXT7_API_KEY` | Passed to the server's own Context7 client for discovery. Set it in the environment Claude Code starts from. |

## Catalog locking

Every session that has the plugin runs its own `mypub-kb` process, and a
DuckDB read-only handle still blocks the catalog's single writer. So the
server opens the catalog at the start of each tool call and closes it
when the call returns. Idle sessions hold no lock, and refresh,
extraction and alignment jobs can run while sessions are open. If a
writer holds the catalog when a tool is called, the call fails with a
"catalog is locked by a writer" error; retry after the job finishes.

## Live docs

`mypub-kb` talks to Context7 and DeepWiki itself during
auto-discovery, so live-doc search works the same in any project. The
plugin doesn't bundle a separate Context7 server; if you want Claude to
call Context7 directly too, keep your own user-level Context7 server.

## Inside the myPub repo

The repo's root `.mcp.json` still defines `mypub-kb` for working on
myPub itself without the plugin. With the plugin installed you'll see
both servers there. Both open the catalog read-only, so that's safe; you
can disable either one in `/mcp` if you only want one.
