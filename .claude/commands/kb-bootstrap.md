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
- `stack` (optional) — explicit language stack override. One of:
  `python | rust | node | typescript | java | go | csharp | ruby |
  generic`. Aliases `py / rs / js / ts / nodejs / golang / cs /
  dotnet / kotlin` also work. If omitted, the generator infers the
  stack from keywords in `description` (e.g., "Rust async server"
  → rust, "Spring Boot" → java, "Node.js Express" → node).

Surface:
- the package folder
- `_build_plan.md` — file-by-file build plan with the resolved stack
- Stack-appropriate files (Cargo.toml for Rust, pyproject.toml for
  Python, pom.xml for Java, go.mod for Go, etc.) — never Python by
  default for non-Python requests
- `_sub_agent_prompts/prompt_NN_<file>.txt` — one prompt per file

## Stack handling

The generator picks the right scaffolding for the target ecosystem.
If no language signal appears in the request AND no `stack` argument
is passed, the output is a minimal language-neutral skeleton (README
+ .gitignore only) rather than a Python-by-default scaffold. The
notes field on the response will say which stack was chosen and why.

## Sub-agent dispatch (manual v1 workflow)

After generation, dispatch one Task agent per prompt to fill in the
placeholders with real code. Each prompt is self-contained: it carries
the resolved stack context + procedure references + the file's purpose.

v1 ships the structural skeleton; sub-agent dispatch is the user's
manual follow-up (or a future v2 enhancement).
