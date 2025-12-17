# Getting Started with myPub

This tutorial walks you through setting up and using the myPub knowledge base.

## Prerequisites

- Python 3.9+
- DuckDB CLI or Python package
- Claude Desktop or Claude Code
- Your ePub collection in `~/Documents/ebooks/`

## Step 1: Install Dependencies

```bash
# Create virtual environment (optional but recommended)
cd ~/Developer/projects/myPub
python3 -m venv venv
source venv/bin/activate

# Install required packages
pip install duckdb ebooklib beautifulsoup4 tiktoken
```

## Step 2: Initialize the Catalog Database

```bash
# Create the data directory
mkdir -p data

# Initialize the database with schema
duckdb data/catalog.ddb < schemas/catalog.sql

# Verify it worked
duckdb data/catalog.ddb -c "SELECT name FROM sqlite_master WHERE type='table';"
```

You should see tables: `books`, `chapters`, `concepts`, etc.

## Step 3: Index Your First Books

Start with a small batch to verify everything works:

```bash
# Index first 10 books
python scripts/index_books.py --source ~/Documents/ebooks --limit 10 --verbose

# Or index a specific book
python scripts/index_books.py --book "data-warehouse-toolkit.epub" --verbose
```

Check the results:

```bash
duckdb data/catalog.ddb -c "
SELECT title, chapter_count, total_tokens 
FROM books 
LIMIT 5;
"
```

## Step 4: Explore the Catalog

### List indexed books

```sql
SELECT 
    title,
    authors[1] as primary_author,
    chapter_count,
    total_tokens
FROM books
ORDER BY title;
```

### Find chapters by topic

```sql
SELECT 
    b.title as book,
    ch.title as chapter,
    ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%dimensional%'
ORDER BY ch.token_count DESC;
```

## Step 5: Add Skills to Claude

### Option A: Claude Desktop Projects

1. Create a new Project in Claude Desktop
2. Add the skills folder path to project settings
3. The kb-usage skill will guide Claude

### Option B: Reference in Conversation

Tell Claude:
```
I have a knowledge base of technical ePubs. The catalog is at 
~/Developer/projects/myPub/data/catalog.ddb. Please read the 
skill file at ~/Developer/projects/myPub/skills/kb-usage/SKILL.md
to understand how to use it.
```

## Step 6: Your First Query

Try asking Claude:

> "Search my knowledge base for chapters about dimensional modeling 
> and show me the top 5 by token count."

Claude should:
1. Query the DuckDB catalog
2. Return matching chapters
3. Offer to load and explain content

## Step 7: Add Concepts (Optional but Recommended)

### Seed basic concepts

```sql
-- Add some foundational concepts
INSERT INTO concepts (concept_id, name, domain, description) VALUES
('dimensional_modeling', 'Dimensional Modeling', 'data_engineering', 
 'A data modeling technique optimized for query and analysis'),
('star_schema', 'Star Schema', 'data_engineering',
 'A dimensional model with a central fact table surrounded by dimensions'),
('fact_table', 'Fact Table', 'data_engineering',
 'A table containing measurements/metrics at a specific grain'),
('dimension_table', 'Dimension Table', 'data_engineering',
 'A table containing descriptive attributes for analysis');

-- Add relationships
INSERT INTO concept_relationships (source_id, target_id, relationship) VALUES
('star_schema', 'dimensional_modeling', 'REQUIRES'),
('star_schema', 'fact_table', 'REQUIRES'),
('star_schema', 'dimension_table', 'REQUIRES');
```

### Map chapters to concepts

```sql
-- Example: Map Kimball chapters to concepts
INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, relevance) VALUES
('data-warehouse-toolkit:3', 'dimensional_modeling', 'deep_dive', 0.95),
('data-warehouse-toolkit:4', 'fact_table', 'deep_dive', 0.90),
('data-warehouse-toolkit:5', 'dimension_table', 'deep_dive', 0.90);
```

## Step 8: Ask Learning Questions

Now you can ask Claude:

> "Explain dimensional modeling using my knowledge base. 
> What are the prerequisites I should understand first?"

Claude will:
1. Find the concept and related chapters
2. Load relevant chapter content
3. Synthesize an explanation
4. Show prerequisites from the concept graph

## Step 9: Index More Books

Once satisfied, index your full collection:

```bash
# Index all books (may take a while)
python scripts/index_books.py --source ~/Documents/ebooks --verbose

# Check progress
duckdb data/catalog.ddb -c "SELECT COUNT(*) as books, SUM(chapter_count) as chapters FROM books;"
```

## Next Steps

1. **Build the concept graph**: Work with Claude to extract concepts from chapters
2. **Create domain skills**: Generate skills for specific domains
3. **Extract patterns**: Identify reusable patterns in your books
4. **Customize commands**: Add custom commands for frequent workflows

## Troubleshooting

### "No such table: books"

Run the schema initialization:
```bash
duckdb data/catalog.ddb < schemas/catalog.sql
```

### "ebooklib not found"

Install dependencies:
```bash
pip install ebooklib beautifulsoup4
```

### "Permission denied reading ePub"

Check file permissions:
```bash
chmod 644 ~/Documents/ebooks/*.epub
```

### "Chapter content empty"

Some ePubs have unusual structure. Check:
```bash
python -c "
import ebooklib
from ebooklib import epub
book = epub.read_epub('path/to/book.epub')
print([item.get_name() for item in book.get_items()])
"
```

## Quick Reference

### Key Paths

| What | Where |
|------|-------|
| Project | `~/Developer/projects/myPub/` |
| Catalog DB | `~/Developer/projects/myPub/data/catalog.ddb` |
| Skills | `~/Developer/projects/myPub/skills/` |
| ePub Source | `~/Documents/ebooks/` |

### Key Commands

```bash
# Index books
python scripts/index_books.py --source ~/Documents/ebooks

# Query catalog
duckdb data/catalog.ddb

# Generate skill scaffold
python scripts/generate_skill.py --topic "CDC" --output skills/generated/cdc/
```

### Key Claude Queries

- "Search my KB for [topic]"
- "Explain [concept] from my books"
- "Compare how different authors treat [topic]"
- "What are the prerequisites for [concept]?"
- "Generate a skill file for [topic]"
