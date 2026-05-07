---
description: Generate a Dialog — scripted exchange between Architect and Practitioner where their rankings diverge
---

You are generating a Dialog for the topic in `$ARGUMENTS`. The
generator finds concepts in the topic's neighborhood where two
characters score divergently; each divergence becomes a dialogue beat.

Default characters: Architect (Pattern-oriented, classical-era) and
Practitioner (Tool-oriented, current-doc).

## How to run

Call `generate_dialog` with `topic`. Surface:
- `dialogue.md` — script form
- `_stage_directions.md` — per-beat score breakdown showing why each
  character takes their position

Beats with low spread are filtered out — characters who agree don't
make for interesting dialogue.
