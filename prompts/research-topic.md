# Super Prompt: Research Topic

## Goal

Conduct comprehensive research on a topic using the knowledge base, synthesizing insights from multiple sources.

## Prerequisites

- Books covering the topic are indexed
- Catalog is accessible

## Variables

- `{{TOPIC}}`: The topic to research
- `{{DEPTH}}`: Research depth (quick, standard, comprehensive)
- `{{FOCUS}}`: Optional focus area

## Prompt

````text
I need to research a topic using my knowledge base.

**Topic:** {{TOPIC}}
**Depth:** {{DEPTH}}
**Focus:** {{FOCUS}} (if any)

Please conduct research following this approach:

1. **Discovery Phase:**

   Find all relevant content:
   ```sql

   -- By concept
   SELECT concept_name, domain, COUNT(*) as chapter_count
   FROM v_concept_chapters
   WHERE concept_name ILIKE '%{{TOPIC}}%'
   GROUP BY concept_name, domain;

   -- By chapter content
   SELECT
       b.title AS book,
       b.authors,
       b.pub_date,
       ch.title AS chapter,
       ch.token_count,
       ch.summary
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.title ILIKE '%{{TOPIC}}%'
      OR ch.summary ILIKE '%{{TOPIC}}%'
   ORDER BY b.pub_date DESC;

   ```text

   Report: How many sources cover this topic?

2. **Source Selection:**

   Based on depth level:
   - **Quick**: Load 1-2 best sources (most authoritative or recent)
   - **Standard**: Load 3-4 sources (mix of foundational + recent)
   - **Comprehensive**: Load 5-7 sources (all significant coverage)

   Prioritize:
   - Foundational texts (Kimball, etc.) for methodology
   - Recent books for current practices
   - Different authors for diverse perspectives

3. **Content Analysis:**

   For each source, extract:
   - **Key points**: Main arguments/insights
   - **Unique contributions**: What this source adds
   - **Examples**: Concrete examples given
   - **Recommendations**: Specific advice

   Note any:
   - **Agreements**: Points multiple authors agree on
   - **Disagreements**: Conflicting viewpoints
   - **Evolution**: How thinking has changed over time

4. **Synthesis:**

   Create a research summary with:

   ```markdown

   # Research: {{TOPIC}}

   ## Executive Summary

   [2-3 paragraph overview of findings]

   ## Key Findings

   ### [Finding 1]

   [Details with source citations]

   ### [Finding 2]

   [Details with source citations]

   ## Perspectives Compared

   | Aspect | [Author 1] | [Author 2] | [Author 3] |
   |--------|------------|------------|------------|
   | [Aspect 1] | View | View | View |

   ## Points of Agreement

   - [Agreed point 1] (Sources: A, B, C)
   - [Agreed point 2] (Sources: A, B)

   ## Points of Disagreement

   - [Disagreement]: [Author 1] says X, while [Author 2] says Y
     - My assessment: [Your analysis]

   ## Evolution of Thinking

   - **Early (pre-2015)**: [approach]
   - **Middle (2015-2020)**: [approach]
   - **Current (2020+)**: [approach]

   ## Practical Recommendations

   Based on this research:
   1. [Recommendation 1]
   2. [Recommendation 2]

   ## Knowledge Gaps

   - [Gap 1]: Consider supplementing with [suggestion]

   ## Sources Consulted

   1. [Book] by [Author] - Chapter: [Title] - Contribution: [what it added]
   2. ...
   ```text

5. **Follow-up Suggestions:**
   - Related topics to explore
   - Patterns that might be useful
   - Skills that could be generated

````

## Expected Output

- Comprehensive research summary
- Source citations
- Practical recommendations
- Identified knowledge gaps

## Depth Guidelines

| Depth | Sources | Time | Best For |
|-------|---------|------|----------|
| Quick | 1-2 | 5 min | Quick answer, familiar topic |
| Standard | 3-4 | 15 min | Learning, decision support |
| Comprehensive | 5-7 | 30 min | Deep understanding, teaching |
