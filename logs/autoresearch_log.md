# Autoresearch log — Phase 2.6 extraction-prompt tuning

Running the loop described in the execution plan Prompt 2.6:

  1. Run eval → baseline
  2. Modify extraction prompt (one change at a time)
  3. Re-extract the target chapters, compare metrics to baseline
  4. If recall/F1 improves AND no new regressions → keep the prompt change
  5. If metrics regress or stay flat → revert and try a different hypothesis

Each iteration below also records what we learned about the extraction
prompt AND about the eval set itself — both are tunable.

---

## Baseline (post-cleanup, 2026-04-18)

Catalog state: 126 chapters extracted across 10 books, Phase 2.4 session 1
done, 5 queue items manually reviewed, SKU alias + Horizontal Scaling
merge applied.

```
=== extraction ===
golden pairs:   22
hits:           20
misses:          2
precision:       0.213  (deflated; see note)
recall:          0.909
f1:              0.345

Missed pairs:
  ch=106072  concept='Star Schema'     (Kimball "Chapter 3: Retail Sales")
  ch=64401   concept='Kafka'           ("Kafka Consumer Concepts")

=== resolution ===
same-pair total:     4
same-pair correct:   2   (SKU → Stock Keeping Unit,
                          Horizontal Scaling ↔ X-axis Scaling)
different-pair total: 5  (one pair skipped — Star Schema/Snowflake not in DB)
different-pair correct: 5
accuracy:            0.778
```

**Note on precision.** The formula counts every extracted concept in a
scoped chapter that isn't in the golden set as an "extra," which penalizes
the real-world case that chapters have 20-60 concepts while the golden
set has only a handful of must-appear pairs per chapter. The number
becomes meaningful only as the golden set grows to cover most legitimate
extractions.

---

## Iteration 1 — "umbrella concepts"

**Hypothesis.** The LLM is too conservative about canonical "headline"
concepts, skipping them as "implied" when sub-components are already
being extracted. Adding a rule that explicitly names umbrella concepts
as required extractions may fix the Star Schema and Kafka misses.

**Prompt change applied.** Added one bullet to the `Rules` block:

> * **Always include headline / umbrella concepts.** If the chapter's
>   title, section headings, or opening paragraphs center on a named,
>   canonical concept (e.g. "Star Schema" in a retail-analytics chapter,
>   "Kafka" in a Kafka-internals chapter, "Linearizability" in a
>   consistency chapter), extract that concept by its canonical name
>   even when you're also extracting its components, variants, or
>   related sub-topics.

**Re-extracted via sub-agents:** ch=106072, ch=64401. Results parked in
`/tmp/mypub-extraction/autoresearch-iter1/` and NOT processed into the DB.

**Golden-pair hits on the iter-1 JSON:**

| chapter | before | after | delta |
|---|--:|--:|---|
| ch=106072 (Kimball ch 3) | 7/8 | 6/8 | **-1** (lost "Additive Facts"; still no Star Schema) |
| ch=64401 (Kafka Consumer) | 2/3 | 2/3 | 0 (still no "Kafka") |

**Decision: reject and revert.** The change regressed recall on the
Kimball chapter and did nothing for Kafka. The LLM appears to have traded
detail-extraction budget for generality-hedging without actually emitting
the targeted umbrella concepts.

---

## Iteration 2 — "headings are promises"

**Hypothesis.** A narrower, more testable rule: if the chapter title or
a heading in the content names a specific concept, the LLM must include
that name in its entities list. This is a rule the LLM can follow
deterministically rather than relying on its own sense of "importance."

**Prompt change applied.**

> * **Headings are promises.** If the chapter title or any H1/H2/H3
>   heading in the provided content names a specific concept, pattern,
>   tool, framework, algorithm, or technique, that name must appear in
>   your entities list.

**Re-extracted via sub-agents:** same two chapters. Results parked in
`/tmp/mypub-extraction/autoresearch-iter2/` and NOT processed into the DB.

