# Feature 6: Hypothesis Tracking & Contrary-Evidence Gate Report

> **Status**: Complete (Offline suite verified; live v2 comparison blocked by provider `response_schema` `400 INVALID_ARGUMENT` and Free Tier quota exhaustion, documented in [docs/investigator_report_v2.md](investigator_report_v2.md)).  
> **Design Spec**: [docs/hypothesis_design.md](hypothesis_design.md)  
> **Prompt Governance**: [agent/prompts/investigator_system_v2.md](../agent/prompts/investigator_system_v2.md) (`SHA-256: ade9f1e57fb55a291ed7ec472428abe4e8497b9ab39a2a985fbd3f3efa0cb97a`)

---

## 1. Hypothesis Ledger Design (As Built)

Implemented in [agent/graph/ledger.py](../agent/graph/ledger.py) as frozen Pydantic v2 models (`HypothesisRecord`, `HypothesisLedger`) manipulated exclusively via pure, side-effect-free transition functions:

- **Hard Cap**: `MAX_HYPOTHESES_PER_RUN = 4` per investigation run.
- **Identity Immutability**: Once created (`hyp_01`..`hyp_04`), `category`, `mechanism`, and `service` cannot be mutated. Pivoting requires adding a new hypothesis.
- **Plain-Text Statement**: `validate_plain_text_statement` trims whitespace, rejects empty strings and non-printable control characters, and enforces `len <= 200`.
- **Enum Validation**:
  - `category`: `{"bad_deploy", "dependency_failure", "false_alarm", "unknown"}`
  - `mechanism`: `{"connection_pool_exhaustion", "cache_bypass_latency", "dependency_timeout", "none", "unknown"}`
  - `status`: `{"open", "supported", "refuted", "inconclusive"}`
- **Status Transition State Machine (`VALID_TRANSITIONS`)**:
  - `open` $\to$ `supported | refuted | inconclusive | open`
  - `supported` $\to$ `inconclusive | refuted | supported`
  - `inconclusive` $\to$ `supported | refuted | inconclusive`
  - `refuted` $\to$ `refuted` only (cannot be reopened unless new evidence is explicitly linked after `evidence_count_at_refuted`).
- **Deterministic Leading Hypothesis Selection (`leading_hypothesis`)**:
  - Ranked purely by count of valid independent-source supporting evidence items, breaking ties by earliest `created_step`. Model self-reported `confidence` has **0%** influence on selection.

---

## 2. Step Schema v2 & Pre-Execution Validation

Implemented in [agent/graph/steps.py](../agent/graph/steps.py):

| Step Variant | Key Fields | Mechanical Constraints |
| :--- | :--- | :--- |
| `CallToolStepV2` | `step="call_tool"`, `tool`, `args`, `tests_hypothesis_id`, `expected_if_true`, `expected_if_false`, `reason` | `tool` in closed `ALLOWED_INVESTIGATION_TOOLS`; `expected_if_true` / `expected_if_false` plain text $\le 160$ chars; `reason` $\le 300$ chars. |
| `UpdateHypothesesStepV2` | `step="update_hypotheses"`, `operations`, `reason` | Max `6` ops/turn (`AddHypothesisOp`, `LinkEvidenceOp`, `SetStatusOp`). `AddHypothesisOp` accepts `confidence_label` (`low \| medium \| high`) only; numeric `confidence` is rejected by `StepValidator`. |
| `ConcludeStepV2` | `step="conclude"`, `leading_hypothesis_id`, `diagnosis_draft`, `confidence_label`, `what_would_change`, `reason` | Links conclusion directly to a ledger hypothesis ID and enforces confidence label discipline. |
| `FlatAgentStepV2` | Flattened superset schema | Provided alongside discriminated `AgentStepV2` for structured-output SDK compatibility. |

`StepValidator` validates every model step before execution and returns a typed `StepRejection` (with no side effects) on schema, allowlist, or ledger invariant violations.

---

## 3. Contrary-Evidence Gate Rules (Mechanical)

Implemented in [agent/graph/gate.py](../agent/graph/gate.py) (`evaluate_gate(ledger, tool_trace, evidence_store, alert) -> GateResult`):

