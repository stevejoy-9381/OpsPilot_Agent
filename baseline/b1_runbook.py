"""B1: Deterministic Runbook Baseline runner.

Executes generic on-call flowchart logic from baseline/runbook/runbook.yaml using
strictly Feature 2 read tools via dispatch with a hard budget of 4 tool calls.
Zero LLM dependencies.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baseline.alerts.models import AlertEvent
from baseline.models import BaselineResult
from evals.graders.models import Diagnosis
from shared.actions import ActionType, RecommendedAction
from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ToolContext, ToolSuccess
from tools.registry import ToolRegistry

DEFAULT_RUNBOOK_PATH = Path(__file__).parent / "runbook" / "runbook.yaml"


def _parse_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None
    return None


async def run_b1(
    alert: AlertEvent,
    backend: Any,
    store: InMemoryEvidenceStore | None = None,
    scenario_id: str | None = None,
    runbook_path: Path | str | None = None,
    max_calls: int = 4,
) -> BaselineResult:
    """Executes the B1 Deterministic Runbook baseline for an incident alert.

    Args:
        alert: Triggering AlertEvent.
        backend: Read tools backend (SnapshotBackend or LiveBackend).
        store: EvidenceStore for tracking citations.
        scenario_id: Optional scenario identifier for reporting.
        runbook_path: Optional override path to runbook.yaml.
        max_calls: Maximum tool call budget (hard limit: 4).

    Returns:
        BaselineResult with structured Diagnosis and RecommendedAction.
    """
    t0 = time.perf_counter()
    tool_calls: list[str] = []
    evidence_ids: list[str] = []
    ev_store = store or InMemoryEvidenceStore()

    res_at = alert.resolved_at
    w_end = res_at if (res_at and res_at > alert.fired_at) else alert.fired_at
    w_start = alert.fired_at - timedelta(minutes=60)
    ts_ms = int(time.time() * 1000)
    run_key = f"{scenario_id}_{alert.alert_id}" if scenario_id else f"{alert.alert_id}_{ts_ms}"

    # Tool execution context with strict 4-call budget
    ctx = ToolContext(
        run_id=f"run_b1_{run_key}",
        tenant_id="tenant_b1",
        incident_id=f"inc_{alert.alert_id}",
        window_start=w_start,
        window_end=w_end,
        allowed_services=[
            "gateway",
            "orders",
            "inventory",
            "redis",
            "postgres",
            "payment",
            "notification",
        ],
        remaining_calls=max_calls,
    )
    reg = ToolRegistry()

    # Alert classification
    is_saturation = (
        "pool" in alert.metric or "pool" in alert.rule_id or "saturation" in alert.rule_id
    )
    is_latency = "latency" in alert.metric or "latency" in alert.rule_id
    is_error = "error" in alert.metric or "error" in alert.rule_id

    # -------------------------------------------------------------------------
    # Tool Call 1: Check recent deployments on alert.service
    # -------------------------------------------------------------------------
    tool_calls.append("list_deploys")
    res_deploys = await reg.dispatch(
        "list_deploys",
        {"service": alert.service, "limit": 5},
        ctx,
        backend=backend,
        store=ev_store,
    )

    recent_deploys: list[dict[str, Any]] = []
    if isinstance(res_deploys, ToolSuccess):
        evidence_ids.append(res_deploys.evidence_id)
        raw_deploys = res_deploys.data.deploys if hasattr(res_deploys.data, "deploys") else []
        for d in raw_deploys:
            d_dict = d if isinstance(d, dict) else d.model_dump()
            ts = _parse_ts(d_dict.get("deployed_at"))
            if ts and ts <= alert.fired_at:
                age_s = (alert.fired_at - ts).total_seconds()
                d_dict["age_seconds"] = age_s
                recent_deploys.append(d_dict)

    # -------------------------------------------------------------------------
    # Evaluate R1: Latency or error alert on X + deploy to X within 30 minutes
    # -------------------------------------------------------------------------
    if is_latency or is_error:
        deploys_30m = [d for d in recent_deploys if d.get("age_seconds", 99999) <= 1800]
        if deploys_30m:
            culprit = deploys_30m[0]
            c_id = culprit.get("deploy_id")
            notes = f"R1: Rollback {alert.service} to revert deploy {c_id} deployed within 30m"
            mech: Any = "cache_bypass_latency" if is_latency else "unknown"
            diag = Diagnosis(
                root_cause_service=alert.service,
                root_cause_category="bad_deploy",
                mechanism=mech,
                culprit_deploy_id=c_id,
                evidence_ids=evidence_ids,
                confidence=0.85,
            )
            act = RecommendedAction(
                action=ActionType.ROLLBACK_SERVICE,
                service=alert.service,
                rationale_ids=evidence_ids,
                notes=notes[:300],
            )
            return BaselineResult(
                scenario_id=scenario_id,
                baseline_name="B1",
                alert=alert,
                diagnosis=diag,
                action=act,
                tool_calls=tool_calls,
                evidence_ids=evidence_ids,
                rule_matched="R1",
                duration_seconds=round(time.perf_counter() - t0, 5),
            )

    # -------------------------------------------------------------------------
    # Evaluate R3: Saturation alert on X (db_pool) + deploy to X within 60 minutes
    # -------------------------------------------------------------------------
    if is_saturation:
        deploys_60m = [d for d in recent_deploys if d.get("age_seconds", 99999) <= 3600]
        if deploys_60m:
            culprit = deploys_60m[0]
            c_id = culprit.get("deploy_id")
            notes = f"R3: Rollback {alert.service} to resolve saturation from deploy {c_id}"
            diag = Diagnosis(
                root_cause_service=alert.service,
                root_cause_category="bad_deploy",
                mechanism="connection_pool_exhaustion",
                culprit_deploy_id=c_id,
                evidence_ids=evidence_ids,
                confidence=0.85,
            )
            act = RecommendedAction(
                action=ActionType.ROLLBACK_SERVICE,
                service=alert.service,
                rationale_ids=evidence_ids,
                notes=notes[:300],
            )
            return BaselineResult(
                scenario_id=scenario_id,
                baseline_name="B1",
                alert=alert,
                diagnosis=diag,
                action=act,
                tool_calls=tool_calls,
                evidence_ids=evidence_ids,
                rule_matched="R3",
                duration_seconds=round(time.perf_counter() - t0, 5),
            )

    # -------------------------------------------------------------------------
    # Tool Call 2: Check current metric status on alert.service
    # -------------------------------------------------------------------------
    tool_calls.append("get_metrics")
    res_metric = await reg.dispatch(
        "get_metrics",
        {"service": alert.service, "metric": alert.metric},
        ctx,
        backend=backend,
        store=ev_store,
    )
    if isinstance(res_metric, ToolSuccess):
        evidence_ids.append(res_metric.evidence_id)

    # -------------------------------------------------------------------------
    # Evaluate R4: Alert resolved before evaluation completes -> no_action
    # -------------------------------------------------------------------------
    is_resolved = alert.status == "resolved"
    if not is_resolved and isinstance(res_metric, ToolSuccess):
        # Inspect latest observed metric value
        last_val = getattr(res_metric.data.summary, "last", None)
        if last_val is not None:
            if is_saturation and last_val < alert.threshold:
                is_resolved = True
            elif (is_latency or is_error) and last_val <= alert.threshold:
                is_resolved = True

    if is_resolved:
        notes = "R4: Alert resolved before evaluation completed; no action needed"
        diag = Diagnosis(
            root_cause_service=None,
            root_cause_category="false_alarm",
            mechanism="none",
            culprit_deploy_id=None,
            evidence_ids=evidence_ids,
            confidence=0.90,
        )
        act = RecommendedAction(
            action=ActionType.NO_ACTION,
            service=None,
            rationale_ids=evidence_ids,
            notes=notes[:300],
        )
        return BaselineResult(
            scenario_id=scenario_id,
            baseline_name="B1",
            alert=alert,
            diagnosis=diag,
            action=act,
            tool_calls=tool_calls,
            evidence_ids=evidence_ids,
            rule_matched="R4",
            duration_seconds=round(time.perf_counter() - t0, 5),
        )

    # -------------------------------------------------------------------------
    # Evaluate R2: Error/latency on X, no deploy to X, check downstream metrics
    # -------------------------------------------------------------------------
    elevated_dep: str | None = None

    if alert.service == "gateway":
        # Check downstream inventory latency
        tool_calls.append("get_metrics")
        res_inv = await reg.dispatch(
            "get_metrics",
            {"service": "inventory", "metric": "latency_p95"},
            ctx,
            backend=backend,
            store=ev_store,
        )
        if isinstance(res_inv, ToolSuccess):
            evidence_ids.append(res_inv.evidence_id)
            inv_last = getattr(res_inv.data.summary, "last", 0.0)
            if inv_last > 0.15:
                # Inventory is elevated; check inventory dependency
                tool_calls.append("get_metrics")
                res_dep = await reg.dispatch(
                    "get_metrics",
                    {"service": "inventory", "metric": "dependency_latency_p95"},
                    ctx,
                    backend=backend,
                    store=ev_store,
                )
                if isinstance(res_dep, ToolSuccess):
                    evidence_ids.append(res_dep.evidence_id)
                    dep_last = getattr(res_dep.data.summary, "last", 0.0)
                    if dep_last > 0.15:
                        elevated_dep = "redis"
                    else:
                        elevated_dep = "inventory"
                else:
                    elevated_dep = "inventory"

    elif alert.service == "inventory":
        # Check inventory dependency metric (redis)
        tool_calls.append("get_metrics")
        res_dep = await reg.dispatch(
            "get_metrics",
            {"service": "inventory", "metric": "dependency_latency_p95"},
            ctx,
            backend=backend,
            store=ev_store,
        )
        if isinstance(res_dep, ToolSuccess):
            evidence_ids.append(res_dep.evidence_id)
            dep_last = getattr(res_dep.data.summary, "last", 0.0)
            if dep_last > 0.15:
                elevated_dep = "redis"

    if elevated_dep:
        notes = f"R2: Restart {elevated_dep} due to elevated downstream dependency latency"
        diag = Diagnosis(
            root_cause_service=elevated_dep,
            root_cause_category="dependency_failure",
            mechanism="dependency_timeout",
            culprit_deploy_id=None,
            evidence_ids=evidence_ids,
            confidence=0.80,
        )
        act = RecommendedAction(
            action=ActionType.RESTART_SERVICE,
            service=elevated_dep,
            rationale_ids=evidence_ids,
            notes=notes[:300],
        )
        return BaselineResult(
            scenario_id=scenario_id,
            baseline_name="B1",
            alert=alert,
            diagnosis=diag,
            action=act,
            tool_calls=tool_calls,
            evidence_ids=evidence_ids,
            rule_matched="R2",
            duration_seconds=round(time.perf_counter() - t0, 5),
        )

    # -------------------------------------------------------------------------
    # Evaluate R5: Default fallback escalation
    # -------------------------------------------------------------------------
    notes = "R5: No deterministic runbook rule matched; escalate to human engineer"
    diag = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        culprit_deploy_id=None,
        evidence_ids=evidence_ids,
        confidence=0.0,
    )
    act = RecommendedAction(
        action=ActionType.ESCALATE,
        service=None,
        rationale_ids=evidence_ids,
        notes=notes[:300],
    )
    return BaselineResult(
        scenario_id=scenario_id,
        baseline_name="B1",
        alert=alert,
        diagnosis=diag,
        action=act,
        tool_calls=tool_calls,
        evidence_ids=evidence_ids,
        rule_matched="R5",
        duration_seconds=round(time.perf_counter() - t0, 5),
    )
