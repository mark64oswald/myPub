# Phase 1: Foundation Setup

## Objective

Set up the catalog database and index initial books to validate the system.

## Prerequisites

- Python 3.9+ installed
- ePub collection in `~/Documents/ebooks/`
- DuckDB CLI or Python package

## Steps

### 1. Install Dependencies

```bash
cd ~/Developer/projects/myPub
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Initialize Catalog Database

```bash
mkdir -p data
duckdb data/catalog.ddb < schemas/catalog.sql
```

Verify tables exist:

```bash
duckdb data/catalog.ddb -c "SELECT name FROM sqlite_master WHERE type='table';"
```

Expected output:

```text
books
chapters
concepts
concept_relationships
chapter_concepts
patterns
pattern_sources
pattern_variations
pattern_extensions
skills
```

### 3. Index Test Batch (10 books)

```bash
python scripts/index_books.py --source ~/Documents/ebooks --limit 10 --verbose
```

Check results:

```sql
-- In duckdb
SELECT title, chapter_count, total_tokens FROM books;
SELECT COUNT(*) as chapters FROM chapters;
```

### 4. Test Basic Queries

```sql
-- Find books by topic
SELECT title, authors FROM books
WHERE title ILIKE '%data%warehouse%';

-- Find chapters
SELECT b.title, ch.title, ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%dimension%'
ORDER BY ch.token_count DESC;
```

### 5. Configure Claude Desktop

Add to your Claude Desktop project or tell Claude:

> "I have a knowledge base at ~/Developer/projects/myPub.
> The catalog is data/catalog.ddb. Please read the skill at
> skills/kb-usage/SKILL.md to understand how to use it."

### 6. Test Claude Integration

Ask Claude:

> "Search my knowledge base for chapters about dimensional modeling
> and show me the top 5 results."

Claude should:

1. Query the DuckDB catalog
2. Return matching chapters
3. Offer to load content

## Validation Checklist

- [ ] Dependencies installed successfully
- [ ] Catalog database created with all tables
- [ ] 10 books indexed with chapter counts
- [ ] Basic SQL queries return expected results
- [ ] Claude can query the catalog
- [ ] Claude offers to load chapter content

## Troubleshooting

### "duckdb: command not found"

```bash
pip install duckdb
# Use python instead:
python -c "import duckdb; duckdb.connect('data/catalog.ddb').execute(open('schemas/catalog.sql').read())"
```

### "ebooklib" import errors

```bash
pip install ebooklib beautifulsoup4 lxml
```

### Claude can't find database

Ensure you're using absolute paths:

```text
~/Developer/projects/myPub/data/catalog.ddb
```

## Next Phase

Proceed to [Phase 2: Concept Extraction](./PHASE_2_CONCEPTS.md) once validation is complete.
