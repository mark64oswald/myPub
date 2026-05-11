---
description: Generate a Quickstart — install + hello-world + verify for a single library (first-contact artifact)
---

You are generating a **Quickstart** for the library in `$ARGUMENTS`.
The output is a single-page artifact for someone trying the library
for the first time: what it is, how to install it, the smallest
runnable example, and how to verify it works.

Distinct from:
- `/kb-cheatsheet` — assumes you're already using the library
- `/kb-tutorial` — sequenced multi-stage learning track
- `/kb-bootstrap` — composed project scaffold (multiple components)

## How to run

### Step 1 — Parse the request

The user supplies a single library name. Optionally a language hint
when the library has examples in multiple languages.

Example requests:
- `/kb-quickstart pypdf`
- `/kb-quickstart Tokio`
- `/kb-quickstart Axum --language Rust`

### Step 2 — Generate

Call `generate_quickstart` with:
- `library` (required, str): resolved against `doc_source.name`
- `language_hint` (optional, str): "Python", "Rust", "JavaScript", etc.

### Step 3 — Surface the result

Response carries: `package_id`, `package_name`, `doc_source_id`,
`doc_source_name`, `n_install_blocks`, `n_hello_blocks`,
`n_verify_blocks`, `has_framing`, `file_paths`, `validation_issues`,
`notes`.

- **`package_id == -1`** → library not found in `doc_source`; the
  validation_issues will explain. Common fix: run `/kb-discover`
  first to seed the library, then re-run.
- **`package_id > 0`** → surface:
  - `_quickstart.md` — the artifact (start here)
  - `hello_world/main.<ext>` — runnable code, when extractable

## Guidance

- **Install / hello-world / verify** are extracted from the latest
  doc_source snapshot via keyword matching (`pip install`,
  `cargo add`, `hello world`, `getting started`, etc.). The
  generator prefers code blocks over prose.
- **Framing ("what it is")** comes from a book chapter that names
  the library if one exists, else the first substantive doc section.
- **`language_hint`** affects which code block is chosen as the
  primary Hello World when the doc has multiple languages.
- **Empty / thin output** → the doc_source's sections may not
  contain explicit install or hello-world headings. Try
  `language_hint`, or fall back to `/kb-cheatsheet` for a reference
  artifact that doesn't depend on those specific topic keywords.
- **The Quickstart is intentionally minimal.** For depth, chain into
  `/kb-cheatsheet`, `/kb-tutorial`, or `/kb-landscape`.