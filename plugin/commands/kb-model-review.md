---
description: Review a data model against the library's data model patterns, with paste-ready change requests
argument-hint: "<model.json | ddl.sql | folder | databricks:<industry> | ref:<id>> [--industry NAME] [--no-compare]"
---

You are reviewing the data model in `$ARGUMENTS` against the data model
patterns the library teaches (Silverston, Hay, Fowler, Blaha). Structural
rules (naming, PK suffixes, no cycles) are what modeling agents already
enforce; this review checks whether the model *uses the patterns*: one
Party behind every role, one owner per business concept, contact
mechanisms, status history, classification tables, and relationships that
have a real source rather than an LLM's justification.

```text
<package>/
  review.md  findings.json  vibes.txt  _provenance.md
```

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder**: resolve it against the current project
   (`${CLAUDE_PROJECT_DIR}`) and pass the **absolute** path.
2. **No folder named, and the current project is not the myPub repo:**
   ask once, recommending the first option:
   - `${CLAUDE_PROJECT_DIR}/mypub-packages/` (keep it with this project)
   - myPub's own `data/generated-packages/` (the shared archive)
3. **Inside the myPub repo, or the user says "default":** omit
   `output_root`.

Always pass an absolute path.

## How to run

### Step 1 — Resolve the model

- A file or folder: pass its **absolute** path. A folder with a
  `model.json` (a Databricks gallery model) uses it; otherwise every
  `.sql` file under it is read as DDL.
- A reference model already in the catalog: `databricks:healthcare`,
  `databricks:banking:ecm`, `omop:healthcare`, or `ref:<id>`
  (`scripts/load_ref_models.py status` lists them). Loaded models use the
  confirmed pattern links from the mapping run; files use the heuristics.

### Step 2 — Review

Call `review_data_model` on `mypub-kb` with `model`, `industry` (only if
the path doesn't name one), `compare` (false for `--no-compare`) and
`output_root`.

### Step 3 — Report

- The grade from `metadata`, then the errors and warnings from
  `review.md`, each with its books. Skip advice unless asked.
- Point to `vibes.txt`: numbered change requests written to paste into
  the Databricks Vibe Data Modeling agent (or any modeling agent) as-is.

### Optional — apply the vibes

If the user is working on DDL in this project and asks, dispatch one Agent
subagent with `vibes.txt` and the DDL to apply the changes, then run the
review again to confirm the grade moved.
