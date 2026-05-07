---
description: Generate a Content Brief — research skeleton (outline + per-section sources + angle hints) for a blog, talk, design doc, or book chapter
---

You are generating a Content Brief for the topic in `$ARGUMENTS`. The
brief is a deterministic research foundation: rhetorical outline +
per-section anchor concept + ranked sources + CONTRASTS_WITH-derived
angle hints. The author fills in the prose; the brief is the structure.

## How to run

### Step 1 — Parse the request

- **Required:** topic
- **Optional:** `--format blog|talk|design-doc|chapter` (default blog)
- **Optional:** `--audience <text>` (default engineers)
- **Optional:** `--angle "<one-line thesis>"`

### Step 2 — Generate

Call `generate_content_brief` from `mypub-kb`.

### Step 3 — Surface the result

Response includes `n_sections`, `n_sources_total`, format, audience.

- **`package_id == -1`** → topic not resolved; suggest `/kb-discover`
- **`package_id > 0`** → surface the package folder, point at:
  - `_brief.md` (overview + how-to-use)
  - `outline.md` (full arc with theses)
  - `sections/<n>-<slug>.md` (per-section anchor + sources + angle hints)
  - `sources.md` (full bibliography)

## Guidance

- **Format choice changes the rhetorical arc.** Blog = hook→context→
  problem→approaches→comparison→recommendation→conclusion. Talk =
  opening story→problem→3 insights→demo→takeaways. Design doc =
  context→requirements→options→analysis→decision→consequences.
  Chapter = introduction→theory→worked examples→edge cases→summary.
- **Angle hints are CONTRASTS_WITH neighbors.** They're explicit
  alternatives the field has debated. The author should pick a
  position rather than enumerate both flatly.
- **This is v1 — no LLM prose.** The brief is the substrate; the
  writer composes prose on top. v2 will add a sub-agent prose layer.
- **Re-running with the same topic+format replaces in place.**