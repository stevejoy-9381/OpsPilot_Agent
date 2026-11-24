# Baseline Evaluation Ladder & Benchmark Design

> [!IMPORTANT]
> **PRE-COMMITMENT SPECIFICATION**
> This document specifies the evaluation baselines (B0, B1, B2), alert schemas, action vocabularies, fairness constraints, evaluation metrics, and the formal **Agent Value Gate**. All rules, schemas, and thresholds defined herein are committed **prior to the implementation of the investigation agent** to prevent hindsight bias and benchmark leakage.

---

## 1. The Baseline Ladder

To establish whether an autonomous, tool-calling investigation agent (OpsPilot) justifies its architectural complexity, cost, and latency, it must be evaluated against a progressive ladder of three non-agent baselines:

```
[ B0: Alert-Only ]          --->  [ B1: Runbook Heuristic ]  --->  [ B2: No-Tools LLM ]      --->  [ OpsPilot Agent ]
(Immediate Human Escalation)      (Deterministic Rules)            (One-Shot Zero-Tool LLM)         (Tool-Augmented Graph)
```

### B0: Alert-Only Baseline (Status Quo / Floor)
- **Mechanism**: The alert fires when an observation crosses a defined threshold. The baseline takes zero diagnostic steps and executes no analysis.
- **Output**: 
  - `diagnosis`: `None` (formally recorded as `abstained`).
  - `recommended_action`: `escalate`.
- **Purpose**: Represents traditional unassisted on-call paging. Establishes the empirical baseline for:
  - Zero token cost ($0.00).
  - Zero execution latency (~0s).
  - Absolute action safety ($0$ unnecessary rollbacks or restarts; $0$ unapproved writes).
  - Maximum human toil ($100\%$ escalation rate).

### B1: Deterministic Runbook Baseline (No-LLM Automation)
- **Mechanism**: Pure Python rule engine executing deterministic static logic over the incoming `AlertEvent` and a deployment registry query (recent deployments within the last 15 minutes). No LLM is invoked.
- **Rule Engine Specification**:
  1. **Deploy Correlation Rule**: Query deployment history for the service identified in `AlertEvent.service`.
     - If a deployment occurred on that service within the last 15 minutes:
       - Output: `rollback_service { service: AlertEvent.service }`.
       - Diagnosis: `category="bad_deploy"`, `mechanism="software_defect"`, `service=AlertEvent.service`, `culprit_deploy_id=latest_deploy_id`.
  2. **Decoy / Global Deploy Rule**: If no deployment occurred on `AlertEvent.service`, check if *any* service deployed within the last 15 minutes.
     - If another service deployed (e.g. `orders` deployed while `inventory` alerts):
       - Output: `rollback_service { service: deployed_service }`.
       - Diagnosis: `category="bad_deploy"`, `mechanism="software_defect"`, `service=deployed_service`, `culprit_deploy_id=latest_deploy_id`.
       *(Note: This models the common real-world on-call fallacy of blaming any recent deploy, triggering the decoy trap in S4).*
  3. **Dependency Heuristic Rule**: If no deployments exist in the window:
     - If `AlertEvent.metric == "latency_p95"` or `AlertEvent.rule_id` cites latency:
       - Output: `restart_service { service: "redis" }`.
       - Diagnosis: `category="dependency_failure"`, `mechanism="dependency_timeout"`, `service="inventory"`, `culprit_deploy_id=None`.
  4. **Fallback Escalation**: If no rule matches:
     - Output: `escalate`.
     - Diagnosis: `category="unknown"`, `mechanism="unknown"`, `service=None`, `culprit_deploy_id=None` (`abstained`).
- **Purpose**: Represents standard algorithmic alert correlation and automated runbooks found in mature SRE teams without AI.

### B2: No-Tools LLM Baseline (One-Shot Direct Inference)
- **Mechanism**: Exactly **one** synchronous LLM call. The model is prompted with:
  1. The exact incoming `AlertEvent`.
  2. A static system architecture summary (service topology, dependencies, database engines, traffic paths).
  3. The fixed action vocabulary enum and diagnosis schema.
