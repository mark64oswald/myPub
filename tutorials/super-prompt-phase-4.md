# Super Prompt: Phase 4 - Pattern Library

## Context

You are helping build the pattern library for the myPub knowledge base. This is Phase 4 of 5, focused on:
- Extracting reusable patterns from chapters
- Documenting variations and extensions
- Creating decision frameworks

**Prerequisite:** Phase 3 complete (skills generated)

## Pattern Library Philosophy

Patterns transform narrative knowledge into actionable building blocks. A developer with Claude + patterns should produce the same quality output as a domain expert.

**Value proposition:** Accelerated time-to-quality
- Without patterns: Generic approach → wrong grain → 3 iterations → 6-12 months
- With patterns: Correct structure → right the first time → 2-4 months

## Step 1: Identify Pattern Candidates

Find chapters with implementable structures:

```sql
-- Chapters likely to contain patterns
SELECT 
    b.title AS book,
    ch.title AS chapter,
    ch.chapter_id,
    ch.token_count,
    array_to_string(ch.key_concepts, ', ') AS concepts
FROM chapters ch
JOIN books b ON ch.book_id = b.book_id
WHERE ch.title ILIKE '%dimension%'
   OR ch.title ILIKE '%fact%'
   OR ch.title ILIKE '%model%'
   OR ch.title ILIKE '%schema%'
   OR ch.title ILIKE '%pattern%'
   OR ch.title ILIKE '%healthcare%'
   OR ch.title ILIKE '%claim%'
ORDER BY b.title;
```

## Step 2: Extract Healthcare Patterns

Priority patterns for healthcare domain:

### Pattern: Healthcare Claim Line Fact

1. Load relevant chapter (e.g., Kimball Ch 11 Healthcare)

2. Extract pattern with this prompt:
```
From this chapter, extract the healthcare claim line fact pattern:

1. Problem statement: What problem does this solve?
2. Grain: What is the correct grain?
3. Schema: List all columns with types and descriptions
4. Keys: Primary key, foreign keys
5. When to use: What scenarios?
6. When NOT to use: Anti-patterns?
7. Variations: Alternative approaches mentioned?
8. Test cases: How to validate correct implementation?

Format as YAML following the pattern template in docs/patterns.md
```

3. Save to: `patterns/healthcare/dimensional/fct_claim_line.yml`

4. Register in database:
```sql
INSERT INTO patterns (pattern_id, name, domain, category, description, canonical_yaml)
VALUES (
    'healthcare.dimensional.fct_claim_line',
    'Healthcare Claim Line Fact',
    'healthcare',
    'dimensional',
    'Fact table for healthcare claims at the service line grain',
    '{yaml_content}'
);
```

### Pattern: Member Dimension (SCD Type 2)

Extract pattern for tracking member history:
- Effective/expiration dates
- Current flag
- Key handling
- Common attributes

Save to: `patterns/healthcare/dimensional/dim_member.yml`

### Pattern: Provider Dimension

Extract provider dimension pattern:
- NPI handling
- Specialty tracking
- Network status
- Credentialing dates

Save to: `patterns/healthcare/dimensional/dim_provider.yml`

### Pattern: PMPM Metric

Extract Per Member Per Month calculation:
- Numerator (paid/allowed amounts)
- Denominator (member months)
- Variations (medical PMPM, Rx PMPM, total)

Save to: `patterns/healthcare/metrics/pmpm.yml`

## Step 3: Document Pattern Variations

For each pattern, identify variations from different sources:

```sql
-- Find different sources discussing same concept
SELECT 
    c.name AS concept,
    b.title AS book,
    b.authors,
    ch.title AS chapter,
    cc.treatment
FROM chapter_concepts cc
JOIN concepts c ON cc.concept_id = c.concept_id
JOIN chapters ch ON cc.chapter_id = ch.chapter_id
JOIN books b ON ch.book_id = b.book_id
WHERE c.concept_id IN ('fact_table', 'healthcare_claims', 'dimensional_modeling')
  AND cc.treatment IN ('deep_dive', 'explain')
ORDER BY c.name, b.pub_date DESC;
```

### Variation Example: Diagnosis Handling

Load multiple sources discussing diagnosis handling, then:

