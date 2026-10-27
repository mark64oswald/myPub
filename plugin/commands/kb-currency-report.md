---
description: Generate a Currency Report — volatility audit across doc_source snapshots over time
---

You are generating a Currency Report for the scope in `$ARGUMENTS`
(or for all sources by default).

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out docs/kb`, "save it under
   design/"): resolve it against the current project
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

Call `generate_currency_report` with optional `source_filter` for a
single source. Surface:
- `_report.md` (volatility-ranked source list)
- `sources/<slug>.md` (per-source timeline)

Volatility = `(distinct_hashes − 1) × log(snapshot_count + 1)`. Single
snapshot → volatility 0; high churn between refreshes → high
volatility. Useful for spotting which docs change frequently.
