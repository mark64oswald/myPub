"""Tests for the Agent Skills / MCP server / plugin scaffold generators (2026-11).

Covers agent_skills.py (spec rules), agent_skill_gen.py (/kb-skill-from-kb),
mcp_server_gen.py (/kb-mcp-server) and plugin_scaffold.py (/kb-plugin-scaffold)
against a tiny seeded catalog. Generated Python must parse and generated JSON
must load; running the servers against the real SDKs is a manual check.
"""
from __future__ import annotations

import ast
import keyword
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import agent_skill_gen  # noqa: E402
import agent_skills  # noqa: E402
import kb_scaffold as ks  # noqa: E402
import mcp_server_gen  # noqa: E402
import plugin_scaffold  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _concept(conn, name, description=None):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) VALUES (?, 'Pattern', ?) "
        "RETURNING concept_id", [name, description],
    ).fetchone()[0]


def _procedure(conn, name, steps, concept_id, *, failure_modes=None, preconditions=None,
               source_id=1):
    pid = conn.execute(
        "INSERT INTO procedure (name, preconditions, steps, failure_modes, source_type, source_id) "
        "VALUES (?, ?, ?, ?, 'chapter', ?) RETURNING procedure_id",
        [name, preconditions, steps, failure_modes, source_id],
    ).fetchone()[0]
    conn.execute("INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
                 [pid, concept_id])
    return pid


def _rel(conn, src, tgt, rel_type, sid):
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
        "source_type, source_id, confidence) VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, sid],
    )


@pytest.fixture
def corpus(catalog):
    es = _concept(catalog, "Event Sourcing",
                  "Event sourcing stores state as an append-only log of events. It enables replay.")
    cqrs = _concept(catalog, "CQRS", "Separate read and write models.")
    _rel(catalog, es, cqrs, "REQUIRES", 1)
    _procedure(catalog, "Append an event to the store", "1. Validate.\n2. Append.\n3. Publish.",
               es, failure_modes="Concurrent writers can conflict; use optimistic versioning.")
    _procedure(catalog, "Rebuild a projection", "1. Reset.\n2. Replay all events.", es,
               preconditions="Events are immutable.")
    ids = {"event sourcing": es, "cqrs": cqrs}
    resolver = MagicMock()
    resolver.resolve_lookup_only.side_effect = lambda n: ids.get(n.strip().lower())
    return catalog, resolver


def _frontmatter(md: str) -> dict:
    head = md.split("\n---\n", 1)[0].splitlines()[1:]
    out = {}
    for line in head:
        if line.startswith(" "):
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        out[k] = json.loads(v) if v.startswith('"') else v
    return out


# ---------------------------------------------------------------------------
# agent_skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("Event Sourcing Basics", "event-sourcing-basics"),
    ("  C++ / Rust: FFI  ", "c-rust-ffi"),
    ("---", "skill"),
])
def test_skill_name_is_spec_slug(title, expected):
    assert agent_skills.skill_name(title) == expected


def test_validate_flags_spec_violations():
    assert agent_skills.validate("good-name", "Does X. Use when Y.", folder="good-name") == []
    probs = agent_skills.validate("Bad--Name", "", folder="other")
    assert any("lowercase" in p for p in probs)
    assert any("match its folder" in p for p in probs)
    assert any("description is required" in p for p in probs)
    assert agent_skills.errors(agent_skills.validate("x", "d", body="\n" * 600)) == []


def test_fit_description_caps_length():
    d = agent_skills.fit_description("word " * 400)
    assert len(d) <= agent_skills.DESCRIPTION_MAX


def test_write_skill_writes_folder(tmp_path):
    w = agent_skills.write_skill(tmp_path, "My Skill", "Does a thing. Use when asked.", "Body",
                                 references={"a.md": "A"}, scripts={"run.sh": "echo"})
    assert w.name == "my-skill" and w.problems == []
    assert (tmp_path / "my-skill" / "references" / "a.md").exists()
    assert (tmp_path / "my-skill" / "scripts" / "run.sh").stat().st_mode & 0o111


