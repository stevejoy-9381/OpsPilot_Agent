# OpsPilot Feature 8 Report: Human Approval Transaction, Durable Interrupt/Resume & Killable Workers

> **Status**: Complete (Offline & In-Process Verified; Live-Postgres Subprocess Suite Ready for Codespaces CI)  
> **Design Spec**: [docs/approval_design.md](approval_design.md)  
> **Demo Transcript**: [docs/demo_feature8_transcript.md](demo_feature8_transcript.md)

---

## 1. Approval Transaction as Built (`ApprovalService.decide`)

`ApprovalService.decide()` in [apps/api/services/approval_service.py](../apps/api/services/approval_service.py) is the **sole code path** permitted to write `app.approvals` or `app.action_executions`.

### Ordered Steps & Error Codes
1. **Service-Layer Principal & Role Guard**: Rejects service principals (`worker`/`broker`) and non-`reviewer`/`admin` users with `ForbiddenError` (`403 forbidden`).
2. **Pre-Transaction Target State Read (`approve` only)**: Queries `TargetStateReader.current_state(target_id, db_now)` **before** opening the locking transaction. Fails closed with `TargetStateUnavailableError` (`503 target_state_unavailable`) if the reader is missing, returns `TargetStateError`, raises, or `read_at` exceeds `TARGET_STATE_MAX_AGE_SECONDS` (default `30`s).
3. **Single Atomic Transaction (`SET LOCAL app.tenant_id`, `FOR UPDATE`)**:
   - Lock `app.action_plans` `FOR UPDATE` (tenant-scoped; `404 not_found` if missing or cross-tenant).
   - Check `app.approval_idempotency` for `(tenant_id, plan_id, reviewer_id, idempotency_key)`: exact body match (`decision`, `submitted_plan_hash`, `reason`) returns cached `DecisionResult` (`200`, `idempotent_replay=True`); different body raises `IdempotencyConflictError` (`409 idempotency_conflict`).
   - Constant-time `hmac.compare_digest` against stored `plan_hash` **and** recomputation of `shared.plans.plan_hash` from stored `action_plans` fields to detect DB tampering (`409 plan_hash_mismatch`).
   - Status checks: `expired` (`410 plan_expired`), `superseded` (`409 plan_superseded`), `approved`/`rejected`/`executed`/`failed` (`409 decision_conflict` or `409 already_decided`), non-`proposed` (`409 plan_not_proposed`).
   - Lock `app.runs` `FOR UPDATE`: verify `run.status == 'needs_review'` (`409 run_not_in_review`), `run.version == run_version` (`409 concurrent_modification`), and optional Separation of Duties (`403 separation_of_duties_violation`).
   - Database clock expiry check (`now()`/`clock_timestamp()` > `expires_at`): transitions `plan -> expired`, `run -> escalated` (`approval_expired`), commits, and raises `PlanExpiredError` (`410 plan_expired`).
   - Stale-target check (`approve` only, `live_version != plan.expected_version`): transitions `plan -> superseded`, `run -> escalated` (`target_changed`), commits, and raises `StaleTargetError` (`409 stale_target`).
   - Insert `app.approvals`, update `app.action_plans.status`, (`approve` only) insert `app.action_executions(status='queued', action_key=...)`, transition `app.runs` via `RunService`, append `approval_recorded` in `app.run_events`, persist `app.approval_idempotency`, `COMMIT`, and emit `NOTIFY opspilot_plan_decided`. Any write failure rolls back the entire transaction leaving 0 partial rows.

---

## 2. Deterministic `action_key` & Uniqueness Constraints

- **`action_key` Encoding** (`shared.plans.compute_action_key`):
  $$\text{action\_key} = \text{SHA-256}\big(\text{UTF-8}(\texttt{f"\{plan\_id\}:\{plan\_hash\}"})\big).\text{hexdigest}()$$
  where `plan_id` is lowercased/stripped canonical UUID and `plan_hash` is 64-char lowercase hex.
- **Uniqueness & Idempotency Constraints**:
  - `app.approval_idempotency`: `UNIQUE (tenant_id, plan_id, reviewer_id, idempotency_key)`
  - `app.approvals`: `UNIQUE (plan_id, reviewer_id)`
  - `app.action_executions`: `UNIQUE (plan_id)` and `UNIQUE (action_key)`

---

## 3. Interrupt & Resume Design

- **`await_review` Node** ([agent/graph/nodes/await_review.py](../agent/graph/nodes/await_review.py)): Performs only documented, side-effect-free reads before `interrupt({"plan_id", "plan_hash", "expires_at"})` and never writes approvals.
- **Post-Resume Routing**:
  - `approved` $\to$ `await_execution` (stub with `ExecutionPort` for Feature 9/10; run stays `approved`).
  - `rejected` / `expired` / `cancelled` $\to$ `END` (run already in `escalated`).
