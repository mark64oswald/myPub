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

## How to run

Call `generate_edi_roundtrip` with `txn_code`. Supported codes:

- `270` / `271` — eligibility inquiry / response
- `834` — benefit enrollment + maintenance
- `835` — claim payment / advice (remittance)
- `837` — claim (professional / institutional / dental)
- `997` / `999` — functional / implementation acknowledgement

Output package lands at `data/generated-packages/edi-roundtrip-<txn>/`.

## What the package validates

1. Each fixture has a valid X12 envelope (ISA/IEA + GS/GE + ST/SE).
2. Each fixture re-serializes byte-identical to its input.
3. Paired (request, response) fixtures share matching control numbers.

A `data-starved` warning means the catalog has no X12 concepts cited for the transaction's segments — the fixtures still ship from the built-in spec metadata; only the citation list is empty.