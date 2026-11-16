---
description: Generate ODPR recipes (CI and agent) that check and build one data product
argument-hint: "<product description> [--odps PATH] [--contract PATH] [--graph PATH] [--mode ci|agent|both]"
---

You are writing Open Data Product Recipes (ODPR 1.0) for the product in
`$ARGUMENTS`: a **ci** recipe that validates the product on every change and an
**agent** recipe that hands the spec to a builder agent with a human-and-agent
review gate. The build plan comes from the KB's procedures for the platform.

```text
<package>/
  recipes/<p>-ci.recipe.yaml  recipes/<p>-agent.recipe.yaml
  handoff/README.md           DataProductRecipe sections (draft kind, Markdown)
  .github/workflows/odp-recipe.yml
  _provenance.md  _validation.md
```

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out tools/`, "save it under
   plugins/"): resolve it against the current project
   (`${CLAUDE_PROJECT_DIR}`) and pass the **absolute** path.
2. **No folder named, and the current project is not the myPub repo:**
   ask once, recommending the first option:
   - `${CLAUDE_PROJECT_DIR}/mypub-packages/` (keep it with this project)
   - myPub's own `data/generated-packages/` (the shared archive)
3. **Inside the myPub repo, or the user says "default":** omit
   `output_root`. The server then uses `$MYPUB_OUTPUT` if set, otherwise
   `<myPub>/data/generated-packages/`.

Always pass an absolute path. The server resolves relative paths against
the myPub repo, not against the project you are working in.

## How to run

### Step 1 — Find the product

If the user points at an ODPS file, pass its **absolute** path as `odps_path`,
and its path inside the repo as `product_file` (e.g. `products/churn.odps.yaml`).
Pass `contract_file` / `graph_file` as repo-relative paths when they exist.
Without a file, the tool writes an ODPS draft from the description.

### Step 2 — Generate

Call `generate_dp_recipe` on `mypub-kb` with `description`, the paths above,
`mode` (`ci`, `agent` or `both`), optional `platform`, and `output_root`.

### Step 3 — Report

- The recipe files and whether `_validation.md` shows them passing
  `recipe validate` and `recipe dry-run`.
- The delivery plan from `handoff/README.md` (numbered steps).
- Tell the user to copy `recipes/` and `.github/` into the product repo.
