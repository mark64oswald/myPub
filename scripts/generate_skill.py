#!/usr/bin/env python3
"""
generate_skill.py - Generate skill files from knowledge base content

This script creates SKILL.md files by gathering relevant chapters
and preparing them for Claude to synthesize.

Usage:
    python scripts/generate_skill.py --topic "CDC" --output skills/generated/cdc/
    python scripts/generate_skill.py --concept "change_data_capture" --output skills/generated/cdc/

Note: This prepares the content. Claude does the actual synthesis in conversation.
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime

try:
    import duckdb
except ImportError:
    print("Missing duckdb. Install with: pip install duckdb")
    sys.exit(1)


DEFAULT_CATALOG = os.path.expanduser("~/Developer/projects/myPub/data/catalog.ddb")

SKILL_TEMPLATE = '''# {title} Skill

## Overview

{overview_placeholder}

## Key Concepts

{concepts_placeholder}

## Common Patterns

{patterns_placeholder}

## Best Practices

{practices_placeholder}

## Pitfalls to Avoid

{pitfalls_placeholder}

## Sources

This skill was generated from the following chapters:

{sources_list}

---
*Generated: {timestamp}*
*Topic: {topic}*
'''


def find_relevant_chapters(conn, topic: str, limit: int = 10) -> list:
    """Find chapters relevant to a topic."""
    
    # Search in chapter titles and summaries
    query = """
        SELECT 
            ch.chapter_id,
            ch.title,
            ch.summary,
            ch.token_count,
            b.title as book_title,
            b.authors,
            b.filepath,
            ch.href
        FROM chapters ch
        JOIN books b ON ch.book_id = b.book_id
        WHERE ch.title ILIKE ?
           OR ch.summary ILIKE ?
        ORDER BY ch.token_count DESC
        LIMIT ?
    """
    
    search_term = f'%{topic}%'
    return conn.execute(query, [search_term, search_term, limit]).fetchall()


def find_chapters_by_concept(conn, concept_id: str, limit: int = 10) -> list:
    """Find chapters that cover a specific concept."""
    
    query = """
        SELECT 
            ch.chapter_id,
            ch.title,
            ch.summary,
            ch.token_count,
            b.title as book_title,
            b.authors,
            b.filepath,
            ch.href,
            cc.treatment
        FROM chapter_concepts cc
        JOIN chapters ch ON cc.chapter_id = ch.chapter_id
        JOIN books b ON ch.book_id = b.book_id
        WHERE cc.concept_id = ?
        ORDER BY 
            CASE cc.treatment 
                WHEN 'deep_dive' THEN 1 
                WHEN 'explain' THEN 2 
                ELSE 3 
            END,
            ch.token_count DESC
        LIMIT ?
    """
    
    return conn.execute(query, [concept_id, limit]).fetchall()


def prepare_skill_scaffold(topic: str, chapters: list, output_dir: str):
    """Create a skill scaffold with source references."""
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Build sources list
    sources = []
    for ch in chapters:
        chapter_id = ch[0]
        chapter_title = ch[1]
        book_title = ch[4]
        authors = ch[5]
        author_str = ', '.join(authors) if authors else 'Unknown'
        sources.append(f"- **{book_title}** by {author_str}")
        sources.append(f"  - Chapter: {chapter_title}")
        sources.append(f"  - ID: `{chapter_id}`")
        sources.append("")
    
    # Create skill file
    content = SKILL_TEMPLATE.format(
        title=topic.title(),
        overview_placeholder="[To be synthesized by Claude from source chapters]",
        concepts_placeholder="[To be extracted by Claude]",
        patterns_placeholder="[To be identified by Claude]",
        practices_placeholder="[To be synthesized by Claude]",
        pitfalls_placeholder="[To be identified by Claude]",
        sources_list='\n'.join(sources),
        timestamp=datetime.now().isoformat(),
        topic=topic
    )
    
    skill_file = output_path / "SKILL.md"
    skill_file.write_text(content)
    
    # Create a chapters manifest for Claude
    manifest = {
        'topic': topic,
        'chapters': [
            {
                'chapter_id': ch[0],
                'title': ch[1],
                'book': ch[4],
                'filepath': ch[6],
                'href': ch[7]
            }
            for ch in chapters
        ]
    }
    
    import json
    manifest_file = output_path / "chapters.json"
    manifest_file.write_text(json.dumps(manifest, indent=2))
    
    return skill_file, manifest_file


def main():
    parser = argparse.ArgumentParser(
        description="Generate skill file scaffolds from knowledge base"
    )
    parser.add_argument('--catalog', '-c', default=DEFAULT_CATALOG)
    parser.add_argument('--topic', '-t', help="Topic to search for")
    parser.add_argument('--concept', help="Concept ID to use")
    parser.add_argument('--output', '-o', required=True, help="Output directory")
    parser.add_argument('--limit', '-l', type=int, default=5, 
                       help="Max chapters to include")
    
    args = parser.parse_args()
    
    if not args.topic and not args.concept:
        print("Error: Specify --topic or --concept")
        sys.exit(1)
    
    conn = duckdb.connect(args.catalog)
    
    # Find relevant chapters
    if args.concept:
        chapters = find_chapters_by_concept(conn, args.concept, args.limit)
        topic = args.concept.replace('_', ' ').title()
    else:
        chapters = find_relevant_chapters(conn, args.topic, args.limit)
        topic = args.topic
    
    if not chapters:
        print(f"No chapters found for: {args.topic or args.concept}")
        sys.exit(1)
    
    print(f"Found {len(chapters)} relevant chapters:")
    for ch in chapters:
        print(f"  - {ch[1]} ({ch[4]})")
    
    # Create scaffold
    skill_file, manifest_file = prepare_skill_scaffold(topic, chapters, args.output)
    
    print(f"\nCreated skill scaffold:")
    print(f"  Skill: {skill_file}")
    print(f"  Manifest: {manifest_file}")
    print(f"\nNext: Ask Claude to synthesize the skill from these chapters")


if __name__ == "__main__":
    main()
