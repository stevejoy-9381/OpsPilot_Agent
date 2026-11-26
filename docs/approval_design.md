# OpsPilot Human Approval, Interrupt/Resume & Crash-Recovery Design Specification

> **Specification Status**: Approved Architecture Specification (Feature 8)  
> **Target Document**: `docs/approval_design.md`  
> **Relevant Contracts**: [outcome_contract.md](file:///e:/opspilot/docs/outcome_contract.md), [data_model.md](file:///e:/opspilot/docs/data_model.md), [proposal_design.md](file:///e:/opspilot/docs/proposal_design.md), [hypothesis_design.md](file:///e:/opspilot/docs/hypothesis_design.md), [DECISIONS.md](file:///e:/opspilot/docs/DECISIONS.md), [plans.py](file:///e:/opspilot/shared/plans.py), [run_service.py](file:///e:/opspilot/apps/api/services/run_service.py)  
> **Implementation Stance**: Approved with recommended defaults (Option A reject -> `escalated`; `409 stale_target` -> `superseded`/`escalated`; Option C configurable SoD defaulting to `false`; `410 Gone` on expiry). Ready for implementation.

---

## 1. The Approval Transaction (`POST /plans/{id}/decisions`)

All state mutations for a plan decision execute inside **one atomic PostgreSQL transaction** (`READ COMMITTED` with row-level `FOR UPDATE` locks and optimistic version checking). Any network read of live target state happens **just before** the transaction begins so row locks are never held across HTTP calls.

### Ordered Execution Sequence

1. **Authenticate Session (`UserPrincipal` Only)**
   - Validate the `opspilot_session` HttpOnly cookie against `app.sessions` (`revoked_at IS NULL AND expires_at > clock_timestamp()`).
   - Service principals (`Authorization: Bearer <service_token>`, i.e., `worker` or `broker`) are **strictly forbidden** from submitting plan decisions.
   - **Failure**: `401 unauthorized` (missing/expired/revoked session) or `403 forbidden` (service principal).

2. **Authorize Role, CSRF, and Tenant Context**
   - Verify `principal.has_role("reviewer")` (`reviewer` or `admin`; `viewer` is rejected).
   - Verify CSRF protection via `verify_csrf` (`X-Requested-With` custom header or `X-CSRF-Token` matching `csrftoken` cookie).
   - Bind `TenantContext(tenant_id=principal.tenant_id, user_id=principal.user_id, role=principal.role)` and set `SET LOCAL app.tenant_id = '<tenant_id>'` inside the DB transaction for PostgreSQL RLS.
   - *(Optional, pending Open Question 3)*: Separation-of-duties check against `runs.created_by_user_id`.
   - **Failure**: `403 forbidden` (insufficient role or SoD violation) or `403 csrf_failed`.

3. **Validate Headers, Request Schema & Rate Limit**
   - Enforce `plans_rate_limiter` per tenant/user (`429 rate_limited`).
   - Require `If-Match` header (integer run version, e.g., `"3"`) and `Idempotency-Key` header (non-empty string, $\le 128$ chars). Missing `If-Match` or `Idempotency-Key` fails before DB locking (`428 precondition_required` / `400 missing_idempotency_key`).
   - Validate request body `{decision, submitted_plan_hash, reason}` (`decision` normalized to `'approve'` or `'reject'`; `submitted_plan_hash` must be 64-char hex).
   - Check idempotency store (`app.approval_idempotency` keyed by `(tenant_id, plan_id, reviewer_id, idempotency_key)`): if an exact match exists, return the stored HTTP response immediately (`200 OK`, `idempotent_replay: true`).

4. **Pre-Transaction Target State Read (`approve` Only)**
   - When `decision == "approve"`, read the plan's `service` / `target_id` / `expected_version` (non-locking read) and query the lab target's live active version from `lab.deploys` / target profile endpoint **just before** opening the locking transaction.
   - *(Why before the transaction)*: Prevents holding PostgreSQL `FOR UPDATE` row locks across an external HTTP call. If the target profile endpoint is unreachable, fail fast with `503 target_unavailable` without mutating DB state.

5. **Begin Transaction & Load `action_plans` `FOR UPDATE`**
   - `SELECT * FROM app.action_plans WHERE id = :plan_id FOR UPDATE` (tenant-scoped by RLS).
   - **Failure**: `404 plan_not_found` (non-existent or cross-tenant).

6. **Verify Cryptographic `plan_hash` Parity**
   - Compare `submitted_plan_hash.strip().lower()` against `plan.plan_hash.lower()`.
   - **Failure**: `409 plan_hash_mismatch` (the reviewer's UI state does not match the exact committed plan).

7. **Check `action_plans.status` & Duplicate/Conflicting Decisions**
   - If `plan.status == 'expired'`: rollback/abort and return `410 plan_expired`.
   - If `plan.status == 'superseded'`: rollback/abort and return `409 plan_superseded`.
   - If `plan.status IN ('approved', 'rejected', 'executed', 'failed')`:
     - Inspect `app.approvals` for `plan_id`.
     - If the **same** `reviewer_id` previously recorded the **opposite** decision (`approve` vs `reject`), return `409 decision_conflict`.
     - If the **same** `reviewer_id` previously recorded the **same** decision with a *different* `Idempotency-Key`, return `409 already_decided` (same `Idempotency-Key` was already handled in Step 3).
     - If a **different** `reviewer_id` already decided the plan, return `409 already_decided`.
   - Otherwise `plan.status` **must** equal `'proposed'`; if not, return `409 invalid_plan_status`.

8. **Load `runs` `FOR UPDATE` & Verify `needs_review` + `If-Match`**
   - `SELECT * FROM app.runs WHERE id = :run_id FOR UPDATE`.
   - Verify `run.status == 'needs_review'`. If not, return `409 run_not_in_review`.
   - Verify `run.version == if_match_version`. If not, raise `ConcurrentModificationError` $\to$ `409 concurrent_modification` (including `expected_version` and `actual_version`).

9. **Verify Expiry Against DB Clock (`clock_timestamp()`)**
   - Evaluate `SELECT clock_timestamp() <= :expires_at` using PostgreSQL's `clock_timestamp()` (single time source per [proposal_design.md](file:///e:/opspilot/docs/proposal_design.md#L164-L168)).
   - If expired (`clock_timestamp() > plan.expires_at`):
     - Update `plan.status = 'expired'`, transition `run` `needs_review -> escalated` via `RunService` with `last_error = {"code": "approval_expired", ...}`, append `approval_expired` event, **commit** this expiry transition, and return `410 plan_expired`.

10. **(`approve` Only) Compare Live Target Version to `plan.expected_version`**
    - Compare the `live_version` read in Step 4 against `plan.expected_version`.
    - If `live_version != plan.expected_version`:
      - Update `plan.status = 'superseded'`.
      - Transition `run` `needs_review -> escalated` via `RunService` (`last_error = {"code": "stale_target", "reason": "target_changed", "details": {"expected_version": plan.expected_version, "live_version": live_version}}`).
      - Append `plan_superseded` event, **commit** this transition, and return `409 stale_target` (see Section 3).

11. **Insert `app.approvals` Row**
    - `INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, decided_at, reason) VALUES (...)`.
    - Enforced by `UNIQUE (plan_id, reviewer_id)`. Any race on the same `(plan_id, reviewer_id)` hits unique violation $\to$ `409 already_decided` / `409 decision_conflict`.

12. **Transition `app.action_plans.status`**
    - On `approve`: `UPDATE app.action_plans SET status = 'approved' WHERE id = :plan_id`.
    - On `reject`: `UPDATE app.action_plans SET status = 'rejected' WHERE id = :plan_id`.

13. **(`approve` Only) Insert `app.action_executions` Row (`queued`)**
    - Compute deterministic `action_key`:
      $$\text{action\_key} = \text{SHA-256}\big(\text{UTF-8}(\texttt{f"\{plan\_id\}:\{plan\_hash\}"})\big).\text{hexdigest}()$$
      where `plan_id` is the canonical lowercase hyphenated UUID string (e.g., `"550e8400-e29b-41d4-a716-446655440000"`) and `plan_hash` is the 64-char lowercase hex SHA-256 digest of the plan (which already commits `tenant_id`, `run_id`, `action`, `service`, `target_id`, `expected_version`, `rollback_to_version`, `rationale_evidence_ids`, and `expires_at`).
    - `INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, status, attempts) VALUES (:exec_id, :plan_id, :tenant_id, :action_key, 'queued', 0)`.
    - Protected by `UNIQUE (plan_id)` and `UNIQUE (action_key)`.

14. **Transition `app.runs` via `RunService`**
    - On `approve`: `RunService.transition(ctx, run_id, to_status="approved", actor=f"reviewer:{principal.user_id}", expected_version=if_match_version, payload={"plan_id": str(plan_id), "plan_hash": plan_hash})`.
    - On `reject`: `RunService.transition(ctx, run_id, to_status="escalated", actor=f"reviewer:{principal.user_id}", expected_version=if_match_version, payload={"plan_id": str(plan_id), "reason": "reviewer_rejected"})` plus `RunService.record_error(...)` setting `last_error = {"code": "reviewer_rejected", "message": "Plan rejected by human reviewer", "details": {"plan_id": str(plan_id), "reviewer_id": str(principal.user_id)}}`.

15. **Append `approval_recorded` Event & Persist Idempotency Snapshot**
    - Call `RunService.append_event(ctx, run_id, event_type="approval_recorded", actor=f"reviewer:{principal.user_id}", payload={"plan_id": str(plan_id), "approval_id": str(approval_id), "decision": decision, "plan_hash": plan_hash, "action_execution_id": str(exec_id) if approve else None})`.
    - Persist the response payload in `app.approval_idempotency` keyed by `(tenant_id, plan_id, reviewer_id, idempotency_key)`.

16. **Commit Transaction & Notify Worker**
    - `COMMIT`.
    - After commit, emit `NOTIFY opspilot_plan_decided, '<run_id>'` (best-effort; worker polling is the safety net) and return `200 OK`.

---

## 2. Reject Path

When `decision == "reject"`:
- Inserts `app.approvals` with `decision = 'reject'` and non-empty `reason`.
- Transitions `app.action_plans.status` from `'proposed'` to `'rejected'`.
- Does **not** insert any `app.action_executions` row.
- **Proposed Run Transition**: `needs_review -> escalated` with `runs.last_error = {"code": "reviewer_rejected", ...}` (matching [outcome_contract.md](file:///e:/opspilot/docs/outcome_contract.md#L107) and [data_model.md](file:///e:/opspilot/docs/data_model.md#L323)).

### Alternatives & Tradeoffs

| Option | Behavior | Pros | Cons / Risks |
| :--- | :--- | :--- | :--- |
| **Option A (Recommended)** | `run -> escalated` (`reviewer_rejected`). Human may optionally click **Re-investigate** (`escalated -> investigating`). | Safe by default; prevents runaway LLM loops; keeps human in control of when/whether to spend another investigation budget. | Requires an extra click if the operator wanted an immediate retry with feedback. |
| **Option B** | Auto-transition `needs_review -> investigating` with `reason` injected into LLM context. | Faster iteration when the proposal was slightly off. | Risk of infinite human/agent retry loop; `reason` is free-text that could inject prompt instructions; ambiguous tool-budget reset rules. |
| **Option C** | `run -> completed` (`resolution = 'rejected_closed'`). | Fewer `escalated` items in queue. | Semantically misleading: the underlying incident was not mitigated or verified healthy. |

---

## 3. Stale-Target Path

If the target's live version read in Step 4 differs from `plan.expected_version` when `decision == "approve"`:
- **Proposed Behavior**:
  1. Transition `action_plans.status` to `'superseded'`.
  2. Transition `runs.status` to `'escalated'` with `last_error = {"code": "stale_target", "reason": "target_changed", "details": {"expected_version": ..., "live_version": ...}}`.
  3. Commit this transition and return `409 Conflict` (`code: "stale_target"`).
  4. A human operator starts a new run (`POST /runs`) or triggers re-investigation (`escalated -> investigating`).

### Alternatives & Tradeoffs

| Option | Behavior | Why Choose / Why Not |
| :--- | :--- | :--- |
| **Escalate & Require New Run (Recommended)** | `409 stale_target`, `plan -> superseded`, `run -> escalated`. | **Choose this**: A version change invalidates the investigation's telemetry and diagnosis (`observations` and `culprit_deploy_id` were captured against the old version). Acting without fresh investigation violates the outcome contract. |
| **Automatic Re-Proposal** | Automatically re-enter `investigating` / `propose` against the new live version. | **Do NOT choose**: Re-proposing using stale observations risks misdiagnosing the new state, burning LLM/tool budget silently, and surprising operators who expected an explicit failure on drift. |
| **Patch `expected_version` in Place** | Mutate `action_plans.expected_version` to match live version and proceed. | **Do NOT choose**: Violates `plan_hash` immutability (enforced by DB trigger `trg_action_plans_enforce_immutability`) and executes against a state the human never reviewed. |

---

## 4. Duplicate, Conflicting, and Expired Requests

| Scenario | Behavior | HTTP Status & Code |
| :--- | :--- | :--- |
| **Same reviewer + same decision + same `Idempotency-Key`** | Return exact original response from `app.approval_idempotency` (`idempotent_replay: true`), no new DB writes. | `200 OK` |
| **Same reviewer + same decision + different `Idempotency-Key`** | Plan is no longer `proposed`; `approvals` row already exists for this reviewer. | `409 Conflict` (`already_decided`) |
| **Same reviewer + different decision** (e.g., `approve` then `reject`) | Refused regardless of `Idempotency-Key`; first decision is final. | `409 Conflict` (`decision_conflict`) |
| **Different reviewer after a decision** | Single-reviewer quorum in v1; second reviewer cannot override or double-approve. | `409 Conflict` (`already_decided`) |
| **Expired plan (`clock_timestamp() > expires_at` or `status == 'expired'`)** | Transitions `plan -> expired`, `run -> escalated` (`approval_expired`), and refuses decision. | `410 Gone` (`plan_expired`) |

### Justification for `410 Gone` vs `409 Conflict` on Expiry
**Pick `410 Gone` (`plan_expired`)**:
- `409 Conflict` signals a state/version conflict where the client can refresh and potentially act on the resource (e.g., `concurrent_modification` where re-fetching `run.version` allows retrying).
- An expired plan's TTL window has **permanently elapsed** and `expires_at` is baked into `plan_hash`, so that `plan_id` can **never** be approved. `410 Gone` gives the Review UI an unambiguous signal to disable the Approve/Reject buttons and prompt the operator to start a new run.

---

## 5. Separation of Duties (SoD)

Currently, `app.incidents` originate from alerts or seeded cases, and `app.runs` does not yet track `created_by_user_id` (see [0003_core_domain_tables.py](file:///e:/opspilot/infra/alembic/versions/0003_core_domain_tables.py)).

| Option | Policy | Pros | Cons |
| :--- | :--- | :--- | :--- |
| **Option A: No SoD Restriction** | Any `reviewer` or `admin` in the tenant may approve any plan. | Zero friction for single-operator local lab demos. | Allows the same user who manually triggered a run to self-approve it. |
| **Option B: Strict 4-Eyes** | Record `runs.created_by_user_id`; forbid approval (`403 separation_of_duties_violation`) if `created_by_user_id == principal.user_id`. | Strongest compliance posture for human-triggered runs. | Breaks single-user local lab demos unless two reviewer accounts are used; needs exception for system/alert-triggered runs (`created_by_user_id IS NULL`). |
| **Option C: Configurable Tenant Policy (Recommended)** | Record `runs.created_by_user_id` for audit; enforce SoD only when `ENFORCE_SEPARATION_OF_DUTIES=true` (or tenant setting, default `false` in local lab). | Full audit trail from day one; single-user demos work out of the box while enterprise/eval modes can toggle strict 4-eyes. | Slightly more configuration surface. |

**Recommendation**: **Option C** — record `created_by_user_id` on `app.runs` (nullable for system/alert-triggered runs) and gate the `403 separation_of_duties_violation` check behind `ENFORCE_SEPARATION_OF_DUTIES` (default `false` for local lab).

---

## 6. API Contract: `POST /plans/{id}/decisions`

### Request Headers
| Header | Required | Format / Rule |
| :--- | :--- | :--- |
| `Cookie` | Yes | `opspilot_session=<token>` (validated to `UserPrincipal`; service tokens rejected) |
| `X-Requested-With` / `X-CSRF-Token` | Yes | CSRF protection per [csrf.py](file:///e:/opspilot/apps/api/auth/csrf.py) |
| `If-Match` | Yes | Integer string matching `runs.version` (e.g., `"3"`). |
| `Idempotency-Key` | Yes | Non-empty client-generated key ($\le 128$ chars, e.g., UUIDv4). |

### Request Body (`PlanDecisionRequest`)
```json
{
  "decision": "approve",
  "submitted_plan_hash": "a3f1...64-char-lowercase-hex...",
  "reason": "Verified orders v1.1.0 pool leak in logs; safe to roll back to v1.0.0."
}
```
- `decision`: `"approve" | "reject"` *(note: aligns `PlanDecisionRequest` in [schemas.py](file:///e:/opspilot/apps/api/routers/schemas.py#L370-L378) with `app.approvals.decision CHECK (decision IN ('approve', 'reject'))`)*.
- `submitted_plan_hash`: 64-character hex SHA-256 string.
- `reason`: Plain text string (`""` allowed on `approve`; recommended/required non-empty on `reject`, max 1000 chars).
- Identity (`reviewer_id`, `tenant_id`) is **never** accepted in the body, headers, or query params; it comes **only** from `Principal`.

### Success Response (`200 OK`)
```json
{
  "plan_id": "550e8400-e29b-41d4-a716-446655440000",
  "run_id": "7a8b9c1d-0000-4000-8000-000000000001",
  "approval_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "decision": "approve",
  "plan_status": "approved",
  "run_status": "approved",
  "run_version": 4,
  "action_execution_id": "c1d2e3f4-0000-4000-8000-000000000002",
  "action_key": "8f4e...64-char-hex...",
  "decided_at": "2026-11-25T16:10:00.123456Z",
  "idempotent_replay": false
}
```
*(For `decision: "reject"`, `plan_status` is `"rejected"`, `run_status` is `"escalated"`, and `action_execution_id` / `action_key` are `null`.)*

### Error Codes Table

| HTTP Status | Typed `code` | Trigger Condition |
| :--- | :--- | :--- |
| `400` | `invalid_request` | Malformed JSON, invalid `decision`, or missing `Idempotency-Key`. |
| `401` | `unauthorized` | Missing, expired, or revoked `opspilot_session` cookie. |
| `403` | `forbidden` / `csrf_failed` / `separation_of_duties_violation` | Non-reviewer role (`viewer`, `worker`, `broker`), CSRF check failed, or SoD policy violated. |
| `404` | `plan_not_found` | `plan_id` does not exist or belongs to another tenant (RLS). |
| `409` | `plan_hash_mismatch` | `submitted_plan_hash != action_plans.plan_hash`. |
| `409` | `concurrent_modification` | `If-Match` does not match current `runs.version`. |
| `409` | `run_not_in_review` | Owning `run.status != 'needs_review'`. |
| `409` | `already_decided` | Plan already decided by another reviewer (or same reviewer with different `Idempotency-Key`). |
| `409` | `decision_conflict` | Same reviewer submitted the opposite decision on an already-decided plan. |
| `409` | `plan_superseded` | Plan was already superseded by a newer proposal. |
| `409` | `stale_target` | Live target version differs from `plan.expected_version` (plan marked `superseded`, run `escalated`). |
| `410` | `plan_expired` | `plan.status == 'expired'` or `clock_timestamp() > plan.expires_at`. |
| `422` | `validation_error` | Pydantic schema validation failure (e.g. non-hex hash, extra fields). |
| `428` | `precondition_required` | Missing or non-integer `If-Match` header. |
| `429` | `rate_limited` | Exceeded `plans_rate_limiter` threshold. |
| `503` | `target_unavailable` | Could not read live target version before approval transaction. |

---

## 7. Checkpointing Design (`checkpoints` Schema)

LangGraph uses `PostgresSaver` (`langgraph-checkpoint-postgres`) configured with `search_path = checkpoints, public` and `thread_id = str(run_id)` (`checkpoint_ns = ""`).

| Store | What It Holds | Authoritative For |
| :--- | :--- | :--- |
| **`checkpoints.*`** (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) | Graph execution state (`InvestigationState`), message/step history, hypothesis ledger mirror, `proposal_rejections` counter, and LangGraph `Interrupt` / `Scratchpad` resume state. | **Graph execution resumption** (where in the `StateGraph` to resume after `interrupt()` or worker restart). |
| **`app.*`** (`runs`, `observations`, `hypotheses`, `diagnoses`, `action_plans`, `approvals`, `action_executions`, `verification_results`, `run_events`) | Domain state machines (`runs.status`, `runs.version`, `action_plans.status`), immutable evidence, cryptographic `plan_hash`, signed `approvals`, broker `action_executions`, and audit log. | **All business truth, authorization, UI display, broker dispatch, and evaluation scoring.** |

### Isolation Rule
- **Tables in `app` NEVER reference `checkpoints` rows**: no foreign keys, no `checkpoint_id` columns, and no joins to `checkpoints.*`.
- Database roles enforce this boundary ([0002_schemas_and_roles.py](file:///e:/opspilot/infra/alembic/versions/0002_schemas_and_roles.py)): `opspilot_checkpointer` has `REVOKE ALL ON SCHEMA app`; `opspilot_api` and `broker` have `REVOKE ALL ON SCHEMA checkpoints`.
- If `checkpoints.*` is wiped or pruned, all `app.*` audit records, approvals, and execution records remain 100% intact.

---

## 8. The Interrupt Node & Resume Semantics

### LangGraph `interrupt()` Execution Model
In the installed `langgraph.types.interrupt(value)`:
1. On first execution of the node, `interrupt(value)` raises `GraphInterrupt(Interrupt(value=value, id=...))`, halting the super-step and persisting the checkpoint.
2. When resumed via `graph.invoke(Command(resume=resume_payload), config={"configurable": {"thread_id": str(run_id)}})`, LangGraph **re-executes the interrupted node from the start of the node**. On re-execution, `interrupt()` finds the resume value in `scratchpad` and returns it.

### Placement & Idempotency Before `interrupt()`
To keep side effects clean and idempotent on re-run:
- **`propose` node** runs *before* the interrupt node: it validates the proposal, persists `app.action_plans` (atomically superseding any prior `proposed` plan), transitions `runs` to `needs_review`, and emits `plan_proposed`.
- **`await_approval` node** is a dedicated node immediately after `propose`:
  - **Side effects before `interrupt()`**: **Zero database mutations.** It only constructs the read-only interrupt payload from state (or performs a read-only check) and calls `decision = interrupt(payload)`.
  - **Why**: Because `await_approval` performs no writes before `interrupt()`, re-executing the node from the top on `Command(resume=...)` is trivially 100% idempotent—no duplicate `action_plans`, no duplicate `run_events`, and no `ConcurrentModificationError` on `runs`.

### Interrupt Payload Exposed
```python
{
    "run_id": state["run_id"],
    "plan_id": state["proposed_plan_id"],
    "plan_hash": state["proposed_plan_hash"],
    "action": state["proposed_action"],
    "service": state["proposed_service"],
    "target_id": state["proposed_target_id"],
    "expected_version": state["proposed_expected_version"],
    "rollback_to_version": state["proposed_rollback_to_version"],
    "expires_at": state["proposed_expires_at"],
}
```

### How Resume Is Triggered
- **Primary wake-up**: After `POST /plans/{id}/decisions` (or the expiry reaper / cancel endpoint) commits, the API/reaper issues `NOTIFY opspilot_plan_decided, '<run_id>'`.
- **Safety net**: The worker's poll loop (`WORKER_POLL_INTERVAL_SECONDS`, default `0.5s`) queries `app.runs` / `app.action_plans` for runs whose plan has transitioned out of `proposed` (or whose run transitioned to `approved`/`escalated`) while the graph checkpoint still has a pending interrupt.

### What "Resume" Means per Outcome
| Outcome | `Command(resume=...)` Payload | Graph Action After `interrupt()` Returns |
| :--- | :--- | :--- |
| **`approve`** | `{"outcome": "approved", "plan_id": ..., "action_execution_id": ..., "action_key": ...}` | Routes to execution/verification handoff node (waits for broker completion / hands off to verifier in Feature 9/10, or exits cleanly to `END` if broker+verifier run asynchronously). |
| **`reject`** | `{"outcome": "rejected", "plan_id": ..., "reason": ...}` | Records terminal `rejected` outcome in graph state and exits cleanly to `END` (checkpoint thread closed; `app.runs` is already `escalated`). |
| **`expired`** | `{"outcome": "expired", "plan_id": ...}` | Records `approval_expired` in graph state and exits cleanly to `END` (`app.runs` is already `escalated`). |
| **`cancelled`** | `{"outcome": "cancelled"}` | Records `cancelled` in graph state and exits cleanly to `END` (`app.runs` is already `escalated`). |

---

## 9. Worker Lease & Crash Recovery

> [!IMPORTANT]
> Currently, `app.runs` has `trg_runs_enforce_transition` ([0004_db_invariants.py](file:///e:/opspilot/infra/alembic/versions/0004_db_invariants.py#L120-L150)), which increments `NEW.version = OLD.version + 1` on **every** `UPDATE app.runs`. Updating `heartbeat_at` directly on `app.runs` would silently bump `runs.version` and break `If-Match` optimistic concurrency!

### Lease Schema & Fencing
To keep heartbeats decoupled from `runs.version`, lease state is stored in a dedicated **`app.run_leases`** table (1-to-1 with `app.runs`):
- `run_id UUID PRIMARY KEY REFERENCES app.runs(id) ON DELETE CASCADE`
- `tenant_id UUID NOT NULL REFERENCES app.tenants(id)`
- `worker_id VARCHAR(128) NOT NULL`
- `lease_token UUID NOT NULL` *(fresh UUID generated on every claim or reclaim)*
- `heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()`
- `lease_expires_at TIMESTAMPTZ NOT NULL`

### Timing & Reclaim Query
- **Heartbeat interval**: `10s` (`WORKER_HEARTBEAT_INTERVAL_SECONDS = 10`).
- **Lease TTL / expiry**: `35s` (`WORKER_LEASE_TTL_SECONDS = 35`, i.e. $> 3\times$ heartbeat interval).
- **Reclaim query**:
  ```sql
  SELECT r.id, r.tenant_id, r.status, r.version, l.lease_token
  FROM app.runs r
  JOIN app.run_leases l ON l.run_id = r.id
  WHERE r.status IN ('investigating', 'verifying')
    AND l.lease_expires_at < clock_timestamp()
  ORDER BY l.lease_expires_at ASC
  LIMIT 1
  FOR UPDATE OF r, l SKIP LOCKED;
  ```
  On reclaim, the new worker updates `app.run_leases` with its own `worker_id`, a **new** `lease_token`, and `lease_expires_at = clock_timestamp() + interval '35 seconds'`, and emits a `lease_reclaimed` event in `app.run_events`.

### Fencing Against Zombie Workers
- Every worker state write (`RunService.transition`, `record_error`, `decrement_calls`, `save_diagnosis`, and heartbeat refresh) includes a fencing check:
  `UPDATE app.run_leases SET heartbeat_at = clock_timestamp(), lease_expires_at = ... WHERE run_id = :run_id AND lease_token = :lease_token`.
- If a zombie worker wakes up after a GC pause or network partition and tries to write or refresh its heartbeat, the `lease_token` no longer matches (`0 rows affected`), raising `LeaseLostError` and forcing the zombie worker to abort immediately without mutating `app.runs` or `app.observations`.

### Reclaimed Run Behavior by State
| `run.status` | Action on Reclaim / Recovery |
| :--- | :--- |
| **`queued`** | Normal claim via `FOR UPDATE SKIP LOCKED`, create lease, transition `queued -> investigating`, start graph. |
| **`investigating`** | Load latest checkpoint from `checkpoints.*` for `thread_id = run_id` and resume graph execution from the last committed super-step (or mark `failed` if `RUN_TIMEOUT_SECONDS` elapsed). |
| **`needs_review`** | **No active worker lease held** while waiting for human input. If a decision/expiry/cancel has already committed in `app.*`, a worker claims the resume task and invokes `Command(resume=...)`. |
| **`approved`** | Owned by the **Broker** (Feature 9) for `action_executions` dispatch, not the agent worker; worker only resumes graph if awaiting post-execution handoff. |
| **`executing`** | Owned by the **Broker**; agent worker never touches `executing` runs. |
| **`verifying`** | Reclaim lease and re-run deterministic verification probe (Feature 10). |
| **`escalated` / `completed`** | Terminal states. If checkpoint still has an open interrupt (e.g., rejected/expired/cancelled), worker runs `Command(resume=...)` once to close the graph to `END`, then releases lease. |

---

## 10. Crash Matrix

| Crash Point | What Survives in PostgreSQL | System Behavior on Restart / Recovery |
| :--- | :--- | :--- |
| **1. Before approval txn** | `action_plans` (`proposed`), `runs` (`needs_review`), `checkpoints` (paused at `await_approval`). | Client receives connection error / 5xx; retries `POST /plans/{id}/decisions` with same `Idempotency-Key` and `If-Match`. Clean execution. |
| **2. Mid-txn (before `COMMIT`)** | PostgreSQL rolls back all uncommitted writes (`approvals`, `action_executions`, `runs`, `run_events`, `approval_idempotency`). | `action_plans` remains `proposed`, `runs` remains `needs_review` at original `version`. Client retry succeeds cleanly. |
| **3. After commit, before graph resume** | `approvals` row, `action_plans` (`approved`/`rejected`), `action_executions` (`queued`, if approved), `runs` (`approved`/`escalated`), `approval_idempotency`. `checkpoints` still has pending interrupt. | `NOTIFY` may be lost, but worker safety-net poll detects decided plan with open checkpoint interrupt, claims resume, and calls `graph.invoke(Command(resume=...))`. |
| **4. During interrupt wait (`needs_review`)** | `action_plans` (`proposed`), `runs` (`needs_review`), `checkpoints` (paused at `await_approval`). Zero worker leases held. | Nothing lost. Worker restart is a no-op; run remains `needs_review` until human decides or reaper expires `expires_at`. |
| **5. After resume, before next write** | `app.*` already has `approved`/`escalated` and `action_executions`. `checkpoints` still points to `await_approval` (next super-step not yet checkpointed). | Lease expires (`35s`); new worker reclaims run, sees `await_approval` interrupt unanswered in `checkpoints` while `app.action_plans` is decided, and re-invokes `Command(resume=...)` idempotently. |
| **6. During verification handoff** | `action_executions` (`acknowledged`), `runs` (`verifying`), `checkpoints` at verification handoff node. | Lease expires (`35s`); new worker reclaims `verifying` run and re-runs the idempotent read-only verification probe (Feature 10). |

---

## 11. Explicit Non-Guarantee: Target-Side Exactly-Once Effects

This approval & interrupt/resume design **explicitly does NOT guarantee exactly-once effects on the target service**.

- **What it guarantees**: At-most-once **human approval** per plan, atomic state transition (`needs_review -> approved`), and durable creation of a single `app.action_executions` (`queued`) intent row with a deterministic `action_key`.
- **Why target-side exactly-once is outside this boundary**: Once the isolated `broker` service sends an HTTP mutation (`POST /admin/actions/restart` or deploy rollback) to a target container, a network timeout or process crash mid-flight leaves the target in an `uncertain` state—PostgreSQL cannot participate in a distributed 2PC transaction with a container's in-memory state. Guaranteeing safe execution, pre-flight target checks, `uncertain` reconciliation, and no automatic retries on ambiguous outcomes is the responsibility of the **Broker** (Feature 9) using `action_key` and `target_state_before`/`target_state_after`.

---

## 12. Metrics for Demo & Evaluation Report

All four metrics are derived deterministically from `app.run_events` and API/worker Prometheus counters:

1. **`duplicate_approval_attempts_blocked`**: Count of `POST /plans/{id}/decisions` requests rejected with `409 already_decided`, `409 decision_conflict`, or `200 idempotent_replay: true` (reported separately as `replayed_idempotent` vs `blocked_conflicting`).
2. **`resumes_after_kill`**: Count of runs whose `await_approval` interrupt was successfully resumed via `Command(resume=...)` by a **different** `worker_id` (or restarted worker process) than the one that originally ran `propose`.
3. **`reclaimed_runs`**: Count of `lease_reclaimed` events in `app.run_events` where a new worker took over an expired lease (`lease_expires_at < clock_timestamp()`).
4. **`approvals_refused_by_reason`**: Histogram of refused `POST /plans/{id}/decisions` requests grouped by typed error `code` (`plan_hash_mismatch`, `stale_target`, `plan_expired`, `already_decided`, `decision_conflict`, `concurrent_modification`, `separation_of_duties_violation`, `forbidden`).

---

## 13. Open Questions for User Approval

Please confirm your decisions on the following four points before implementation begins:

1. **Reject Handling**: Confirm **Option A** (`needs_review -> escalated` with `last_error.code = "reviewer_rejected"`, requiring explicit human click to re-investigate), or prefer automatic `needs_review -> investigating` with reviewer feedback?
2. **Stale-Target Handling**: Confirm returning `409 stale_target` with `plan -> superseded` and `run -> escalated` (`target_changed`), requiring a human to start a new run—or do you want automatic re-proposal?
3. **Separation of Duties**: Should the user who created an incident/run be allowed to approve its plan in v1 (**Option A**), strictly blocked (**Option B**), or gated behind a configurable `ENFORCE_SEPARATION_OF_DUTIES` flag defaulting to `false` for local lab demos (**Option C**)?
4. **Expiry HTTP Status Code**: Confirm **`410 Gone`** (`plan_expired`) vs `409 Conflict` when a decision is submitted for an expired plan?