| Rule | Failure Code | Mechanical Check |
| :--- | :--- | :--- |
| **Rule (a)** | `no_rival` | At least `2` hypotheses registered on the ledger, **OR** `leading.category == "false_alarm"` with verified post-alert baseline-return metrics evidence. |
| **Rule (b)** | `no_discriminating_test` | At least `1` dispatched tool call (`is_discriminating_test`) succeeded or returned a typed error, targeted a hypothesis via `tests_hypothesis_id`, and had its evidence/error linked as `contradicts` (or supporting a rival). Bypassed only for certified `false_alarm`. |
| **Cosmetic Rival Check** | `rival_not_linked` | Any non-leading hypothesis that was neither targeted by `tests_hypothesis_id` (`tests_run`) nor linked with supporting/contrary evidence fails the gate (unless `false_alarm`). |
| **Rule (c)** | `unsupported_citation` | Every `evidence_id` on the leading hypothesis must exist in the run's evidence store, originate from `ALLOWED_TOOL_SOURCES`, and overlap the incident time window. |
| **Rule (d)** | `single_source_support` | Leading hypothesis must cite supporting evidence from $\ge 2$ independent source kinds (`metrics`, `logs`, `traces`, `deploys`), or—for `false_alarm`—provide valid baseline-return metrics evidence. |

- **Retry & Forced Downgrade (`agent/graph/nodes/conclude.py`)**: On gate failure, `conclude_node` records a `run_event`, sends a typed `StepRejection` back to `plan_step`, and allows at most **2** retries (`gate_rejection_count < 2`). On the 3rd failure, it forces `outcome_kind="unknown"` with `outcome_reason="gate_not_satisfied"`.

---

## 4. Outcome Table

Implemented in `resolve_outcome` ([agent/graph/gate.py](../agent/graph/gate.py)) and finalized in [agent/graph/nodes/finalize.py](../agent/graph/nodes/finalize.py):

| Outcome Kind | Typed Reason | Trigger Condition | Persisted Run Status |
| :--- | :--- | :--- | :--- |
| `escalate` | `multiple_concurrent_causes` | $\ge 2$ `supported` hypotheses on distinct services | `escalated` |
| `unknown` | `evidence_unavailable` | Tool/backend errors blocked telemetry checks | `escalated` |
| `diagnosed` | `false_alarm_no_action` | Gate passed; `false_alarm` with baseline-return metrics | `completed` (`no_action` recorded) |
| `diagnosed` | `concluded_by_model` | Gate passed; standard root cause diagnosis | Remains in graph for Feature 7 (`HandoffPort.propose`) |
| `unknown` | `budget_exhausted` | Call or step budget exhausted without passing gate | `escalated` |
| `unknown` | `contradictory_evidence` | Gate failed and leading hypothesis has `contrary_evidence_ids` | `escalated` |
| `unknown` | `gate_not_satisfied` | Gate failed and retry limit (`2` retries) exhausted | `escalated` |
| `unknown` | `insufficient_evidence` | Gate failed without hitting budget/contradiction/retry limit | `escalated` |

---

## 5. Deterministic Confidence Cap Table

Stored as data in [agent/graph/confidence_caps.yaml](../agent/graph/confidence_caps.yaml) and evaluated by `evaluate_confidence_cap` (`final_confidence = min(model_label_float, cap)`). Both `model_confidence` and `capped_confidence` are persisted on `Diagnosis`.

| Rule ID | Max Confidence | Nominal Category | Condition |
| :--- | :---: | :--- | :--- |
| `tool_error_during_discriminating_test` | `0.30` | `low` | Any tool error occurred during a discriminating test |
| `no_rival_tested` | `0.30` | `low` | No rival hypothesis was tested or falsified |
| `unrefuted_contrary_evidence` | `0.30` | `low` | Unrefuted contrary evidence remains linked to leading hypothesis |
| `single_source_support` | `0.60` | `medium` | Leading hypothesis supported by only a single source kind |
| `gate_satisfied_few_evidence` | `0.75` | `medium` | Gate satisfied with 1 rival refuted, but total evidence items $< 3$ |
| `false_alarm_certified` | `0.90` | `high` | False alarm certified with baseline return evidence across full incident window |
| `gate_satisfied_strong` | `0.95` | `high` | Gate satisfied with $\ge 2$ independent source kinds, rival refuted, zero tool errors, and $\ge 3$ verified evidence items |

