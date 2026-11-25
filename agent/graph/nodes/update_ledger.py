"""agent/graph/nodes/update_ledger.py

LangGraph node for updating the hypothesis ledger during incident investigation (Feature 6).

Responsibilities:
1. Applies validated UpdateHypothesesStep operations through pure ledger functions.
2. If RunService is available, synchronizes mutations with Postgres (app.hypotheses
   and app.hypothesis_evidence) and emits transactional run_events.
3. Updates hypotheses_ledger state and tracks mandatory ledger update count.
4. Routes back to plan_step for next cognitive turn.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from agent.graph.ledger import (
    HypothesisLedger,
    HypothesisRecord,
    add_hypothesis,
    link_evidence,
    set_status,
)
from agent.graph.state import InvestigationState
from agent.graph.steps import CONFIDENCE_LABEL_MAP
from apps.api.domain.context import TenantContext
from apps.api.repositories.hypothesis_repository import HypothesisRepository
from apps.api.services.run_service import RunService

logger = logging.getLogger("opspilot.agent.update_ledger")


def update_ledger_node(
    state: InvestigationState,
    run_service: RunService | None = None,
) -> dict[str, Any]:
    """Applies validated UpdateHypothesesStep operations to the hypothesis ledger."""
    last_step = state.get("last_step_decision") or {}
    if last_step.get("step") != "update_hypotheses":
        logger.warning("update_ledger_node invoked without update_hypotheses in last_step_decision")
        return {}

    raw_ops = last_step.get("operations") or []
    current_step = state.get("step_count", 1)

    # Rehydrate current ledger from state
    current_ledger_dict = state.get("hypotheses_ledger") or {}
    ledger_records: dict[str, HypothesisRecord] = {}

    for hyp_id, hyp_data in current_ledger_dict.items():
        if isinstance(hyp_data, HypothesisRecord):
            ledger_records[hyp_id] = hyp_data
        elif isinstance(hyp_data, dict):
            # Parse dict into HypothesisRecord
            ledger_records[hyp_id] = HypothesisRecord(
                hypothesis_id=hyp_data.get("id") or hyp_id,
                statement=hyp_data.get("statement", "Candidate proposition"),
                category=hyp_data.get("category", "unknown"),
                mechanism=hyp_data.get("mechanism", "unknown"),
                service=hyp_data.get("service"),
                status=hyp_data.get("status", "open"),
                supporting_evidence_ids=tuple(hyp_data.get("supporting_evidence_ids", ())),
                contrary_evidence_ids=tuple(hyp_data.get("contrary_evidence_ids", ())),
                tests_run=tuple(hyp_data.get("tests_run", ())),
                confidence=float(hyp_data.get("confidence", 0.5)),
                created_step=int(hyp_data.get("created_step", 1)),
                updated_step=int(hyp_data.get("updated_step", 1)),
            )

    ledger = HypothesisLedger(hypotheses=ledger_records)

    # Database repository setup if run_service is active
    tenant_id_str = state.get("tenant_id")
    run_id_str = state.get("run_id")
    ctx = TenantContext(tenant_id=UUID(tenant_id_str)) if tenant_id_str else None
    run_uuid = UUID(run_id_str) if run_id_str else None

    hyp_repo: HypothesisRepository | None = None
    if run_service and hasattr(run_service, "session") and run_service.session is not None:
        hyp_repo = HypothesisRepository(session=run_service.session)

    op_summaries: list[str] = []

    for op in raw_ops:
        op_dict = op if isinstance(op, dict) else op.model_dump()
        op_type = op_dict.get("op")

        try:
            if op_type == "add":
                conf_label = op_dict.get("confidence_label", "medium")
                confidence_val = CONFIDENCE_LABEL_MAP.get(conf_label, 0.50)

                ledger, new_hyp = add_hypothesis(
                    ledger=ledger,
                    statement=op_dict.get("statement", ""),
                    category=op_dict.get("category", "unknown"),
                    mechanism=op_dict.get("mechanism", "unknown"),
                    service=op_dict.get("service"),
                    confidence=confidence_val,
                    step=current_step,
                )
                op_summaries.append(f"added {new_hyp.hypothesis_id}")

                if hyp_repo and ctx and run_uuid:
                    try:
                        hyp_repo.add_hypothesis(
                            ctx=ctx,
                            run_id=run_uuid,
                            statement=new_hyp.statement,
                            category=new_hyp.category,
                            mechanism=new_hyp.mechanism,
                            service=new_hyp.service,
                            confidence=new_hyp.confidence,
                            step=current_step,
                        )
                    except Exception as db_exc:
                        logger.warning("DB hypothesis persist skipped or failed: %s", db_exc)
                elif run_service and ctx and run_uuid and hasattr(run_service, "append_event"):
                    run_service.append_event(
                        ctx=ctx,
                        run_id=run_uuid,
                        event_type="hypothesis_added",
                        actor="investigator",
                        payload={
                            "hypothesis_id": new_hyp.hypothesis_id,
                            "statement": new_hyp.statement,
                            "category": new_hyp.category,
                            "mechanism": new_hyp.mechanism,
                            "service": new_hyp.service,
                            "step": current_step,
                        },
                    )

            elif op_type == "link_evidence":
                target_hyp_id = str(op_dict.get("hypothesis_id")) if op_dict.get("hypothesis_id") else None
                ev_id = str(op_dict.get("evidence_id")) if op_dict.get("evidence_id") else None
                relation_raw = op_dict.get("relation", "supports")
                relation: Any = relation_raw if relation_raw in ("supports", "contradicts") else "supports"

                if target_hyp_id and ev_id:
                    ledger, updated_hyp = link_evidence(
                        ledger=ledger,
                        hypothesis_id=target_hyp_id,
                        evidence_id=ev_id,
                        relation=relation,
                        step=current_step,
                    )
                    op_summaries.append(f"linked {ev_id} ({relation}) -> {target_hyp_id}")

                    if hyp_repo and ctx and run_uuid:
                        try:
                            hyp_repo.link_evidence(
                                ctx=ctx,
                                run_id=run_uuid,
                                hypothesis_id=UUID(target_hyp_id) if len(target_hyp_id) == 36 else None,  # type: ignore[arg-type]
                                evidence_id=ev_id,
                                relation=relation,
                                step=current_step,
                            )
                        except Exception as db_exc:
                            logger.debug("DB evidence link skipped: %s", db_exc)
                    elif run_service and ctx and run_uuid and hasattr(run_service, "append_event"):
                        run_service.append_event(
                            ctx=ctx,
                            run_id=run_uuid,
                            event_type="evidence_linked",
                            actor="investigator",
                            payload={
                                "hypothesis_id": target_hyp_id,
                                "evidence_id": ev_id,
                                "relation": relation,
                                "step": current_step,
                            },
                        )

            elif op_type == "set_status":
                status_hyp_id = str(op_dict.get("hypothesis_id")) if op_dict.get("hypothesis_id") else None
                new_status_raw = op_dict.get("status")
                new_status: Any = str(new_status_raw) if new_status_raw else None

                if status_hyp_id and new_status:
                    ledger, updated_hyp = set_status(
                        ledger=ledger,
                        hypothesis_id=status_hyp_id,
                        new_status=new_status,
                        step=current_step,
                    )
                    op_summaries.append(f"status {status_hyp_id} -> {new_status}")

                    if hyp_repo and ctx and run_uuid:
                        try:
                            hyp_repo.set_status(
                                ctx=ctx,
                                run_id=run_uuid,
                                hypothesis_id=UUID(status_hyp_id) if len(status_hyp_id) == 36 else None,  # type: ignore[arg-type]
                                new_status=new_status,
                                step=current_step,
                            )
                        except Exception as db_exc:
                            logger.debug("DB status change skipped: %s", db_exc)
                    elif run_service and ctx and run_uuid and hasattr(run_service, "append_event"):
                        run_service.append_event(
                            ctx=ctx,
                            run_id=run_uuid,
                            event_type="status_changed",
                            actor="investigator",
                            payload={
                                "hypothesis_id": status_hyp_id,
                                "new_status": new_status,
                                "step": current_step,
                            },
                        )

        except Exception as op_exc:
            logger.error("Failed executing ledger op %s: %s", op_type, op_exc)

    # Convert updated ledger to lightweight summary dictionary for state
    updated_ledger_dict: dict[str, dict[str, Any]] = {}
    for hyp_id, hyp_rec in ledger.hypotheses.items():
        summary = hyp_rec.to_summary_dict()
        # Preserve structured tests_run if present
        if hyp_rec.hypothesis_id in current_ledger_dict:
            orig_tests = current_ledger_dict[hyp_rec.hypothesis_id].get("tests_run")
            if orig_tests:
                summary["tests_run"] = orig_tests
        updated_ledger_dict[hyp_id] = summary

    current_summary = state.get("messages_summary", "")
    op_line = f"Turn {current_step}: update_ledger - {', '.join(op_summaries)}"
    new_summary = f"{current_summary.strip()}\n{op_line}" if current_summary.strip() else op_line

    new_update_count = state.get("ledger_update_count", 0) + 1

    return {
        "hypotheses_ledger": updated_ledger_dict,
        "messages_summary": new_summary,
        "ledger_update_count": new_update_count,
        "has_updated_ledger": True,
    }
