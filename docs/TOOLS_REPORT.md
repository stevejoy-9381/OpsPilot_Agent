# Tools Report (Feature 2 Close)

This report documents the implementation of the read tool suite for OpsPilot, specifying tool inventory, schemas, runtime bounds, error code behavior, isolation guarantees, and design choices.

---

## 1. Tool Inventory & Contracts

OpsPilot equips investigating agents with 5 strictly bounded, read-only tools:

| Tool Name | Module | Primary Purpose | Load-Bearing Scenarios |
| :--- | :--- | :--- | :--- |
| **`get_metrics`** | [tools/read/metrics.py](file:///e:/opspilot/tools/read/metrics.py) | Downsampled numeric time series with LTTB ($\le 30$ pts), statistical summaries, and automatic baseline comparison over preceding window. | **S1, S5** |
| **`search_logs`** | [tools/read/logs.py](file:///e:/opspilot/tools/read/logs.py) | Substring search with full-window statistical aggregation (counts by level/event, top 5 message templates) and sampled ordered rows. | **S3, S4** |
| **`get_trace`** | [tools/read/traces.py](file:///e:/opspilot/tools/read/traces.py) | Distributed span tree inspection with automatic collapsing of repeated adjacent sibling spans ($\le 40$ spans), critical path, and candidate search mode. | **S2, S3, S4** |
| **`list_deploys`** | [tools/read/deploys.py](file:///e:/opspilot/tools/read/deploys.py) | Recent deployment discovery newest first with `seconds_before_window_end` proximity metric. | **S1, S2, S4** |
| **`get_deploy_diff`** | [tools/read/deploys.py](file:///e:/opspilot/tools/read/deploys.py) | Redacted, quarantined diff inspection between two deploy versions for an authorized service. | **S1, S2, S4** |

---

## 2. Hard Bounds and Constraints

Configured in [tools/common/limits.py](file:///e:/opspilot/tools/common/limits.py):
- **Window Limit**: Default 1 hour maximum span per tool query.
- **Metric Points**: Hard ceiling of 30 points per time series (downsampled via LTTB).
- **Log Limit**: Hard ceiling of 50 rows per response; lines capped at 250 characters.
- **Trace Spans**: Hard ceiling of 40 spans per trace tree (collapses repetitive siblings with `repeat_count`).
- **Diff Characters**: Capped at 4,000 characters with `[truncated N chars]` notice.
- **Call Budget**: Strict maximum of 8 tool calls per investigation (enforced server-side in `tools/registry.py`).
- **Token Budget**: 1,500 token target per tool response (all 5 tools profile at $\le 554$ tokens p95).

---

## 3. Error Codes & Error Envelope

All tools return a discriminated union `ToolSuccess[T] | ToolError` and never raise unhandled exceptions:
- `invalid_argument`: Malformed arguments, unrecognized enums, reversed windows, extra model fields.
- `out_of_window`: Requested timestamps outside `[window_start, window_end]`.
- `access_denied`: Attempting to query services outside `ToolContext.allowed_services`.
- `not_found`: Service metric, trace ID, or deployment version tag not found.
- `stale_data`: Telemetry lag exceeding 120 seconds.
- `source_unavailable`: Backend timeouts (15s limit) or filesystem errors.
- `budget_exhausted`: Invoked after `remaining_calls == 0`.

---

## 4. Isolation & Security Guarantees

1. **Untrusted Data Quarantine**: Log text, span attributes, and diffs are wrapped in `<untrusted_data origin="...">` envelopes. Any closing tags inside content are escaped (`&lt;/untrusted_data&gt;`).
2. **Deterministic Redaction**: Bearer tokens, passwords, JWTs, and API keys are masked with `[REDACTED:<type>]`.
3. **Tenant & Run Partitioning**: `EvidenceStore` enforces append-only JSONL files partitioned by tenant and run. Cross-tenant and cross-run reads are rejected.
4. **Duplicate Call Cache**: Identical calls return cached results without consuming budget.
5. **No Model-Controlled PromQL**: PromQL queries use fixed server-side templates with allowlisted service names and metric enums.

---

## 5. Deviations from `tool_contract.md`

- **Single Trace Tool**: Full trace retrieval and candidate search were consolidated into a single `get_trace` tool with mutually exclusive parameters (`trace_id` vs `service + window`).
- **Evidence Storage**: Built `InMemoryEvidenceStore` and `JsonlEvidenceStore` protocol foundations in Feature 2 ahead of Feature 4's PostgreSQL persistence.
