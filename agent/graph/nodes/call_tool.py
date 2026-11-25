"""agent/graph/nodes/call_tool.py

LangGraph node executing authorized read-only investigation tools via ToolRegistry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.graph.state import InvestigationState
from tools.common.models import ToolContext, ToolError, ToolResult, ToolSuccess
from tools.registry import ToolRegistry, registry

logger = logging.getLogger("opspilot.agent.call_tool")


def _get_window_bounds(state: InvestigationState) -> tuple[datetime, datetime]:
    """Determines window start and end bounds from state alert."""
    alert = state.get("alert") or {}
    fired_at_raw = alert.get("fired_at")
    if fired_at_raw:
        if isinstance(fired_at_raw, str):
            fired_at = datetime.fromisoformat(fired_at_raw)
        else:
            fired_at = fired_at_raw
    else:
        fired_at = datetime.now(timezone.utc)

    window_sec = int(alert.get("window_seconds", 300))
    start = fired_at - timedelta(seconds=window_sec * 2)
    end = fired_at + timedelta(seconds=window_sec * 2)
    return start, end


def call_tool_node(
    state: InvestigationState,
    tool_registry: ToolRegistry | None = None,
    backend: Any = None,
) -> dict[str, Any]:
    """Executes the tool call proposed in the model's last step decision."""
    last_step = state.get("last_step_decision") or {}
    tool_name = last_step.get("tool")
    tool_args = last_step.get("args") or {}

    if not tool_name:
        logger.warning("call_tool_node invoked without tool in last_step_decision")
        return {}

    start_window, end_window = _get_window_bounds(state)
    remaining_calls = state.get("remaining_calls", 8)

    ctx = ToolContext(
        run_id=state.get("run_id", "default_run"),
        tenant_id=state.get("tenant_id", "default_tenant"),
        incident_id=state.get("incident_id", "default_incident"),
        window_start=start_window,
        window_end=end_window,
        allowed_services=["gateway", "orders", "inventory"],
        remaining_calls=remaining_calls,
    )

    reg = tool_registry or registry
    start_time = time.monotonic()

    # Dispatch tool execution (bridging async if called in sync context)
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                res: ToolResult = executor.submit(
                    asyncio.run,
                    reg.dispatch(tool_name, tool_args, ctx, backend=backend),
                ).result()
        else:
            res = asyncio.run(reg.dispatch(tool_name, tool_args, ctx, backend=backend))
    except Exception as exc:
        logger.error("Error executing tool %s: %s", tool_name, exc)
        duration_ms = (time.monotonic() - start_time) * 1000.0
        return {
            "transient_tool_result": {
                "ok": False,
                "tool": tool_name,
                "duration_ms": duration_ms,
                "error_code": "execution_failed",
                "message": str(exc),
                "args": tool_args,
            },
            "remaining_calls": max(0, remaining_calls - 1),
        }

    duration_ms = (time.monotonic() - start_time) * 1000.0

    if isinstance(res, ToolSuccess):
        win = res.window
        if hasattr(win, "start") and hasattr(win, "end"):
            window_repr = [win.start.isoformat(), win.end.isoformat()]
        else:
            window_repr = [str(getattr(win, "start", "")), str(getattr(win, "end", ""))]
        result_dict = {
            "ok": True,
            "tool": res.tool,
            "evidence_id": res.evidence_id,
            "duration_ms": duration_ms,
            "captured_at": res.captured_at.isoformat(),
            "window": window_repr,
            "source": res.source,
            "data": res.data,
            "tests_hypothesis_id": last_step.get("tests_hypothesis_id"),
            "args": tool_args,
        }
    elif isinstance(res, ToolError):
        result_dict = {
            "ok": False,
            "tool": tool_name,
            "duration_ms": duration_ms,
            "error_code": res.error.code,
            "message": res.error.message,
            "details": res.error.details,
            "tests_hypothesis_id": last_step.get("tests_hypothesis_id"),
            "args": tool_args,
        }
    else:
        result_dict = {
            "ok": False,
            "tool": tool_name,
            "duration_ms": duration_ms,
            "error_code": "unknown_result_type",
            "args": tool_args,
        }

    updates: dict[str, Any] = {
        "transient_tool_result": result_dict,
        "remaining_calls": ctx.remaining_calls,
    }

    if isinstance(res, ToolSuccess):
        tests_hyp_id = last_step.get("tests_hypothesis_id")
        if tests_hyp_id:
            import hashlib
            import json

            args_hash = hashlib.sha256(
                json.dumps(tool_args, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:8]

            test_run_entry = {
                "evidence_id": res.evidence_id,
                "tool": res.tool,
                "args_hash": args_hash,
                "expected_if_true": last_step.get("expected_if_true"),
                "expected_if_false": last_step.get("expected_if_false"),
            }

            current_ledger = state.get("hypotheses_ledger") or {}
            if tests_hyp_id in current_ledger:
                hyp_entry = current_ledger[tests_hyp_id]
                if hasattr(hyp_entry, "model_dump"):
                    hyp_dict = hyp_entry.model_dump()
                elif isinstance(hyp_entry, dict):
                    hyp_dict = dict(hyp_entry)
                else:
                    hyp_dict = {"id": tests_hyp_id}

                existing_tests = list(hyp_dict.get("tests_run") or [])
                existing_tests.append(test_run_entry)
                hyp_dict["tests_run"] = existing_tests
                updates["hypotheses_ledger"] = {tests_hyp_id: hyp_dict}

    return updates
