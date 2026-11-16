---
description: Grade an ODPS data product file against the specs and the library, with fixes
argument-hint: "<path/to/product.odps.yaml> [--contract PATH] [--graph PATH]"
---

You are reviewing the ODPS 4.2 product file in `$ARGUMENTS`. The review is
read-only: it scores each section (missing / draft / filled), gives a letter
grade, and lists findings, each with a fix, the ODPS spec section and the
library procedure behind it.

```text
<package>/
  review.md  findings.json  _provenance.md  _prompts/apply-fixes.md
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

### Step 1 — Resolve the path

Pass the product file as an **absolute** path (`odps_path`). Add
`contract_path` and `graph_path` if the user names them; otherwise the
contract is found from the product's `contractURL` when it's a local path.

### Step 2 — Review

Call `review_dp_product` on `mypub-kb` with `odps_path`, `contract_path`,
`graph_path` and `output_root`.

### Step 3 — Report

- The grade and score from `metadata`, then errors and warnings from
  `review.md` (skip advice unless asked).
- Placeholder values (`TODO`, zero targets) are the usual cause of a low
  grade; say which sections they're in.

### Optional — apply the fixes

If the user asks, dispatch one Agent subagent with `_prompts/apply-fixes.md`.
It edits the product file, keeps it valid, and lists questions for any value
it would otherwise have to invent.
