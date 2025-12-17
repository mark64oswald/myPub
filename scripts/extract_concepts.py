#!/usr/bin/env python3
"""
extract_concepts.py - Extract concepts from indexed chapters

This script uses Claude to analyze chapter content and extract:
- Key concepts discussed
- Concept relationships (prerequisites, related topics)
- Treatment level (mention, explain, deep_dive)

Usage:
    python scripts/extract_concepts.py --book "book-id"
    python scripts/extract_concepts.py --chapter "book-id:sequence"
    python scripts/extract_concepts.py --all --limit 10

Note: This script is designed to be run interactively with Claude
assisting in the concept extraction. It prepares the data and 
prompts for Claude to process.

Requirements:
    pip install duckdb ebooklib beautifulsoup4
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import duckdb
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing required package: {e}")
    print("Install with: pip install duckdb ebooklib beautifulsoup4")
    sys.exit(1)


DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")


def get_chapter_content(filepath: str, href: str) -> str:
    """Extract chapter content as markdown-like text."""
    book = epub.read_epub(filepath)
    
    # Handle href with anchors
    base_href = href.split('#')[0] if href else None
    
    if not base_href:
        return None
    
    item = book.get_item_with_href(base_href)
    if not item:
        return None
    
    soup = BeautifulSoup(item.get_content(), 'html.parser')
    
    # Remove unwanted elements
    for element in soup(['script', 'style', 'nav']):
        element.decompose()
    
    # Convert to text with some structure preserved
    text_parts = []
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'pre', 'code']):
        if element.name.startswith('h'):
            level = int(element.name[1])
            text_parts.append(f"\n{'#' * level} {element.get_text(strip=True)}\n")
        elif element.name == 'li':
            text_parts.append(f"- {element.get_text(strip=True)}")
        elif element.name in ['pre', 'code']:
            text_parts.append(f"```\n{element.get_text()}\n```")
        else:
            text_parts.append(element.get_text(strip=True))
    
    return '\n'.join(text_parts)



def generate_extraction_prompt(chapter_title: str, book_title: str, content: str) -> str:
    """Generate a prompt for Claude to extract concepts from chapter content."""
    
    return f"""Analyze this chapter and extract concepts.

**Book:** {book_title}
**Chapter:** {chapter_title}

**Content:**
{content[:15000]}  # Truncate if very long

---

Please extract:

1. **Key Concepts** - List each distinct concept discussed in this chapter
   - concept_name: The canonical name for this concept
   - treatment: How deeply is it covered? (mention, explain, deep_dive)
   - excerpt: A brief quote or summary showing how it's discussed

2. **Concept Relationships** - For concepts that have clear relationships
   - source_concept → target_concept: relationship_type
   - Relationship types: REQUIRES (prerequisite), RELATED_TO, EXTENDS, CONTRASTS_WITH

3. **Chapter Metadata**
   - content_type: tutorial, reference, conceptual, case_study
   - difficulty: beginner, intermediate, advanced
   - summary: 2-3 sentence summary of the chapter

Output as JSON:
```json
{{
  "concepts": [
    {{"name": "...", "treatment": "...", "excerpt": "..."}}
  ],
  "relationships": [
    {{"source": "...", "target": "...", "type": "...", "notes": "..."}}
  ],
  "metadata": {{
    "content_type": "...",
    "difficulty": "...",
    "summary": "..."
  }}
}}
```
"""


def prepare_chapters_for_extraction(conn: duckdb.DuckDBPyConnection, 
                                     book_id: str = None,
                                     chapter_id: str = None,
                                     limit: int = None) -> list[dict]:
    """Get chapters that need concept extraction."""
    
    query = """
        SELECT 
            ch.chapter_id,
            ch.title AS chapter_title,
            ch.href,
            ch.token_count,
            b.title AS book_title,
            b.filepath
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.href IS NOT NULL
          AND ch.key_concepts IS NULL
    """
    
    params = []
    if book_id:
        query += " AND ch.book_id = ?"
        params.append(book_id)
    if chapter_id:
        query += " AND ch.chapter_id = ?"
        params.append(chapter_id)
    
    query += " ORDER BY b.title, ch.sequence"
    
    if limit:
        query += f" LIMIT {limit}"
    
    return conn.execute(query, params).fetchall()


def save_extraction_results(conn: duckdb.DuckDBPyConnection,
                            chapter_id: str,
                            results: dict) -> None:
    """Save extracted concepts to the database."""
    
    # Update chapter metadata
    metadata = results.get('metadata', {})
    key_concepts = [c['name'] for c in results.get('concepts', [])]
    
    conn.execute("""
        UPDATE chapters 
        SET key_concepts = ?,
            content_type = ?,
            difficulty = ?,
            summary = ?
        WHERE chapter_id = ?
    """, [
        key_concepts,
        metadata.get('content_type'),
        metadata.get('difficulty'),
        metadata.get('summary'),
        chapter_id
    ])
    
    # Insert concepts (if not exists)
    for concept in results.get('concepts', []):
        concept_id = concept['name'].lower().replace(' ', '_').replace('-', '_')
        
        # Check if concept exists
        existing = conn.execute(
            "SELECT concept_id FROM concepts WHERE concept_id = ?",
            [concept_id]
        ).fetchone()
        
        if not existing:
            conn.execute("""
                INSERT INTO concepts (concept_id, name, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, [concept_id, concept['name']])
        
        # Insert chapter-concept mapping
        conn.execute("""
            INSERT OR REPLACE INTO chapter_concepts 
            (chapter_id, concept_id, treatment, excerpt)
            VALUES (?, ?, ?, ?)
        """, [chapter_id, concept_id, concept.get('treatment'), concept.get('excerpt')])
    
    # Insert relationships
    for rel in results.get('relationships', []):
        source_id = rel['source'].lower().replace(' ', '_').replace('-', '_')
        target_id = rel['target'].lower().replace(' ', '_').replace('-', '_')
        
        conn.execute("""
            INSERT OR IGNORE INTO concept_relationships 
            (source_id, target_id, relationship, source_ref, notes, created_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, [source_id, target_id, rel['type'], chapter_id, rel.get('notes')])
    
    conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Extract concepts from indexed chapters"
    )
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--book', '-b', help="Process specific book by book_id")
    parser.add_argument('--chapter', help="Process specific chapter by chapter_id")
    parser.add_argument('--limit', '-l', type=int, help="Limit chapters to process")
    parser.add_argument('--output', '-o', help="Output prompts to file instead of stdout")
    
    args = parser.parse_args()
    
    conn = duckdb.connect(args.catalog)
    
    chapters = prepare_chapters_for_extraction(
        conn, 
        book_id=args.book,
        chapter_id=args.chapter,
        limit=args.limit
    )
    
    print(f"Found {len(chapters)} chapters needing concept extraction")
    
    for chapter in chapters:
        chapter_id, chapter_title, href, token_count, book_title, filepath = chapter
        
        print(f"\n{'='*60}")
        print(f"Chapter: {chapter_title}")
        print(f"Book: {book_title}")
        print(f"Tokens: {token_count}")
        print(f"{'='*60}")
        
        content = get_chapter_content(filepath, href)
        if content:
            prompt = generate_extraction_prompt(chapter_title, book_title, content)
            
            if args.output:
                with open(args.output, 'a') as f:
                    f.write(f"\n\n{'='*60}\n")
                    f.write(f"CHAPTER_ID: {chapter_id}\n")
                    f.write(f"{'='*60}\n")
                    f.write(prompt)
            else:
                print(prompt)
        else:
            print("  Could not extract content")
    
    conn.close()


if __name__ == "__main__":
    main()
