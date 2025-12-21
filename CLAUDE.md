# myPub Knowledge Base - Claude Code Instructions

## Project Overview

myPub is a Claude-native knowledge base for technical ePub books. It provides:
- **Catalog database** (DuckDB) with book/chapter metadata and concept graph
- **Pattern library** with reusable building blocks
- **Native-first retrieval** - load full chapters, not chunks

## Key Locations

> **Note:** Update these paths for your environment

| Resource | Path |
|----------|------|
| ePub Library | `~/Documents/ebooks/` ← *Change to your ebook location* |
| Catalog Database | `./data/catalog.ddb` (relative to project root) |
| Patterns | `./patterns/` |
| Scripts | `./scripts/` |

## Opening ePub Files

When the user wants to open/view an ePub file directly (not just extract content), use **OmniReader Pro**:

```bash
open -a "OmniReader Pro" "/path/to/book.epub"
```

## Querying the Catalog

Use Python with DuckDB to query the catalog:

```bash
python3 -c "
import duckdb
conn = duckdb.connect('./data/catalog.ddb')  # Run from project root
result = conn.execute('YOUR QUERY HERE').fetchall()
for row in result:
    print(row)
conn.close()
"
```

### Common Queries

**List all books:**
```sql
SELECT book_id, title, authors, chapter_count FROM books ORDER BY title;
```

**Search chapters by topic:**
```sql
SELECT b.title AS book, ch.title AS chapter, ch.chapter_id, ch.href
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%dimensional%'
ORDER BY b.title, ch.sequence;
```

**Get chapter details for ebook-mcp:**
```sql
SELECT ch.chapter_id, ch.title, ch.href, b.filepath
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.chapter_id = 'book-id:chapter-sequence';
```

## Loading Chapter Content

Use the **ebook-mcp** tools to load chapter content:

1. First, query the catalog to get `filepath` and `href`
2. Then use: `ebook-mcp:get_epub_chapter_markdown(epub_path, chapter_id)`

Example workflow:
```python
# 1. Query catalog for chapter info
# Result: filepath="~/Documents/ebooks/book.epub", href="chapter1.xhtml"

# 2. Load via ebook-mcp
ebook-mcp:get_epub_chapter_markdown(
    epub_path="~/Documents/ebooks/book.epub",
    chapter_id="chapter1.xhtml"
)
```

## Pattern Library

Patterns are stored as YAML files in `/patterns/`. Structure:
- `patterns/healthcare/dimensional/` - Healthcare dimensional patterns
- `patterns/healthcare/metrics/` - Healthcare metrics patterns
- `patterns/dimensional-modeling/` - General dimensional patterns
- `patterns/data-engineering/` - Data engineering patterns

Load patterns by reading the YAML files directly.

## Available Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `index_books.py` | Index ePubs into catalog | `python3 scripts/index_books.py --source ~/Documents/ebooks` |
| `extract_concepts.py` | Extract concepts from chapters | `python3 scripts/extract_concepts.py --list` |
| `generate_skill.py` | Generate skill from topic | `python3 scripts/generate_skill.py --topic "Topic"` |

## Response Patterns

### When asked to find content on a topic:

1. Query the catalog for matching chapters
2. Show top results with book/chapter info
3. Offer to load specific chapters for detailed explanation

### When asked to explain a concept:

1. Query catalog for chapters covering the concept
2. Load 1-2 authoritative chapters via ebook-mcp
3. Synthesize explanation citing sources
4. Offer to compare perspectives or show prerequisites

### When asked to build something (e.g., data model):

1. Check pattern library for relevant patterns
2. Load pattern YAML files
3. Apply appropriate variation based on context
4. Generate code from templates
5. Explain design decisions with rationale

### When asked to open/view a book:

Use OmniReader Pro:
```bash
open -a "OmniReader Pro" "~/Documents/ebooks/book-name.epub"
```

## Domain Coverage

The library (~345 books) covers:
- **Data Engineering**: Pipelines, ETL/ELT, CDC, Quality, Streaming
- **Databases**: SQL, NoSQL, DuckDB, Modeling, Optimization
- **AI/ML/LLM**: Machine Learning, Deep Learning, NLP, LLM Development
- **Healthcare**: Claims, Clinical, Analytics, FHIR, Risk Adjustment
- **Cloud/DevOps**: AWS, Azure, GCP, Kubernetes, Terraform
- **Architecture**: Microservices, Event-Driven, DDD
- **Dimensional Modeling**: Kimball, Data Vault, Star Schema

## Quick Reference

```bash
# Count books in catalog
python3 -c "import duckdb; print(duckdb.connect('data/catalog.ddb').execute('SELECT COUNT(*) FROM books').fetchone()[0], 'books')"

# Search for a topic
python3 -c "
import duckdb
conn = duckdb.connect('data/catalog.ddb')
for r in conn.execute(\"\"\"
    SELECT b.title, ch.title 
    FROM chapters ch JOIN books b ON ch.book_id = b.book_id 
    WHERE ch.title ILIKE '%YOUR_TOPIC%' LIMIT 10
\"\"\").fetchall():
    print(f'{r[0]}: {r[1]}')
"

# Open a book in OmniReader Pro
open -a "OmniReader Pro" ~/Documents/ebooks/book-name.epub
```
