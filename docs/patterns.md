# Pattern Library Guide

## Overview

Patterns are reusable building blocks extracted from your ePub collection. They transform narrative knowledge into actionable, AI-consumable templates.

## Why Patterns?

### Without Patterns

```
User: "Build a claims fact table"

Claude: [Re-reads 15 pages of Kimball every time]
        [May interpret differently each time]
        [Uses many tokens for retrieval]
        [No consistency guarantee]
```

### With Patterns

```
User: "Build a claims fact table"

Claude: [Loads structured pattern definition]
        [Applies decision framework]
        [Generates consistent, correct schema]
        [Explains rationale from pattern]
```

## Pattern Structure

### Pattern YAML Schema

```yaml
pattern_id: healthcare.dimensional.fct_claim_line
name: Claim Line Fact Table
version: "1.0"
domain: healthcare
category: dimensional

description: |
  Fact table for healthcare claims at the claim line grain.
  Each row represents a single service/procedure on a claim.

problem_statement: |
  Need to analyze healthcare claims for cost, utilization, and outcomes
  while supporting drill-down to individual services.

sources:
  - book: "The Data Warehouse Toolkit"
    chapter: 11
    authority: high
    contribution: canonical

context:
  when_to_use:
    - Claims analytics and reporting
    - Provider performance analysis
    - Cost and utilization studies
    - Quality measure calculation
  
  when_not_to_use:
    - Real-time claims processing
    - OLTP systems
    - When only header-level aggregates needed

  prerequisites:
    - Understanding of dimensional modeling
    - Healthcare claims domain knowledge

schema:
  grain: "One row per claim line (service/procedure)"
  
  primary_key:
    name: claim_line_key
    type: BIGINT
    generation: surrogate_sequence
  
  foreign_keys:
    - name: member_key
      references: dim_member
      role: patient
    
    - name: provider_key
      references: dim_provider
      role: rendering_provider
    
    - name: facility_key
      references: dim_provider
      role: service_facility
    
    - name: service_date_key
      references: dim_date
      role: date_of_service
    
    - name: paid_date_key
      references: dim_date
      role: payment_date
    
    - name: procedure_key
      references: dim_procedure
    
    - name: primary_diagnosis_key
      references: dim_diagnosis
      role: primary_dx

  degenerate_dimensions:
    - name: claim_id
      type: VARCHAR(50)
      description: Source system claim identifier
    
    - name: claim_line_number
      type: INTEGER
      description: Line number within claim

  measures:
    - name: charge_amount
      type: DECIMAL(12,2)
      aggregation: SUM
      description: Billed amount
    
    - name: allowed_amount
      type: DECIMAL(12,2)
      aggregation: SUM
      description: Contracted allowed amount
    
    - name: paid_amount
      type: DECIMAL(12,2)
      aggregation: SUM
      description: Amount paid by payer
    
    - name: member_liability
      type: DECIMAL(12,2)
      aggregation: SUM
      description: Member cost sharing
    
    - name: units
      type: DECIMAL(8,2)
      aggregation: SUM
      description: Service units

template:
  sql: |
    CREATE TABLE fct_claim_line (
        -- Surrogate key
        claim_line_key BIGINT PRIMARY KEY,
        
        -- Foreign keys to dimensions
        member_key BIGINT NOT NULL REFERENCES dim_member(member_key),
        provider_key BIGINT NOT NULL REFERENCES dim_provider(provider_key),
        facility_key BIGINT REFERENCES dim_provider(provider_key),
        service_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
        paid_date_key INTEGER REFERENCES dim_date(date_key),
        procedure_key BIGINT NOT NULL REFERENCES dim_procedure(procedure_key),
        primary_diagnosis_key BIGINT REFERENCES dim_diagnosis(diagnosis_key),
        
        -- Degenerate dimensions
        claim_id VARCHAR(50) NOT NULL,
        claim_line_number INTEGER NOT NULL,
        
        -- Measures
        charge_amount DECIMAL(12,2),
        allowed_amount DECIMAL(12,2),
        paid_amount DECIMAL(12,2),
        member_liability DECIMAL(12,2),
        units DECIMAL(8,2),
        
        -- Audit
        source_system VARCHAR(50),
        loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    
    -- Indexes for common query patterns
    CREATE INDEX idx_fct_claim_line_member ON fct_claim_line(member_key);
    CREATE INDEX idx_fct_claim_line_provider ON fct_claim_line(provider_key);
    CREATE INDEX idx_fct_claim_line_service_date ON fct_claim_line(service_date_key);
    CREATE INDEX idx_fct_claim_line_claim ON fct_claim_line(claim_id);

test_cases:
  - name: Basic aggregation
    query: |
      SELECT 
          SUM(paid_amount) as total_paid,
          COUNT(*) as line_count
      FROM fct_claim_line
      WHERE service_date_key BETWEEN 20240101 AND 20241231
    expected: "Returns total paid and line count for 2024"

  - name: PMPM calculation
    query: |
      SELECT 
          d.year_month,
          SUM(f.paid_amount) / COUNT(DISTINCT f.member_key) as pmpm
      FROM fct_claim_line f
      JOIN dim_date d ON f.service_date_key = d.date_key
      GROUP BY d.year_month
    expected: "Returns per-member-per-month costs"

related_patterns:
  - dim_member
  - dim_provider
  - dim_diagnosis
  - dim_procedure
  - dim_date
  - metrics.pmpm
```