---

## 6. Known Gaming Risks & Mechanical Mitigations

| Gaming Risk | Mechanical Mitigation in Feature 6 |
| :--- | :--- |
| **Immediate single-call conclusion** | Gate Rule (a) (`no_rival`) and Rule (b) (`no_discriminating_test`) reject and force `unknown` (`gate_not_satisfied`) after `2` retries. |
| **Cosmetic rival** (added to ledger, never tested/linked) | `evaluate_gate` emits `rival_not_linked` if any rival has neither `tests_hypothesis_id`/`tests_run` nor linked evidence. |
| **Fabricated / foreign / out-of-window citations** | Gate Rule (c) (`unsupported_citation`) and `finalize_node` strip invalid, foreign-run, unauthorized-tool, and out-of-window IDs and downgrade to `unknown` if zero valid IDs remain. |
| **Metric-only citation bloat** | Gate Rule (d) (`single_source_support`) counts distinct `TOOL_SOURCE_KIND_MAP` source kinds (`metrics`, `logs`, `traces`, `deploys`), not raw call count. |
| **Reopening refuted hypothesis without new evidence** | `HypothesisRecord.evidence_count_at_refuted` in `ledger.py` blocks transitioning out of `refuted` unless new evidence has been linked. |
| **Ignoring `stale_data` / tool errors with high confidence** | `resolve_outcome` and `confidence_caps.yaml` cap confidence (`0.30`) or force `unknown` (`evidence_unavailable`). |
| **Confidence inflation** | `AddHypothesisOp` rejects numeric `confidence`; `finalize_node` applies `min(model_label, cap)` from `confidence_caps.yaml`. |
| **Hostile log injection** | `quarantine()` wraps untrusted telemetry text and `what_would_change` display strings; `StepValidator` enforces closed tool/category/mechanism/status enums. |

---

## 7. Limits & Deviations from `hypothesis_design.md`

### Limits
- **Prompt-only constraints**: Choosing semantically *meaningful* discriminating queries (e.g., querying `dependency` vs `deploy` rather than a trivially weak query on the target service) and formulating good initial rivals remain prompt-guided (`investigator_system_v2.md`), not semantically graded in code.
- **Live v2 evaluation blocked**: In the live comparison run ([docs/investigator_report_v2.md](investigator_report_v2.md)), `15/15` `v2` trials failed at Gemini `generation_config.response_schema` with `400 INVALID_ARGUMENT` (`additional_properties`), so all verified v2 behavioral evidence comes from the deterministic offline test suite (`tests/test_v2_loop_deterministic.py` and unit suites).
- **No action execution**: Action proposals and the Agent Value Gate remain deferred to Feature 7 and Feature 13.

### Deviations from `docs/hypothesis_design.md`
1. **Confidence Labels Instead of Floats**: `hypothesis_design.md` Section 3.3 depicted `confidence: 0.70`; as-built `steps.py` requires `confidence_label` (`low | medium | high`) and rejects raw numeric `confidence` in `StepValidator`.
2. **Hypothesis ID Format**: Section 2.1 specified `hyp_[0-9a-f]{4}` while Section 3.3 showed `hyp_01`..`hyp_04`; as-built `ledger.py` uses sequential IDs (`hyp_01`..`hyp_04`).
3. **Expectation Length Cap**: Section 3.2 specified `150` chars for `expected_if_true`/`expected_if_false`; as-built `steps.py` enforces `160` chars.
4. **False-Alarm Terminal Status**: Section 5 listed `needs_review`; as-built `finalize.py` (Option A) transitions `false_alarm` diagnoses directly to `completed` with `no_action` recorded.
5. **Flattened Structured-Output Schema**: `FlatAgentStepV2` was added in `steps.py` to accommodate structured-output SDK constraints.
