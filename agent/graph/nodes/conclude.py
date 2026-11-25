"""agent/graph/nodes/conclude.py

LangGraph node validating the investigator model's proposed Diagnosis draft.

Responsibilities:
1. Receives the ConcludeStep or draft_diagnosis from the planner node.
2. Validates the Diagnosis artifact against the Feature 1.12 Diagnosis schema.
3. If missing or malformed, falls back to a deterministic unknown Diagnosis.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.graph.state import InvestigationState
from agent.graph.steps import ConcludeStep
from evals.graders.models import Diagnosis

logger = logging.getLogger("opspilot.agent.conclude")


def conclude_node(state: InvestigationState) -> dict[str, Any]:
    """LangGraph node validating the model's diagnosis draft before finalization.

    Validates:
    - If last_step_decision is a ConcludeStep, extracts and validates its diagnosis_draft.
    - If draft_diagnosis is present in state, validates it against Diagnosis model.
    - If missing or schema validation fails, sets a safe fallback 'unknown' diagnosis.
    """
    draft_raw: Any = state.get("draft_diagnosis")

    # If draft_diagnosis is not yet in state, check last_step_decision
    if draft_raw is None:
        last_step = state.get("last_step_decision")
        if isinstance(last_step, ConcludeStep):
            draft_raw = last_step.diagnosis_draft
        elif isinstance(last_step, dict) and last_step.get("step") == "conclude":
            draft_raw = last_step.get("diagnosis_draft")

    if draft_raw is not None:
        try:
            if isinstance(draft_raw, Diagnosis):
                diagnosis = draft_raw
            else:
                diagnosis = Diagnosis.model_validate(draft_raw)
            logger.info("Successfully validated diagnosis draft: category=%s", diagnosis.root_cause_category)
        except Exception as exc:
            logger.warning("Diagnosis draft failed schema validation: %s; falling back to unknown", exc)
            diagnosis = Diagnosis(
                root_cause_service=None,
                root_cause_category="unknown",
                mechanism="unknown",
                culprit_deploy_id=None,
                evidence_ids=list(state.get("evidence_ids", [])),
                confidence=0.0,
            )
    else:
        logger.warning("No diagnosis draft found in state; falling back to unknown")
        diagnosis = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.0,
        )

    return {
        "draft_diagnosis": diagnosis.model_dump(),
        "status": "concluded_draft",
    }
