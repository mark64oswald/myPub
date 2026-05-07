---
description: Generate a Currency Report — volatility audit across doc_source snapshots over time
---

You are generating a Currency Report for the scope in `$ARGUMENTS`
(or for all sources by default).

## How to run

Call `generate_currency_report` with optional `source_filter` for a
single source. Surface:
- `_report.md` (volatility-ranked source list)
- `sources/<slug>.md` (per-source timeline)

Volatility = `(distinct_hashes − 1) × log(snapshot_count + 1)`. Single
snapshot → volatility 0; high churn between refreshes → high
volatility. Useful for spotting which docs change frequently.
