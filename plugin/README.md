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
| `CONTEXT7_API_KEY` | Passed to the server's own Context7 client for discovery. Set it in the environment Claude Code starts from. |

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
