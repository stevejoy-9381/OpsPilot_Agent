"""Real-PostgreSQL integration tests for ApprovalService.decide() (Feature 8).

Runs against live PostgreSQL when DATABASE_URL / TEST_DB_URL is reachable;
skips cleanly in offline environments without a running Postgres instance.
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.auth.dependencies import Principal
from apps.api.domain.errors import (
    AlreadyDecidedError,
    ConcurrentModificationError,
    PlanHashMismatchError,
)
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalModel,
    IncidentModel,
    RunModel,
    TenantModel,
    UserModel,
)
from apps.api.services.approval_service import ApprovalService
from shared.actions import ActionType
from shared.plans import PlanStatus, plan_hash
from shared.targets import TargetState, TargetStateError, TargetStateErrorCode

PG_URL = os.environ.get(
    "TEST_DB_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:5432/opspilot",
    ),
)
if PG_URL.startswith("postgresql://"):
    PG_URL = "postgresql+psycopg://" + PG_URL[len("postgresql://") :]


def _pg_available() -> bool:
    try:
        eng = create_engine(PG_URL, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")


class _StubTargetReader:
    def __init__(self, version: str = "v1.1.0", fail: bool = False) -> None:
        self.version = version
        self.fail = fail

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        if self.fail:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=target_id,
                message="target unreachable",
            )
        return TargetState(
            target_id=target_id,
            current_version=self.version,
            previous_version="v1.0.0",
            source="pg_test",
            read_at=as_of,
        )


def _seed_pg_plan(
    session: Session, *, ttl_seconds: int = 900
) -> tuple[UUID, UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    user_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    session.add(TenantModel(id=tenant_id, name="PG Test", slug=f"pg-{tenant_id.hex[:8]}"))
    session.add(
        UserModel(
            id=user_id,
            tenant_id=tenant_id,
            email=f"rev-{user_id.hex[:6]}@example.com",
            role="reviewer",
            password_hash="not-used",
        )
    )
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_pg",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={"service": "orders"},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="needs_review",
            version=1,
            remaining_calls=6,
            max_calls=8,
        )
    )
    phash = plan_hash(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        target_id="lab-orders",
        expected_version="v1.1.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=["ev_1"],
        expires_at=expires_at,
    )
    session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="orders",
            target_id="lab-orders",
            expected_version="v1.1.0",
            rollback_to_version="v1.0.0",
            rationale_evidence_ids=["ev_1"],
            rollback_route="Re-deploy orders v1.1.0",
            plan_hash=phash,
            policy_version="v1",
            status=PlanStatus.PROPOSED.value,
            notes="pool leak",
            expires_at=expires_at,
        )
    )
    session.commit()
    return tenant_id, user_id, run_id, plan_id, phash


def test_pg_tampered_plan_detected_by_recompute() -> None:
    eng = create_engine(PG_URL)
    SessionFactory = sessionmaker(bind=eng, expire_on_commit=False)
    with SessionFactory() as session:
        tenant_id, user_id, _, plan_id, phash = _seed_pg_plan(session)
        # Privileged tamper: disable immutability trigger, mutate service, re-enable trigger
        session.execute(text("ALTER TABLE app.action_plans DISABLE TRIGGER trg_action_plans_enforce_immutability"))
        session.execute(
            text("UPDATE app.action_plans SET service = 'inventory' WHERE id = :pid"),
            {"pid": str(plan_id)},
        )
        session.execute(text("ALTER TABLE app.action_plans ENABLE TRIGGER trg_action_plans_enforce_immutability"))
        session.commit()

        principal = Principal(tenant_id=tenant_id, user_id=user_id, role="reviewer")
        svc = ApprovalService(session, target_reader=_StubTargetReader("v1.1.0"))
        with pytest.raises(PlanHashMismatchError):
            svc.decide(
                principal=principal,
                plan_id=plan_id,
                decision="approve",
                submitted_plan_hash=phash,
                reason="tamper test",
                run_version=1,
                idempotency_key="pg-tamper",
            )
    eng.dispose()


def test_pg_concurrent_same_reviewer_and_different_reviewers_race() -> None:
    eng = create_engine(PG_URL, pool_size=10)
    SessionFactory = sessionmaker(bind=eng, expire_on_commit=False)
    with SessionFactory() as session:
        tenant_id, user1_id, run_id, plan_id, phash = _seed_pg_plan(session)
        user2_id = uuid4()
        session.add(
            UserModel(
                id=user2_id,
                tenant_id=tenant_id,
                email=f"rev2-{user2_id.hex[:6]}@example.com",
                role="reviewer",
                password_hash="not-used",
            )
        )
        session.commit()

    def _submit(uid: UUID, key: str) -> str:
        with SessionFactory() as s:
            svc = ApprovalService(s, target_reader=_StubTargetReader("v1.1.0"))
            try:
                res = svc.decide(
                    principal=Principal(tenant_id=tenant_id, user_id=uid, role="reviewer"),
                    plan_id=plan_id,
                    decision="approve",
                    submitted_plan_hash=phash,
                    reason="race",
                    run_version=1,
                    idempotency_key=key,
                )
                return f"ok:{res.idempotent_replay}"
            except (AlreadyDecidedError, ConcurrentModificationError) as exc:
                return f"blocked:{type(exc).__name__}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(_submit, user1_id, "same-1"),
            pool.submit(_submit, user1_id, "same-2"),
            pool.submit(_submit, user2_id, "other-1"),
        ]
        results = [f.result() for f in futs]

    assert results.count("ok:False") == 1
    with SessionFactory() as s:
        assert s.query(ApprovalModel).filter_by(plan_id=plan_id).count() == 1
        assert s.query(ActionExecutionModel).filter_by(plan_id=plan_id).count() == 1
    eng.dispose()
