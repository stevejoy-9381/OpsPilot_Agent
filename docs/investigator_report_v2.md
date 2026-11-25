# Investigator Prompt v1 vs v2 Live Evaluation Report

## 1. Run Configuration & Frozen Artifacts

This report records the single live comparison between `investigator_system_v1.md` (flat prompt without hypothesis ledger or gate) and `investigator_system_v2.md` (hypothesis ledger + deterministic gate + confidence cap table) across development scenarios `S1`–`S5` (`trials=3` per scenario per version, `30` runs total).

- **Git SHA (pre-run commit)**: `afadbc2092ed45efc3305cf77d144d8cde43841b`
- **Prompt v1 SHA-256**: `7c065736fe3721bba931953c9378015603f54f132a30446dd345a10472dad315`
- **Prompt v2 SHA-256**: `ade9f1e57fb55a291ed7ec472428abe4e8497b9ab39a2a985fbd3f3efa0cb97a` (unchanged since Feature 6.6)
- **Frozen Gate / Cap Artifacts**: `agent/graph/gate.py` and `agent/graph/confidence_caps.yaml` (unchanged since Feature 6.6)
- **Model & Settings**: `gemini-3.5-flash`, `temperature=0.0`, `max_output_tokens=1024`, `spending_cap_tokens_per_run=50000`
- **Backend**: `SnapshotBackend` (deterministic snapshot fixture data)
- **Pricing Source**: `baseline/pricing.yaml` (`checked_on: 2026-11-24`, `unverified: true`)
- **Raw Results Directory**: `evals/results/investigator_v2/20261125_124357/`

---

## 2. Scenario × Version × Outcome

All counts are reported strictly as `numerator/denominator`.

| Scenario | Version | Correct | Partial | Abstained | Wrong | Provider Error |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| S1 | v1 | 0/3 | 0/3 | 1/3 | 0/3 | 2/3 |
| S1 | v2 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S2 | v1 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S2 | v2 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S3 | v1 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S3 | v2 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S4 | v1 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S4 | v2 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S5 | v1 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |
| S5 | v2 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 |

---

## 3. Version Summary Metrics (v1 vs v2)

| Metric | v1 (15 trials) | v2 (15 trials) |
| :--- | :---: | :---: |
| Diagnosis Correct | 0/15 | 0/15 |
| Diagnosis Partial | 0/15 | 0/15 |
| Diagnosis Abstained (non-provider-error) | 1/15 | 0/15 |
| Diagnosis Wrong | 0/15 | 0/15 |
| Provider Errors (separated) | 14/15 | 15/15 |
| Run Status: `escalated` | 15/15 | 15/15 |
| Overconfident Count | 0/15 | 0/15 |
| Overcautious Count | 1/15 | 0/15 |
| Gate Rejections (total) | 0 | 0 |
| Hypotheses per Run (mean) | 0.00 | 0.00 |
| Tool Calls Used (total / mean per run) | 18 / 1.20 | 0 / 0.00 |
| Total Tokens (`input_tokens_est` / `output_tokens_est`) | 104,034 (88,429 / 15,605) | 0 (0 / 0) |
| Estimated Cost (total / mean per run) | \$0.01508 / \$0.00101 | \$0.00000 / \$0.00000 |
| Wall Time (total / mean per run) | 281.73s / 18.78s | 5.82s / 0.39s |

*Note*: On `v1`, `S1` trial 1 (`token_budget_exceeded`), `S1` trial 2 (`400 INVALID_ARGUMENT` after 8 tool calls), `S1` trial 3 (`429 RESOURCE_EXHAUSTED` after 1 tool call), and `S2` trial 3 (`429 RESOURCE_EXHAUSTED` after 3 tool calls) accounted for all `18` tool calls and `104,034` tokens (`46,560 + 38,973 + 4,338 + 14,163`). All `15/15` `v2` trials failed on the first LLM request with `400 INVALID_ARGUMENT` before executing any tool call or creating any hypothesis.

---

## 4. Provisional Baseline Comparison

**Provisional, development scenarios only** (`S1`–`S5`; B0/B1/B2 had `1` trial per scenario = `5` trials each, while live v1/v2 had `3` trials per scenario = `15` trials each).

