# Getting Started with myPub

This tutorial walks you through setting up and using myPub for the first time.

## Prerequisites

- Python 3.10+
- Claude Desktop or Claude Code
- Your ePub book collection (in `~/Documents/ebooks/` or similar)
- DuckDB CLI (optional but helpful)

## Step 1: Install Dependencies

```bash
cd ~/Developer/projects/myPub

# Create virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install required packages
pip install duckdb ebooklib beautifulsoup4 tiktoken
```

## Step 2: Initialize the Catalog Database

```bash
# Create the data directory
mkdir -p data

# Initialize the schema
duckdb data/catalog.ddb < schemas/catalog.sql

# Verify it was created
duckdb data/catalog.ddb "SELECT name FROM sqlite_master WHERE type='table';"
```

Expected output:
```
┌──────────────────────┐
│         name         │
│       varchar        │
├──────────────────────┤
│ books                │
│ chapters             │
│ concepts             │
│ concept_relationships│
│ chapter_concepts     │
│ patterns             │
│ ...                  │
└──────────────────────┘
```

## Step 3: Index Your First Books

```bash
# Index a few books to start (adjust path to your ebooks)
python scripts/index_books.py \
    --source ~/Documents/ebooks \
    --limit 10 \
    --verbose
```

Example output:
```
Connecting to catalog: ~/Developer/projects/myPub/data/catalog.ddb
Found 345 ePub files to index (limiting to 10)
--------------------------------------------------
[1/10] fundamentals-of-data-engineering.epub
  Title: Fundamentals of Data Engineering
  Authors: Joe Reis, Matt Housley
  Chapters: 42
  ✓ Indexed successfully (156000 tokens)
...
```

## Step 4: Extract Concepts

```bash
# Extract concepts from indexed books
python scripts/extract_concepts.py --all --verbose
```

This will:
- Scan chapter content for known concept keywords
- Create concept records in the catalog
- Link concepts to chapters with treatment levels



## Step 5: Configure Claude Desktop

Add the kb-usage skill to your Claude Desktop configuration.

### Option A: Copy to Skills Directory

```bash
# Find your Claude skills directory and copy
cp -r skills/kb-usage ~/.claude/skills/
```

### Option B: Reference in Project

Create a Claude project that references the skills directory.

## Step 6: Verify the Setup

### Check the Catalog

```bash
# Open DuckDB and run some queries
duckdb data/catalog.ddb
```

```sql
-- How many books indexed?
SELECT COUNT(*) AS book_count FROM books;

-- Books by domain coverage
SELECT 
    UNNEST(subjects) AS subject,
    COUNT(*) AS book_count
FROM books
WHERE subjects IS NOT NULL
GROUP BY subject
ORDER BY book_count DESC
LIMIT 10;

-- Chapters with most concepts
SELECT 
    b.title AS book,
    ch.title AS chapter,
    array_length(ch.key_concepts) AS concept_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.key_concepts IS NOT NULL
ORDER BY concept_count DESC
LIMIT 10;

-- Concepts by domain
SELECT domain, COUNT(*) AS concept_count
FROM concepts
GROUP BY domain
ORDER BY concept_count DESC;
```

### Test with Claude

Open Claude Desktop and try these queries:

1. **Basic search:**
   ```
   What books in my collection cover Change Data Capture?
   ```

2. **Learning request:**
   ```
   Explain dimensional modeling based on my knowledge base
   ```

3. **Comparison:**
   ```
   Compare how different authors approach data warehouse architecture
   ```

## Step 7: Generate Your First Skill

```bash
# Generate a skill for a topic you're interested in
python scripts/generate_skill.py \
    --topic "Data Pipeline Patterns" \
    --verbose
```

This creates a template skill at `skills/generated/data-pipeline-patterns/SKILL.md`
that you can enhance with Claude.

## Next Steps

### Enrich Concepts with Claude

Use Claude to improve concept extraction and add relationships:

```
Review the concepts in my knowledge base for "data engineering" domain.
Suggest missing concepts and relationships between them.
```

### Create Patterns

Identify patterns in your books and create pattern files:

```
Find the dimensional modeling patterns described in my Kimball books.
Create pattern YAML files for the key patterns.
```

### Build a Domain Skill

Create a comprehensive skill for your primary domain:

```
Generate a detailed skill file for healthcare analytics using all
relevant chapters from my knowledge base.
```

## Troubleshooting

### "No such table" errors

Make sure you initialized the schema:
```bash
duckdb data/catalog.ddb < schemas/catalog.sql
```

### Books not indexing

- Check the ePub files are valid
- Verify the source path is correct
- Look for error messages in verbose output

### Concepts not linking

The keyword-based extraction is basic. For better results:
- Use Claude to enrich concepts after initial extraction
- Manually add important concepts and relationships

### Claude not finding content

- Verify the catalog has data: `duckdb data/catalog.ddb "SELECT COUNT(*) FROM chapters"`
- Check that the kb-usage skill is loaded
- Ensure DuckDB MCP server is configured

## Quick Reference

| Task | Command |
|------|---------|
| Index all books | `python scripts/index_books.py --source ~/Documents/ebooks` |
| Index one book | `python scripts/index_books.py --book specific.epub` |
| Extract concepts | `python scripts/extract_concepts.py --all` |
| Generate skill | `python scripts/generate_skill.py --topic "Topic"` |
| Check catalog | `duckdb data/catalog.ddb` |
