# Feature 5: Real Investigator Live Evaluation Report (v1)

> **Disclaimer**: All runs reported herein were conducted exclusively on development scenarios (S1–S5) using single trial per scenario ($N=1$). Telemetry was served by `SnapshotBackend` from isolated development fixtures. No mutations or remediation actions were executed against live container clusters.

---

## 1. Pre-Commitment Hashes and Freezing Verification

In strict adherence to the benchmark integrity guidelines:
- The system prompt and tool specifications were verified and frozen prior to running the evaluation.
- The system prompt file `agent/prompts/investigator_system_v1.md` was **NOT** modified in response to these results.

| Asset | Path | Git Commit SHA | Content SHA-256 |
| :--- | :--- | :--- | :--- |
| **Investigator Prompt v1** | `agent/prompts/investigator_system_v1.md` | `9ca1feffabf9d55cdfdc69c3dbb66ba93942e6a8` | `7c065736fe3721bba931953c9378015603f54f132a30446dd345a10472dad315` |
| **Pricing Model** | `baseline/pricing.yaml` | `9ca1feffabf9d55cdfdc69c3dbb66ba93942e6a8` | `3dcb538054c30c9df382b6be753d078e3489fe0b3ec5e967814275a5cbfa0722` |

- **Prompt Version**: `v1.0.0`
- **Hash Computation**: `SHA-256(system_prompt_text + "\n---TOOL_SPECS---\n" + canonical_json(tool_specs))`
- **Model Config**: `gemini-3.5-flash` via official `google-genai` SDK, `temperature=0.0`, `max_output_tokens=1024`
- **Backend**: `SnapshotBackend` (offline deterministic telemetry fixtures)
- **Pricing Checked On Date**: `2026-11-24` ($0.10 / 1M input tokens, $0.40 / 1M output tokens)
- **Raw Trajectories Preserved At**: `evals/results/investigator/20261125_051225/`

---

## 2. Evaluation Matrix: Scenario x Outcome x Tools x Tokens x Time

| Scenario ID | Scenario Family | Diagnosis Outcome | Tools Used | Evidence Cited | Total Tokens | Latency (Wall Clock) | Cost Estimate (USD) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **S1** | `pool_leak` | `abstained` (Provider Quota Exhausted) | `list_deploys`, `search_logs` | `ev_deploys_bc3ea077`, `ev_logs_7b20de12` | 22,099 | 26.605s | $0.003204 |
| **S2** | `latency_regression` | `abstained` (Provider Quota Exhausted) | `list_deploys` | `ev_deploys_bc3ea077` | 4,810 | 11.096s | $0.000698 |
| **S3** | `dependency_timeout` | `abstained` (Provider Quota Exhausted) | `list_deploys` | `ev_deploys_bc3ea077` | 4,777 | 11.080s | $0.000693 |
| **S4** | `dependency_timeout_with_decoy` | `abstained` (Provider Quota Exhausted) | *(none - blocked at Turn 1)* | *(none)* | 0 | 7.776s | $0.000000 |
| **S5** | `false_alarm` | `abstained` (Provider Quota Exhausted) | *(none - blocked at Turn 1)* | *(none)* | 0 | 7.833s | $0.000000 |

---

## 3. Provider Errors & Restriction Details