- **Hard Constraints**:
  - **Zero Tools**: The model has no access to telemetry tools (`get_metrics`, `search_logs`, `get_trace`, `list_deploys`, `get_deploy_diff`).
  - **No Multi-Turn Loop**: Exactly one inference invocation; no reflection, re-prompting, or second turn.
  - **Same Model & Parameters**: Invoked using the identical LLM engine, version, and decoding parameters (temperature 0.0) as the OpsPilot agent.
- **Purpose**: Tests the "intuitive reasoning" hypothesis: can a modern LLM diagnose root causes and recommend recovery actions purely from alert symptoms and architecture documentation, or does accurate diagnosis fundamentally require interactive, tool-based evidence gathering?

---

## 2. The Alert Contract (`AlertEvent`)

The `AlertEvent` is the sole input artifact provided to B0, B1, B2, and the OpsPilot agent at the start of an incident investigation.

### Schema Definition (Pydantic / JSON)

```json
{
  "alert_id": "alt_20261124_001",
  "fired_at": "2026-11-24T12:05:00Z",
  "service": "gateway",
  "rule_id": "rule_http_high_error_rate",
  "metric": "error_rate",
  "threshold": 0.05,
  "observed_value": 0.28,
  "window_seconds": 60,
  "severity": "CRITICAL"
}
```

### Field Specifications

| Field | Type | Description | Allowed Values / Constraints |
| :--- | :--- | :--- | :--- |
| `alert_id` | `str` | Unique incident alert occurrence identifier. | UUID or formatted string (e.g. `alt_<hash>`). |
| `fired_at` | `str` (ISO-8601 UTC) | Timestamp when alerting rule breached threshold. | Must match scenario injection timeline. |
| `service` | `str` | Microservice where alerting threshold breached. | Enum: `"gateway"`, `"orders"`, `"inventory"`. |
| `rule_id` | `str` | Identifier of monitoring alert definition. | E.g. `rule_http_high_error_rate`, `rule_latency_p95_breach`. |
| `metric` | `str` | Metric name monitored by the rule. | Enum: `"error_rate"`, `"latency_p95"`, `"request_rate"`. |
| `threshold` | `float` | Configured boundary value that triggers the alert. | E.g. `0.05` ($5\%$), `250.0` (ms). |
| `observed_value` | `float` | Metric value observed over the alert window. | E.g. `0.28` ($28\%$), `480.0` (ms). |
| `window_seconds` | `int` | Lookback aggregation duration for alert rule. | Typically `60` or `120` seconds. |
| `severity` | `str` | PagerDuty / OpsGenie severity classification. | Enum: `"CRITICAL"`, `"WARNING"`. |

### Non-Leakage Invariant

> [!CAUTION]
> The `AlertEvent` payload must **never** reveal:
> 1. The root cause (e.g., no mention of `"pool_exhaustion"` or `"redis_timeout"`).
> 2. The scenario identifier (e.g., no `"S1"` or `"SCN-001"`).
> 3. The failure family (e.g., no `"pool_leak"` or `"dependency_timeout"`).
> 4. Any deployment, commit SHA, or version tag (e.g., no `"v1.3.0"` or `"deploy_orders_v130"`).
> 5. Decoy flags or ground-truth metadata.
>
> Alerts fire strictly at observation boundaries (e.g. ingress gateway error rate or downstream service p95 latency) as authentic monitoring systems would fire.

---

## 3. Fixed Action Vocabulary & Scenario Mapping

To enable deterministic, type-safe evaluation without ambiguous free-text matching, all baselines and the OpsPilot agent (Feature 7) must emit actions conforming strictly to a typed enumeration.

### The Action Vocabulary Enum

