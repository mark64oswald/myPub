# Phase 3.1 — Session 17 (2026-05-03)

Seventeenth session. **334 procedures** across 250 chapters
(density 1.34 — highest of any Phase 3.1 session). Mid-session rate-limit
required retries on 5 batches; all recovered cleanly after the spend
limit was raised.

## Scope

D-range continued: late Designing/Doing books, plus a heavy bloc of
Domain-Driven Design tactical books and DDD-related cookbook chapters
(very procedural). Several batches >20 procs each.

Densest batches: 11 (25), 19 (21), 17 (21), 23 (21), 2 (19), 12 (16).

## Results

```text
chapters dispatched   250
chapters landed       250 (after retries)
procedures written    334
chapters w/ ≥1 proc   146  (58%)
chapters w/ 0 procs   104  (42%)
```

## Disruptions

- 5 batches (11, 12, 14, 16, plus 3 chapters of 15) hit the user's
  spend limit mid-session and returned "You've hit your limit" errors.
  After the limit was raised, all 5 retries completed cleanly. No
  procedures lost.

## Cumulative across Phase 3.1 (sessions 1–17)

```text
total procedures              3,908  (+334)
chapters attempted            4,263  (~33.1% of corpus)
chapters w/ procedures        1,918  (+146)
```

## Stopping criterion (decided at end of s17)

User confirmed Phase 3.1 will halt when **two consecutive sessions land
below 0.4 procs/chapter**. Resumable later: any chapter with
`procedure_attempted_at IS NULL` will be auto-selected by future `prep`
runs. Phase 3.2 (incremental re-indexing) will additionally pick up
new/changed chapters via content_hash diff.

## Artifacts

- `/tmp/mypub-procedures/session-17/`
