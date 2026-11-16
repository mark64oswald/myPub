# Reference data models

myPub keeps industry data models as **structured metadata**, separate from
book chapters and doc sections, so they can be compared, reviewed and
composed against the data model patterns the library teaches.

## Why a separate layer

Industry models come in two kinds. Patterns (Silverston Vol. 3, Hay,
Blaha, Fowler) are small and abstract: Party, Party Role, Agreement,
Status, Classification, Observation. Industry models (Silverston Vol. 2,
OMOP, FHIR, the Databricks gallery) are large and concrete. The
Databricks Lakehouse Industry Data Models gallery alone has 6.6k tables
and 274k columns in its 41 MVMs. Indexing those as concepts would swamp
the concept graph with generated names. As rows with pattern links, they
answer questions like "which healthcare entities, from any source,
implement Party Role, and which source copies person identity onto each
role?"

## Sources

| source | what | license | how it loads |
|---|---|---|---|
| `databricks` | `model.json` per industry, MVM (default) or ECM | Databricks License: use with Databricks services, local catalog only | `load-databricks --repo <clone>` |
| `silverston` | *Data Model Resource Book Vol. 2* appendices (entities + attributes per industry) | book copyright, local only | `silverston-pages` → subagent transcription → `load-silverston` |
| `omop` | OMOP CDM 5.4 field/table CSVs (OHDSI) | Apache 2.0 | `load-omop` (fetched) |
| `user` / `generated` | any DDL (`CREATE TABLE` + FK constraints) | — | `load-ddl --file` |

The Silverston transcriptions and mapping runs live in gitignored
`data/ref_models/{silverston,mapping-runs}/`.

## Tables

```sql
ref_model            (ref_model_id, source, industry, flavour, version, name, origin_uri, license_note)
ref_entity           (ref_entity_id, ref_model_id, entity_key, domain, subdomain, name, description,
                      primary_key, standard_refs, n_attributes)
ref_attribute        (ref_attribute_id, ref_entity_id, ordinal, name, data_type, is_pk, fk_to, tags,
                      description, reference, generated_ref)
ref_relationship     (ref_model_id, from_entity_id, to_entity_id, via_attribute, kind)
ref_metric           (ref_metric_id, ref_model_id, ref_entity_id, name, n_dimensions, n_measures, definition)
ref_entity_embedding (ref_entity_id, embedding FLOAT[384])        -- entity "cards", MiniLM
ref_entity_concept   (ref_entity_id, pattern_key, concept_id, confidence, method, note)
```

`generated_ref` marks Databricks attributes whose reference reads "LLM
Generated Cross-Domain Link" (741 of 5,176 columns in the healthcare MVM).

## Patterns

`data/ref_models/patterns.yaml` is the curated pattern map: 23 patterns,
each with the library books that teach it and name/attribute heuristics.
Linking an entity to its patterns happens in two passes:

1. `classify`: deterministic heuristics (name match 0.60–0.65, attribute
   hints 0.45, "copies person identity" flags a Party Role at 0.45). It
   covers about 39% of the gallery's entities.
2. `map-prep` / `map-apply`: Claude Code subagents confirm, drop or add
   links in batches of 25 entity cards (`method='subagent'`, 0.5–0.95).
   Run in Remote Control on the Mac, on the subscription.

## Run on the Mac

```bash
git clone --depth 1 https://github.com/databricks-industry-solutions/lakehouse-industry-data-models \
    ~/Developer/lakehouse-industry-data-models
.venv/bin/python3 scripts/load_ref_models.py load-databricks --repo ~/Developer/lakehouse-industry-data-models
.venv/bin/python3 scripts/load_ref_models.py load-omop
.venv/bin/python3 scripts/load_ref_models.py silverston-pages --industry healthcare --industry insurance
#   one subagent per page: read pNN.prompt.md, write pNN.csv
.venv/bin/python3 scripts/load_ref_models.py load-silverston --industry healthcare --industry insurance
.venv/bin/python3 scripts/load_ref_models.py classify --all
.venv/bin/python3 scripts/load_ref_models.py embed --all
.venv/bin/python3 scripts/load_ref_models.py map-prep --industry healthcare --industry health_insurance \
    --industry insurance --source omop --out data/ref_models/mapping-runs/<date>
#   one subagent per batch: read batch_NNN.prompt.md, write batch_NNN.json
.venv/bin/python3 scripts/load_ref_models.py map-apply --dir data/ref_models/mapping-runs/<date>
.venv/bin/python3 scripts/load_ref_models.py status
```

Loading all 41 MVMs and OMOP takes about 10 seconds, and classifying them
another 10.

## Reviewing a model (`/kb-model-review`)

`model_review_gen.py` grades a data model against the patterns, not just
structural rules. Input is a Databricks `model.json` (or its folder), DDL,
or a loaded reference model (`databricks:healthcare`, `ref:<id>`).

| section | weight | what it flags |
|---|---|---|
| Party and roles | 3 | two or more role tables each carrying name/birth date/SSN |
| One owner per concept | 3 | a `single_owner` pattern (coverage) held in several domains; cross-domain name + column overlap |
| Contact mechanisms | 2 | address/phone/e-mail columns inline on several tables |
| Relationship provenance | 2 | FKs whose reference is "LLM Generated Cross-Domain Link" |
| Structure | 2 | FK cycles, tables without a PK, unconnected tables |
| Status history | 1 | transactional tables with a status column and no history table |
| Classification | 1 | free-text `_type`/`_code`/`_category` columns |
| Reference comparison | 1 | Silverston Vol. 2 / OMOP entities with no counterpart |

Each finding cites the pattern's books (via `patterns.yaml`) and carries a
vibe; `vibes.txt` lists them in order of severity. The healthcare MVM v2
grades **D**: four role tables copy person identity, insurance coverage is
held in six tables across four domains, nine tables inline addresses, and
741 of 1,056 FKs are LLM-generated links.

## Data products from a model (`/kb-dp-from-model`)

`model_products_gen.py` bridges a model to the Open Data Product
generators. A product is a slice of tables (`"Claims and denials: ... =>
claim.claim, claim.denial"`, or `domain:claim`). For each product it writes
an ODCS contract with every table as a schema object, ODPS `productKPIs`
from the model's metric-view measures (rates and averages first, targets
left as TODO), Unity Catalog DDL (`model_emit.uc_ddl`: parents first, FKs
outside the slice kept as comments) and the metric views. The portfolio
files (ODPC, ODPG, recipe, Makefile) come from `/kb-dp-portfolio`.
`model-notes.md` lists the model-wide review findings that touch each
product's tables, and `sample-data/healthsim-request.md` asks HealthSim
(PatientSim + MemberSim) for synthetic rows in FK order.

The built-in healthcare example over the Databricks MVM: Encounter
activity (5 tables), Claims and denials (6), Patient coverage (4), two use
cases (denial prevention, length of stay) and two objectives. All products,
contracts, the graph, recipe and built catalog pass the SDK and ODCS schema
checks. Because the output contains gallery content, it stays in the local
`data/generated-packages/`.
