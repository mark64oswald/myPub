# /kb-search Command

## Description

Search the knowledge base for chapters and concepts related to a topic.

## Usage

```text
/kb-search <topic>
```

## Behavior

When this command is invoked:

1. **Search concepts table** for matching concept

   ```sql
   SELECT concept_id, name, description, domain, aliases
   FROM concepts
   WHERE name ILIKE '%{topic}%'
      OR '{topic}' = ANY(aliases)
      OR description ILIKE '%{topic}%';
   ```

2. **Search chapters** via full-text search

   ```sql
   SELECT
       ch.chapter_id,
       ch.title,
       ch.summary,
       b.title AS book_title,
       b.authors
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.title ILIKE '%{topic}%'
      OR ch.summary ILIKE '%{topic}%'
   ORDER BY b.pub_date DESC
   LIMIT 10;
   ```

3. **If concept found**, also get related chapters

   ```sql
   SELECT book_title, chapter_title, treatment
   FROM v_concept_chapters
   WHERE concept_id = '{found_concept}'
   ORDER BY treatment DESC
   LIMIT 10;
   ```

4. **Format response**:
   - Show matching concept(s) with descriptions
   - List top chapters ranked by relevance
   - Indicate treatment level for each
   - Offer next steps

## Example Output

```text
**Concept Found:** Change Data Capture (CDC)
- Domain: Data Engineering
- Description: Pattern for capturing incremental changes from source systems
- Aliases: cdc, incremental capture

**Top Chapters:**
1. 📖 Fundamentals of Data Engineering, Ch 7: Ingestion [deep_dive]
   "Covers CDC patterns, tools, and implementation strategies..."

2. 📖 Kafka: The Definitive Guide, Ch 6: Kafka Connect [explain]
   "Discusses CDC connectors and streaming integration..."

3. 📖 Building Event-Driven Microservices, Ch 4 [explain]
   "CDC as event source for microservices..."

**Would you like me to:**
- Load a chapter for detailed explanation?
- Compare how different authors approach CDC?
- Show prerequisites for understanding CDC?
```
