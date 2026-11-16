---
description: Turn data product ideas into an ODPC catalog and an ODPG value graph
argument-hint: "<portfolio> --products \"A: desc\" ... [--use-cases \"U: desc => A\"] [--objectives \"O: desc => U\"]"
---

You are laying out a data product portfolio for `$ARGUMENTS` with the Open
Data Product standards: one ODPS draft per product, ODPC fragments (product
references, use cases, objectives with KPIs), the built ODPC catalog and an
ODPG graph showing how value flows.

```text
<package>/
  products/<p>.odps.yaml  contracts/<p>.odcs.yaml
  catalog/fragments/*.yaml  catalog/<portfolio>.odpc.yaml
  graph/<portfolio>.odpg.yaml  recipes/validate.recipe.yaml
  Makefile  README.md  guidance.md  _validation.md  _prompts/fill-portfolio.md
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

### Step 1 — Gather the lists

Ask for anything missing, in one message:

- **Products** (required): `"Name: one-line description"`.
- **Use cases**: `"Name: description => Product A, Product B"` (which products it uses).
- **Objectives**: `"Name: description => Use case X"` (which use cases support it).

Links are optional: without them every use case uses every product and every
objective is supported by every use case. Missing use cases or objectives get
defaults, which the README flags.

### Step 2 — Generate

Call `generate_dp_portfolio` on `mypub-kb` with `portfolio`, `products`,
`use_cases`, `objectives`, optional `platform`, and `output_root`.

### Step 3 — Report

- Counts from `metadata` (products, use cases, objectives) and whether the
  catalog was built (`catalog_built`; needs the SDK).
- The "How value flows" table from the README.
- `odpg-analyze` advice from `_validation.md` (e.g. ungoverned assets).
- Next: `/kb-dp-recipe` per product, `/kb-dp-review` once products are filled.
