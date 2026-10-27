---
description: Hybrid search with explicit discovery loop — handles asked_user disambiguation interactively
---

You are running a knowledge-base query that may need to grow the corpus on
the fly. The user has provided a search term (in $ARGUMENTS or in the
message body). Your job is to drive the full retrieve → discover →
disambiguate → re-retrieve loop.

## How to run

### Step 1 — Initial search

Call the `search_chapters` MCP tool from the `mypub-kb` server with the
user's query. Use `mode="interactive"` and let `auto_discover` default to
True. Don't pass a `weight_profile` unless the query is clearly about a
timeless concept (then use `foundational_interactive`).

### Step 2 — Branch on the discovery field

The response carries a `discovery` array with one entry per query term the
gap detector flagged. There are four possible decisions:

- **No discovery field, or discovery == []** → the corpus already knew the
  topic. Skip to Step 5 and present results.

- **decision == "ingested"** → auto-discovery confidently matched a
  library/repo, ingested it, and the modality fan-out re-ran with the new
  content included. Tell the user what was ingested (source, identifier,
  display name from `chosen_match`), then go to Step 5 with the existing
  response — the fresh content is already in `primary` / `corroborations`.

- **decision == "asked_user"** → ambiguous match. Go to Step 3.

- **decision == "discarded"** → no source had a confident match. Tell the
  user the term wasn't found in the corpus or live doc sources, suggest
  reformulating, and stop.

### Step 3 — Present asked_user candidates

When discovery returned `asked_user`, the response carries a `candidates`
list under that outcome. Present them to the user in a compact, ranked
form: name, identifier, score (if any), and a one-line description.
Number them so the user can pick by index.

```
For "Spark" the candidates were:
  [1] Apache Spark    /apache/spark      score=85   Unified analytics engine
  [2] Spark NLP       /johnsnowlabs/spark-nlp  score=42  NLP library for Apache Spark
  [3] sparkjava       /perwendel/spark   score=12   Micro web framework for Java

Which one matches what you're asking about? (1-3, or 'none' to skip)
```

Wait for the user's pick.

### Step 4 — Disambiguate and re-search

Once the user picks, call `disambiguate_discovery` from `mypub-kb` with:

- `source` — the source field of the asked_user outcome (e.g., `"context7"`)
- `identifier` — the picked candidate's `identifier`
- `display_name` — the picked candidate's `name`
- `query_term` — the original query term

The response tells you whether this was a fresh ingest (`status="ingested"`)
or already-known (`status="already_present"`), and gives you
`section_count`. Surface that briefly.

Then re-run `search_chapters` with the same query — the freshly-ingested
content is now in the corpus and will surface in the modality fan-out.

If the user picks `"none"`, stop without ingesting.

### Step 5 — Present results

For interactive mode, the response shape is:

- `primary` — the top-scored result
- `corroborations` — up to 5 supporting results
- `conflicts` — up to 5 results that CONTRADICT the primary (alignment edges)
- `all_scored` — full ranked list
- `by_modality` — per-modality buckets for transparency

Show the user:

1. **Primary**, with: title, source (book or doc_source_name), an excerpt,
   and the `combined_score` + `components` so they can see WHY it ranked
   first (recency vs authority vs corroboration).
2. **Corroborations** — title and source only; one line each.
3. **Conflicts** — if non-empty, call them out prominently. The §8.4 case
   ("book superseded by current docs") shows up here. Tell the user what
   the conflict is and which source is "more current."
4. **By-modality summary** — one count per modality so the user can see
   which signal dominated. Useful for query-quality intuition.

If `discovery` had any entry, also report what got auto-ingested vs.
asked_user vs. discarded so the user knows the corpus shape changed.

## Guidance

- **Don't auto-pick** an asked_user candidate. The whole point of the
  asked_user decision is to keep the knowledge base clean — the user
  picks, not you.
- **One discovery cycle per invocation**. If the re-search after
  disambiguate is still thin, surface that honestly rather than recursing
  into another discovery round in the same turn.
- **Surface conflicts even when the user didn't ask for them**. If the
  primary has CONTRADICTS edges from current docs, that's exactly the
  signal the §8.4 ranking story is built to detect — don't bury it.
- **Default the `weight_profile`** unless the user explicitly mentions
  "timeless" or "foundational" or asks about classical CS topics. The
  `currency_critical_interactive` default is right for most queries.