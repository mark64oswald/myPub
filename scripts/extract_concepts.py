#!/usr/bin/env python3
"""
extract_concepts.py - Extract concepts from indexed chapters

This script analyzes chapter content and extracts concepts,
then maps concepts to chapters with treatment levels.

Usage:
    python scripts/extract_concepts.py --book "book-id"
    python scripts/extract_concepts.py --all --limit 10
    
This script is designed to be run interactively with Claude
to leverage AI for concept extraction.
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("Missing duckdb. Install with: pip install duckdb")
    sys.exit(1)

DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")


def get_chapters_for_extraction(conn: duckdb.DuckDBPyConnection, 
                                 book_id: str = None, 
                                 limit: int = None) -> list[dict]:
    """Get chapters that need concept extraction."""
    
    query = """
        SELECT 
            ch.chapter_id,
            ch.book_id,
            ch.title AS chapter_title,
            ch.href,
            ch.token_count,
            b.title AS book_title,
            b.filepath
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.key_concepts IS NULL
    """
    
    if book_id:
        query += f" AND ch.book_id = '{book_id}'"
    
    query += " ORDER BY b.title, ch.sequence"
    
    if limit:
        query += f" LIMIT {limit}"
    
    results = conn.execute(query).fetchall()
    columns = ['chapter_id', 'book_id', 'chapter_title', 'href', 
               'token_count', 'book_title', 'filepath']
    
    return [dict(zip(columns, row)) for row in results]


def save_concept(conn: duckdb.DuckDBPyConnection, 
                 concept_id: str,
                 name: str,
                 description: str = None,
                 domain: str = None,
                 aliases: list = None) -> bool:
    """Save or update a concept."""
    
    existing = conn.execute(
        "SELECT concept_id FROM concepts WHERE concept_id = ?",
        [concept_id]
    ).fetchone()
    
    if existing:
        conn.execute("""
            UPDATE concepts 
            SET name = ?, description = ?, domain = ?, aliases = ?, updated_at = CURRENT_TIMESTAMP
            WHERE concept_id = ?
        """, [name, description, domain, aliases or [], concept_id])
    else:
        conn.execute("""
            INSERT INTO concepts (concept_id, name, description, domain, aliases)
            VALUES (?, ?, ?, ?, ?)
        """, [concept_id, name, description, domain, aliases or []])
    
    conn.commit()
    return True


def save_chapter_concept(conn: duckdb.DuckDBPyConnection,
                         chapter_id: str,
                         concept_id: str,
                         treatment: str = 'mention',
                         relevance: float = 1.0) -> bool:
    """Save chapter-concept mapping."""
    
    conn.execute("""
        INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, relevance)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (chapter_id, concept_id) DO UPDATE SET
            treatment = EXCLUDED.treatment,
            relevance = EXCLUDED.relevance
    """, [chapter_id, concept_id, treatment, relevance])
    
    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Extract concepts from indexed chapters"
    )
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--book', '-b', help="Process specific book by ID")
    parser.add_argument('--all', '-a', action='store_true', help="Process all books")
    parser.add_argument('--limit', '-l', type=int, help="Limit chapters to process")
    parser.add_argument('--list', action='store_true', help="List chapters needing extraction")
    
    args = parser.parse_args()
    
    conn = duckdb.connect(args.catalog)
    
    chapters = get_chapters_for_extraction(
        conn, 
        book_id=args.book,
        limit=args.limit
    )
    
    if args.list:
        print(f"Chapters needing concept extraction: {len(chapters)}")
        for ch in chapters[:20]:
            print(f"  {ch['chapter_id']}: {ch['chapter_title']} ({ch['book_title']})")
        if len(chapters) > 20:
            print(f"  ... and {len(chapters) - 20} more")
    else:
        print(f"Found {len(chapters)} chapters for concept extraction.")
        print("Use this script with Claude to extract concepts interactively.")
        print("\nExample workflow:")
        print("  1. Load a chapter via ebook-mcp")
        print("  2. Ask Claude to identify key concepts")
        print("  3. Save concepts using this script's functions")
    
    conn.close()


if __name__ == "__main__":
    main()
