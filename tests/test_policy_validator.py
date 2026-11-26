"""Comprehensive table-driven and isolation tests for agent/policy/validator.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent.graph.gate import GateResult, Outcome
from agent.policy import (
    PolicyContext,
    PolicyTable,
    PolicyTableEntry,
    RecentExecution,
    load_policy_table,
    validate,
)
from evals.graders.models import Diagnosis, EvidenceItem
from shared.actions import ActionType
from shared.plans import ActionPlan, PlanStatus, ProposalInput
from shared.targets import TargetState, TargetStateError, TargetStateErrorCode

REPO_ROOT = Path(__file__).resolve().parent.parent
W_START = datetime(2026, 11, 25, 12, 40, 0, tzinfo=timezone.utc)
W_END = datetime(2026, 11, 25, 12, 50, 0, tzinfo=timezone.utc)
DB_NOW = datetime(2026, 11, 25, 12, 50, 5, tzinfo=timezone.utc)


class _StubTargetReader:
    def __init__(self, result: TargetState | TargetStateError | Exception) -> None:
        self._result = result

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _default_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            evidence_id="ev_logs_01",
            timestamp=W_START + timedelta(minutes=2),
            source="search_logs",
            data={"msg": "pool exhausted"},
        ),
        EvidenceItem(
            evidence_id="ev_deploys_01",
            timestamp=W_START + timedelta(minutes=1),
            source="list_deploys",
            data={"deploys": [{"deploy_id": "dep_orders_v130", "new_version": "v1.3.0"}]},
        ),
    ]


def _default_diagnosis(**overrides: Any) -> Diagnosis:
    data: dict[str, Any] = {
        "root_cause_service": "orders",
        "root_cause_category": "bad_deploy",
        "mechanism": "connection_pool_exhaustion",
        "culprit_deploy_id": "dep_orders_v130",
        "evidence_ids": ["ev_logs_01", "ev_deploys_01"],
        "confidence": 0.90,
        "outcome_kind": "diagnosed",
        "outcome_reason": "concluded_by_model",
    }
    data.update(overrides)
    return Diagnosis(**data)


def _default_state(**overrides: Any) -> TargetState:
    data: dict[str, Any] = {
        "target_id": "lab-orders",
        "current_version": "v1.3.0",
        "previous_version": "v1.0.0",
        "last_action_at": None,
        "source": "snapshot",
        "read_at": W_END,
    }
    data.update(overrides)
    return TargetState(**data)


def _make_ctx(**overrides: Any) -> PolicyContext:
    diag = overrides.pop("diagnosis", _default_diagnosis())
    state = overrides.pop("target_state", _default_state())
    reader = overrides.pop("target_reader", _StubTargetReader(state))
    data: dict[str, Any] = {
        "tenant_id": "tenant-default",
        "run_id": "run_01",
        "diagnosis": diag,
        "outcome": Outcome(
            kind="diagnosed",
            reason="concluded_by_model",
            confidence_cap=0.95,
            confidence_label="high",
        ),
        "gate_result": GateResult(passed=True),
        "window_start": W_START,
        "window_end": W_END,
        "as_of": W_END,
        "evidence_items": _default_evidence(),
        "target_reader": reader,
        "db_clock_fn": lambda: DB_NOW,
    }
    data.update(overrides)
    return PolicyContext(**data)


# ---------------------------------------------------------------------------
# 1. Policy table coverage & loader tests
# ---------------------------------------------------------------------------


def test_policy_table_covers_every_category_and_mechanism_exactly_once() -> None:
    table = load_policy_table()
    assert table.allowed_actions_for("bad_deploy", "connection_pool_exhaustion") == (
        ActionType.ROLLBACK_SERVICE,
    )
    assert table.allowed_actions_for("bad_deploy", "cache_bypass_latency") == (
        ActionType.ROLLBACK_SERVICE,
    )
    assert table.allowed_actions_for("dependency_failure", "dependency_timeout") == (
        ActionType.RESTART_SERVICE,
    )
    assert table.allowed_actions_for("false_alarm", "none") == ()
    assert table.allowed_actions_for("unknown", "unknown") == ()

    with pytest.raises(ValueError, match="missing diagnosis"):
        PolicyTable.from_entries(
            version="1.0",
            entries=[
                PolicyTableEntry(
                    category="bad_deploy",
                    mechanism="connection_pool_exhaustion",
                    allowed_actions=(ActionType.ROLLBACK_SERVICE,),
                )
            ],
        )


# ---------------------------------------------------------------------------
# 2. Table-driven rules matrix tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_name,proposal_kw,ctx_kw,expected_allowed,expected_codes",
    [
        (
            "valid_s1_rollback_orders",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {},
            True,
            set(),
        ),
        (
            "valid_s3_restart_inventory",
            {
                "action": ActionType.RESTART_SERVICE,
                "service": "inventory",
                "expected_version": "v1.0.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "diagnosis": _default_diagnosis(
                    root_cause_service="inventory",
                    root_cause_category="dependency_failure",
                    mechanism="dependency_timeout",
                    culprit_deploy_id=None,
                ),
                "target_state": _default_state(
                    target_id="lab-inventory",
                    current_version="v1.0.0",
                    previous_version=None,
                ),
            },
            True,
            set(),
        ),
        (
            "P1_unknown_diagnosis_rejected",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "diagnosis": _default_diagnosis(
                    root_cause_category="unknown",
                    mechanism="unknown",
                    outcome_kind="unknown",
                ),
                "outcome": Outcome(
                    kind="unknown",
                    reason="insufficient_evidence",
                    confidence_cap=0.30,
                    confidence_label="low",
                ),
            },
            False,
            {"diagnosis_required"},
        ),
        (
            "P1_escalate_outcome_rejected",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "outcome": Outcome(
                    kind="escalate",
                    reason="multiple_concurrent_causes",
                    confidence_cap=0.30,
                    confidence_label="low",
                ),
            },
            False,
            {"diagnosis_required"},
        ),
        (
            "P1_gate_failed_rejected",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {"gate_result": GateResult(passed=False, failures=["no_rival"])},
            False,
            {"diagnosis_required"},
        ),
        (
            "P2_wrong_action_for_bad_deploy",
            {
                "action": ActionType.RESTART_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {},
            False,
            {"action_consistent_with_diagnosis"},
        ),
        (
            "P3_wrong_service",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "gateway",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "target_state": _default_state(
                    target_id="lab-gateway",
                    current_version="v1.3.0",
                    previous_version="v1.0.0",
                ),
            },
            False,
            {"service_matches_diagnosis"},
        ),
        (
            "P4_non_allowlisted_target_redis",
            {
                "action": ActionType.RESTART_SERVICE,
                "service": "redis",
                "expected_version": "v7.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "diagnosis": _default_diagnosis(
                    root_cause_service="inventory",
                    root_cause_category="dependency_failure",
                    mechanism="dependency_timeout",
                    culprit_deploy_id=None,
                ),
            },
            False,
            {"service_matches_diagnosis", "target_allowlisted"},
        ),
        (
            "P5_stale_expected_version",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.2.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {},
            False,
            {"precondition_current", "culprit_deploy_mismatch"},
        ),
        (
            "P6_no_previous_version_for_rollback",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "target_state": _default_state(previous_version=None),
            },
            False,
            {"rollback_possible"},
        ),
        (
            "P7_fabricated_rationale_id",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_fake_999"],
            },
            {},
            False,
            {"evidence_valid"},
        ),
        (
            "P7_out_of_window_rationale_id",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_old_01"],
            },
            {
                "diagnosis": _default_diagnosis(evidence_ids=["ev_old_01"]),
                "evidence_items": [
                    EvidenceItem(
                        evidence_id="ev_old_01",
                        timestamp=W_START - timedelta(hours=2),
                        source="search_logs",
                        data={},
                    )
                ],
            },
            False,
            {"evidence_valid"},
        ),
        (
            "P8_active_proposed_plan_supersedes",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "active_plan": ActionPlan(
                    plan_id="plan_prev",
                    tenant_id="tenant-default",
                    run_id="run_01",
                    action=ActionType.ROLLBACK_SERVICE,
                    service="orders",
                    target_id="lab-orders",
                    expected_version="v1.3.0",
                    rollback_to_version="v1.0.0",
                    rollback_route="Re-deploy orders v1.3.0",
                    rationale_evidence_ids=["ev_logs_01"],
                    status=PlanStatus.PROPOSED,
                    created_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                ),
            },
            True,
            set(),
        ),
        (
            "P8_approved_plan_blocks_new_proposal",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "active_plan": ActionPlan(
                    plan_id="plan_appr",
                    tenant_id="tenant-default",
                    run_id="run_01",
                    action=ActionType.ROLLBACK_SERVICE,
                    service="orders",
                    target_id="lab-orders",
                    expected_version="v1.3.0",
                    rollback_to_version="v1.0.0",
                    rollback_route="Re-deploy orders v1.3.0",
                    rationale_evidence_ids=["ev_logs_01"],
                    status=PlanStatus.APPROVED,
                    created_at=W_START,
                    expires_at=W_END + timedelta(minutes=10),
                ),
            },
            False,
            {"one_active_plan"},
        ),
        (
            "P9_flapping_guard_recent_execution",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "recent_executions": [
                    RecentExecution(
                        target_id="lab-orders",
                        status="reconciled",
                        executed_at=W_END - timedelta(minutes=3),
                    )
                ],
            },
            False,
            {"no_recent_action"},
        ),
        (
            "P10_multi_service_attempt",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders,inventory",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {},
            False,
            {"single_effect", "service_matches_diagnosis", "target_allowlisted"},
        ),
        (
            "fail_closed_reader_error",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "target_reader": _StubTargetReader(
                    TargetStateError(
                        code=TargetStateErrorCode.STATE_UNAVAILABLE,
                        target_id="lab-orders",
                        message="db down",
                    )
                ),
            },
            False,
            {"policy_unavailable"},
        ),
        (
            "fail_closed_reader_exception",
            {
                "action": ActionType.ROLLBACK_SERVICE,
                "service": "orders",
                "expected_version": "v1.3.0",
                "rationale_evidence_ids": ["ev_logs_01"],
            },
            {
                "target_reader": _StubTargetReader(RuntimeError("unexpected crash")),
            },
            False,
            {"policy_unavailable"},
        ),
    ],
)
def test_policy_validator_matrix(
    case_name: str,
    proposal_kw: dict[str, Any],
    ctx_kw: dict[str, Any],
    expected_allowed: bool,
    expected_codes: set[str],
) -> None:
    proposal = ProposalInput(**proposal_kw)
    ctx = _make_ctx(**ctx_kw)
    res = validate(proposal, ctx)

    assert res.allowed is expected_allowed, f"{case_name}: {res.violations}"
    assert res.policy_version and res.policy_version != "unknown"
    actual_codes = {v.code for v in res.violations}
    assert actual_codes == expected_codes, f"{case_name}: got {actual_codes}, expected {expected_codes}"

    if expected_allowed:
        assert res.derived is not None
        assert len(res.derived.plan_hash) == 64
        assert res.derived.expires_at == DB_NOW + timedelta(seconds=900)
        if ctx.active_plan is not None:
            assert res.derived.supersedes_plan_id == ctx.active_plan.plan_id
    else:
        assert res.derived is None


def test_model_cannot_supply_tenant_id_or_expires_at() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProposalInput(
            action=ActionType.ROLLBACK_SERVICE,
            service="orders",
            expected_version="v1.3.0",
            rationale_evidence_ids=["ev_logs_01"],
            tenant_id="tenant-evil",  # type: ignore[call-arg]
            expires_at="2099-01-01T00:00:00Z",  # type: ignore[call-arg]
        )


def test_validator_never_imports_scenarios_vault_controller_or_ground_truth() -> None:
    for path in (REPO_ROOT / "agent" / "policy").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for forbidden in ("lab.scenarios", "lab/scenarios", "lab_vault", "vault", "controller", "ground_truth"):
            assert forbidden not in src, f"Forbidden reference '{forbidden}' in {path}"
