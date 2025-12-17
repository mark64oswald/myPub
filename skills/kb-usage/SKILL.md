# Knowledge Base Usage Skill

## Overview

This skill guides Claude in using the myPub technical ebook knowledge base containing ~345 books on data engineering, healthcare analytics, AI/ML, and software architecture.

## Architecture

The knowledge base consists of:

1. **DuckDB Catalog** (`~/Developer/projects/myPub/data/catalog.ddb`)
   - Books and chapters metadata
   - Concept graph (concepts, relationships)
   - Pattern library
   - Skills tracking

2. **ePub Source Files** (`~/Documents/ebooks/*.epub`)
   - Original books (source of truth)
   - Never modified, only read

3. **Generated Artifacts** (`~/Developer/projects/myPub/`)
   - Skills files
   - Pattern YAML files
   - Documentation

## Available Tools

### DuckDB Queries (healthsim-duckdb:query or similar)

Query the catalog database for discovery and navigation:

```sql
-- Connect to the catalog
-- Database path: ~/Developer/projects/myPub/data/catalog.ddb
```

### ePub Reader (ebook-mcp)

Retrieve full chapter content:

- `get_epub_toc(path)` - Get book structure
- `get_epub_chapter_markdown(path, chapter_id)` - Get full chapter content
- `get_epub_metadata(path)` - Get book metadata

### Memory (memory MCP)

For dynamic concept exploration during conversations:

- Track concepts discussed
- Build temporary relationship maps



## Query Strategies

### Finding Content for a Topic

```sql
-- Step 1: Find matching concepts
SELECT concept_id, name, description, domain, aliases
FROM concepts
WHERE name ILIKE '%{topic}%' 
   OR '{topic}' = ANY(aliases)
   OR description ILIKE '%{topic}%';

-- Step 2: Find chapters that cover it well
SELECT 
    book_title,
    chapter_title,
    treatment,
    token_count,
    summary,
    chapter_id
FROM v_concept_chapters
WHERE concept_id = '{found_concept_id}'
ORDER BY 
    CASE treatment 
        WHEN 'deep_dive' THEN 1 
        WHEN 'explain' THEN 2 
        WHEN 'mention' THEN 3 
    END,
    pub_date DESC
LIMIT 10;

-- Step 3: Get book filepath for ePub retrieval
SELECT filepath, href 
FROM v_chapters_with_books 
WHERE chapter_id = '{selected_chapter_id}';
```

Then load the chapter via ebook-mcp:get_epub_chapter_markdown.

### Comparing Author Perspectives

When user wants multiple viewpoints on a topic:

```sql
SELECT DISTINCT
    authors[1] AS primary_author,
    book_title,
    chapter_title,
    pub_date,
    treatment,
    summary
FROM v_concept_chapters
WHERE concept_id = '{concept_id}'
  AND treatment IN ('explain', 'deep_dive')
ORDER BY pub_date DESC;
```

Load 2-3 chapters from different authors and synthesize perspectives.

### Finding Prerequisites

```sql
-- Direct prerequisites
SELECT prereq_name, strength
FROM v_concept_prerequisites
WHERE concept_id = '{concept_id}';

-- Recursive prerequisites (up to 3 levels)
WITH RECURSIVE prereq_chain AS (
    SELECT target_id AS concept_id, 1 AS depth
    FROM concept_relationships
    WHERE source_id = '{concept_id}' AND relationship = 'REQUIRES'
    
    UNION ALL
    
    SELECT cr.target_id, pc.depth + 1
    FROM concept_relationships cr
    JOIN prereq_chain pc ON cr.source_id = pc.concept_id
    WHERE cr.relationship = 'REQUIRES' AND pc.depth < 3
)
SELECT c.name, MIN(pc.depth) AS depth
FROM prereq_chain pc
JOIN concepts c ON pc.concept_id = c.concept_id
GROUP BY c.name
ORDER BY depth;
```

### Getting Patterns

```sql
-- Find relevant patterns
SELECT pattern_id, name, description, domain, category
FROM patterns
WHERE domain = '{domain}' 
  AND (category = '{category}' OR '{category}' IS NULL);

-- Get full pattern with variations
SELECT 
    p.pattern_id,
    p.name,
    p.canonical_yaml,
    p.problem_statement
FROM patterns p
WHERE p.pattern_id = '{pattern_id}';

-- Get variations
SELECT variation_id, name, when_to_use, variation_yaml
FROM pattern_variations
WHERE pattern_id = '{pattern_id}';

-- Get extensions
SELECT extension_id, name, when_required, extension_yaml
FROM pattern_extensions
WHERE pattern_id = '{pattern_id}';
```



## Response Patterns

### For Learning Requests

**User asks to explain a topic:**

1. Query catalog for the concept and relevant chapters
2. Select 1-2 top chapters (prefer 'deep_dive' or 'explain' treatment)
3. Load full chapter content via ebook-mcp (native-first retrieval)
4. Synthesize explanation in your own words
5. Cite sources: "According to [Book Title], Chapter N..."
6. Offer to explore further: "I found N other chapters on this topic from different authors. Want me to compare perspectives?"

