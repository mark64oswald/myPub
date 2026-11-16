---
description: Generate an ODCS v3.1.0 data contract for an existing table (schema, quality rules, SLA)
argument-hint: "<table> [--ddl FILE] [--columns c1,c2] [--platform P] [--location cat.schema.table]"
---

You are writing a Bitol ODCS v3.1.0 data contract for the table in `$ARGUMENTS`.

```text
<package>/
  <name>.odcs.yaml           schema, quality rules, SLA properties, server, team, support
  README.md                  what is filled in and how to check it
  checklist.md               what the library says to settle before going active
  _provenance.md  _validation.md  _prompts/fill-contract.md
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

### Step 1 — Get the columns

The contract is only as good as its columns. In order of preference:

1. **DDL.** The user gave a `CREATE TABLE` (or a file with one): pass it as `ddl`.
2. **A live table.** If a database tool is connected (Databricks, Snowflake,
   Postgres…), get the table's DDL or `DESCRIBE` output: metadata only, never
   row values. Turn it into a `CREATE TABLE` and pass `ddl`.
3. **Column specs.** Otherwise pass `columns` as short specs:
   `"order_id bigint pk"`, `"email string required -- login email"`.

With none of these, the tool writes one placeholder column and says so.

### Step 2 — Generate

Call `generate_data_contract` on `mypub-kb` with `table`, `ddl` or `columns`,
and any of `platform`, `location` (`catalog.schema.table`), `purpose`,
`domain`, `owner`, `contract_name`, `output_root`.

### Step 3 — Report

- The contract path, the column and quality-rule counts from `metadata`.
- **Restricted columns** (`metadata.restricted_columns`): name them; they may
  hold personal or health data and need an access policy.
- The SLA values are drafts marked TODO: say so plainly.
- Next steps: `/kb-dp-recipe` to wire it into a product, or
  `open-data-products product wrap-contract` for an ODPS draft.

### Optional — fill in the TODOs

Dispatch one Agent subagent with `_prompts/fill-contract.md` if the user wants
the descriptions filled from the table's metadata. It must not invent numbers.
