"""Deterministic Policy Validator (P1–P12) for Feature 7.

No LLM calls. No network or filesystem I/O beyond loading static config tables
and querying the injected TargetStateReader, evidence map, and DB clock.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent.graph.gate import ALLOWED_TOOL_SOURCES, GateResult, Outcome
from agent.policy.table import PolicyTable, compute_policy_version, load_policy_table
from evals.graders.models import Diagnosis, EvidenceItem
from shared.actions import ActionType
from shared.plans import ActionPlan, PlanStatus, ProposalInput, plan_hash
from shared.targets import (
    TargetRegistry,
    TargetSpec,
    TargetState,
    TargetStateError,
    TargetStateErrorCode,
    TargetStateReader,
    load_target_registry,
)

DEFAULT_APPROVAL_TTL_SECONDS: int = int(os.environ.get("APPROVAL_TTL_SECONDS", "900"))
DEFAULT_COOLDOWN_MINUTES: int = int(os.environ.get("ACTION_COOLDOWN_MINUTES", "10"))
_MULTI_SERVICE_SPLIT_RE = re.compile(r"[,;\s+|/]+")


class PolicyViolation(BaseModel):
    """Typed policy rule violation returned to the graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    field: str
    hint: str


class DerivedPlanFields(BaseModel):
    """Server-derived fields computed when a proposal passes all policy rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    rollback_to_version: str | None = None
    rollback_route: str
    expires_at: datetime
    plan_hash: str
    policy_version: str
    supersedes_plan_id: str | None = None


class PolicyResult(BaseModel):
    """Result returned by validate()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    violations: list[PolicyViolation] = Field(default_factory=list)
    derived: DerivedPlanFields | None = None
    policy_version: str


class RecentExecution(BaseModel):
    """Recent execution record for flapping-guard checks (P9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    status: str
    executed_at: datetime


class PolicyContext(BaseModel):
    """All deterministic context required to validate a ProposalInput."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tenant_id: str | UUID
    run_id: str | UUID
    diagnosis: Diagnosis | None = None
    outcome: Outcome | None = None
    gate_result: GateResult | None = None
    ledger_summary: dict[str, Any] | None = None
    window_start: datetime
    window_end: datetime
    as_of: datetime | None = None
    active_plan: ActionPlan | None = None
    recent_executions: list[RecentExecution] = Field(default_factory=list)
    evidence_items: Sequence[Any] | Mapping[str, Any] = ()
    target_reader: Any = Field(description="Injected TargetStateReader")
    db_clock_fn: Callable[[], datetime] = Field(
        description="Injected DB clock provider returning PostgreSQL clock_timestamp()"
    )
    approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    target_registry: TargetRegistry | None = None
    policy_table: PolicyTable | None = None


