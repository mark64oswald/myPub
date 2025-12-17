# myPub Architecture

## System Overview

myPub is a Claude-native knowledge base that makes your technical ePub collection queryable and actionable. Unlike traditional RAG systems that chunk documents, myPub preserves author structure and enables native-first retrieval.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           User Interaction Layer                             │
│                    Claude Desktop / Claude Code / API                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐         │
│  │     Skills      │    │   MCP Servers   │    │    Commands     │         │
│  │                 │    │                 │    │                 │         │
│  │ • kb-usage      │    │ • DuckDB        │    │ • /kb-search    │         │
│  │ • domains/*     │    │ • ebook-mcp     │    │ • /kb-compare   │         │
│  │ • patterns/*    │    │ • memory        │    │ • /kb-prereqs   │         │
│  │ • generated/*   │    │ • filesystem    │    │ • /kb-pattern   │         │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘         │
│                                                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                            Data Layer                                        │
│                                                                              │
│  ┌─────────────────────────────┐    ┌─────────────────────────────┐        │
│  │     DuckDB Catalog          │    │     ePub Source Files       │        │
│  │     data/catalog.ddb        │    │     ~/Documents/ebooks/     │        │
│  │                             │    │                             │        │
│  │  • books (metadata)         │    │  • Original files           │        │
│  │  • chapters (TOC + meta)    │    │  • Source of truth          │        │
│  │  • concepts (graph nodes)   │    │  • Never modified           │        │
│  │  • relationships (edges)    │    │                             │        │
│  │  • patterns (library)       │    │                             │        │
│  │  • skills (tracking)        │    │                             │        │
│  └─────────────────────────────┘    └─────────────────────────────┘        │
│                                                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                          Generated Artifacts                                 │
│                                                                              │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐         │
│  │  Skills Files   │    │  Pattern YAML   │    │    Exports      │         │
│  │  skills/*.md    │    │  patterns/*.yml │    │  exports/*.csv  │         │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘         │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Design Principles

### 1. Native-First Retrieval

Traditional RAG chunks documents into small pieces and retrieves by vector similarity. This loses context and structure.

**myPub approach:**
- Index metadata and structure, not content
- Load full chapters into Claude's context (4K-17K tokens typical)
- Preserve author's organization and flow
- Let Claude work with complete thoughts

### 2. Structure Preservation

ePub books have intentional structure: chapters, sections, hierarchies. This structure carries meaning.

**myPub approach:**
- Extract and preserve table of contents
- Track chapter parent-child relationships
- Maintain sequence and hierarchy
- Enable structural navigation

### 3. Concept Graph (Not Vector Store)

Concepts relate to each other in meaningful ways that vectors don't capture.

**myPub approach:**
- Store concepts as graph nodes
- Define typed relationships (REQUIRES, EXTENDS, CONTRASTS_WITH)
- Enable prerequisite discovery
- Support learning path generation

### 4. Pattern Library

Knowledge should be actionable, not just retrievable.

**myPub approach:**
- Extract reusable patterns from books
- Store with variations and extensions
- Include decision frameworks
- Generate code from patterns

## Data Flow

### Indexing Flow

```
ePub File → Read Metadata → Parse TOC → Extract Chapters → Store in DuckDB
                ↓                            ↓
           books table              chapters table
```

### Concept Extraction Flow

```
Chapter Content → Claude Analysis → Identify Concepts → Store Relationships
                                          ↓
                              concepts + chapter_concepts tables
```

### Query Flow

```
User Question → Skill Loaded → Query Catalog → Find Chapters → Load Content → Synthesize Response
                    ↓               ↓                              ↓
             kb-usage skill    DuckDB query         ebook-mcp:get_chapter
```

### Pattern Usage Flow

```
Build Request → Find Pattern → Load Variations → Apply Decision Framework → Generate Code
                    ↓               ↓                     ↓
            patterns table   pattern_variations    Context analysis
```

## Component Details

### DuckDB Catalog

**Why DuckDB:**
- SQL you already know
- Embedded (no server)
- Fast analytical queries
- Array support for authors, subjects, aliases
- Recursive CTEs for graph traversal

**Key Tables:**
- `books` - Book metadata
- `chapters` - TOC with summaries and key concepts
- `concepts` - Canonical concept definitions
- `concept_relationships` - Graph edges
- `chapter_concepts` - What chapters discuss what concepts
- `patterns` - Pattern library
- `pattern_variations` - Alternative approaches
- `skills` - Generated skill tracking

### Skills Files

Skills encode domain expertise for Claude:

```
skills/
├── kb-usage/SKILL.md          # How to use the KB
├── domains/                    # Domain expertise
│   ├── data-engineering/
│   ├── healthcare-analytics/
│   └── dimensional-modeling/
├── patterns/                   # Pattern-focused skills
│   ├── healthcare/
│   └── dimensional/
└── generated/                  # Ad-hoc generated
```

### Pattern Library

Patterns are stored as YAML with structure:

```yaml
pattern_id: healthcare.dimensional.fct_claim_line
name: Healthcare Claim Line Fact
domain: healthcare
category: dimensional
problem_statement: Model healthcare claims at the service line level
schema:
  columns: [...]
  keys: [...]
variations:
  - name: bridge_table_diagnosis
    when_to_use: "Need flexible 'any diagnosis contains' queries"
extensions:
  - name: hcc_risk_mapping
    when_required: "Medicare Advantage analysis"
```

## Integration Points

### Claude Desktop/Code

- Skills loaded from `skills/` directory
- MCP servers provide tool access
- Custom commands for common workflows

### MCP Servers Used

| Server | Purpose |
|--------|---------|
| DuckDB (healthsim-duckdb) | Catalog queries |
| ebook-mcp | Chapter content retrieval |
| memory | Dynamic concept exploration |
| filesystem | File operations |

### External Data Sources

The architecture supports adding:
- Web search for current information
- Enterprise databases via MCP
- Personal data sources
- API integrations
