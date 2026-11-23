---
description: Generate a working shadcn/ui app (dashboard, list + detail, forms) from a data product or a schema
argument-hint: "<what the app is for> [--product PATH] [--ddl FILE] [--contract FILE] [--model model.json] [--stack vite|nextjs] [--base base|radix]"
---

You are generating a shadcn/ui app for `$ARGUMENTS` with the `generate_ui` tool.

```text
<package>/
  ui-spec.json  ui-spec.schema.json   the app as data, and the schema it passed
  app-src/                            overlay for the project's src/ (or root): components/kb,
                                      views, data (types, sample, source stub), lib, App.tsx or app/
  setup.sh                            shadcn init + add + npm install + copy overlay (--build)
  design-notes.md                     each region's pattern and why: rule, books, shadcn docs
  README.md  _provenance.md  _validation.md  _prompts/refine-ui.md
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
3. **Inside the myPub repo, or the user says "default":** omit `output_root`.

Always pass an absolute path.

## How to run

### Step 1 — Pick the input

| The user has… | Mode | Pass |
|---|---|---|
| An ODPS product or a `/kb-dp-from-model` package | data-product | `product_path` (+ `product` when the folder holds several) |
| `CREATE TABLE` statements (text or file) | schema | `ddl` |
| An ODCS contract | schema | `contract` |
| A Databricks industry `model.json` | schema | `model_json` + `tables` |
| Metric view SQL, with any schema input | schema + dashboard | `metrics_sql` |

For a live table, fetch its DDL or `DESCRIBE` output through a connected
database tool: **metadata only, never row values**. Use `tables` to keep the
app to the tables that matter (six list pages at most).

### Step 2 — Stack and primitives

- `stack`: `vite` (default; a static SPA with hash routes) or `nextjs`
  (App Router, one route per page). Use `nextjs` when the user says Next,
  server rendering, Vercel, or Route Handlers for the data.
- `base`: `base` (Base UI, the shadcn default) or `radix` when the project
  already uses Radix.

### Step 3 — Generate

Call `generate_ui` on `mypub-kb` with `request`, the input, `stack`, `base`,
and `title` / `app_name` when the user gave them.

### Step 4 — Report

- Pages from `metadata.pages` (dashboard / list) and the stack + base.
- `validation_issues`: spec errors are blocking; missing shadcn doc pages or
  no book matches are warnings (grounding is thinner, the app still works).
- Restricted columns are masked and sample data is generated: say so, and
  that `src/data/source.ts` is where real data plugs in.
- How to run it: `./setup.sh` then `npm run dev` (needs Node 20+ and network).

### Optional — build it

If the user wants proof it compiles and Node is available, run
`./setup.sh <app> --build` in the package folder. Fix type errors in the
generated files, not in `components/ui/`.

### Optional — polish

Dispatch one Agent subagent with `_prompts/refine-ui.md` for copy and layout
polish. It must keep imports, data-source signatures and masking.
