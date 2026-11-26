# OpsPilot Feature 7: Action Proposal & Policy Validator Report

> **Status**: Feature 7 Complete  
> **Design Specification**: [docs/proposal_design.md](proposal_design.md)  
> **Live Evaluation Report**: [docs/proposal_report_v1.md](proposal_report_v1.md)

---

## 1. Graph As Built

In [agent/graph/graph.py](../agent/graph/graph.py), `finalize_node` routes v2 runs via `route_after_finalize`:
- `outcome_kind == "diagnosed"` and `root_cause_category != "false_alarm"` (and no explicit `handoff_port` override): routes `finalize` $\to$ `propose_node` ([agent/graph/nodes/propose.py](../agent/graph/nodes/propose.py)) $\to$ `END`.
  - `propose_node` invokes `LLMClient.generate_structured(..., ProposalDecision)` using [agent/prompts/proposal_v1.md](../agent/prompts/proposal_v1.md), validates `ProposalInput`, runs `agent.policy.validator.validate()`, and retries up to `MAX_PROPOSAL_RETRIES = 2` on typed violations.
  - On `allowed=True`, persists the `ActionPlan` via `PlanRepository.create()` and transitions the run `investigating -> needs_review`.
  - On 3 rejections: transitions to `escalated` (`policy_violation`, no plan created).
  - On `decision="decline"`: transitions to `escalated` (`model_declined`, no plan created).
- `outcome_kind == "diagnosed"` and `root_cause_category == "false_alarm"`: `finalize_node` transitions directly `investigating -> completed` with `resolution="false_alarm_closed"` and emits `no_action_recorded` (no `action_plans` row, guarded by `app.fn_runs_enforce_transition()`).
- `outcome_kind in ("unknown", "escalate")`: `finalize_node` transitions directly to `escalated` with `last_error`.

---

## 2. Policy Rules (`P1`–`P12`) & Backing Tables

| Rule | Typed Code | Backing Table / Source | Deterministic Check |
| :--- | :--- | :--- | :--- |
| **P1** | `diagnosis_required` | `Diagnosis`, `GateResult`, `Outcome` | Requires `outcome_kind == "diagnosed"`, `gate_result.passed == True`, and `root_cause_category != "false_alarm"`. |
| **P2** | `action_consistent_with_diagnosis` | [config/policy_table.yaml](../config/policy_table.yaml) | `(root_cause_category, mechanism)` must permit `proposal.action`. |
| **P3** | `service_matches_diagnosis` | `Diagnosis.root_cause_service` | Normalized `proposal.service` must equal diagnosed service (`inventory` for `dependency_failure`, never `redis`). |
| **P4** | `target_allowlisted` | [config/lab_targets.yaml](../config/lab_targets.yaml) | `proposal.service` resolves 1:1 to an allowlisted `target_id` (`lab-gateway`, `lab-orders`, `lab-inventory`). |
| **P5** | `precondition_current` | `TargetStateReader.current_state()` | `proposal.expected_version` equals live `current_version`. |
| **P6** | `rollback_possible` | `TargetStateReader.current_state()` | For `rollback_service`, `previous_version` exists and differs from `current_version`. |
| **P7** | `evidence_valid` | `app.observations`, `Diagnosis.evidence_ids` | All `rationale_evidence_ids` exist in this run, inside incident window, from `ALLOWED_TOOL_SOURCES`, and overlap `Diagnosis.evidence_ids`. |
| **P8** | `one_active_plan` | `app.action_plans` | Existing `proposed` plan is superseded (`supersedes_plan_id`); `approved`/`executed` plan blocks new proposals. |
| **P9** | `no_recent_action` | `TargetState.last_action_at`, `recent_executions` | No executed action on `target_id` within `ACTION_COOLDOWN_MINUTES = 10`. |
| **P10** | `single_effect` | `ProposalInput` | Exactly one scalar `service` and one scalar `action`. |
| **P11** | `culprit_deploy_mismatch` | `Diagnosis.culprit_deploy_id`, `observations` | For `rollback_service`, `expected_version` matches culprit deploy version when present. |
| **P12** | `action_unsupported_by_target` | [config/lab_targets.yaml](../config/lab_targets.yaml) | `proposal.action` is in `TargetSpec.allowed_actions`. |

---

## 3. Plan Fields & Canonical `plan_hash` Definition

Implemented in [shared/plans.py](../shared/plans.py) (reused by [apps/api/models/hashing.py](../apps/api/models/hashing.py)):
- **Model-supplied (`ProposalInput`, `extra="forbid"`)**: `action` (`rollback_service` | `restart_service`), `service`, `expected_version`, `rationale_evidence_ids` (1–8), `note` (max 300 chars plain text, control chars stripped).
- **Server-derived (`ActionPlan`, frozen)**: `plan_id`, `tenant_id`, `run_id`, `target_id`, `rollback_to_version`, `rollback_route`, `created_at`, `expires_at` (from DB `clock_timestamp()`), `plan_hash`, `status` (`PlanStatus` enum).
- **Hashed Fields in `plan_hash`**: `action`, `expected_version`, `expires_at` (UTC ISO-8601 `Z`), `rationale_evidence_ids` (sorted deduplicated list), `rollback_to_version`, `run_id`, `service`, `target_id`, `tenant_id`. Serialized as compact key-sorted JSON (`separators=(",", ":")`) and hashed with SHA-256. Unhashed fields (`note`, `status`, `plan_id`, `created_at`, `rollback_route`) are excluded.

---

## 4. Prompt Version, Policy Version & Fail-Closed Behavior

- **Proposal Prompt (`proposal_v1.md`)**: SHA-256 `073d2e5226166cce142c1ffd7672d98a75bd974ebe587e26fb07c611296ea8b7` (committed in [agent/prompts/prompt_hashes.json](../agent/prompts/prompt_hashes.json), zero-leakage verified). `investigator_system_v2.md` (`ade9f1e5...`) remains frozen.
- **Policy Version (`policy_version`)**: `df6da5f8fd9b82d7` (SHA-256 prefix over `config/policy_table.yaml`, `config/lab_targets.yaml`, and rule codes).
- **Fail-Closed Behavior**: Any `TargetStateError` (`state_unavailable`, `stale_state`), missing/corrupt config table, or unexpected exception in `validate()` returns `allowed=False` with code `policy_unavailable` (never `allowed=True`). Persistence/optimistic-lock failures in `PlanRepository.create()` roll back the savepoint and route `propose_node` to `escalated` (`persistence_failed` / `concurrent_modification`).

---

## 5. Known Limits & Deviations from `proposal_design.md`

1. **P13 Handled at Schema Layer**: Note sanitization and length capping (`<= 300` chars) are enforced in `ProposalInput` / `sanitize_note()` rather than as a Policy Validator rejection code (`P13`), leaving policy rules as `P1`–`P12` (per Section 12 decision #6).
2. **Live Gemini Evaluation Blocked Upstream**: In [docs/proposal_report_v1.md](proposal_report_v1.md), `15/15` live trials failed on Turn 1 of `investigator v2` (`FlatAgentStepV2` `400 INVALID_ARGUMENT` on `additional_properties`) before reaching `propose_node`. All verified end-to-end proposal behavior comes from the 70 offline deterministic tests.
3. **Offline SQLite Fallback in Unit Tests**: When PostgreSQL on `127.0.0.1:54333` is not running, `test_db_immutability_trigger_rejects_direct_sql_update` skips cleanly and SQL parity tests verify against the offline SQL-equivalent builder; live PostgreSQL trigger enforcement runs in CI with the Postgres service container.
