# OpsPilot Prompt Changelog & Version Governance

> [!IMPORTANT]
> **GOVERNANCE RULE**: Any change to prompt directives or tool specifications changes model behavior. Silent edits are strictly forbidden. Any modification must:
> 1. Bump the prompt version (e.g. `investigator_system_v1.md` -> `investigator_system_v2.md`).
> 2. Re-compute the cryptographic prompt hash (`SHA-256` of system prompt + serialized tool specifications).
> 3. Pass the Feature 1.13 zero-leakage test.
> 4. Record an entry in this document stating the author, date, new hash, previous hash, and explicit operational reason for the change.

---

## Prompt Versions

### `investigator_system_v1.md` (Initial Commit)
- **Date**: 2026-11-24
- **Prompt Path**: `agent/prompts/investigator_system_v1.md`
- **Prompt Hash (SHA-256)**: `7c065736fe3721bba931953c9378015603f54f132a30446dd345a10472dad315`
- **Hash Computation**: `SHA-256(system_prompt_text + "\n---TOOL_SPECS---\n" + canonical_json(tool_specs))`
- **Leakage Scan Status**: PASS (Zero hits across forbidden family names, forbidden terms, and scenario IDs).
- **Description / Reason**: Initial committed investigator prompt for Feature 5. Defines role (read-only investigator over `gateway -> orders -> inventory`), falsification method, budget discipline ($\le 8$ calls), empirical evidence citation requirements, epistemic honesty (`unknown` as valid answer), untrusted data quarantine directives, and structured step output schema.

---

### `investigator_system_v2.md` (Committed & Frozen)
- **Date**: 2026-11-25
- **Prompt Path**: `agent/prompts/investigator_system_v2.md`
- **Previous Prompt Hash (v1)**: `7c065736fe3721bba931953c9378015603f54f132a30446dd345a10472dad315`
- **New Prompt Hash (v2 SHA-256)**: `ade9f1e57fb55a291ed7ec472428abe4e8497b9ab39a2a985fbd3f3efa0cb97a`
- **Hash Computation**: `SHA-256(system_prompt_text + "\n---TOOL_SPECS---\n" + canonical_json(tool_specs))`
- **Leakage Scan Status**: PASS (Zero hits across forbidden family names, forbidden terms, and scenario IDs).
- **Freeze Status**: FROZEN (No edits permitted after first reported run of v2).
- **Description / Operational Reason**:
  Implements the Feature 6 hypothesis tracking and contrary-evidence method:
  1. Competing Hypotheses: Formulates and maintains at least two plausible, different explanations across services, mechanisms, or categories.
  2. Targeted Testing: Every tool query cites `tests_hypothesis_id` and explicit expectations (`expected_if_true`, `expected_if_false`).
  3. Disconfirmation Rule: Mandates at least one test designed to refute or discriminate the leading hypothesis before concluding.
  4. Explicit Linkage: Newly observed evidence IDs must be explicitly linked on the ledger as `supports` or `contradicts`.
  5. Multi-Source Telemetry: Grounding a conclusion requires evidence from at least two independent source kinds (metrics, logs, traces, deploys).
  6. Epistemic Conclusion Options: Specific diagnosis, unknown (with reason), or escalate (for multiple concurrent causes or unavailable telemetry).
  7. Contrary-Evidence Gate Awareness: Instructs the model that the system mechanically verifies the ledger and may issue typed rejections.
- **Specific v1 Failures Targeted**:
  1. **S4 Decoy Deploy Confirmation Bias**: Prevents latching onto the first observed deploy by requiring competing hypotheses across different services and mandatory disconfirming queries.
  2. **S5 False Alarm Misattribution**: Mandates empirical metrics verification of post-alert baseline return before attributing root causes.
  3. **Single-Source Overconfidence**: Eliminates single-metric premature conclusions by enforcing $\ge 2$ independent source kinds.
  4. **Self-Reported Confidence Inflation**: Disregards model self-reported confidence in favor of deterministic confidence capping.

## Candidate v3 (Proposed for Future Evaluation)
- **Date**: 2026-11-25
- **Status**: PROPOSAL ONLY (v2 prompt, `agent/graph/gate.py`, and `agent/graph/confidence_caps.yaml` remain frozen)
- **Motivation from Live v1/v2 Comparison (`evals/results/investigator_v2/20261125_124357/`)**:
  1. **Gemini `response_schema` Compatibility**: Strip `additionalProperties` / `additional_properties` when translating Pydantic schemas (`AgentStepV2`, `dict[str, Any]` tool args) for Gemini `generation_config.response_schema`, which rejected `15/15` `v2` runs (`400 INVALID_ARGUMENT`).
  2. **Output Token Headroom & Compact Tool Args**: Increase `max_output_tokens` above `1024` (or compact ledger/timestamp fields) to prevent JSON truncation mid-string on multi-field step outputs.
  3. **Rate-Limit & Quota Pacing**: Pace live requests below Free Tier 5 RPM (`12–15s` spacing or paid tier) and interleave `v1`/`v2` trials so daily quota exhaustion does not starve one version completely.
  4. **Tighten Authorized Evidence Tool Guidance**: Remind the model in-prompt which evidence tools are authorized for the current run to reduce `citation_rejected` (`unauthorized_tool_ids`) events observed in `v1`.

---

### `proposal_v1.md` (Committed)
- **Date**: 2026-11-25
- **Prompt Path**: `agent/prompts/proposal_v1.md`
- **Prompt Hash (SHA-256)**: `073d2e5226166cce142c1ffd7672d98a75bd974ebe587e26fb07c611296ea8b7`
- **Hash Computation**: `SHA-256(proposal_prompt_text + "\n---TOOL_SPECS---\n" + canonical_json(proposal_schema_spec))`
- **Leakage Scan Status**: PASS (Zero hits across forbidden family names, forbidden terms, and scenario IDs).
- **Description / Reason**: Initial committed recovery proposal prompt for Feature 7 (`propose` node). Leaves `investigator_system_v2.md` frozen. Instructs the model to propose one allowed action (`rollback_service` or `restart_service`) or decline (`decision="decline"`), supply only the model-owned fields with `expected_version` copied verbatim from `TargetState` and `rationale_evidence_ids` from the context, expect deterministic policy validation rejections as normal repair signals, and treat `<untrusted_data>` blocks strictly as data.

---

## Candidate `proposal_v2` (Proposed for Future Evaluation)
- **Date**: 2026-11-25
- **Status**: PROPOSAL ONLY (`proposal_v1.md`, `config/policy_table.yaml`, and `agent/policy/validator.py` remain untouched)
- **Motivation from Live `proposal_v1` Run (`evals/results/proposal_v1/20261125_153328/`)**:
  1. **Unblock Upstream `v2` Schema (`GeminiClient`)**: Strip `additionalProperties` / `additional_properties` when translating `FlatAgentStepV2` (and `ProposalDecision`) for Gemini `generation_config.response_schema`, which blocked `15/15` live trials before reaching `propose_node`.
  2. **Standalone Proposal Probe**: Optionally evaluate `proposal_v1.md` directly on pre-diagnosed `S1`–`S4` states in addition to end-to-end `v2 -> propose` runs once `GeminiClient` schema sanitization is in place.