**Golden-pair hits on the iter-2 JSON:**

| chapter | before | after | delta |
|---|--:|--:|---|
| ch=106072 (Kimball ch 3) | 7/8 | 5/8 | **-2** (lost "Additive Facts", "Dimension Table"; still no Star Schema) |
| ch=64401 (Kafka Consumer) | 2/3 | 2/3 | 0 |

**Decision: reject and revert.** Even bigger regression on Kimball. The
narrower rule pushed the LLM to stick closely to heading text, which
caused it to emit "Dimension" instead of "Dimension Table" and "Additive
Fact" (singular) instead of "Additive Facts" — legitimate extractions
per its own interpretation but misses against the golden set.

---

## Cross-iteration findings

1. **Both misses resist prompt tweaks.** "Star Schema" is not extracted
   from Kimball Chapter 3 under any of the three prompts we tested. The
   LLM consistently classifies that chapter's content as "Dimensional
   Modeling" — which is arguably correct. The chapter *applies* star
   schemas to a retail business process but doesn't lead with "Star
   Schema" as a named headline. Similarly, the Kafka Consumer chapter is
   titled and organized around "Kafka Consumer," not "Kafka" — the
   umbrella is assumed context, not the chapter's subject.

2. **The golden set itself may be a tuning dimension.** The execution
   plan frames Phase 2.6 as prompt tuning against a fixed golden set,
   but real autoresearch needs to calibrate both: the prompt AND the
   golden set. Our two "misses" may be aspirational rather than
   defensible from the actual chapter content. Leaving them in as-is
   creates a permanent 2-point ceiling on extraction recall.

3. **Recall regressions from stricter rules.** Both iterations added
   rules intended to strengthen certain extractions but caused the LLM
   to drop other extractions it was previously emitting (singular vs
   plural surface forms, "Dimension" vs "Dimension Table"). Prompt
   surgery is delicate — narrow instructions can constrict rather than
   expand coverage.

4. **Resolution accuracy improved via cleanup, not prompt work.** The
   baseline bump from 0.556 → 0.778 resolution accuracy came from
   registering the SKU alias and merging the Horizontal Scaling dup. That
   kind of catalog-level cleanup is at least as impactful as prompt
   tuning, and much cheaper (no sub-agent invocations, no re-extraction).

---

## Future iteration ideas

These are recorded but **not run yet** — they become the next tranche
when someone sits down with the eval again.

- **Iter 3 (unrun): golden-set review.** Walk each pair that fails
  against a fresh chapter read. Either: (a) confirm the pair is
  defensible and lean on the prompt, (b) mark the pair as aspirational
  with a tolerant matcher, or (c) remove. This is likely where the most
  value sits.
- **Iter 4 (unrun): few-shot examples.** Instead of adding rules,
  provide one concrete example in the prompt showing the kind of
  extraction we want (canonical-name-first). Might reach where iter
  1's "rule in English" failed.
- **Iter 5 (unrun): resolution band tuning.** Borderline threshold is
  currently 0.75; spot-checking the review queue, some items (CAP
  Theorem ↔ Linearizability at sim=0.895) barely made the auto-queue
  cutoff at 0.90. Worth sweeping high_threshold across [0.88, 0.92] and
  low_threshold across [0.70, 0.80] against the golden resolution
  pairs to find the operating point with best same/different balance.

---

## Status

- Prompt: reverted to baseline (identical to pre-iteration state)
- Live catalog: post-cleanup state unchanged (iter-1 and iter-2 result
  JSONs were analyzed in-place and NOT processed into the DB)
- `logs/extraction_eval_baseline.md`: captures the post-cleanup baseline
- `tests/eval/golden_extractions.json`: unchanged

Autoresearch infrastructure is operational. Two completed iterations
demonstrate the loop rejecting bad changes without manual cleanup.
More iterations (especially the golden-set review) are the natural
next step but require investigator judgment — automating it further
would risk drifting the eval to match whatever the extractor emits.
