# Healthcare corpora: PubMed Central and FHIR specs

Status: v1 implemented on 2026-10-27 (`scripts/jats.py`, `scripts/ingest_pmc.py`,
`scripts/fhir_spec.py`). Closes open item 3 ("Domain gaps") in
`docs/mypub-overview.pdf`.

## Why

The healthcare generators (standards translator, de-id bundle, FHIR IG
scaffold, integration channel) run on books plus a few DeepWiki repos.
Two kinds of source they need are missing:

- **Literature.** Research articles give the evidence behind clinical and
  informatics practice. They are newer than most books and come with
  structured metadata (journal, date, MeSH keywords).
- **Normative specs.** A FHIR resource definition is the ground truth for
  what an element is called, its cardinality and its binding. Books
  paraphrase it, and DeepWiki covers implementations, not the spec.

These two don't fit the existing ePub or live-doc fetchers, so each gets
its own ingestion path.

## What: two mappings, no schema changes

```
                     ┌──────────────── existing tables ────────────────┐
PMC JATS article ──► │ book     (source_path = "pmc:PMC<id>")          │──► embeddings,
                     │ chapter  (Abstract, Introduction, Methods, …)   │    extraction,
                     ├─────────────────────────────────────────────────┤    search,
FHIR NPM package ──► │ doc_source (source_type = "fhir_package")       │    generators
                     │ doc_snapshot → doc_section (one per resource)   │    (unchanged)
                     └─────────────────────────────────────────────────┘
```

| Decision | Choice | Reason |
|---|---|---|
| Article storage | `book` + `chapter` | Chapter-level search, concept extraction and generators work unchanged. Articles are stable texts like books, not volatile docs. |
| Chapter granularity | Top-level `<sec>`. Nested sections fold in as `###` headings. | Keeps a Methods section together, which matches book chapter sizes. |
| What's dropped | References, formulas, supplementary files, citation markers | Noise for embeddings and extraction. |
| Corpus marker | `source_path` prefix `pmc:` | No schema change. `WHERE source_path LIKE 'pmc:%'` separates articles from books. |
| Spec storage | `doc_source` / `doc_section` | Specs are versioned (R4 → R5, US Core 6 → 7), so they belong in the live-doc pipeline. That pipeline already has snapshots, hashing and currency tracking. |
| Spec section | One section per StructureDefinition | A resource is how people ask about FHIR ("what's in Encounter"). |
| Profiles | Optional (`--include-profiles`), differential only | US Core and other IGs matter, but their snapshots repeat the base resource. |
| Authority | FHIR packages get `authority_score` 0.9 | Normative source, above Context7 (0.60). |
| Access scope | PMC open-access subset only | Licences allow text mining. All public literature, no PHI. |

## How

```bash
# PubMed Central (fetch needs no catalog; index is a writer step)
.venv/bin/python3 scripts/ingest_pmc.py fetch --query "FHIR interoperability" \
    --max 50 --output-dir data/pmc/fhir-interop
.venv/bin/python3 scripts/ingest_pmc.py index --xml-dir data/pmc/fhir-interop
.venv/bin/python3 scripts/generate_embeddings.py
.venv/bin/python3 scripts/extract_batch.py prep --books <printed ids> --output-dir …

# FHIR packages
curl -L -o /tmp/r4-core.tgz https://packages.fhir.org/hl7.fhir.r4.core/4.0.1
.venv/bin/python3 scripts/fhir_spec.py render   --package-file /tmp/r4-core.tgz --out /tmp/r4.md
.venv/bin/python3 scripts/fhir_spec.py register --package-file /tmp/r4-core.tgz --name "FHIR R4 core"
.venv/bin/python3 scripts/refresh_docs.py prep  --source-id <id> …   # concept extraction
```

## Known gaps and next steps

- `refresh_docs.get_fetcher` doesn't know `fhir_package` yet. A
  `refresh --all` reports these sources as errors instead of skipping them.
  The fix is a one-line registration in `refresh_docs.py`. It's deferred
  until the live-docs refresh work that owns that file lands.
- Clinical-trial records (ClinicalTrials.gov API v2) follow the PMC pattern:
  one trial maps to a book, and its sections (eligibility, arms, outcomes)
  map to chapters. That needs a JSON mapper, not JATS.
- MeSH terms come from PubMed, not PMC. Adding an `efetch db=pubmed` call
  would give controlled subjects for the `book_subject` table that
  `enrich_book_metadata.py` creates.
- Neither the cloud container nor `ingest_pmc.py fetch` has been run against
  live NCBI or packages.fhir.org: the container's proxy blocks both. The
  parsers are tested on fixtures. Run the first real fetch on the Mac.