# ---------------------------------------------------------------------------
# /kb-skill-from-kb
# ---------------------------------------------------------------------------


def test_agent_skill_generator_end_to_end(corpus, tmp_path):
    conn, resolver = corpus
    gen = agent_skill_gen.make_agent_skill_generator()
    pid, report, issues = gen.run_deterministic(conn, resolver, "Event Sourcing",
                                                output_root=str(tmp_path))
    assert pid != -1, issues
    root = tmp_path / "event-sourcing"
    md = (root / "event-sourcing" / "SKILL.md").read_text()
    fm = _frontmatter(md)
    assert fm["name"] == "event-sourcing"
    assert "Use when the task involves Event Sourcing" in fm["description"]
    assert agent_skills.errors(agent_skills.validate(fm["name"], fm["description"],
                                                     folder="event-sourcing")) == []
    assert "Rebuild a projection" in md and "This builds on: CQRS" in md
    assert "optimistic versioning" in (root / "event-sourcing/references/procedures.md").read_text()
    for f in ("_provenance.md", "_validation.md", "_prompts/refine-skill.md"):
        assert (root / f).exists()
    n = conn.execute("SELECT count(*) FROM generated_package WHERE generator_type='agent_skill'"
                     ).fetchone()[0]
    assert n == 1


def test_agent_skill_unknown_topic_fails_cleanly(corpus, tmp_path):
    conn, resolver = corpus
    pid, _report, issues = agent_skill_gen.make_agent_skill_generator().run_deterministic(
        conn, resolver, "Nonexistent", output_root=str(tmp_path))
    assert pid == -1
    assert any("not found" in i.message for i in issues)


# ---------------------------------------------------------------------------
# /kb-mcp-server
# ---------------------------------------------------------------------------


def test_tools_from_domain_procedures(corpus):
    conn, resolver = corpus
    tools, notes = mcp_server_gen.tools_from_request(conn, resolver, domain="Event Sourcing")
    names = {t.name for t in tools}
    assert names == {"append_an_event_to_the_store", "rebuild_a_projection"}
    assert all(t.procedure_id for t in tools) and notes == []
    fallback, notes = mcp_server_gen.tools_from_request(conn, resolver, domain="Nope")
    assert [t.name for t in fallback] == ["echo"] and notes


@pytest.mark.parametrize("ui", [False, True])
@pytest.mark.parametrize("with_skills", [False, True])
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_python_server_files_parse(corpus, ui, with_skills, transport):
    conn, resolver = corpus
    tm = ks.topic_material(conn, resolver, "Event Sourcing")
    spec = mcp_server_gen.ServerSpec(
        name="es-server", purpose='Event "sourcing" tools', ui=ui, transport=transport,
        tools=[mcp_server_gen.ToolStub("replay", "Replay events."),
               mcp_server_gen.ToolStub("append", "Append one.")],
        skills=[tm] if with_skills else [])
    files = dict(mcp_server_gen.build_server_files(spec))
    ast.parse(files["server.py"])
    ast.parse(files["tests/test_server.py"])
    json.loads(files["server.json"])
    assert ('ui/replay.html' in files) == ui
    assert ("skills/event-sourcing/SKILL.md" in files) == with_skills
    assert ("streamable-http" in files["server.py"]) == (transport == "http")
    if ui:
        # App tools must be registered on the extension before MCPServer() reads it.
        src = files["server.py"]
        assert src.index("@apps.tool") < src.index("mcp = MCPServer(")


