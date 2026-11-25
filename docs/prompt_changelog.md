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
