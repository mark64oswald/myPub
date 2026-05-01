# Dimensional Modeling Skill

## Overview

This skill guides Claude in dimensional modeling using the myPub knowledge base.

**Status:** Placeholder - To be generated from indexed chapters

## Domain Coverage

Dimensional modeling is the foundation for analytical databases:

- Star and snowflake schemas
- Fact table types (transaction, periodic, accumulating)
- Dimension table design
- Slowly changing dimensions (SCD Types 1, 2, 3)
- Conformed dimensions
- Degenerate dimensions
- Role-playing dimensions
- Bridge tables and many-to-many relationships

## Key Concepts

*To be populated from concept graph:*

- [ ] Dimensional modeling fundamentals
- [ ] Grain definition
- [ ] Fact table types
- [ ] Dimension table design
- [ ] Slowly changing dimensions
- [ ] Conformed dimensions
- [ ] Star vs snowflake
- [ ] Aggregate tables

## Methodologies

### Kimball (Bottom-Up)

- Business process focus
- Dimensional bus architecture
- Conformed dimensions
- Star schema oriented

### Inmon (Top-Down)

- Enterprise data warehouse first
- 3NF normalized hub
- Dependent data marts
- Corporate information factory

### Data Vault

- Hub and spoke architecture
- Historical tracking built-in
- Auditability focus

## Source Books

Key books in collection:

- The Data Warehouse Toolkit (Kimball, Ross)
- Building the Data Warehouse (Inmon)
- Data Vault 2.0 (Linstedt)
- Deciphering Data Architectures (Inmon, Linstedt, Levins)
- *[Additional from your collection]*

## Generation Instructions

To populate this skill:

```bash
python scripts/generate_skill.py --topic "dimensional modeling" --output skills/domains/dimensional-modeling/
```
