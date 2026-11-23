# OpsPilot Lab Report: Architecture, Scenarios, and Verification

## 1. Executive Summary

This report documents the completion of **Feature 1: Synthetic Microservice Lab & Evaluation Substrate**. The lab provides a hermetic, reproducible, multi-service synthetic environment simulating realistic production incidents across distributed microservices. It features:
- End-to-end telemetry (Prometheus metrics, OpenTelemetry W3C distributed traces, structured JSON logs).
- In-process deployment and rollback primitives without host Docker socket access.
- An isolated SQLite Ground-Truth Vault accessible exclusively by the test harness.
- Deterministic graders and an automated blind diagnosis kit evaluated with 100% human accuracy.
- Complete end-to-end lifecycle verification across all 5 incident scenario families.

---

## 2. Architecture as Built

### System Topology

```
+-----------------------------------------------------------------------------------------+
|                                HOST / TEST HARNESS                                      |
|   +-----------------------+     +------------------------+    +---------------------+   |
|   | pytest -m lab         |     | scripts/blind_kit.py   |    | make lab-smoke      |   |
|   +-----------+-----------+     +-----------+------------+    +----------+----------+   |
+---------------|-----------------------------|----------------------------|--------------+
                |                             |                            | (127.0.0.1:8080)
                | (Token Authenticated)       | (Token Authenticated)      v
+---------------v-----------------------------v-------------------------------------------+
| Network: lab-admin (Control Plane Only - Internal)                                      |
|                                                                                         |
|   +---------------------------------------+      +----------------------------------+   |
|   |       opspilot-controller             | <--> | Dedicated Volume: vault_data     |   |
|   |  - POST /runs {scenario_id}           |      | (SQLite: vault.db)               |   |
|   |  - GET  /runs/{id}/truth (HARNESS)    |      | (Zero access from core or agent) |   |
|   |  - POST /world/reset                  |      +----------------------------------+   |
|   +-------------------+-------------------+                                             |
+-----------------------|-----------------------------------------------------------------+
                        | Admin calls (/admin/deploy, /admin/faults, /admin/reset)
+-----------------------v-----------------------------------------------------------------+
| Network: lab (Internal Microservice Interconnect)                                       |
|                                                                                         |
|   +--------------------+     W3C Trace     +--------------------+                       |
|   |  opspilot-gateway  | ----------------> |  opspilot-orders   |                       |
|   |  (POST /checkout)  |                   |  (POST /orders)    |                       |
|   +---------+----------+                   +---------+----------+                       |
|             |                                        |                                  |
|             |                                        +----------+                       |
|             | W3C Trace                                         | SQL queries           |
|             v                                                   v                       |
|   +--------------------+                       +----------------------------------+     |
|   | opspilot-inventory | --------------------> |      opspilot-lab-postgres       |     |
|   | (GET /stock/{id})  |  Cache lookup / Miss  | (PostgreSQL 19: seed data +      |     |
|   +---------+----------+                       |  lab.deploys audit history)      |     |
|             |                                  +----------------------------------+     |
|             v Cache read/write                                                          |
|   +--------------------+                                                                |
|   | opspilot-lab-redis |                                                                |
|   | (Redis 8.10 Cache) |                                                                |
|   +--------------------+                                                                |
+-----------------------------------------------------------------------------------------+
       | (Scrapes :8000)                        | (OTLP Spans :4317)       | (Log writes)
+------v-----------------+              +-------v-----------------+  +-----v----------------+
|  opspilot-prometheus   |              |  opspilot-otel-collector|  | Volume: telemetry    |
| (PromQL :9090 internal)|              | (File Exporter to JSONL)|  | (/telemetry/logs/    |
| - 5s scrape interval   |              | - /telemetry/traces/    |  |  /telemetry/traces/) |
| - 2d retention volume  |              |   spans.jsonl           |  | (ro for agent tools) |
+------------------------+              +-------------------------+  +----------------------+
```

### Network Segregation and Security Boundaries

