# myPub Architecture

## Overview

myPub is a Claude-native knowledge base that transforms a collection of technical ePub books into an intelligent, queryable resource for learning and building.

## Design Principles

### 1. Native-First Retrieval

Unlike traditional RAG systems that chunk documents into vectors, myPub preserves the author's intended structure:

- **Full chapters** are loaded into context, not fragments
- **Author organization** is preserved (sections, examples, flow)
- **Semantic integrity** is maintained

Most technical chapters are 4K-17K tokens, which fits well within Claude's context window.

### 2. Structure-Preserved Indexing

The catalog captures structure without modifying source files:

```
ePub File (Source of Truth)
    ↓
Metadata Extraction
    ↓
DuckDB Catalog (Index)
    ├── books (metadata)
    ├── chapters (TOC + summaries)
    ├── concepts (graph nodes)
    └── patterns (reusable building blocks)
```

### 3. Graph-Based Discovery

Concepts form a knowledge graph enabling:

- **Prerequisite chains**: What do I need to know first?
- **Related topics**: What else should I explore?
- **Multi-perspective**: How do different authors treat this?

### 4. Pattern-Informed Building

Patterns extracted from books provide:

- **Canonical implementations**: The standard approach
- **Documented variations**: Alternative valid approaches
- **Decision frameworks**: When to use which approach

## Component Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         User Interaction Layer                           │
│                                                                          │
│   Claude Desktop          Claude Code           Custom Commands          │
│   (Conversations)         (Agentic coding)      (/kb-search, etc.)      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           Skills Layer                                   │
│                                                                          │
│   kb-usage/SKILL.md      domains/*/SKILL.md    patterns/*/SKILL.md      │
│   (How to use KB)        (Domain expertise)    (Pattern guidance)       │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            MCP Layer                                     │
│                                                                          │
│   DuckDB MCP             ebook-mcp              memory MCP               │
│   (Catalog queries)      (Chapter content)      (Session state)         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           Data Layer                                     │
│                                                                          │
│   catalog.ddb            ~/Documents/ebooks/    patterns/*.yaml          │
│   (Metadata + Graph)     (Source ePubs)         (Pattern definitions)   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Data Model

### Core Entities

```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│    books    │──────<│  chapters   │>──────│  concepts   │
└─────────────┘       └─────────────┘       └─────────────┘
                            │                      │
                            │                      │
                            ▼                      ▼
                      ┌───────────┐         ┌───────────────┐
                      │ patterns  │         │ relationships │
                      └───────────┘         └───────────────┘
```

### Relationship Types

| Relationship | Meaning | Example |
|--------------|---------|---------|
| REQUIRES | Prerequisite knowledge | Dimensional Modeling → SQL |
| RELATED_TO | Associated concept | CDC → Event Sourcing |
| EXTENDS | Builds upon | SCD Type 2 → SCD Type 1 |
| CONTRASTS_WITH | Alternative approach | Kimball → Inmon |

## Workflow Patterns

### Learning Workflow

```
User Question
    │
    ▼
Query Concepts ──────────────────────────┐
    │                                    │
    ▼                                    ▼
Find Chapters ◄─────────────────── No Match? Search books/chapters
    │
    ▼
Load Full Chapter (native-first)
    │
    ▼
Synthesize Explanation
    │
    ▼
Cite Sources + Offer Exploration
```

### Building Workflow

```
Build Request
    │
    ▼
Identify Patterns
    │
    ▼
Load Pattern + Variations
    │
    ▼
Apply Decision Framework
    │
    ▼
Select Variation + Extensions
    │
    ▼
Generate Code/Schema
    │
    ▼
Explain Rationale
```

### Research Workflow

```
Research Question
    │
    ▼
Find All Relevant Chapters
    │
    ▼
Group by Author/Perspective
    │
    ▼
Load Multiple Chapters
    │
    ▼
Identify Agreements/Conflicts
    │
    ▼
Synthesize with Citations
```

## File Locations

| Component | Location |
|-----------|----------|
| Project Root | `~/Developer/projects/myPub/` |
| Catalog Database | `~/Developer/projects/myPub/data/catalog.ddb` |
| Skills Files | `~/Developer/projects/myPub/skills/` |
| Pattern YAML | `~/Developer/projects/myPub/patterns/` |
| Scripts | `~/Developer/projects/myPub/scripts/` |
| ePub Source | `~/Documents/ebooks/` (configurable) |

## Extension Points

### Adding New Domains

1. Create skill file: `skills/domains/{domain}/SKILL.md`
2. Add domain to concepts table
3. Map chapters to concepts
4. Extract domain patterns

### Adding New Patterns

1. Create pattern YAML in `patterns/{domain}/{category}/`
2. Insert into patterns table
3. Link to source chapters
4. Document variations and extensions

### Integrating External Data

1. Add MCP server for data source
2. Create skill for integration guidance
3. Reference in kb-usage skill
