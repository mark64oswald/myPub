# Super Prompt: Phase 1 - Initial Setup and Book Indexing

## Context

You are helping set up and populate the myPub knowledge base. This is Phase 1 of 5, focused on:
- Initializing the DuckDB catalog
- Indexing ePub books
- Validating the setup

## Prerequisites

- Python 3.9+ installed
- Required packages: `pip install duckdb ebooklib beautifulsoup4 tiktoken`
- ePub collection at `~/Documents/ebooks/` (or specified path)
- Project cloned to `~/Developer/projects/myPub/`

## Step 1: Initialize the Database

```bash
cd ~/Developer/projects/myPub

# Create data directory if needed
mkdir -p data

# Initialize the database with schema
duckdb data/catalog.ddb < schemas/catalog.sql
```

Verify with:
```bash
duckdb data/catalog.ddb -c "SELECT name FROM sqlite_master WHERE type='table';"
```

Expected tables: books, chapters, concepts, concept_relationships, chapter_concepts, patterns, pattern_variations, pattern_extensions, skills

## Step 2: Index Books

### Option A: Index a few books for testing
```bash
python scripts/index_books.py --source ~/Documents/ebooks --limit 10 --verbose
```

### Option B: Index a specific book
```bash
python scripts/index_books.py --source ~/Documents/ebooks --book "data-warehouse-toolkit.epub" --verbose
```

### Option C: Index all books
```bash
python scripts/index_books.py --source ~/Documents/ebooks --verbose
```

## Step 3: Validate Indexing

Run these queries to verify:

```sql
-- Check book count
SELECT COUNT(*) AS book_count FROM books;

-- Check chapter count
SELECT COUNT(*) AS chapter_count FROM chapters;

-- See sample books
SELECT book_id, title, authors, chapter_count 
FROM books 
ORDER BY indexed_at DESC 
LIMIT 5;

-- See sample chapters with token counts
SELECT 
    b.title AS book,
    ch.title AS chapter,
    ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.token_count IS NOT NULL
ORDER BY ch.token_count DESC
LIMIT 10;

-- Check for books without chapters (indexing issues)
SELECT b.book_id, b.title
FROM books b
LEFT JOIN chapters ch ON b.book_id = ch.book_id
WHERE ch.chapter_id IS NULL;
```

## Step 4: Test Chapter Retrieval

Use the ebook-mcp to load a chapter:

1. Get chapter details from catalog:
```sql
SELECT ch.chapter_id, ch.title, ch.href, b.filepath
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE b.book_id = '{some_book_id}'
LIMIT 1;
```

2. Load via ebook-mcp:
```
ebook-mcp:get_epub_chapter_markdown(filepath, href)
```

3. Verify content loads correctly

## Step 5: Record Statistics

After indexing, record these metrics:

```sql
-- Summary statistics
SELECT 
    COUNT(DISTINCT book_id) AS books,
    COUNT(*) AS chapters,
    SUM(token_count) AS total_tokens,
    AVG(token_count) AS avg_chapter_tokens,
    MAX(token_count) AS max_chapter_tokens
FROM chapters;

-- By estimated reading time
SELECT 
    CASE 
        WHEN token_count < 2000 THEN 'Short (<2K tokens)'
        WHEN token_count < 8000 THEN 'Medium (2-8K tokens)'
        WHEN token_count < 16000 THEN 'Long (8-16K tokens)'
        ELSE 'Very Long (>16K tokens)'
    END AS chapter_size,
    COUNT(*) AS count
FROM chapters
WHERE token_count IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

## Troubleshooting

### "No ePub files found"
- Check the source path exists
- Verify files have .epub extension
- Check file permissions

### "Error reading ePub"
- Some ePubs may be DRM-protected
- Some may have malformed structure
- The script continues on errors, check output for details

### "No chapters extracted"
- The book may have non-standard TOC
- Check if book opens in a reader
- May need manual entry

## Success Criteria for Phase 1

- [ ] Database initialized with all tables
- [ ] At least 10 books indexed successfully
- [ ] Chapters have token counts
- [ ] Can retrieve chapter content via ebook-mcp
- [ ] No critical errors in indexing log

## Next Phase

After Phase 1 is complete, proceed to Phase 2: Concept Extraction.

Load the Phase 2 super prompt: `tutorials/super-prompt-phase-2.md`
