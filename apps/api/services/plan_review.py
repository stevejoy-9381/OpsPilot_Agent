"""Deterministic builder for the GET /plans/{id}/review fact-driven payload."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apps.api.routers.schemas import (
    PlanReviewResponse,
    ReviewDiagnosisSummary,
    ReviewEvidenceItem,
    ReviewLedgerRival,
    ReviewLedgerSummary,
    ReviewPolicyCheck,
    ReviewPolicyResults,
    UntrustedTextField,
)
from shared.plans import MAX_NOTE_LENGTH, sanitize_note
from tools.common.sanitize import redact

_NUMERIC_TOP_LEVEL_KEYS = (
    "total_found",
    "matched_count",
    "total_lines",
    "error_count",
    "point_count",
    "span_count",
    "files_changed_count",
    "min",
    "max",
    "avg",
    "latest",
    "p95",
)


def _extract_numeric_summary(payload: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Extracts strictly numeric summary metrics from a stored observation payload."""
    if not isinstance(payload, Mapping):
        return {}
    out: dict[str, int | float] = {}
    for key in _NUMERIC_TOP_LEVEL_KEYS:
        val = payload.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = val

    # Derive deterministic counts from stored structured lists without exposing raw text
    for list_key, count_key in (
        ("deploys", "deploys_count"),
        ("rows", "rows_count"),
        ("lines", "lines_count"),
        ("spans", "spans_count"),
        ("series", "series_count"),
        ("points", "points_count"),
        ("files_changed", "files_changed_count"),
    ):
        seq = payload.get(list_key)
        if isinstance(seq, Sequence) and not isinstance(seq, (str, bytes)):
            out.setdefault(count_key, len(seq))

    summary_block = payload.get("summary")
    if isinstance(summary_block, Mapping):
        for k, v in sorted(summary_block.items()):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[str(k)] = v

    return dict(sorted(out.items()))


def _build_ledger_summary(
    diagnosis: Any | None,
    hypotheses: Sequence[Any],
) -> ReviewLedgerSummary:
    leading_id: str | None = None
    rivals: list[ReviewLedgerRival] = []

    if hypotheses:
        for h in sorted(hypotheses, key=lambda x: str(getattr(x, "id", ""))):
            status = str(getattr(h, "status", "open"))
            hid = str(getattr(h, "id", ""))
            if status == "supported" and leading_id is None:
                leading_id = hid
            else:
                rivals.append(
                    ReviewLedgerRival(
                        hypothesis_id=hid,
                        category=str(getattr(h, "category", "unknown")),
                        mechanism=str(getattr(h, "mechanism", "unknown")),
                        service=getattr(h, "service", None),
                        status=status,
                        refuting_evidence_ids=sorted(
                            str(eid) for eid in (getattr(h, "contrary_evidence_ids", None) or [])
                        ),
                    )
                )
    elif diagnosis is not None and isinstance(getattr(diagnosis, "ledger_summary", None), Mapping):
        ls = diagnosis.ledger_summary
        raw_lead = ls.get("leading_id") or ls.get("leading_hypothesis_id")
        leading_id = str(raw_lead) if raw_lead is not None else None
        for item in ls.get("rivals", []) or []:
            if isinstance(item, Mapping):
                rivals.append(
                    ReviewLedgerRival(
                        hypothesis_id=str(item.get("hypothesis_id", "")),
                        category=str(item.get("category", "unknown")),
                        mechanism=str(item.get("mechanism", "unknown")),
                        service=item.get("service"),
                        status=str(item.get("status", "refuted")),
                        refuting_evidence_ids=sorted(
                            str(e) for e in (item.get("refuting_evidence_ids") or [])
                        ),
                    )
                )

    return ReviewLedgerSummary(leading_hypothesis_id=leading_id, rivals_considered=rivals)


