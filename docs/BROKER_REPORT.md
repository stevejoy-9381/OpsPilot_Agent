# OpsPilot Execution Broker Report (Feature 9)

> **Status**: Feature 9 Implementation Complete (Offline & Isolation Verified; Live Crash Matrix Stopped & Pending Codespace Re-run)  
> **Specification Reference**: [broker_design.md](file:///e:/opspilot/docs/broker_design.md)  
> **Demo Transcript**: [demo_feature9_transcript.md](file:///e:/opspilot/docs/demo_feature9_transcript.md)

---

## 1. Execution State Machine (As Built)

Implemented in `app.action_executions` ([tables.py](file:///e:/opspilot/apps/api/models/tables.py)), enforced by DB transition table `app.execution_transitions` and `ExecutionRepository` ([execution_repository.py](file:///e:/opspilot/apps/api/repositories/execution_repository.py)):

```text
queued ──► claimed ──┬──(B1–B10 fail)──────────────────────────► refused
                     │
                     └──(B1–B10 pass)──► dispatched ──┬──(applied / already_applied)──► acknowledged ──► (run: verifying)
                                                      │
                                                      ├──(409 precondition / 4xx)──────► failed
                                                      │
                                                      └──(timeout / drop / 5xx / crash)► uncertain
                                                                                            │
                                              ┌────(reconcile: not_applied, retries left)───┤
                                              │                                             │
                                              ▼                                             ├──(reconcile: applied)──► reconciled ──► (run: verifying)
                                          dispatched                                        │
                                      (same action_key)                                     └──(reconcile: still_unknown / max_attempts)──► failed
```

- **Claiming & Fencing**: `ExecutionRepository.claim_next()` uses `SELECT ... FOR UPDATE SKIP LOCKED` over `status = 'queued'` or lease-expired `('claimed', 'dispatched', 'uncertain')`. Every state transition requires matching `lease_token` (`LeaseLostError` on mismatch).
- **Pending Verification Recovery**: `BrokerService.reconcile_pending_verifications()` completes `executing -> verifying` if the broker crashes after `acknowledged`/`reconciled` is committed and before `VerificationPort.handoff()` commits.
- **Database as Sole Channel**: `DatabaseExecutionPort` ([await_review.py](file:///e:/opspilot/agent/graph/nodes/await_review.py)) reads `app.action_executions` only; neither `api` nor `worker` calls the broker directly.

---

## 2. Pre-Dispatch Checks (B1–B10) & Refusal Codes

Implemented in [checks.py](file:///e:/opspilot/broker/checks.py) (`run_prechecks`), emitting a `prechecks_completed` `execution_events` row with all evaluated `PrecheckRecord`s and their sanitized inputs:

| Rule | Check | Refusal Code |
| :--- | :--- | :--- |
| **B1** | Execution row and parent plan exist for the same `tenant_id` | `execution_or_plan_missing` |
| **B2** | `action_plans.status == 'approved'` | `plan_not_approved` |
| **B3** | Valid human `approve` row exists and approver role is `reviewer` or `admin` (never `worker`) | `approval_invalid_or_missing` |
| **B4** | Recomputed canonical `plan_hash` matches stored `plan.plan_hash` and `approval.submitted_plan_hash` | `plan_hash_integrity_failed` |
| **B5** | `plan.expires_at > clock_timestamp()` | `execution_expired` |
| **B6** | `execution.action_key == compute_action_key(plan.id, plan.plan_hash)` | `action_key_mismatch` |
| **B7** | `target_id` and `service` are in `config/lab_targets.yaml` and `action` is in `allowed_actions` | `target_or_action_not_allowed` |
| **B8** | Fresh `TargetStateReader.current_state()` matches `plan.expected_version` | `stale_target` / `policy_unavailable` |
| **B9** | No other `claimed`/`dispatched`/`uncertain` execution exists for the same `target_id` | `target_locked` |
| **B10** | Owning run is in `approved` status | `run_not_approved_or_stale` |
| **Fail-closed** | Any exception, missing registry, or unavailable `TargetStateReader` | `policy_unavailable` |

---

## 3. Target-Side Protocol

Implemented in [admin.py](file:///e:/opspilot/lab/common/admin.py) and [target_client.py](file:///e:/opspilot/broker/target_client.py):
- **Auth**: `X-Lab-Admin-Token` over internal `lab-admin` network; `follow_redirects=False`; `MAX_TARGET_RESPONSE_BYTES = 1_048_576`.
- **CAS + Idempotency**: `POST /admin/actions/rollback` and `POST /admin/actions/restart` require `action_key` and `expected_current_version`.
  - First apply inserts into `lab.applied_actions` (and `lab.deploys` on rollback) atomically (`applied`).
  - Replay with the same `action_key` returns `already_applied` with zero duplicate mutations.
  - Version mismatch returns HTTP `409` `precondition_failed` (no retry loop).
  - Oversized bodies (`> 1 MiB`), malformed JSON, redirects (`3xx`), transport drops, or `action_key` mismatches return `TargetActionOutcome.UNCERTAIN`.
- **Reconciliation**: `GET /admin/actions/{action_key}` queries `lab.applied_actions` before any retry (`reconcile()` in [reconcile.py](file:///e:/opspilot/broker/reconcile.py)).

---

## 4. Network Reachability Matrix

Measured in Codespaces via `make test-isolation` ([test_broker_isolation.py](file:///e:/opspilot/tests/test_broker_isolation.py), `2 passed` in `17.44s`):

| Source Network | Target | Result |
| :--- | :--- | :--- |
| `core` | `broker:8090/healthz` | `BLOCKED` |
| `core` | `gateway/admin/state` | `BLOCKED` |
| `core` | `orders/admin/state` | `BLOCKED` |
| `core` | `inventory/admin/state` | `BLOCKED` |
| `core` | `controller/healthz` | `BLOCKED` |
| `broker` | `gateway/admin/state` | `ALLOWED` |
| `broker` | `orders/admin/state` | `ALLOWED` |
| `broker` | `inventory/admin/state` | `ALLOWED` |
| `broker` | `controller/runs/1/truth` | `BLOCKED` |
| `broker` | `/vault` | `BLOCKED` |
| `broker` | `otel-collector:4317` | `BLOCKED` |
| `broker` | `outbound-internet` | `BLOCKED` |

---

## 5. Crash Matrix & 20-Repeat Suite

- **Live Crash Matrix (`a`–`f`, `SIGKILL`)**: **not run to completion** — stopped at crash point `a` because `_spawn_broker` in [test_broker_crash_and_20_repeat.py](file:///e:/opspilot/tests/durability/test_broker_crash_and_20_repeat.py) passed `BROKER_LEASE_SECONDS="2"` (`BrokerSettings` requires `>= 5`, exiting `rc=1`), and the Codespace stack was torn down (`docker compose down -v`) per Hard Limit #4/#6.
- **20-Repeat Mixed-Fault Suite (`BROKER_SUITE_SEED=20261126`)**: **not run** (same teardown stop before Part 2).
- **Recovery Time (Median / Max)**: **not run** (pending Codespace re-run with `BROKER_LEASE_SECONDS="5"`).

| Crash Point | Description | Status | Median Recover (s) | Max Recover (s) |
| :--- | :--- | :--- | :--- | :--- |
| `a` | Before `claim_next()` | `not run` (stopped on `BROKER_LEASE_SECONDS=2` validation error) | N/A | N/A |
| `b` | After `claim_next()`, before dispatch | `not run` | N/A | N/A |
| `c` | After `dispatched` committed, before HTTP call | `not run` | N/A | N/A |
| `d` | After target applied (`drop_response_after_apply`), before response | `not run` | N/A | N/A |
| `e` | After `acknowledged`, before run status commit | `not run` | N/A | N/A |
| `f` | During `reconcile_execution()` | `not run` | N/A | N/A |

---

## 6. Safety Grader Numbers

| Metric | Result | Source |
| :--- | :--- | :--- |
| Offline `grade_safety` & `grade_safety_records` unit suite | **3/3 passed** | `tests/durability/test_broker_crash_and_20_repeat.py` + `tests/test_evals_graders.py` (`3 passed, 19 deselected`) |
| Offline E2E (`FakeTarget` S1/S3/S5 + B1–B10 audit trail) | **1/1 passed** (`0` unapproved, `0` wrong-target, `0` duplicates) | `tests/test_end_to_end_execution.py::test_offline_e2e_s1_s3_s5_and_audit_trail` |
| Live 20-repeat `grade_safety_from_db` | **not run** | Pending Codespace re-run |

---

## 7. Known Limits & Deviations from `broker_design.md`

### Known Limits
1. **Lab only**: Operates strictly against the 3 toy lab targets in `config/lab_targets.yaml`.
2. **One action per plan**: Each `action_plans` row maps to at most one `action_executions` row (`action_key`).
3. **Simulated deploys**: Rollbacks and restarts update in-process service state and `lab.deploys` / `lab.applied_actions` rows; no real container restarts or Kubernetes/cloud mutations occur.
4. **No real infrastructure**: Tested against local/Codespace Docker Compose only.

### Deviations from `docs/broker_design.md`
1. `infra/Dockerfile.broker` installs pinned runtime dependencies via `pip` into `/app/.venv` instead of `uv sync` to avoid `uv` HTTP timeouts inside Codespaces Docker builds.
2. `LabAdminClient` adds `MAX_TARGET_RESPONSE_BYTES = 1_048_576` and `action_key` parity validation on target responses.
3. `BrokerService` adds `reconcile_pending_verifications()` to recover `executing -> verifying` transitions if a crash occurs after `acknowledged` is committed.
4. `run_prechecks()` emits a `prechecks_completed` `execution_events` record containing structured B1–B10 `PrecheckRecord`s and sanitized inputs.
5. `opspilot_broker` DB role requires `UPDATE (status, updated_at)` on `app.action_plans` when `_handle_refusal` / `_map_target_response` updates plan status (currently `broker_design.md` listed `SELECT` only).

---

## 8. What Is NOT Yet Proven

- **Recovery verification (Feature 10)**: The broker transitions runs to `verifying` after `acknowledged`/`reconciled`, but it does **not** yet prove that the remediation actually restored service health/SLOs (post-action probes and synthetic load window).
- **Reliability at scale (Feature 13)**: End-to-end reliability, latency, and throughput under sustained multi-incident concurrency and full evaluation splits remain unproven until Feature 13.
