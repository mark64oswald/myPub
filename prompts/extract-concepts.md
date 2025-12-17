# Super Prompt: Extract Concepts from Chapter

## Goal
Analyze a chapter and extract key concepts, relationships, and metadata for the concept graph.

## Prerequisites
- Book is indexed in catalog
- Chapter content is accessible via ebook-mcp

## Variables
- `{{CHAPTER_ID}}`: The chapter_id from the catalog (e.g., "book-slug:7")
- `{{DOMAIN}}`: Optional domain hint (e.g., "data_engineering", "healthcare")

## Prompt

```
I need to extract concepts from a chapter in my knowledge base.

**Chapter ID:** {{CHAPTER_ID}}
**Domain hint:** {{DOMAIN}}

Please:

1. **Get chapter details from catalog:**
   ```sql
   SELECT 
       ch.chapter_id,
       ch.title,
       ch.href,
       ch.token_count,
       b.title AS book_title,
       b.authors,
       b.filepath
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.chapter_id = '{{CHAPTER_ID}}';
   ```

2. **Load the full chapter content:**
   Use ebook-mcp:get_epub_chapter_markdown with the filepath and href.

3. **Analyze and extract:**

   For each **concept** discussed in the chapter:
   - `name`: Canonical name (use consistent naming across the KB)
   - `treatment`: How deeply covered?
     - `mention`: Brief reference only
     - `explain`: Concept is explained with some detail
     - `deep_dive`: Comprehensive coverage with examples
   - `excerpt`: A brief quote (1-2 sentences) showing the treatment

   For **relationships** between concepts:
   - `source` → `target`: `relationship_type`
   - Types:
     - `REQUIRES`: Source requires understanding of target first
     - `RELATED_TO`: Concepts are related but neither depends on other
     - `EXTENDS`: Source builds upon or extends target
     - `CONTRASTS_WITH`: Source is an alternative to or contrasts with target

   For **chapter metadata**:
   - `content_type`: tutorial, reference, conceptual, case_study
   - `difficulty`: beginner, intermediate, advanced
   - `summary`: 2-3 sentence summary

4. **Output as JSON:**
   ```json
   {
     "chapter_id": "{{CHAPTER_ID}}",
     "concepts": [
       {"name": "...", "treatment": "...", "excerpt": "..."}
     ],
     "relationships": [
       {"source": "...", "target": "...", "type": "...", "notes": "..."}
     ],
     "metadata": {
       "content_type": "...",
       "difficulty": "...",
       "summary": "..."
     }
   }
   ```

5. **Generate SQL to store results:**
   Provide INSERT/UPDATE statements to save to the catalog.

6. **Check for existing concepts:**
   Before creating new concepts, check if similar ones exist:
   ```sql
   SELECT concept_id, name, aliases
   FROM concepts
   WHERE name ILIKE '%keyword%'
      OR 'keyword' = ANY(aliases);
   ```
   Reuse existing concept_ids where appropriate.
```

## Expected Output
- Structured JSON with extracted concepts
- SQL statements to store in catalog
- Notes on any ambiguous extractions

## Quality Checklist
- [ ] Concept names are consistent with existing KB
- [ ] Treatment levels accurately reflect coverage depth
- [ ] Relationships have clear directionality
- [ ] Summary captures chapter essence
- [ ] No duplicate concepts created
