# myPub Quickstart Guide

Get your knowledge base up and running in 15 minutes.

## Prerequisites

1. **Python 3.9+** with pip
2. **ePub collection** (your technical books)
3. **Claude Desktop** or Claude Code

## Installation

### 1. Clone the Repository

```bash
cd ~/Developer/projects
git clone https://github.com/YOUR_USERNAME/myPub.git
cd myPub
```

### 2. Install Python Dependencies

```bash
pip install duckdb ebooklib beautifulsoup4 tiktoken
```

### 3. Set Up Your ePub Directory

By default, scripts look for ePubs in `~/Documents/ebooks/`. Either:
- Move/copy your ePubs there, OR
- Use `--source` flag to specify your location

### 4. Initialize the Database

```bash
mkdir -p data
duckdb data/catalog.ddb < schemas/catalog.sql
```

### 5. Index Your First Books

Start with a few books to test:

```bash
python scripts/index_books.py --source ~/Documents/ebooks --limit 10 --verbose
```

### 6. Verify Setup

```bash
duckdb data/catalog.ddb -c "SELECT title, chapter_count FROM books LIMIT 5;"
```

## First Queries

### In DuckDB CLI

```bash
duckdb data/catalog.ddb
```

```sql
-- Find all books
SELECT title, authors, chapter_count FROM books ORDER BY title;

-- Search chapters
SELECT b.title, ch.title 
FROM chapters ch 
JOIN books b ON ch.book_id = b.book_id 
WHERE ch.title ILIKE '%dimension%';
```

### With Claude

Load the kb-usage skill and ask:

```
Using my knowledge base, explain dimensional modeling and show me 
which books cover it.
```

## Next Steps

1. **Index all books:** Remove the `--limit` flag
2. **Extract concepts:** Follow Phase 2 super prompt
3. **Generate skills:** Follow Phase 3 super prompt
4. **Build patterns:** Follow Phase 4 super prompt

## Quick Reference

| Task | Command |
|------|---------|
| Index books | `python scripts/index_books.py --source PATH` |
| Query catalog | `duckdb data/catalog.ddb` |
| Generate skill | `python scripts/generate_skill.py --topic "TOPIC"` |

## Troubleshooting

**"No module named duckdb"**
```bash
pip install duckdb
```

**"ePub file not found"**
- Check the filepath in the error
- Verify the file exists and has .epub extension

**"No chapters found"**
- Some ePubs have non-standard TOC formats
- Try opening in a reader to verify structure

## Getting Help

- Check `docs/` for detailed guides
- Review `tutorials/` for phase-by-phase instructions
- Open an issue on GitHub
