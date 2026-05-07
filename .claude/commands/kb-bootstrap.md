---
description: Generate a Project Bootstrap scaffold — composed Concept→Pattern→Procedure tree with placeholder files + sub-agent prompts ready to dispatch
---

You are generating a Project Bootstrap scaffold from the description
in `$ARGUMENTS`. The user's stated #1 generator: takes a project
request, resolves named technologies + patterns from the corpus,
plans a file tree, and emits placeholder files + per-file sub-agent
prompts.

## How to run

Call `generate_project_bootstrap` with:
- `description` — the project request
- `technologies` — list of tech names (Apache Kafka, etc.)
- `patterns` — list of pattern names (CQRS, Event-driven architecture)
- `project_name` (optional) — output folder name

Surface:
- the package folder
- `_build_plan.md` — file-by-file build plan + stack metrics
- `README.md`, `src/main.py`, `tests/test_smoke.py`, etc. (placeholders)
- `_sub_agent_prompts/prompt_NN_<file>.txt` — one prompt per file

## Sub-agent dispatch (manual v1 workflow)

After generation, dispatch one Task agent per prompt to fill in the
placeholders with real code. Each prompt is self-contained: it carries
the resolved stack context + procedure references + the file's purpose.

v1 ships the structural skeleton; sub-agent dispatch is the user's
manual follow-up (or a future v2 enhancement).
