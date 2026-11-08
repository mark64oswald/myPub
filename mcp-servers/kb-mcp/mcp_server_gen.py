"""
mcp_server_gen.py — ``/kb-mcp-server``: an MCP server skeleton grounded in the current spec (2026-11).

Targets MCP base protocol revision 2026-07-28 (stateless; roots, sampling and
logging deprecated) and the v2 SDKs:

* **python** — ``mcp`` v2 (``MCPServer``, formerly FastMCP). Supports
  stdio or Streamable HTTP, MCP Apps (``mcp.server.apps.Apps``) and the
  Skills extension (``mcp.server.skills.Skills``, SEP-2640).
* **typescript** — ``@modelcontextprotocol/server`` v2 with zod v4, stdio.
  MCP Apps via ``@modelcontextprotocol/ext-apps`` (vite single-file view).
  The Skills extension isn't offered here yet: put skills in the plugin's
  ``skills/`` folder instead.

Tools come from, in order: the names the caller passes; the procedures of a
``domain`` concept in the KB (one stub per procedure, with the procedure's
steps as the TODO); or a single example tool. Every tool is a stub that says
what to implement. The package also carries ``server.json`` for the MCP
Registry, an ``.mcp.json`` snippet for Claude Code, a smoke test, and
``_provenance.md`` citing the spec sections it follows.

:func:`build_server_files` is reused by the plugin generator.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import duckdb

import agent_skill_gen
import agent_skills
import kb_scaffold as ks
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue

GENERATOR_TYPE = "mcp_server"
LANGUAGES = ("python", "typescript")
TRANSPORTS = ("stdio", "http")
MCP_REVISION = "2026-07-28"


@dataclass
class ToolStub:
    name: str
    description: str
    todo: str = ""
    procedure_id: Optional[int] = None


@dataclass
class ServerSpec:
    name: str                     # kebab-case server name
    purpose: str
    language: str = "python"
    transport: str = "stdio"
    ui: bool = False
    tools: list[ToolStub] = field(default_factory=list)
    skills: list[ks.TopicMaterial] = field(default_factory=list)
    registry_namespace: str = "io.github.your-github-user"

    @property
    def module(self) -> str:
        return ks.identifier(self.name, style="snake")


def tools_from_request(
    conn: duckdb.DuckDBPyConnection, resolver: Any, *,
    tool_names: Sequence[str] = (), domain: Optional[str] = None, max_tools: int = 4,
) -> tuple[list[ToolStub], list[str]]:
    notes: list[str] = []
    if tool_names:
        return [ToolStub(ks.identifier(t), f"TODO: describe what {t} does and when to call it.")
                for t in tool_names[:12]], notes
    if domain:
        tm = ks.topic_material(conn, resolver, domain, max_procedures=max_tools)
        if tm and tm.procedures:
            out = []
            for p in tm.procedures:
                desc = p.name.rstrip(".")
                if (p.preconditions or "").strip():
                    desc += f". Requires: {ks.first_sentence(p.preconditions, 160)}"
                out.append(ToolStub(ks.identifier(p.name), desc,
                                    todo=" ".join(p.steps.split())[:400],
                                    procedure_id=p.procedure_id))
            # De-duplicate names.
            seen: set[str] = set()
            for t in out:
                base, i = t.name, 2
                while t.name in seen:
                    t.name, i = f"{base}_{i}", i + 1
                seen.add(t.name)
            return out, notes
        notes.append(f"domain {domain!r} has no procedures in the KB; using an example tool")
    return [ToolStub("echo", "Echo the input back. Replace with a real tool.",
                     todo="Replace this example with the server's first real tool.")], notes


# ---------------------------------------------------------------------------
# Python templates
# ---------------------------------------------------------------------------


_PY_SKILLS_BLOCK = '''\
# Skills: every file under skills/<name>/ is served as a skill://<name>/<path> resource.
# When the SDK ships the Skills extension (io.modelcontextprotocol/skills, SEP-2640),
# skills/list and skills/get also describe them, with sha256 digests hosts can verify.
try:
    from mcp.server.skills import Skills
    from mcp.shared.exceptions import MCPError
    from mcp.shared.skills import GetSkillResult, ListSkillsResult, Skill, SkillResource
    from mcp.types import INVALID_PARAMS
except ImportError:  # mcp <= 2.3 has no Skills extension yet: resources only
    Skills = None

SKILLS_DIR = HERE / "skills"


def _frontmatter(text: str) -> dict:
    fm: dict = {}
    if text.startswith("---\\n"):
        for line in text.split("\\n---\\n", 1)[0].splitlines()[1:]:
            key, _, value = line.partition(":")
            if key in ("name", "description") and value.strip():
                v = value.strip()
                fm[key] = json.loads(v) if v.startswith('"') else v
    return fm


def _skill_files() -> list[tuple[str, Path]]:
    """(skill://<name>/<path>, file) for every file of every skill."""
    out = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        root = skill_md.parent
        for f in sorted(p for p in root.rglob("*") if p.is_file()):
            out.append((f"skill://{root.name}/{f.relative_to(root).as_posix()}", f))
    return out


SKILL_FILES = _skill_files()

if Skills is not None:
    def _skills() -> dict:
        skills: dict = {}
        for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            prefix = f"skill://{skill_md.parent.name}/"
            resources = []
            for uri, f in SKILL_FILES:
                if uri.startswith(prefix):
                    data = f.read_bytes()
                    resources.append(SkillResource(
                        uri=uri, digest="sha256:" + hashlib.sha256(data).hexdigest(), size=len(data)))
            skills[prefix + "SKILL.md"] = Skill(
                uri=prefix + "SKILL.md", frontmatter=_frontmatter(skill_md.read_text()),
                resources=resources)
        return skills

    SKILLS = _skills()

    async def list_skills(ctx, params) -> ListSkillsResult:
        return ListSkillsResult(skills=list(SKILLS.values()))

    async def get_skill(ctx, params) -> GetSkillResult:
        if params.uri not in SKILLS:
            raise MCPError(code=INVALID_PARAMS, message=f"unknown skill: {params.uri}")
        return GetSkillResult(skill=SKILLS[params.uri])

    EXTENSIONS.append(Skills(list_skills=list_skills, get_skill=get_skill))
'''


def _py_server(s: ServerSpec) -> str:
    """server.py for the MCP Python SDK v2 (checked against mcp 2.3)."""
    out = [
        f'"""{s.name}: {s.purpose.replace(chr(34) * 3, chr(39) * 3)}',
        "",
        f"Generated by myPub /kb-mcp-server for MCP {MCP_REVISION} and the MCP Python SDK v2.",
        "Every tool below is a stub: implement it, then tighten its description and annotations.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    if s.skills:
        out += ["import hashlib", "import json"]
    out += ["from pathlib import Path", ""]
    if s.ui:
        out.append("from mcp.server.apps import Apps, client_supports_apps")
    out.append("from mcp.server.mcpserver import Context, MCPServer" if s.ui
               else "from mcp.server.mcpserver import MCPServer")
    if s.skills:
        out.append("from mcp.server.mcpserver.resources import TextResource")
    out += ["from mcp.types import ToolAnnotations", "", "HERE = Path(__file__).resolve().parent",
            "EXTENSIONS: list = []", "", ""]
    tools = list(s.tools)
    if s.ui:
        t = tools.pop(0)
        uri = f"ui://{s.name}/{t.name}.html"
        doc = t.description.replace('"""', "'''")
        out += [
            "# MCP Apps (io.modelcontextprotocol/ui, SEP-1865): this tool carries",
            "# _meta.ui.resourceUri, so Apps-capable hosts render ui/" + t.name + ".html beside the result.",
            "# Register app tools before MCPServer() is built: the server reads them from the extension.",
            "apps = Apps()",
            "",
            "",
            f'@apps.tool(resource_uri="{uri}", description="""{doc}""")',
            f"def {t.name}(query: str, ctx: Context) -> str:",
            f"    # TODO: {t.todo or 'implement.'}",
            f'    result = f"{t.name}: {{query}}"',
            "    if not client_supports_apps(ctx):",
            "        return result  # hosts without Apps get the text result only",
            "    return result",
            "",
            "",
            f'apps.add_html_resource("{uri}", (HERE / "ui" / "{t.name}.html").read_text(), title="{t.name}")',
            "EXTENSIONS.append(apps)",
            "",
            "",
        ]
    if s.skills:
        out += [_PY_SKILLS_BLOCK, ""]
    instructions = " ".join(s.purpose.split()).replace("\\", "").replace('"', "'")
    out += [f'mcp = MCPServer("{s.name}", instructions="{instructions}", extensions=EXTENSIONS)', ""]
    if s.skills:
        out += ["for _uri, _file in SKILL_FILES:",
                "    mcp.add_resource(TextResource(uri=_uri, name=_uri, mime_type=\"text/markdown\",",
                "                                  text=_file.read_text()))", ""]
    out.append("")
    for t in tools:
        doc = t.description.replace('"""', "'''")
        out += [
            "@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))",
            f"def {t.name}(query: str) -> str:",
            f'    """{doc}"""',
            f"    # TODO: {t.todo or 'implement.'}",
            f'    return f"{t.name}: {{query}}"',
            "",
            "",
        ]
    out.append('if __name__ == "__main__":')
    if s.transport == "http":
        out += ["    # Streamable HTTP at http://127.0.0.1:8000/mcp. Add authorization before exposing it",
                "    # beyond localhost (MCP spec: basic/authorization).",
                '    mcp.run(transport="streamable-http", port=8000)']
    else:
        out.append('    mcp.run(transport="stdio")')
    text = "\n".join(out) + "\n"
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text


def _py_view(s: ServerSpec, tool: str) -> str:
    # Plain postMessage handshake, as in the Python SDK's MCP Apps guide: no build step.
    return textwrap.dedent(f"""\
        <!doctype html>
        <meta charset="utf-8">
        <title>{tool}</title>
        <main>
          <h1>{tool}</h1>
          <pre id="result">Waiting for the tool result…</pre>
        </main>
        <script>
          // MCP Apps view (SEP-1865). Replace the rendering below with your UI.
          const parent = window.parent;
          let initialized = false;
          window.addEventListener("message", (event) => {{
            if (event.source !== parent || event.data?.jsonrpc !== "2.0") return;
            if (event.data.id === 1 && event.data.result?.protocolVersion) {{
              parent.postMessage({{jsonrpc: "2.0", method: "ui/notifications/initialized"}}, "*");
              initialized = true;
              return;
            }}
            if (!initialized || event.data.method !== "ui/notifications/tool-result") return;
            const text = event.data.params?.content?.[0]?.text;
            if (typeof text === "string") document.getElementById("result").textContent = text;
          }});
          parent.postMessage({{
            jsonrpc: "2.0", id: 1, method: "ui/initialize",
            params: {{appInfo: {{name: "{s.name}", version: "0.1.0"}}, appCapabilities: {{}},
                      protocolVersion: "2026-01-26"}}
          }}, "*");
        </script>
        """)


def _py_pyproject(s: ServerSpec) -> str:
    return textwrap.dedent(f"""\
        [project]
        name = "{s.name}"
        version = "0.1.0"
        description = "{s.purpose.replace('"', "'")}"
        requires-python = ">=3.10"
        dependencies = ["mcp>=2.3"]

        [dependency-groups]
        dev = ["pytest>=8", "pytest-asyncio>=0.23"]
        """)


def _py_test(s: ServerSpec) -> str:
    names = ", ".join(f'"{t.name}"' for t in s.tools)
    return textwrap.dedent(f"""\
        import asyncio
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        import server  # noqa: E402


        def test_tools_are_registered():
            tools = asyncio.run(server.mcp.list_tools())
            assert {{t.name for t in tools}} >= {{{names}}}
        """)


# ---------------------------------------------------------------------------
# TypeScript templates
# ---------------------------------------------------------------------------


def _ts_server(s: ServerSpec) -> str:
    lines = [
        f"// {s.name}: {s.purpose}",
        f"// Generated by myPub /kb-mcp-server for MCP {MCP_REVISION} and the MCP TypeScript SDK v2.",
        "// Every tool below is a stub: implement it, then tighten its description.",
        'import { McpServer } from "@modelcontextprotocol/server";',
        'import { StdioServerTransport } from "@modelcontextprotocol/server/stdio";',
        'import * as z from "zod/v4";',
    ]
    if s.ui:
        lines += [
            "import {",
            "  registerAppResource,",
            "  registerAppTool,",
            "  RESOURCE_MIME_TYPE,",
            '} from "@modelcontextprotocol/ext-apps/server";',
            'import fs from "node:fs/promises";',
            'import path from "node:path";',
        ]
    lines += ["", f'const server = new McpServer({{ name: "{s.name}", version: "0.1.0" }});', ""]
    for i, t in enumerate(s.tools):
        desc = json.dumps(t.description)
        todo = f"  // TODO: {t.todo}" if t.todo else "  // TODO: implement."
        if s.ui and i == 0:
            uri = f"ui://{s.name}/mcp-app.html"
            lines += [
                f'const resourceUri = "{uri}";',
                "",
                "registerAppTool(",
                "  server,",
                f'  "{t.name}",',
                "  {",
                f"    description: {desc},",
                "    inputSchema: z.object({ query: z.string() }),",
                "    _meta: { ui: { resourceUri } },",
                "  },",
                "  async ({ query }) => {",
                todo.replace("  //", "    //"),
                f'    return {{ content: [{{ type: "text", text: `{t.name}: ${{query}}` }}] }};',
                "  },",
                ");",
                "",
                "registerAppResource(server, resourceUri, resourceUri, { mimeType: RESOURCE_MIME_TYPE }, async () => {",
                '  const html = await fs.readFile(path.join(import.meta.dirname, "..", "dist", "mcp-app.html"), "utf-8");',
                "  return { contents: [{ uri: resourceUri, mimeType: RESOURCE_MIME_TYPE, text: html }] };",
                "});",
                "",
            ]
            continue
        lines += [
            "server.registerTool(",
            f'  "{t.name}",',
            f"  {{ description: {desc}, inputSchema: z.object({{ query: z.string() }}) }},",
            "  async ({ query }) => {",
            todo.replace("  //", "    //"),
            f'    return {{ content: [{{ type: "text", text: `{t.name}: ${{query}}` }}] }};',
            "  },",
            ");",
            "",
        ]
    lines += ["await server.connect(new StdioServerTransport());", ""]
    return "\n".join(lines)


def _ts_package(s: ServerSpec) -> str:
    deps = {"@modelcontextprotocol/server": "^2.4.0", "zod": "^4.0.0"}
    dev = {"@types/node": "^22.0.0", "tsx": "^4.0.0", "typescript": "^5.6.0"}
    scripts = {"start": "tsx src/server.ts", "build": "tsc", "check": "tsc --noEmit"}
    if s.ui:
        deps["@modelcontextprotocol/ext-apps"] = "^2.0.0"
        dev.update({"vite": "^6.0.0", "vite-plugin-singlefile": "^2.0.0"})
        scripts["build:ui"] = "vite build"
        scripts["start"] = "vite build && tsx src/server.ts"
    return json.dumps({"name": s.name, "version": "0.1.0", "private": True, "type": "module",
                       "description": s.purpose, "bin": {s.name: "dist/server.js"},
                       "scripts": scripts, "dependencies": deps, "devDependencies": dev},
                      indent=2) + "\n"


_TSCONFIG = json.dumps({"compilerOptions": {
    "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
    "outDir": "dist", "rootDir": "src", "strict": True, "skipLibCheck": True},
    "include": ["src"]}, indent=2) + "\n"

_TS_VIEW_HTML = """\
<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><title>App</title></head>
  <body>
    <pre id="result">Waiting for the tool result…</pre>
    <script type="module" src="/src/mcp-app.ts"></script>
  </body>
</html>
"""

_TS_VIEW = """\
import { App } from "@modelcontextprotocol/ext-apps";

const resultEl = document.getElementById("result")!;
const app = new App({ name: "%s", version: "0.1.0" });

// Register handlers before connect() so the first tool result isn't missed.
app.ontoolresult = (result) => {
  const text = result.content?.find((c) => c.type === "text")?.text;
  resultEl.textContent = text ?? "(no text result)";
};

app.connect();
"""

_VITE_CONFIG = """\
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Bundle the view into one HTML file, served as the ui:// resource.
export default defineConfig({
  plugins: [viteSingleFile()],
  build: { outDir: "dist", rollupOptions: { input: "mcp-app.html" } },
});
"""


# ---------------------------------------------------------------------------
# Shared outputs
# ---------------------------------------------------------------------------


def run_command(s: ServerSpec, root: str = ".") -> tuple[str, list[str]]:
    if s.language == "python":
        return "uv", ["run", "--directory", root, "server.py"]
    return "npx", ["tsx", f"{root}/src/server.ts"]


def mcp_json_entry(s: ServerSpec, root: str = ".") -> dict:
    if s.transport == "http":
        return {"type": "http", "url": "http://127.0.0.1:8000/mcp"}
    cmd, args = run_command(s, root)
    return {"command": cmd, "args": args}


def server_json(s: ServerSpec) -> str:
    pkg: dict[str, Any] = (
        {"registryType": "pypi", "identifier": s.name, "version": "0.1.0"}
        if s.language == "python" else
        {"registryType": "npm", "identifier": s.name, "version": "0.1.0"}
    )
    pkg["transport"] = {"type": "stdio"} if s.transport == "stdio" else {
        "type": "streamable-http", "url": "https://REPLACE-ME.example.com/mcp"}
    return json.dumps({
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": f"{s.registry_namespace}/{s.name}",
        "description": s.purpose[:100],
        "version": "0.1.0",
        "packages": [pkg],
    }, indent=2) + "\n"


def readme(s: ServerSpec) -> str:
    run = "uv run server.py" if s.language == "python" else "npm install && npm start"
    inspect = ("npx @modelcontextprotocol/inspector uv run server.py" if s.language == "python"
               else "npx @modelcontextprotocol/inspector npx tsx src/server.ts")
    tools = "\n".join(f"| `{t.name}` | {t.description} |" for t in s.tools)
    extras = []
    if s.ui:
        extras.append("- **MCP Apps:** the first tool renders an interactive view (`ui://` resource). "
                      "Hosts without Apps support get the text result.")
    if s.skills:
        extras.append("- **Skills extension:** files under `skills/` are served as `skill://` resources "
                      "with `skills/list` and `skills/get`. SDK and host support is still rolling out "
                      "(see the MCP extension client matrix).")
    return "\n".join([
        f"# {s.name}", "", s.purpose, "",
        f"An MCP server for protocol revision {MCP_REVISION}, generated by myPub `/kb-mcp-server` "
        f"({s.language}, {s.transport}). Every tool is a stub to implement.", "",
        "## Run", "", f"```bash\n{run}\n```", "",
        "## Try it in the MCP Inspector", "", f"```bash\n{inspect}\n```", "",
        "## Tools", "", "| Tool | Description |", "| --- | --- |", tools, "",
        *(["## Extensions", "", *extras, ""] if extras else []),
        "## Publish", "",
        "`server.json` is a starting point for the MCP Registry. Set `name` to your namespace "
        "(`io.github.<user>/<server>`), publish the package, then run `mcp-publisher publish`.", "",
        "## Add to Claude Code", "",
        f"```bash\nclaude mcp add {s.name} -- {' '.join([run_command(s)[0], *run_command(s)[1]])}\n```", "",
    ])


def build_server_files(s: ServerSpec, *, prefix: str = "") -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    if s.language == "python":
        files += [("pyproject.toml", _py_pyproject(s)), ("server.py", _py_server(s)),
                  ("tests/test_server.py", _py_test(s))]
        if s.ui:
            files.append((f"ui/{s.tools[0].name}.html", _py_view(s, s.tools[0].name)))
        for tm in s.skills:
            _n, sk_files, _p = agent_skill_gen.build_skill_files(tm, prefix="skills/")
            files += sk_files
    else:
        files += [("package.json", _ts_package(s)), ("tsconfig.json", _TSCONFIG),
                  ("src/server.ts", _ts_server(s))]
        if s.ui:
            files += [("mcp-app.html", _TS_VIEW_HTML), ("src/mcp-app.ts", _TS_VIEW % s.name),
                      ("vite.config.ts", _VITE_CONFIG)]
    files += [("README.md", readme(s)), ("server.json", server_json(s)),
              (".gitignore", "node_modules/\ndist/\n.venv/\n__pycache__/\n")]
    return [(f"{prefix}{p}", c) for p, c in files]


def server_problems(s: ServerSpec) -> list[str]:
    out = []
    if s.language not in LANGUAGES:
        out.append(f"language must be one of {LANGUAGES}")
    if s.transport not in TRANSPORTS:
        out.append(f"transport must be one of {TRANSPORTS}")
    if s.language == "typescript" and s.transport == "http":
        out.append("typescript scaffold supports stdio only; use python for Streamable HTTP")
    if s.language == "typescript" and s.skills:
        out.append("the Skills extension scaffold is python-only; put skills in a plugin's skills/")
    if not s.tools:
        out.append("no tools")
    return out


def citations_for(conn: duckdb.DuckDBPyConnection, s: ServerSpec) -> list[ks.Citation]:
    names = list(ks.MCP_SPEC_SOURCES) + list(ks.MCP_SDK_SOURCES.get(s.language, ()))
    names += list(ks.MCP_REGISTRY_SOURCES)
    if s.ui:
        names += list(ks.MCP_APPS_SOURCES)
    if s.skills:
        names += list(ks.MCP_SKILLS_EXT_SOURCES)
    return ks.doc_citations(conn, names, keywords=("tool", "server", "transport"), per_source=1)


def validation_checklist(s: ServerSpec, problems: Sequence[str]) -> str:
    run = "uv run pytest" if s.language == "python" else "npm install && npm run check"
    lines = [f"# Checks for {s.name}", ""]
    lines += [f"- {p}" for p in problems] or ["Structure checks passed."]
    lines += ["", "Before you rely on it:", "",
              f"1. `{run}` — the smoke test lists the tools.",
              "2. Open it in the MCP Inspector (see README) and call each tool.",
              "3. Replace every `TODO` and write tool descriptions a model can choose from.",
              "4. Set tool annotations honestly (read-only, destructive, open-world).",
              "5. For HTTP: add authorization (MCP spec, basic/authorization) before exposing it.",
              "", "The SDK calls here follow the v2 SDK docs in the knowledge base (2026-11). "
              "If an import fails, check the SDK's migration guide; v2 renamed FastMCP to MCPServer."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Generator components
# ---------------------------------------------------------------------------


class ServerDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str, **kw: Any):
        tools, notes = tools_from_request(conn, resolver, tool_names=kw.get("tools") or (),
                                          domain=kw.get("domain"))
        skills, missing = [], []
        for topic in kw.get("skill_topics") or ():
            tm = ks.topic_material(conn, resolver, topic)
            (skills.append(tm) if tm else missing.append(topic))
        if missing:
            notes.append(f"skill topics not in the KB, skipped: {', '.join(missing)}")
        spec = ServerSpec(
            name=agent_skills.skill_name(kw.get("server_name") or query)[:40].rstrip("-"),
            purpose=" ".join(query.split()), language=kw.get("language", "python"),
            transport=kw.get("transport", "stdio"), ui=bool(kw.get("ui")), tools=tools,
            skills=skills, registry_namespace=kw.get("registry_namespace") or ServerSpec.registry_namespace,
        )
        return {"spec": spec, "notes": notes, "citations": citations_for(conn, spec)}


class ServerPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, d: dict, *,
             package_name: Optional[str] = None, **_: Any) -> GenPlan:
        s: ServerSpec = d["spec"]
        problems = server_problems(s)
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or s.name,
                       domain=s.purpose, source_query=s.purpose, notes=list(d["notes"]),
                       package_metadata={"language": s.language, "transport": s.transport,
                                         "ui": s.ui, "n_tools": len(s.tools),
                                         "skills": [t.name for t in s.skills],
                                         "problems": problems})
        for i, t in enumerate(s.tools):
            plan.units.append(GenUnit(
                unit_type="mcp_tool", name=t.name, ordinal=i, logical_key=f"tool:{t.name}",
                content_markdown=t.description, metadata={"todo": t.todo},
                sources=[("procedure", t.procedure_id, 1.0, 1.0, None)] if t.procedure_id else []))
        if not problems:
            plan.files += [GenFile(filename=p, content=c, purpose="server")
                           for p, c in build_server_files(s)]
            plan.files.append(GenFile(filename=".mcp.json", purpose="client_config", content=json.dumps(
                {"mcpServers": {s.name: mcp_json_entry(s)}}, indent=2) + "\n"))
        plan.files += [
            GenFile(filename="_provenance.md", purpose="provenance", content=(
                f"# Provenance: {s.name}\n\nMCP revision {MCP_REVISION}, {s.language} SDK v2.\n\n"
                + ks.render_citations(d["citations"], "Specs and SDK docs this scaffold follows"))),
            GenFile(filename="_validation.md", purpose="validation",
                    content=validation_checklist(s, problems)),
        ]
        return plan


class ServerValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        return [ValidationIssue("", "error", p) for p in plan.package_metadata.get("problems", [])]


def make_mcp_server_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=ServerDecomposer(),
                     planner=ServerPlanner(), ranking_mode="generation",
                     validator=ServerValidator(), materializer=ks.TreeMaterializer())