## Variations

Variations represent alternative valid approaches within a pattern.

### Example: Diagnosis Handling Variations

```yaml
# In fct_claim_line pattern
variations:
  - variation_id: positional_diagnosis
    name: Positional Diagnosis Columns
    description: |
      Store diagnoses in positional columns (dx_1, dx_2, ... dx_25).
      This is the canonical Kimball approach.
    
    when_to_use:
      - Primary diagnosis queries are most common
      - Simple star schema preferred
      - Limited number of diagnoses (≤25)
    
    when_not_to_use:
      - Need to query "any diagnosis contains X"
      - Unlimited diagnosis codes
      - Building for flexible analytics
    
    schema_additions:
      - name: dx_1_key
        type: BIGINT
        references: dim_diagnosis
      - name: dx_2_key
        type: BIGINT
        references: dim_diagnosis
      # ... up to dx_25_key

  - variation_id: bridge_table
    name: Diagnosis Bridge Table
    description: |
      Use a bridge table to handle multiple diagnoses per claim.
      Supports flexible "any diagnosis" queries.
    
    when_to_use:
      - Frequently query "find claims with diagnosis X anywhere"
      - Need unlimited diagnosis codes
      - HCC risk adjustment analysis
      - Quality measure calculation
    
    when_not_to_use:
      - Only need primary diagnosis
      - Query performance is critical
      - Simple reporting requirements
    
    schema_additions:
      tables:
        - name: bridge_claim_diagnosis
          columns:
            - claim_line_key BIGINT
            - diagnosis_key BIGINT
            - diagnosis_position INTEGER
            - poa_indicator VARCHAR(1)
          primary_key: [claim_line_key, diagnosis_key]

  - variation_id: array_column
    name: Array/JSON Diagnosis Column
    description: |
      Store diagnoses in an array or JSON column.
      Modern approach for cloud data warehouses.
    
    when_to_use:
      - Databricks, Snowflake, BigQuery
      - ML feature engineering
      - Schema flexibility needed
    
    when_not_to_use:
      - Traditional RDBMS
      - Need referential integrity
      - Complex joins required
    
    schema_additions:
      - name: diagnosis_codes
        type: ARRAY<VARCHAR(10)>
      - name: diagnosis_keys
        type: ARRAY<BIGINT>
```

## Extensions

Extensions add capabilities to a base pattern.

### Example: HCC Risk Adjustment Extension

