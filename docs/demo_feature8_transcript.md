# Feature 8 Demo Transcript (Offline, No Network)

- **Mode**: `offline-sqlite-fallback (Postgres unreachable locally; re-run with live Postgres + real broker/lab in Codespaces)`
- **Safety Note**: **Nothing is executed on any lab target** (`action_executions` remains `queued` for Feature 9 Broker).

## Variant 1: Restart-and-Approve (Scenario S1)
- Run `1f003016-1663-49d0-b556-66062944b400` paused at `await_review` (`needs_review`), worker killed & restarted (`snap.next=('await_review',)`).
- `POST /plans/1a3ee6a6-1dd2-4e2f-8ca0-9f6244ddc8f1/decisions` (approve): HTTP `200` (`plan_status=approved`, `action_execution_id=db1b44c3-87f4-43db-b8bb-9506683d6b43`).
- Duplicate approval with new key: HTTP `409` (`already_decided`).
- Worker resume driver resumed `[UUID('1f003016-1663-49d0-b556-66062944b400')]` -> final `run.status=approved`, `approvals=1`, `action_executions=1` (`queued`, **0 target mutations**).
- **Events Timeline**:
- seq=1 | type=`status_transition` | actor=`reviewer:ca373244-2ff1-4818-b447-a520e7e20c2d` | payload=`{"from_status": "needs_review", "to_status": "approved", "expected_version": 3, "plan_id": "1a3ee6a6-1dd2-4e2f-8ca0-9f6244ddc8f1", "plan_hash": "7889e15240687496b5ebbc12458d8817f86701052f53ff765252a394ad22a02b", "action_execution_id": "db1b44c3-87f4-43db-b8bb-9506683d6b43"}`
- seq=2 | type=`approval_recorded` | actor=`reviewer:ca373244-2ff1-4818-b447-a520e7e20c2d` | payload=`{"plan_id": "1a3ee6a6-1dd2-4e2f-8ca0-9f6244ddc8f1", "approval_id": "df52d2cd-84df-43d4-8e06-0e38d3b114bd", "decision": "approve", "plan_hash": "7889e15240687496b5ebbc12458d8817f86701052f53ff765252a394ad22a02b", "action_execution_id": "db1b44c3-87f4-43db-b8bb-9506683d6b43", "action_key": "e4ae91e9b7324f564b8a3cc958c4c88f15e9f19fda1604a1c1216c4aa645340e"}`
- seq=3 | type=`graph_resumed` | actor=`worker:demo-worker-2` | payload=`{"plan_id": "1a3ee6a6-1dd2-4e2f-8ca0-9f6244ddc8f1", "outcome": "approved"}`

## Variant 2: Stale Target Refusal
- Target version drifted (`v1.1.0` -> `v1.2.0`); `POST /plans/b2b5e020-97f3-4ba2-9f38-b396b0d3d2a5/decisions` returned HTTP `409` (`stale_target`: `Target changed since investigation; start a new run to re-investigate.`).
- **Events Timeline**:
- seq=1 | type=`status_transition` | actor=`system:stale_target` | payload=`{"from_status": "needs_review", "to_status": "escalated", "expected_version": "v1.1.0", "plan_id": "b2b5e020-97f3-4ba2-9f38-b396b0d3d2a5", "reason": "target_changed", "live_version": "v1.2.0"}`
- seq=2 | type=`error_recorded` | actor=`system:stale_target` | payload=`{"error": {"code": "stale_target", "reason": "target_changed", "message": "Target version changed from 'v1.1.0' to 'v1.2.0' before approval", "details": {"plan_id": "b2b5e020-97f3-4ba2-9f38-b396b0d3d2a5", "expected_version": "v1.1.0", "live_version": "v1.2.0"}}, "current_status": "escalated"}`
- seq=3 | type=`plan_superseded` | actor=`system:stale_target` | payload=`{"plan_id": "b2b5e020-97f3-4ba2-9f38-b396b0d3d2a5", "reason": "target_changed", "expected_version": "v1.1.0", "live_version": "v1.2.0"}`

## Variant 3: Expired Plan Refusal
- Plan TTL elapsed; `POST /plans/b04a923f-9422-4946-bed1-c5064ac77e65/decisions` returned HTTP `410` (`plan_expired`: `Plan expired; ask for a new investigation.`).
- **Events Timeline**:
- seq=1 | type=`status_transition` | actor=`system:approval_expiry` | payload=`{"from_status": "needs_review", "to_status": "escalated", "expected_version": 3, "plan_id": "b04a923f-9422-4946-bed1-c5064ac77e65", "reason": "approval_expired"}`
- seq=2 | type=`error_recorded` | actor=`system:approval_expiry` | payload=`{"error": {"code": "approval_expired", "message": "Action plan expired before approval", "details": {"plan_id": "b04a923f-9422-4946-bed1-c5064ac77e65"}}, "current_status": "escalated"}`
- seq=3 | type=`approval_expired` | actor=`system:approval_expiry` | payload=`{"plan_id": "b04a923f-9422-4946-bed1-c5064ac77e65", "expires_at": "2026-11-25T17:03:11.573950+00:00"}`

## Invariants & Next Steps
- **db_invariants**: offline-sqlite checks clean (approvals=1, executions=1, 0 unapproved executions; run scripts/db_invariants.py against live Postgres in Codespaces)
- **Re-run in Codespaces**: Execute `scripts/demo_feature8.py` and `scripts/db_invariants.py` against live PostgreSQL + Feature 9 Broker + local Docker lab.