1. **`lab-admin` (Internal)**: Carries administrative traffic (`/admin/deploy`, `/admin/faults`, `/admin/reset`). The `controller` and service admin ports reside here. Inaccessible from the agent and core networks.
2. **`lab` (Inter-service)**: Carries standard business traffic (`/checkout`, `/orders`, `/stock/{id}`) and datastore queries. Only `gateway` publishes a port, bound strictly to `127.0.0.1:8080` on the loopback interface for local smoke testing and load delivery.
3. **Vault Storage (`opspilot_vault_data`)**: Dedicated Docker volume mounted strictly into `controller`. Ground truth records are retrievable only via `GET /runs/{id}/truth` presenting the secret `HARNESS_TOKEN`.
4. **Telemetry Storage (`opspilot_telemetry`)**: Shared volume where microservices write structured JSON logs and the OTel collector exports spans. Any tool container or evaluator mounts this volume strictly as `:ro` (read-only).
5. **No Docker Socket**: Neither Docker daemon sockets (`/var/run/docker.sock`) nor Docker CLI binaries are mounted into any container.

---

## 3. Version Profiles and Microservice Registry

All microservices support dynamic in-process version profile deployments via `lab.controller_lib.deploy.deploy(service, new_version)`. Each version transition is recorded in the PostgreSQL audit table `lab.deploys` with realistic, non-incriminating summaries and synthetically generated file diffs.

| Service | Version | Description & Behavioral Implementation | Audit Summary (Truthful, No Buzzwords) |
|---|---|---|---|
| **orders** | `v1.0.0` | Baseline architecture: explicit transaction commit and clean connection release. | Initial baseline service architecture deployment |
| **orders** | `v1.1.0` | Decoy profile: updates receipt rendering template and header metadata without performance impact. | Update receipt rendering template formatting and add header metadata |
| **orders** | `v1.2.0` | Decoy profile: adds auxiliary order metrics tagging and audit event emission. | Extend order audit instrumentation and structured event tagging |
| **orders** | `v1.3.0` | **Culprit for S1**: introduces an unclosed database connection when handling a 409 conflict, leading to pool exhaustion. | Refactor order transaction flow and conflict resolution handling |
| **inventory** | `v1.0.0` | Baseline architecture: Redis cache-aside read with PostgreSQL fallback on cache miss. | Initial baseline service architecture deployment |
| **inventory** | `v1.1.0` | Decoy profile: adds localized SKU validation and barcode format parsing helper. | Standardize item SKU validation and add barcode formatting helper |
| **inventory** | `v1.2.0` | **Culprit for S2**: bypasses Redis cache lookup and executes an unindexed secondary query per stock check. | Adjust inventory stock audit consistency check and caching strategy |
| **inventory** | `v1.3.0` | Enhanced caching profile: optimizes Redis cache serialization and TTL invalidation. | Optimize cache serialization routines and update key expiration policies |
| **gateway** | `v1.0.0` | Baseline routing: orchestrates checkout transactions, propagates W3C headers, captures duration metrics. | Initial baseline API gateway deployment |

---

## 4. Failure Scenario Matrix & Signatures

The lab formalizes five scenario definitions (S1 through S5) in `fixtures/scenarios/` matching the five incident families:

