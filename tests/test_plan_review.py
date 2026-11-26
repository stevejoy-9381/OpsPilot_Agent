"""Tests for GET /plans/{id}/review and build_plan_review_payload."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from apps.api.auth.dependencies import Principal, get_current_principal, get_db_session
from apps.api.main import app
from apps.api.repositories.plan_repository import PlanRepository
from apps.api.services.plan_review import build_plan_review_payload
from tools.common.sanitize import redact

NOW = datetime(2026, 11, 25, 14, 0, 0, tzinfo=timezone.utc)
W_START = datetime(2026, 11, 25, 13, 50, 0, tzinfo=timezone.utc)
W_END = datetime(2026, 11, 25, 14, 0, 0, tzinfo=timezone.utc)


def _sample_rows(
    *,
    tenant_id: UUID,
    run_id: UUID,
    plan_id: UUID,
    service: str = "orders",
    action: str = "rollback_service",
    category: str = "bad_deploy",
    mechanism: str = "connection_pool_exhaustion",
    current_ver: str = "v1.3.0",
    target_ver: str | None = "v1.0.0",
    note: str = "Rollback orders",
    wwc: str = "If pool wait drops without rollback",
) -> tuple[Any, Any, list[Any], list[Any]]:
    plan = SimpleNamespace(
        id=plan_id,
        run_id=run_id,
        tenant_id=tenant_id,
        action=action,
        service=service,
        target_id=f"lab-{service}",
        expected_version=current_ver,
        rollback_to_version=target_ver,
        rationale_evidence_ids=["ev_logs_01", "ev_deploys_01"],
        rollback_route=f"Re-deploy {service} {current_ver} (new proposal required)"
        if action == "rollback_service"
        else f"None — idempotent client-state reset on {service}",
        plan_hash="a" * 64,
        policy_version="pol_v1",
        status="proposed",
        notes=note,
        expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
    )
    diag = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        root_cause_service=service,
        root_cause_category=category,
        mechanism=mechanism,
        culprit_deploy_id="dep_orders_v130" if category == "bad_deploy" else None,
        evidence_ids=["ev_logs_01", "ev_deploys_01"],
        confidence=0.90,
        capped_confidence=0.90,
        model_confidence="high",
        outcome_kind="diagnosed",
        outcome_reason="concluded_by_model",
        what_would_change=wwc,
        ledger_summary=None,
    )
    observations = [
        SimpleNamespace(
            evidence_id="ev_logs_01",
            run_id=run_id,
            tenant_id=tenant_id,
            tool="search_logs",
            source_id=f"logs:{service}",
            captured_at=W_END,
            window_start=W_START,
            window_end=W_END,
            payload={
                "total_found": 42,
                "matched_count": 42,
                "lines": ["RAW_LOG_LINE_MUST_NOT_APPEAR_IN_REVIEW"],
            },
        ),
        SimpleNamespace(
            evidence_id="ev_deploys_01",
            run_id=run_id,
            tenant_id=tenant_id,
            tool="list_deploys",
            source_id=f"deploys:{service}",
            captured_at=W_END,
            window_start=W_START,
            window_end=W_END,
            payload={
                "total_found": 1,
                "deploys": [{"deploy_id": "dep_orders_v130", "new_version": current_ver}],
                "diff_text": "RAW_DIFF_TEXT_MUST_NOT_APPEAR_IN_REVIEW",
            },
        ),
    ]
    hypotheses = [
        SimpleNamespace(
            id="hyp_01",
            category=category,
            mechanism=mechanism,
            service=service,
            status="supported",
            contrary_evidence_ids=[],
        ),
        SimpleNamespace(
            id="hyp_02",
            category="dependency_failure" if category == "bad_deploy" else "bad_deploy",
            mechanism="dependency_timeout" if category == "bad_deploy" else "connection_pool_exhaustion",
            service="inventory" if service == "orders" else "orders",
            status="refuted",
            contrary_evidence_ids=["ev_deploys_01"],
        ),
    ]
    return plan, diag, observations, hypotheses


def test_review_endpoint_matches_db_rebuild_is_deterministic_and_excludes_raw_text(
    monkeypatch: Any,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    run_id, plan_id = uuid4(), uuid4()
    plan, diag, obs, hyps = _sample_rows(
        tenant_id=tenant_a,
        run_id=run_id,
        plan_id=plan_id,
        note="Bearer sk_test_1234567890abcdef1234567890abcdef",
        wwc="If token=ghp_1234567890abcdef1234567890abcdef123456 is rotated",
    )

    def _fake_get_review_data(self: PlanRepository, ctx: Any, pid: UUID) -> Any:
        if ctx.tenant_id == tenant_a and pid == plan_id:
            return plan, diag, obs, hyps
        from apps.api.domain.errors import NotFoundError

        raise NotFoundError("not found")

    monkeypatch.setattr(PlanRepository, "get_review_data", _fake_get_review_data)
    app.dependency_overrides[get_db_session] = lambda: iter([None])
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid4(), tenant_id=tenant_a, role="viewer"
    )
    try:
        client = TestClient(app)
        resp1 = client.get(f"/plans/{plan_id}/review")
        resp2 = client.get(f"/plans/{plan_id}/review")
        assert resp1.status_code == 200
        assert resp1.json() == resp2.json()  # deterministic

        expected = build_plan_review_payload(
            plan=plan, diagnosis=diag, observations=obs, hypotheses=hyps
        ).model_dump(mode="json")
        assert resp1.json() == expected

        raw_json = json.dumps(resp1.json())
        # No raw log text or diff text
        assert "RAW_LOG_LINE_MUST_NOT_APPEAR_IN_REVIEW" not in raw_json
        assert "RAW_DIFF_TEXT_MUST_NOT_APPEAR_IN_REVIEW" not in raw_json
        # Untrusted text is labelled and secrets are masked by Feature 2.3 redact()
        assert resp1.json()["model_note"]["label"] == "untrusted model text"
        assert resp1.json()["what_would_change"]["label"] == "untrusted model text"
        assert "sk_test_" not in raw_json
        assert "ghp_" not in raw_json
        payload_without_hash = json.dumps(
            {k: v for k, v in resp1.json().items() if k != "plan_hash"}
        )
        assert redact(payload_without_hash) == payload_without_hash  # zero unmasked secrets remain

        # Evidence numeric summaries & URLs present
        assert resp1.json()["evidence"][0]["numeric_summary"]["total_found"] == 42
        assert resp1.json()["evidence"][0]["evidence_url"] == f"/runs/{run_id}/evidence/ev_logs_01"
        assert len(resp1.json()["policy_results"]["checks_passed"]) == 10

        # Cross-tenant returns 404
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            user_id=uuid4(), tenant_id=tenant_b, role="reviewer"
        )
        assert client.get(f"/plans/{plan_id}/review").status_code == 404
    finally:
        app.dependency_overrides.clear()
