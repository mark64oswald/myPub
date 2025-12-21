# myPub - AI-Powered Technical Knowledge Base

A Claude-native knowledge base system that transforms a collection of technical ePub books into an intelligent, queryable resource for learning and building.

## Overview

myPub extracts structure and knowledge from your ePub collection (~345 technical books) and makes it accessible through:

- **Natural language queries** - Ask Claude about any topic
- **Concept relationships** - Understand prerequisites and related topics
- **Multiple perspectives** - Compare how different authors treat subjects
- **Pattern libraries** - Reusable building blocks for data systems
- **Skills files** - Domain expertise encoded for Claude

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Claude Desktop / Code                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Skills (Loaded)           MCP Servers            Commands       │
│  ├── kb-usage/            ├── DuckDB              /kb-search    │
│  ├── domains/             ├── ebook-mcp           /kb-compare   │
│  └── patterns/            └── memory              /kb-prereqs   │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  DuckDB Catalog                    ePub Source Files             │
│  ~/Developer/projects/myPub/       ~/Documents/ebooks/           │
│  └── data/catalog.ddb              └── *.epub                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

1. **Initialize the catalog database**
   ```bash
   cd ~/Developer/projects/myPub
   duckdb data/catalog.ddb < schemas/catalog.sql
   ```

2. **Index your ePub collection**
   ```bash
   python scripts/index_books.py --source ~/Documents/ebooks --limit 10
   ```

3. **Add the Skills to Claude Desktop**
   - Copy `skills/` to your Claude Desktop skills location
   - Or reference via project settings

4. **Start querying**
   ```
   You: "Explain Change Data Capture and show me which books cover it"
   ```

## Directory Structure

```
myPub/
├── README.md                 # This file
├── docs/                     # Documentation
│   ├── architecture.md       # System architecture
│   ├── concepts.md           # How concepts work
│   └── patterns.md           # Pattern library guide
├── skills/                   # Claude Skills files
│   ├── kb-usage/            # Core KB usage skill
│   ├── domains/             # Domain-specific skills
│   ├── patterns/            # Pattern-focused skills
│   └── generated/           # Ad-hoc generated skills
├── mcp-servers/             # MCP server implementations
│   └── epub-kb/             # ePub knowledge base server
├── scripts/                  # Utility scripts
│   ├── index_books.py       # Index ePubs into catalog
│   ├── extract_concepts.py  # Extract concepts from chapters
│   └── generate_skill.py    # Generate skills from chapters
├── schemas/                  # Database schemas
│   └── catalog.sql          # DuckDB catalog schema
├── patterns/                 # Pattern library (YAML)
│   ├── healthcare/          # Healthcare domain patterns
│   ├── dimensional-modeling/# Dimensional modeling patterns
│   ├── data-engineering/    # Data engineering patterns
│   └── pipelines/           # Pipeline patterns (dbt, Spark)
├── commands/                 # Custom command definitions
├── tutorials/               # Usage tutorials
├── data/                    # Catalog database (gitignored)
└── tests/                   # Test files
```

## Core Concepts

### Native-First Retrieval
Instead of chunking books into vectors, we preserve author structure and load full chapters into Claude's context. Most chapters (4K-17K tokens) fit easily.

### Concept Graph
Concepts are stored with relationships (REQUIRES, RELATED_TO, EXTENDS, CONTRASTS_WITH) enabling:
- Prerequisite discovery
- Learning path generation
- Related topic exploration

### Pattern Library
Reusable patterns extracted from books, with:
- Canonical implementations
- Documented variations
- Decision frameworks for selection

## Key Commands

| Command | Description |
|---------|-------------|
| `/kb-search <topic>` | Find chapters and concepts |
| `/kb-compare <concept>` | Compare author perspectives |
| `/kb-prereqs <concept>` | Show learning prerequisites |
| `/kb-pattern <id>` | Get a pattern with variations |
| `/kb-generate-skill <topic>` | Generate a skill file |
| `/kb-learning-path <target>` | Generate reading order |

## Configuration

### ⚠️ Paths to Configure

This project has default paths that you'll need to update for your environment:

| Setting | Default Value | Where to Change |
|---------|---------------|-----------------|
| ePub library location | `~/Documents/ebooks` | `scripts/index_books.py` → `DEFAULT_SOURCE` |
| Catalog database | `~/Developer/projects/myPub/data/catalog.ddb` | All scripts → `DEFAULT_CATALOG` |

**Files with configurable paths:**

1. **`scripts/index_books.py`** (lines 20-21)
   ```python
   DEFAULT_SOURCE = os.path.expanduser("~/Documents/ebooks")
   DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")
   ```

2. **`scripts/extract_concepts.py`** (line 18)
   ```python
   DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")
   ```

3. **`scripts/generate_skill.py`** (lines 19-20)
   ```python
   DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")
   DEFAULT_OUTPUT = os.path.expanduser("~/Developer/projects/myPub/skills/generated")
   ```

4. **`CLAUDE.md`** — Update the paths in the "Key Locations" table for Claude Code

**Note:** You can also override paths via command-line arguments without editing files:
```bash
python scripts/index_books.py --source /your/ebooks/path --catalog /your/catalog/path.ddb
```

### Claude Desktop / Claude Code Setup

**MCP Server for ebook access** — Add to your MCP config:
```json
{
  "ebook-mcp": {
    "command": "uv",
    "args": ["--directory", "/path/to/ebook-mcp/", "run", "main.py"]
  }
}
```

**Optional: DuckDB MCP for catalog queries:**
```json
{
  "mypub-catalog": {
    "command": "uvx",
    "args": ["mcp-server-motherduck", "--db-path", "/your/path/to/data/catalog.ddb"]
  }
}
```

## License

Private - All rights reserved.

## Acknowledgments

Built on knowledge from ~345 technical books covering data engineering, healthcare analytics, AI/ML, and software architecture.
