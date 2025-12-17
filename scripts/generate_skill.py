#!/usr/bin/env python3
"""
generate_skill.py - Generate a Skill file from knowledge base content

This script queries the catalog for chapters covering a topic and
generates a structured SKILL.md file that Claude can use.

Usage:
    python scripts/generate_skill.py "Change Data Capture" --domain data-engineering
    python scripts/generate_skill.py "HCC Risk Adjustment" --domain healthcare

The script outputs a template that should be reviewed and enhanced
with Claude's help for the actual content synthesis.

Requirements:
    pip install duckdb
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("Missing required package: duckdb")
    print("Install with: pip install duckdb")
    sys.exit(1)


DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")
DEFAULT_OUTPUT = os.path.expanduser("~/Developer/projects/myPub/skills/generated")


def find_relevant_chapters(conn: duckdb.DuckDBPyConnection, 
                           topic: str,
                           domain: str = None,
                           limit: int = 10) -> list:
    """Find chapters relevant to a topic."""
    
    # Search in concepts first
    concept_query = """
        SELECT DISTINCT
            vcc.chapter_id,
            vcc.chapter_title,
            vcc.book_title,
            vcc.authors,
            vcc.treatment,
            vcc.token_count,
            vcc.concept_name
        FROM v_concept_chapters vcc
        WHERE vcc.concept_name ILIKE ?
           OR vcc.concept_name ILIKE ?
    """
    params = [f"%{topic}%", topic.replace(' ', '_')]
    
    if domain:
        concept_query += " AND vcc.domain = ?"
        params.append(domain)
    
    concept_query += " ORDER BY vcc.treatment DESC LIMIT ?"
    params.append(limit)
    
    results = conn.execute(concept_query, params).fetchall()
    
    # If no concept matches, search in chapter titles/summaries
    if not results:
        chapter_query = """
            SELECT DISTINCT
                ch.chapter_id,
                ch.title AS chapter_title,
                b.title AS book_title,
                b.authors,
                'unknown' AS treatment,
                ch.token_count,
                NULL AS concept_name
            FROM chapters ch
            JOIN books b ON ch.book_id = b.book_id
            WHERE ch.title ILIKE ?
               OR ch.summary ILIKE ?
            ORDER BY ch.token_count DESC
            LIMIT ?
        """
        results = conn.execute(chapter_query, [f"%{topic}%", f"%{topic}%", limit]).fetchall()
    
    return results



def generate_skill_template(topic: str, domain: str, chapters: list) -> str:
    """Generate a SKILL.md template."""
    
    slug = topic.lower().replace(' ', '-').replace('_', '-')
    
    # Group chapters by book
    books = {}
    for ch in chapters:
        chapter_id, chapter_title, book_title, authors, treatment, tokens, concept = ch
        if book_title not in books:
            books[book_title] = {'authors': authors, 'chapters': []}
        books[book_title]['chapters'].append({
            'id': chapter_id,
            'title': chapter_title,
            'treatment': treatment,
            'tokens': tokens
        })
    
    template = f"""# {topic} Skill

## Overview

[TODO: Synthesize overview from source chapters]

This skill provides Claude with expertise on {topic}, 
derived from {len(chapters)} chapters across {len(books)} books.

## Key Concepts

[TODO: List and explain key concepts]

- **Concept 1**: Description
- **Concept 2**: Description

## Common Patterns

[TODO: Document common patterns and approaches]

### Pattern 1: [Name]

**When to use:** [Context]

**Implementation:**
```sql
-- Example code
```

## Best Practices

[TODO: Synthesize best practices from sources]

1. Practice 1
2. Practice 2

## Common Pitfalls

[TODO: Document common mistakes and how to avoid them]

- Pitfall 1: How to avoid
- Pitfall 2: How to avoid

## Source Chapters

The following chapters were used to create this skill:

"""
    
    for book_title, book_info in books.items():
        authors = book_info['authors']
        author_str = ', '.join(authors) if authors else 'Unknown'
        template += f"\n### {book_title}\n*by {author_str}*\n\n"
        
        for ch in book_info['chapters']:
            treatment_badge = {
                'deep_dive': '🔬',
                'explain': '📖',
                'mention': '📌',
                'unknown': '📄'
            }.get(ch['treatment'], '📄')
            
            tokens_str = f"~{ch['tokens']} tokens" if ch['tokens'] else "unknown size"
            template += f"- {treatment_badge} **{ch['title']}** ({tokens_str})\n"
            template += f"  - Chapter ID: `{ch['id']}`\n"
    
    template += f"""

## Usage Notes

To use this skill effectively:

1. Load this skill when working on {topic.lower()} tasks
2. Query the catalog for specific chapter content when needed
3. Reference source chapters for detailed explanations

## Metadata

- **Generated:** {datetime.now().isoformat()}
- **Domain:** {domain or 'general'}
- **Topic:** {topic}
- **Source chapters:** {len(chapters)}
- **Source books:** {len(books)}
"""
    
    return template


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Skill file from knowledge base content"
    )
    parser.add_argument('topic', help="Topic to generate skill for")
    parser.add_argument('--domain', '-d', help="Domain to filter by")
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--output', '-o', default=DEFAULT_OUTPUT)
    parser.add_argument('--limit', '-l', type=int, default=10)
    
    args = parser.parse_args()
    
    conn = duckdb.connect(args.catalog)
    
    print(f"Searching for chapters on: {args.topic}")
    chapters = find_relevant_chapters(conn, args.topic, args.domain, args.limit)
    
    if not chapters:
        print("No relevant chapters found.")
        print("Try a different search term or check if books are indexed.")
        sys.exit(1)
    
    print(f"Found {len(chapters)} relevant chapters")
    
    # Generate skill template
    template = generate_skill_template(args.topic, args.domain, chapters)
    
    # Create output directory
    slug = args.topic.lower().replace(' ', '-').replace('_', '-')
    output_dir = Path(args.output) / slug
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / "SKILL.md"
    output_file.write_text(template)
    
    print(f"\nSkill template saved to: {output_file}")
    print("\nNext steps:")
    print("1. Load the source chapters with Claude")
    print("2. Ask Claude to synthesize the [TODO] sections")
    print("3. Review and refine the generated content")
    
    conn.close()


if __name__ == "__main__":
    main()
