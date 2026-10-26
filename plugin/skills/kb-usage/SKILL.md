---
name: kb-usage
description: Use when the user asks to search their books or knowledge base (myPub, "my ePubs", "the kb"), compare how authors treat a concept, or find a concept's prerequisites. Explains which mypub-kb tool to call and how to weight it.
---

# Using the myPub knowledge base

myPub indexes Mark's technical ePub library (chapters) plus live doc
sources (Context7, DeepWiki, GitHub) in one local DuckDB catalog. The
`mypub-kb` MCP server from this plugin is the way in. It runs from the
myPub repo on this Mac, so it works the same from any project.

## Which tool

| The user wants | Call |
|---|---|
| Find material on a topic | `search_chapters(query, mode="interactive")` |
| Curated sources to build something from | `search_chapters(query, mode="generation")` |
| How different authors treat a concept | `compare_concept_across_authors(concept_name)` |
| What to learn first | `find_prerequisites(concept_name, max_depth)` |
| Pick between libraries that discovery flagged | `disambiguate_discovery(source, identifier, display_name, query_term)` |

`search_chapters` in interactive mode returns `primary`,
`corroborations`, `conflicts`, `all_scored`, `by_modality` and
`discovery`. Lead with `primary`, and mention `conflicts` when present.

## Weighting

Leave `weight_profile` at the default (`currency_critical_interactive`).
Use `foundational_interactive` only for timeless topics (algorithms,
classical CS, design-pattern theory), where authority and corroboration
matter more than recency.

## Live docs and discovery

When the corpus doesn't know a library, `search_chapters` probes
Context7, then DeepWiki, then GitHub, and may ingest what it finds. The
server runs its own Context7 client for this, so it does not depend on
a Context7 server being configured in the current project. If
`discovery` comes back `asked_user`, show the candidates, let the user
pick, call `disambiguate_discovery`, then search again. `/mypub:kb-discover`
runs that loop for you.

Ingest writes to the catalog, and the catalog allows one writer. If a
refresh or extraction job is running in the myPub repo, discovery can't
ingest until it finishes; searching still works.

## Generators

The `/mypub:kb-*` commands wrap the generator tools. Each one asks where
to write its package. Prefer a folder in the current project unless the
user wants myPub's shared `data/generated-packages/` archive.
