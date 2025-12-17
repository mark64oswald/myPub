#!/usr/bin/env python3
"""
generate_skill.py - Generate a Skill file from chapters

This script creates a SKILL.md file by gathering relevant chapters
on a topic and providing them to Claude for synthesis.

Usage:
    python scripts/generate_skill.py --topic "HCC Risk Adjustment" --output skills/generated/hcc/
    
The actual skill content generation is done by Claude interactively.
This script sets up the structure and gathers source material.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("Missing duckdb. Install with: pip install duckdb")
    sys.exit(1)

DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")
DEFAULT_OUTPUT = os.path.expanduser("~/Developer/projects/myPub/skills/generated")


def find_relevant_chapters(conn: duckdb.DuckDBPyConnection, 
                           topic: str,
                           limit: int = 10) -> list[dict]:
    """Find chapters relevant to a topic."""
    
    # Search in chapter titles, summaries, and key concepts
    query = """
        SELECT 
            ch.chapter_id,
            ch.title AS chapter_title,
            ch.href,
            ch.token_count,
            ch.summary,
            ch.key_concepts,
            b.book_id,
            b.title AS book_title,
            b.authors,
            b.filepath
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.title ILIKE ?
           OR ch.summary ILIKE ?
           OR array_to_string(ch.key_concepts, ',') ILIKE ?
        ORDER BY 
            CASE WHEN ch.title ILIKE ? THEN 1 ELSE 2 END,
            b.pub_date DESC
        LIMIT ?
    """
    
    search_pattern = f"%{topic}%"
    results = conn.execute(query, [
        search_pattern, search_pattern, search_pattern, 
        search_pattern, limit
    ]).fetchall()
    
    columns = ['chapter_id', 'chapter_title', 'href', 'token_count', 
               'summary', 'key_concepts', 'book_id', 'book_title', 
               'authors', 'filepath']
    
    return [dict(zip(columns, row)) for row in results]


def create_skill_scaffold(topic: str, output_dir: str, chapters: list[dict]) -> str:
    """Create the initial SKILL.md scaffold."""
    
    skill_id = topic.lower().replace(' ', '-').replace('_', '-')
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    skill_file = output_path / "SKILL.md"
    
    content = f"""# {topic} Skill

## Overview

[Claude: Generate a 2-3 paragraph overview of {topic} based on the source chapters]

## Key Concepts

[Claude: Extract and explain the key concepts]

## Common Patterns

[Claude: Identify common patterns and approaches]

## Best Practices

[Claude: Summarize best practices from the sources]

## Pitfalls to Avoid

[Claude: Note common mistakes and how to avoid them]

## Implementation Guidance

[Claude: Provide practical implementation guidance]

## Source Material

This skill was generated from the following chapters:

"""
    
    for ch in chapters:
        authors = ', '.join(ch['authors']) if ch['authors'] else 'Unknown'
        content += f"- **{ch['book_title']}** by {authors}\n"
        content += f"  - Chapter: {ch['chapter_title']}\n"
        content += f"  - Chapter ID: `{ch['chapter_id']}`\n\n"
    
    content += f"""
## Generation Info

- Generated: {datetime.now().isoformat()}
- Topic: {topic}
- Skill ID: {skill_id}

---
*This skill file should be reviewed and edited after generation.*
"""
    
    skill_file.write_text(content)
    return str(skill_file)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Skill file from chapters on a topic"
    )
    parser.add_argument('--topic', '-t', required=True, help="Topic for the skill")
    parser.add_argument('--output', '-o', default=DEFAULT_OUTPUT, help="Output directory")
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--limit', '-l', type=int, default=10, help="Max chapters to include")
    
    args = parser.parse_args()
    
    conn = duckdb.connect(args.catalog)
    
    print(f"Finding chapters for topic: {args.topic}")
    chapters = find_relevant_chapters(conn, args.topic, args.limit)
    
    if not chapters:
        print("No relevant chapters found. Try a different topic or index more books.")
        conn.close()
        sys.exit(1)
    
    print(f"Found {len(chapters)} relevant chapters:")
    for ch in chapters:
        print(f"  - {ch['chapter_title']} ({ch['book_title']})")
    
    # Create output directory based on topic
    skill_dir = Path(args.output) / args.topic.lower().replace(' ', '-')
    skill_file = create_skill_scaffold(args.topic, str(skill_dir), chapters)
    
    print(f"\nSkill scaffold created: {skill_file}")
    print("\nNext steps:")
    print("1. Load the relevant chapters in Claude")
    print("2. Ask Claude to fill in the [Claude: ...] sections")
    print("3. Review and edit the generated content")
    
    conn.close()


if __name__ == "__main__":
    main()
