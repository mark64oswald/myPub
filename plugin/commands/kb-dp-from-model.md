---
description: Cut Open Data Products (ODPS + ODCS + DDL + KPIs) out of an industry data model
argument-hint: "<model.json | ddl | databricks:<industry>> [products...] [--catalog NAME]"
---

You are building a data product portfolio from the data model in
`$ARGUMENTS`. Each product is a slice of the model's tables; the package
gets real ODCS contracts (one schema object per table), ODPS products whose
KPIs come from the model's metric views, Unity Catalog DDL, the ODPC catalog
and ODPG value graph, model-review notes and a HealthSim sample-data request.

```text
<package>/
  products/  contracts/  ddl/  metrics/  catalog/  graph/  recipes/
  model-notes.md  sample-data/healthsim-request.md  README.md  _validation.md
```

## Output location

Every generator tool takes an `output_root` argument. If the user named a
folder, pass its absolute path. Outside the myPub repo with no folder named,
ask once: `${CLAUDE_PROJECT_DIR}/mypub-packages/` (recommended) or myPub's
`data/generated-packages/`. Inside the myPub repo, omit it.

## How to run

### Step 1 — Model and products

- The model: same forms as `/kb-model-review` (a `model.json` or its
  folder, DDL, `databricks:healthcare`, `ref:<id>`).
- Products: ask the user which products they want, as
  `"Name: description => domain.table, table, domain:<name>"`. Offer the
  model's domains as a menu. For a healthcare model the user can say
  "example" (or give nothing) to get the built-in three: Encounter
  activity, Claims and denials, Patient coverage.
- Optional use cases (`=> Product, Product`) and objectives
  (`=> Use case`); without them each product gets a default.

### Step 2 — Generate

Call `generate_dp_from_model` on `mypub-kb` with `model`, `products`,
`use_cases`, `objectives`, `catalog` (Unity Catalog catalog name, if
given) and `output_root`.

### Step 3 — Report

- Products and their tables, from `metadata.tables`; any unmatched table
  names from the validation issues.
- The errors in `model-notes.md` (they're model problems the product
  inherits) and whether `_validation.md` passes.
- Next steps: fill the TODO targets in `productKPIs`; run
  `sample-data/healthsim-request.md` with the healthsim skill; load with
  `ddl/*.sql`; `/kb-dp-review` each product; `/kb-dp-recipe` for a build plan.

The package contains the source model's content. For the Databricks
gallery (Databricks License), keep it out of public repositories.
