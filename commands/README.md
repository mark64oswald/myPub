# myPub Custom Commands

These commands provide shortcuts for common knowledge base operations.

## /kb-search

Search the knowledge base for content on a topic.

### Usage
```
/kb-search <topic>
```

### Behavior

1. Search concepts table for matching concept
2. Search chapters table for topic in title/summary
3. Return:
   - Matching concepts with descriptions
   - Top 10 chapters ranked by relevance
   - Books that cover this topic extensively

### Example

```
/kb-search dimensional modeling
```

**Expected Output:**

```
📚 **Concept Found:** Dimensional Modeling
- Domain: data_engineering
- Related: Star Schema, Fact Table, Dimension Table

📖 **Top Chapters:**
1. The Data Warehouse Toolkit, Ch 3: Retail Sales (deep_dive, 12K tokens)
2. Building the Data Warehouse, Ch 8: Dimensional Design (explain, 8K tokens)
3. Fundamentals of Data Engineering, Ch 5: Data Modeling (explain, 6K tokens)

Would you like me to:
- Load a chapter for detailed explanation?
- Compare perspectives from different authors?
- Show prerequisites for this topic?
```

---

## /kb-compare

Compare how different authors treat a concept.

### Usage
```
/kb-compare <concept>
```

### Behavior

1. Find all chapters covering the concept
2. Group by author/book
3. Load key excerpts from each
4. Synthesize comparison

### Example

```
/kb-compare slowly changing dimensions
```

---

## /kb-prereqs

Show learning prerequisites for a concept.

### Usage
```
/kb-prereqs <concept>
```

### Behavior

1. Query concept relationships (REQUIRES)
2. Build recursive prerequisite chain
3. Suggest reading order
4. Link to recommended chapters

### Example

```
/kb-prereqs star schema
```

**Expected Output:**

```
📊 **Prerequisites for Star Schema:**

Level 3 (Start here):
  └─ SQL Fundamentals
     📖 Read: Learning SQL, Ch 2: Creating and Populating Tables

Level 2:
  └─ Data Warehouse Concepts
     📖 Read: Building the Data Warehouse, Ch 1: Introduction

Level 1:
  ├─ Dimensional Modeling
  │  📖 Read: The Data Warehouse Toolkit, Ch 1: Introduction
  │
  └─ Fact Table / Dimension Table
     📖 Read: The Data Warehouse Toolkit, Ch 2: Core Concepts

Level 0 (Target):
  └─ Star Schema
     📖 Read: The Data Warehouse Toolkit, Ch 3: Retail Sales
```

---

## /kb-pattern

Retrieve a pattern with all variations and extensions.

### Usage
```
/kb-pattern <pattern_id>
```

### Behavior

1. Load pattern from patterns table
2. Include all variations
3. Include applicable extensions
4. Show decision framework

### Example

```
/kb-pattern healthcare.dimensional.fct_claim_line
```

---

## /kb-generate-skill

Generate a skill file for a topic from relevant chapters.

### Usage
```
/kb-generate-skill <topic> [--output <path>]
```

### Behavior

1. Find all relevant chapters
2. Load top 3-5 chapters
3. Synthesize into SKILL.md format
4. Save to skills/generated/ or specified path
5. Update skills table

### Example

```
/kb-generate-skill "Change Data Capture"
```

---

## /kb-learning-path

Generate a reading order to learn a concept.

### Usage
```
/kb-learning-path <target_concept>
```

### Behavior

1. Find prerequisites recursively
2. Order by dependency
3. Select best chapter for each concept
4. Output as ordered reading list

### Example

```
/kb-learning-path dimensional_modeling
```

---

## /kb-index

Index a new book or re-index existing.

### Usage
```
/kb-index <book_filename>
/kb-index --all
```

### Behavior

1. Run indexing script
2. Report results
3. Suggest concept extraction

---

## /kb-stats

Show knowledge base statistics.

### Usage
```
/kb-stats
```

### Example Output

```
📊 **Knowledge Base Statistics**

Books: 345
Chapters: 4,892
Total Tokens: ~12.4M

Concepts: 234
  - data_engineering: 89
  - healthcare: 45
  - ai_ml: 52
  - other: 48

Patterns: 42
  - healthcare: 18
  - dimensional: 12
  - data_engineering: 12

Skills: 8
  - domains: 4
  - patterns: 2
  - generated: 2
```