| Agent / Version | Diagnosis Correct | Partial | Abstained | Provider Errors |
| :--- | :---: | :---: | :---: | :---: |
| B0 (rule-based abstain) | 0/5 | 0/5 | 5/5 | 0/5 |
| B1 (heuristic baseline) | 3/5 | 2/5 | 0/5 | 0/5 |
| B2 (live LLM baseline) | 0/5 | 0/5 | 0/5 | 5/5 |
| Investigator v1 (live) | 0/15 | 0/15 | 1/15 | 14/15 |
| Investigator v2 (live) | 0/15 | 0/15 | 0/15 | 15/15 |

Neither `v1` nor `v2` matched `B1` (`3/5` correct, `2/5` partial) in this live run because `29/30` trials terminated on provider errors (`400 INVALID_ARGUMENT`, `429 RESOURCE_EXHAUSTED`, or `RemoteProtocolError`) and the remaining `1/30` trial (`v1` `S1` trial 1) exhausted the `50,000`-token spending cap.

---

## 5. Mechanical Explanations of Every Failure

1. **Gemini `response_schema` Rejection (`400 INVALID_ARGUMENT`) — `15/15` v2 trials and `1/15` v1 trial (`S1` trial 2)**:
   - Every `v2` trial (`15/15`) and `v1` `S1` trial 2 (after 8 tool calls) failed with:
     `400 INVALID_ARGUMENT: Invalid JSON payload received. Unknown name "additional_properties" at 'generation_config.response_schema'`.
   - Mechanically, Pydantic's JSON schema for `AgentStepV2` (and a schema path reached in `v1` `S1` trial 2) emits `additionalProperties` / `additional_properties` (for `extra="forbid"` or `dict[str, Any]` fields), which the Gemini `response_schema` endpoint rejects as an unknown field. Because this occurs on the initial structured-output request for `v2`, `v2` executed `0` turns, `0` tool calls, and `0` gate evaluations.
2. **Google AI Studio Free Tier Rate & Daily Quota (`429 RESOURCE_EXHAUSTED`) — `12/15` v1 trials**:
   - `5/15` `v1` trials (`S1` t3, `S2` t1, `S2` t2, `S2` t3, `S3` t1) hit the `5` requests/minute limit (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, `quotaValue: 5`). `S1` t3 completed `1` tool call (`4,338` tokens) and `S2` t3 completed `3` tool calls (`14,163` tokens) before hitting the 5 RPM ceiling.
   - `7/15` `v1` trials (`S3` t2, `S3` t3, `S4` t1, `S4` t3, `S5` t1, `S5` t2, `S5` t3) hit the Free Tier daily request quota (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`).
3. **Token Budget Exhaustion (`token_budget_exceeded`) — `1/15` v1 trials (`S1` trial 1)**:
   - `v1` `S1` trial 1 consumed `46,560` tokens across `5` agent steps and `6` tool calls; requesting `5,346` additional tokens exceeded the `50,000`-token cap, escalating with `outcome=abstained` (`overcautious=True`). It also recorded `1` `citation_rejected` event (`unauthorized_tool_ids: ['ev_trace_89c8e89e']`).
4. **Unauthorized Tool Citation (`citation_rejected`) — `v1` `S2` trial 3**:
   - Before hitting `429 RESOURCE_EXHAUSTED` on turn 4, `v1` `S2` trial 3 emitted `1` `citation_rejected` event (`unauthorized_tool_ids: ['ev_trace_fc37fedf']`).
5. **Transient HTTP Disconnect (`RemoteProtocolError`) — `1/15` v1 trials (`S4` trial 2)**:
   - Terminated with `Server disconnected without sending a response`.

---

## 6. Limits

- **Five development scenarios only**: `S1`–`S5` are small development fixtures with no held-out or sealed split yet.
- **Small trial count**: `3` trials per scenario per version (`30` runs total).
- **Zero live v2 turns executed**: Because `400 INVALID_ARGUMENT` blocked all `15/15` `v2` runs at `generation_config.response_schema` validation, this run provides **zero empirical evidence** about `investigator_system_v2.md`'s reasoning quality, gate rejection behavior, or accuracy relative to `v1` or `B1`.
- **No proposals or actions**: Evaluates diagnosis and evidence support only; action proposals and the value gate are deferred to Feature 7 and Feature 13.
- **Unverified pricing**: Token cost estimates use `baseline/pricing.yaml` (`checked_on: 2026-11-24`, `unverified: true`).
