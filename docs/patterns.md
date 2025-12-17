# Pattern Library Guide

## Overview

The Pattern Library is a collection of reusable building blocks extracted from the ePub knowledge base. Unlike simple code snippets, patterns include:

- **Structure**: Schema definitions, relationships
- **Context**: When to use, when not to use
- **Variations**: Alternative valid approaches
- **Extensions**: Additive capabilities
- **Sources**: Which books/chapters inform the pattern

## Pattern Structure

Each pattern is stored as YAML with a consistent structure:

```yaml
pattern:
  id: domain.category.name
  name: Human Readable Name
  description: What this pattern solves
  domain: healthcare | dimensional_modeling | data_engineering
  category: facts | dimensions | metrics | ingestion | etc.
  
  problem_statement: |
    The problem this pattern addresses...
  
  when_to_use:
    - Condition 1
    - Condition 2
  
  when_not_to_use:
    - Anti-pattern 1
    - Anti-pattern 2
  
  canonical:
    description: The standard/default implementation
    schema: |
      CREATE TABLE ...
    template: |
      -- Parameterized code
    example: |
      -- Concrete example
  
  variations:
    - id: variation_name
      name: Variation Display Name
      description: How this differs
      when_to_use: |
        Best when...
      when_not_to_use: |
        Avoid when...
      schema: |
        CREATE TABLE ...
  
  extensions:
    - id: extension_name
      name: Extension Display Name
      description: What this adds
      when_required: |
        Required when...
      schema: |
        -- Additional tables/columns
  
  sources:
    - book: Book Title
      chapter: Chapter Name
      authority: high | medium | low
      contribution: canonical | variation | extension
  
  related_patterns:
    - pattern_id_1
    - pattern_id_2
```

## Using Patterns

### Finding Patterns

```sql
-- Search by domain
SELECT pattern_id, name, description
FROM patterns
WHERE domain = 'healthcare';

-- Search by keyword
SELECT pattern_id, name, description
FROM patterns
WHERE name ILIKE '%claim%'
   OR description ILIKE '%claim%';
```

### Loading a Pattern

```sql
-- Get canonical pattern
SELECT canonical_yaml
FROM patterns
WHERE pattern_id = 'healthcare.dimensional.fct_claim_line';

-- Get variations
SELECT variation_id, name, when_to_use, variation_yaml
FROM pattern_variations
WHERE pattern_id = 'healthcare.dimensional.fct_claim_line';

-- Get extensions
SELECT extension_id, name, when_required, extension_yaml
FROM pattern_extensions
WHERE pattern_id = 'healthcare.dimensional.fct_claim_line';
```

### Applying Decision Framework

When Claude retrieves a pattern, it should:

1. **Understand the context** - What is the user building?
2. **Check variations** - Which variation fits best?
3. **Apply extensions** - Are any extensions required?
4. **Generate code** - Use template with user's specifics
5. **Explain choices** - Document why this approach

## Pattern Taxonomy

```
patterns/
├── healthcare/
│   ├── entities/
│   │   ├── member.yaml
│   │   ├── provider.yaml
│   │   ├── claim_header.yaml
│   │   └── claim_line.yaml
│   ├── dimensional/
│   │   ├── dim_member.yaml
│   │   ├── dim_provider.yaml
│   │   ├── dim_diagnosis.yaml
│   │   └── fct_claim_line.yaml
│   ├── metrics/
│   │   ├── pmpm.yaml
│   │   ├── mlr.yaml
│   │   └── utilization.yaml
│   └── reference_data/
│       ├── icd10.yaml
│       └── cpt.yaml
│
├── dimensional-modeling/
│   ├── facts/
│   │   ├── transaction.yaml
│   │   ├── periodic_snapshot.yaml
│   │   └── accumulating_snapshot.yaml
│   ├── dimensions/
│   │   ├── scd_type_1.yaml
│   │   ├── scd_type_2.yaml
│   │   ├── role_playing.yaml
│   │   └── bridge_table.yaml
│   └── common/
│       ├── date_dimension.yaml
│       └── surrogate_key.yaml
│
├── data-engineering/
│   ├── ingestion/
│   │   ├── cdc.yaml
│   │   ├── batch_extract.yaml
│   │   └── streaming.yaml
│   ├── transformation/
│   │   ├── medallion.yaml
│   │   └── staging.yaml
│   └── quality/
│       ├── completeness.yaml
│       └── referential.yaml
│
└── pipelines/
    ├── dbt/
    │   └── project_structure.yaml
    └── spark/
        └── batch_pipeline.yaml
```

## Handling Conflicts

When different sources disagree:

### Authority Hierarchy

1. **Regulatory/Standards** (CMS, HIPAA, HL7) - Highest authority
2. **Foundational Texts** (Kimball, Inmon) - Define methodology
3. **Platform Documentation** (Databricks, Snowflake) - Implementation
4. **Community/Emerging** (recent books, blogs) - Flag as emerging

### Documenting Conflicts

```yaml
conflicts:
  - topic: Claim fact grain
    positions:
      - source: Kimball (DW Toolkit Ch 11)
        position: Claim LINE is correct grain
        authority: high
        rationale: Preserves service-level detail
      - source: Blog Post XYZ
        position: Claim HEADER is simpler
        authority: low
        rationale: Sufficient for dashboards
    resolution: Recommend claim line grain
    exceptions: Header acceptable for executive dashboards only
```

## Creating New Patterns

### 1. Identify Pattern Candidate

Look for in source chapters:
- Reusable schema definitions
- Named approaches (e.g., "bridge table pattern")
- Best practices with concrete examples
- Decision frameworks

### 2. Extract Structure

```yaml
# Start with the basics
pattern:
  id: domain.category.name
  name: Clear Name
  description: One paragraph
  canonical:
    schema: |
      -- The core schema
```

### 3. Document Variations

Check multiple sources for alternative approaches:
- Different authors may have different methods
- Newer books may have evolved approaches
- Platform-specific optimizations

### 4. Add Sources

Always trace back to source material:
```yaml
sources:
  - book: The Data Warehouse Toolkit
    chapter: "Chapter 11: Healthcare"
    authority: high
    contribution: canonical
```

### 5. Test with Use Cases

Before finalizing, verify:
- Does the pattern generate valid code?
- Do variations handle their documented cases?
- Are extensions properly additive?

## Pattern Quality Checklist

- [ ] Clear problem statement
- [ ] When to use / when not to use documented
- [ ] Canonical implementation complete
- [ ] Variations have clear differentiation
- [ ] Extensions are truly additive
- [ ] Sources cited with authority level
- [ ] Related patterns linked
- [ ] Example generates valid code
