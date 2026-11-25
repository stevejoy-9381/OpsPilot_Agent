"""tests/test_gate_and_outcomes.py

Deterministic tests for the Contrary-Evidence Gate, Confidence Caps,
and Outcome Resolution in Feature 6.

Test Coverage:
1. Each gate failure code: no_rival, no_discriminating_test, unsupported_citation,
   single_source_support, rival_not_linked.
2. Cosmetic rival detection (rival formulated but never tested or linked).
3. False alarm claims: with and without baseline return metrics evidence.
4. Table-driven test for confidence cap table (YAML data-driven).
5. Deterministic outcome resolver covering all canonical outcome reasons:
   - concluded_by_model
   - false_alarm_no_action
   - insufficient_evidence
   - contradictory_evidence
   - evidence_unavailable
   - budget_exhausted
   - gate_not_satisfied
   - multiple_concurrent_causes
6. Conclude node 2-retry state machine and forced downgrade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from agent.graph.gate import (
    GateFailureCode,
    GateResult,
    evaluate_confidence_cap,
    evaluate_gate,
    resolve_outcome,
)
from agent.graph.ledger import (
    HypothesisLedger,
    add_hypothesis,
    link_evidence,
    set_status,
)
from agent.graph.nodes.conclude import conclude_node
from agent.graph.state import create_initial_state
from baseline.alerts.models import AlertEvent
from evals.graders.models import EvidenceItem

# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def standard_alert(base_time: datetime) -> AlertEvent:
    return AlertEvent(
        alert_id="alert-123",
        service="orders",
        rule_id="HighLatencyAlert",
        metric="http_request_duration_seconds",
        fired_at=base_time,
        severity="CRITICAL",
        status="firing",
        threshold=1.5,
        observed_value=3.2,
        window_seconds=300,
    )


def make_evidence_store(base_time: datetime) -> list[EvidenceItem]:
    """Helper creating standard multi-source telemetry items inside the incident window."""
    return [
        EvidenceItem(
            evidence_id="ev_m1",
            timestamp=base_time - timedelta(seconds=60),
            source="prometheus",
            data={"metric_value": 3.4, "threshold": 1.5},
        ),
        EvidenceItem(
            evidence_id="ev_l1",
            timestamp=base_time,
            source="logs",
            data={"log_line": "Connection pool exhausted (max_connections=50 reached)"},
        ),
        EvidenceItem(
            evidence_id="ev_t1",
            timestamp=base_time + timedelta(seconds=30),
            source="traces",
            data={"span": "db_query", "duration_ms": 2500},
        ),
        EvidenceItem(
            evidence_id="ev_d1",
            timestamp=base_time - timedelta(seconds=120),
            source="deploys",
            data={"deploy_id": "dep-v2.1", "service": "orders"},
        ),
        EvidenceItem(
            evidence_id="ev_rival_log",
            timestamp=base_time,
            source="logs",
            data={"log_line": "Cache hit ratio normal at 98.5%"},
        ),
        EvidenceItem(
            evidence_id="ev_out_of_window",
            timestamp=base_time - timedelta(hours=5),
            source="logs",
            data={"log_line": "Historical log line"},
        ),
        EvidenceItem(
            evidence_id="ev_unauth_tool",
            timestamp=base_time,
            source="unknown_scanner",
            data={"scan": "unauthorized"},
        ),
        EvidenceItem(
            evidence_id="ev_false_alarm_baseline",
            timestamp=base_time + timedelta(seconds=400),
            source="prometheus",
            data={"metric_value": 0.45, "baseline": 0.50, "returned_to_baseline": True},
        ),
        EvidenceItem(
            evidence_id="ev_false_alarm_elevated",
            timestamp=base_time + timedelta(seconds=400),
            source="prometheus",
            data={"metric_value": 2.8, "baseline": 0.50, "returned_to_baseline": False},
        ),
    ]


# ---------------------------------------------------------------------------
# 1. Gate Failure Codes Tests
# ---------------------------------------------------------------------------


def test_gate_failure_no_rival(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Gate fails with 'no_rival' when only 1 hypothesis is created without false-alarm exception."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Connection pool exhausted",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
    )
    ledger, _ = link_evidence(ledger, h1.id, "ev_m1", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_l1", "supports")

    tool_trace = [
        {"tool": "search_logs", "tests_hypothesis_id": h1.id, "evidence_id": "ev_l1", "ok": True}
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.NO_RIVAL.value in result.failures


def test_gate_failure_no_discriminating_test(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Gate fails with 'no_discriminating_test' when tool calls do not link refuting/discriminating evidence."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Connection pool exhausted",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
    )
    ledger, h2 = add_hypothesis(
        ledger,
        statement="Cache bypass latency",
        category="dependency_failure",
        mechanism="cache_bypass_latency",
        service="inventory",
    )
    # Both hypotheses exist, but tool trace did not target or link refuting evidence
    ledger, _ = link_evidence(ledger, h1.id, "ev_m1", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_l1", "supports")

    # Call did not set tests_hypothesis_id
    tool_trace = [
        {"tool": "get_metrics", "evidence_id": "ev_m1", "ok": True},
        {"tool": "search_logs", "evidence_id": "ev_l1", "ok": True},
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.NO_DISCRIMINATING_TEST.value in result.failures


def test_gate_failure_cosmetic_rival_caught(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Gate fails with 'rival_not_linked' when a rival was formulated but never tested or linked."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Connection pool exhausted",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
    )
    ledger, h2 = add_hypothesis(
        ledger,
        statement="Cache bypass latency",
        category="dependency_failure",
        mechanism="cache_bypass_latency",
        service="inventory",
    )
    ledger, _ = link_evidence(ledger, h1.id, "ev_m1", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_l1", "supports")

    # Tool trace tested h1, but h2 was completely ignored
    tool_trace = [
        {"tool": "get_metrics", "tests_hypothesis_id": h1.id, "evidence_id": "ev_m1", "ok": True},
        {"tool": "search_logs", "tests_hypothesis_id": h1.id, "evidence_id": "ev_l1", "ok": True},
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.RIVAL_NOT_LINKED.value in result.failures
    assert h2.id in result.details.get("cosmetic_rivals", [])


def test_gate_failure_unsupported_citation(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Gate fails with 'unsupported_citation' for fabricated, out-of-window, or unauthorized IDs."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    ledger, h2 = add_hypothesis(ledger, statement="H2", category="dependency_failure", mechanism="cache_bypass_latency", service="inventory")

    # Link fabricated ID, out of window ID, and unauthorized tool ID
    ledger, _ = link_evidence(ledger, h1.id, "ev_fabricated_999", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_out_of_window", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_unauth_tool", "supports")
    ledger, _ = link_evidence(ledger, h2.id, "ev_rival_log", "contradicts")

    tool_trace = [
        {"tool": "search_logs", "tests_hypothesis_id": h2.id, "evidence_id": "ev_rival_log", "ok": True}
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.UNSUPPORTED_CITATION.value in result.failures
    assert "ev_fabricated_999" in result.details.get("unsupported_citations", [])
    assert "ev_out_of_window" in result.details.get("out_of_window_citations", [])
    assert "ev_unauth_tool" in result.details.get("unauthorized_tool_citations", [])


def test_gate_failure_single_source_support(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Gate fails with 'single_source_support' if leading hypothesis only has 1 source kind (metrics only)."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    ledger, h2 = add_hypothesis(ledger, statement="H2", category="dependency_failure", mechanism="cache_bypass_latency", service="inventory")

    # Only metrics linked
    ledger, _ = link_evidence(ledger, h1.id, "ev_m1", "supports")
    ledger, _ = link_evidence(ledger, h2.id, "ev_rival_log", "contradicts")

    tool_trace = [
        {"tool": "search_logs", "tests_hypothesis_id": h2.id, "evidence_id": "ev_rival_log", "ok": True}
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.SINGLE_SOURCE_SUPPORT.value in result.failures


# ---------------------------------------------------------------------------
# 2. Discriminating Test & Clean Gate Pass
# ---------------------------------------------------------------------------


def test_clean_gate_pass(standard_alert: AlertEvent, base_time: datetime) -> None:
    """All rules pass: dual hypotheses, discriminating test linked, multi-source proof, in window."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    ledger, h2 = add_hypothesis(ledger, statement="H2", category="dependency_failure", mechanism="cache_bypass_latency", service="inventory")

    # Multi-source support for H1 (metrics + logs + traces)
    ledger, _ = link_evidence(ledger, h1.id, "ev_m1", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_l1", "supports")
    ledger, _ = link_evidence(ledger, h1.id, "ev_t1", "supports")

    # Discriminating test targeting H2 with contrary evidence linked
    ledger, _ = link_evidence(ledger, h2.id, "ev_rival_log", "contradicts")
    ledger, _ = set_status(ledger, h2.id, "refuted")

    tool_trace = [
        {"tool": "get_metrics", "evidence_id": "ev_m1", "ok": True},
        {"tool": "search_logs", "evidence_id": "ev_l1", "ok": True},
        {"tool": "get_trace", "evidence_id": "ev_t1", "ok": True},
        {"tool": "search_logs", "tests_hypothesis_id": h2.id, "evidence_id": "ev_rival_log", "ok": True},
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert result.passed
    assert len(result.failures) == 0


# ---------------------------------------------------------------------------
# 3. False Alarm Path Tests
# ---------------------------------------------------------------------------


def test_false_alarm_with_baseline_return(standard_alert: AlertEvent, base_time: datetime) -> None:
    """False alarm succeeds when metrics show return to baseline post-alert."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Transient traffic spike returned to normal",
        category="false_alarm",
        mechanism="none",
        service=None,
    )
    ledger, _ = link_evidence(ledger, h1.id, "ev_false_alarm_baseline", "supports")

    tool_trace = [
        {"tool": "get_metrics", "evidence_id": "ev_false_alarm_baseline", "ok": True}
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert result.passed
    assert len(result.failures) == 0

    outcome = resolve_outcome(ledger, result, alert=standard_alert)
    assert outcome.kind == "diagnosed"
    assert outcome.reason == "false_alarm_no_action"
    assert outcome.confidence_cap == 0.90


def test_false_alarm_without_baseline_return(standard_alert: AlertEvent, base_time: datetime) -> None:
    """False alarm is rejected when metrics do not demonstrate baseline return."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Transient traffic spike claimed without proof",
        category="false_alarm",
        mechanism="none",
        service=None,
    )
    ledger, _ = link_evidence(ledger, h1.id, "ev_false_alarm_elevated", "supports")

    tool_trace = [
        {"tool": "get_metrics", "evidence_id": "ev_false_alarm_elevated", "ok": True}
    ]

    result = evaluate_gate(ledger, tool_trace, store, standard_alert)
    assert not result.passed
    assert GateFailureCode.NO_RIVAL.value in result.failures


# ---------------------------------------------------------------------------
# 4. Table-Driven Confidence Cap Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition_desc", "evidence_ids", "rival_tested", "has_tool_error", "has_contrary", "is_fa", "expected_cap", "expected_cat"),
    [
        ("Tool error during discriminating test", ["ev_m1", "ev_l1", "ev_t1"], True, True, False, False, 0.30, "low"),
        ("No rival tested or falsified", ["ev_m1", "ev_l1", "ev_t1"], False, False, False, False, 0.30, "low"),
        ("Unrefuted contrary evidence on leading", ["ev_m1", "ev_l1", "ev_t1"], True, False, True, False, 0.30, "low"),
        ("Single source kind only (metrics only)", ["ev_m1"], True, False, False, False, 0.60, "medium"),
        ("Gate satisfied but < 3 evidence items", ["ev_m1", "ev_l1"], True, False, False, False, 0.75, "medium"),
        ("Strong proof: >= 2 sources, rival refuted, 0 errors, >= 3 items", ["ev_m1", "ev_l1", "ev_t1"], True, False, False, False, 0.95, "high"),
        ("False alarm certified with baseline return", ["ev_false_alarm_baseline"], False, False, False, True, 0.90, "high"),
    ],
)
def test_confidence_cap_table_driven(
    condition_desc: str,
    evidence_ids: list[str],
    rival_tested: bool,
    has_tool_error: bool,
    has_contrary: bool,
    is_fa: bool,
    expected_cap: float,
    expected_cat: str,
    base_time: datetime,
) -> None:
    """Table-driven test validating deterministic confidence caps against the YAML specification."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    cat = "false_alarm" if is_fa else "bad_deploy"
    mech = "none" if is_fa else "connection_pool_exhaustion"
    ledger, h1 = add_hypothesis(ledger, statement="Leading", category=cat, mechanism=mech, service="orders")

    if not is_fa:
        ledger, h2 = add_hypothesis(ledger, statement="Rival", category="dependency_failure", mechanism="cache_bypass_latency", service="inventory")
        if rival_tested:
            ledger, _ = link_evidence(ledger, h2.id, "ev_rival_log", "contradicts")
            ledger, _ = set_status(ledger, h2.id, "refuted")

    for eid in evidence_ids:
        ledger, _ = link_evidence(ledger, h1.id, eid, "supports")

    if has_contrary:
        ledger, _ = link_evidence(ledger, h1.id, "ev_rival_log", "contradicts")

    tool_trace: list[dict[str, Any]] = []
    if rival_tested and not is_fa:
        tool_trace.append({"tool": "search_logs", "tests_hypothesis_id": h2.id, "evidence_id": "ev_rival_log", "ok": True})
    if has_tool_error:
        tool_trace.append({"tool": "get_trace", "tests_hypothesis_id": h1.id, "ok": False, "error_code": "timeout"})

    cap, label, _ = evaluate_confidence_cap(
        ledger=ledger,
        tool_trace=tool_trace,
        evidence_store=store,
        is_false_alarm=is_fa,
    )

    assert cap == pytest.approx(expected_cap, 0.01), f"Failed for {condition_desc}: cap {cap} != {expected_cap}"
    assert label == expected_cat, f"Failed for {condition_desc}: label {label} != {expected_cat}"


# ---------------------------------------------------------------------------
# 5. Outcome Resolver Tests (All Outcomes Reachable)
# ---------------------------------------------------------------------------


def test_outcome_multiple_concurrent_causes() -> None:
    """Two supported hypotheses on distinct services force escalate: multiple_concurrent_causes."""
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="Orders pool leak", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    ledger, h2 = add_hypothesis(ledger, statement="Inventory redis timeout", category="dependency_failure", mechanism="dependency_timeout", service="inventory")
    ledger, _ = set_status(ledger, h1.id, "supported")
    ledger, _ = set_status(ledger, h2.id, "supported")

    outcome = resolve_outcome(ledger=ledger, gate=GateResult(passed=True))
    assert outcome.kind == "escalate"
    assert outcome.reason == "multiple_concurrent_causes"


def test_outcome_evidence_unavailable() -> None:
    """Tool errors blocking telemetry force unknown: evidence_unavailable."""
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    outcome = resolve_outcome(
        ledger=ledger,
        gate=GateResult(passed=False, failures=["no_rival"]),
        tool_errors=[{"error": "Database query timed out"}],
    )
    assert outcome.kind == "unknown"
    assert outcome.reason == "evidence_unavailable"


def test_outcome_budget_exhausted() -> None:
    """Budget exhausted without gate passing forces unknown: budget_exhausted."""
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    outcome = resolve_outcome(
        ledger=ledger,
        gate=GateResult(passed=False, failures=["no_rival"]),
        budget_state={"remaining_calls": 0},
    )
    assert outcome.kind == "unknown"
    assert outcome.reason == "budget_exhausted"


def test_outcome_contradictory_evidence() -> None:
    """Unrefuted contrary evidence on leading hypothesis yields unknown: contradictory_evidence."""
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    ledger, _ = link_evidence(ledger, h1.id, "ev_contra", "contradicts")

    outcome = resolve_outcome(
        ledger=ledger,
        gate=GateResult(passed=False, failures=["no_rival"]),
        budget_state={"remaining_calls": 4},
    )
    assert outcome.kind == "unknown"
    assert outcome.reason == "contradictory_evidence"


def test_outcome_gate_not_satisfied_after_rejections() -> None:
    """Rejection retries exhausted forces unknown: gate_not_satisfied."""
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="H1", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")
    outcome = resolve_outcome(
        ledger=ledger,
        gate=GateResult(passed=False, failures=["gate_not_satisfied"]),
        budget_state={"gate_rejections_exhausted": True},
    )
    assert outcome.kind == "unknown"
    assert outcome.reason == "gate_not_satisfied"


# ---------------------------------------------------------------------------
# 6. Conclude Node 2-Retry Limit & Downgrade
# ---------------------------------------------------------------------------


def test_conclude_node_retries_and_downgrade(standard_alert: AlertEvent, base_time: datetime) -> None:
    """Conclude node allows at most 2 retries on gate failure, then forces unknown gate_not_satisfied."""
    store = make_evidence_store(base_time)
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(ledger, statement="Sole hypothesis", category="bad_deploy", mechanism="connection_pool_exhaustion", service="orders")

    run_service = MagicMock()
    run_id = str(uuid4())
    tenant_id = str(uuid4())
    inc_id = str(uuid4())

    # Initial state: 0 prior rejections
    state = create_initial_state(run_id, tenant_id, inc_id, standard_alert, schema_version="v2")
    state["hypotheses_ledger"] = {h1.id: h1.model_dump()}
    state["last_step_decision"] = {"step": "conclude", "confidence_label": "high"}

    # Attempt 1: Should reject and increment gate_rejection_count to 1
    res1 = conclude_node(state, run_service=run_service, evidence_store=store)
    assert res1.get("status") == "gate_rejected"
    assert res1.get("gate_rejection_count") == 1
    assert "last_step_rejection" in res1
    assert run_service.append_event.call_count == 1
    assert run_service.append_event.call_args[1]["event_type"] == "gate_rejection_emitted"

    # Attempt 2: Should reject and increment gate_rejection_count to 2
    state["gate_rejection_count"] = 1
    res2 = conclude_node(state, run_service=run_service, evidence_store=store)
    assert res2.get("status") == "gate_rejected"
    assert res2.get("gate_rejection_count") == 2
    assert run_service.append_event.call_count == 2

    # Attempt 3: 2 retries already exhausted -> force downgrade to unknown
    state["gate_rejection_count"] = 2
    res3 = conclude_node(state, run_service=run_service, evidence_store=store)
    assert res3.get("status") == "concluded_draft"
    assert res3.get("termination_reason") == "gate_not_satisfied"
    draft = res3.get("draft_diagnosis")
    assert draft["root_cause_category"] == "unknown"
    assert draft["confidence"] == 0.0
    assert run_service.append_event.call_count == 3
    assert run_service.append_event.call_args[1]["event_type"] == "gate_rejection_cap_reached"
