"""agent/graph/graph.py

LangGraph StateGraph assembly for the OpsPilot Incident Investigator Agent.

Graph Shape:
------------
START -> load_context -> plan_step -> [route_after_plan_step]
    -> call_tool -> record -> plan_step
    -> conclude -> finalize -> END

Constraints:
1. Conditional edges are pure functions of state.
2. Hard recursion/step limit set as a backstop beyond MAX_TOOL_CALLS (default 25 turns).
3. Flexible checkpointer support: MemorySaver for offline tests, PostgresSaver for
   stateful execution on the 'checkpoints' schema with thread_id = run_id.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.graph.handoff import HandoffPort
from agent.graph.nodes import (
    ExecutionPort,
    await_execution_node,
    await_review_node,
    call_tool_node,
    conclude_node,
    finalize_node,
    load_context_node,
    plan_step_node,
    propose_node,
    record_node,
    update_ledger_node,
)
from agent.graph.state import (
    InvestigationState,
    TerminationReason,
)
from agent.llm.interface import LLMClient
from apps.api.services.run_service import RunService
from tools.registry import ToolRegistry

logger = logging.getLogger("opspilot.agent.graph")

DEFAULT_RECURSION_LIMIT: int = 50


def route_after_plan_step(state: InvestigationState) -> str:
    """Pure conditional edge routing from plan_step to call_tool, update_ledger, or conclude."""
    # Check if a termination condition was flagged
    termination_reason = state.get("termination_reason")
    if termination_reason in (
        TerminationReason.INVALID_STEP_CAP_REACHED.value,
        TerminationReason.PROVIDER_ERROR_EXHAUSTED.value,
        TerminationReason.TOKEN_BUDGET_EXCEEDED.value,
        TerminationReason.TIMEOUT_EXCEEDED.value,
        TerminationReason.BUDGET_EXHAUSTED.value,
        TerminationReason.STEP_CAP_REACHED.value,
    ):
        return "conclude"

    last_decision = state.get("last_step_decision") or {}
    step_type = last_decision.get("step")
    remaining_calls = state.get("remaining_calls", 8)
    tool_trace = state.get("tool_trace") or []
    ledger_update_count = state.get("ledger_update_count", 0)

    if step_type == "update_hypotheses":
        return "update_ledger"

    if step_type == "call_tool":
        if remaining_calls > 0:
            return "call_tool"
        return "conclude"

    if step_type == "conclude":
        # Mandatory ledger step: after the first tool result, route through update_ledger
        # at least once before any conclude is accepted (enforced in router for v2 runs).
        is_v2 = state.get("schema_version") == "v2"
        if is_v2 and len(tool_trace) > 0 and ledger_update_count == 0:
            logger.info("Router refusing conclude: mandatory ledger update step required after tool results")
            return "plan_step"
        return "conclude"

    # If an invalid step occurred and cap not reached yet, retry planning
    if state.get("invalid_step_count", 0) < 3 and not termination_reason:
        return "plan_step"

    # Default fallback
    return "conclude"


def route_after_conclude(state: InvestigationState) -> str:
    """Pure conditional edge routing from conclude node to plan_step (on gate retry) or finalize."""
    if state.get("status") == "gate_rejected":
        return "plan_step"
    return "finalize"


def route_after_finalize(state: InvestigationState) -> str:
    """Routes v2 diagnosed non-false-alarm runs to propose unless a handoff_port already handled them."""
    if state.get("handoff_result"):
        return "end"
    if state.get("schema_version") == "v2":
        diag = state.get("draft_diagnosis") or {}
        if diag.get("outcome_kind") == "diagnosed" and diag.get("root_cause_category") != "false_alarm":
            return "propose"
    return "end"


def route_after_propose(state: InvestigationState) -> str:
    """Routes to await_review interrupt node when a plan was proposed (needs_review)."""
    if state.get("status") == "needs_review":
        return "await_review"
    return "end"


def route_after_await_review(state: InvestigationState) -> str:
    """Deterministic post-resume routing: approved -> await_execution; else -> end."""
    if state.get("approval_decision") == "approved":
        return "await_execution"
    return "end"


def build_investigator_graph(
    llm_client: LLMClient | None = None,
    run_service: RunService | None = None,
    tool_registry: ToolRegistry | None = None,
    handoff_port: HandoffPort | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    checkpointer_type: str = "memory",
    postgres_conn_string: str | None = None,
    backend: Any = None,
    target_reader: Any = None,
    plan_persister: Any = None,
    db_clock_fn: Any = None,
    plan_reader: Any = None,
    execution_port: ExecutionPort | None = None,
) -> CompiledStateGraph:
    """Assembles and compiles the OpsPilot Investigator LangGraph StateGraph.

    Args:
        llm_client: LLM client implementation.
        run_service: RunService for database operations and audit events.
        tool_registry: Tool registry for dispatching read-only tools.
        handoff_port: Handoff port implementation.
        checkpointer: Pre-configured BaseCheckpointSaver instance.
        checkpointer_type: "memory" or "postgres".
        postgres_conn_string: PostgreSQL connection string if checkpointer_type="postgres".
    """
    workflow = StateGraph(InvestigationState)

    # 1. Define Nodes with bound dependencies
    def _load_context_node(state: InvestigationState) -> dict[str, Any]:
        return load_context_node(state=state, run_service=run_service)

    def _plan_step_node(state: InvestigationState) -> dict[str, Any]:
        if llm_client is None:
            raise RuntimeError("Cannot execute plan_step: LLMClient is not provided")
        return plan_step_node(state=state, llm_client=llm_client, run_service=run_service)

    def _call_tool_node(state: InvestigationState) -> dict[str, Any]:
        return call_tool_node(state=state, tool_registry=tool_registry, backend=backend)

    def _record_node(state: InvestigationState) -> dict[str, Any]:
        return record_node(state=state, run_service=run_service)

    def _update_ledger_node(state: InvestigationState) -> dict[str, Any]:
        return update_ledger_node(state=state, run_service=run_service)

    def _conclude_node(state: InvestigationState) -> dict[str, Any]:
        return conclude_node(state=state, run_service=run_service)

    def _finalize_node(state: InvestigationState) -> dict[str, Any]:
        return finalize_node(
            state=state,
            run_service=run_service,
            handoff_port=handoff_port,
        )

    def _propose_node(state: InvestigationState) -> dict[str, Any]:
        if llm_client is None:
            raise RuntimeError("Cannot execute propose: LLMClient is not provided")
        return propose_node(
            state=state,
            llm_client=llm_client,
            run_service=run_service,
            target_reader=target_reader,
            plan_persister=plan_persister,
            db_clock_fn=db_clock_fn,
        )

    def _await_review_node(state: InvestigationState) -> dict[str, Any]:
        return await_review_node(state=state, plan_reader=plan_reader)

    def _await_execution_node(state: InvestigationState) -> dict[str, Any]:
        return await_execution_node(state=state, execution_port=execution_port)

    # 2. Add nodes to workflow
    workflow.add_node("load_context", _load_context_node)
    workflow.add_node("plan_step", _plan_step_node)
    workflow.add_node("call_tool", _call_tool_node)
    workflow.add_node("record", _record_node)
    workflow.add_node("update_ledger", _update_ledger_node)
    workflow.add_node("conclude", _conclude_node)
    workflow.add_node("finalize", _finalize_node)
    workflow.add_node("propose", _propose_node)
    workflow.add_node("await_review", _await_review_node)
    workflow.add_node("await_execution", _await_execution_node)

    # 3. Add Edges
    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "plan_step")

    workflow.add_conditional_edges(
        "plan_step",
        route_after_plan_step,
        {
            "call_tool": "call_tool",
            "update_ledger": "update_ledger",
            "conclude": "conclude",
            "plan_step": "plan_step",
        },
    )

    workflow.add_edge("call_tool", "record")
    workflow.add_edge("record", "plan_step")
    workflow.add_edge("update_ledger", "plan_step")
    workflow.add_conditional_edges(
        "conclude",
        route_after_conclude,
        {
            "plan_step": "plan_step",
            "finalize": "finalize",
        },
    )
    workflow.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {
            "propose": "propose",
            "end": END,
        },
    )
    workflow.add_conditional_edges(
        "propose",
        route_after_propose,
        {
            "await_review": "await_review",
            "end": END,
        },
    )
    workflow.add_conditional_edges(
        "await_review",
        route_after_await_review,
        {
            "await_execution": "await_execution",
            "end": END,
        },
    )
    workflow.add_edge("await_execution", END)

    # 4. Resolve checkpointer
    actual_checkpointer: BaseCheckpointSaver
    if checkpointer is not None:
        actual_checkpointer = checkpointer
    elif checkpointer_type == "postgres" and postgres_conn_string:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        # Ensure search_path=checkpoints is set and dict_row/prepare_threshold=0 match PostgresSaver API
        conn = psycopg.connect(
            postgres_conn_string,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        with conn.cursor() as cur:
            cur.execute("SET search_path = checkpoints, public;")
        actual_checkpointer = PostgresSaver(conn)
        actual_checkpointer.setup()
    else:
        actual_checkpointer = MemorySaver()

    return workflow.compile(checkpointer=actual_checkpointer)


def run_investigation_graph(
    graph: CompiledStateGraph,
    initial_state: InvestigationState,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> InvestigationState:
    """Convenience helper to invoke a compiled investigation graph with configured thread_id and limits."""
    run_id = initial_state.get("run_id", "default_thread")
    config = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": recursion_limit,
    }
    result = graph.invoke(initial_state, config=config)
    return cast(InvestigationState, result)
