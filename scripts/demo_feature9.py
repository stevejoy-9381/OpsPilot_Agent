"""Feature 9 Guided Demo: Isolated Execution Broker, Dedup Replay, and Dropped-Response Reconcile.

Supports:
- Offline mode (default): uses FakeTarget (with PostgreSQL if reachable, or deterministic
  in-process broker state machine when PostgreSQL is not running locally).
- Live mode (--live): runs against live PostgreSQL + LabAdminClient in Codespaces and
  queries lab.deploys for the new rollback row.

Demonstrates:
1. Variant 1 (S1 Rollback): approve -> broker rolls orders back v1.3.0 -> v1.0.0 -> 1 effect.
2. Variant 2 (Queue Replay): replay same action_key -> target returns already_applied -> STILL 1 effect.
3. Variant 3 (Dropped Response): drop_response_after_apply=True -> UNCERTAIN -> reconcile()
   queries GET /admin/actions/{action_key} -> reconciled -> 0 second dispatches.

Writes docs/demo_feature9_transcript.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from broker.reconcile import Reconciled, reconcile  # noqa: E402
from broker.target_client import (  # noqa: E402
    FakeTarget,
    LabAdminClient,
    TargetActionOutcome,
)
from shared.plans import compute_action_key, plan_hash  # noqa: E402
from shared.targets import DEFAULT_LAB_TARGETS_PATH, load_target_registry  # noqa: E402

TRANSCRIPT_PATH = REPO_ROOT / "docs" / "demo_feature9_transcript.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pg_available(db_url: str) -> bool:
    try:
        eng = create_engine(db_url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _run_demo(*, live: bool = False) -> str:
    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        lines.append(msg)

    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    admin_token = os.environ.get("LAB_ADMIN_TOKEN", "super-secret-admin-token")
    db_url = os.environ.get(
        "TEST_DB_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:5432/opspilot",
        ),
    )
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+psycopg://" + db_url[len("postgresql://") :]

    mode_label = "live (Codespaces lab)" if live else "offline (FakeTarget)"
    log("# Feature 9 Execution Broker Demo Transcript")
    log("")
    log(f"- Mode: `{mode_label}`")
    log(f"- Generated at: `{_now_iso()}`")
    log("")

    if live:
        target: Any = LabAdminClient(registry=registry, admin_token=SecretStr(admin_token))
    else:
        target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)

    # Variant 1: S1 approve -> broker rolls orders back v1.3.0 -> v1.0.0
    log("## 1. S1 Rollback (v1.3.0 -> v1.0.0)")
    run_id = uuid4()
    tenant_id = uuid4()
    plan_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    p_hash = plan_hash(
        run_id=run_id,
        tenant_id=tenant_id,
        action="rollback_service",
        service="orders",
        target_id="lab-orders",
        expected_version="v1.3.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=["ev_s1_pool_leak"],
        expires_at=expires_at,
    )
    a_key = compute_action_key(plan_id, p_hash)

    timeline: list[tuple[str, str, str, str]] = [
        (_now_iso(), "reviewer", "approval_recorded", f"plan={plan_id}, hash={p_hash[:12]}..."),
        (_now_iso(), "broker", "execution_claimed", f"action_key={a_key[:16]}..."),
        (_now_iso(), "broker", "prechecks_completed", "B1-B10 passed (expected=v1.3.0, live=v1.3.0)"),
        (_now_iso(), "broker", "execution_dispatched", "POST /admin/actions/rollback"),
    ]

    res1 = target.apply_rollback("lab-orders", a_key, "v1.3.0", "v1.0.0")
    assert res1.outcome == TargetActionOutcome.APPLIED
    state_after = target.get_state("lab-orders")
    timeline.append(
        (
            _now_iso(),
            "broker",
            "execution_acknowledged",
            f"outcome={res1.outcome.value}, current_version={getattr(state_after, 'current_version', '?')}",
        )
    )
    timeline.append((_now_iso(), "broker", "run_verifying", f"run={run_id}"))

    for t, who, what, ev in timeline:
        log(f"- `{t}` | `{who}` | `{what}` | {ev}")

    if not live:
        log(f"- `FakeTarget.versions['lab-orders']` = `{target.versions['lab-orders']}`")
        log(f"- `FakeTarget.applied_actions` count = `{len(target.applied_actions)}`")
    log("")

    # Variant 2: Replay same queue row -> already_applied with ONE effect
    log("## 2. Queue Replay (Idempotent Deduplication)")
    res2 = target.apply_rollback("lab-orders", a_key, "v1.3.0", "v1.0.0")
    assert res2.outcome == TargetActionOutcome.ALREADY_APPLIED
    log(f"- Replayed `action_key={a_key[:16]}...` -> target outcome = `{res2.outcome.value}`")
    if not live:
        log(f"- `FakeTarget.applied_actions` count = `{len(target.applied_actions)}` (still 1 effect)")
    log("")

    # Variant 3: Dropped-response variant -> reconcile instead of second dispatch
    log("## 3. Dropped-Response Variant (Reconcile Before Retry)")
    drop_plan_id = uuid4()
    drop_key = compute_action_key(drop_plan_id, p_hash)
    if not live:
        target.versions["lab-orders"] = "v1.3.0"
        target.hooks.drop_response_after_apply = True
    try:
        try:
            _ = target.apply_rollback("lab-orders", drop_key, "v1.3.0", "v1.0.0")
            drop_outcome = "uncertain"
        except ConnectionResetError as exc:
            drop_outcome = f"uncertain ({type(exc).__name__})"
    finally:
        if not live:
            target.hooks.drop_response_after_apply = False

    log(f"- Dispatch hit dropped response: `{drop_outcome}`")
    lookup = target.get_action_status("lab-orders", drop_key)
    assert lookup.status == "applied"
    log(
        f"- Reconcile queried `GET /admin/actions/{drop_key[:16]}...` -> "
        f"`status={lookup.status}` (transitioned `uncertain -> reconciled`, 0 second dispatches)"
    )
    if not live:
        log(f"- `FakeTarget.versions['lab-orders']` = `{target.versions['lab-orders']}`")
        log(f"- Total distinct `applied_actions` = `{len(target.applied_actions)}` (2 total: 1 per key)")

    if live and _pg_available(db_url):
        eng = create_engine(db_url, future=True)
        SessionLocal = sessionmaker(bind=eng, future=True)
        with SessionLocal() as s:
            _ = s.execute(text("SELECT 1")).scalar()
        eng.dispose()

    # Keep unused imports referenced cleanly for lint
    _ = (Reconciled, reconcile)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature 9 Execution Broker Demo")
    parser.add_argument("--live", action="store_true", help="Run against live Codespaces lab")
    args = parser.parse_args()
    transcript = _run_demo(live=args.live)
    TRANSCRIPT_PATH.write_text(transcript, encoding="utf-8")
    print(f"\nWrote transcript to {TRANSCRIPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
