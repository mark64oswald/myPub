#!/usr/bin/env python3
"""
index_books.py - Index ePub books into the myPub catalog

This script reads ePub files from the source directory and populates
the DuckDB catalog database with book and chapter metadata.

Usage:
    python scripts/index_books.py --source ~/Documents/ebooks --limit 10
    python scripts/index_books.py --source ~/Documents/ebooks --book "specific-book.epub"
    python scripts/index_books.py --source ~/Documents/ebooks  # Index all

Requirements:
    pip install duckdb ebooklib beautifulsoup4 tiktoken
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import duckdb
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import tiktoken
except ImportError as e:
    print(f"Missing required package: {e}")
    print("Install with: pip install duckdb ebooklib beautifulsoup4 tiktoken")
    sys.exit(1)


# Configuration
DEFAULT_SOURCE = os.path.expanduser("~/Documents/ebooks")
DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text[:100]  # Limit length


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken."""
    try:
        encoder = tiktoken.get_encoding(model)
        return len(encoder.encode(text))
    except Exception:
        # Fallback: rough estimate
        return len(text) // 4


def extract_text_from_html(html_content: bytes) -> str:
    """Extract plain text from HTML content."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Remove script and style elements
    for element in soup(['script', 'style', 'nav', 'header', 'footer']):
        element.decompose()
    
    return soup.get_text(separator='\n', strip=True)



def get_book_metadata(book: epub.EpubBook, filepath: str) -> dict:
    """Extract metadata from an ePub book."""
    
    # Get title
    title = book.get_metadata('DC', 'title')
    title = title[0][0] if title else Path(filepath).stem
    
    # Get authors
    creators = book.get_metadata('DC', 'creator')
    authors = [c[0] for c in creators] if creators else []
    
    # Get publisher
    publisher = book.get_metadata('DC', 'publisher')
    publisher = publisher[0][0] if publisher else None
    
    # Get publication date
    date = book.get_metadata('DC', 'date')
    pub_date = None
    if date:
        try:
            date_str = date[0][0]
            # Handle various date formats
            for fmt in ['%Y-%m-%d', '%Y-%m', '%Y']:
                try:
                    pub_date = datetime.strptime(date_str[:len(fmt.replace('%', '').replace('-', ''))+ fmt.count('-')], fmt).date()
                    break
                except ValueError:
                    continue
        except Exception:
            pass
    
    # Get description
    description = book.get_metadata('DC', 'description')
    description = description[0][0] if description else None
    
    # Get subjects
    subjects = book.get_metadata('DC', 'subject')
    subjects = [s[0] for s in subjects] if subjects else []
    
    # Generate book_id from filename
    book_id = slugify(Path(filepath).stem)
    
    return {
        'book_id': book_id,
        'title': title,
        'authors': authors,
        'publisher': publisher,
        'pub_date': pub_date,
        'filepath': filepath,
        'description': description,
        'subjects': subjects
    }


def get_chapters(book: epub.EpubBook, book_id: str) -> list[dict]:
    """Extract chapter information from an ePub book."""
    chapters = []
    
    # Get table of contents
    toc = book.toc
    
    def process_toc_item(item, sequence: int, parent_id: str = None) -> int:
        """Process a TOC item (could be a link or a section with children)."""
        nonlocal chapters
        
        if isinstance(item, tuple):
            # Section with children: (Section, [children])
            section, children = item
            section_id = f"{book_id}:{sequence}"
            
            chapters.append({
                'chapter_id': section_id,
                'book_id': book_id,
                'title': section.title if hasattr(section, 'title') else str(section),
                'sequence': sequence,
                'href': section.href if hasattr(section, 'href') else None,
                'parent_id': parent_id,
                'token_count': None,  # Will be calculated if content available
            })
            sequence += 1
            
            for child in children:
                sequence = process_toc_item(child, sequence, section_id)
                
        elif isinstance(item, epub.Link):
            # Direct link to content
            chapter_id = f"{book_id}:{sequence}"
            
            # Try to get content and count tokens
            token_count = None
            try:
                content_item = book.get_item_with_href(item.href.split('#')[0])
                if content_item:
                    text = extract_text_from_html(content_item.get_content())
                    token_count = count_tokens(text)
            except Exception:
                pass
            
            chapters.append({
                'chapter_id': chapter_id,
                'book_id': book_id,
                'title': item.title,
                'sequence': sequence,
                'href': item.href,
                'parent_id': parent_id,
                'token_count': token_count,
            })
            sequence += 1
            
        return sequence
    
    sequence = 1
    for item in toc:
        sequence = process_toc_item(item, sequence)
    
    return chapters



def index_book(conn: duckdb.DuckDBPyConnection, filepath: str, verbose: bool = False) -> bool:
    """Index a single ePub book into the catalog."""
    
    try:
        if verbose:
            print(f"  Reading: {filepath}")
        
        book = epub.read_epub(filepath)
        
        # Extract metadata
        metadata = get_book_metadata(book, filepath)
        
        if verbose:
            print(f"  Title: {metadata['title']}")
            print(f"  Authors: {', '.join(metadata['authors'])}")
        
        # Check if book already exists
        existing = conn.execute(
            "SELECT book_id FROM books WHERE book_id = ?", 
            [metadata['book_id']]
        ).fetchone()
        
        if existing:
            if verbose:
                print(f"  Already indexed, updating...")
            conn.execute("DELETE FROM chapters WHERE book_id = ?", [metadata['book_id']])
            conn.execute("DELETE FROM books WHERE book_id = ?", [metadata['book_id']])
        
        # Extract chapters
        chapters = get_chapters(book, metadata['book_id'])
        
        if verbose:
            print(f"  Chapters: {len(chapters)}")
        
        # Calculate total tokens
        total_tokens = sum(c['token_count'] or 0 for c in chapters)
        
        # Insert book
        conn.execute("""
            INSERT INTO books (book_id, title, authors, publisher, pub_date, 
                             filepath, description, subjects, total_tokens, 
                             chapter_count, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, [
            metadata['book_id'],
            metadata['title'],
            metadata['authors'],
            metadata['publisher'],
            metadata['pub_date'],
            metadata['filepath'],
            metadata['description'],
            metadata['subjects'],
            total_tokens,
            len(chapters)
        ])
        
        # Insert chapters
        for chapter in chapters:
            conn.execute("""
                INSERT INTO chapters (chapter_id, book_id, title, sequence, 
                                    href, parent_id, token_count, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                chapter['chapter_id'],
                chapter['book_id'],
                chapter['title'],
                chapter['sequence'],
                chapter['href'],
                chapter['parent_id'],
                chapter['token_count']
            ])
        
        if verbose:
            print(f"  ✓ Indexed successfully ({total_tokens} tokens)")
        
        return True
        
    except Exception as e:
        print(f"  ✗ Error indexing {filepath}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Index ePub books into the myPub catalog"
    )
    parser.add_argument(
        '--source', '-s',
        default=DEFAULT_SOURCE,
        help=f"Source directory containing ePub files (default: {DEFAULT_SOURCE})"
    )
    parser.add_argument(
        '--catalog', '-c',
        default=DEFAULT_CATALOG,
        help=f"Path to catalog database (default: {DEFAULT_CATALOG})"
    )
    parser.add_argument(
        '--limit', '-l',
        type=int,
        default=None,
        help="Limit number of books to index (for testing)"
    )
    parser.add_argument(
        '--book', '-b',
        default=None,
        help="Index a specific book by filename"
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help="Show detailed progress"
    )
    
    args = parser.parse_args()
    
    # Ensure catalog directory exists
    catalog_dir = Path(args.catalog).parent
    catalog_dir.mkdir(parents=True, exist_ok=True)
    
    # Connect to database
    print(f"Connecting to catalog: {args.catalog}")
    conn = duckdb.connect(args.catalog)
    
    # Initialize schema if needed
    schema_file = Path(__file__).parent.parent / 'schemas' / 'catalog.sql'
    if schema_file.exists():
        print(f"Initializing schema from {schema_file}")
        conn.execute(schema_file.read_text())
    
    # Find ePub files
    source_path = Path(args.source)
    if not source_path.exists():
        print(f"Error: Source directory not found: {args.source}")
        sys.exit(1)
    
    if args.book:
        epub_files = [source_path / args.book]
        if not epub_files[0].exists():
            print(f"Error: Book not found: {epub_files[0]}")
            sys.exit(1)
    else:
        epub_files = sorted(source_path.glob("*.epub"))
    
    if args.limit:
        epub_files = epub_files[:args.limit]
    
    print(f"Found {len(epub_files)} ePub files to index")
    print("-" * 50)
    
    # Index each book
    success_count = 0
    for i, filepath in enumerate(epub_files, 1):
        print(f"[{i}/{len(epub_files)}] {filepath.name}")
        if index_book(conn, str(filepath), args.verbose):
            success_count += 1
    
    # Commit and close
    conn.commit()
    conn.close()
    
    print("-" * 50)
    print(f"Indexing complete: {success_count}/{len(epub_files)} books indexed")
    print(f"Catalog saved to: {args.catalog}")


if __name__ == "__main__":
    main()