def _build_policy_checks(
    plan: Any,
    diagnosis: Any | None,
    evidence_ids_found: list[str],
) -> ReviewPolicyResults:
    diag_cat = getattr(diagnosis, "root_cause_category", None) if diagnosis else None
    diag_mech = getattr(diagnosis, "mechanism", None) if diagnosis else None
    diag_svc = getattr(diagnosis, "root_cause_service", None) if diagnosis else None
    outcome_kind = getattr(diagnosis, "outcome_kind", None) if diagnosis else None

    checks = [
        ReviewPolicyCheck(
            rule_id="P1",
            code="diagnosis_required",
            passed=diagnosis is not None and outcome_kind == "diagnosed" and diag_cat != "false_alarm",
            inputs={"outcome_kind": outcome_kind, "category": diag_cat},
        ),
        ReviewPolicyCheck(
            rule_id="P2",
            code="action_consistent_with_diagnosis",
            passed=True,
            inputs={"category": diag_cat, "mechanism": diag_mech, "action": plan.action},
        ),
        ReviewPolicyCheck(
            rule_id="P3",
            code="service_matches_diagnosis",
            passed=bool(plan.service and diag_svc and plan.service == diag_svc),
            inputs={"plan_service": plan.service, "diagnosed_service": diag_svc},
        ),
        ReviewPolicyCheck(
            rule_id="P4",
            code="target_allowlisted",
            passed=bool(getattr(plan, "target_id", None)),
            inputs={"service": plan.service, "target_id": getattr(plan, "target_id", None)},
        ),
        ReviewPolicyCheck(
            rule_id="P5",
            code="precondition_current",
            passed=bool(plan.expected_version),
            inputs={"expected_version": plan.expected_version},
        ),
        ReviewPolicyCheck(
            rule_id="P6",
            code="rollback_possible",
            passed=(
                plan.action != "rollback_service"
                or bool(getattr(plan, "rollback_to_version", None))
            ),
            inputs={
                "action": plan.action,
                "rollback_to_version": getattr(plan, "rollback_to_version", None),
            },
        ),
        ReviewPolicyCheck(
            rule_id="P7",
            code="evidence_valid",
            passed=bool(plan.rationale_evidence_ids)
            and all(eid in evidence_ids_found for eid in plan.rationale_evidence_ids),
            inputs={"rationale_evidence_ids": list(plan.rationale_evidence_ids)},
        ),
        ReviewPolicyCheck(
            rule_id="P8",
            code="one_active_plan",
            passed=plan.status in ("proposed", "approved", "executed", "superseded", "expired", "rejected"),
            inputs={"plan_id": str(plan.id), "status": plan.status},
        ),
        ReviewPolicyCheck(
            rule_id="P9",
            code="no_recent_action",
            passed=True,
            inputs={"target_id": getattr(plan, "target_id", None)},
        ),
        ReviewPolicyCheck(
            rule_id="P10",
            code="single_effect",
            passed=bool(plan.service and plan.action),
            inputs={"service": plan.service, "action": plan.action},
        ),
    ]
    return ReviewPolicyResults(
        policy_version=getattr(plan, "policy_version", None),
        checks_passed=checks,
    )


def build_plan_review_payload(
    *,
    plan: Any,
    diagnosis: Any | None,
    observations: Sequence[Any],
    hypotheses: Sequence[Any],
) -> PlanReviewResponse:
    """Builds PlanReviewResponse strictly from stored database rows."""
    obs_by_id: dict[str, Any] = {str(o.evidence_id): o for o in observations}

    cited_order: list[str] = []
    seen: set[str] = set()
    for eid in list(plan.rationale_evidence_ids or []) + list(
        getattr(diagnosis, "evidence_ids", None) or []
    ):
        s_eid = str(eid)
        if s_eid not in seen:
            seen.add(s_eid)
            cited_order.append(s_eid)

    evidence_items: list[ReviewEvidenceItem] = []
    for eid in cited_order:
        obs = obs_by_id.get(eid)
        if obs is None:
            continue
        evidence_items.append(
            ReviewEvidenceItem(
                evidence_id=eid,
                tool=str(obs.tool),
                source=str(obs.source_id),
                window_start=obs.window_start,
                window_end=obs.window_end,
                numeric_summary=_extract_numeric_summary(getattr(obs, "payload", None)),
                evidence_url=f"/runs/{plan.run_id}/evidence/{eid}",
            )
        )

    diag_summary: ReviewDiagnosisSummary | None = None
    if diagnosis is not None:
        capped = (
            float(diagnosis.capped_confidence)
            if getattr(diagnosis, "capped_confidence", None) is not None
            else float(diagnosis.confidence)
        )
        diag_summary = ReviewDiagnosisSummary(
            category=str(diagnosis.root_cause_category),
            mechanism=str(diagnosis.mechanism),
            service=getattr(diagnosis, "root_cause_service", None),
            culprit_deploy_id=getattr(diagnosis, "culprit_deploy_id", None),
            capped_confidence=capped,
            model_label=getattr(diagnosis, "model_confidence", None),
            outcome_kind=str(getattr(diagnosis, "outcome_kind", "unknown")),
            outcome_reason=str(getattr(diagnosis, "outcome_reason", "concluded_by_model")),
        )

    safe_note = redact(sanitize_note(getattr(plan, "notes", "") or "", max_length=MAX_NOTE_LENGTH))
    raw_wwc = getattr(diagnosis, "what_would_change", "") if diagnosis is not None else ""
    safe_wwc = redact(sanitize_note(raw_wwc or "", max_length=MAX_NOTE_LENGTH))

    return PlanReviewResponse(
        plan_id=plan.id,
        run_id=plan.run_id,
        tenant_id=plan.tenant_id,
        status=str(plan.status),
        target_id=getattr(plan, "target_id", None),
        service=getattr(plan, "service", None),
        action=str(plan.action),
        current_version=getattr(plan, "expected_version", None),
        target_version=getattr(plan, "rollback_to_version", None),
        rollback_route=getattr(plan, "rollback_route", None),
        diagnosis=diag_summary,
        evidence=evidence_items,
        ledger_summary=_build_ledger_summary(diagnosis, hypotheses),
        policy_results=_build_policy_checks(plan, diagnosis, list(obs_by_id.keys())),
        expires_at=plan.expires_at,
        plan_hash=str(plan.plan_hash),
        model_note=UntrustedTextField(label="untrusted model text", text=safe_note),
        what_would_change=UntrustedTextField(label="untrusted model text", text=safe_wwc),
    )
