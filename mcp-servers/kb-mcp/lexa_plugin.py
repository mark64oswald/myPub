"""Read Lexa BioScience's skills and MCP tools from the installed plugin.

The Bootstrap ``lexa`` profile builds an agent on top of the
``lexa-bioscience`` Claude Code plugin. Lexa is not indexed in the myPub
catalog, and it does not need to be: the plugin itself is the source of
truth. At generation time this module finds the installed plugin, reads
what it ships, and picks the skills and tools that fit the request.

What the plugin ships (all static files, nothing is launched):

- ``.claude-plugin/plugin.json``: name + version.
- ``skills/<folder>/SKILL.md``: one skill per folder (frontmatter name +
  description).
- ``runtime/operation-contract.json``: every MCP tool with its server and
  parameters. Older builds without it fall back to ``.mcp.json`` (server
  names only).

Where the plugin is looked for, first hit wins:

1. an explicit path (``plugin_path`` on the bootstrap tool),
2. ``$LEXA_PLUGIN_ROOT``,
3. Claude Code's plugin folders under ``~/.claude/plugins`` (marketplace
   cache, synced and marketplace checkouts).

When nothing is found the profile falls back to a built-in snapshot and
says so in the package notes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

PLUGIN_NAME = "lexa-bioscience"
ENV_VAR = "LEXA_PLUGIN_ROOT"

# Lexa skills every generated agent leans on, whatever the domain: licensing
# and per-domain teaching rules, and provenance for citations.
ALWAYS_SKILLS = ("education-domain-rules", "provenance")

# Built-in snapshot (plugin 0.3.0-alpha.1) for when no plugin is installed.
FALLBACK_SKILLS = (
    "cancer-genomics", "clinical", "drug-discovery", "education",
    "education-domain-rules", "education-family-rules", "education-routing",
    "epigenomics", "genomics", "graph", "image-spatial", "immuno-oncology",
    "literature", "longitudinal-clinical", "matrix", "metabolomics",
    "metagenomics", "molecularsim", "molecularsim-outputs", "multi-omics",
    "oncology", "pharmacogenomics", "pipeline-builder", "precision-oncology",
    "proteomics", "provenance", "resistance-reasoning", "sequence",
    "single-cell", "spatial-bio", "spectra-signal", "structural-bio",
    "structure-3d", "synbio", "systems-bio", "tabular", "transcriptomics")
FALLBACK_SERVERS = (
    "cbioportal-mcp", "chembl-mcp", "clinvar-mcp", "cosmic-mcp", "ensembl-mcp",
    "gdc-mcp", "geo-mcp", "learner-mcp", "molecularsim-mcp", "ncbi-mcp",
    "ontology-mcp", "pathway-mcp", "pdb-mcp", "string-mcp", "uniprot-mcp")

# Words too common in Lexa's skill descriptions to say anything about fit.
_STOP = frozenset("""
a an and are as at be by can do does for from has have how i in into is it its
me my not of on or our so that the their them then this to use used uses using
we what when where which who why will with without you your about any all also
analysis analyze activate trigger triggers load loads skill skills tool tools
question questions data agent build built make help user users lexa plugin
look looks up explain explains check checks assistant based
""".split())

# Teaching requests ("a tutor that explains ...") want Lexa's education
# skills, whose descriptions don't use those words.
TEACHING_WORDS = frozenset(
    "tutor tutoring teach teaching teacher learn learner learning lesson lessons "
    "course student students curriculum explain explains".split())
TEACHING_SKILLS = ("education", "education-routing")


@dataclass
class LexaSkill:
    folder: str            # skills/<folder>; invoked as /lexa-bioscience:<folder>
    name: str              # frontmatter name
    description: str


@dataclass
class LexaTool:
    server: str            # e.g. clinvar-mcp
    name: str              # e.g. search_variant
    params: list[str] = field(default_factory=list)   # required first, '?' marks optional

    @property
    def qualified(self) -> str:
        """The name Claude Code shows for a plugin MCP tool."""
        return f"mcp__plugin_{PLUGIN_NAME}_{self.server}__{self.name}"


@dataclass
class LexaCapabilities:
    version: str
    source: str                          # where it was read from, or 'built-in snapshot'
    skills: list[LexaSkill]
    tools: list[LexaTool]
    servers: list[str]
    installed: bool = True

    def tools_for(self, server: str) -> list[LexaTool]:
        return [t for t in self.tools if t.server == server]

    def to_json(self) -> dict:
        return {
            "plugin": PLUGIN_NAME, "version": self.version, "source": self.source,
            "skills": [{"invoke": f"/{PLUGIN_NAME}:{s.folder}", "name": s.name,
                        "description": s.description} for s in self.skills],
            "mcp_servers": {srv: [{"tool": t.name, "qualified": t.qualified,
                                   "params": t.params} for t in self.tools_for(srv)]
                            for srv in self.servers},
        }


@dataclass
class LexaSelection:
    caps: LexaCapabilities
    skills: list[LexaSkill]              # picked for the request, best first
    servers: list[str]                   # servers those skills (or the request) use
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Finding and reading the plugin
# ---------------------------------------------------------------------------


def _version_key(path: Path) -> tuple:
    try:
        v = json.loads((path / ".claude-plugin" / "plugin.json").read_text()).get("version", "")
    except (OSError, ValueError):
        v = ""
    nums = tuple(int(n) for n in re.findall(r"\d+", v)[:3])
    return nums, path.stat().st_mtime


def _candidates(home: Path) -> Iterable[Path]:
    base = home / ".claude" / "plugins"
    if not base.is_dir():
        return []
    # cache/<marketplace>/<plugin>/<version>/, synced/<id>/<plugin>/,
    # marketplaces/<marketplace>/[plugins/]<plugin>/ — at most four levels down.
    found = []
    for pattern in ("*/*/.claude-plugin/plugin.json", "*/*/*/.claude-plugin/plugin.json",
                    "*/*/*/*/.claude-plugin/plugin.json"):
        for manifest in base.glob(pattern):
            root = manifest.parent.parent
            try:
                if json.loads(manifest.read_text()).get("name") == PLUGIN_NAME:
                    found.append(root)
            except (OSError, ValueError):
                continue
    return found


def is_plugin_root(path: Path) -> bool:
    manifest = path / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text()).get("name") == PLUGIN_NAME
    except (OSError, ValueError):
        return False


def find_plugin(explicit: Optional[str] = None, *, home: Optional[Path] = None,
                env: Optional[dict] = None) -> Optional[Path]:
    """Return the installed Lexa plugin root, or None."""
    env = os.environ if env is None else env
    for given in (explicit, env.get(ENV_VAR)):
        if given:
            p = Path(given).expanduser()
            # Accept a repo checkout whose plugin lives one level down too.
            for cand in (p, p / PLUGIN_NAME, p / "plugin"):
                if is_plugin_root(cand):
                    return cand
    roots = list(_candidates(home or Path.home()))
    if not roots:
        return None
    return max(roots, key=_version_key)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    head = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in head.splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            val = m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                try:
                    val = json.loads(val)
                except ValueError:
                    val = val.strip('"')
            out[m.group(1)] = val.strip()
    return out


def read_capabilities(root: Path) -> LexaCapabilities:
    """Read skills and MCP tools from a plugin root (no servers are started)."""
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    skills_dir = root / str(manifest.get("skills", "skills")).strip("./") \
        if isinstance(manifest.get("skills"), str) else root / "skills"
    skills = []
    for md in sorted(skills_dir.glob("*/SKILL.md")):
        fm = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        skills.append(LexaSkill(folder=md.parent.name, name=fm.get("name", md.parent.name),
                                description=fm.get("description", "")))

    tools: list[LexaTool] = []
    contract = root / "runtime" / "operation-contract.json"
    if contract.exists():
        ops = json.loads(contract.read_text()).get("operations", {})
        for op in ops.values():
            params = [p["name"] if p.get("required") else p["name"] + "?"
                      for p in op.get("parameters", [])]
            tools.append(LexaTool(server=op["server"], name=op["tool"], params=params))
    servers = sorted({t.server for t in tools})
    mcp_json = root / ".mcp.json"
    if mcp_json.exists():
        try:
            declared = json.loads(mcp_json.read_text()).get("mcpServers", {})
            servers = sorted(set(servers) | set(declared))
        except ValueError:
            pass
    tools.sort(key=lambda t: (t.server, t.name))
    return LexaCapabilities(version=str(manifest.get("version", "unknown")),
                            source=str(root), skills=skills, tools=tools, servers=servers)


def fallback_capabilities() -> LexaCapabilities:
    return LexaCapabilities(
        version="0.3.0-alpha.1 (snapshot)", source="built-in snapshot",
        skills=[LexaSkill(folder=f, name=f, description="") for f in FALLBACK_SKILLS],
        tools=[], servers=list(FALLBACK_SERVERS), installed=False)


def load(explicit: Optional[str] = None, **kw) -> tuple[LexaCapabilities, list[str]]:
    """Find and read the plugin; fall back to the snapshot with a note."""
    root = find_plugin(explicit, **kw)
    if root is None:
        where = f"{explicit!r}, " if explicit else ""
        return fallback_capabilities(), [
            f"lexa: plugin not found ({where}${ENV_VAR}, ~/.claude/plugins); used the "
            "built-in skill/server snapshot. Install lexa-bioscience or pass plugin_path "
            "to read its live skills and tools."]
    caps = read_capabilities(root)
    return caps, [f"lexa: read plugin {caps.version} from {root} "
                  f"({len(caps.skills)} skills, {len(caps.tools)} tools on "
                  f"{len(caps.servers)} MCP servers)"]


# ---------------------------------------------------------------------------
# Picking what fits the request
# ---------------------------------------------------------------------------


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9-]+", text.lower()):
        for part in {w, *w.split("-")}:
            if len(part) > 2 and part not in _STOP:
                out.add(part[:-1] if part.endswith("s") and len(part) > 4 else part)
    return out


def _server_stem(server: str) -> str:
    return server.removesuffix("-mcp")


def _servers_named(text: str, servers: list[str]) -> list[str]:
    low = text.lower()
    return [s for s in servers if re.search(rf"\b{re.escape(_server_stem(s))}\b", low)]


def select(caps: LexaCapabilities, request: str, *, limit: int = 3) -> LexaSelection:
    """Pick the Lexa skills and MCP servers that fit a request.

    Skills score on overlap between the request and the skill's folder,
    name and description (folder/name hits count triple). The always-on
    skills (licensing rules, provenance) are appended. Servers are those
    the picked skills name in their descriptions ("load it BEFORE calling
    the clinvar, ncbi or ensembl tools") plus any the request names.
    """
    req = _words(request)
    scored = []
    for s in caps.skills:
        head = _words(s.folder + " " + s.name.replace("-analysis", ""))
        body = _words(s.description)
        score = 3 * len(req & head) + len(req & body)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1].folder))
    # Keep the clear winners only: a description shares a word or two with
    # almost any biology request, so near-best is the bar, not any overlap.
    best = scored[0][0] if scored else 0
    bar = max(2, 0.75 * best)
    by_folder = {s.folder: s for s in caps.skills}
    picked = [by_folder[f] for f in TEACHING_SKILLS
              if f in by_folder and set(re.findall(r"[a-z]+", request.lower())) & TEACHING_WORDS]
    picked += [s for score, s in scored
               if score >= bar and s.folder not in ALWAYS_SKILLS and s not in picked]
    picked = picked[:limit]
    notes = []
    if not picked:
        notes.append("lexa: no Lexa skill matched the request words; the workflow "
                     "skill leaves the domain skill for a sub-agent to choose")
    picked += [by_folder[f] for f in ALWAYS_SKILLS if f in by_folder]

    servers: list[str] = []
    # The always-on skills mention many servers in passing; only domain
    # skills say which tools they own.
    for s in picked:
        if s.folder not in ALWAYS_SKILLS:
            servers += _servers_named(s.description, caps.servers)
    servers += _servers_named(request, caps.servers)
    # A tool name in the request ("search_variant") pulls in its server.
    for t in caps.tools:
        if t.name in request:
            servers.append(t.server)
    servers = list(dict.fromkeys(servers))
    return LexaSelection(caps=caps, skills=picked, servers=servers, notes=notes)


# ---------------------------------------------------------------------------
# Rendering helpers for the profile
# ---------------------------------------------------------------------------


def _one_line(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def skills_table(sel: LexaSelection) -> str:
    rows = ["| Skill | Use it for |", "|---|---|"]
    for s in sel.skills:
        rows.append(f"| `/{PLUGIN_NAME}:{s.folder}` | {_one_line(s.description) or s.name} |")
    return "\n".join(rows)


def tools_table(sel: LexaSelection) -> str:
    caps = sel.caps
    if not sel.servers:
        return "_No MCP server matched the request; see `.claude/lexa-capabilities.json`._"
    rows = ["| MCP server | Tools |", "|---|---|"]
    for srv in sel.servers:
        names = [t.name for t in caps.tools_for(srv)]
        rows.append(f"| `{srv}` | {', '.join(f'`{n}`' for n in names) or '_(see plugin)_'} |")
    return "\n".join(rows)