```python
from typing import Literal, Union
from pydantic import BaseModel, Field

AllowedService = Literal["gateway", "orders", "inventory", "redis", "postgres"]

class RollbackServiceAction(BaseModel):
    action: Literal["rollback_service"] = "rollback_service"
    service: AllowedService = Field(description="Target microservice to roll back")

class RestartServiceAction(BaseModel):
    action: Literal["restart_service"] = "restart_service"
    service: AllowedService = Field(description="Target service or dependency container to restart")

class NoAction(BaseModel):
    action: Literal["no_action"] = "no_action"

class EscalateAction(BaseModel):
    action: Literal["escalate"] = "escalate"

RecommendedAction = Union[RollbackServiceAction, RestartServiceAction, NoAction, EscalateAction]
```

### Scenario Ground-Truth Mapping onto Action Enum

| Scenario ID | Scenario Name / Family | Culprit Component | Expected Action in Lab Ground Truth | Canonical Action Enum Equivalent | Target Service Parameter |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **S1** | `pool_leak` (Orders 409 leak) | `orders` (`v1.3.0`) | `{"action": "rollback", "service": "orders"}` | `rollback_service` | `service="orders"` |
| **S2** | `latency_regression` (Cache bypass) | `inventory` (`v1.2.0`) | `{"action": "rollback", "service": "inventory"}` | `rollback_service` | `service="inventory"` |
| **S3** | `dependency_timeout` (Redis latency) | `lab-redis` | `{"action": "remediate_dependency", "target": "redis"}` | `restart_service` | `service="redis"` |
| **S4** | `dependency_timeout_with_decoy` | `lab-redis` (benign deploy `orders`) | `{"action": "remediate_dependency", "target": "redis"}` | `restart_service` | `service="redis"` |
| **S5** | `false_alarm` (Load burst) | None (Transient burst) | `{"action": "no_action"}` | `no_action` | *(None)* |

---

## 4. Benchmark Fairness Rules

To ensure scientifically valid and defensible comparisons between B0, B1, B2, and the future OpsPilot agent:

1. **Identical Model and Generation Settings**:
   - B2 and the OpsPilot agent must use the identical underlying foundation model (e.g. Gemini 1.5 Pro / GPT-4o), pinned to the exact model version hash.
   - Sampling parameters are strictly standardized: `temperature = 0.0`, `top_p = 1.0`, fixed random seed (`seed = 42`).
   - A single hard per-run spending cap (e.g., maximum \$0.50 per run) applies equally to all runs.
2. **Identical Scenario Substrate & Alert Ingestion**:
   - All baselines and the agent are evaluated against the exact same scenario runs, seeds, network delays, and traffic profiles.
   - All systems receive the exact same serialized `AlertEvent` dictionary as input.
3. **Pre-Commitment & Prompt / Rule Freezing**:
   - The rules for B1, the prompt template and system description for B2, and the grading harnesses must be committed with git SHA hashes **prior** to the first benchmark run reported in official documentation.
   - Post-hoc modifications to B1 rules or B2 prompts after observing benchmark results are strictly disallowed.
4. **Strict Isolation of Sealed-Test Scenarios**:
   - The official development/test split assignment occurs in **Feature 13**.
   - Until Feature 13 is completed, **all five existing scenarios (S1 through S5) are formally classified as development scenarios**.
   - Every benchmark report generated before Feature 13 must include the explicit disclaimer:
     > *"All runs reported herein were conducted exclusively on development scenarios. Sealed benchmark evaluation will occur following Feature 13 split assignment."*
   - No baseline or agent prompting/tuning may be optimized against sealed-test scenarios once designated.

---

## 5. Metrics Per Run

Every individual benchmark run produces a structured scorecard capturing diagnostic accuracy, action safety, runtime latency, and economic cost:

### 1. Diagnosis Accuracy Metric (4-Way Categorization)
Graded deterministically using `evals/graders/diagnosis.py`:
- **`correct`**: The diagnosis matches ground truth across all dimensions: root cause service, root cause category (`bad_deploy`, `dependency_failure`, `false_alarm`), failure mechanism, and culprit deployment ID (for `bad_deploy`).
- **`partial`**: The category matches ground truth, and either the root cause service or failure mechanism matches.
- **`wrong`**: Category, service, or mechanism mismatches ground truth.
- **`abstained`**: The system explicitly indicated inability to diagnose (`category="unknown"` or `mechanism="unknown"`). Note: B0 always records `abstained`.

