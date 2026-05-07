---
description: Generate a Tech Assessment — uniform feature-matrix comparison across N candidate technologies with deterministic recommendation
---

You are generating a Tech Assessment for the candidates in
`$ARGUMENTS`. The generator computes per-candidate metrics from the
corpus (chapter coverage, doc_section count, neighborhood size,
procedure count) and produces a comparison matrix + per-candidate
deep dives + a deterministic recommendation.

## How to run

### Step 1 — Parse the request

The user supplies:

- A title for the assessment ("Streaming engines", "Vector DBs")
- A list of candidate technology names

### Step 2 — Generate

Call `generate_tech_assessment` with `title` and `candidates: list[str]`.

### Step 3 — Surface the result

Response: `n_candidates`, `winner_score`, `file_paths`,
`validation_issues`.

- **`package_id == -1`** → no candidates resolved; suggest
  `/kb-discover` for missing names
- **`package_id > 0`** → surface:
  - `_matrix.md` (the comparison table)
  - `candidates/<slug>.md` (per-technology deep dive)
  - `_recommendation.md` (the deterministic pick + caveats)

## Guidance

- **The recommendation is corpus-coverage signal, not endorsement.**
  Newer technologies will under-rank because they have less corpus
  coverage. The recommendation file always includes a caveats section
  the user should read.
- **Composite score formula:** `chapters + 3·doc_sections + 0.5·min(neighbors, 30) + 1.5·procedures`.
  Doc sections weighted highest because current vendor docs are the
  strongest signal of "real-world used today".
- **Skipped candidates are flagged in `notes`.** A candidate that
  doesn't resolve gets a note rather than silently dropping.