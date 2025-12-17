# Super Prompt: Create Pattern from Sources

## Goal
Extract and document a reusable pattern from knowledge base sources.

## Prerequisites
- Relevant books are indexed
- Pattern structure is understood (see docs/patterns.md)

## Variables
- `{{PATTERN_NAME}}`: Human-readable name (e.g., "Claim Line Fact Table")
- `{{PATTERN_ID}}`: Hierarchical ID (e.g., "healthcare.dimensional.fct_claim_line")
- `{{DOMAIN}}`: Domain (e.g., "healthcare", "dimensional_modeling")
- `{{CATEGORY}}`: Category (e.g., "facts", "dimensions", "metrics")

## Prompt

```
I need to create a reusable pattern for my knowledge base pattern library.

**Pattern Name:** {{PATTERN_NAME}}
**Pattern ID:** {{PATTERN_ID}}
**Domain:** {{DOMAIN}}
**Category:** {{CATEGORY}}

Please:

1. **Find relevant source chapters:**
   ```sql
   -- Search for chapters covering this pattern
   SELECT 
       ch.chapter_id,
       ch.title,
       b.title AS book_title,
       b.authors,
       b.pub_date,
       ch.token_count
   FROM chapters ch
   JOIN books b ON ch.book_id = b.book_id
   WHERE ch.title ILIKE '%{{SEARCH_TERMS}}%'
      OR ch.summary ILIKE '%{{SEARCH_TERMS}}%'
   ORDER BY b.pub_date DESC;
   ```

2. **Load the most authoritative sources** (2-4 chapters):
   - Prioritize foundational texts (Kimball, Inmon, etc.)
   - Include recent sources for modern variations
   - Load via ebook-mcp

3. **Analyze and extract the pattern:**

   a. **Problem Statement**: What problem does this pattern solve?
   
   b. **Canonical Implementation**: The standard/recommended approach
      - Schema definition (DDL)
      - Key design decisions
      - Example data
   
   c. **Variations**: Alternative valid approaches
      - When does each apply?
      - How does the schema differ?
      - What are the trade-offs?
   
   d. **Extensions**: Additive capabilities
      - What optional features can be added?
      - When are they required?
   
   e. **Decision Framework**: How to choose between options

4. **Output as YAML:**

   ```yaml
   pattern:
     id: {{PATTERN_ID}}
     name: {{PATTERN_NAME}}
     domain: {{DOMAIN}}
     category: {{CATEGORY}}
     
     description: |
       [One paragraph description]
     
     problem_statement: |
       [What problem this pattern solves]
     
     when_to_use:
       - [Condition 1]
       - [Condition 2]
     
     when_not_to_use:
       - [Anti-condition 1]
       - [Anti-condition 2]
     
     canonical:
       description: |
         [Explain the standard approach]
       schema: |
         CREATE TABLE {{table_name}} (
             -- Schema definition
         );
       template: |
         -- Parameterized template
         CREATE TABLE {{table_name}} (
             {{surrogate_key}} BIGINT PRIMARY KEY,
             -- etc.
         );
       example: |
         -- Concrete example
         CREATE TABLE fct_claim_line (
             claim_line_key BIGINT PRIMARY KEY,
             -- etc.
         );
     
     variations:
       - id: variation_1
         name: [Variation Name]
         description: |
           [How this differs from canonical]
         when_to_use: |
           [When this variation is preferred]
         when_not_to_use: |
           [When to avoid this variation]
         trade_offs:
           pros:
             - [Advantage 1]
           cons:
             - [Disadvantage 1]
         schema: |
           -- Variation schema
     
     extensions:
       - id: extension_1
         name: [Extension Name]
         description: |
           [What this adds]
         when_required: |
           [When you need this extension]
         schema: |
           -- Additional schema
     
     decision_framework: |
       Use this framework to select the right approach:
       
       1. [Question 1]?
          - If yes → [recommendation]
          - If no → [recommendation]
       
       2. [Question 2]?
          - If yes → [recommendation]
     
     sources:
       - book: [Book Title]
         chapter: [Chapter Title]
         authority: high  # high, medium, low
         contribution: canonical  # canonical, variation, extension
     
     related_patterns:
       - [related_pattern_id_1]
       - [related_pattern_id_2]
   ```

5. **Save the pattern:**
   - File: `patterns/{{DOMAIN}}/{{CATEGORY}}/{{PATTERN_NAME_SLUG}}.yaml`

6. **Register in catalog:**
   ```sql
   INSERT INTO patterns (pattern_id, name, description, domain, category, canonical_yaml)
   VALUES ('{{PATTERN_ID}}', '{{PATTERN_NAME}}', '[description]', 
           '{{DOMAIN}}', '{{CATEGORY}}', '[full yaml]');
   
   -- Add sources
   INSERT INTO pattern_sources (pattern_id, chapter_id, authority, contribution)
   VALUES ('{{PATTERN_ID}}', '[chapter_id]', 'high', 'canonical');
   
   -- Add variations
   INSERT INTO pattern_variations (variation_id, pattern_id, name, when_to_use, variation_yaml)
   VALUES ('{{PATTERN_ID}}:variation_1', '{{PATTERN_ID}}', '[name]', '[when]', '[yaml]');
   ```

7. **Report:**
   - Pattern file location
   - Sources used with authority levels
   - Identified variations and extensions
   - Decision framework summary
```

## Expected Output
- Complete pattern YAML file
- SQL to register in catalog
- Summary of pattern structure

## Quality Checklist
- [ ] Problem statement is clear
- [ ] Canonical approach is well-defined
- [ ] Variations have clear differentiation
- [ ] Decision framework helps selection
- [ ] Sources properly cited with authority
- [ ] Schema is valid and complete
- [ ] Examples are concrete and realistic