```yaml
extension_id: hcc_risk_mapping
name: HCC Risk Adjustment
base_pattern: healthcare.dimensional.fct_claim_line
description: |
  Adds HCC (Hierarchical Condition Category) risk adjustment
  support for Medicare Advantage analysis.

when_required:
  - Medicare Advantage population
  - Risk adjustment analytics
  - RAF score calculation

schema_additions:
  tables:
    - name: xref_diagnosis_hcc
      description: Crosswalk from ICD-10 to HCC
      columns:
        - icd10_code VARCHAR(10)
        - hcc_code VARCHAR(10)
        - hcc_version VARCHAR(10)
        - effective_date DATE
        - end_date DATE
    
    - name: dim_hcc
      description: HCC dimension
      columns:
        - hcc_key BIGINT PRIMARY KEY
        - hcc_code VARCHAR(10)
        - hcc_description VARCHAR(200)
        - hcc_category VARCHAR(50)
        - coefficient DECIMAL(6,4)

  views:
    - name: v_claim_hcc_mapping
      description: Maps claim diagnoses to HCCs
      sql: |
        SELECT 
            f.claim_line_key,
            f.member_key,
            x.hcc_code,
            h.coefficient
        FROM fct_claim_line f
        JOIN bridge_claim_diagnosis bd ON f.claim_line_key = bd.claim_line_key
        JOIN dim_diagnosis d ON bd.diagnosis_key = d.diagnosis_key
        JOIN xref_diagnosis_hcc x ON d.icd10_code = x.icd10_code
        JOIN dim_hcc h ON x.hcc_code = h.hcc_code
```

## Decision Framework

Patterns include decision trees for Claude to select appropriate variations:

```yaml
decision_framework:
  - question: "What is the primary query pattern?"
    options:
      - answer: "Primary diagnosis only"
        recommendation: positional_diagnosis
      - answer: "Any diagnosis contains X"
        recommendation: bridge_table
      - answer: "ML feature engineering"
        recommendation: array_column
  
  - question: "What is the target platform?"
    options:
      - answer: "Traditional RDBMS (Postgres, SQL Server)"
        recommendation: positional_diagnosis or bridge_table
      - answer: "Cloud DW (Snowflake, Databricks, BigQuery)"
        recommendation: array_column acceptable
  
  - question: "Is HCC risk adjustment needed?"
    options:
      - answer: "Yes (Medicare Advantage)"
        recommendation: Add hcc_risk_mapping extension
        note: "Requires bridge_table or array_column variation"
      - answer: "No"
        recommendation: Base pattern sufficient
```

## Using Patterns in Claude

### Query Workflow

```
1. User: "Build a claims dimensional model for MA risk adjustment"

2. Claude queries patterns:
   SELECT * FROM patterns WHERE domain = 'healthcare' 
   AND category = 'dimensional';

3. Claude loads fct_claim_line pattern with variations

4. Claude applies decision framework:
   - MA → HCC extension required
   - HCC needs flexible dx queries → bridge_table variation

5. Claude generates:
   - Base fact table DDL
   - Bridge table DDL  
   - HCC extension tables
   - Explains rationale
```

### Pattern Retrieval SQL

```sql
-- Get pattern with all variations
SELECT 
    p.pattern_id,
    p.name,
    p.canonical_yaml
FROM patterns p
WHERE p.pattern_id = 'healthcare.dimensional.fct_claim_line';

-- Get variations
SELECT 
    v.variation_id,
    v.name,
    v.when_to_use,
    v.variation_yaml
FROM pattern_variations v
WHERE v.pattern_id = 'healthcare.dimensional.fct_claim_line';

-- Get required extensions for context
SELECT 
    e.extension_id,
    e.name,
    e.when_required,
    e.extension_yaml
FROM pattern_extensions e
WHERE e.pattern_id = 'healthcare.dimensional.fct_claim_line';
```

## Creating Patterns

### From Books

1. Identify reusable structures in chapters
2. Extract schema definitions
3. Parameterize examples
4. Document when to use/not use
5. Note variations from different sources
6. Add test cases

### Pattern Quality Checklist

- [ ] Clear problem statement
- [ ] Specific grain definition
- [ ] Complete schema (keys, FKs, measures)
- [ ] Working SQL template
- [ ] When to use / when not to use
- [ ] At least one variation documented
- [ ] Source references with authority
- [ ] Test cases included
