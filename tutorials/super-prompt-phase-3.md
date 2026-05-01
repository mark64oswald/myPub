# Super Prompt: Phase 3 - Skills Generation

## Context

You are helping generate Skills files for the myPub knowledge base. This is Phase 3 of 5, focused on:

- Creating domain-specific skills
- Auto-generating skills from chapters
- Establishing skill patterns

**Prerequisite:** Phase 2 complete (concepts extracted)

## Understanding Skills

A Skill file is a structured document that encodes expertise for Claude. It tells Claude:

- What domain knowledge is available
- How to query and use that knowledge
- Response patterns for different request types
- Key concepts, pitfalls, and best practices

## Step 1: Review Domain Coverage

```sql
-- See which domains have good coverage
SELECT
    c.domain,
    COUNT(DISTINCT c.concept_id) AS concepts,
    COUNT(DISTINCT cc.chapter_id) AS chapters,
    COUNT(DISTINCT ch.book_id) AS books
FROM concepts c
LEFT JOIN chapter_concepts cc ON c.concept_id = cc.concept_id
LEFT JOIN chapters ch ON cc.chapter_id = ch.chapter_id
GROUP BY c.domain
ORDER BY concepts DESC;
```

## Step 2: Generate Domain Skills

For each major domain, create a skill file.

### Skill Template

```markdown
# {Domain Name} Skill

## Overview

[2-3 paragraph domain overview synthesized from key chapters]

## Key Concepts

| Concept | Description | Key Sources |
|---------|-------------|-------------|
| {concept_1} | {description} | {book: chapter} |
| {concept_2} | {description} | {book: chapter} |

## Concept Relationships

```

{concept_a} ─REQUIRES→ {concept_b}
{concept_c} ─RELATED_TO→ {concept_d}

```text

## Common Patterns

### Pattern 1: {pattern_name}
- **When to use:** {context}
- **Key elements:** {list}
- **Source:** {book, chapter}

## Query Strategies

### Finding {domain} Content
```sql

-- Find chapters on {domain} topics
SELECT ...

```text

### Getting Related Concepts
```sql

-- Find related {domain} concepts
SELECT ...

```text

## Response Patterns

### For Learning Requests
[How to respond to "explain {domain topic}"]

### For Building Requests
[How to respond to "build/create {domain artifact}"]

## Key Sources

| Book | Relevance | Key Chapters |
|------|-----------|--------------|
| {book_1} | {why relevant} | Ch 1, Ch 5, Ch 8 |

## Pitfalls to Avoid

- {common mistake 1}
- {common mistake 2}

## Domain-Specific Terminology

| Term | Definition |
|------|------------|
| {term_1} | {definition} |
```

## Step 3: Generate Data Engineering Skill

```sql
-- Get top chapters for data engineering
SELECT
    b.title AS book,
    ch.title AS chapter,
    ch.chapter_id,
    array_agg(cc.concept_id) AS concepts
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
JOIN concepts c ON cc.concept_id = c.concept_id
WHERE c.domain = 'data_engineering'
  AND cc.treatment IN ('deep_dive', 'explain')
GROUP BY b.title, ch.title, ch.chapter_id
ORDER BY COUNT(*) DESC
LIMIT 20;
```

Load 3-5 top chapters and generate the skill:

**Prompt for Claude:**

```text
Based on these chapters, generate a Data Engineering skill file that:

1. Provides an overview of data engineering fundamentals
2. Lists key concepts with brief descriptions
3. Shows concept relationships (what requires what)
4. Documents common patterns (ETL, ELT, CDC, Medallion, etc.)
5. Provides query strategies for finding data engineering content
6. Lists pitfalls and best practices
7. Cites sources appropriately

Format as markdown following the skill template.
```

Save to: `skills/domains/data-engineering/SKILL.md`

## Step 4: Generate Healthcare Analytics Skill

```sql
-- Get top chapters for healthcare
SELECT
    b.title AS book,
    ch.title AS chapter,
    ch.chapter_id,
    cc.treatment
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
JOIN chapter_concepts cc ON ch.chapter_id = cc.chapter_id
JOIN concepts c ON cc.concept_id = c.concept_id
WHERE c.domain = 'healthcare'
  AND cc.treatment IN ('deep_dive', 'explain')
GROUP BY b.title, ch.title, ch.chapter_id, cc.treatment
ORDER BY cc.treatment, b.pub_date DESC
LIMIT 20;
```

Focus areas for healthcare skill:

- Claims data structures (header, line, diagnosis, procedure)
- Provider and member dimensions
- Healthcare metrics (PMPM, MLR, utilization)
- Quality measures (HEDIS, Stars)
- Risk adjustment (HCC, RAF)
- Regulatory context (HIPAA, CMS)

Save to: `skills/domains/healthcare-analytics/SKILL.md`

## Step 5: Generate Dimensional Modeling Skill

Focus areas:

- Kimball methodology fundamentals
- Fact table types (transaction, snapshot, factless)
- Dimension types (SCD, role-playing, junk, degenerate)
- Star vs snowflake schemas
- Conformed dimensions
- Bus architecture

Save to: `skills/domains/dimensional-modeling/SKILL.md`

## Step 6: Create Ad-Hoc Skill Generator

Use the `scripts/generate_skill.py` script for on-demand skills:

```bash
python scripts/generate_skill.py --topic "HCC Risk Adjustment" --limit 5
```

This creates a scaffold that Claude can fill in.

## Step 7: Register Skills in Catalog

```sql
INSERT INTO skills (skill_id, name, filepath, domain, description, source_chapters, generated_at) VALUES
('data-engineering', 'Data Engineering',
 'skills/domains/data-engineering/SKILL.md',
 'data_engineering',
 'Core data engineering concepts, patterns, and practices',
 ARRAY['{chapter_id_1}', '{chapter_id_2}', ...],
 CURRENT_TIMESTAMP),

('healthcare-analytics', 'Healthcare Analytics',
 'skills/domains/healthcare-analytics/SKILL.md',
 'healthcare',
 'Healthcare data, claims, quality measures, and risk adjustment',
 ARRAY['{chapter_id_1}', '{chapter_id_2}', ...],
 CURRENT_TIMESTAMP),

('dimensional-modeling', 'Dimensional Modeling',
 'skills/domains/dimensional-modeling/SKILL.md',
 'dimensional_modeling',
 'Kimball dimensional modeling methodology and patterns',
 ARRAY['{chapter_id_1}', '{chapter_id_2}', ...],
 CURRENT_TIMESTAMP);
```

## Step 8: Test Skills

For each skill, test with sample queries:

**Data Engineering:**

- "Explain CDC and when to use it"
- "What are the differences between ETL and ELT?"
- "How does medallion architecture work?"

**Healthcare Analytics:**

- "Explain healthcare claims data structure"
- "What is HCC risk adjustment?"
- "How do I calculate PMPM?"

**Dimensional Modeling:**

- "Explain SCD Type 2"
- "When should I use a bridge table?"
- "What's the difference between fact types?"

## Success Criteria for Phase 3

- [ ] 3+ domain skills created (data-eng, healthcare, dimensional)
- [ ] Skills registered in catalog
- [ ] Each skill has: overview, concepts, patterns, sources
- [ ] Can generate ad-hoc skills with script
- [ ] Test queries work correctly with skills loaded

## Next Phase

After Phase 3 is complete, proceed to Phase 4: Pattern Library.

Load the Phase 4 super prompt: `tutorials/super-prompt-phase-4.md`
