"""scripts/print_s1_context.py

Demonstration and acceptance artifact printing the assembled investigator LLM context
for Scenario S1 at Turn 1 (initial alert ingestion) and Turn 5 (after 4 evidence collection steps).

Uses FakeLLMClient-free fixtures and real registry tool specs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent.graph.context import build_investigator_context
from agent.graph.state import ToolCallRecordRef, create_initial_state
from baseline.alerts.models import AlertEvent


def main() -> None:
    print("=" * 80)
    print("OPSPILOT SCENARIO S1: ASSEMBLED INVESTIGATOR CONTEXT DEMONSTRATION")
    print("=" * 80)
    print()

    # 1. S1 Ingress Threshold Breach Alert
    s1_alert = AlertEvent(
        alert_id="alt_932c5e86cb84",
        fired_at=datetime(2026, 11, 24, 14, 8, 0, tzinfo=timezone.utc),
        service="gateway",
        rule_id="rule_http_high_error_rate",
        metric="error_rate",
        threshold=0.05,
        observed_value=0.28,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )

    # -----------------------------------------------------------------------
    # TURN 1: Initial State (No tools called yet)
    # -----------------------------------------------------------------------
    state_turn_1 = create_initial_state(
        run_id="run_s1_demo_001",
        tenant_id="tenant-default",
        incident_id="inc_20261124_001",
        alert=s1_alert,
        max_calls=8,
    )

    ctx_turn_1 = build_investigator_context(state_turn_1)

    print("################################################################################")
    print("# TURN 1 CONTEXT (Initial Turn)")
    print(f"# Prompt Hash: {ctx_turn_1.prompt_hash}")
    print(f"# Estimated Tokens: {ctx_turn_1.total_tokens_est} (System: {ctx_turn_1.system_prompt_tokens}, User: {ctx_turn_1.user_prompt_tokens})")
    print(f"# Evidence Count: {ctx_turn_1.evidence_count}, Compacted: {ctx_turn_1.compacted}")
    print("################################################################################")
    print("\n--- [SYSTEM PROMPT MESSAGE] ---")
    print(ctx_turn_1.messages[0].content)
    print("\n--- [USER TURN PROMPT MESSAGE] ---")
    print(ctx_turn_1.messages[1].content)
    print("\n" + "=" * 80 + "\n")

    # -----------------------------------------------------------------------
    # TURN 5: State after 4 tool execution steps (S1 investigative sequence)
    # -----------------------------------------------------------------------
    state_turn_5 = create_initial_state(
        run_id="run_s1_demo_001",
        tenant_id="tenant-default",
        incident_id="inc_20261124_001",
        alert=s1_alert,
        max_calls=8,
    )

    # Populate 4 realistic S1 evidence items according to tool_contract.md sequence
    evidence_ids = [
        "ev_metric_a1b2c3d4",
        "ev_metric_e5f6a7b8",
        "ev_log_9c8b7a6f",
        "ev_deploy_1a2b3c4d",
    ]

    tool_trace = [
        ToolCallRecordRef(
            tool="get_metrics",
            args_summary="service=gateway, metric=error_rate",
            duration_ms=42.1,
            ok=True,
            evidence_id="ev_metric_a1b2c3d4",
            approx_tokens=120,
        ).model_dump(),
        ToolCallRecordRef(
            tool="get_metrics",
            args_summary="service=orders, metric=db_pool_in_use",
            duration_ms=38.5,
            ok=True,
            evidence_id="ev_metric_e5f6a7b8",
            approx_tokens=110,
        ).model_dump(),
        ToolCallRecordRef(
            tool="search_logs",
            args_summary="service=orders, min_level=ERROR, event_name=db_pool_timeout",
            duration_ms=78.2,
            ok=True,
            evidence_id="ev_log_9c8b7a6f",
            approx_tokens=220,
        ).model_dump(),
        ToolCallRecordRef(
            tool="list_deploys",
            args_summary="service=orders, limit=5",
            duration_ms=51.0,
            ok=True,
            evidence_id="ev_deploy_1a2b3c4d",
            approx_tokens=180,
        ).model_dump(),
    ]

    history = [
        "Turn 1: Evaluated gateway ingress error rate. Confirmed spike to 0.28 (502 Bad Gateway).",
        "Turn 2: Tested orders connection pool. Confirmed db_pool_in_use pegged at capacity (10.0/10).",
        "Turn 3: Searched orders error logs. Captured db_pool_timeout event: Timeout acquiring connection.",
        "Turn 4: Inspected orders release history. Identified recent deployment deploy_orders_v130 (v1.0.0 -> v1.3.0).",
    ]

    state_turn_5["evidence_ids"] = evidence_ids
    state_turn_5["tool_trace"] = tool_trace
    state_turn_5["step_count"] = 4
    state_turn_5["remaining_calls"] = 4
    state_turn_5["messages_summary"] = "\n".join(history)

    ctx_turn_5 = build_investigator_context(state_turn_5)

    print("################################################################################")
    print("# TURN 5 CONTEXT (After 4 Tool Invocations)")
    print(f"# Prompt Hash: {ctx_turn_5.prompt_hash}")
    print(f"# Estimated Tokens: {ctx_turn_5.total_tokens_est} (System: {ctx_turn_5.system_prompt_tokens}, User: {ctx_turn_5.user_prompt_tokens})")
    print(f"# Evidence Count: {ctx_turn_5.evidence_count}, Compacted: {ctx_turn_5.compacted}")
    print("################################################################################")
    print("\n--- [USER TURN PROMPT MESSAGE] ---")
    print(ctx_turn_5.messages[1].content)
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
