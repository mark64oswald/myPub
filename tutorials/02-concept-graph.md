# Tutorial 2: Building the Concept Graph

This tutorial shows how to extract concepts from your indexed chapters and build the knowledge graph.

## Overview

The concept graph enables:
- **Prerequisites**: "What do I need to learn first?"
- **Related topics**: "What else should I explore?"
- **Learning paths**: "What order should I read?"
- **Multi-perspective**: "How do different authors treat this?"

## Step 1: Understand the Model

```
┌─────────────┐         ┌─────────────────────────┐
│  concepts   │◄────────│  chapter_concepts       │
│             │         │  (many-to-many)         │
│  • name     │         │  • treatment level      │
│  • domain   │         │  • relevance score      │
│  • aliases  │         └───────────┬─────────────┘
└──────┬──────┘                     │
       │                            │
       ▼                            ▼
┌──────────────────┐         ┌─────────────┐
│ concept_         │         │  chapters   │
│ relationships    │         │             │
│                  │         │  • title    │
│ • REQUIRES       │         │  • summary  │
│ • RELATED_TO     │         │  • tokens   │
│ • EXTENDS        │         └─────────────┘
│ • CONTRASTS_WITH │
└──────────────────┘
```

## Step 2: Prepare Chapters for Extraction

Identify chapters that haven't been processed yet:

```sql
-- Find chapters without concepts
SELECT 
    ch.chapter_id,
    ch.title,
    b.title AS book,
    ch.token_count
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.key_concepts IS NULL
  AND ch.token_count > 1000  -- Skip tiny chapters
ORDER BY b.title, ch.sequence
LIMIT 20;
```

## Step 3: Extract Concepts (Claude-Assisted)

The extraction is semi-automated - the script prepares prompts, Claude does the analysis:

```bash
# Generate extraction prompts for a book
python scripts/extract_concepts.py --book "fundamentals-of-data-engineering" --limit 5

# Or output to a file for batch processing
python scripts/extract_concepts.py --book "fundamentals-of-data-engineering" --output extractions.txt
```

### Manual Extraction with Claude

Load a chapter and ask Claude to extract concepts:

```
You: "Load chapter 7 of Fundamentals of Data Engineering and extract the key concepts"

Claude will:
1. Load the chapter content
2. Identify concepts discussed
3. Determine treatment level (mention, explain, deep_dive)
4. Identify relationships between concepts
5. Return structured JSON
```

### Expected Output Format

```json
{
  "concepts": [
    {
      "name": "Change Data Capture",
      "treatment": "deep_dive",
      "excerpt": "CDC is a technique for tracking changes..."
    },
    {
      "name": "Event Sourcing",
      "treatment": "explain",
      "excerpt": "Related to CDC, event sourcing..."
    }
  ],
  "relationships": [
    {
      "source": "Change Data Capture",
      "target": "Database Replication",
      "type": "REQUIRES",
      "notes": "CDC builds on replication concepts"
    },
    {
      "source": "Change Data Capture",
      "target": "Event Sourcing",
      "type": "RELATED_TO",
      "notes": "Both capture state changes over time"
    }
  ],
  "metadata": {
    "content_type": "conceptual",
    "difficulty": "intermediate",
    "summary": "This chapter covers ingestion patterns including CDC, streaming, and batch approaches."
  }
}
```

## Step 4: Store Extraction Results

After Claude provides the JSON, save it:

```sql
-- Insert a new concept
INSERT INTO concepts (concept_id, name, domain, created_at)
VALUES ('change_data_capture', 'Change Data Capture', 'data_engineering', CURRENT_TIMESTAMP);

-- Map concept to chapter
INSERT INTO chapter_concepts (chapter_id, concept_id, treatment, excerpt)
VALUES ('fundamentals-of-data-engineering:7', 'change_data_capture', 'deep_dive', 
        'CDC is a technique for tracking changes...');

-- Add relationship
INSERT INTO concept_relationships (source_id, target_id, relationship, source_ref)
VALUES ('change_data_capture', 'database_replication', 'REQUIRES', 
        'fundamentals-of-data-engineering:7');

-- Update chapter metadata
UPDATE chapters
SET key_concepts = ['change_data_capture', 'event_sourcing', 'streaming_ingestion'],
    content_type = 'conceptual',
    difficulty = 'intermediate',
    summary = 'This chapter covers ingestion patterns...'
WHERE chapter_id = 'fundamentals-of-data-engineering:7';
```

