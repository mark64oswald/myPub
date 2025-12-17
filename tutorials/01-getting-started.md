# Tutorial 1: Getting Started with myPub

This tutorial walks you through setting up myPub and running your first queries.

## Prerequisites

- Python 3.10+
- DuckDB CLI (optional but helpful)
- Claude Desktop with MCP support
- ePub files in `~/Documents/ebooks/`

## Step 1: Install Dependencies

```bash
cd ~/Developer/projects/myPub

# Create virtual environment (optional)
python -m venv .venv
source .venv/bin/activate

# Install required packages
pip install duckdb ebooklib beautifulsoup4 tiktoken
```

## Step 2: Initialize the Catalog

```bash
# Create data directory
mkdir -p data

# Initialize the database schema
duckdb data/catalog.ddb < schemas/catalog.sql

# Verify
duckdb data/catalog.ddb "SELECT name FROM sqlite_master WHERE type='table';"
```

Expected output:
```
┌────────────────────────┐
│          name          │
├────────────────────────┤
│ books                  │
│ chapters               │
│ concepts               │
│ concept_relationships  │
│ chapter_concepts       │
│ patterns               │
│ ...                    │
└────────────────────────┘
```

## Step 3: Index Your First Books

Start with a small batch to verify everything works:

```bash
# Index first 5 books (verbose mode)
python scripts/index_books.py --source ~/Documents/ebooks --limit 5 --verbose

# Or index a specific book
python scripts/index_books.py --book "fundamentals-of-data-engineering.epub" --verbose
```

Expected output:
```
Connecting to catalog: /Users/.../data/catalog.ddb
Found 5 ePub files to index
--------------------------------------------------
[1/5] fundamentals-of-data-engineering.epub
  Reading: /Users/.../ebooks/fundamentals-of-data-engineering.epub
  Title: Fundamentals of Data Engineering
  Authors: Joe Reis, Matt Housley
  Chapters: 42
  ✓ Indexed successfully (125000 tokens)
...
```

## Step 4: Verify the Index

```bash
# Check books
duckdb data/catalog.ddb "SELECT book_id, title, array_length(authors) as num_authors, chapter_count FROM books;"

# Check chapters
duckdb data/catalog.ddb "SELECT chapter_id, title, token_count FROM chapters WHERE book_id = 'fundamentals-of-data-engineering' LIMIT 10;"
```

## Step 5: Query the Catalog

Now you can query the knowledge base:

```sql
-- Find books about a topic
SELECT title, authors
FROM books
WHERE title ILIKE '%data%warehouse%'
   OR array_to_string(subjects, ',') ILIKE '%dimensional%';

-- Find chapters about CDC
SELECT b.title, ch.title, ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%cdc%'
   OR ch.title ILIKE '%change data capture%';

-- Get chapter details for loading
SELECT ch.chapter_id, ch.href, b.filepath
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.chapter_id = 'fundamentals-of-data-engineering:7';
```

## Step 6: Load Chapter Content

Using the ebook-mcp in Claude Desktop:

```
You: "Load chapter 7 from Fundamentals of Data Engineering"

Claude will:
1. Query catalog for chapter href and filepath
2. Use get_epub_chapter_markdown(filepath, href)
3. Display the full chapter content
```

## Step 7: Test with Claude

Start a conversation in Claude Desktop:

```
You: "Using my knowledge base, explain how CDC fits into data pipelines"

Claude (with kb-usage skill):
1. Queries catalog for 'cdc' or 'change data capture'
2. Finds relevant chapters
3. Loads chapter content
4. Synthesizes explanation with citations
```

## Troubleshooting

### "No module named 'duckdb'"
```bash
pip install duckdb
```

### "Permission denied" errors
```bash
# Check file permissions
ls -la ~/Documents/ebooks/
chmod 644 ~/Documents/ebooks/*.epub
```

### "No chapters found"
The ePub might have a non-standard TOC. Check:
```bash
python -c "import ebooklib; from ebooklib import epub; b = epub.read_epub('path/to/book.epub'); print(b.toc)"
```

### Database locked
```bash
# Close any open DuckDB connections
# The database file can only be open by one process
```

## Next Steps

1. **Index more books**: Remove the `--limit` flag to index all
2. **Extract concepts**: Run `extract_concepts.py` to build the concept graph
3. **Generate skills**: Use `generate_skill.py` to create domain skills
4. **Explore patterns**: See Tutorial 3 for pattern library usage

---

**Tutorial completed!** You now have a working knowledge base with indexed books.
