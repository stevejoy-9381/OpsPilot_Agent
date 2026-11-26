"""Tests for agent/prompts/proposal_v1.md, agent/graph/nodes/propose.py, and graph wiring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.graph.context import LeakageDetectedError, scan_for_leakage
from agent.graph.graph import build_investigator_graph, route_after_finalize
from agent.graph.nodes.propose import (
    ProposalDecision,
    build_proposal_context,
    load_proposal_prompt,
    propose_node,
)
from agent.graph.state import create_initial_state
from agent.llm.interface import (
    BudgetExceededError,
    LLMClient,
    LLMError,
    Message,
    StructuredResult,
    TokenUsage,
    ToolCallResult,
)
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import ConcurrentModificationError
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis
from shared.actions import ActionType
from shared.plans import ActionPlan
from shared.targets import TargetState

W_START = datetime(2026, 11, 25, 12, 40, 0, tzinfo=timezone.utc)
W_END = datetime(2026, 11, 25, 12, 50, 0, tzinfo=timezone.utc)
DB_NOW = datetime.now(timezone.utc)


class _FakeLLMClient(LLMClient):
    def __init__(self, responses: list[StructuredResult[ProposalDecision] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    def generate_structured(
        self,
        messages: list[Message],
        response_schema: type[Any],
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        timeout_s: float = 30.0,
    ) -> StructuredResult[Any]:
        self.calls.append(list(messages))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def generate_with_tools(self, *args: Any, **kwargs: Any) -> ToolCallResult:
        raise NotImplementedError


class _FakeTargetReader:
    def __init__(self, state: TargetState) -> None:
        self._state = state

    def current_state(self, target_id: str, as_of: datetime) -> TargetState:
        return self._state


class _FakeRun:
    def __init__(self, run_id: UUID, tenant_id: UUID, status: str = "investigating") -> None:
        self.id = run_id
        self.tenant_id = tenant_id
        self.status = status
        self.version = 1
        self.last_error: dict[str, Any] | None = None


class _FakeObs:
    def __init__(self, evidence_id: str, run_id: UUID, tool: str = "search_logs") -> None:
        self.evidence_id = evidence_id
        self.run_id = run_id
        self.tool = tool
        self.summary = f"Observed {tool} output"
        self.window_start = W_START
        self.window_end = W_END
        self.payload: dict[str, Any] = {
            "deploys": [{"deploy_id": "dep_orders_v130", "new_version": "v1.3.0"}]
        }


class _FakeRunRepo:
    def __init__(self, run: _FakeRun, obs: list[_FakeObs]) -> None:
        self.run = run
        self.obs = obs

    def get_by_id(self, ctx: TenantContext, run_id: UUID) -> _FakeRun:
        return self.run

    def list_observations(self, ctx: TenantContext, run_id: UUID) -> list[_FakeObs]:
        return self.obs

    def record_error(self, ctx: TenantContext, run_id: UUID, error: dict[str, Any]) -> None:
        self.run.last_error = error


class _FakeEvent:
    def __init__(self, seq: int, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        self.seq = seq
        self.event_type = event_type
        self.actor = actor
        self.payload = payload


class _FakeRunService:
    def __init__(self, run: _FakeRun, obs: list[_FakeObs]) -> None:
        self.run_repo = _FakeRunRepo(run, obs)
        self.events: list[_FakeEvent] = []

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        resolution: str | None = None,
    ) -> tuple[_FakeRun, _FakeEvent]:
        if self.run_repo.run.version != expected_version:
            raise ConcurrentModificationError(str(run_id), expected_version, self.run_repo.run.version)
        self.run_repo.run.status = to_status
        self.run_repo.run.version += 1
        ev = self.append_event(
            ctx, run_id, "status_transition", actor, {"to_status": to_status, **(payload or {})}
        )
        return self.run_repo.run, ev

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> _FakeEvent:
        ev = _FakeEvent(len(self.events) + 1, event_type, actor, payload or {})
        self.events.append(ev)
        return ev


def _make_state(run_id: UUID, tenant_id: UUID) -> dict[str, Any]:
    alert = AlertEvent(
        alert_id="alt_01",
        fired_at=W_START,
        service="gateway",
        rule_id="rule_err",
        metric="error_rate",
        threshold=0.05,
        observed_value=0.25,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )
    st = dict(
        create_initial_state(
            run_id=str(run_id),
            tenant_id=str(tenant_id),
            incident_id=str(uuid4()),
            alert=alert,
            schema_version="v2",
        )
    )
    st["window_start"] = W_START.isoformat()
    st["window_end"] = W_END.isoformat()
    st["evidence_ids"] = ["ev_logs_01", "ev_deploys_01"]
    st["draft_diagnosis"] = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_logs_01", "ev_deploys_01"],
        confidence=0.90,
        outcome_kind="diagnosed",
        outcome_reason="concluded_by_model",
        ledger_summary={"leading_id": "hyp_01", "refuted_rivals": ["hyp_02"]},
    ).model_dump()
    return st


def _ok_result(decision: ProposalDecision, tokens: int = 120) -> StructuredResult[ProposalDecision]:
    return StructuredResult(
        parsed=decision,
        token_usage=TokenUsage(input_tokens=80, output_tokens=40, total_tokens=tokens),
        model="fake-model",
    )


def test_proposal_prompt_hash_and_zero_leakage() -> None:
    prompt = load_proposal_prompt()
    assert scan_for_leakage(prompt) == []

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_logs_01"],
        confidence=0.90,
    )
    target_state = TargetState(
        target_id="lab-orders",
        current_version="v1.3.0",
        previous_version="v1.0.0",
        last_action_at=None,
        source="snapshot",
        read_at=W_END,
    )
    msgs = build_proposal_context(
        diagnosis=diag,
        ledger_summary={"leading_id": "hyp_01"},
        evidence_digest=[{"evidence_id": "ev_logs_01", "tool": "search_logs", "summary": "timeout"}],
        target_state=target_state,
        allowed_actions=(ActionType.ROLLBACK_SERVICE,),
        remaining_attempts=3,
    )
    assert len(msgs) == 2
    assert scan_for_leakage(msgs[1].content) == []

    # Injecting forbidden term raises LeakageDetectedError
    with pytest.raises(LeakageDetectedError):
        build_proposal_context(
            diagnosis=diag,
            ledger_summary={"secret": "S1 pool_leak"},
            evidence_digest=[],
            target_state=target_state,
            allowed_actions=(ActionType.ROLLBACK_SERVICE,),
            remaining_attempts=3,
        )


def test_propose_valid_first_try() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    run_svc = _FakeRunService(_FakeRun(run_id, tenant_id), [_FakeObs("ev_logs_01", run_id), _FakeObs("ev_deploys_01", run_id, "list_deploys")])
    persisted: list[ActionPlan] = []
    llm = _FakeLLMClient(
        [
            _ok_result(
                ProposalDecision(
                    decision="propose",
                    action=ActionType.ROLLBACK_SERVICE,
                    service="orders",
                    expected_version="v1.3.0",
                    rationale_evidence_ids=["ev_logs_01"],
                    note="SECRET_NOTE_TEXT_SHOULD_NOT_BE_IN_EVENT",
                )
            )
        ]
    )
    reader = _FakeTargetReader(
        TargetState(
            target_id="lab-orders",
            current_version="v1.3.0",
            previous_version="v1.0.0",
            source="snapshot",
            read_at=W_END,
        )
    )
    out = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        plan_persister=lambda _ctx, p, _sup: persisted.append(p),
        db_clock_fn=lambda: DB_NOW,
    )
    assert out["status"] == "needs_review"
    assert out["total_tokens_consumed"] == 120
    assert len(persisted) == 1
    assert persisted[0].rollback_to_version == "v1.0.0"

    attempt_events = [e for e in run_svc.events if e.event_type == "proposal_attempt"]
    assert len(attempt_events) == 1
    assert attempt_events[0].payload["allowed"] is True
    assert attempt_events[0].payload["violation_codes"] == []
    assert "SECRET_NOTE_TEXT_SHOULD_NOT_BE_IN_EVENT" not in str(attempt_events[0].payload)


def test_propose_invalid_then_repaired() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    run_svc = _FakeRunService(_FakeRun(run_id, tenant_id), [_FakeObs("ev_logs_01", run_id), _FakeObs("ev_deploys_01", run_id, "list_deploys")])
    persisted: list[ActionPlan] = []
    llm = _FakeLLMClient(
        [
            # Attempt 1: wrong action + stale version
            _ok_result(
                ProposalDecision(
                    decision="propose",
                    action=ActionType.RESTART_SERVICE,
                    service="orders",
                    expected_version="v1.2.0",
                    rationale_evidence_ids=["ev_logs_01"],
                    note="wrong first try",
                ),
                tokens=100,
            ),
            # Attempt 2: repaired
            _ok_result(
                ProposalDecision(
                    decision="propose",
                    action=ActionType.ROLLBACK_SERVICE,
                    service="orders",
                    expected_version="v1.3.0",
                    rationale_evidence_ids=["ev_logs_01"],
                    note="repaired rollback",
                ),
                tokens=110,
            ),
        ]
    )
    reader = _FakeTargetReader(
        TargetState(
            target_id="lab-orders",
            current_version="v1.3.0",
            previous_version="v1.0.0",
            source="snapshot",
            read_at=W_END,
        )
    )
    out = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        plan_persister=lambda _ctx, p, _sup: persisted.append(p),
        db_clock_fn=lambda: DB_NOW,
    )
    assert out["status"] == "needs_review"
    assert out["total_tokens_consumed"] == 210
    assert len(persisted) == 1

    attempt_events = [e for e in run_svc.events if e.event_type == "proposal_attempt"]
    assert len(attempt_events) == 2
    assert attempt_events[0].payload["allowed"] is False
    assert set(attempt_events[0].payload["violation_codes"]) == {
        "action_consistent_with_diagnosis",
        "precondition_current",
    }
    assert attempt_events[1].payload["allowed"] is True
    assert attempt_events[1].payload["violation_codes"] == []


def test_propose_three_failures_escalate_policy_violation() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    run_svc = _FakeRunService(_FakeRun(run_id, tenant_id), [_FakeObs("ev_logs_01", run_id)])
    persisted: list[ActionPlan] = []
    bad = _ok_result(
        ProposalDecision(
            decision="propose",
            action=ActionType.RESTART_SERVICE,
            service="orders",
            expected_version="v1.3.0",
            rationale_evidence_ids=["ev_logs_01"],
        )
    )
    llm = _FakeLLMClient([bad, bad, bad])
    reader = _FakeTargetReader(
        TargetState(
            target_id="lab-orders",
            current_version="v1.3.0",
            previous_version="v1.0.0",
            source="snapshot",
            read_at=W_END,
        )
    )
    out = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        plan_persister=lambda _ctx, p, _sup: persisted.append(p),
        db_clock_fn=lambda: DB_NOW,
    )
    assert out["status"] == "escalated"
    assert out["termination_reason"] == "policy_violation"
    assert len(persisted) == 0
    assert len([e for e in run_svc.events if e.event_type == "proposal_attempt"]) == 3


def test_propose_decline_escalates_model_declined_and_no_plan() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    run_svc = _FakeRunService(_FakeRun(run_id, tenant_id), [_FakeObs("ev_logs_01", run_id)])
    persisted: list[ActionPlan] = []
    llm = _FakeLLMClient(
        [
            _ok_result(
                ProposalDecision(
                    decision="decline",
                    decline_reason="No safe rollback path available",
                )
            )
        ]
    )
    reader = _FakeTargetReader(
        TargetState(
            target_id="lab-orders",
            current_version="v1.3.0",
            previous_version="v1.0.0",
            source="snapshot",
            read_at=W_END,
        )
    )
    out = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        plan_persister=lambda _ctx, p, _sup: persisted.append(p),
        db_clock_fn=lambda: DB_NOW,
    )
    assert out["status"] == "escalated"
    assert out["termination_reason"] == "model_declined"
    assert len(persisted) == 0


def test_propose_provider_and_budget_errors() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    run_svc = _FakeRunService(_FakeRun(run_id, tenant_id), [_FakeObs("ev_logs_01", run_id)])
    reader = _FakeTargetReader(
        TargetState(
            target_id="lab-orders",
            current_version="v1.3.0",
            previous_version="v1.0.0",
            source="snapshot",
            read_at=W_END,
        )
    )

    llm_err = _FakeLLMClient(
        [StructuredResult(error=LLMError(error_type="provider_error", message="429"))]
    )
    out_err = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm_err,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        db_clock_fn=lambda: DB_NOW,
    )
    assert out_err["status"] == "escalated"
    assert out_err["termination_reason"] == "provider_error_exhausted"

    llm_budget = _FakeLLMClient([BudgetExceededError(current_usage=100, limit=100, requested=50)])
    out_budget = propose_node(
        state=_make_state(run_id, tenant_id),  # type: ignore[arg-type]
        llm_client=llm_budget,
        run_service=run_svc,  # type: ignore[arg-type]
        target_reader=reader,
        db_clock_fn=lambda: DB_NOW,
    )
    assert out_budget["status"] == "escalated"
    assert out_budget["termination_reason"] == "token_budget_exceeded"


def test_graph_routes_v2_diagnosed_to_propose_node() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    st = _make_state(run_id, tenant_id)
    assert route_after_finalize(st) == "propose"  # type: ignore[arg-type]

    # When handoff_result already populated (e.g., legacy stub), routes to end
    st_with_handoff = dict(st)
    st_with_handoff["handoff_result"] = {"target_status": "escalated"}
    assert route_after_finalize(st_with_handoff) == "end"  # type: ignore[arg-type]

    # Graph compiles cleanly with propose node
    graph = build_investigator_graph(
        llm_client=_FakeLLMClient([]),
        target_reader=_FakeTargetReader(
            TargetState(
                target_id="lab-orders",
                current_version="v1.3.0",
                previous_version="v1.0.0",
                source="snapshot",
                read_at=W_END,
            )
        ),
    )
    assert "propose" in graph.nodes
