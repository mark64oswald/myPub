---
description: Generate a Refactoring Playbook — anti-pattern findings + recommended refactor targets with available procedures
---

You are generating a Refactoring Playbook for the topic in
`$ARGUMENTS`. The generator walks the topic's neighborhood for
Pattern-typed concepts with CONTRASTS_WITH neighbors; each pair
becomes an anti-pattern → refactor-target finding.

## How to run

Call `generate_refactoring_playbook` with `topic`. Surface:
- `_findings.md` — overview of all anti-pattern findings
- `refactors/<anti>-to-<target>.md` — per-finding refactor steps
