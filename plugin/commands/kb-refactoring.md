---
description: Generate a Refactoring Playbook — anti-pattern findings + recommended refactor targets with available procedures
---

You are generating a Refactoring Playbook for the topic in
`$ARGUMENTS`. The generator walks the topic's neighborhood for
Pattern-typed concepts with CONTRASTS_WITH neighbors; each pair
becomes an anti-pattern → refactor-target finding.

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

Call `generate_refactoring_playbook` with `topic`. Surface:
- `_findings.md` — overview of all anti-pattern findings
- `refactors/<anti>-to-<target>.md` — per-finding refactor steps
