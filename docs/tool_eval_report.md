# Tool Evaluation & Ablation Report

This report evaluates the investigation tool suite across all five benchmark failure scenarios (S1–S5) using the deterministic scripted investigator ([evals/tools/scripted_investigator.py](file:///e:/opspilot/evals/tools/scripted_investigator.py)).

---

## 1. Executive Summary
- **Benchmark Goal**: Prove tools are sufficient and efficient before any LLM uses them.
- **Result**: **5/5 scenarios correctly diagnosed** within the 8-call budget ($3 \le \text{calls} \le 5$).
- **Token Budget Adherence**: **0 tool responses exceeded the 1,500 token budget target**. The largest response was $\approx 554$ tokens (search_logs).

---

## 2. Scenario Investigation Summary (Baseline)

| Scenario | Primary Fault / Mechanism | Calls Used | Call Budget | Result | Category |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **S1** | Orders DB Connection Pool Exhaustion | **4** | $\le 8$ | **DIAGNOSED** | `resource_exhaustion` |
| **S2** | Inventory N+1 DB Queries Loop | **5** | $\le 8$ | **DIAGNOSED** | `latency_regression` |
| **S3** | Inventory Upstream Supplier Timeout | **4** | $\le 8$ | **DIAGNOSED** | `dependency_timeout` |
| **S4** | Dependency Timeout with Decoy Deploy | **5** | $\le 8$ | **DIAGNOSED** | `dependency_timeout` |
| **S5** | Transient Metric Spike Self-Recovered | **3** | $\le 8$ | **DIAGNOSED** | `false_alarm` |

---

## 3. Tool Performance & Response Size Profile

All measurements taken over the complete benchmark run:

| Tool Name | Invocations | Chars (p50) | Chars (p95) | Approx Tokens (p50) | Approx Tokens (p95) | Avg Duration | Over Target (>1500 Tok) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `get_metrics` | 2 | 2,118 | 2,118 | 529 | 529 | 0.0 ms | **0** |
| `search_logs` | 4 | 2,140 | 2,218 | 534 | 554 | 4.0 ms | **0** |
| `get_trace` | 4 | 1,200 | 1,246 | 299 | 311 | 7.7 ms | **0** |
| `list_deploys` | 5 | 815 | 815 | 203 | 203 | 0.0 ms | **0** |
| `get_deploy_diff` | 3 | 763 | 814 | 190 | 203 | 31.3 ms | **0** |

---

## 4. Tool Ablation Study (Load-Bearing Analysis)

To identify which tools are load-bearing, each tool was systematically disabled one at a time and evaluated against the 5 benchmark scenarios:

| Disabled Tool | S1 (Pool Leak) | S2 (N+1 Query) | S3 (Timeout) | S4 (Decoy) | S5 (False Alarm) | Conclusion / Load-Bearing Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| *None (Baseline)* | DIAGNOSED | DIAGNOSED | DIAGNOSED | DIAGNOSED | DIAGNOSED | 5/5 Pass |
| `- get_metrics` | **UNDIAGNOSABLE** | DIAGNOSED | DIAGNOSED | DIAGNOSED | **UNDIAGNOSABLE** | **Critical**: Load-bearing for trend analysis (S1 pool rise) & S5 false alarm verification. |
| `- search_logs` | DIAGNOSED | DIAGNOSED | **UNDIAGNOSABLE** | **UNDIAGNOSABLE** | DIAGNOSED | **Critical**: Load-bearing for surfacing upstream HTTP 504 and warehouse supplier errors. |
| `- get_trace` | DIAGNOSED | **UNDIAGNOSABLE** | **UNDIAGNOSABLE** | **UNDIAGNOSABLE** | DIAGNOSED | **Critical**: Essential for identifying N+1 repeated queries (S2) and downstream latency attribution (S3, S4). |
| `- list_deploys` | DIAGNOSED | DIAGNOSED | DIAGNOSED | DIAGNOSED | DIAGNOSED | **Facilitating**: Provides candidate deploy identification before diffing. |
| `- get_deploy_diff` | **UNDIAGNOSABLE** | **UNDIAGNOSABLE** | DIAGNOSED | **UNDIAGNOSABLE** | DIAGNOSED | **Critical**: Load-bearing for proving code/config causation (S1, S2) and ruling out decoy deploys (S4). |

---

## 5. Tool Adjustments & Design Decisions

1. **Downsampling & Span Collapsing**:
   - `get_metrics` applies Largest Triangle Three Buckets (LTTB) downsampling capped at 30 points.
   - `get_trace` collapses repeated adjacent sibling spans (e.g., 50 sibling `db.query` spans collapsed to a single `50 x db.query` span with duration summation), ensuring trace payloads remain strictly under the 40-span ceiling.
2. **Whole-Window Log Aggregation**:
   - `search_logs` computes full-window message counts by level, event, and top 5 message templates while sampling only capped rows (first 5, last 5, and evenly spaced).
3. **No Call Budget Expansion**:
   - Every scenario succeeded in $\le 5$ tool calls, well below the maximum limit of 8 calls. No call budget expansion was needed.