**Example response structure:**
```
[Explanation synthesized from chapter content]

**Source:** [Book Title] by [Author], Chapter N: [Chapter Title]

I also found coverage of this topic in:
- [Book 2] - [treatment level]
- [Book 3] - [treatment level]

Would you like me to:
- Compare how different authors approach this?
- Show the prerequisites for this topic?
- Go deeper into [specific subtopic]?
```

### For Building Requests

**User asks to build/create something:**

1. Identify relevant patterns from the pattern library
2. Load pattern YAML with variations and extensions
3. Apply decision framework based on user context
4. Select appropriate variation
5. Generate code/schemas using pattern templates
6. Explain choices with rationale

**Example response structure:**
```
I'll use the [pattern_name] pattern for this. Here's my approach:

**Pattern Selected:** [pattern_id]
**Variation:** [variation_name] (because [rationale])
**Extensions Applied:** [extension_names] (required for [reason])

[Generated code/schema]

**Design Decisions:**
- [Decision 1]: [Rationale based on pattern guidance]
- [Decision 2]: [Rationale]

**Sources:** Based on patterns from [Book Title] Chapter N
```

### For Research Requests

**User asks to research/compare/analyze:**

1. Find all chapters discussing the topic(s)
2. Group by author/perspective/methodology
3. Load multiple chapters for comparison
4. Identify agreements, differences, and conflicts
5. Synthesize with citations
6. Note knowledge gaps if relevant

**Example response structure:**
```
I found [N] chapters covering [topic] across [M] books. Here's my analysis:

**Perspectives:**

*[Author 1] ([Book 1]):*
[Summary of their approach]

*[Author 2] ([Book 2]):*
[Summary of their approach]

**Key Agreements:**
- [Point 1]
- [Point 2]

**Key Differences:**
- [Author 1] emphasizes X, while [Author 2] focuses on Y

**Recommendation:** For your context, I'd suggest [approach] because [reason].
```

### For Skill Generation Requests

**User asks to generate a skill file:**

1. Find all relevant chapters for the topic
2. Load top 3-5 chapters
3. Synthesize into structured SKILL.md format
4. Include: overview, key concepts, patterns, pitfalls, sources
5. Save to skills/generated/ directory
6. Update skills table in catalog



## Key Principles

### Native-First Retrieval
- Load **full chapters**, not chunks
- Most chapters are 4K-17K tokens (fits in context)
- Preserve author's structure and flow
- Only summarize when chapter is too large

### Source Traceability
- Always cite book, author, and chapter
- Provide enough detail for user to find source
- Note when synthesizing from multiple sources

### Multi-Perspective Awareness
- Different authors have different approaches
- Methodological differences (Kimball vs Inmon, etc.) are valid
- Present alternatives when relevant
- Help user choose based on their context

### Pattern-Informed Building
- Use patterns for consistency and quality
- Apply decision frameworks to select variations
- Explain rationale for choices
- Extend patterns as needed for specific requirements

### Concept-Aware Navigation
- Use concept relationships for discovery
- Show prerequisites when helpful
- Suggest related topics
- Build learning paths when requested

## Domain Coverage

The knowledge base covers approximately:

| Domain | Books | Key Topics |
|--------|-------|------------|
| Data Engineering | ~80 | Pipelines, ETL/ELT, Data Quality, Streaming |
| Databases | ~50 | SQL, NoSQL, Data Modeling, Optimization |
| AI/ML/LLM | ~60 | Machine Learning, Deep Learning, NLP, LLMs |
| Healthcare | ~30 | Claims, Clinical, Analytics, FHIR |
| Cloud/DevOps | ~40 | AWS, Azure, GCP, Kubernetes, CI/CD |
| Architecture | ~35 | Microservices, Event-Driven, DDD |
| Programming | ~50 | Python, SQL, Spark, Scala |

## Workflow Examples

### Example 1: Learning Query
```
User: "Explain CDC and how it fits into data pipelines"

Claude:
1. Query concepts for 'cdc' or 'change data capture'
2. Find chapters with deep_dive treatment
3. Load "Fundamentals of Data Engineering" Chapter 7
4. Synthesize explanation with pipeline context
5. Offer Kafka perspective as alternative view
```

### Example 2: Building Query
```
User: "Build a dimensional model for healthcare claims"

Claude:
1. Query patterns: healthcare.dimensional.*
2. Load fct_claim_line pattern with variations
3. Check for HCC extension if Medicare context
4. Select bridge_table variation if flexible diagnosis queries needed
5. Generate DDL using pattern template
6. Explain design decisions
```

### Example 3: Research Query
```
User: "Compare Kimball vs Inmon for my data warehouse"

Claude:
1. Find chapters on both methodologies
2. Load "Deciphering Data Architectures" comparison chapter
3. Load canonical chapters from each methodology
4. Synthesize comparison
5. Ask about user context for recommendation
```

## Database Location

The catalog database should be at:
```
~/Developer/projects/myPub/data/catalog.ddb
```

If it doesn't exist, guide user to initialize:
```bash
cd ~/Developer/projects/myPub
duckdb data/catalog.ddb < schemas/catalog.sql
```

## Error Handling

- If concept not found: Try aliases, broader search, suggest alternatives
- If chapter too large: Summarize key sections, offer to focus on specifics
- If pattern not found: Check for similar patterns, offer to create
- If no results: Suggest using web search as supplement
