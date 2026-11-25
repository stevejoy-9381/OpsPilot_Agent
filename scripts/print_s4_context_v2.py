"""scripts/print_s4_context_v2.py

Demonstration and acceptance artifact printing the assembled investigator LLM context (v2)
for Scenario S4 at Turn 1 (initial alert ingestion) and Turn 3 (mid-run with dual hypotheses and telemetry).

Uses offline fixtures without calling live LLMs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent.graph.context import build_investigator_context, scan_for_leakage
from agent.graph.ledger import HypothesisLedger, add_hypothesis, link_evidence
from agent.graph.state import ToolCallRecordRef, create_initial_state
from baseline.alerts.models import AlertEvent


def main() -> None:
    print("=" * 80)
    print("OPSPILOT SCENARIO S4: ASSEMBLED INVESTIGATOR CONTEXT (V2) DEMONSTRATION")
    print("=" * 80)
    print()

    # 1. S4 Threshold Breach Alert (Inventory latency breach)
    s4_alert = AlertEvent(
        alert_id="alt_inv_482c9183da01",
        fired_at=datetime(2026, 11, 24, 12, 0, 30, tzinfo=timezone.utc),
        service="inventory",
        rule_id="rule_inventory_p95_latency",
        metric="latency_p95",
        threshold=0.35,
        observed_value=0.78,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )

    # -----------------------------------------------------------------------
    # TURN 1: Initial State (No tools dispatched yet)
    # -----------------------------------------------------------------------
    state_turn_1 = create_initial_state(
        run_id="run_s4_v2_demo_001",
        tenant_id="tenant-default",
        incident_id="inc_20261124_s4_001",
        alert=s4_alert,
        max_calls=8,
        schema_version="v2",
    )

    ctx_turn_1 = build_investigator_context(state_turn_1)

    # Verify zero leakage on Turn 1
    sys_leaks_1 = scan_for_leakage(ctx_turn_1.messages[0].content)
    user_leaks_1 = scan_for_leakage(ctx_turn_1.messages[1].content)
    assert not sys_leaks_1, f"Turn 1 system prompt leakage: {sys_leaks_1}"
    assert not user_leaks_1, f"Turn 1 user content leakage: {user_leaks_1}"

    print("################################################################################")
    print("# TURN 1 CONTEXT (Initial Turn)")
    print(f"# Prompt Hash: {ctx_turn_1.prompt_hash}")
    print(
        f"# Estimated Tokens: {ctx_turn_1.total_tokens_est} "
        f"(System: {ctx_turn_1.system_prompt_tokens}, User: {ctx_turn_1.user_prompt_tokens})"
    )
    print(f"# Evidence Count: {ctx_turn_1.evidence_count}, Compacted: {ctx_turn_1.compacted}")
    print("################################################################################")
    print("\n--- [SYSTEM PROMPT MESSAGE] ---")
    print(ctx_turn_1.messages[0].content)
    print("\n--- [USER TURN PROMPT MESSAGE] ---")
    print(ctx_turn_1.messages[1].content)
    print()

    # -----------------------------------------------------------------------
    # MID-RUN TURN: Turn 3 (After 2 tool calls and hypothesis formulation)
    # -----------------------------------------------------------------------
    state_mid = create_initial_state(
        run_id="run_s4_v2_demo_001",
        tenant_id="tenant-default",
        incident_id="inc_20261124_s4_001",
        alert=s4_alert,
        max_calls=8,
        schema_version="v2",
    )

    # Formulate dual hypotheses
    ledger = HypothesisLedger()
    ledger, h1 = add_hypothesis(
        ledger,
        statement="Orders v1.1.0 release introduced downstream latency degradation",
        category="bad_deploy",
        mechanism="cache_bypass_latency",
        service="orders",
        step=2,
    )
    ledger, h2 = add_hypothesis(
        ledger,
        statement="Inventory cache connection timeout to in-memory store",
        category="dependency_failure",
        mechanism="dependency_timeout",
        service="inventory",
        step=2,
    )

    # Evidence collected in turns 1 and 2
    ev1 = "ev_deploys_001"
    ev2 = "ev_trace_002"

    ledger, _ = link_evidence(ledger, h1.id, ev1, "supports")
    ledger, _ = link_evidence(ledger, h2.id, ev2, "supports")

    state_mid["hypotheses_ledger"] = {
        h1.id: h1.to_summary_dict(),
        h2.id: h2.to_summary_dict(),
    }
    state_mid["evidence_ids"] = [ev1, ev2]
    state_mid["tool_trace"] = [
        ToolCallRecordRef(
            tool="list_deploys",
            args_summary="service=orders, window=600s",
            ok=True,
            evidence_id=ev1,
            approx_tokens=150,
        ).model_dump(),
        ToolCallRecordRef(
            tool="get_trace",
            args_summary="service=inventory, limit=5",
            ok=True,
            evidence_id=ev2,
            approx_tokens=220,
        ).model_dump(),
    ]
    state_mid["step_count"] = 2
    state_mid["remaining_calls"] = 6
    state_mid["ledger_update_count"] = 1
    state_mid["messages_summary"] = (
        "Turn 1: call_tool(list_deploys) - Observed orders v1.1.0 release 25s prior to breach.\n"
        "Turn 2: update_hypotheses(2 ops) - Formulated competing hypotheses: orders release vs inventory cache timeout."
    )

    ctx_mid = build_investigator_context(state_mid)

    # Verify zero leakage on mid-run turn
    sys_leaks_mid = scan_for_leakage(ctx_mid.messages[0].content)
    user_leaks_mid = scan_for_leakage(ctx_mid.messages[1].content)
    assert not sys_leaks_mid, f"Mid-run system prompt leakage: {sys_leaks_mid}"
    assert not user_leaks_mid, f"Mid-run user content leakage: {user_leaks_mid}"

    print("################################################################################")
    print("# MID-RUN TURN CONTEXT (Turn 3: Post-Telemetry & Dual Hypotheses Formulated)")
    print(f"# Prompt Hash: {ctx_mid.prompt_hash}")
    print(
        f"# Estimated Tokens: {ctx_mid.total_tokens_est} "
        f"(System: {ctx_mid.system_prompt_tokens}, User: {ctx_mid.user_prompt_tokens})"
    )
    print(f"# Evidence Count: {ctx_mid.evidence_count}, Compacted: {ctx_mid.compacted}")
    print("################################################################################")
    print("\n--- [USER TURN PROMPT MESSAGE] ---")
    print(ctx_mid.messages[1].content)
    print()


if __name__ == "__main__":
    main()
