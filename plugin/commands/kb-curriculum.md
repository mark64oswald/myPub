---
description: Generate a multi-week Curriculum — composes Learning Path + Tutorial + Pattern Catalog activities into per-week folders
---

You are generating a multi-week Curriculum for the topic in
`$ARGUMENTS`. The composite generator anchors each week on a Learning
Path stage; mid-weeks add tutorial activities; the last third adds
pattern catalog reviews.

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

Call `generate_curriculum` with `topic` and `n_weeks` (default 12).
Surface:
- `_curriculum.md` (overview + schedule)
- `weeks/week-N/_week.md` per week (anchor + concepts + reading list +
  links to run dependent generators)

Each week's `_week.md` includes pointers to run `/kb-tutorial` and
`/kb-pattern-catalog` for that week's anchor concept — the curriculum
itself is the schedule, the dependent generators provide the depth.
