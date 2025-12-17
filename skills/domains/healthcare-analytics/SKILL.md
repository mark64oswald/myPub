# Healthcare Analytics Skill

## Overview

This skill guides Claude in healthcare data analytics using the myPub knowledge base.

**Status:** Placeholder - To be generated from indexed chapters

## Domain Coverage

Healthcare analytics spans:

- Claims data (professional, institutional, pharmacy)
- Eligibility and enrollment
- Provider networks
- Clinical data (when available)
- Quality measures (HEDIS, Stars, CAHPS)
- Risk adjustment (HCC, RAF)
- Value-based care metrics

## Key Concepts

*To be populated from concept graph:*

- [ ] Healthcare claims structure
- [ ] Member eligibility
- [ ] Provider dimensions
- [ ] Diagnosis coding (ICD-10)
- [ ] Procedure coding (CPT, HCPCS)
- [ ] Place of service
- [ ] Risk adjustment (HCC)
- [ ] Quality measures

## Available Patterns

See `patterns/healthcare/` for:

- `dimensional/fct_claim_line` - Claims fact table
- `dimensional/dim_member` - Member dimension (SCD-2)
- `dimensional/dim_provider` - Provider dimension
- `metrics/pmpm` - Per-member-per-month calculation
- `metrics/mlr` - Medical loss ratio

## Common Tasks

### Cost Analysis
*Query patterns and chapters for:*
- PMPM trending
- Cost by service category
- High-cost member identification

### Utilization Analysis
*Query patterns and chapters for:*
- ER utilization rates
- Inpatient admissions
- Readmission rates

### Quality Measures
*Query patterns and chapters for:*
- HEDIS measure calculation
- Stars ratings
- Quality gaps in care

## Source Books

*To be populated from catalog:*

- The Data Warehouse Toolkit (Ch 11: Healthcare)
- Healthcare Data Analytics
- Claims Data Analytics
- *[Additional from your collection]*

## Generation Instructions

To populate this skill:

```bash
python scripts/generate_skill.py --topic "healthcare claims" --output skills/domains/healthcare-analytics/
```

Then have Claude synthesize from the gathered chapters.
