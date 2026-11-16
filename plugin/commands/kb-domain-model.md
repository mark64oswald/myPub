---
description: Build a pattern-first domain model (model.json + UC DDL + DBML) from an industry model
argument-hint: "<business description> [--base model] [--industry NAME] [--domains a,b] [--catalog NAME]"
---

You are building a domain model for the business described in
`$ARGUMENTS`. The generator keeps the relevant part of a base industry
model (by default the Databricks gallery model for the industry, loaded in
the catalog) and rewrites it with the data model patterns from the
library: one Party behind every role, contact mechanisms, one owner per
concept, and status history.

```text
<package>/
  model.json  ddl/model.sql  model.dbml  changes.md  review.md  vibes.txt
  _prompts/refine-model.md  README.md
```

## Output location

Every generator tool takes an `output_root` argument. If the user named a
folder, pass its absolute path. Outside the myPub repo with no folder named,
ask once: `${CLAUDE_PROJECT_DIR}/mypub-packages/` (recommended) or myPub's
`data/generated-packages/`. Inside the myPub repo, omit it.

## How to run

### Step 1 — Base and scope

- `industry` names the gallery model (`healthcare`, `health_insurance`,
  `banking`...); `base` overrides it with a `model.json`, DDL or `ref:<id>`.
  `scripts/load_ref_models.py status` lists what's loaded.
- Scope comes from the description. If the user names domains or tables,
  pass `domains` / `tables` instead.
- `refactor` limits the rewrites (default: all four).

### Step 2 — Generate

Call `generate_domain_model` on `mypub-kb` with `description`, `industry`
or `base`, `name`, `domains`, `tables`, `catalog` and `output_root`.

### Step 3 — Report

- The grade before and after (`metadata.grade_before` → `grade_after`)
  and the changes from `changes.md`, one line each.
- What's still open in `review.md` (usually advice: classification tables,
  LLM-generated links kept inside the scope).

### Optional — refine with a subagent

If the description needs tables the base model lacks, dispatch one Agent
subagent with `_prompts/refine-model.md`. Then run `/kb-model-review` on the
refined `model.json`. To deploy, run `ddl/model.sql` in a Databricks SQL
warehouse, or open `model.json` in the Vibe Data Modeling app.

When the base is the Databricks gallery (Databricks License), keep the
package out of public repositories.
