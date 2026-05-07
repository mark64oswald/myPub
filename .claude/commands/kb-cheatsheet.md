---
description: Generate a Cheatsheet — one-page distilled reference of procedures for a library, tool, or technology
---

You are generating a Cheatsheet for the subject in `$ARGUMENTS`. The
cheatsheet pulls procedures linked to the subject (and optional
EXTENDS descendants), groups them by category (CRUD, Configuration,
Performance, Errors, Integration, Install, Operations, General), and
renders the canonical commands per entry plus a Gotchas section.

This is a **fully deterministic** generator — no sub-agents, no LLM
prose. Single MCP call produces the package.

## How to run

### Step 1 — Parse the request

The user's input is one or more of:

- **Required:** subject (e.g. "Delta Lake", "LangChain", "kubectl")
- **Optional:** `--extends-depth N` (default 1) — how far to walk
  EXTENDS to pull descendant concepts' procedures. 0 = subject only;
  1 catches specializations like "Spark Structured Streaming" under
  "Apache Spark"
- **Optional:** `--max-per-section N` (default 6) — procedures per
  category. Lower this (3-4) for a tighter one-page output;
  higher (8-10) for more comprehensive reference
- **Optional:** `--max-words N` (default 1200) — page-fit warning
  threshold

### Step 2 — Generate

Call the `generate_cheatsheet` MCP tool from `mypub-kb` with:

- `subject` — the user's input verbatim
- `extends_depth`, `max_per_section`, `max_words` per the user's flags

### Step 3 — Surface the result

The response carries `package_id`, `package_name`,
`n_procedures_total`, `n_clusters`, `file_paths`, `validation_issues`,
`notes`.

Branch on the response:

- **`package_id == -1`** — generation failed. Walk
  `validation_issues`. Most common: "subject concept not resolved" —
  suggest `/kb-discover <subject>` first.
- **`package_id > 0`** — generation succeeded. Tell the user:
  - the package folder (`<output_root>/<package_name>`)
  - procedure count, cluster count
  - if any `validation_issues` are warnings (page-fit, missing
    procedures), surface them
  - point them at `cheatsheet.md` (the deliverable),
    `_provenance.md` (per-line source pointer), `_gotchas.md`
    (extended failure modes)

## Guidance

- **Best for procedure-rich subjects.** The live catalog has 4,341
  procedures — well-covered subjects (Delta Lake: 67, LangChain: 55,
  Apache Spark: 29) produce useful output. Subjects with <5
  procedures will yield a thin cheatsheet; warn the user.
- **Page-fit warnings are honest signal, not failure.** The default
  1200-word target rarely fits a procedure-rich subject in one
  page. Either accept the longer output or tighten with
  `--max-per-section 3`.
- **The `# comment` lines in the rendered code blocks are
  narrative steps with no command field.** When a procedure's
  steps are descriptive ("In the UI, click X") rather than
  executable, the renderer shows them as comments to keep the
  copy-pasteable subset clear.
- **Re-running with the same subject replaces in place.** Catalog
  package_id stays stable; the disk folder is rewritten.