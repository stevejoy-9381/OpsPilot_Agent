"""agent/graph/nodes/record.py

LangGraph node persisting tool observations to DB and recording lightweight trace references in state.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from agent.graph.state import InvestigationState, ToolCallRecordRef
from apps.api.domain.context import TenantContext
from apps.api.models.tables import ObservationModel
from apps.api.services.run_service import RunService

logger = logging.getLogger("opspilot.agent.record")


def record_node(
    state: InvestigationState,
    run_service: RunService | None = None,
) -> dict[str, Any]:
    """Records the outcome of a tool execution in state and persists raw observations to PostgreSQL."""
    tool_result: dict[str, Any] | None = state.get("transient_tool_result")
    if not tool_result:
        logger.warning("record_node called without transient_tool_result")
        return {}

    tool_name = tool_result.get("tool", "unknown_tool")
    ok = bool(tool_result.get("ok", False))
    evidence_id = tool_result.get("evidence_id")
    duration_ms = float(tool_result.get("duration_ms", 0.0))
    error_code = tool_result.get("error_code")
    raw_args = tool_result.get("args") or {}

    # Format concise args summary
    args_summary = ", ".join(f"{k}={v}" for k, v in sorted(raw_args.items()))[:200]

    record_ref = ToolCallRecordRef(
        tool=tool_name,
        args_summary=args_summary,
        duration_ms=duration_ms,
        ok=ok,
        error_code=error_code,
        evidence_id=evidence_id,
        tests_hypothesis_id=tool_result.get("tests_hypothesis_id"),
    )

    run_id_str = state.get("run_id")
    tenant_id_str = state.get("tenant_id")

    # If run_service is provided, persist full raw observation and audit event
    if run_service and run_id_str and tenant_id_str:
        try:
            ctx = TenantContext(tenant_id=UUID(tenant_id_str))
            run_uuid = UUID(run_id_str)

            if ok and evidence_id:
                # Calculate deterministic content hash
                payload_data = tool_result.get("data")
                content_hash = hashlib.sha256(
                    json.dumps(payload_data, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()

                captured_at_str = tool_result.get("captured_at")
                captured_at = (
                    datetime.fromisoformat(captured_at_str)
                    if captured_at_str
                    else datetime.now(timezone.utc)
                )

                window_raw = tool_result.get("window") or [captured_at_str, captured_at_str]
                w_start = (
                    datetime.fromisoformat(window_raw[0])
                    if isinstance(window_raw[0], str)
                    else captured_at
                )
                w_end = (
                    datetime.fromisoformat(window_raw[1])
                    if isinstance(window_raw[1], str)
                    else captured_at
                )

                obs = ObservationModel(
                    evidence_id=evidence_id,
                    run_id=run_uuid,
                    tenant_id=ctx.tenant_id,
                    tool=tool_name,
                    source_id=tool_result.get("source", "telemetry"),
                    captured_at=captured_at,
                    window_start=w_start,
                    window_end=w_end,
                    args=raw_args,
                    summary=f"Observation from {tool_name} with {args_summary}"[:250],
                    payload=payload_data if isinstance(payload_data, dict) else {"data": payload_data},
                    content_hash=content_hash,
                )
                if hasattr(run_service, "session") and run_service.session is not None:
                    run_service.session.add(obs)
                    run_service.session.flush()
                if hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "observations"):
                    run_service.run_repo.observations.setdefault(run_uuid, []).append(obs)

            # Emit tool_call audit event
            run_service.append_event(
                ctx=ctx,
                run_id=run_uuid,
                event_type="tool_call",
                actor="investigator",
                payload={
                    "tool": tool_name,
                    "ok": ok,
                    "evidence_id": evidence_id,
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                },
            )
        except Exception as exc:
            logger.error("Failed to persist observation or audit event in record_node: %s", exc)

    updates: dict[str, Any] = {
        "tool_trace": [record_ref.model_dump()],
        "transient_tool_result": None,
    }

    if ok and evidence_id:
        updates["evidence_ids"] = [evidence_id]
    elif not ok:
        updates["last_error"] = {
            "code": error_code or "tool_error",
            "message": tool_result.get("message", "Tool invocation failed"),
        }

    return updates
