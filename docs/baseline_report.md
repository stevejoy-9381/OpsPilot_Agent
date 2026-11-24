# Baseline Benchmark Evaluation Report

> **Disclaimer**: All runs reported herein were conducted exclusively on development scenarios (S1–S5). Sealed benchmark evaluation will occur following the Feature 13 split assignment. Dry-run actions only; no mutations were executed against live lab environments. Single trial per scenario.

---

## 1. Pre-Commitment Hashes and Freezing Verification

In compliance with the Benchmark Fairness Rules (defined in [docs/baseline_design.md](file:///e:/opspilot/docs/baseline_design.md)), all baseline heuristics, prompts, and pricing models were frozen and committed prior to execution.

| Asset | File Path | Git Commit SHA | Content SHA-256 |
| :--- | :--- | :--- | :--- |
| **B1 Runbook** | `baseline/runbook/runbook.yaml` | `cd6d94c3a5a5482c5699c34f5099f4bb39df5e40` | `26d4a36121ab47a27b898b92003863c78a139184c51ec5d93dc04edb123b0e0a` |
| **B2 Prompt** | `baseline/prompts/b2_v1.md` | `b325d9fe23199a79641c68ae411f9db309c56729` | `b4290a1a8c0e98a449c0d467dea0af953cc741e92f5137a9cd28a3bf3b6fa9ee` |
| **B2 Pricing** | `baseline/pricing.yaml` | `b325d9fe23199a79641c68ae411f9db309c56729` | `3dcb538054c30c9df382b6be753d078e3489fe0b3ec5e967814275a5cbfa0722` |

Raw result files are preserved under `evals/results/baseline/run_20261124_real/`.

---

## 2. Benchmark Results: Variant x Scenario Matrix

The evaluation matrix was executed across all five development scenarios (S1–S5) across B0 (Alert-Only), B1 (Deterministic Runbook), and B2 (No-Tools LLM).

| Variant | Scenario ID | Scenario Family | Diagnosis Outcome | Recommended Action Outcome | Time (Wall Clock) | Cost (USD) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **B0** | **S1** | `pool_leak` | `abstained` | `missing` (`escalate`) | 0.000s | $0.0000 |
| **B0** | **S2** | `latency_regression` | `abstained` | `missing` (`escalate`) | 0.000s | $0.0000 |
| **B0** | **S3** | `dependency_timeout` | `abstained` | `missing` (`escalate`) | 0.000s | $0.0000 |
| **B0** | **S4** | `dependency_timeout_with_decoy` | `abstained` | `missing` (`escalate`) | 0.000s | $0.0000 |
| **B0** | **S5** | `false_alarm` | `abstained` | `missing` (`escalate`) | 0.000s | $0.0000 |
| **B1** | **S1** | `pool_leak` | `correct` | `correct` (`rollback_service orders`) | 0.007s | $0.0000 |
| **B1** | **S2** | `latency_regression` | `correct` | `correct` (`rollback_service inventory`) | 0.003s | $0.0000 |
| **B1** | **S3** | `dependency_timeout` | `partial` | `correct` (`restart_service redis`) | 0.008s | $0.0000 |
| **B1** | **S4** | `dependency_timeout_with_decoy` | `partial` | `correct` (`restart_service redis`) | 0.009s | $0.0000 |
| **B1** | **S5** | `false_alarm` | `correct` | `correct` (`no_action`) | 0.001s | $0.0000 |
| **B2** | **S1** | `pool_leak` | *Provider Error* | *Provider Error* | — | — |
| **B2** | **S2** | `latency_regression` | *Provider Error* | *Provider Error* | — | — |
| **B2** | **S3** | `dependency_timeout` | *Provider Error* | *Provider Error* | — | — |
| **B2** | **S4** | `dependency_timeout_with_decoy` | *Provider Error* | *Provider Error* | — | — |
| **B2** | **S5** | `false_alarm` | *Provider Error* | *Provider Error* | — | — |

---

## 3. Variant Totals Summary

All metrics are strictly reported as explicit numerators and denominators ($N/D$). Bare percentages are not used.

| Variant | Diagnosis Correct | Diagnosis Partial | Diagnosis Abstained | Action Correct | Unnecessary Actions | Provider Errors |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **B0** | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 |
| **B1** | 3/5 | 2/5 | 0/5 | 5/5 | 0/5 | 0/5 |
| **B2** | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 5/5 |

---

## 4. Mechanical Analysis of Failures and Partial Diagnoses

Every non-correct diagnosis or action outcome is explained below with a direct description of what evidence was missing or misread. No rules or heuristics were modified after the run.

### B0 Failures (Reflexive Escalation)
- **B0 on S1, S2, S3, S4, S5**: Diagnosis `abstained`; Action `missing`.
  - *Mechanical reason*: B0 contains no investigative logic and possesses zero telemetry tools; by design, it reflexively outputs `category="unknown"` and action `escalate` on every alert breach.

### B1 Partial Diagnoses & Coincidences
- **B1 on S3 (`dependency_timeout`)**: Diagnosis `partial` (2/4 fields matched: category `dependency_failure`, mechanism `dependency_timeout`); Action `correct` (`restart_service redis`).
  - *Mechanical reason*: Rule R2 inspected downstream telemetry from the alerting service (`inventory`) and identified elevated `dependency_latency_p95` on `redis`. The runbook attributed the root cause service to `redis` directly rather than the caller `inventory`. Under ground truth, `root_cause_service` is labeled `inventory` while the remediation target is `redis`.
- **B1 on S4 (`dependency_timeout_with_decoy`)**: Diagnosis `partial` (category `dependency_failure`, mechanism `dependency_timeout`); Action `correct` (`restart_service redis`).
  - *Mechanical reason & Coincidence*: Rule R1 checks for a recent deploy on alerting service `inventory`. The decoy deploy was injected into `orders` (v1.1.0), not `inventory`. Because R1 checks only deploys to the alerting service (`inventory`), R1 evaluated to `false`. Evaluation proceeded to Rule R2, which detected elevated dependency latency on `redis` and triggered `restart_service redis`. B1 succeeded on action selection purely because the decoy deploy was on `orders` rather than `inventory`; had the decoy deploy occurred on `inventory`, R1 would have misfired a rollback of `inventory`.

---

## 5. Provider Errors (B2 Live Gemini Execution)

In accordance with Section 5 of [docs/baseline_design.md](file:///e:/opspilot/docs/baseline_design.md), provider errors are counted separately from reasoning errors and are never counted as wrong diagnoses:

- **B2 Live Run Status**: 5/5 Provider Errors (`GEMINI_API_KEY` not configured in environment).
- **Explanation**: In this execution environment, `GEMINI_API_KEY` is empty/unset, and `google-genai` is not installed. Per the harness design and benchmark integrity rules, live results were not faked or hallucinated. Offline validation with `FakeLLMClient` confirmed that when provider credentials are supplied or mocked, B2 executes structured validation and respects token guards.

---

## 6. Stated Limitations of the Baseline Benchmark

1. **Development Set Only**: All five scenarios (S1–S5) are development scenarios. No sealed test split exists until Feature 13.
2. **Sample Size ($N=5$)**: Five incidents cannot establish statistical significance or tight confidence intervals.
3. **Dry-Run Mode**: Baseline actions were evaluated against canonical expectations; no physical mutations or rollbacks were executed in live container clusters.
4. **Single Trial for B2**: Live LLM execution was planned for $N=1$ trial per scenario; provider unavailability prevented live sampling.