def test_typescript_server_files(corpus):
    spec = mcp_server_gen.ServerSpec(name="es-server", purpose="Event tools", language="typescript",
                                     ui=True, tools=[mcp_server_gen.ToolStub("replay", "Replay `x`.")])
    files = dict(mcp_server_gen.build_server_files(spec))
    pkg = json.loads(files["package.json"])
    assert "@modelcontextprotocol/ext-apps" in pkg["dependencies"]
    assert "registerAppTool" in files["src/server.ts"]
    assert json.loads(files["tsconfig.json"])["compilerOptions"]["strict"] is True
    assert {"mcp-app.html", "src/mcp-app.ts", "vite.config.ts"} <= set(files)


def test_server_problems():
    ts_http = mcp_server_gen.ServerSpec(name="x", purpose="p", language="typescript",
                                        transport="http", tools=[mcp_server_gen.ToolStub("a", "b")])
    assert any("stdio only" in p for p in mcp_server_gen.server_problems(ts_http))


def test_mcp_server_generator_end_to_end(corpus, tmp_path):
    conn, resolver = corpus
    pid, report, issues = mcp_server_gen.make_mcp_server_generator().run_deterministic(
        conn, resolver, "Event store tools", domain="Event Sourcing", ui=True,
        skill_topics=["Event Sourcing", "Missing Topic"], output_root=str(tmp_path))
    assert pid != -1, issues
    root = tmp_path / "event-store-tools"
    ast.parse((root / "server.py").read_text())
    cfg = json.loads((root / ".mcp.json").read_text())
    assert cfg["mcpServers"]["event-store-tools"]["command"] == "uv"
    assert (root / "skills/event-sourcing/SKILL.md").exists()
    assert any("Missing Topic" in n for n in report.notes)


# ---------------------------------------------------------------------------
# /kb-plugin-scaffold
# ---------------------------------------------------------------------------


