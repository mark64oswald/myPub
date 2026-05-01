# Super Prompt: Build System Using Patterns

## Goal

Build a data system (schema, pipeline, queries) using patterns from the knowledge base.

## Prerequisites

- Pattern library populated
- Relevant domain skills available
- Target platform known (Databricks, Snowflake, PostgreSQL, etc.)

## Variables

- `{{SYSTEM_DESCRIPTION}}`: What to build
- `{{DOMAIN}}`: Primary domain (healthcare, finance, etc.)
- `{{PLATFORM}}`: Target platform
- `{{CONSTRAINTS}}`: Any specific constraints or requirements

## Prompt

````text
I need to build a data system using my knowledge base patterns.

**System:** {{SYSTEM_DESCRIPTION}}
**Domain:** {{DOMAIN}}
**Platform:** {{PLATFORM}}
**Constraints:** {{CONSTRAINTS}}

Please:

1. **Understand Requirements:**

   Analyze the system description and identify:
   - Core entities needed
   - Key relationships
   - Primary use cases (reporting, analytics, ML, etc.)
   - Performance requirements
   - Regulatory considerations

2. **Find Relevant Patterns:**

   ```sql

   -- Find applicable patterns
   SELECT
       p.pattern_id,
       p.name,
       p.description,
       p.category
   FROM patterns p
   WHERE p.domain = '{{DOMAIN}}'
   ORDER BY p.category;

   -- Check for variations
   SELECT
       pv.pattern_id,
       pv.name AS variation,
       pv.when_to_use
   FROM pattern_variations pv
   WHERE pv.pattern_id IN (SELECT pattern_id FROM patterns WHERE domain = '{{DOMAIN}}');

   ```text

3. **Load and Analyze Patterns:**

   For each relevant pattern:
   - Load the canonical YAML
   - Review variations
   - Check extensions
   - Apply decision framework to select approach

4. **Design the System:**

   Create a design document:

   ```markdown

   # System Design: {{SYSTEM_DESCRIPTION}}

   ## Overview

   [What this system does]

   ## Architecture

   [High-level architecture diagram in text/mermaid]

   ## Data Model

   ### Entities

   | Entity | Pattern Used | Variation | Notes |
   |--------|--------------|-----------|-------|
   | ... | ... | ... | ... |

   ### Relationships

   [ERD or relationship description]

   ## Design Decisions

   ### Decision 1: [Topic]

   **Options considered:**
   - Option A (from pattern X)
   - Option B (from pattern Y)

   **Selected:** Option A
   **Rationale:** [Why, based on pattern guidance]

   ## Implementation

   [Generated code below]

   ```text

5. **Generate Code:**

   Using the selected patterns, generate:

   a. **DDL (Schema)**
   ```sql

   -- Generated from patterns: [list pattern_ids]
   -- Platform: {{PLATFORM}}

   CREATE TABLE ...

   ```text

   b. **Sample Queries** (if applicable)
   ```sql

   -- Common query patterns

   ```text

   c. **Pipeline Code** (if applicable)
   ```python

   # ETL/ELT logic

   ```text

6. **Validate Against Patterns:**

   For each generated component:
   - [ ] Follows pattern structure
   - [ ] Appropriate grain selected
   - [ ] Surrogate keys implemented correctly
   - [ ] Relationships properly defined
   - [ ] Platform-specific optimizations applied

7. **Document Sources:**

   ```markdown

   ## Pattern Attribution

   | Component | Pattern | Source |
   |-----------|---------|--------|
   | fct_claims | healthcare.dimensional.fct_claim_line | DW Toolkit Ch 11 |
   | dim_member | healthcare.dimensional.dim_member | FDE Ch 8 |

   ```text

8. **Identify Gaps:**

   Note any requirements not covered by existing patterns:
   - Custom components needed
   - Pattern extensions to create
   - Additional research needed
````

## Expected Output

- Complete design document
- Generated DDL/code
- Design decision rationale
- Pattern attribution
- Gap analysis

## Platform-Specific Notes

### Databricks

- Use Delta Lake table format
- Consider liquid clustering
- Use Unity Catalog naming

### Snowflake

- Use VARIANT for semi-structured
- Consider clustering keys
- Use streams for CDC

### PostgreSQL

- Standard SQL syntax
- Consider partitioning for large tables
- Index strategy

## Example Use Cases

1. **Healthcare Claims Warehouse**
   - Patterns: fct_claim_line, dim_member, dim_provider, dim_diagnosis
   - Extensions: hcc_risk_mapping (if Medicare)

2. **Data Pipeline with CDC**
   - Patterns: cdc, medallion_architecture, staging
   - Variations: Debezium vs log-based CDC

3. **Analytics Data Mart**
   - Patterns: periodic_snapshot, aggregate_fact
   - Consider: pre-aggregation strategy