```
Compare how these sources handle multiple diagnoses on claims:

Source 1: [Kimball DW Toolkit]
Source 2: [Healthcare Analytics book]
Source 3: [Modern Data Warehouse book]

For each approach, document:
1. Name of the variation
2. Schema structure
3. When to use it
4. Pros and cons
5. Authority level of source
```

Save variations:
```sql
INSERT INTO pattern_variations (variation_id, pattern_id, name, when_to_use, variation_yaml)
VALUES 
('healthcare.dimensional.diagnosis_handling:positional', 
 'healthcare.dimensional.fct_claim_line',
 'Positional Columns',
 'Primary diagnosis queries, simple star schema, limited diagnosis count',
 '{yaml}'),
 
('healthcare.dimensional.diagnosis_handling:bridge_table',
 'healthcare.dimensional.fct_claim_line', 
 'Bridge Table',
 'Any-diagnosis-contains queries, unlimited diagnoses, HCC analysis',
 '{yaml}');
```

## Step 4: Document Pattern Extensions

Identify extensions that add capabilities:

### HCC Risk Mapping Extension

```yaml
extension_id: healthcare.dimensional.hcc_risk_mapping
extends_pattern: healthcare.dimensional.fct_claim_line
name: HCC Risk Mapping
when_required: Medicare Advantage analysis, risk adjustment calculations

additional_tables:
  - name: xref_diagnosis_hcc
    description: Crosswalk from ICD-10 to HCC
    columns:
      - icd10_code
      - hcc_code
      - version_year
      
  - name: dim_hcc
    description: HCC dimension
    columns:
      - hcc_sk
      - hcc_code
      - hcc_description
      - coefficient
      - category

schema_additions:
  - table: fct_claim_line
    column: hcc_flag
    type: BOOLEAN
    description: Whether any diagnosis maps to an HCC
```

## Step 5: Create Pattern Skills

Create skills that teach Claude how to use patterns:

`skills/patterns/healthcare/SKILL.md`:
```markdown
# Healthcare Patterns Skill

## Available Patterns

| Pattern ID | Description |
|------------|-------------|
| healthcare.dimensional.fct_claim_line | Claim line fact table |
| healthcare.dimensional.dim_member | Member dimension (SCD-2) |
| healthcare.dimensional.dim_provider | Provider dimension |
| healthcare.metrics.pmpm | PMPM calculation |

## Using Patterns

### Finding Patterns
```sql
SELECT pattern_id, name, description 
FROM patterns 
WHERE domain = 'healthcare';
```

### Loading a Pattern
```sql
SELECT canonical_yaml FROM patterns WHERE pattern_id = ?;
SELECT * FROM pattern_variations WHERE pattern_id = ?;
SELECT * FROM pattern_extensions WHERE pattern_id = ?;
```

### Decision Framework

When user asks to build healthcare data model:

1. Identify required entities (claims? members? providers?)
2. Load relevant patterns
3. Check for variations needed:
   - "HCC" or "risk adjustment" → bridge_table variation + hcc_extension
   - "simple reporting" → canonical positional columns
4. Generate code from templates
5. Explain choices

## Example Response

"I'll use the fct_claim_line pattern with the bridge table variation 
because you mentioned HCC analysis. This requires flexible diagnosis 
queries. I'm also applying the hcc_risk_mapping extension.

[Generated DDL]

**Design decisions:**
- Grain: claim line (per Kimball Ch 11)
- Diagnosis handling: bridge table (for HCC queries)
- Extensions: HCC mapping tables included
"
```

## Step 6: Validate Patterns

For each pattern, create test cases:

```sql
-- Test case: PMPM calculation
-- Setup: Known member months and paid amounts
-- Expected: Specific PMPM value

-- Test case: SCD-2 query
-- Setup: Member with historical changes
-- Expected: Correct current and historical records
```

## Success Criteria for Phase 4

- [ ] 5+ healthcare patterns extracted
- [ ] Each pattern has schema + template + tests
- [ ] 2+ variations documented where applicable
- [ ] 1+ extension documented
- [ ] Pattern skills created
- [ ] Can generate code from patterns

## Next Phase

After Phase 4 is complete, proceed to Phase 5: Full Indexing & Refinement.

Load the Phase 5 super prompt: `tutorials/super-prompt-phase-5.md`