- **Worker Resume Driver** ([agent/worker.py](../agent/worker.py)): Polls DB for decided plans, checks `graph.get_state(config).next` first (making double resume a no-op), resumes via `Command(resume=decision_from_db)`, and appends `graph_resumed`.
- **Checkpointer**: `PostgresSaver` (`langgraph-checkpoint-postgres==2.0.16`, `langgraph==0.3.5`) on the `checkpoints` schema (`thread_id = run_id`, `opspilot_checkpointer` role).

---

## 4. Worker Lease, Fencing & Graceful Shutdown

- **Columns & Index**: `app.runs` carries `lease_owner`, `lease_token` (UUID rotated on every claim), `lease_expires_at`, and `heartbeat_at`, indexed by `idx_runs_lease_reclaim`. `fn_runs_enforce_transition()` exempts lease-only updates from bumping `runs.version`.
- **Heartbeat Ratio**: `HEARTBEAT_SECONDS = 5.0` vs `LEASE_SECONDS = 30.0` (**6:1 ratio**).
- **Mandatory Fencing**: `FencedRunService(run_service, lease_token)` requires `lease_token` on every worker write and enforces `WHERE lease_token = :token`, raising `LeaseLostError` and emitting `worker_lease_lost` on zombie writes.
- **Reclaim & `SIGTERM`**: `investigating` reclaims resume from last checkpoint; `needs_review` is never claimed until decided; **no state ever reclaims into `executing`** (owned exclusively by the Feature 9 Broker). `SIGTERM` stops claiming new runs, finishes/checkpoints the current step, and releases the lease.

---

## 5. Crash Matrix & Measured Results

| Crash / Race Scenario | What Was Tested | Observed Result |
| :--- | :--- | :--- |
| **Partial-failure rollback (5 write steps)** | In-process SQLite fault injection (`test_partial_failure_rollback_leaves_zero_partial_rows`) | **PASSED**: 0 rows left in `approvals`, `action_executions`, `approval_idempotency`, `run_events` across all 5 steps. |
| **Expiry race (200 iterations)** | Concurrent `ApprovalService.decide()` vs `WorkerService.sweep_expired_plans()` | **PASSED**: `iterations=200 approved=0 expired=200`, 0 double outcomes. |
| **Worker restart at `await_review`** | New compiled graph object + shared checkpointer (`test_await_review_restart_double_resume_and_post_resume_routing`) | **PASSED**: Resumes cleanly; double resume is a no-op. |
| **Zombie worker fencing** | Expired `worker-1` fenced out after `worker-2` reclaim (`test_worker_lease_reclaim_and_zombie_fencing`) | **PASSED**: `LeaseLostError` raised; `worker_claimed -> run_reclaimed -> worker_lease_lost`. |
| **Subprocess `SIGKILL` durability (`tests/durability/`)** | Real Postgres + `uvicorn` + `agent.worker` subprocesses | **Not run locally** (`SKIPPED`: no Docker / live OpsPilot Postgres in local Windows sandbox; scheduled for Codespaces & `lab_nightly.yml`). |

---

## 6. Explicit Non-Guarantee (Exactly-Once Target Effects)

> [!IMPORTANT]
> **Effects on the target service are NOT exactly-once yet.** Feature 8 guarantees at-most-once human approval and durable creation of a single `action_executions(status='queued')` intent row. Guaranteeing safe container dispatch, pre-flight target checks, and `uncertain` reconciliation is the **Execution Broker's job in Feature 9**.

---

## 7. Known Limits & Deviations from `docs/approval_design.md`

1. **Lease Columns on `app.runs` vs `app.run_leases`**: Migration `a1b2c3d4e5f6` initially created `app.run_leases`, and migration `b2c3d4e5f6a7` added `lease_owner`, `lease_token`, `lease_expires_at` directly on `app.runs` with `fn_runs_enforce_transition()` updated to preserve `runs.version` on lease-only updates (keeping claim/reclaim in a single row lock).
2. **`langgraph==0.3.5` Interrupt Inspection**: `graph.invoke()` returns `AddableValuesDict` without `'__interrupt__'`; pending interrupts are inspected via `graph.get_state(config).tasks[0].interrupts` (recorded in ADR-006 of [DECISIONS.md](DECISIONS.md)).
3. **Offline Sandbox Scope**: Live-Postgres tests (`test_approval_service_pg.py`, `tests/durability/`, live `db_invariants.py`) require a running PostgreSQL instance and are configured to run in GitHub Actions CI / Codespaces.
