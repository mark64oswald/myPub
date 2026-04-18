---
description: Interactively review borderline concept-resolution items
---

You are driving the concept-resolution review loop. The EntityResolver has
been queuing "borderline" concepts — candidates whose embedding similarity
to an existing concept fell in the 0.75–0.89 band (configurable). Your job
is to walk the user through the pending queue and apply one of four
actions per item.

## How to run

1. Show the top pending items so we know what's in the queue:

   ```bash
   .venv/bin/python3 scripts/resolve_concept.py list --limit 10
   ```

2. Pick the highest-similarity pending item and show full details:

   ```bash
   .venv/bin/python3 scripts/resolve_concept.py show <queue_id>
   ```

3. Present the item to the user concisely — candidate name, nearest
   concept name, similarity, and a hint about the candidate_context.
   Ask them which action to take. Valid actions:

   - **merge** — The candidate IS the same concept as the nearest.
     Rewrites provisional edges to the nearest concept and deletes the
     provisional.
     ```bash
     .venv/bin/python3 scripts/resolve_concept.py merge <queue_id> [--register-alias]
     ```

   - **alias** — Same effect as `merge --register-alias`: treat as
     duplicate and also record the candidate_name as a future alias.
     ```bash
     .venv/bin/python3 scripts/resolve_concept.py alias <queue_id>
     ```

   - **keep-separate** — The candidate is genuinely a distinct concept.
     Clears the pending_review flag so it becomes canonical.
     ```bash
     .venv/bin/python3 scripts/resolve_concept.py keep-separate <queue_id>
     ```

   - **rename** — The extractor produced a bad name. Provide a better
     one. Add `--merge-into <concept_id>` if after renaming it should
     fold into an existing concept.
     ```bash
     .venv/bin/python3 scripts/resolve_concept.py rename <queue_id> "Better Name" [--merge-into <concept_id>]
     ```

4. Loop: back to step 2 with the next highest-similarity pending item
   until the user says stop or the queue is empty.

## Guidance

- Default batch size: review 5–10 items per session unless the user asks
  to go further. Quality drops with fatigue.
- When unsure, favor **keep-separate** — false merges are harder to
  unwind than false keep-separates (which can be merged later if the
  user changes their mind).
- When the candidate_name is an obvious typo/variant of the nearest
  name (e.g. "LoRa" vs "LoRA"), **alias** is usually correct.
- When both concepts are legitimate but orthogonal members of a family
  (e.g. "Snapshot Isolation" vs "Read Committed"), **keep-separate**.
- Don't run `rename` without explicit user direction; it's the most
  destructive action.