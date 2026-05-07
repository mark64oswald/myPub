---
description: Generate a multi-week Curriculum — composes Learning Path + Tutorial + Pattern Catalog activities into per-week folders
---

You are generating a multi-week Curriculum for the topic in
`$ARGUMENTS`. The composite generator anchors each week on a Learning
Path stage; mid-weeks add tutorial activities; the last third adds
pattern catalog reviews.

## How to run

Call `generate_curriculum` with `topic` and `n_weeks` (default 12).
Surface:
- `_curriculum.md` (overview + schedule)
- `weeks/week-N/_week.md` per week (anchor + concepts + reading list +
  links to run dependent generators)

Each week's `_week.md` includes pointers to run `/kb-tutorial` and
`/kb-pattern-catalog` for that week's anchor concept — the curriculum
itself is the schedule, the dependent generators provide the depth.
