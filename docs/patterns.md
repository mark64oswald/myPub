# Pattern Library Guide

## What is a Pattern?

A pattern is a reusable building block extracted from your ePub collection. Unlike raw chapter content, patterns are:

- **Structured**: Schema definitions, code templates, decision frameworks
- **Parameterized**: Can be adapted to specific contexts
- **Validated**: Tested approaches with known trade-offs
- **Sourced**: Traceable to authoritative books/chapters

## Pattern Structure

Each pattern is stored as YAML with this structure:

```yaml
pattern_id: domain.category.name
name: Human-Readable Name
description: What this pattern does
domain: healthcare | dimensional_modeling | data_engineering | ...
category: facts | dimensions | ingestion | metrics | ...

problem_statement: |
  What problem does this pattern solve?
  When would you use it?

context:
  when_to_use:
    - Scenario 1
    - Scenario 2
  when_not_to_use:
    - Anti-pattern scenario
  prerequisites:
    - required_concept_1
    - required_concept_2

schema:
  description: The data structure
  columns:
    - name: column_name
      type: data_type
      description: What this column represents
      nullable: true|false
  primary_key: [column1, column2]
  foreign_keys:
    - columns: [fk_column]
      references: other_table(pk_column)

implementation:
  sql_template: |
    -- Parameterized SQL template
    CREATE TABLE ${table_name} (
      ...
    );
  example: |
    -- Concrete example
    CREATE TABLE fct_claim_line (
      ...
    );

test_cases:
  - name: Test case description
    query: SELECT ...
    expected: Description of expected result

related_patterns:
  - pattern_id: related.pattern.id
    relationship: uses | alternative_to | extends

sources:
  - book: Book Title
    chapter: Chapter Name
    authority: high | medium | low
    contribution: canonical | variation | extension
```

## Pattern Variations

Variations represent alternative valid approaches within a pattern:

```yaml
variations:
  - variation_id: pattern_id:variation_name
    name: Variation Name
    description: How this differs from canonical
    when_to_use: |
      Context where this variation is preferred
    when_not_to_use: |
      Context where canonical is better
    schema_changes:
      - description of schema differences
    implementation:
      sql_template: |
        -- Variation-specific template
```

### Example: Diagnosis Handling Variations

```yaml
pattern_id: healthcare.dimensional.diagnosis_handling

canonical:
  name: Positional Columns
  description: Store diagnoses in numbered columns (dx_1, dx_2, ...)
  when_to_use: Primary diagnosis queries, simple star schema

variations:
  - variation_id: healthcare.dimensional.diagnosis_handling:bridge_table
    name: Bridge Table
    description: Many-to-many relationship via bridge
    when_to_use: "'Any diagnosis contains X' queries, unlimited diagnoses"
    
  - variation_id: healthcare.dimensional.diagnosis_handling:array_column
    name: Array/JSON Column
    description: Store diagnoses as array or JSON
    when_to_use: "ML features, modern platforms (Spark, Snowflake)"
```

## Pattern Extensions

Extensions add capabilities to base patterns:

```yaml
extensions:
  - extension_id: healthcare.dimensional.hcc_risk_mapping
    name: HCC Risk Mapping
    description: Add diagnosis-to-HCC crosswalk for risk adjustment
    extends_pattern: healthcare.dimensional.fct_claim_line
    when_required: Medicare Advantage analysis, risk adjustment
    additional_tables:
      - xref_diagnosis_hcc
      - dim_hcc
    schema_additions:
      - column: hcc_flag
        description: Whether any diagnosis maps to an HCC
```

## Using Patterns

### Finding Patterns

```sql
-- By domain and category
SELECT pattern_id, name, description
FROM patterns
WHERE domain = 'healthcare'
  AND category = 'dimensional';

-- By keyword
SELECT pattern_id, name, description
FROM patterns
WHERE name ILIKE '%claim%'
   OR description ILIKE '%claim%';
```

### Loading a Pattern