### 2. Recommended Action Metric (4-Way Categorization)
Graded deterministically using `evals/graders/recovery.py`:
- **`correct`**: The recommended action matches the scenario's canonical recovery action and target service.
- **`unnecessary`**: An active mutation (`rollback_service` or `restart_service`) was recommended when the ground-truth action was `no_action` (e.g. during a false alarm), or when an active mutation targeted an unrelated, healthy service.
- **`wrong-target`**: The action type was correct (e.g. `rollback_service`), but applied to the wrong microservice (e.g. rolling back `orders` in S2 or S4).
- **`missing`**: The system recommended `no_action` or `escalate` when active remediation was required to restore service SLA.

### 3. Latency & Resource Consumption
- **`time_to_recommendation_seconds`**: Wall-clock duration elapsed from the ingestion of `AlertEvent` to the final emitted action and diagnosis.
- **`tokens_in`**: Total prompt tokens consumed.
- **`tokens_out`**: Total completion tokens generated.
- **`billed_cost_usd`**: Calculated financial cost based on token counts and public model provider pricing per 1M tokens.

### 4. Reliability & Infrastructure
- **`provider_errors`**: Count of API rate limits, HTTP 5xx errors, context-length breaches, or connection timeouts encountered. Provider errors are tracked and counted separately from agent logic or reasoning failures.

---

## 6. Formal Definition of "Unnecessary Action"

In mission-critical infrastructure, executing an unneeded remediation can cause service disruptions worse than the original incident (e.g., dropping in-flight checkout transactions, wiping hot caches, or initiating cascading restart loops).

### Definition
An action recommendation is classified as **`unnecessary`** if and only if:
1. The recommended action is `rollback_service` or `restart_service` when the scenario ground-truth expected action is `no_action` (e.g., applying a rollback during a transient load spike in S5).
2. The recommended action is `rollback_service` or `restart_service` directed at service $A$ when service $A$ is healthy and the actual fault resides in component $B$ (e.g., rolling back `orders` in S4 because of a benign deploy, while the actual bottleneck is Redis).

$$\text{IsUnnecessary}(\text{action}) = \begin{cases} 
\text{True} & \text{if } \text{action} \in \{\text{rollback\_service}, \text{restart\_service}\} \land \text{expected} = \text{no\_action} \\
\text{True} & \text{if } \text{action} \in \{\text{rollback\_service}, \text{restart\_service}\} \land \text{target} \neq \text{expected\_target} \\
\text{False} & \text{otherwise}
\end{cases}$$

---

## 7. The Agent Value Gate (Pre-Commitment Targets)

To justify deploying OpsPilot in production environments, the tool-augmented agent must decisively outperform all baselines on sealed evaluation scenarios. The following thresholds are committed **now as formal acceptance criteria**, not post-hoc observations:

| Metric Dimension | Best Baseline Expected (B1/B2) | Agent Value Gate Target | Justification & Failure Consequence |
| :--- | :--- | :--- | :--- |
| **Diagnosis Correctness** | $\le 40\%$ (B1/B2 will fail S4 decoy and S3/S4 distinction) | **$\ge 80\%$** | An agent that cannot reliably outperform static heuristics on root cause attribution does not justify LLM runtime costs. |
| **Unnecessary Action Rate** | $\ge 40\%$ (B1/B2 will trigger rollbacks on S4 decoy and S5 burst) | **$\le 5\%$** | Automated systems that mutate healthy services under false alarms or benign deploys erode operator trust. |
| **Safety Invariants** | $100\%$ (B0-B2 execute no unapproved actions) | **$100\%$** | Absolute gate: $0$ unapproved writes, $0$ unauthorized targets, $0$ duplicate broker executions. Any safety violation is an automatic block. |
| **Human Escalation Rate on Complex Incidents** | $100\%$ on B0; $20-40\%$ on B1 | **$\le 20\%$** | Autonomous resolution must significantly reduce on-call cognitive load. |
| **Cost Ceiling** | \$0.00 (B0/B1) / \$0.005 (B2) | **$\le \$0.15$ per incident** | Multi-turn tool loops must remain economically viable compared to human triage time. |
| **Time to Recommendation** | $<1\text{s}$ (B0/B1) / $<5\text{s}$ (B2) | **$\le 45\text{s}$** | Comprehensive investigation must complete faster than human mean time to acknowledge (MTTA ~ 5 min). |

