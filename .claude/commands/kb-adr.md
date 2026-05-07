---
description: Generate an Architecture Decision Record from a question — context, options (from CONTRASTS_WITH), pros/cons, decision template
---

You are generating an ADR for the question in `$ARGUMENTS`. The
generator resolves the question's anchor concept, walks
CONTRASTS_WITH neighbors as candidate options, and produces a
fillable ADR template.

Fully deterministic. Single MCP call.

## How to run

### Step 1 — Parse the request

- **Required:** decision question (e.g. "Adopt event sourcing for billing service?")
- **Optional:** `--max-options N` (default 5)
- **Optional:** `--max-references N` (default 4) — top-K chapters per option

### Step 2 — Generate

Call `generate_adr` from `mypub-kb`.

### Step 3 — Surface the result

Response: `n_options`, `file_paths`, `validation_issues`, `notes`.

- **`package_id == -1`** → anchor concept not resolved; suggest `/kb-discover`
- **`package_id > 0`** → surface:
  - `adr.md` (Status / Context / Options / Pros & Cons / Decision template)
  - `_options.md` (per-option deep dive)
  - `_references.md` (source bibliography)

## Guidance

- **Single-option ADRs are honest.** If the corpus has no
  CONTRASTS_WITH neighbors for the anchor, the ADR has just the
  status-quo option. The validator warns; the file is still useful
  as a context-gathering exercise.
- **The decision section is intentionally blank.** This is a
  template; the human picks the option and writes the rationale.
- **Coverage counts in `_options.md` are a rough confidence proxy.**
  Use them as a tiebreaker, not as the decision itself.