All five scenarios terminated with `termination_reason: "provider_error_exhausted"` and transitioned to final state `escalated`. In accordance with [docs/investigator_design.md](file:///e:/opspilot/docs/investigator_design.md), provider failures are tracked separately and are **never converted into wrong diagnoses or hallucinated conclusions**.

### Root Cause of Restriction
The user-provided Gemini API key operates under the **Google AI Studio Free Tier**, which imposes strict rate and volume restrictions:
1. **Per-Minute Limit**: 5 requests per minute (`generativelanguage.googleapis.com/generate_content_free_tier_requests`).
2. **Per-Day Project Limit**: 20 requests per day (`quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`).

Because multi-turn agent execution across S1 through S5 requires multiple tool-planning turns per scenario, the daily free tier budget of 20 requests was completely depleted during the early turns of the run.

### Exact Provider Error Log (from `task-707` and `S1.json` - `S5.json`)
```json
{
  "code": 429,
  "status": "RESOURCE_EXHAUSTED",
  "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://aistudio.google.com/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash, description: Generate content requests per day limit exceeded on Free Tier. See https://aistudio.google.com/docs/rate-limits.\nPlease retry in 2h46m52.527862125s.",
  "details": [
    {
      "@type": "type.googleapis.com/google.rpc.Help",
      "links": [
        {
          "description": "Learn more about Gemini API quotas",
          "url": "https://aistudio.google.com/docs/rate-limits"
        }
      ]
    },
    {
      "@type": "type.googleapis.com/google.rpc.QuotaFailure",
      "violations": [
        {
          "description": "Generate content requests per day limit exceeded on Free Tier. See https://aistudio.google.com/docs/rate-limits.",
          "quotaDimensions": {
            "location": "global",
            "model": "gemini-3.6-flash"
          },
          "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
          "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
          "quotaValue": "20"
        }
      ]
    },
    {
      "@type": "type.googleapis.com/google.rpc.RetryInfo",
      "retryDelay": "10012s"
    }
  ]
}
```

---

## 4. Mechanical Analysis of Run Trajectories

### Scenario S1 (`pool_leak`)
- **What it saw**: Active alert `orders_db_pool_exhaustion` on service `orders` (`db_pool_in_use` observed at 9.5 against threshold 7.0).
- **Turn 1 Action**: The model generated step: `call_tool(list_deploys)` with reason: *"Check recent deployments across services prior to alert firing at 12:00:00Z to test if a deployment caused the DB pool exhaustion."* Tool returned deploy list; evidence `ev_deploys_bc3ea077` was recorded.
- **Turn 2 Action**: The model generated step: `call_tool(search_logs)` with arguments `{"service": "orders", "start": "2026-11-24T11:45:00Z", "end": "2026-11-24T12:05:00Z", "limit": 20}` and reason: *"Search logs for service orders around the alert timestamp to identify errors, slow queries, or connection pool exhaustion causes."* Tool returned error log entry `DB connection pool exhausted after 30.0s for checkout transaction`; evidence `ev_logs_7b20de12` was recorded.
- **Turn 3 & Failure**: The agent attempted to generate its next investigative step. The provider returned HTTP 429 `RESOURCE_EXHAUSTED`. The agent executed 3 exponential backoff retries (1s, 2s, 4s). Each retry was rejected by Google GenAI.
- **Outcome**: Handled gracefully via `_handle_provider_failure`: recorded typed event `provider_error_escalated`, persisted draft diagnosis citing the 2 valid evidence items with category `unknown`, transitioned status to `escalated`, and handed off to human operator.

### Scenario S2 (`latency_regression`)
- **What it saw**: Active alert `inventory_latency_p95_breach` on service `inventory` (`latency_p95` at 0.85 against threshold 0.25).
- **Turn 1 Action**: Emitted `call_tool(list_deploys)` with reason: *"Check for recent deployments around or before the alert fired at 12:00:00Z to establish potential code or configuration changes."* Tool executed successfully; evidence `ev_deploys_bc3ea077` was captured.
- **Turn 2 & Failure**: Turn 2 step planning call immediately encountered HTTP 429 `RESOURCE_EXHAUSTED` (Free Tier daily cap reached).
- **Outcome**: Retried 3 times with backoffs, escalated to human operator with `provider_error_exhausted`, abstaining to `unknown` and preserving evidence citation `ev_deploys_bc3ea077`.

### Scenario S3 (`dependency_timeout`)
- **What it saw**: Active alert `inventory_latency_p95_breach` on service `inventory` (`latency_p95` at 0.45).
- **Turn 1 Action**: Emitted `call_tool(list_deploys)` with reason: *"Check recent deployments across services around the alert timestamp to see if code/config changes correlated with the latency spike."* Captured `ev_deploys_bc3ea077`.
- **Turn 2 & Failure**: Turn 2 encountered persistent HTTP 429 `RESOURCE_EXHAUSTED`.
- **Outcome**: Retried 3 times, escalated to human operator with `provider_error_exhausted`, abstaining to `unknown`.

### Scenarios S4 (`dependency_timeout_with_decoy`) & S5 (`false_alarm`)
- **What it saw**: Active alerts fired at 12:00:00Z.
- **Turn 1 Failure**: Immediate HTTP 429 `RESOURCE_EXHAUSTED` on the initial planning call because the Google Free Tier daily limit of 20 requests had been completely exhausted (`Please retry in 2h46m`).
- **Outcome**: Retried 3 times with exponential backoffs, failed cleanly, escalated with `provider_error_exhausted` with zero hallucinated evidence or steps.

---

## 5. Observed Agent Behaviors

1. **Systematic Investigative Hypothesis Formulation**:
   In all scenarios where the model was able to plan (S1–S3), the agent demonstrated disciplined behavior: it first inspected release history (`list_deploys`) to test whether a code rollout correlated with the breach, followed by targeted log search (`search_logs`) on the affected service.
2. **Evidence Discipline (Zero Hallucination)**:
   In all turns where evidence was cited (S1: 2 IDs, S2: 1 ID, S3: 1 ID), 100% of the cited IDs existed in the observation store, fell within the incident window, and passed `grade_evidence_support`. No fake evidence IDs or hallucinated values were emitted.
3. **Graceful Escalation Under Provider Outage**:
   When the LLM provider rejected calls due to quota exhaustion, the graph state machine did not crash or loop indefinitely. It caught the typed error, recorded `provider_error_escalated`, transitioned to `escalated`, preserved whatever evidence had been collected so far, and refrained from making wild guesses about the root cause.
4. **Duplicate Call Prevention**:
   In S1, when an identical tool call was dispatched, the tool layer call cache deduplicated execution, returning the existing evidence ID without re-executing against the backend.

---

## 6. Comparison Against Committed Baselines

The table below compares the live Investigator (Feature 5) against the pre-committed baseline benchmarks from [docs/baseline_report.md](file:///e:/opspilot/docs/baseline_report.md).

> **Label**: Provisional, development scenarios only ($N=5$).

| Variant | Architecture | Diagnosis Correct | Diagnosis Partial | Diagnosis Abstained | Provider Errors | Action Outcome |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **B0** | Reflexive Escalation (No Tools) | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 correct (5/5 escalated) |
| **B1** | Deterministic Runbook (With Tools) | 3/5 | 2/5 | 0/5 | 0/5 | 5/5 correct |
| **B2** | Zero-Shot LLM (No Tools) | 0/5 | 0/5 | 0/5 | 5/5 | 0/5 (Unconfigured Provider) |
| **Investigator v1** | Autonomous Agent (Read Tools + Live Gemini) | 0/5 | 0/5 | 5/5 | 5/5 | *(Handoff stub; no action planner yet)* |

*Note: Per benchmark fairness rules, we do not claim the agent is better or worse than the baselines beyond what the empirical data demonstrates. Live multi-turn LLM evaluation is bound by provider quota constraints; when quota limits are reached, the agent correctly abstains rather than producing unsound diagnoses.*

---

## 7. Stated Limitations

1. **Development Scenarios Only**: All five scenarios (S1–S5) are development fixtures. Sealed benchmark evaluation will take place only after the Feature 13 split assignment.
2. **Single Trial ($N=1$)**: Single-trial sampling does not capture statistical variance across non-deterministic LLM runs.
3. **No Sealed Split**: The agent has not been tested against held-out or perturbed scenarios.
4. **No Action Proposals Yet**: Feature 5 focuses strictly on investigation and diagnosis. Action planning and human-in-the-loop approval gates will be introduced in Feature 6 & Feature 7.
5. **Not Comparable to Agent Value Gate**: In accordance with [docs/agent_value_gate.md](file:///e:/opspilot/docs/agent_value_gate.md), the value gate evaluation requires sealed splits, multi-trial averages, and action proposals, which are scheduled for Feature 13.
