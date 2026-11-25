"""agent/graph/nodes/load_context.py

LangGraph node initializing context and operational state for the investigation run.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from agent.graph.state import InvestigationState
from apps.api.domain.context import TenantContext
from apps.api.services.run_service import RunService

logger = logging.getLogger("opspilot.agent.load_context")


def load_context_node(
    state: InvestigationState,
    run_service: RunService | None = None,
) -> dict[str, Any]:
    """Initializes investigation state and fetches incident alert context if needed."""
    run_id_str = state.get("run_id")
    tenant_id_str = state.get("tenant_id")

    alert = state.get("alert")
    if (not alert) and run_service and run_id_str and tenant_id_str:
        try:
            ctx = TenantContext(tenant_id=UUID(tenant_id_str))
            run_uuid = UUID(run_id_str)
            run = run_service.run_repo.get_by_id(ctx, run_uuid)
            # Retrieve incident alert payload if available
            if hasattr(run, "incident") and run.incident and hasattr(run.incident, "alert_payload"):
                alert = run.incident.alert_payload
        except Exception as exc:
            logger.warning("Failed to load incident alert from DB: %s", exc)

    return {
        "status": "investigating",
        "remaining_calls": state.get("remaining_calls", 8),
        "step_count": state.get("step_count", 0),
        "alert": alert or {},
        "messages_summary": state.get("messages_summary", ""),
        "evidence_ids": list(state.get("evidence_ids", [])),
        "tool_trace": list(state.get("tool_trace", [])),
    }