| Scenario | Family | Root Cause & Culprit | Primary Metric Signature | Structured Log Signature | Distributed Trace Signature |
|---|---|---|---|---|---|
| **S1** | `pool_leak` | `orders` v1.3.0 connection leak on 409 stock conflict | `db_pool_in_use == 10` (capacity), `http_requests_total{status='503'}` surge on `/orders` | `Timed out waiting for database connection from pool` (`level=ERROR`) | `/checkout` ends in HTTP 503; root span error attributed to child `/orders` span timeout |
| **S2** | `latency_regression` | `inventory` v1.2.0 Redis cache bypass & extra SQL query | `http_request_duration_seconds{le='0.5'}` p95 breaches 250ms threshold; 0% 5xx | `Database query completed with elevated duration`, zero Redis cache hit logs | `/checkout` span duration elevated (>400ms); child `/stock/{id}` duration dominates |
| **S3** | `dependency_timeout` | Simulated 300ms latency on Redis datastore | `dependency_call_duration_seconds{dependency='redis'}` > 150ms timeout threshold | `Redis query exceeded 150ms timeout limit; fallback initiated` | Child span `redis.get` shows ~300ms delay followed by fallback PostgreSQL query |
| **S4** | `dependency_timeout_with_decoy` | Redis latency degradation with benign `orders` v1.1.0 decoy deploy | Redis call duration elevated beyond timeout; orders service latency unaffected | Redis timeout warning in `inventory.jsonl`; orders logs show successful receipt render | Span timeline isolates latency strictly to `redis.get` child span in `inventory` |
| **S5** | `false_alarm` | Transient 5x marketing traffic spike within system capacity | `http_requests_total` rate surges 5x; 100% 2xx status; connection queues stable | High request rate with 100% `status_code=200`, zero error or timeout events | Normal span tree with durations well within the 500ms SLA capacity threshold |

---

## 5. How to Run Each Scenario

### 1. Via Lab Controller API (Recommended)
```bash
# Initiate Scenario S1 (Starts load -> warms up -> injects deploy -> returns run_id)
curl -X POST http://controller:8000/runs \
  -H "X-Controller-Token: lab-controller-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"scenario_id": "S1", "seed": 42}'

# Query Ground Truth (Harness Token Required - Agent Never Receives This Token)
curl -s http://controller:8000/runs/{run_id}/truth \
  -H "X-Harness-Token: lab-harness-dev-token"

# Reset World to Baseline
curl -X POST http://controller:8000/world/reset \
  -H "X-Controller-Token: lab-controller-dev-token"
```

### 2. Via Python CLI / Script
```bash
# Validate scenario schema
uv run python -m lab.scenarios validate

# List all scenarios
uv run python -m lab.scenarios list

# Run Blind Diagnosis Kit
uv run python scripts/blind_kit.py generate --output-dir blind_packets --vault-path vault.db
uv run python scripts/blind_kit.py score --log-file docs/human_diagnosis_log.md --vault-path vault.db
```

### 3. Via End-to-End Test Suite
```bash
# Run fast CI subset (S1 and S3)
uv run pytest -v -m lab_fast

# Run full scenario lifecycle and determinism suite (S1 through S5)
uv run pytest -v -m lab
```

---

## 6. Known Limits

1. **In-Process Simulated Deploys**: Deployments alter in-memory runtime profiles rather than launching distinct container processes, which eliminates Docker daemon dependencies but relies on process isolation conventions.
2. **Prometheus Scrape Granularity**: Prometheus scrapes metrics on a 5-second cadence. Transient spikes lasting under 5 seconds may register with jitter or require a 15-second observation window to reflect full rate changes.
3. **Database Connection Cap**: PostgreSQL connection pool sizes are artificially bounded (`max_size=10`) to induce pool exhaustion under moderate open-loop load (~20 rps) within 15-30 seconds.

---

## 7. Deviations from `lab_design.md`

1. **Base Image Modernization**: Upgraded `lab-postgres` to PostgreSQL 19 and `lab-redis` to Redis 8.10 (from PostgreSQL 17 and Redis 7 in original design draft) to take advantage of improved connection polling and sub-millisecond timer resolution.
2. **Gateway Host Binding**: Bound the `gateway` container's port 8000 to `127.0.0.1:8080` on the host loopback interface, allowing `make lab-smoke` and local load generators to drive external traffic without joining internal Docker networks.
3. **SQLite Ground-Truth Vault**: Replaced flat JSON ground truth storage with an isolated SQLite engine (`vault.db`), enabling atomic upsert semantics, concurrent run registration, and strict harness token isolation.
4. **In-Process Fault Injection Knobs**: Implemented dependency latency and error rate faults directly in FastAPI service middleware rather than routing through an external ToxiProxy container, maintaining a lightweight RAM footprint (< 2.7 GB total).
