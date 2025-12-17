# Healthcare Patterns Skill

## Overview

This skill guides Claude in using healthcare-specific patterns from the myPub pattern library.

## Available Patterns

### Dimensional Patterns

| Pattern | Description |
|---------|-------------|
| `healthcare.dimensional.fct_claim_line` | Claims at line grain |
| `healthcare.dimensional.dim_member` | Member dimension (SCD-2) |
| `healthcare.dimensional.dim_provider` | Provider dimension |
| `healthcare.dimensional.dim_diagnosis` | Diagnosis dimension (ICD-10) |
| `healthcare.dimensional.dim_procedure` | Procedure dimension (CPT/HCPCS) |

### Entity Patterns

| Pattern | Description |
|---------|-------------|
| `healthcare.entities.claim_header` | Claim header structure |
| `healthcare.entities.eligibility` | Member eligibility |
| `healthcare.entities.enrollment` | Plan enrollment |

### Metric Patterns

| Pattern | Description |
|---------|-------------|
| `healthcare.metrics.pmpm` | Per-member-per-month |
| `healthcare.metrics.mlr` | Medical loss ratio |
| `healthcare.metrics.readmission` | Readmission rate |

### Reference Data Patterns

| Pattern | Description |
|---------|-------------|
| `healthcare.reference.icd10` | ICD-10 code hierarchy |
| `healthcare.reference.cpt_hcpcs` | Procedure code reference |
| `healthcare.reference.npi` | Provider NPI reference |

## Using Patterns

### Basic Retrieval

```sql
-- Find healthcare patterns
SELECT pattern_id, name, description
FROM patterns
WHERE domain = 'healthcare'
ORDER BY category, name;
```

### With Variations

```sql
-- Get pattern with variations
SELECT 
    p.pattern_id,
    p.canonical_yaml,
    pv.name as variation,
    pv.when_to_use
FROM patterns p
LEFT JOIN pattern_variations pv ON p.pattern_id = pv.pattern_id
WHERE p.pattern_id = 'healthcare.dimensional.fct_claim_line';
```

## Decision Frameworks

When building healthcare analytics, consider:

1. **Claim Grain**: Line vs header - usually line is correct
2. **Diagnosis Handling**: Positional vs bridge table
3. **Risk Adjustment**: Add HCC extension if Medicare Advantage
4. **Quality Measures**: Plan for HEDIS/Stars calculations

## Pattern Location

Pattern YAML files are in:
```
patterns/healthcare/
├── dimensional/
│   ├── fct_claim_line.yaml
│   ├── fct_claim_line_variations.yaml
│   ├── dim_member.yaml
│   └── ...
├── entities/
├── metrics/
└── reference-data/
```

## Generating Code from Patterns

Ask Claude:

> "Use the fct_claim_line pattern with the bridge_table variation 
> to generate a dimensional model for Medicare Advantage claims analysis."

Claude will:
1. Load the pattern and variation
2. Apply the HCC extension (detected from "Medicare Advantage")
3. Generate SQL DDL
4. Explain the design decisions