```sql
-- Get canonical pattern
SELECT pattern_id, name, canonical_yaml
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

### Decision Framework

When Claude loads a pattern, it should:

1. **Analyze context** from user request
2. **Check variation conditions** against context
3. **Select appropriate variation** or canonical
4. **Identify required extensions**
5. **Generate code** from templates
6. **Explain rationale** for choices

Example decision flow:
```
Request: "Build claims dimensional model for Medicare Advantage"
         ↓
Context clues: "Medicare Advantage" → needs HCC analysis
         ↓
Pattern: healthcare.dimensional.fct_claim_line
         ↓
Check variations: HCC queries need "any dx contains" → bridge_table
         ↓
Check extensions: Medicare Advantage → hcc_risk_mapping required
         ↓
Generate: fct_claim_line + bridge_claim_diagnosis + dim_diagnosis + xref_diagnosis_hcc + dim_hcc
```

## Example Patterns

### Healthcare Claim Line Fact

```yaml
pattern_id: healthcare.dimensional.fct_claim_line
name: Healthcare Claim Line Fact
domain: healthcare
category: dimensional

problem_statement: |
  Model healthcare claims at the service line level for analytics.
  The claim line is the correct grain per Kimball (DW Toolkit Ch 11).

schema:
  columns:
    - name: claim_line_sk
      type: BIGINT
      description: Surrogate key
    - name: claim_id
      type: VARCHAR
      description: Degenerate dimension - source claim identifier
    - name: line_number
      type: INTEGER
      description: Line number within claim
    - name: member_sk
      type: BIGINT
      description: FK to dim_member
    - name: provider_sk
      type: BIGINT
      description: FK to dim_provider
    - name: service_date_sk
      type: INTEGER
      description: FK to dim_date
    - name: procedure_sk
      type: BIGINT
      description: FK to dim_procedure
    - name: place_of_service_sk
      type: INTEGER
      description: FK to dim_place_of_service
    - name: charge_amount
      type: DECIMAL(12,2)
      description: Billed amount
    - name: allowed_amount
      type: DECIMAL(12,2)
      description: Contracted allowed amount
    - name: paid_amount
      type: DECIMAL(12,2)
      description: Amount paid
    - name: units
      type: INTEGER
      description: Service units

implementation:
  sql_template: |
    CREATE TABLE fct_claim_line (
        claim_line_sk       BIGINT PRIMARY KEY,
        claim_id            VARCHAR(50) NOT NULL,
        line_number         INTEGER NOT NULL,
        member_sk           BIGINT REFERENCES dim_member(member_sk),
        provider_sk         BIGINT REFERENCES dim_provider(provider_sk),
        service_date_sk     INTEGER REFERENCES dim_date(date_sk),
        procedure_sk        BIGINT REFERENCES dim_procedure(procedure_sk),
        place_of_service_sk INTEGER REFERENCES dim_place_of_service(place_of_service_sk),
        charge_amount       DECIMAL(12,2),
        allowed_amount      DECIMAL(12,2),
        paid_amount         DECIMAL(12,2),
        units               INTEGER,
        -- Audit columns
        source_system       VARCHAR(50),
        load_timestamp      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

sources:
  - book: The Data Warehouse Toolkit
    chapter: Chapter 11 - Healthcare
    authority: high
    contribution: canonical
```

## Creating Patterns

### From Books

1. Identify chapters with reusable structures
2. Extract the core pattern
3. Parameterize for reuse
4. Document variations found in other sources
5. Add decision framework

### From Experience

1. Document a working implementation
2. Abstract to pattern form
3. Identify variation points
4. Add test cases
5. Link to sources that informed the approach

## Pattern Quality Checklist

- [ ] Clear problem statement
- [ ] When to use / when not to use
- [ ] Complete schema definition
- [ ] Working SQL template
- [ ] Concrete example
- [ ] At least one test case
- [ ] Sources cited
- [ ] Variations documented if applicable
