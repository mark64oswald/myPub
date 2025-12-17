#!/usr/bin/env python3
"""
extract_concepts.py - Extract concepts from indexed chapters

This script analyzes chapter content and extracts concepts,
building the concept graph with relationships.

Usage:
    python scripts/extract_concepts.py --book "book-id"
    python scripts/extract_concepts.py --all --limit 10
    python scripts/extract_concepts.py --chapter "book-id:1"

Requirements:
    pip install duckdb ebooklib beautifulsoup4
    
Note: This script prepares data for Claude to analyze. 
The actual concept extraction is done conversationally with Claude.
"""

import argparse
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
    try:
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
        
        # Convert to text preserving some structure
        text_parts = []
        for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'pre', 'code']):
            if element.name.startswith('h'):
                level = int(element.name[1])
                text_parts.append('\n' + '#' * level + ' ' + element.get_text(strip=True))
            elif element.name == 'pre' or element.name == 'code':
                text_parts.append('\n```\n' + element.get_text() + '\n```')
            elif element.name == 'li':
                text_parts.append('- ' + element.get_text(strip=True))
            else:
                text_parts.append(element.get_text(strip=True))
        
        return '\n\n'.join(text_parts)
        
    except Exception as e:
        print(f"Error reading chapter: {e}")
        return None


def list_chapters_for_extraction(conn: duckdb.DuckDBPyConnection, 
                                  book_id: str = None,
                                  limit: int = None) -> list:
    """Get chapters that need concept extraction."""
    
    query = """
        SELECT 
            ch.chapter_id,
            ch.title,
            ch.token_count,
            b.title as book_title,
            b.filepath,
            ch.href
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.key_concepts IS NULL
          AND ch.token_count > 500  -- Skip very short chapters
    """
    
    if book_id:
        query += f" AND ch.book_id = '{book_id}'"
    
    query += " ORDER BY b.title, ch.sequence"
    
    if limit:
        query += f" LIMIT {limit}"
    
    return conn.execute(query).fetchall()


def export_chapter_for_analysis(conn: duckdb.DuckDBPyConnection, 
                                 chapter_id: str,
                                 output_dir: str = None) -> dict:
    """Export a chapter's content for Claude analysis."""
    
    # Get chapter info
    result = conn.execute("""
        SELECT 
            ch.chapter_id,
            ch.title,
            ch.href,
            b.title as book_title,
            b.authors,
            b.filepath
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.chapter_id = ?
    """, [chapter_id]).fetchone()
    
    if not result:
        return None
    
    chapter_id, title, href, book_title, authors, filepath = result
    
    # Get content
    content = get_chapter_content(filepath, href)
    
    return {
        'chapter_id': chapter_id,
        'title': title,
        'book_title': book_title,
        'authors': authors,
        'content': content
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract concepts from chapters for analysis"
    )
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--book', '-b', help="Process specific book")
    parser.add_argument('--chapter', help="Process specific chapter")
    parser.add_argument('--list', '-l', action='store_true', 
                       help="List chapters needing extraction")
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--export', '-e', help="Export chapter content to file")
    
    args = parser.parse_args()
    
    conn = duckdb.connect(args.catalog)
    
    if args.list:
        chapters = list_chapters_for_extraction(conn, args.book, args.limit)
        print(f"Chapters needing concept extraction ({len(chapters)}):")
        print("-" * 60)
        for ch in chapters:
            print(f"  {ch[0]}: {ch[1]} ({ch[2]} tokens)")
            print(f"    From: {ch[3]}")
        return
    
    if args.chapter:
        data = export_chapter_for_analysis(conn, args.chapter)
        if data:
            print(f"Chapter: {data['title']}")
            print(f"Book: {data['book_title']}")
            print(f"Authors: {', '.join(data['authors'] or [])}")
            print("-" * 60)
            if args.export:
                Path(args.export).write_text(data['content'] or "No content")
                print(f"Content exported to: {args.export}")
            else:
                print(data['content'][:2000] + "..." if data['content'] else "No content")
        return
    
    print("Use --list to see chapters needing extraction")
    print("Use --chapter <id> to export a specific chapter")
    print("Use --chapter <id> --export <file> to save content")


if __name__ == "__main__":
    main()
