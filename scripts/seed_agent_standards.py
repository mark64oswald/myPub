#!/usr/bin/env python3
"""
seed_agent_standards.py — Agent Skills, MCP, MCP Apps and Claude Code plugins as doc_sources (2026-11-08).

Before this, the catalog had one MCP source (FastMCP on DeepWiki) and
nothing for the MCP specification, MCP Apps, Agent Skills or Claude Code
plugins. This seeds the standards and the vendor docs needed to build
and scaffold plugins that bundle Skills and MCP servers.

Three tiers, highest authority first:

1. Normative specs, one github_raw file per page (authority 0.90). The
   spec text itself, not Context7's sampled snippets:
     - MCP base protocol, revision 2026-07-28 (latest)
     - MCP extensions: Apps (SEP-1865, stable 2026-01-26) and Skills
       (io.modelcontextprotocol/skills, SEP-2640 Final, Beta stage)
     - Agent Skills specification (agentskills.io)
2. Design records and guides (0.80): the SEPs behind the 2026 changes
   and the modelcontextprotocol.io / agentskills.io how-to pages.
3. Vendor docs (0.85): Claude Code and Claude platform pages through the
   web_md fetcher, and Context7 for the SDKs and tooling.

The spec pages are pinned to revision 2026-07-28. When MCP ships a new
revision, add its pages and lower or drop the old ones; the dated paths
never change in place.

Idempotent: reuses ``seed_doc_sources._upsert``.

Usage:
    .venv/bin/python3 scripts/seed_agent_standards.py --dry-run
    .venv/bin/python3 scripts/seed_agent_standards.py
    .venv/bin/python3 scripts/refresh_docs.py refresh --source-id <id>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_doc_sources import _upsert, _validate  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("seed_agent_standards")

MCP_REPO = "modelcontextprotocol/modelcontextprotocol:main"
MCP_REV = "2026-07-28"
SPEC_AUTHORITY = 0.90
RECORD_AUTHORITY = 0.80
EXAMPLE_AUTHORITY = 0.65   # examples and the pre-standard MCP-UI project


def _gh(name: str, repo_branch: str, path: str, *, authority: float, ttl: int) -> dict[str, Any]:
    return {
        "name": name,
        "source_type": "github_raw",
        "identifier": f"{repo_branch}:{path}",
        "authority_score": authority,
        "refresh_ttl_days": ttl,
    }


def _mcp_spec(title: str, page: str) -> dict[str, Any]:
    return _gh(
        f"MCP spec {MCP_REV}: {title}", MCP_REPO,
        f"docs/specification/{MCP_REV}/{page}.mdx",
        authority=SPEC_AUTHORITY, ttl=30,
    )


def _mcp_doc(title: str, page: str, *, authority: float = RECORD_AUTHORITY,
             ttl: int = 30) -> dict[str, Any]:
    return _gh(f"MCP {title}", MCP_REPO, f"docs/{page}.mdx", authority=authority, ttl=ttl)


def _web(name: str, url: str, *, ttl: int = 14) -> dict[str, Any]:
    return {"name": name, "source_type": "web_md", "identifier": url,
            "refresh_ttl_days": ttl}


def _c7(name: str, library_id: str, *, ttl: int = 14) -> dict[str, Any]:
    return {"name": name, "source_type": "context7", "identifier": library_id,
            "refresh_ttl_days": ttl}


CLAUDE_CODE = "https://code.claude.com/docs/en"
CLAUDE_PLATFORM = "https://platform.claude.com/docs/en"

AGENT_STANDARD_SEEDS: list[dict[str, Any]] = [
    # ---- Tier 1: MCP base protocol, revision 2026-07-28 ---------------------
    _mcp_spec("overview", "index"),
    _mcp_spec("changelog", "changelog"),
    _mcp_spec("deprecated features", "deprecated"),
    _mcp_spec("architecture", "architecture/index"),
    _mcp_spec("base protocol", "basic/index"),
    _mcp_spec("versioning", "basic/versioning"),
    _mcp_spec("transports", "basic/transports/index"),
    _mcp_spec("stdio transport", "basic/transports/stdio"),
    _mcp_spec("Streamable HTTP transport", "basic/transports/streamable-http"),
    _mcp_spec("authorization", "basic/authorization/index"),
    _mcp_spec("authorization security", "basic/authorization/security-considerations"),
    _mcp_spec("authorization server discovery", "basic/authorization/authorization-server-discovery"),
    _mcp_spec("client registration", "basic/authorization/client-registration"),
    _mcp_spec("patterns", "basic/patterns/index"),
    _mcp_spec("multi-round-trip requests", "basic/patterns/mrtr"),
    _mcp_spec("cancellation", "basic/patterns/cancellation"),
    _mcp_spec("progress", "basic/patterns/progress"),
    _mcp_spec("subscriptions", "basic/patterns/subscriptions"),
    _mcp_spec("server discovery", "server/discover"),
    _mcp_spec("tools", "server/tools"),
    _mcp_spec("resources", "server/resources"),
    _mcp_spec("prompts", "server/prompts"),
    _mcp_spec("completion", "server/utilities/completion"),
    _mcp_spec("logging", "server/utilities/logging"),
    _mcp_spec("pagination", "server/utilities/pagination"),
    _mcp_spec("elicitation", "client/elicitation"),
    # Roots and sampling are deprecated by SEP-2577 but still in this
    # revision; kept so generators can say so when a book recommends them.
    _mcp_spec("roots (deprecated)", "client/roots"),
    _mcp_spec("sampling (deprecated)", "client/sampling"),

    # ---- Tier 1: MCP extensions ---------------------------------------------
    _mcp_doc("extensions overview", "extensions/overview", authority=SPEC_AUTHORITY),
    _mcp_doc("extension client matrix", "extensions/client-matrix", ttl=14),
    _gh("MCP Apps spec (SEP-1865, 2026-01-26)", "modelcontextprotocol/ext-apps:main",
        "specification/2026-01-26/apps.mdx", authority=SPEC_AUTHORITY, ttl=30),
    _gh("MCP Apps spec (draft)", "modelcontextprotocol/ext-apps:main",
        "specification/draft/apps.mdx", authority=RECORD_AUTHORITY, ttl=14),
    _mcp_doc("Apps overview", "extensions/apps/overview", authority=SPEC_AUTHORITY),
    *[
        _gh(f"MCP Apps guide: {title}", "modelcontextprotocol/ext-apps:main", f"docs/{page}.md",
            authority=RECORD_AUTHORITY, ttl=14)
        for title, page in (
            ("quickstart", "quickstart"),
            ("patterns", "patterns"),
            ("migrate from OpenAI Apps SDK", "migrate_from_openai_apps"),
            ("overview", "overview"),
            ("CSP and CORS", "csp-cors"),
            ("authorization", "authorization"),
            ("agent skills", "agent-skills"),
        )
    ],
    # Two worked examples: a React server and a minimal host.
    _gh("MCP Apps example: React server", "modelcontextprotocol/ext-apps:main",
        "examples/basic-server-react/README.md", authority=EXAMPLE_AUTHORITY, ttl=30),
    _gh("MCP Apps example: basic host", "modelcontextprotocol/ext-apps:main",
        "examples/basic-host/README.md", authority=EXAMPLE_AUTHORITY, ttl=30),
    # MCP-UI (idosal/mcp-ui) is the community project MCP Apps grew out of;
    # SEP-1865 standardised its ideas. Kept as history and for its
    # migration notes, ranked below the spec so it never outranks it.
    *[
        _gh(f"MCP-UI (pre-MCP Apps): {title}", "idosal/mcp-ui:main", path,
            authority=EXAMPLE_AUTHORITY, ttl=30)
        for title, path in (
            ("README", "README.md"),
            ("introduction", "docs/src/guide/introduction.md"),
            ("relation to MCP Apps", "docs/src/guide/mcp-apps.md"),
            ("protocol details", "docs/src/guide/protocol-details.md"),
            ("Apps SDK adapter", "docs/src/guide/apps-sdk.md"),
            ("TypeScript server walkthrough", "docs/src/guide/server/typescript/walkthrough.md"),
            ("Python server walkthrough", "docs/src/guide/server/python/walkthrough.md"),
            ("client overview", "docs/src/guide/client/overview.md"),
            ("supported hosts", "docs/src/guide/supported-hosts.md"),
        )
    ],
    _mcp_doc("Apps build guide", "extensions/apps/build", ttl=14),
    _gh("MCP Skills extension spec (beta)", "modelcontextprotocol/ext-skills:main",
        "specification/beta/skills.mdx", authority=SPEC_AUTHORITY, ttl=14),
    _mcp_doc("Skills extension overview", "extensions/skills/overview", authority=SPEC_AUTHORITY),
    _gh("MCP Skills extension rationale", "modelcontextprotocol/ext-skills:main",
        "docs/rationale.md", authority=RECORD_AUTHORITY, ttl=30),
    _gh("MCP Skills extension implementations", "modelcontextprotocol/ext-skills:main",
        "docs/implementations.md", authority=RECORD_AUTHORITY, ttl=14),
    _mcp_doc("Tasks extension overview", "extensions/tasks/overview", authority=SPEC_AUTHORITY),

    # ---- Tier 1: Agent Skills (agentskills.io) ------------------------------
    _gh("Agent Skills specification", "agentskills/agentskills:main",
        "docs/specification.mdx", authority=SPEC_AUTHORITY, ttl=30),

    # ---- Tier 2: SEPs behind the 2026 changes -------------------------------
    _mcp_doc("SEP-2133 extensions", "seps/2133-extensions"),
    _mcp_doc("SEP-2640 Skills extension", "seps/2640-skills-extension"),
    _mcp_doc("SEP-1865 MCP Apps", "seps/1865-mcp-apps-interactive-user-interfaces-for-mcp"),
    _mcp_doc("SEP-2127 server cards", "seps/2127-mcp-server-cards"),
    _mcp_doc("SEP-2575 stateless MCP", "seps/2575-stateless-mcp"),
    _mcp_doc("SEP-2577 deprecate roots, sampling and logging",
             "seps/2577-deprecate-roots-sampling-and-logging"),

    # ---- Tier 2: how-to guides ----------------------------------------------
    _mcp_doc("guide: architecture", f"docs/{MCP_REV}/learn/architecture"),
    _mcp_doc("guide: build a server", f"docs/{MCP_REV}/develop/build-server"),
    _mcp_doc("guide: build with Agent Skills", f"docs/{MCP_REV}/develop/build-with-agent-skills"),
    _mcp_doc("guide: security best practices", f"docs/{MCP_REV}/tutorials/security/security_best_practices"),
    _mcp_doc("guide: SDKs", f"docs/{MCP_REV}/sdk", ttl=14),
    _gh("MCP Registry quickstart", "modelcontextprotocol/registry:main",
        "docs/modelcontextprotocol-io/quickstart.mdx", authority=RECORD_AUTHORITY, ttl=14),
    _gh("MCP Registry server.json reference", "modelcontextprotocol/registry:main",
        "docs/reference/server-json/generic-server-json.md", authority=RECORD_AUTHORITY, ttl=14),
    *[
        _gh(f"Agent Skills guide: {title}", "agentskills/agentskills:main",
            f"docs/{page}.mdx", authority=RECORD_AUTHORITY, ttl=30)
        for title, page in (
            ("quickstart", "skill-creation/quickstart"),
            ("best practices", "skill-creation/best-practices"),
            ("optimizing descriptions", "skill-creation/optimizing-descriptions"),
            ("evaluating skills", "skill-creation/evaluating-skills"),
            ("using scripts", "skill-creation/using-scripts"),
            ("adding skills support to a client", "client-implementation/adding-skills-support"),
        )
    ],

    # ---- Tier 3: Claude Code (plugins, skills, MCP) -------------------------
    _web("Claude Code: plugins", f"{CLAUDE_CODE}/plugins.md"),
    _web("Claude Code: plugins reference", f"{CLAUDE_CODE}/plugins-reference.md"),
    _web("Claude Code: plugin marketplaces", f"{CLAUDE_CODE}/plugin-marketplaces.md"),
    _web("Claude Code: discover plugins", f"{CLAUDE_CODE}/discover-plugins.md"),
    _web("Claude Code: skills", f"{CLAUDE_CODE}/skills.md"),
    _web("Claude Code: MCP", f"{CLAUDE_CODE}/mcp.md"),
    _web("Claude Code: subagents", f"{CLAUDE_CODE}/sub-agents.md"),
    _web("Claude Code: hooks", f"{CLAUDE_CODE}/hooks.md"),
    _web("Claude Agent SDK: overview", f"{CLAUDE_CODE}/agent-sdk/overview.md"),
    _web("Claude Agent SDK: skills", f"{CLAUDE_CODE}/agent-sdk/skills.md"),
    _web("Claude Agent SDK: plugins", f"{CLAUDE_CODE}/agent-sdk/plugins.md"),
    _web("Claude Agent SDK: MCP", f"{CLAUDE_CODE}/agent-sdk/mcp.md"),

    # ---- Tier 3: MCP Apps in Claude (host docs) ------------------------------
    *[
        _web(f"Claude MCP Apps: {title}",
             f"https://claude.com/docs/connectors/building/mcp-apps/{page}.md")
        for title, page in (
            ("getting started", "getting-started"),
            ("quickstart", "quickstart"),
            ("design guidelines", "design-guidelines"),
            ("troubleshooting", "troubleshooting"),
        )
    ],

    # ---- Tier 3: Claude platform (Agent Skills, MCP connector) --------------
    _web("Claude platform: Agent Skills overview",
         f"{CLAUDE_PLATFORM}/agents-and-tools/agent-skills/overview.md"),
    _web("Claude platform: Agent Skills quickstart",
         f"{CLAUDE_PLATFORM}/agents-and-tools/agent-skills/quickstart.md"),
    _web("Claude platform: Agent Skills best practices",
         f"{CLAUDE_PLATFORM}/agents-and-tools/agent-skills/best-practices.md"),
    _web("Claude platform: Skills API guide",
         f"{CLAUDE_PLATFORM}/build-with-claude/skills-guide.md"),
    _web("Claude platform: MCP connector",
         f"{CLAUDE_PLATFORM}/agents-and-tools/mcp-connector.md"),

    # ---- Tier 3: SDKs and tooling via Context7 ------------------------------
    _c7("MCP Python SDK", "/modelcontextprotocol/python-sdk"),
    _c7("MCP TypeScript SDK", "/modelcontextprotocol/typescript-sdk"),
    _c7("MCP Apps SDK (ext-apps)", "/modelcontextprotocol/ext-apps"),
    _c7("MCP Apps SDK API reference", "/websites/apps_extensions_modelcontextprotocol_io_api"),
    _c7("OpenAI Apps SDK examples (ChatGPT host)", "/openai/openai-apps-sdk-examples", ttl=30),
    _c7("MCP Inspector", "/modelcontextprotocol/inspector"),
    _c7("MCP Registry", "/modelcontextprotocol/registry"),
    _c7("Anthropic example skills", "/anthropics/skills"),
    _c7("Agent2Agent (A2A) protocol", "/websites/a2a-protocol", ttl=30),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be seeded without opening the catalog.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    for seed in AGENT_STANDARD_SEEDS:
        _validate(seed)
    if args.dry_run:
        for seed in AGENT_STANDARD_SEEDS:
            LOG.info("would seed %-10s %-55s %s", seed["source_type"], seed["name"],
                     seed["identifier"])
        LOG.info("%d sources", len(AGENT_STANDARD_SEEDS))
        return 0

    conn = duckdb.connect(str(args.catalog))
    try:
        conn.execute("BEGIN")
        counts = {"inserted": 0, "updated": 0}
        for seed in AGENT_STANDARD_SEEDS:
            counts[_upsert(conn, seed)] += 1
        conn.execute("COMMIT")
        LOG.info("done: %d inserted, %d updated", counts["inserted"], counts["updated"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
