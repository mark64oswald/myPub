# Open items runbook (2026-10-27)

How to run the fixes for the open items in section 5 of
`docs/mypub-overview.pdf`. Everything here needs the catalog, so it runs on
the Mac in `~/Developer/projects/myPub`.

Ground rules:

- **Reads use an APFS clone.** `cp -c data/catalog.ddb /tmp/catalog-ro.ddb`.
  DuckDB allows one writer, and the clone is instant and free.
- **Writes wait for the writer slot.** Only steps marked *writer* touch
  `data/catalog.ddb`. Stop the `mypub-kb` server and any refresh job first.
- **LLM work runs as Claude Code Task sub-agents on the subscription.** No
  paid API, no Batch API, no `claude -p`.

## 1. Book metadata (subjects, dates, publishers)

```bash
cp -c data/catalog.ddb /tmp/catalog-ro.ddb
.venv/bin/python3 scripts/enrich_book_metadata.py lookup \
    --catalog /tmp/catalog-ro.ddb --output-dir /tmp/mypub-enrich
# review /tmp/mypub-enrich/enrichment.json, then (writer):
.venv/bin/python3 scripts/enrich_book_metadata.py apply \
    --output-dir /tmp/mypub-enrich --write
```

Only empty fields are filled. Subjects land in the `book_subject` side
table: DuckDB runs an UPDATE of the `book.subjects` LIST column as a delete
and an insert, which trips the chapter → book foreign key.

## 2. CONTRADICTS re-alignment, then Migration Guide and Currency Report

```bash
.venv/bin/python3 scripts/realign_contradictions.py prep \
    --catalog /tmp/catalog-ro.ddb --output-dir /tmp/mypub-realign
```

In Claude Code, dispatch one Task sub-agent per entry in
`manifest.json["batches"]`. Each reads its prompt files and writes the JSON
answer to the matching `result_path`. Then:

```bash
.venv/bin/python3 scripts/realign_contradictions.py process --output-dir /tmp/mypub-realign
# dry run prints the vote summary; then (writer):
.venv/bin/python3 scripts/realign_contradictions.py process --output-dir /tmp/mypub-realign --apply
```

Each pair is judged 3 times under different framings. A vote counts only if
its book and doc quotes really occur in the excerpts. A strict majority of
the planned samples decides the outcome: re-labelled, re-scored, or deleted
as UNRELATED. Every vote is kept in `alignment_vote`. After applying, re-run
`/kb-migration-guide` and `/kb-currency-report` for the same topics as the
stale packages.

## 3. Prose layer for Dialog, Tutorial, Slides and Content Brief

```bash
.venv/bin/python3 scripts/prose_pass.py prep --catalog /tmp/catalog-ro.ddb \
    --generator dialog|tutorial|slide_deck|content_brief --package <name> --output-dir /tmp/mypub-prose/<name>
# one Task sub-agent: read prompt.txt, write result.md
.venv/bin/python3 scripts/prose_pass.py process --output-dir /tmp/mypub-prose/<name>
```

`process` rejects a result that drops a heading or cites an excerpt it
wasn't given. The deterministic file is kept as `_deterministic/<file>`.
Slides rewrite `speaker-notes.md`. Content Brief sends all `sections/*.md`
files as one draft with `<!-- file: ... -->` markers, and `process` splits
the result back into the section files (a dropped marker is rejected).

## 4. Bootstrap v2 loop

```bash
.venv/bin/python3 scripts/bootstrap_v2.py status   data/generated-packages/<project>
# sub-agents fill the pending files
.venv/bin/python3 scripts/bootstrap_v2.py validate data/generated-packages/<project>
```

`validate` scans for leftover placeholders, then runs the stack's tests
(pytest, cargo test, npm test, go test ./..., mvn verify, dotnet test, or
rspec). Then it runs the entry point as a smoke check. Add `_smoke.json`
with `{"expect_stdout": "..."}` to check the output too. Each failure writes
a retry prompt to `_sub_agent_prompts/retry/`, and the next `status` lists
those prompts first. Repeat until the package passes.

## 5. Healthcare corpora (PubMed Central, FHIR specs)

See `docs/healthcare-corpora-design.md`. `ingest_pmc.py index` and
`fhir_spec.py register` are writer steps.

## 6. Generator fixes that apply on the next run

- Learning Path and Tutorial: prerequisites below 0.25 cosine similarity to
  the target are dropped and not expanded (`min_similarity`, 0 turns this
  off). Stage titles come from the most on-topic concept.
- Quickstart: code blocks are ranked by install or hello-world intent. CI
  YAML, venv and build-tool internals are filtered out.
- Refactoring Playbook: a contrast becomes a finding only when the target
  has a procedure or an IMPLEMENTS edge.

Re-run the Delta Lake tutorial, the CDC learning path, the PyMuPDF
quickstart and the refactoring playbook from the overview to compare.
