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

### Candidate v2 (Proposed for Feature 6)
- **Status**: Proposed / Under Evaluation (DO NOT commit to `investigator_system_v1.md` in Feature 5)
- **Target Version**: `investigator_system_v2.md` (Feature 6: Hypothesis/Contrary Evidence & Action Proposals)
- **Proposed Enhancements**:
  1. **Explicit In-Prompt Step Schemas**: Directly provide the JSON representation of `{step: "call_tool", tool, args, reason}` and `{step: "conclude", diagnosis_draft, reason}` in Section 7 of the prompt to prevent schema ambiguity on models or providers without polymorphic structured output support.
  2. **Hypothesis / Contrary Evidence Formulation**: Structure the turn decision around explicit dual hypotheses (e.g. `primary_hypothesis`, `competing_hypothesis`, and `disconfirming_query`) to actively prevent confirmation bias when analyzing decoy deploys (such as S4).
  3. **Multi-Turn Telemetry Quota Efficiency**: Guidance encouraging batching or prioritizing high-information queries (such as cross-service latency metrics or recent deploys) in initial turns to minimize LLM round-trips and preserve API quota under constrained provider tiers.
  4. **Strict False Alarm Verification**: Add explicit instruction that an alert breach without corresponding latency/error elevation or log anomalies across the incident window should prompt checking for spurious threshold breaches before attributing code or infrastructure causes.
