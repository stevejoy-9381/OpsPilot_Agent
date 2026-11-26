# OpsPilot Proposal Step v1 Live Evaluation Report

## 1. Run Configuration & Frozen Artifacts

This report records the single live end-to-end evaluation (`investigator v2` $\to$ `propose_node` + Policy Validator) across development scenarios `S1`–`S5` (`trials=3` per scenario, `15` runs total).

- **Git SHA**: `822ed91c01cd9fb1fddcd41ab1ec2c1ae51842c6`
- **Investigator Prompt v2 SHA-256**: `ade9f1e57fb55a291ed7ec472428abe4e8497b9ab39a2a985fbd3f3efa0cb97a` (frozen)
- **Proposal Prompt v1 SHA-256**: `073d2e5226166cce142c1ffd7672d98a75bd974ebe587e26fb07c611296ea8b7` (unchanged since 7.5)
- **Policy Version (`policy_version`)**: `df6da5f8fd9b82d7` (`config/policy_table.yaml` + `config/lab_targets.yaml`, unchanged since 7.5)
- **Model & Settings**: `gemini-3.5-flash`, `temperature=0.0`, `max_output_tokens=1024`, `spending_cap_tokens_per_run=50000`
- **Backend & Target Reader**: `SnapshotBackend` and `SnapshotTargetReader` (offline deterministic snapshot fixtures)
- **Pricing Source**: [baseline/pricing.yaml](../baseline/pricing.yaml) (`checked_on: 2026-11-24`, `unverified: true`)
- **Raw Results Directory**: [evals/results/proposal_v1/20261125_153328/](../evals/results/proposal_v1/20261125_153328/)

---

## 2. Proposal & Outcome Summary (Numerator / Denominator)

| Metric | Measured Result | Notes |
| :--- | :---: | :--- |
| **Proposals First-Try Valid** | `0/15` | `0/15` runs reached `propose_node` (all `15/15` terminated on Turn 1 of `investigator v2`). |
| **Wrong-Action Proposals Caught** | `0/0` (`0/15` runs) | No proposal reached the Policy Validator in live runs. |
| **Escalations by Reason: `provider_error_exhausted`** | `15/15` | `400 INVALID_ARGUMENT` on `FlatAgentStepV2` schema in `plan_step`. |
| **Escalations by Reason: `policy_violation`** | `0/15` | — |
| **Escalations by Reason: `model_declined`** | `0/15` | — |
| **Unnecessary Actions (`S5`)** | `0/3` | `S5` escalated (`provider_error_exhausted`) without creating any plan. |
| **Decline Rate** | `0/15` | — |
| **Provider Errors (Separated)** | `15/15` | All `15/15` occurred on Turn 1 before any tool call or proposal. |
| **Total Tokens / Estimated Cost / Wall Time** | `0` tokens / `\$0.00000` / `8.159s` | Mean wall time: `0.544s` per trial. |

### Rejections by Policy Code

| Policy Code | Rejections |
| :--- | :---: |
| `P1`–`P12` (all codes) | `0/0` *(no proposal attempts reached the validator)* |

### Per-Scenario Breakdown

| Scenario | Trials | `needs_review` | Action Correct | Escalated (`provider_error_exhausted`) | Unnecessary Action | Provider Errors |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **S1** | `3` | `0/3` | `0/3` | `3/3` | `0/3` | `3/3` |
| **S2** | `3` | `0/3` | `0/3` | `3/3` | `0/3` | `3/3` |
| **S3** | `3` | `0/3` | `0/3` | `3/3` | `0/3` | `3/3` |
| **S4** | `3` | `0/3` | `0/3` | `3/3` | `0/3` | `3/3` |
| **S5** | `3` | `0/3` | `0/3` | `3/3` | `0/3` | `3/3` |

---

## 3. Mechanical Explanation of Every Failure

- **`15/15` Trials (`S1`–`S5`, trials 1–3) — `provider_error_exhausted`**:
  Every trial terminated on the very first LLM invocation inside `plan_step_node` (`schema=FlatAgentStepV2`) with Gemini API error:
  `400 INVALID_ARGUMENT: Invalid JSON payload received. Unknown name "additional_properties" at 'generation_config.response_schema'`.
  Because `investigator_system_v2.md` and `FlatAgentStepV2` (frozen in Feature 6) emit `additionalProperties` / `additional_properties` in the Pydantic JSON schema passed to `google-genai`, the Gemini API rejects the request prior to token generation (`total_tokens=0`). Consequently, no investigation completed (`0/15` diagnosed) and `propose_node` (`proposal_v1.md`) was never reached (`0/15` proposal attempts).

---

## 4. Provisional Baseline Comparison (Action Correctness Only)

> **Provisional, development scenarios only** (`S1`–`S5`; B0/B1 had `1` trial per scenario = `5` trials each, while live Proposal v1 had `3` trials per scenario = `15` trials).

| System | Action Correct | Unnecessary Actions | Provider Errors | Notes |
| :--- | :---: | :---: | :---: | :--- |
| **B0** (Alert-Only) | `0/5` | `0/5` | `0/5` | Reflexive escalation on all 5 scenarios. |
| **B1** (Deterministic Runbook) | `5/5` | `0/5` | `0/5` | Rule-based; `S3`/`S4` mapped to `restart_service redis`. |
| **Investigator v2 → Proposal v1 (live)** | `0/15` | `0/15` | `15/15` | Blocked upstream by `FlatAgentStepV2` `400 INVALID_ARGUMENT` in `gemini-3.5-flash`. |

---

## 5. Honest Limits

1. **Five Development Scenarios Only (`S1`–`S5`)**: No sealed evaluation split exists yet (Feature 13).
2. **Three Trials Per Scenario (`N = 15`)**: Small sample size.
3. **Zero Executions**: Nothing was executed against a live lab or broker (Feature 9).
4. **Upstream Schema Block**: Because `investigator v2` (`FlatAgentStepV2`) failed `15/15` on Gemini `response_schema`, this live run measures end-to-end failure at the `v2` schema boundary rather than live LLM performance on `proposal_v1.md`. Deterministic offline coverage of `propose_node` and `PolicyValidator` is recorded separately in [tests/test_proposal_e2e_deterministic.py](../tests/test_proposal_e2e_deterministic.py).
