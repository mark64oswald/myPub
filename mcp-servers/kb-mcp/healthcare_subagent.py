"""Sub-agent dispatch layer for the 5 healthcare interop generators.

The healthcare generators (EDI Round-Trip, De-id Bundle, Standards
Translator, FHIR IG Scaffold, Integration Channel) emit complete,
working code that delegates to ``healthcare_libs``. What they cannot
emit deterministically is the *use-case-specific customization* a real
deployment needs: trading-partner IDs, payer IG deviations, local field
inventories, channel topology, deployment runbooks, and so on.

The Project Bootstrap generator handles this by writing per-file
sub-agent prompts under ``_sub_agent_prompts/``. This module mirrors
that pattern for the 5 healthcare generators: each customization point
becomes one prompt file the user (or the Task tool) can dispatch to
fill in the partner-specific details on top of the deterministic base.

Public API:
    customization_prompts(generator_kind, decomp_meta) -> list[GenFile]
        Returns the sub-agent prompt GenFiles for one generator's
        customization points. Each is rooted at ``_sub_agent_prompts/``
        with purpose ``"subagent_prompt"``.

    dispatch_readme(generator_kind) -> str
        Returns the markdown for ``_sub_agent_prompts/README.md`` —
        explains the customization workflow for that generator.

Generator kinds:
    "edi_roundtrip"   — EDI Round-Trip (X12 270/271/834/835/837)
    "deid_bundle"     — De-identification bundle
    "standards_translator" — HL7v2/X12 ↔ FHIR translator
    "fhir_ig_scaffold"— FHIR Implementation Guide scaffold
    "integration_channel" — Mirth/OIE/BridgeLink channel
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from generator import GenFile

# ---------------------------------------------------------------------------
# Customization-point definitions
# ---------------------------------------------------------------------------
#
# Each entry is one sub-agent prompt the user can dispatch. The body
# is rendered via ``str.format(**ctx)`` where ``ctx`` is built from the
# generator's decomposition metadata.
#
# Conventions:
# - Filenames are zero-padded (``01_``, ``02_``, ...) so they sort the
#   way the user is expected to work through them.
# - Each prompt opens with **Goal** and closes with **Deliverable** so
#   the dispatching agent has a clear contract.
# - Prompts ask for *files* (paths relative to the package root), not
#   loose prose, so they integrate cleanly with the deterministic base.

@dataclass(frozen=True)
class _Prompt:
    filename: str
    title: str
    body_template: str


_EDI_ROUNDTRIP_PROMPTS: list[_Prompt] = [
    _Prompt(
        filename="01_trading_partner_setup.md",
        title="Trading-partner identifiers + control-number policy",
        body_template=(
            "# Trading partner setup — {txn_code}\n\n"
            "**Goal:** replace the synthetic ISA/GS sender + receiver IDs "
            "in `fixtures/{txn_code}_request.x12` with the real values "
            "for *one specific* trading partner (payer, clearinghouse, or "
            "internal system), and codify the control-number policy.\n\n"
            "## Context\n\n"
            "- Transaction set: **X12 {txn_code}** ({txn_name})\n"
            "- Paired response: {paired}\n"
            "- The deterministic fixtures use placeholder IDs "
            "(`SENDERID`, `RECEIVERID`) and ICN `000000001`. Real "
            "submissions must use the IDs the partner assigned and a "
            "monotonic ICN drawn from a per-partner counter.\n\n"
            "## What to fill in\n\n"
            "1. ISA-06/ISA-08 sender/receiver IDs (15-char EDI IDs)\n"
            "2. ISA-05/ISA-07 qualifiers (`ZZ`, `01`, `30`, ...)\n"
            "3. GS-02/GS-03 application sender/receiver codes\n"
            "4. ICN allocation policy: source of the next ICN, retry "
            "behavior on rejection, audit trail\n"
            "5. Production vs test mode flag (ISA-15 = `T` or `P`)\n\n"
            "## Deliverable\n\n"
            "Update the package in place:\n\n"
            "- `partner_config.yaml` — the real IDs + policy (NEW file)\n"
            "- `fixtures/{txn_code}_request.x12` — regenerated using "
            "`healthcare_libs.x12.build_envelope(...)` with the real IDs\n"
            "- `README.md` — add a *Trading partner* section explaining "
            "the ID mapping and ICN policy\n\n"
            "Do NOT modify `transformer.py` or `tests/` — those stay "
            "partner-agnostic. The real IDs live in `partner_config.yaml` "
            "and get loaded at runtime.\n"
        ),
    ),
    _Prompt(
        filename="02_payer_ig_deviations.md",
        title="Payer-specific implementation-guide deviations",
        body_template=(
            "# Payer IG deviations — {txn_code}\n\n"
            "**Goal:** capture the ways your target payer's "
            "implementation guide deviates from the base ASC X12 "
            "{txn_code} spec, and add validation that catches them.\n\n"
            "## Context\n\n"
            "Most payers publish a *Companion Guide* (or Implementation "
            "Guide) on top of the base X12 spec. Common deviations:\n\n"
            "- Required loops/segments that the base spec marks "
            "*situational*\n"
            "- Stricter cardinality (e.g., max 1 service line where the "
            "spec allows many)\n"
            "- Code-list restrictions (only HCPCS, no CPT II)\n"
            "- Forbidden segments that trigger 999 rejection\n"
            "- Data-element length constraints tighter than the spec\n\n"
            "## What to do\n\n"
            "1. Locate the payer's companion guide for the {txn_code} "
            "transaction (PDF link in the README is fine).\n"
            "2. Inventory the deviations in a table.\n"
            "3. Write a `validate_payer.py` module that wraps "
            "`healthcare_libs.x12.validate(...)` with the additional "
            "rules. Each deviation is one validator function returning "
            "`X12Issue` rows.\n"
            "4. Add tests in `tests/test_payer_validate.py` that "
            "fixture-test each deviation (one passing, one failing).\n\n"
            "## Deliverable\n\n"
            "- `validate_payer.py` (NEW)\n"
            "- `tests/test_payer_validate.py` (NEW)\n"
            "- `payer_companion_guide.md` — link + summary table (NEW)\n"
        ),
    ),
    _Prompt(
        filename="03_redacted_real_fixtures.md",
        title="Replace synthetic fixtures with redacted real-world examples",
        body_template=(
            "# Redacted fixtures — {txn_code}\n\n"
            "**Goal:** swap the deterministic synthetic fixtures for "
            "redacted real-world {txn_code} samples, so tests catch "
            "edge cases the synthetic builder never produces.\n\n"
            "## Why this matters\n\n"
            "`healthcare_libs.x12.build_{txn_code_lower}(...)` produces "
            "minimal, well-formed messages. Real production traffic "
            "exercises corner cases: long names, repeating loops, "
            "non-ASCII characters, edge dates, NPI variations.\n\n"
            "## What to do\n\n"
            "1. Source 3-5 real {txn_code} samples (from production "
            "logs, partner test files, or X12 sample libraries).\n"
            "2. **Redact PHI**: use "
            "`healthcare_libs.deid.AuditLog` + the X12-aware redaction "
            "helpers in `healthcare_libs.deid` to scrub names, DOBs, "
            "SSNs, member IDs. Verify zero PHI remains.\n"
            "3. Save under `fixtures/real/{txn_code}_<scenario>.x12`.\n"
            "4. Add a parametrized test in `tests/test_real_fixtures.py` "
            "that asserts each fixture parses + round-trips clean.\n\n"
            "## Deliverable\n\n"
            "- `fixtures/real/*.x12` (3-5 files)\n"
            "- `fixtures/real/REDACTION_LOG.md` — what was scrubbed\n"
            "- `tests/test_real_fixtures.py`\n"
        ),
    ),
]


_DEID_BUNDLE_PROMPTS: list[_Prompt] = [
    _Prompt(
        filename="01_local_field_inventory.md",
        title="Inventory local PHI fields beyond the standard schema",
        body_template=(
            "# Local PHI field inventory — {shape}\n\n"
            "**Goal:** enumerate every local field in your data that "
            "carries PHI but isn't in the canonical {shape} schema, so "
            "the de-id pipeline catches it.\n\n"
            "## Context\n\n"
            "The deterministic pipeline (`pipeline.py`) handles the "
            "{shape} fields HIPAA Safe Harbor explicitly names. But "
            "every real deployment has *local* fields the standard "
            "doesn't anticipate:\n\n"
            "- Free-text notes columns that contain names\n"
            "- `legacy_*` columns from prior systems\n"
            "- Custom JSON blobs with embedded identifiers\n"
            "- Vendor-injected fields (e.g., scheduling system IDs)\n\n"
            "## What to do\n\n"
            "1. Run `transformer.py audit --input <sample.{shape_ext}>` "
            "to dump the full field set.\n"
            "2. Cross-check each field against the 18 Safe Harbor "
            "categories in `healthcare_libs.deid.SAFE_HARBOR_CATEGORIES`.\n"
            "3. For each field NOT in the canonical schema, decide: "
            "PHI / quasi-identifier / safe — and note the rule.\n"
            "4. Encode the rules in `local_fields.yaml` (the pipeline "
            "loads this at runtime).\n\n"
            "## Deliverable\n\n"
            "- `local_fields.yaml` — one entry per local field with "
            "`{{field, classification, action}}`\n"
            "- `LOCAL_FIELD_INVENTORY.md` — narrative + decision log\n"
        ),
    ),
    _Prompt(
        filename="02_pseudonymization_strategy.md",
        title="Pseudonymization key custody + re-identification policy",
        body_template=(
            "# Pseudonymization strategy — {shape}\n\n"
            "**Goal:** lock down the operational policy around the "
            "HMAC pseudonymization key — who holds it, how it rotates, "
            "what the re-identification process looks like (if any).\n\n"
            "## Context\n\n"
            "`healthcare_libs.deid.hmac_pseudonym(...)` derives stable "
            "tokens from `(value, key)`. The whole de-id story collapses "
            "if the key is mishandled. Decisions you have to make:\n\n"
            "- **Custody.** Where does the key live (KMS, vault, env "
            "var)? Who can read it?\n"
            "- **Rotation.** Same key forever (stable tokens across "
            "datasets) or per-dataset (no cross-dataset linkage)?\n"
            "- **Re-identification.** Is there a re-id pathway "
            "(researcher needs to contact a patient)? If yes — who "
            "approves, who executes, what audit trail?\n"
            "- **Audit retention.** How long do `AuditLog` rows live?\n\n"
            "## Deliverable\n\n"
            "- `pseudonym_policy.md` — answers to all four\n"
            "- `key_management.tf` (or similar) — IaC for the KMS path "
            "if applicable\n"
        ),
    ),
    _Prompt(
        filename="03_k_anonymity_thresholds.md",
        title="k-anonymity threshold + quasi-identifier set",
        body_template=(
            "# k-anonymity tuning — {shape}\n\n"
            "**Goal:** pick the k threshold and quasi-identifier columns "
            "for your dataset, then verify the de-id'd output meets "
            "them on a representative sample.\n\n"
            "## Context\n\n"
            "`healthcare_libs.deid.k_anonymize(...)` enforces k-anon "
            "but you have to tell it which columns are quasi-IDs and "
            "what k value to enforce. Defaults of `k=5` over "
            "`{{age_band, sex, zip3}}` are a starting point — tighter "
            "or looser depending on data sensitivity and intended "
            "audience.\n\n"
            "## What to do\n\n"
            "1. Identify the quasi-identifiers in your output. "
            "(Anything that, combined, could re-identify a row.)\n"
            "2. Choose k based on use case (k=5 for research, k=20+ "
            "for public release).\n"
            "3. Update `pipeline.py` to apply `k_anonymize` with your "
            "values.\n"
            "4. Add `tests/test_k_anonymity.py` that runs the pipeline "
            "on a sample and asserts every group has size ≥ k.\n\n"
            "## Deliverable\n\n"
            "- `k_anonymity_config.yaml`\n"
            "- updated `pipeline.py` (the call site)\n"
            "- `tests/test_k_anonymity.py`\n"
        ),
    ),
    _Prompt(
        filename="04_compliance_attestation.md",
        title="HIPAA Safe Harbor / Limited Data Set attestation",
        body_template=(
            "# Compliance attestation — {shape}\n\n"
            "**Goal:** document which compliance pathway this de-id "
            "pipeline targets (Safe Harbor, Limited Data Set, Expert "
            "Determination), why, and what evidence backs that claim.\n\n"
            "## What to write\n\n"
            "1. **Pathway choice + rationale.** Safe Harbor is "
            "mechanical (the 18 categories). Limited Data Set leaves "
            "dates + zip3. Expert Determination needs a statistician's "
            "letter.\n"
            "2. **Evidence.** Map each Safe Harbor category to the "
            "pipeline step that handles it. (`SAFE_HARBOR_CATEGORIES` "
            "in `healthcare_libs.deid` is the index.)\n"
            "3. **Residual risk.** Free-text fields, photos, anything "
            "the pipeline can't fully clean — call it out.\n"
            "4. **Sign-off.** Who reviewed this, when.\n\n"
            "## Deliverable\n\n"
            "- `COMPLIANCE.md` (top-level)\n"
        ),
    ),
]


_STANDARDS_TRANSLATOR_PROMPTS: list[_Prompt] = [
    _Prompt(
        filename="01_local_field_extensions.md",
        title="Local field mapping extensions",
        body_template=(
            "# Local field mapping — {source_format} → {target_format}\n\n"
            "**Goal:** extend the canonical {source_format} → "
            "{target_format} mapping with the local fields your source "
            "system carries beyond the standard schema.\n\n"
            "## Context\n\n"
            "`transformer.py` already wraps "
            "`healthcare_libs.cross_standards.{transformer_func}` which "
            "covers the canonical mapping. Real source systems carry "
            "extra fields (custom OBX results, Z-segments, vendor "
            "extensions) that need explicit handling.\n\n"
            "## What to do\n\n"
            "1. Inventory the non-canonical fields in your source data.\n"
            "2. For each, decide the target representation (FHIR "
            "extension URL, custom slice, drop, log-and-skip).\n"
            "3. Add per-field handlers in `local_extensions.py` "
            "callable from `transformer.py`.\n"
            "4. Add tests in `tests/test_local_extensions.py`.\n\n"
            "## Deliverable\n\n"
            "- `local_extensions.py`\n"
            "- `tests/test_local_extensions.py`\n"
            "- `local_field_map.md` — table of source field → target\n"
        ),
    ),
    _Prompt(
        filename="02_business_rules.md",
        title="Transformation business rules",
        body_template=(
            "# Business rules — {source_format} → {target_format}\n\n"
            "**Goal:** capture the business rules that govern *which* "
            "rows transform and *how*, beyond pure structural mapping.\n\n"
            "## Examples of business rules\n\n"
            "- Skip cancelled visits (PV1-45 = 'C')\n"
            "- Roll lab corrections (OBR-25 = 'C') into the original "
            "report rather than a new one\n"
            "- Drop test patients (custom flag)\n"
            "- Apply payer-specific code remapping before standard "
            "transform\n\n"
            "## What to do\n\n"
            "1. Document each rule in `business_rules.md`.\n"
            "2. Encode each as a filter or pre-processor function in "
            "`rules.py`.\n"
            "3. Wire `transformer.py` to apply them before/after the "
            "canonical transform.\n"
            "4. Add tests covering each rule.\n\n"
            "## Deliverable\n\n"
            "- `rules.py`\n"
            "- `tests/test_rules.py`\n"
            "- `business_rules.md`\n"
        ),
    ),
    _Prompt(
        filename="03_partner_test_data.md",
        title="Partner-realistic test data",
        body_template=(
            "# Partner test data — {source_format} → {target_format}\n\n"
            "**Goal:** swap the synthetic test fixtures for redacted "
            "samples representative of your actual partner traffic.\n\n"
            "## What to do\n\n"
            "1. Source 5-10 representative {source_format} samples.\n"
            "2. De-identify using `healthcare_libs.deid` (or the "
            "matching `kb-deid-bundle` package if you generated one).\n"
            "3. Save under `fixtures/partner/`.\n"
            "4. Add tests asserting each transforms cleanly + the "
            "output validates as {target_format}.\n\n"
            "## Deliverable\n\n"
            "- `fixtures/partner/*` (5-10 files)\n"
            "- `fixtures/partner/REDACTION_LOG.md`\n"
            "- `tests/test_partner_fixtures.py`\n"
        ),
    ),
]


_FHIR_IG_SCAFFOLD_PROMPTS: list[_Prompt] = [
    _Prompt(
        filename="01_use_case_narrative.md",
        title="Implementation Guide narrative — use case + scope",
        body_template=(
            "# IG narrative — {ig_name}\n\n"
            "**Goal:** write the human-facing narrative that frames "
            "this Implementation Guide for its real audience: who it's "
            "for, what it solves, what it explicitly doesn't cover.\n\n"
            "## Context\n\n"
            "The deterministic scaffold gives you the FSH profile "
            "shells, examples, and ImplementationGuide resource. The "
            "narrative is what makes implementers adopt the IG vs "
            "rolling their own.\n\n"
            "## What to write\n\n"
            "1. **Background.** What real-world problem does this IG "
            "solve? Who's been burned by the lack of standardization?\n"
            "2. **Scope.** What's in (specific resources, profiles, "
            "value sets). What's explicitly out.\n"
            "3. **Actors.** Sender, receiver, intermediaries.\n"
            "4. **Use cases.** 2-4 concrete scenarios with sequence "
            "diagrams.\n"
            "5. **Conformance.** What MUST/SHOULD/MAY mean here.\n\n"
            "## Deliverable\n\n"
            "- `input/pagecontent/index.md`\n"
            "- `input/pagecontent/background.md`\n"
            "- `input/pagecontent/use_cases.md`\n"
            "- `input/pagecontent/conformance.md`\n"
        ),
    ),
    _Prompt(
        filename="02_must_support_review.md",
        title="Must-support flag review per profile",
        body_template=(
            "# Must-support review — {ig_name}\n\n"
            "**Goal:** review every `MS` flag the scaffold emitted, "
            "and tighten or relax based on what your implementers can "
            "actually deliver.\n\n"
            "## Context\n\n"
            "The deterministic scaffold marks fields MS based on "
            "general clinical relevance. Your IG may need stricter or "
            "looser rules — the difference is real (MS = receiver MUST "
            "process if present).\n\n"
            "## What to do\n\n"
            "1. For each profile in `input/fsh/profiles/`, list every "
            "field marked MS.\n"
            "2. Decide: keep MS, drop MS, or upgrade to required (`1..1`).\n"
            "3. Document the rationale per profile.\n"
            "4. Update the FSH files.\n\n"
            "## Deliverable\n\n"
            "- updated `input/fsh/profiles/*.fsh`\n"
            "- `must_support_decisions.md`\n"
        ),
    ),
    _Prompt(
        filename="03_value_set_bindings.md",
        title="Value set bindings + terminology server",
        body_template=(
            "# Value set bindings — {ig_name}\n\n"
            "**Goal:** lock down the value sets each coded element "
            "binds to, and configure terminology server access for "
            "validation.\n\n"
            "## What to do\n\n"
            "1. Inventory every coded field across profiles.\n"
            "2. For each: pick the binding strength (required, "
            "extensible, preferred, example).\n"
            "3. For each required binding: identify or build the "
            "value set (FSH `ValueSet` resource).\n"
            "4. Configure `sushi-config.yaml` to point at a "
            "terminology server (UMLS, FHIR Terminology Server, or "
            "self-hosted).\n\n"
            "## Deliverable\n\n"
            "- `input/fsh/valuesets/*.fsh` (new value sets)\n"
            "- updated profile FSH (binding declarations)\n"
            "- updated `sushi-config.yaml`\n"
        ),
    ),
    _Prompt(
        filename="04_use_case_examples.md",
        title="Use-case-specific example resources",
        body_template=(
            "# Use-case examples — {ig_name}\n\n"
            "**Goal:** replace the canonical synthetic examples with "
            "real-world (de-identified) instances that demonstrate the "
            "IG's specific use cases.\n\n"
            "## What to do\n\n"
            "1. For each use case named in `use_cases.md`, build 1-2 "
            "example Bundles using `healthcare_libs.fhir`.\n"
            "2. De-identify if sourced from real data.\n"
            "3. Save under `input/examples/`.\n"
            "4. Cross-reference from the use case page.\n\n"
            "## Deliverable\n\n"
            "- `input/examples/*.json`\n"
            "- updated `input/pagecontent/use_cases.md`\n"
        ),
    ),
]


_INTEGRATION_CHANNEL_PROMPTS: list[_Prompt] = [
    _Prompt(
        filename="01_channel_topology.md",
        title="Real source/destination connection details",
        body_template=(
            "# Channel topology — {scenario}\n\n"
            "**Goal:** replace the placeholder source + destination "
            "configs in `channel.xml` with the real connection details "
            "for your environment.\n\n"
            "## Context\n\n"
            "The deterministic builder emits a `channel.xml` valid for "
            "{engine_target}. Source/destination configs use placeholder "
            "hosts, ports, and paths. Real deployment needs:\n\n"
            "- Source listener (host, port, TLS, auth)\n"
            "- Destination connector (host, port, TLS, auth)\n"
            "- Credential storage (vault path, env var, keystore)\n"
            "- Network controls (firewall rules, allowlist IPs)\n\n"
            "## What to do\n\n"
            "1. Document the topology in `topology.md` (source → "
            "channel → destination + any branching).\n"
            "2. Update `channel.xml` source/destination configs.\n"
            "3. Add credential refs (use the engine's vault integration; "
            "do NOT inline secrets).\n"
            "4. Document firewall + DNS prerequisites.\n\n"
            "## Deliverable\n\n"
            "- updated `channel.xml`\n"
            "- `topology.md`\n"
            "- `infrastructure_prereqs.md`\n"
        ),
    ),
    _Prompt(
        filename="02_transformer_logic.md",
        title="Channel-specific transformer enrichment",
        body_template=(
            "# Transformer enrichment — {scenario}\n\n"
            "**Goal:** extend the deterministic JS transformer in "
            "`channel.xml` with the partner-specific business logic "
            "this channel needs.\n\n"
            "## Context\n\n"
            "The base transformer handles the canonical "
            "{source_format} → {target_format} translation. Real "
            "channels typically add:\n\n"
            "- Inbound enrichment (patient lookup, encounter linking)\n"
            "- Code translation (local codes → standard codes)\n"
            "- Routing rules (pick destination based on message "
            "content)\n"
            "- Filtering (drop messages matching certain criteria)\n\n"
            "## What to do\n\n"
            "1. List the enrichment / routing / filter rules.\n"
            "2. Add them as steps to the channel transformer "
            "(or split into a chain of channels).\n"
            "3. For complex logic, factor out to a code template / "
            "JS library that the transformer calls.\n"
            "4. Update tests in `tests/test_transformer.js` (or "
            "Mirth's built-in test harness).\n\n"
            "## Deliverable\n\n"
            "- updated `channel.xml`\n"
            "- `code_templates/*.js` (if applicable)\n"
            "- `transformer_rules.md`\n"
        ),
    ),
    _Prompt(
        filename="03_error_handling.md",
        title="Error handling, retries, and alerting",
        body_template=(
            "# Error handling — {scenario}\n\n"
            "**Goal:** define what happens when this channel fails — "
            "what gets retried, what gets parked, who gets paged.\n\n"
            "## What to specify\n\n"
            "1. **Retry policy.** Transient (network, timeout) vs "
            "permanent (parse error). Backoff strategy.\n"
            "2. **Dead-letter handling.** Where parked messages live, "
            "how long, who reviews.\n"
            "3. **Alerting.** What thresholds page someone (error rate, "
            "queue depth, age of oldest message).\n"
            "4. **Reconciliation.** End-of-day check that source count "
            "= destination count.\n\n"
            "## Deliverable\n\n"
            "- `error_handling.md`\n"
            "- updated `channel.xml` (response + postprocessor scripts)\n"
            "- `alerts.yaml` (or wherever your monitoring lives)\n"
        ),
    ),
    _Prompt(
        filename="04_deployment_runbook.md",
        title="Deployment + operations runbook",
        body_template=(
            "# Deployment runbook — {scenario}\n\n"
            "**Goal:** write the runbook the operator follows to "
            "deploy this channel, watch it in production, and roll it "
            "back if needed.\n\n"
            "## What to cover\n\n"
            "1. **Deploy.** Where to import `channel.xml` "
            "({engine_target}-specific steps), what to set in the UI.\n"
            "2. **Smoke test.** First message, expected log output.\n"
            "3. **Monitoring.** Dashboards / queries to watch the "
            "first 24h.\n"
            "4. **Rollback.** How to disable + restore prior version.\n"
            "5. **On-call.** Who owns this channel, hours, escalation.\n\n"
            "## Deliverable\n\n"
            "- `RUNBOOK.md` (top-level)\n"
        ),
    ),
]


_REGISTRY: dict[str, list[_Prompt]] = {
    "edi_roundtrip": _EDI_ROUNDTRIP_PROMPTS,
    "deid_bundle": _DEID_BUNDLE_PROMPTS,
    "standards_translator": _STANDARDS_TRANSLATOR_PROMPTS,
    "fhir_ig_scaffold": _FHIR_IG_SCAFFOLD_PROMPTS,
    "integration_channel": _INTEGRATION_CHANNEL_PROMPTS,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def supported_kinds() -> list[str]:
    return sorted(_REGISTRY.keys())


def customization_prompts(
    generator_kind: str, decomp_meta: dict[str, Any],
) -> list[GenFile]:
    """Return GenFile entries for a generator's customization prompts.

    ``decomp_meta`` is the per-generator context dict used to fill in
    the prompt templates. Each generator passes whatever fields its
    decomposition exposes (txn_code, shape, scenario, ig_name, ...).
    Missing fields are tolerated — they show as the literal placeholder
    in the prompt text, which still reads as a TODO marker for the
    sub-agent.
    """
    if generator_kind not in _REGISTRY:
        raise ValueError(
            f"unknown generator kind: {generator_kind!r}; "
            f"expected one of {supported_kinds()}"
        )
    safe_meta = _SafeFormatDict(decomp_meta)
    out: list[GenFile] = []
    for p in _REGISTRY[generator_kind]:
        body = p.body_template.format_map(safe_meta)
        out.append(GenFile(
            filename=f"_sub_agent_prompts/{p.filename}",
            content=body,
            purpose="subagent_prompt",
        ))
    out.append(GenFile(
        filename="_sub_agent_prompts/README.md",
        content=dispatch_readme(generator_kind),
        purpose="subagent_dispatch_readme",
    ))
    return out


def dispatch_readme(generator_kind: str) -> str:
    """Render the README explaining how to dispatch the prompts."""
    if generator_kind not in _REGISTRY:
        raise ValueError(f"unknown generator kind: {generator_kind!r}")
    prompts = _REGISTRY[generator_kind]
    lines = [
        f"# Sub-agent dispatch — {generator_kind}",
        "",
        "The deterministic generator emits complete, working code that ",
        "exercises `healthcare_libs`. The prompts in this directory ",
        "cover the **use-case-specific customization** that has to come ",
        "from the deployment context (trading partner, payer IG, ",
        "channel topology, etc.).",
        "",
        "## How to dispatch",
        "",
        "Each prompt is a self-contained ask. Two ways to run them:",
        "",
        "1. **Manually.** Open the prompt, do the work, commit the ",
        "   resulting files into the package.",
        "2. **Sub-agent.** Pass the prompt to Claude (via the Task tool ",
        "   or `claude` CLI). The prompt declares its own deliverable ",
        "   files; the sub-agent writes them straight into the package.",
        "",
        "Order matters where one prompt's deliverable feeds the next ",
        "(noted inline). Otherwise dispatch in any order.",
        "",
        "## Prompts",
        "",
    ]
    for p in prompts:
        lines.append(f"- `{p.filename}` — {p.title}")
    lines.append("")
    lines.append(
        "After all prompts are addressed, re-run the package's tests "
        "(`pytest tests/`) — the deterministic core should still pass, "
        "and the new partner-specific tests should pass too."
    )
    return "\n".join(lines) + "\n"


class _SafeFormatDict(dict):
    """str.format_map view that returns ``{key}`` for missing keys
    instead of raising. Lets generators omit context fields that don't
    apply, and the sub-agent reads the placeholder as a TODO marker."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
