---
description: Generate an EDI Round-Trip Test package for an X12 transaction set (270/271, 834, 835, 837, 997, 999) — synthetic spec-conformant fixtures + Python parse/round-trip tests
---

You are generating an EDI Round-Trip Test package for the X12 transaction set in `$ARGUMENTS`.

The generator emits:
- `README.md` — transaction-set purpose, key segments + loops, how to run the tests
- `mapping.md` — segment + loop reference with citations from the X12 doc sources in the catalog (pyx12, Ballerina EDI, Stedi)
- `fixtures/<txn>_request.x12` — synthetic spec-conformant message with full ISA/GS/ST/SE/GE/IEA envelope
- `fixtures/<txn>_response.x12` — paired response (for transactions that have one: 270→271, 834→999, 837→835)
- `tests/test_roundtrip.py` — pytest tests asserting parse, round-trip equivalence, and paired-control-number alignment

## Output location

Every generator tool takes an `output_root` argument. Settle it before you
call the tool:

1. **The user named a folder** (e.g. `--out docs/kb`, "save it under
   design/"): resolve it against the current project
   (`${CLAUDE_PROJECT_DIR}`) and pass the **absolute** path.
2. **No folder named, and the current project is not the myPub repo:**
   ask once, recommending the first option:
   - `${CLAUDE_PROJECT_DIR}/mypub-packages/` (keep it with this project)
   - myPub's own `data/generated-packages/` (the shared archive)
3. **Inside the myPub repo, or the user says "default":** omit
   `output_root`. The server then uses `$MYPUB_OUTPUT` if set, otherwise
   `<myPub>/data/generated-packages/`.

Always pass an absolute path. The server resolves relative paths against
the myPub repo, not against the project you are working in.

## How to run

Call `generate_edi_roundtrip` with `txn_code`. Supported codes:

- `270` / `271` — eligibility inquiry / response
- `834` — benefit enrollment + maintenance
- `835` — claim payment / advice (remittance)
- `837` — claim (professional / institutional / dental)
- `997` / `999` — functional / implementation acknowledgement

Output package lands at `<output_root>/edi-roundtrip-<txn>/`.

## What the package validates

1. Each fixture has a valid X12 envelope (ISA/IEA + GS/GE + ST/SE).
2. Each fixture re-serializes byte-identical to its input.
3. Paired (request, response) fixtures share matching control numbers.

A `data-starved` warning means the catalog has no X12 concepts cited for the transaction's segments — the fixtures still ship from the built-in spec metadata; only the citation list is empty.