# Super Prompt: Generate Domain Skill

## Goal
Generate a comprehensive SKILL.md file for a topic/domain based on knowledge base content.

## Prerequisites
- Books covering the topic are indexed
- Concepts have been extracted (recommended)
- Target directory exists

## Variables
- `{{TOPIC}}`: The topic/domain (e.g., "Change Data Capture", "HCC Risk Adjustment")
- `{{DOMAIN}}`: Domain category (e.g., "data-engineering", "healthcare")
- `{{OUTPUT_DIR}}`: Where to save (default: skills/generated/)

## Prompt

```
I need to generate a comprehensive Skill file for my knowledge base.

**Topic:** {{TOPIC}}
**Domain:** {{DOMAIN}}
**Output directory:** ~/Developer/projects/myPub/{{OUTPUT_DIR}}

Please:

1. **Find all relevant content in the knowledge base:**

   ```sql
   -- Find by concept
   SELECT DISTINCT
       vcc.chapter_id,
       vcc.chapter_title,
       vcc.book_title,
       vcc.authors,
       vcc.treatment,
       vcc.token_count
   FROM v_concept_chapters vcc
   WHERE vcc.concept_name ILIKE '%{{TOPIC}}%'
      OR vcc.concept_name ILIKE '%{{TOPIC_SLUG}}%'
   ORDER BY 
       CASE vcc.treatment 
           WHEN 'deep_dive' THEN 1 
           WHEN 'explain' THEN 2 
           ELSE 3 
       END;
   
   -- Also search chapter titles/summaries
   SELECT 
       ch.chapter_id,
       ch.title,
       b.title AS book_title,
       b.authors,
       ch.token_count,
       ch.summary
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.title ILIKE '%{{TOPIC}}%'
      OR ch.summary ILIKE '%{{TOPIC}}%'
   ORDER BY ch.token_count DESC
   LIMIT 15;
   ```

2. **Load the top 3-5 chapters** (prioritize deep_dive and explain):
   - Load each chapter via ebook-mcp
   - Note the key points from each

3. **Synthesize into a SKILL.md with this structure:**

   ```markdown
   # {{TOPIC}} Skill

   ## Overview
   [Synthesize from sources - what is this, why does it matter]

   ## Key Concepts
   [List and explain core concepts with definitions]
   - **Concept 1**: Definition and significance
   - **Concept 2**: Definition and significance

   ## How It Works
   [Technical explanation synthesized from sources]
   
   ## Common Patterns
   [Practical patterns and approaches]
   
   ### Pattern 1: [Name]
   **When to use:** [Context]
   **Implementation:**
   ```sql
   -- Example code
   ```

   ## Best Practices
   [Synthesized from all sources]
   1. Practice with explanation
   2. Practice with explanation

   ## Common Pitfalls
   [What to avoid, synthesized from sources]
   - Pitfall: How to avoid

   ## When to Use / When Not to Use
   **Use when:**
   - Condition 1
   
   **Avoid when:**
   - Condition 1

   ## Related Concepts
   [Link to related topics in the KB]
   - Related Topic 1: Brief connection explanation
   - Related Topic 2: Brief connection explanation

   ## Source Chapters
   [List all chapters used, with treatment indicators]
   
   ### [Book Title]
   *by [Authors]*
   - 🔬 **[Chapter Title]** (~N tokens) - [brief note on what it covers]
   
   ## Metadata
   - Generated: [timestamp]
   - Domain: {{DOMAIN}}
   - Source chapters: N
   - Source books: M
   ```

4. **Save the skill file:**
   - Create directory: `{{OUTPUT_DIR}}/{{TOPIC_SLUG}}/`
   - Save as: `SKILL.md`

5. **Update the skills table:**
   ```sql
   INSERT INTO skills (skill_id, name, filepath, domain, description, source_chapters, generated_at)
   VALUES (
       '{{TOPIC_SLUG}}',
       '{{TOPIC}}',
       '{{OUTPUT_DIR}}/{{TOPIC_SLUG}}/SKILL.md',
       '{{DOMAIN}}',
       '[One-line description]',
       ['chapter_id_1', 'chapter_id_2', ...],
       CURRENT_TIMESTAMP
   );
   ```

6. **Report:**
   - Skill file location
   - Number of source chapters used
   - Key insights synthesized
   - Suggested related skills to generate
```

## Expected Output
- Complete SKILL.md file
- SQL to register in catalog
- Summary of what was synthesized

## Quality Checklist
- [ ] Overview is clear and actionable
- [ ] Key concepts are well-defined
- [ ] Patterns include working code examples
- [ ] Best practices are specific, not generic
- [ ] Sources are properly cited
- [ ] Content is synthesized, not just copied
