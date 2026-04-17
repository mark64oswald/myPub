# myPub - AI-Powered Technical Knowledge Base

A Claude-native knowledge base system that transforms a collection of technical ePub books into an intelligent, queryable resource for learning and building.

## v2 Development

Active v2 work lives on the `v2-substrate` branch. v2 layers semantic (HNSW/VSS),
full-text (BM25/FTS), and graph (DuckPGQ) retrieval on top of a cleaned-up DuckDB
catalog, introduces live doc sources (Context7, DeepWiki, GitHub MCP) alongside
books, and adds a Skills Factory that generates whole Claude Skills packages.

- **Architecture doc:** [docs/mypub-v2-architecture.md](docs/mypub-v2-architecture.md) — full system design, data model, ranking, refresh strategy
- **Execution plan:** [docs/mypub-v2-execution-plan.md](docs/mypub-v2-execution-plan.md) — phased roadmap

v1 on `main` is stable and unaffected. The v1 catalog is preserved at
`data/catalog_v1_backup.ddb` on the branch; `data/catalog.ddb` was
rebuilt from scratch during Phase 1.

### Phase 1 status: substrate complete

Phase 1 (substrate upgrade) is done on `v2-substrate`. Results:

- **Schema** — singular-named v2 tables with BIGINT surrogate PKs (author, book,
  book_author, chapter, concept, concept_relation, concept_alias,
  concept_resolution_queue, concept_query_log, concept_doc_link, doc_source,
  doc_snapshot, doc_section, procedure, skill_package, skill, skill_source,
  skill_file, skill_relation) plus side tables for each FLOAT[384] embedding.
- **Corpus** — 541 books / 113,165 chapters / 112,968 with content indexed
  from `~/Documents/eBooks`.
- **FTS (BM25)** — Porter-stemmed index on `chapter.content`; keyword search
  returns the right textbook sources ("dimensional modeling" → Kimball).
- **VSS (HNSW, cosine)** — 384-dim `sentence-transformers/all-MiniLM-L6-v2`
  embeddings on every chapter with content; semantic search returns topically
  relevant chapters across multiple books.
- **DuckPGQ property graph** — `mypub` graph with author/book/chapter vertex
  tables and wrote/book_contains/concept_relates_to/… edge labels. Concept
  and doc edges are stubbed for Phase 2.
- **Tests** — 40 passing: 28 schema, 12 integration (FTS × VSS × graph on
  3 seed topics with overlap/divergence assertions).

Known DuckDB-version-specific workarounds (encoded in schema comments and
scripts):

- Pinned to **duckdb == 1.5.0**: DuckPGQ community extension is absent on 1.5.2
  and broken on 1.5.1.
- Embeddings live in side tables (`chapter_embedding`, etc.) because DuckDB 1.5
  fails with a spurious FK violation on `UPDATE … SET <FLOAT[N] column>` for any
  row with inbound FK references.
- Self-referential FKs (`chapter.parent_chapter_id`, `doc_section.parent_id`)
  are declared as plain `BIGINT` columns with application-enforced integrity;
  the same bug mis-blocks UPDATE/DELETE on parent rows.
- `SET hnsw_enable_experimental_persistence = true` is required before any
  HNSW index on a file-backed database.
- DuckPGQ edge-table `LABEL`s must be globally unique across the graph and
  lowercase (Book/Edge collide with reserved tokens), and its parser
  extension can't handle `--` comments before `DROP/CREATE PROPERTY GRAPH`.

### Dev environment setup

```bash
git checkout v2-substrate
python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install -e ".[dev]"

# Smoke test
.venv/bin/python3 -c "import duckdb; print(duckdb.__version__)"
```

Dependencies are declared in [`pyproject.toml`](pyproject.toml): `duckdb`,
`sentence-transformers`, `fastmcp`, `markdown-it-py`, `pydantic`, `httpx`,
`ebooklib`, `beautifulsoup4`, `tiktoken`, and `pytest` (dev extra).

### Rebuilding the substrate from scratch

The scripts are idempotent and resumable. A full cold start:

```bash
# 1. Create the v2 schema (writes a backup first).
.venv/bin/python3 scripts/migrate_v2_schema.py

# 2. Install and smoke-test extensions.
.venv/bin/python3 scripts/install_extensions.py

# 3. Index ePubs (~6 min for 541 books).
.venv/bin/python3 scripts/index_books.py

# 4. Generate chapter embeddings (~55 min on Apple-silicon MPS).
.venv/bin/python3 scripts/generate_embeddings.py

# 5. Build FTS and VSS indexes.
.venv/bin/python3 scripts/build_fts_index.py
.venv/bin/python3 scripts/build_vss_index.py

# 6. Declare the property graph (must run on every new connection).
.venv/bin/python3 scripts/build_property_graph.py
```

### Running the tests

```bash
.venv/bin/python3 -m pytest tests/ -v
```

The integration tests embed the test queries live, so the first run downloads
the `all-MiniLM-L6-v2` weights (~90 MB) into the HuggingFace cache.

### v2 layout

```text
.claude/
  skills/kb-usage/           # Claude Code skill: how to use the KB
  skills/skills-factory/     # Claude Code skill: how to run the Skills Factory
  commands/                  # /kb-* slash commands
mcp-servers/kb-mcp/          # FastMCP server: hybrid retrieval + Skills Factory
scripts/                     # Indexing, extensions, embeddings, FTS, VSS, graph
schemas/                     # catalog.sql (target DDL) + property_graph.sql
patterns/                    # YAML pattern library
tests/                       # pytest suite + eval/ seed datasets
logs/                        # Local run logs (gitignored)
launchd/                     # macOS LaunchAgent plists for scheduled refresh
data/                        # catalog.ddb (gitignored) + generated-packages/
```

## Overview

myPub extracts structure and knowledge from your ePub collection (~345 technical books) and makes it accessible through:

- **Natural language queries** - Ask Claude about any topic
- **Concept relationships** - Understand prerequisites and related topics
- **Multiple perspectives** - Compare how different authors treat subjects
- **Pattern libraries** - Reusable building blocks for data systems
- **Skills files** - Domain expertise encoded for Claude

## Architecture

```text
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

   ```text
   You: "Explain Change Data Capture and show me which books cover it"
   ```

## Directory Structure

```text
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
| ------- | ----------- |
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
| ------- | ------------- | --------------- |
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

Apache License 2.0 - See [LICENSE](LICENSE) for details.

## Acknowledgments

Built on knowledge from ~345 technical books covering data engineering, healthcare analytics, AI/ML, and software architecture.