def test_plugin_scaffold_end_to_end(corpus, tmp_path):
    conn, resolver = corpus
    gen = plugin_scaffold.make_plugin_scaffold_generator()
    pid, report, issues = gen.run_deterministic(
        conn, resolver, "Event sourcing helpers", plugin_name="es-tools",
        skill_topics=["Event Sourcing"], workflows=["Review event model"],
        mcp_server=True, domain="Event Sourcing", hooks=True, output_root=str(tmp_path))
    assert pid != -1, issues
    pkg = tmp_path / "es-tools"
    market = json.loads((pkg / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"][0]["source"] == "./plugins/es-tools"
    plug = pkg / "plugins/es-tools"
    manifest = json.loads((plug / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "es-tools"
    for skill_md in plug.glob("skills/*/SKILL.md"):
        fm = _frontmatter(skill_md.read_text())
        assert fm["name"] == skill_md.parent.name
    wf = (plug / "skills/review-event-model/SKILL.md").read_text()
    assert "disable-model-invocation: true" in wf
    mcp_cfg = json.loads((plug / ".mcp.json").read_text())
    args = mcp_cfg["mcpServers"]["es-tools-server"]["args"]
    assert "${CLAUDE_PLUGIN_ROOT}/servers/es-tools-server" in args
    ast.parse((plug / "servers/es-tools-server/server.py").read_text())
    hooks = json.loads((plug / "hooks/hooks.json").read_text())
    assert hooks["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit"
    assert (plug / "scripts/after-edit.sh").stat().st_mode & 0o111


def test_plugin_rejects_reserved_name():
    p = plugin_scaffold.PluginSpec(name="claude-helper", purpose="x", workflows=["Do it"])
    assert any("reserved" in x for x in plugin_scaffold.plugin_problems(p))


def test_plugin_requires_content():
    p = plugin_scaffold.PluginSpec(name="empty", purpose="x")
    assert plugin_scaffold.plugin_problems(p)


def test_identifiers_are_valid_python():
    for text in ("Append an event!", "123 start", "class"):
        ident = ks.identifier(text)
        assert re.match(r"^[a-z_][a-z0-9_]*$", ident), ident
        assert ident.isidentifier() and not keyword.iskeyword(ident), ident


# ---------------------------------------------------------------------------
# Follow-ups from the first real-catalog run (2026-11-08)
# ---------------------------------------------------------------------------


def test_domain_procedures_filtered_by_purpose(corpus):
    conn, resolver = corpus
    tools, notes = mcp_server_gen.tools_from_request(
        conn, resolver, domain="Event Sourcing", purpose="Rebuild projections")
    assert [t.name for t in tools] == ["rebuild_a_projection"]
    tools, notes = mcp_server_gen.tools_from_request(
        conn, resolver, domain="Event Sourcing", purpose="Search my notes")
    assert [t.name for t in tools] == ["search_my_notes"]
    assert any("match the purpose" in n for n in notes)


def test_topic_material_pools_same_name_concepts(corpus):
    conn, resolver = corpus
    twin = conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES ('event sourcing', 'Tool') "
        "RETURNING concept_id").fetchone()[0]
    _procedure(conn, "Snapshot an aggregate", "1. Save state.", twin, source_id=2)
    tm = ks.topic_material(conn, resolver, "Event Sourcing")
    assert "Snapshot an aggregate" in {p.name for p in tm.procedures}


def test_identifier_cuts_at_word_boundary():
    ident = ks.identifier("Install the MCP Apps skills with the Vercel skills command line tool")
    assert len(ident) <= 48 and not ident.endswith("_skil")
    assert ident.split("_")[-1] in {"skills", "vercel", "the", "with", "command"}


def test_plugin_warns_on_placeholder_author(corpus, tmp_path):
    conn, resolver = corpus
    pid, _r, issues = plugin_scaffold.make_plugin_scaffold_generator().run_deterministic(
        conn, resolver, "Helpers", plugin_name="helpers", workflows=["Do it"],
        output_root=str(tmp_path))
    assert pid != -1
    assert any(i.severity == "warning" and "Your Name" in i.message for i in issues)


def test_rank_procedures_prefers_substance_and_spreads_sources():
    rows = [
        (1, "Display FastMCP version information", None, "Run fastmcp version", None, "doc_section", 7),
        (2, "Copy version information to clipboard", None, "Click copy", None, "doc_section", 7),
        (3, "Build an MCP server with tools", None, "1. Create server\n2. Add tool\n3. Run", None,
         "doc_section", 7),
        (4, "Add a tool to an MCP server", None, "1. Decorate\n2. Return content", None,
         "doc_section", 7),
        (5, "Connect an MCP client", None, "1. Start\n2. Initialize", None, "chapter", 9),
    ]
    out = ks.rank_procedures(rows, "Model Context Protocol", limit=3)
    ids = [r[0] for r in out]
    assert ids[:2] == [3, 4] and 5 in ids          # "mcp" acronym match, 2-per-source cap
    assert 1 not in ids and 2 not in ids             # housekeeping ranked out


def test_tool_match_uses_procedure_names_only(corpus):
    conn, resolver = corpus
    # "Publish" appears only in the steps of "Append an event to the store".
    tools, notes = mcp_server_gen.tools_from_request(
        conn, resolver, domain="Event Sourcing", purpose="Publish things")
    assert [t.name for t in tools] == ["publish_things"]


def test_rerun_removes_stale_files(corpus, tmp_path):
    conn, resolver = corpus
    gen = mcp_server_gen.make_mcp_server_generator()
    gen.run_deterministic(conn, resolver, "Notes", tools=["old_tool"], ui=True,
                          server_name="notes", output_root=str(tmp_path))
    assert (tmp_path / "notes/ui/old_tool.html").exists()
    _pid, report, _ = gen.run_deterministic(conn, resolver, "Notes", tools=["new_tool"], ui=True,
                                            server_name="notes", output_root=str(tmp_path))
    assert not (tmp_path / "notes/ui/old_tool.html").exists()
    assert (tmp_path / "notes/ui/new_tool.html").exists()
    assert any("removed stale ui/old_tool.html" in n for n in report.notes)
