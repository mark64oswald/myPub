---
description: Generate a Library Landscape / Ecosystem Map — multi-job-to-be-done × multi-candidate orientation doc for a domain
---

You are generating a **Library Landscape** for the domain in
`$ARGUMENTS`. The generator anchors on a domain concept, discovers (or
accepts) candidate libraries, optionally splits the domain into
jobs-to-be-done, scores each (candidate, job) cell from concept-graph
overlap, and renders a discovery doc.

Audience: someone new to the domain who wants orientation — *what's out
there, how do I think about it, where do I start*. Distinct from
`/kb-tech-assessment` (one decision, N candidates) and `/kb-bootstrap`
(opinionated project scaffold).

## How to run

### Step 1 — Parse the request

The user supplies a domain string. Optionally:
- A list of candidate library names (else auto-discover from
  doc_sources by concept-overlap with the domain anchor).
- A list of jobs-to-be-done (else default to a single "Overview" job).

Example requests:
- `/kb-landscape PDF processing libraries`
- `/kb-landscape Rust async runtimes`
- `/kb-landscape Vector databases`

If the user supplies explicit candidates, treat the comma-separated
or bullet list after a colon as `candidates=[...]`. If they supply
jobs ("jobs: extract, create, manipulate"), parse those as
`jobs=[...]`.

### Step 2 — Generate

Call `generate_library_landscape` with:
- `domain` (required, str)
- `candidates` (optional, list[str])
- `jobs` (optional, list[str])
- `max_candidates` (default 12)

### Step 3 — Surface the result

Response carries: `package_id`, `package_name`, `n_candidates`,
`n_jobs`, `anchor_concept_ids`, `file_paths`, `validation_issues`,
`notes`.

- **`package_id == -1`** → anchor unresolved or zero candidates; the
  `validation_issues` will explain. Common fixes: run `/kb-discover`
  on the domain term first; or pass explicit `candidates=[...]`.
- **`package_id > 0`** → surface the files:
  - `_landscape.md` — the full assembled doc (start here)
  - `_matrix.md` — jobs × candidates coverage table
  - `_decisions.md` — "if your job is X, start with Y" shortcuts
  - `jobs/<slug>.md` — per-job detail (one per job)
- **`validation_issues`** with severity=warning: candidates with zero
  coverage on all jobs, or jobs with no covered candidates. These are
  signals that the auto-discovery or jobs labels may need tightening.

## Guidance

- **Scoring**: each (candidate, job) cell shows raw shared-concept
  count and a rarity-weighted score. Rare/specific shared concepts
  weight more; common-to-everyone ones weight less. **Ranking uses
  the weighted score**, not raw coverage — this eliminates the bias
  toward libraries with larger doc_source neighborhoods.
- **Coverage is a corpus-overlap signal, not an endorsement.** A
  library with rich doc_source coverage but light book coverage will
  rank high on doc_section concepts but low on chapter framing.
  Always read the `_decisions.md` caveats.
- **Generic verbs in job names are noisy.** "Create", "build",
  "manage" appear in all docs and dilute the cluster signal. Prefer
  specific job labels: "render PDF from HTML" beats "create PDF".
- **Auto-discovery is anchored on the domain concept's 1-hop
  neighborhood.** If `/kb-landscape "Rust async"` returns weak
  candidates, try `/kb-discover "Rust async"` first to confirm the
  anchor resolves, then re-run the landscape.
- **Explicit `candidates` + explicit `jobs` is the strongest mode.**
  Auto-discovery is best for "what's out there"; explicit lists are
  best when you already know the field.
- **Empty cells (coverage = 0) are kept in the matrix.** They're an
  evidence signal — a candidate that's strong overall but has nothing
  for a particular job tells you something about its scope.
- **Cross-cutting metadata** (license, language, momentum) is *not*
  yet rendered in v1 — License-Risk Audit will be a follow-up
  generator with its own license-field schema add.