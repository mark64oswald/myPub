# Customization

Tuning ranking, adding character profiles, and writing a new generator.

[← back to top-level README](../README.md) · [Architecture ↗](architecture.md) · [Generators ↗](generators.md)

---

## Weight profiles

The five-factor ranker (`relevance × recency × authority × corroboration × doc_alignment`) is parameterized by `weight_profile`. Pick a profile to bias the ranker for the kind of question being asked.

### Built-in profiles

```python
WEIGHT_PROFILES = {
    "currency_critical_interactive": {
        "rec":  0.30,   "doc":  0.20,   "rel":  0.30,
        "corr": 0.10,   "auth": 0.10,
    },
    "foundational_interactive": {
        "rec":  0.05,   "doc":  0.05,   "rel":  0.40,
        "corr": 0.20,   "auth": 0.30,
    },
    "balanced_interactive": {
        "rec":  0.10,   "doc":  0.10,   "rel":  0.45,
        "corr": 0.15,   "auth": 0.20,
    },
    "skill_recent_doc_anchored": {
        "rec":  0.40,   "doc":  0.30,   "rel":  0.20,
        "corr": 0.05,   "auth": 0.05,
    },
    "skill_consensus_synthesis": {
        "rec":  0.10,   "doc":  0.10,   "rel":  0.35,
        "corr": 0.30,   "auth": 0.15,
    },
}
```

(`rec` = recency, `doc` = doc_alignment, `rel` = relevance, `corr` = corroboration, `auth` = authority.)

### When to pick which

| Use case | Profile | Why |
|---|---|---|
| "What does Kafka do *now*?" | `currency_critical_interactive` | Recency + doc_alignment dominate; old book content can't drown out current docs |
| "Explain the CAP theorem" | `foundational_interactive` | Recency irrelevant; relevance and authority should win; corroboration is welcome |
| Daily Q&A, mixed | `balanced_interactive` | Best default for natural-language search |
| Skill package for current vendor | `skill_recent_doc_anchored` | Pin to current docs; books supplement |
| Skill package for foundational topic | `skill_consensus_synthesis` | Restrict to where book + doc agree |

### Defining a new profile

Profiles live in [`mcp-servers/kb-mcp/ranking.py`](../mcp-servers/kb-mcp/ranking.py) under `WEIGHT_PROFILES`. Add a new key:

```python
WEIGHT_PROFILES["my_custom_profile"] = {
    "rec":  0.50,
    "doc":  0.10,
    "rel":  0.30,
    "corr": 0.05,
    "auth": 0.05,
}
```

The five values must sum to 1.0 (the ranker doesn't currently enforce this — it's a discipline). Restart the MCP server for the new profile to be picked up.

### Other ranking knobs

| Knob | Default | What it does |
|---|---|---|
| `TITLE_COVERAGE_BOOST` | 0.8 | Multiplier applied when query tokens appear in chapter title; high values reward "exact title" matches |
| `SCORING_POOL_MULTIPLIER` | 5 | Ranker sees `limit * 5` candidates before truncation; higher = more accurate ranking but slower |
| `RRF_K` | 60 | Reciprocal-rank-fusion smoothing constant |
| `PER_MODALITY_LIMIT` | 20 | Candidates pulled from each of FTS / VSS / graph before merging |

These constants live near the top of `mcp-servers/kb-mcp/server.py` and `ranking.py`.

### Calibration status

