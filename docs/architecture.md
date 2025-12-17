# myPub Architecture

## System Overview

myPub is a Claude-native knowledge base system that transforms a collection of technical ePub books into an intelligent, queryable resource.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Claude Desktop / Claude Code                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐         │
│   │     SKILLS       │  │   MCP SERVERS    │  │  CUSTOM COMMANDS │         │
│   │                  │  │                  │  │                  │         │
│   │  kb-usage/       │  │  DuckDB          │  │  /kb-search      │         │
│   │  domains/        │  │  ebook-mcp       │  │  /kb-compare     │         │
│   │  patterns/       │  │  memory          │  │  /kb-prereqs     │         │
│   │  generated/      │  │  filesystem      │  │  /kb-pattern     │         │
│   │                  │  │                  │  │                  │         │
│   └──────────────────┘  └────────┬─────────┘  └──────────────────┘         │
│                                  │                                          │
└──────────────────────────────────┼──────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
                    ▼                             ▼
         ┌─────────────────────┐      ┌─────────────────────┐
         │   DuckDB Catalog    │      │   ePub Source Files │
         │                     │      │                     │
         │   data/catalog.ddb  │      │   ~/Documents/      │
         │                     │      │   ebooks/*.epub     │
         │   • books           │      │                     │
         │   • chapters        │      │   (Source of truth, │
         │   • concepts        │      │    never modified)  │
         │   • relationships   │      │                     │
         │   • patterns        │      │                     │
         │   • skills          │      │                     │
         └─────────────────────┘      └─────────────────────┘
```

## Design Principles

### 1. Native-First Retrieval

Unlike RAG systems that chunk documents into vectors, myPub preserves the author's intended structure:

- **Full chapters** are loaded into context, not fragments
- **Author organization** is preserved (sections, examples, flow)
- **Token-aware** - most chapters (4K-17K tokens) fit in modern context windows

This approach provides:
- Better coherence in explanations
- Preserved context for code examples
- Author's pedagogical intent intact

### 2. Structure-First Extraction

The indexing process captures structure, not just content:

```
ePub File
    └── Metadata (title, authors, date)
    └── Table of Contents
        └── Chapter 1
            └── href (reference to content)
            └── token_count (size estimate)
            └── key_concepts (extracted)
            └── summary (AI-generated)
```

### 3. Concept Graph (Relational, Not Neo4j)

Concepts and their relationships are stored in DuckDB using standard relational tables:

```sql
concepts                    -- Canonical concepts
concept_relationships       -- Edges: REQUIRES, RELATED_TO, etc.
chapter_concepts           -- Which chapters discuss which concepts
```

This provides:
- SQL you already know
- Single database (no separate graph DB)
- Recursive CTEs for graph traversal

### 4. Pattern Library

Reusable building blocks extracted from books:

```yaml
pattern:
  id: healthcare.dimensional.fct_claim_line
  canonical: [base implementation]
  variations:
    - positional_columns (simple, limited)
    - bridge_table (flexible, complex)
  extensions:
    - hcc_risk_mapping (for Medicare Advantage)
```

## Data Flow

### Indexing (One-time, incremental)

```
ePub Files → index_books.py → DuckDB Catalog
                                  │
                                  ├── books table
                                  └── chapters table
```

### Concept Extraction (Batch or incremental)

```
Chapters → extract_concepts.py → Claude Analysis → DuckDB
                                                       │
                                                       ├── concepts
                                                       ├── concept_relationships
                                                       └── chapter_concepts
```

### Query Flow (Runtime)

```
User Query
    │
    ▼
Claude (with kb-usage skill loaded)
    │
    ├── Query DuckDB: Find relevant concepts/chapters
    │
    ├── Load chapter content via ebook-mcp
    │
    └── Synthesize response with citations
```

## Component Details

### DuckDB Catalog

Single SQLite-like database containing all metadata:

| Table | Purpose |
|-------|---------|
| books | Book metadata (title, authors, filepath) |
| chapters | Chapter metadata (title, href, tokens, summary) |
| concepts | Canonical concept definitions |
| concept_relationships | Edges between concepts |
| chapter_concepts | Many-to-many: chapters ↔ concepts |
| patterns | Reusable building blocks |
| pattern_variations | Alternative approaches |
| pattern_extensions | Additive capabilities |
| skills | Generated skill file tracking |

### Skills System

Skills are markdown files that guide Claude's behavior:

```
skills/
├── kb-usage/SKILL.md          # Core: how to use the KB
├── domains/
│   ├── data-engineering/      # Domain expertise
│   └── healthcare-analytics/
├── patterns/
│   └── healthcare/            # Pattern-specific guidance
└── generated/                 # Ad-hoc generated skills
```

### MCP Servers

| Server | Purpose |
|--------|---------|
| DuckDB | Query catalog database |
| ebook-mcp | Read ePub content |
| memory | Dynamic concept exploration |
| filesystem | Read/write generated files |

## Security & Privacy

- **ePub files**: Read-only, never modified
- **Catalog**: Local database, no external access
- **No cloud dependencies**: Everything runs locally
- **Git-ignored data**: catalog.ddb excluded from repo

## Extensibility

### Adding New Books

```bash
# Copy to ebooks folder, then re-index
python scripts/index_books.py --book "new-book.epub"
```

### Adding New Domains

1. Create domain skill: `skills/domains/new-domain/SKILL.md`
2. Index relevant books
3. Extract concepts with domain tag

### Adding New Patterns

1. Create pattern YAML in `patterns/` directory
2. Register in patterns table
3. Update relevant domain skills
