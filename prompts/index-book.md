# Super Prompt: Index New Book

## Goal

Index a new ePub book into the myPub catalog, extracting metadata and chapter structure.

## Prerequisites

- ePub file exists in the ebooks directory
- Catalog database initialized
- Python dependencies installed

## Variables

- `{{BOOK_FILENAME}}`: Name of the ePub file (e.g., "new-book.epub")
- `{{EBOOKS_PATH}}`: Path to ebooks directory (default: ~/Documents/ebooks)

## Prompt

````text
I need to index a new ePub book into my myPub knowledge base.

**Book to index:** {{BOOK_FILENAME}}
**Location:** {{EBOOKS_PATH}}

Please:

1. First, verify the book exists and can be read:
   - Use ebook-mcp to get the book metadata
   - Use ebook-mcp to get the table of contents
   - Report: title, authors, chapter count, estimated size

2. Then run the indexing script:
   ```bash

   python ~/Developer/projects/myPub/scripts/index_books.py \
     --source {{EBOOKS_PATH}} \
     --book "{{BOOK_FILENAME}}" \
     --verbose

   ```text

3. Verify the index by querying:
   ```sql

   SELECT book_id, title, authors, chapter_count, total_tokens
   FROM books
   WHERE filepath LIKE '%{{BOOK_FILENAME}}%';

   SELECT chapter_id, title, token_count
   FROM chapters
   WHERE book_id = (SELECT book_id FROM books WHERE filepath LIKE '%{{BOOK_FILENAME}}%')
   ORDER BY sequence;

   ```text

4. Report:
   - Book successfully indexed (yes/no)
   - Number of chapters
   - Total tokens
   - Any issues encountered
   - Suggested next steps (concept extraction, etc.)
````

## Expected Output

- Confirmation of successful indexing
- Book and chapter statistics
- Next steps recommendation

## Follow-up Actions

- Extract concepts: Use `extract-concepts.md` prompt
- Generate skill: If book covers new domain
- Update existing skills: If book adds to covered topics