## Step 5: Query the Concept Graph

### Find Prerequisites

```sql
-- Direct prerequisites for a concept
SELECT prereq_name, strength
FROM v_concept_prerequisites
WHERE concept_id = 'dimensional_modeling';

-- Recursive prerequisites (learning path)
WITH RECURSIVE prereqs AS (
    SELECT target_id, 1 AS depth
    FROM concept_relationships
    WHERE source_id = 'dimensional_modeling' AND relationship = 'REQUIRES'
    
    UNION ALL
    
    SELECT cr.target_id, p.depth + 1
    FROM concept_relationships cr
    JOIN prereqs p ON cr.source_id = p.target_id
    WHERE cr.relationship = 'REQUIRES' AND p.depth < 3
)
SELECT c.name, MIN(depth) AS learn_first
FROM prereqs p
JOIN concepts c ON p.target_id = c.concept_id
GROUP BY c.name
ORDER BY learn_first DESC;
```

### Find Related Topics

```sql
-- Concepts that co-occur in chapters
WITH my_chapters AS (
    SELECT chapter_id FROM chapter_concepts WHERE concept_id = 'cdc'
)
SELECT c.name, COUNT(*) AS co_occurrences
FROM chapter_concepts cc
JOIN concepts c ON cc.concept_id = c.concept_id  
WHERE cc.chapter_id IN (SELECT * FROM my_chapters)
  AND cc.concept_id != 'cdc'
GROUP BY c.name
ORDER BY co_occurrences DESC
LIMIT 10;
```

### Compare Author Perspectives

```sql
-- How different authors treat a concept
SELECT DISTINCT
    b.authors[1] AS author,
    b.title AS book,
    ch.title AS chapter,
    cc.treatment,
    ch.summary
FROM chapter_concepts cc
JOIN chapters ch ON cc.chapter_id = ch.chapter_id
JOIN books b ON ch.book_id = b.book_id
WHERE cc.concept_id = 'dimensional_modeling'
  AND cc.treatment IN ('explain', 'deep_dive')
ORDER BY b.pub_date DESC;
```

## Step 6: Validate the Graph

```sql
-- Concepts without any chapter mappings
SELECT c.concept_id, c.name
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
WHERE cc.chapter_id IS NULL;

-- Orphan relationships (concepts not in concepts table)
SELECT DISTINCT source_id 
FROM concept_relationships
WHERE source_id NOT IN (SELECT concept_id FROM concepts);

-- Circular dependencies
WITH RECURSIVE cycle_check AS (
    SELECT source_id, target_id, ARRAY[source_id] AS path
    FROM concept_relationships WHERE relationship = 'REQUIRES'
    
    UNION ALL
    
    SELECT cr.source_id, cr.target_id, array_append(cc.path, cr.source_id)
    FROM concept_relationships cr
    JOIN cycle_check cc ON cr.source_id = cc.target_id
    WHERE NOT array_contains(cc.path, cr.source_id)
      AND array_length(cc.path) < 10
)
SELECT * FROM cycle_check WHERE array_contains(path, target_id);
```

## Tips for Good Concept Extraction

1. **Be consistent with naming**: Use the same concept name across books
2. **Use canonical names**: "SCD Type 2" not "Slowly Changing Dimension Type Two"
3. **Capture aliases**: Store variations in the aliases array
4. **Be judicious with relationships**: Only create meaningful connections
5. **Include excerpts**: Brief quotes help validate the extraction

## Next Steps

- Build learning paths for key topics
- Generate domain skills from the concept graph
- Identify gaps in coverage

---

**Tutorial completed!** You now have a working concept graph.