def _to_utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        raw = dt.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    else:
        parsed = dt
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _index_evidence(
    evidence_items: Sequence[Any] | Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    items = evidence_items.values() if isinstance(evidence_items, Mapping) else evidence_items
    for item in items:
        if isinstance(item, EvidenceItem):
            indexed[item.evidence_id] = {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "timestamp": item.timestamp,
                "data": item.data,
            }
        elif isinstance(item, Mapping):
            eid = str(item.get("evidence_id", "")).strip()
            if eid:
                indexed[eid] = dict(item)
        else:
            eid = str(getattr(item, "evidence_id", "")).strip()
            if eid:
                indexed[eid] = {
                    "evidence_id": eid,
                    "run_id": getattr(item, "run_id", None),
                    "source": getattr(item, "tool", None) or getattr(item, "source", ""),
                    "timestamp": getattr(item, "captured_at", None) or getattr(item, "timestamp", None),
                    "window_start": getattr(item, "window_start", None),
                    "window_end": getattr(item, "window_end", None),
                    "data": getattr(item, "payload", None) or getattr(item, "data", {}),
                }
    return indexed


def _extract_culprit_deploy_version(
    culprit_deploy_id: str,
    evidence_map: dict[str, dict[str, Any]],
) -> str | None:
    for ev in evidence_map.values():
        data = ev.get("data") or {}
        if not isinstance(data, Mapping):
            continue
        deploys = data.get("deploys")
        if isinstance(deploys, list):
            for d in deploys:
                if isinstance(d, Mapping) and d.get("deploy_id") == culprit_deploy_id:
                    ver = d.get("new_version") or d.get("version")
                    if ver:
                        return str(ver).strip()
        if data.get("deploy_id") == culprit_deploy_id:
            ver = data.get("new_version") or data.get("version")
            if ver:
                return str(ver).strip()
    return None


def validate(proposal: ProposalInput, context: PolicyContext) -> PolicyResult:
    """Validates a ProposalInput deterministically against rules P1–P12.

    Collects ALL violations in one pass. Fails closed on any internal error or
    unavailable dependency with code `policy_unavailable`.
    """
    policy_ver = "unknown"
    try:
        table = context.policy_table or load_policy_table()
        registry = context.target_registry or load_target_registry()
        policy_ver = compute_policy_version(table=table, registry=registry)
    except Exception as exc:
        return PolicyResult(
            allowed=False,
            violations=[
                PolicyViolation(
                    code="policy_unavailable",
                    field="policy",
                    hint=f"Failed to load policy table or target registry: {exc}",
                )
            ],
            derived=None,
            policy_version=policy_ver,
        )

    try:
        violations: list[PolicyViolation] = []
        w_start = _to_utc(context.window_start)
        w_end = _to_utc(context.window_end)
        as_of = _to_utc(context.as_of) if context.as_of is not None else w_end

        # P10: single_effect (no multi-service / compound string)
        raw_svc = proposal.service.strip().lower()
        if _MULTI_SERVICE_SPLIT_RE.search(raw_svc):
            violations.append(
                PolicyViolation(
                    code="single_effect",
                    field="service",
                    hint="Exactly one scalar service is permitted per plan.",
                )
            )

        # P1: diagnosis_required
        diag = context.diagnosis
        outcome_kind = (
            context.outcome.kind
            if context.outcome is not None
            else (diag.outcome_kind if diag is not None else None)
        )
        gate_passed = context.gate_result.passed if context.gate_result is not None else False
        if (
            diag is None
            or outcome_kind != "diagnosed"
            or not gate_passed
            or diag.root_cause_category == "false_alarm"
        ):
            violations.append(
                PolicyViolation(
                    code="diagnosis_required",
                    field="diagnosis",
                    hint="A gate-passed 'diagnosed' non-false-alarm diagnosis is required.",
                )
            )
        else:
            # P2: action_consistent_with_diagnosis
            allowed_actions = table.allowed_actions_for(diag.root_cause_category, diag.mechanism)
            if allowed_actions is None or proposal.action not in allowed_actions:
                violations.append(
                    PolicyViolation(
                        code="action_consistent_with_diagnosis",
                        field="action",
                        hint=(
                            f"Action '{proposal.action.value}' is not permitted for "
                            f"({diag.root_cause_category}, {diag.mechanism})."
                        ),
                    )
                )

            # P3: service_matches_diagnosis
            diag_svc = (diag.root_cause_service or "").strip().lower()
            if not diag_svc or raw_svc != diag_svc:
                violations.append(
                    PolicyViolation(
                        code="service_matches_diagnosis",
                        field="service",
                        hint=(
                            f"Proposed service '{raw_svc}' must match diagnosed "
                            f"root_cause_service '{diag_svc or None}'."
                        ),
                    )
                )

        # P4: target_allowlisted
        spec: TargetSpec | None = registry.get_by_service(raw_svc)
        if spec is None:
            violations.append(
                PolicyViolation(
                    code="target_allowlisted",
                    field="service",
                    hint=f"Service '{raw_svc}' is not in the lab target allowlist.",
                )
            )
        else:
            # P12: action_unsupported_by_target
            if proposal.action not in spec.allowed_actions:
                violations.append(
                    PolicyViolation(
                        code="action_unsupported_by_target",
                        field="action",
                        hint=(
                            f"Action '{proposal.action.value}' is not supported on target "
                            f"'{spec.target_id}'."
                        ),
                    )
                )

        # P5, P6, P9: TargetStateReader checks (fail closed on reader error)
        target_state: TargetState | None = None
        if spec is not None:
            reader: TargetStateReader = context.target_reader
            state_res = reader.current_state(spec.target_id, as_of)
            if isinstance(state_res, TargetStateError):
                if state_res.code == TargetStateErrorCode.TARGET_NOT_FOUND:
                    violations.append(
                        PolicyViolation(
                            code="target_allowlisted",
                            field="service",
                            hint=state_res.message,
                        )
                    )
                else:
                    violations.append(
                        PolicyViolation(
                            code="policy_unavailable",
                            field="target_state",
                            hint=f"{state_res.code.value}: {state_res.message}",
                        )
                    )
            else:
                target_state = state_res
                # P5: precondition_current
                if proposal.expected_version != target_state.current_version:
                    violations.append(
                        PolicyViolation(
                            code="precondition_current",
                            field="expected_version",
                            hint=(
                                f"expected_version '{proposal.expected_version}' does not match "
                                f"live version '{target_state.current_version}'."
                            ),
                        )
                    )
                # P6: rollback_possible
                if proposal.action == ActionType.ROLLBACK_SERVICE:
                    if (
                        not target_state.previous_version
                        or target_state.previous_version == target_state.current_version
                    ):
                        violations.append(
                            PolicyViolation(
                                code="rollback_possible",
                                field="action",
                                hint="No distinct previous deployed version exists for rollback.",
                            )
                        )
                # P9: no_recent_action (from target's last_action_at + recent_executions)
                cooldown = timedelta(minutes=context.cooldown_minutes)
                recent_ts: list[datetime] = []
                if target_state.last_action_at is not None:
                    recent_ts.append(target_state.last_action_at)
                for ex in context.recent_executions:
                    if ex.target_id == spec.target_id and ex.status in (
                        "dispatched",
                        "acknowledged",
                        "reconciled",
                        "executed",
                    ):
                        recent_ts.append(_to_utc(ex.executed_at))
                for ts in recent_ts:
                    if (as_of - ts) < cooldown:
                        violations.append(
                            PolicyViolation(
                                code="no_recent_action",
                                field="service",
                                hint=(
                                    f"Target '{spec.target_id}' had a recent action at "
                                    f"{ts.isoformat()} within {context.cooldown_minutes}m cooldown."
                                ),
                            )
                        )
                        break

        # P7: evidence_valid
        evidence_map = _index_evidence(context.evidence_items)
        diag_eids = set(diag.evidence_ids) if diag is not None else set()
        run_id_str = str(context.run_id)
        invalid_eids: list[str] = []
        for eid in proposal.rationale_evidence_ids:
            ev = evidence_map.get(eid)
            if ev is None:
                invalid_eids.append(eid)
                continue
            ev_run = ev.get("run_id")
            if ev_run is not None and str(ev_run) != run_id_str:
                invalid_eids.append(eid)
                continue
            src = str(ev.get("source", "")).strip().lower()
            if src not in ALLOWED_TOOL_SOURCES:
                invalid_eids.append(eid)
                continue
            ev_w_start = ev.get("window_start")
            ev_w_end = ev.get("window_end")
            ev_ts = ev.get("timestamp")
            if ev_w_start is not None and ev_w_end is not None:
                if _to_utc(ev_w_end) < w_start or _to_utc(ev_w_start) > w_end:
                    invalid_eids.append(eid)
                    continue
            elif ev_ts is not None:
                ts_utc = _to_utc(ev_ts)
                if ts_utc < w_start or ts_utc > w_end:
                    invalid_eids.append(eid)
                    continue
        overlap = set(proposal.rationale_evidence_ids) & diag_eids
        if invalid_eids or (diag is not None and not overlap):
            violations.append(
                PolicyViolation(
                    code="evidence_valid",
                    field="rationale_evidence_ids",
                    hint=(
                        "Rationale evidence IDs must exist in this run's window from read tools "
                        "and overlap with the diagnosis's cited evidence."
                    ),
                )
            )

        # P8: one_active_plan (non-proposed active plan blocks; proposed active plan is superseded)
        supersedes_id: str | None = None
        if context.active_plan is not None:
            if context.active_plan.status == PlanStatus.PROPOSED:
                supersedes_id = context.active_plan.plan_id
            elif context.active_plan.status in (PlanStatus.APPROVED, PlanStatus.EXECUTED):
                violations.append(
                    PolicyViolation(
                        code="one_active_plan",
                        field="run_id",
                        hint=(
                            f"Run already has a '{context.active_plan.status.value}' plan "
                            f"({context.active_plan.plan_id})."
                        ),
                    )
                )

        # P11: culprit_deploy_mismatch
        if (
            diag is not None
            and proposal.action == ActionType.ROLLBACK_SERVICE
            and diag.culprit_deploy_id
        ):
            culprit_ver = _extract_culprit_deploy_version(diag.culprit_deploy_id, evidence_map)
            if culprit_ver is not None and culprit_ver != proposal.expected_version:
                violations.append(
                    PolicyViolation(
                        code="culprit_deploy_mismatch",
                        field="expected_version",
                        hint=(
                            f"expected_version '{proposal.expected_version}' does not match "
                            f"culprit deploy '{diag.culprit_deploy_id}' version '{culprit_ver}'."
                        ),
                    )
                )

        if violations:
            return PolicyResult(
                allowed=False,
                violations=violations,
                derived=None,
                policy_version=policy_ver,
            )

        # All checks passed -> derive server fields using DB clock
        assert spec is not None and target_state is not None
        db_now = _to_utc(context.db_clock_fn())
        expires_at = db_now + timedelta(seconds=context.approval_ttl_seconds)
        rollback_to_version = (
            target_state.previous_version
            if proposal.action == ActionType.ROLLBACK_SERVICE
            else None
        )
        if proposal.action == ActionType.ROLLBACK_SERVICE:
            rollback_route = (
                f"Re-deploy {spec.service} {proposal.expected_version} (new proposal required)"
            )
        else:
            rollback_route = (
                f"None — idempotent client-state reset on {spec.service}; "
                "escalate or re-investigate if degraded"
            )

        computed_hash = plan_hash(
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            action=proposal.action,
            service=spec.service,
            target_id=spec.target_id,
            expected_version=proposal.expected_version,
            rollback_to_version=rollback_to_version,
            rationale_evidence_ids=proposal.rationale_evidence_ids,
            expires_at=expires_at,
        )

        derived = DerivedPlanFields(
            target_id=spec.target_id,
            rollback_to_version=rollback_to_version,
            rollback_route=rollback_route,
            expires_at=expires_at,
            plan_hash=computed_hash,
            policy_version=policy_ver,
            supersedes_plan_id=supersedes_id,
        )
        return PolicyResult(
            allowed=True,
            violations=[],
            derived=derived,
            policy_version=policy_ver,
        )
    except Exception as exc:
        return PolicyResult(
            allowed=False,
            violations=[
                PolicyViolation(
                    code="policy_unavailable",
                    field="validator",
                    hint=f"Internal policy validator failure (fail closed): {exc}",
                )
            ],
            derived=None,
            policy_version=policy_ver,
        )