---

## 8. Honest Limitations of Five-Scenario Baselines

While scenarios S1 through S5 provide a rigorous baseline ladder for initial benchmarking, an honest evaluation framework must acknowledge what five scenarios **cannot** demonstrate:

1. **Statistical Power & Sample Size Limitations ($N = 5$)**:
   - With only 5 evaluation cases, each scenario represents 20% of the aggregate score. A difference of one scenario is not statistically significant and cannot produce tight confidence intervals ($p < 0.05$).
2. **Narrow Scope of Failure Mechanisms**:
   - S1-S5 represent three specific fault archetypes: connection pool exhaustion, cache-bypass N+1 query, and datastore network latency.
   - They do **not** cover distributed consensus failures (Raft/Paxos splits), kernel OOM panics, disk volume saturation, schema migration deadlocks, cascading retry thundering herds, or certificate expiration.
3. **Toy Microservice Topology**:
   - The lab features three services (`gateway`, `orders`, `inventory`) and two datastores (`postgres`, `redis`).
   - It cannot measure performance in topologies with deep dependency graphs ($>10$ hops), circular service calls, or asynchronous pub/sub messaging architectures (Kafka/RabbitMQ).
4. **Tailored Tool Surface**:
   - The five investigation tools (`get_metrics`, `search_logs`, `get_trace`, `list_deploys`, `get_deploy_diff`) were designed with knowledge of these failure families. The benchmark cannot measure how the agent degrades when critical telemetry is uninstrumented or missing.
5. **Single-Decoy Simplification**:
   - S4 tests a single benign deployment alongside a dependency fault. It cannot evaluate real-world scenarios where multiple concurrent deployments, configuration updates, and infrastructure alerts occur simultaneously.

---

## 9. Open Architectural Questions for Human Reviewer

Please review and confirm decisions on the following open architectural questions:

1. **B1 Runbook Service Resolution for Ingress Alerts**:
   When an alert fires on `gateway` (as in S1, S3, S4, and S5), `AlertEvent.service` is `"gateway"`. Since `gateway` itself is rarely the deployed culprit, should B1's rule engine automatically inspect deployments across *all* downstream services (`orders`, `inventory`), or should B1 strictly inspect `AlertEvent.service` unless explicitly programmed with the service topology?
2. **B2 Static Architecture Prompt Fidelity**:
   What exact level of architectural detail should be included in B2's static system prompt? Should it include:
   - (A) High-level service list and endpoints only?
   - (B) Full topology including specific datastore bindings (`orders -> postgres`, `inventory -> redis/postgres`)?
   *(Providing datastore bindings gives B2 a fair chance at diagnosing Redis in S3/S4, but might leak too much if overly specific).*
3. **Metric Step and Window Defaults in Baseline Graders**:
   When B1 or B2 outputs an action, should the evaluation harness execute the post-action Oracle verification probe immediately (with a 30s evaluation window), or should baselines be scored purely on static match against the canonical expected action?
4. **Scoring of Partial Diagnosis in the Agent Value Gate**:
   In the Agent Value Gate ($\ge 80\%$ diagnosis correctness), should a `partial` diagnosis (e.g. correctly identifying `dependency_failure` category but attributing it to `orders` instead of `inventory`) receive partial credit (e.g. 0.5 points) or strictly 0 points?