Current values were settled by working backward from observed bad-result patterns during dogfooding (May 2026). Real eval data from sustained dogfooding should drive the next iteration. Specific uncertainties tracked in [docs/operations.md](operations.md#deferred-work):

- Is `rec=0.10` too low for the default? Watch for currency-critical queries getting wrong answers under `balanced_interactive`.
- Is `TITLE_COVERAGE_BOOST=0.8` too aggressive? Watch for chapters with cute / metaphorical titles winning over content-strong matches.
- Are the `skill_*` profiles correctly calibrated for the Skills Factory? Untested against new runs since the May 2026 calibration.

---

## Character profiles

Characters are *view functions* over the ranking engine. The same chapter can be filtered, re-weighted, or rejected by different characters — and that's how Dialog and Author Panel get their voice.

### Built-in characters

| Character | Filter |
|---|---|
| **Architect** | Concepts of type `Pattern`, `Concept`, `Algorithm`; chapters that emphasize "why" and "tradeoff" framing |
| **Practitioner** | Chapters with linked procedures; emphasis on `failure_modes` and configuration specifics |

Definitions live in [`mcp-servers/kb-mcp/character.py`](../mcp-servers/kb-mcp/character.py).

### Anatomy of a character

A character is a class implementing two methods:

```python
class Character:
    def filter(self, candidates: list[Candidate]) -> list[Candidate]:
        """Drop candidates this character wouldn't bring up."""

    def reweight(self, candidate: Candidate) -> float:
        """Adjust a candidate's score in line with character preference."""
```

For example, the Practitioner character prefers chapters that link to a procedure — its `reweight` adds 0.15 if `candidate.has_linked_procedure`, and its `filter` drops any candidate where the chapter has no procedure links *and* no failure-mode-shaped paragraphs.

### Adding a new character

1. Subclass `Character` in `character.py`.
2. Implement `filter` and `reweight`.
3. Register the character in `CHARACTERS` registry.
4. Optionally add a slash command that pre-selects the character pair.

Example: a "Skeptic" character that prefers chapters that explicitly discuss limitations or anti-patterns:

```python
class Skeptic(Character):
    def filter(self, candidates):
        return [
            c for c in candidates
            if c.has_concept_type("AntiPattern")
            or "limitation" in c.chapter.content.lower()
            or "tradeoff" in c.chapter.content.lower()
        ]

    def reweight(self, candidate):
        boost = 0.0
        if candidate.has_concept_type("AntiPattern"):
            boost += 0.20
        return boost
```

Then in `Dialog`, swap `Architect` for `Skeptic` to get an Architect-vs-Skeptic dialog.

---

## Adding a generator

The Phase 7 framework defines four protocols. A new generator implements them.

### 1. Sketch the shape

Pick a one-sentence purpose. Decide the inputs, the output shape, the per-file structure. Skim an existing generator with similar shape — start with `cheatsheet.py` for single-file generators or `project_bootstrap.py` for multi-file.

### 2. Implement the protocols

In `mcp-servers/kb-mcp/<your_generator>.py`:

```python
from generator import Decomposer, Planner, Validator, Materializer, Generator

class MyDecomposer(Decomposer):
    def decompose(self, inputs):
        # Use search_chapters(mode="generation"), find_prerequisites,
        # compare_concept_across_authors, or direct catalog queries
        return [GeneratedUnit(...), ...]

class MyPlanner(Planner):
    def plan(self, units):
        return [PlannedFile(path=..., template=..., sources=[...]), ...]

class MyValidator(Validator):
    def validate(self, plan):
        # Resolve targets, match procedures, enforce confidence floor
        return ValidationResult(ok=True, issues=[])

class MyMaterializer(Materializer):
    def materialize(self, plan, output_dir):
        # Render each PlannedFile to disk; write manifest.json
        ...

class MyGenerator(Generator):
    decomposer = MyDecomposer
    planner = MyPlanner
    validator = MyValidator
    materializer = MyMaterializer
    name = "my-generator"
```

### 3. Wire a slash command

Create `.claude/commands/kb-my-generator.md`:

```markdown
---
description: One-line description of what this generator does
allowed-tools: ["Bash", "Read", "Write"]
---

Run my-generator over the input topic: $ARGUMENTS.

Steps:
1. Call the mypub-kb MCP server's search_chapters with mode="generation"
2. Invoke the my-generator pipeline at mcp-servers/kb-mcp/<your_generator>.py
3. Materialize output under data/generated-packages/my-generator_<topic>_<timestamp>/
4. Print the output path and a 3-line summary
```

### 4. Add tests

In `tests/test_my_generator.py`:

- Validator unit tests (the easy / fast / deterministic ones)
- Materializer test that writes deterministic output to a tmp dir, asserts file count and manifest shape
- Idempotency test (run twice; second run produces a new timestamped dir)
- Integration test that runs the full pipeline against a small fixture catalog

The repository has 825 unit tests and 5 live integration tests; new generators typically add 6–12 unit tests and 1 live test.

### 5. Persist via Phase 7 tables

Use `generated_package`, `generated_unit`, `generated_file`, `generated_source`. The framework's base `Generator.run()` handles INSERTs in the right order; you only need to populate the data structures.

```python
# In Materializer
package = self.framework.create_package(
    name="my-generator",
    inputs={"topic": topic},
    output_dir=output_dir,
)
for planned in plan.files:
    file_row = self.framework.create_file(package, planned.path)
    for src in planned.sources:
        self.framework.create_source(file_row, src.source_type, src.source_id, src.role)
```

### 6. Hook into the registry (optional)

If you want the generator listed in `/kb-curriculum` or composable from another generator, register it in [`mcp-servers/kb-mcp/generator.py`](../mcp-servers/kb-mcp/generator.py)'s `GENERATOR_REGISTRY`.

---

## Adding a doc source

Live doc sources are rows in `doc_source`. To add one manually:

```bash
.venv/bin/python3 scripts/seed_doc_sources.py \
    --name "Apache Pulsar" \
    --source-type context7 \
    --identifier "/apache/pulsar" \
    --authority 0.60 \
    --refresh-ttl-days 30
```

Then snapshot it:

```bash
.venv/bin/python3 scripts/refresh_docs.py refresh --source "Apache Pulsar"
```

Or use the interactive `/kb-discover <library>` slash command, which presents discovery candidates and registers the user's choice.

### Source-type-specific identifiers

| Source type | Identifier format | Example |
|---|---|---|
| `context7` | `/<owner>/<repo>` | `/apache/kafka` |
| `deepwiki` | `<owner>/<repo>` (no slash prefix) | `cwida/duckpgq-extension` |
| `github_raw` | full URL to a docs file | `https://raw.githubusercontent.com/.../README.md` |

---

## Adding extraction rules

The extraction sub-agent prompt is templated in `scripts/extract_batch.py`. To customize:

1. Edit the prompt template (look for the schema-validated output template)
2. Re-run `prep` for chapters you want to re-extract — old prompts are overwritten only on `--force`
3. Sub-agents process the new prompts
4. `process` ingests the new results

Common reasons to customize:

- **Different concept types.** If you're indexing a domain with concept types not in the default list (e.g., bio-science with `Gene`, `Protein`, `Pathway`), extend the schema's `concept_type` enum.
- **Domain-specific aliases.** If the resolver is mis-grouping concepts (e.g., abbreviations colliding), pre-populate `concept_alias` rows or extend the prompt with hand-written examples.
- **Tighter relations.** If `CITES` is too noisy for your domain, drop it from the schema; the resolver and ranker handle missing relation types gracefully.

---

## Tuning the auto-discovery loop

Discovery probe order, authority defaults, and novel-library detection live in [`mcp-servers/kb-mcp/discovery.py`](../mcp-servers/kb-mcp/discovery.py).

To change probe order (e.g., to prefer DeepWiki over Context7 for your domain):

```python
PROBE_ORDER = [
    ("deepwiki", 0.50),
    ("context7", 0.60),
    ("github_raw", 0.40),
]
```

To change novel-library detection (the heuristic that decides when a query mentions an unknown library):

```python
NOVEL_LIBRARY_PATTERNS = [
    re.compile(r"\b(?:install|setup|configure)\s+(?P<lib>\w+)"),
    re.compile(r"\b(?P<lib>\w+(?:\.\w+)+)\b"),  # foo.bar style
    # add custom patterns here
]
```

Restart the MCP server after changes.

---

## Customizing the Skills Factory

The Skills Factory has its own decomposition + per-skill generation pipeline (it predates the generic Phase 7 framework). Customization points:

| What | Where |
|---|---|
| Domain decomposition (graph community detection) | [`decomposition.py`](../mcp-servers/kb-mcp/decomposition.py) |
| Per-skill source selection strategy | [`package_planning.py`](../mcp-servers/kb-mcp/package_planning.py) |
| Per-skill content generation prompt | [`skill_generation.py`](../mcp-servers/kb-mcp/skill_generation.py) |
| Trigger-routing eval | [`skills_eval.py`](../mcp-servers/kb-mcp/skills_eval.py) |

The generation strategy (one of `recent_doc_anchored`, `consensus_synthesis`, `book_authoritative`) is chosen during the decomposition stage; users can override via the `/kb-generate-skills` slash command.

---

## See also

- [docs/architecture.md](architecture.md) — ranker internals and Phase 7 protocols
- [docs/generators.md](generators.md) — the seventeen current generators
- [docs/operations.md](operations.md) — eval harness for testing customizations
