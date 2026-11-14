---
description: Refresh live docs on demand — see what's stale, then re-fetch only what changed
argument-hint: "[site name | domain | all] [--check] [--extract]"
---

You are refreshing myPub's live docs. Freshness is on demand: nothing runs
on a schedule. The work is deterministic Python; no LLM calls happen unless
the user asks for extraction at the end, and then only through Claude Code
sub-agents (never the Anthropic API, Batch API or `claude -p`).

All commands run against the myPub repo and catalog, not the project you
are in. Below, `PY` means `"${CLAUDE_PLUGIN_ROOT}/../.venv/bin/python3"` and
`S` means `"${CLAUDE_PLUGIN_ROOT}/../scripts"`.

## Two kinds of sources

- **Doc sites** — whole docs sites enumerated from a sitemap or llms.txt,
  one doc_source per page (`databricks`, `databricks-api`, `claude-code`, `claude-platform`, `claude-docs`, `mcp`, `agent-skills`). Refreshed by
  `doc_sites.py`, which uses conditional GETs, compares a hash of the
  extracted text, re-ingests only changed pages, and retires pages that
  left the index.
- **Single sources** — everything else (Context7, DeepWiki, GitHub files,
  single web pages). Refreshed by `refresh_docs.py refresh`.

## Steps

1. **Show the state.** Always start read-only:

   ```bash
   PY S/doc_sites.py status
   PY S/doc_sites.py stale            # single sources past their TTL
   ```

   With an argument, narrow it: a site name → `status --site NAME`; a
   domain → `stale --domain DOMAIN`.

2. **Plan (dry run).** For the site(s) in scope:

   ```bash
   PY S/doc_sites.py plan --site NAME          # index diff + overdue pages
   PY S/doc_sites.py plan --site NAME --check  # also ask the server what changed
   ```

   `--check` makes one conditional request per known page (polite rate,
   several minutes for a large site); add `--limit N` to sample the N
   pages checked longest ago. For single sources:

   ```bash
   PY S/refresh_docs.py refresh --stale --dry-run [--domain DOMAIN]
   ```

   Report the counts to the user: new, removed, overdue, changed (with
   `--check`), and whether retiring was blocked (`retire_blocked`: the
   index lost more than 10% of pages at once, which usually means a broken
   sitemap). Ask before going on unless they already said to sync.

3. **Sync.** Writer — the catalog has one writer at a time; the scripts stop
   the read-only kb-mcp server themselves and it restarts on next use.
   Before a first sync of a large site (thousands of pages), make an APFS
   clone backup: `cp -c data/catalog.ddb data/catalog_pre-<site>-<date>.ddb`.

   ```bash
   PY S/doc_sites.py sync --site NAME [--max-pages N] [--prep-extract]
   PY S/refresh_docs.py refresh --stale [--domain DOMAIN] [--no-extract]
   ```

   A full first sync of docs.databricks.com takes about an hour at the
   preset's 2 requests/second; run it in the background and report when it
   finishes. `--max-pages` caps a run; the next run picks up the pages
   checked longest ago. `--only new|changed|removed` narrows what a run
   does. `--force-retire` overrides the 10% retire guard — only after the
   user has confirmed the pages really are gone.

4. **Report** from the printed JSON / `sync_report.json`: pages registered,
   not modified (304), unchanged (hash equal), changed, retired, errors;
   sections new / kept / dropped. Kept sections kept their embeddings and
   extracted concepts.

5. **Extraction (optional, ask first).** `--prep-extract` (sites) or a
   refresh without `--no-extract` (single sources) writes prompt
   manifests for just the new or changed sections. If the user wants
   concepts and procedures extracted, dispatch plain Agent sub-agents over
   the manifest in parallel batches (not the Workflow tool), then:

   ```bash
   PY S/refresh_docs.py process --output-dir <manifest dir>
   ```

## Adding a site

```bash
PY S/doc_sites.py add-site --preset databricks     # or databricks-api, claude-code, claude-platform, claude-docs, mcp, agent-skills
PY S/doc_sites.py add-site --name NAME --index-url https://…/sitemap.xml \
    --page-format html --selector article --include 'https://…/en/*' \
    --exclude '*/archive/*' --name-prefix 'Vendor' --ttl 14
```

Sites whose pages are served as Markdown use `--page-format markdown` (and
`--md-url-suffix .md` if the index lists HTML URLs). Check a new site with
`plan` and a small `sync --max-pages 20` before a full run.
