# Feature 9 Execution Broker Demo Transcript

- Mode: `offline (FakeTarget)`
- Generated at: `2026-11-27T05:16:48.540502+00:00`

## 1. S1 Rollback (v1.3.0 -> v1.0.0)
- `2026-11-27T05:16:48.541521+00:00` | `reviewer` | `approval_recorded` | plan=a9ddab60-ff79-4e5e-89a4-61051dbe8e0c, hash=2230f09df4a5...
- `2026-11-27T05:16:48.541521+00:00` | `broker` | `execution_claimed` | action_key=a9ee8f5fbd1cfabe...
- `2026-11-27T05:16:48.541521+00:00` | `broker` | `prechecks_completed` | B1-B10 passed (expected=v1.3.0, live=v1.3.0)
- `2026-11-27T05:16:48.541521+00:00` | `broker` | `execution_dispatched` | POST /admin/actions/rollback
- `2026-11-27T05:16:48.541521+00:00` | `broker` | `execution_acknowledged` | outcome=applied, current_version=v1.0.0
- `2026-11-27T05:16:48.541521+00:00` | `broker` | `run_verifying` | run=7ebe63e8-36c8-4a3f-85c5-1100821458da
- `FakeTarget.versions['lab-orders']` = `v1.0.0`
- `FakeTarget.applied_actions` count = `1`

## 2. Queue Replay (Idempotent Deduplication)
- Replayed `action_key=a9ee8f5fbd1cfabe...` -> target outcome = `already_applied`
- `FakeTarget.applied_actions` count = `1` (still 1 effect)

## 3. Dropped-Response Variant (Reconcile Before Retry)
- Dispatch hit dropped response: `uncertain (ConnectionResetError)`
- Reconcile queried `GET /admin/actions/60c3250ba081ea1b...` -> `status=applied` (transitioned `uncertain -> reconciled`, 0 second dispatches)
- `FakeTarget.versions['lab-orders']` = `v1.0.0`
- Total distinct `applied_actions` = `2` (2 total: 1 per key)
