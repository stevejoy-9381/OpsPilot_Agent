"""agent/graph/gate.py

Deterministic Contrary-Evidence Gate and Outcome Resolver for Feature 6.

Mechanical, non-LLM validation checkpoint:
1. evaluate_gate: checks rules (a)-(e) per docs/hypothesis_design.md:
   - Rule A: Dual hypothesis registered or certified false alarm exception (failure: no_rival)
   - Rule B: Refutation/discrimination test dispatched and linked (failure: no_discriminating_test)
   - Rule C: All cited evidence grounded in store, in incident window, from allowed tools (failure: unsupported_citation)
   - Rule D: Multi-source independent telemetry for leading hypothesis (failure: single_source_support)
   - Cosmetic rivals check: rival created but never tested or linked (failure: rival_not_linked)
2. evaluate_confidence_cap: evaluates data-driven caps from confidence_caps.yaml.
3. resolve_outcome: maps state, gate result, tool errors, and budget state to typed outcomes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from agent.graph.ledger import (
    HypothesisLedger,
    HypothesisRecord,
    leading_hypothesis,
    rivals,
)
from baseline.alerts.models import AlertEvent
from evals.graders.models import EvidenceItem

logger = logging.getLogger("opspilot.agent.gate")

CONFIDENCE_CAPS_FILE = Path(__file__).parent / "confidence_caps.yaml"


class GateFailureCode(str, Enum):
    """Standardized failure codes emitted when gate rules are violated."""

    NO_RIVAL = "no_rival"
    NO_DISCRIMINATING_TEST = "no_discriminating_test"
    UNSUPPORTED_CITATION = "unsupported_citation"
    SINGLE_SOURCE_SUPPORT = "single_source_support"
    RIVAL_NOT_LINKED = "rival_not_linked"


TOOL_SOURCE_KIND_MAP: dict[str, str] = {
    "get_metrics": "metrics",
    "prometheus": "metrics",
    "search_logs": "logs",
    "logs": "logs",
    "get_trace": "traces",
    "traces": "traces",
    "list_deploys": "deploys",
    "get_deploy_diff": "deploys",
    "diff": "deploys",
    "deploys": "deploys",
}

ALLOWED_TOOL_SOURCES: set[str] = set(TOOL_SOURCE_KIND_MAP.keys())


class GateResult(BaseModel):
    """Result envelope from evaluate_gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failures: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


OutcomeKind = Literal["diagnosed", "unknown", "escalate"]


class Outcome(BaseModel):
    """Authoritative outcome decision for an investigated incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OutcomeKind
    reason: str
    confidence_cap: float
    confidence_label: str
    details: dict[str, Any] = Field(default_factory=dict)


def _load_caps_table() -> list[dict[str, Any]]:
    """Loads the confidence cap data table from YAML."""
    if not CONFIDENCE_CAPS_FILE.exists():
        # Fallback inline default
        return [
            {"id": "tool_error_during_discriminating_test", "max_confidence": 0.30, "category": "low"},
            {"id": "no_rival_tested", "max_confidence": 0.30, "category": "low"},
            {"id": "unrefuted_contrary_evidence", "max_confidence": 0.30, "category": "low"},
            {"id": "single_source_support", "max_confidence": 0.60, "category": "medium"},
            {"id": "gate_satisfied_few_evidence", "max_confidence": 0.75, "category": "medium"},
            {"id": "gate_satisfied_strong", "max_confidence": 0.95, "category": "high"},
            {"id": "false_alarm_certified", "max_confidence": 0.90, "category": "high"},
        ]
    with open(CONFIDENCE_CAPS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return cast(list[dict[str, Any]], data.get("caps", []))


def confidence_label_to_float(label_or_val: str | float | int | None) -> float:
    """Converts a confidence label ('high', 'medium', 'low') or numeric value to float."""
    if isinstance(label_or_val, (int, float)):
        return max(0.0, min(1.0, float(label_or_val)))
    if isinstance(label_or_val, str):
        norm = label_or_val.strip().lower()
        if norm == "high":
            return 0.90
        if norm == "medium":
            return 0.60
        if norm == "low":
            return 0.30
        try:
            return max(0.0, min(1.0, float(norm)))
        except ValueError:
            return 0.0
    return 0.0


def evaluate_confidence_cap(
    ledger: HypothesisLedger | dict[str, Any],
    tool_trace: Sequence[Any] | None = None,
    evidence_store: Sequence[Any] | dict[str, Any] | None = None,
    is_false_alarm: bool = False,
    tool_errors: Sequence[Any] | None = None,
) -> tuple[float, str, str]:
    """Pure function computing deterministic confidence cap per Section 6 of hypothesis_design.md.

    Returns:
        (max_confidence, category_label, matched_cap_id)
    """
    trace_list = list(tool_trace or [])
    errors_list = list(tool_errors or [])
    
    # 1. Normalize ledger
    norm_ledger = normalize_ledger(ledger)
    lead = leading_hypothesis(norm_ledger)

    # Condition 1: Any tool error occurred (during a discriminating test or stale/unavailable telemetry)
    has_tool_error_in_test = False
    for item in trace_list:
        call = item if isinstance(item, dict) else item.model_dump() if hasattr(item, "model_dump") else {}
        if not call.get("ok", True) or call.get("error_code"):
            has_tool_error_in_test = True
            break
    if errors_list or has_tool_error_in_test:
        return (0.30, "low", "tool_error_during_discriminating_test")

    if lead is None:
        return (0.30, "low", "no_rival_tested")

    # Condition 2: No rival hypothesis was tested or falsified
    rivs = rivals(norm_ledger)
    rivals_tested = any(len(r.tests_run) > 0 or len(r.contrary_evidence_ids) > 0 for r in rivs)
    if not is_false_alarm and rivs and not rivals_tested:
        return (0.30, "low", "no_rival_tested")

    # Condition 3: Unrefuted contrary evidence remains linked to leading hypothesis
    if len(lead.contrary_evidence_ids) > 0:
        return (0.30, "low", "unrefuted_contrary_evidence")

    # Condition: False alarm certified
    if is_false_alarm or lead.category == "false_alarm":
        return (0.90, "high", "false_alarm_certified")

    # Condition 4: Leading hypothesis supported by only a single source kind
    supporting_kinds: set[str] = set()
    ev_map = _index_evidence_store(evidence_store)
    for ev_id in lead.supporting_evidence_ids:
        ev_item = ev_map.get(ev_id)
        if ev_item:
            source = _get_source_kind(ev_item)
            if source:
                supporting_kinds.add(source)

    if len(supporting_kinds) < 2:
        return (0.60, "medium", "single_source_support")

    # Condition 5: Gate satisfied with 1 rival refuted, but total evidence items < 3
    total_evidence_count = len(lead.supporting_evidence_ids)
    if total_evidence_count < 3:
        return (0.75, "medium", "gate_satisfied_few_evidence")

    # Condition 6: Gate satisfied strong: >= 2 source kinds, rival refuted, zero errors, >= 3 items
    return (0.95, "high", "gate_satisfied_strong")


def normalize_ledger(ledger: HypothesisLedger | dict[str, Any] | tuple[Any, ...]) -> HypothesisLedger:
    """Ensures ledger is a HypothesisLedger instance."""
    if isinstance(ledger, HypothesisLedger):
        return ledger
    if isinstance(ledger, tuple) and len(ledger) >= 1 and isinstance(ledger[0], HypothesisLedger):
        return ledger[0]
    if isinstance(ledger, dict):
        raw_items: dict[str, Any]
        if "hypotheses" in ledger and isinstance(ledger["hypotheses"], dict):
            raw_items = ledger["hypotheses"]
        else:
            raw_items = ledger

        hyps: dict[str, HypothesisRecord] = {}
        for k, v in raw_items.items():
            if isinstance(v, HypothesisRecord):
                hyps[k] = v
            elif isinstance(v, dict):
                clean_v = dict(v)
                if "id" in clean_v and "hypothesis_id" not in clean_v:
                    clean_v["hypothesis_id"] = clean_v.pop("id")
                elif "id" in clean_v:
                    clean_v.pop("id", None)
                hyps[k] = HypothesisRecord(**clean_v)
        return HypothesisLedger(hypotheses=hyps)
    return HypothesisLedger()


def _index_evidence_store(evidence_store: Sequence[Any] | dict[str, Any] | None) -> dict[str, Any]:
    """Indexes evidence store into a map of evidence_id -> item."""
    indexed: dict[str, Any] = {}
    if not evidence_store:
        return indexed
    if isinstance(evidence_store, dict):
        if "items" in evidence_store and isinstance(evidence_store["items"], list):
            items = evidence_store["items"]
        else:
            items = list(evidence_store.values())
    else:
        items = list(evidence_store)

    for item in items:
        if isinstance(item, EvidenceItem):
            indexed[item.evidence_id] = item
        elif isinstance(item, dict):
            ev_id = str(item.get("evidence_id", ""))
            if ev_id:
                indexed[ev_id] = item
        elif hasattr(item, "evidence_id"):
            indexed[str(item.evidence_id)] = item
    return indexed


def _get_source_kind(evidence_item: Any) -> str | None:
    """Resolves canonical source kind (metrics, logs, traces, deploys) from an evidence item."""
    raw_source = ""
    if isinstance(evidence_item, EvidenceItem):
        raw_source = evidence_item.source
    elif isinstance(evidence_item, dict):
        raw_source = evidence_item.get("source") or evidence_item.get("tool", "")
    elif hasattr(evidence_item, "source"):
        raw_source = str(evidence_item.source)
    elif hasattr(evidence_item, "tool"):
        raw_source = str(evidence_item.tool)

    norm = raw_source.lower().strip()
    return TOOL_SOURCE_KIND_MAP.get(norm)


def _extract_incident_window(alert: AlertEvent | dict[str, Any] | None) -> tuple[datetime, datetime] | None:
    """Extracts incident window from alert context."""
    if alert is None:
        return None
    alert_dict = alert.model_dump() if isinstance(alert, AlertEvent) else dict(alert)
    fired_at_raw = alert_dict.get("fired_at")
    if not fired_at_raw:
        return None
    if isinstance(fired_at_raw, str):
        fired_at = datetime.fromisoformat(fired_at_raw)
    else:
        fired_at = fired_at_raw

    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)

    window_sec = int(alert_dict.get("window_seconds", 300))
    start = fired_at - timedelta(seconds=window_sec * 2)
    end = fired_at + timedelta(seconds=window_sec * 2)
    return (start, end)


def is_discriminating_test(
    call: dict[str, Any],
    ledger: HypothesisLedger,
    leading_id: str | None,
) -> bool:
    """Determines whether a tool call qualifies as a discriminating / refutation test.

    A test counts as discriminating only if:
    1. Targeted a hypothesis via tests_hypothesis_id.
    2. Was dispatched and succeeded or returned a typed error.
    3. Its evidence (or error) was later linked as 'contradicts' (or 'supports' a rival) in the ledger.
    """
    tests_hyp_id = call.get("tests_hypothesis_id")
    if not tests_hyp_id:
        return False

    ev_id = call.get("evidence_id")
    err_code = call.get("error_code")
    ok = call.get("ok", True)

    if not ok and not err_code:
        return False

    if not ev_id:
        # If call produced no evidence, it cannot be linked
        return False

    # Check if this evidence was linked in the ledger
    for hyp_id, hyp in ledger.hypotheses.items():
        if hyp_id == tests_hyp_id:
            # If tested hypothesis has this evidence linked as contrary, it's discriminating!
            if ev_id in hyp.contrary_evidence_ids:
                return True
        elif hyp_id != tests_hyp_id:
            # If a rival hypothesis has this evidence linked as supporting, it discriminated against tests_hyp_id!
            if ev_id in hyp.supporting_evidence_ids:
                return True

    # If tests_hyp_id is leading_id, and evidence is in leading contrary or rival supporting
    if leading_id and tests_hyp_id == leading_id:
        lead = ledger.hypotheses.get(leading_id)
        if lead and ev_id in lead.contrary_evidence_ids:
            return True

    return False


def _check_false_alarm_baseline_return(
    lead: HypothesisRecord,
    ev_map: dict[str, Any],
    alert: AlertEvent | dict[str, Any] | None,
) -> bool:
    """Verifies that metrics evidence demonstrates return to baseline after the alert window."""
    has_baseline_return = False
    for ev_id in lead.supporting_evidence_ids:
        item = ev_map.get(ev_id)
        if not item:
            continue
        source_kind = _get_source_kind(item)
        if source_kind != "metrics":
            continue

        # Extract data payload
        data: dict[str, Any] = {}
        if isinstance(item, EvidenceItem):
            data = item.data
        elif isinstance(item, dict):
            data = item.get("data") or item.get("payload") or {}
        elif hasattr(item, "payload") and isinstance(item.payload, dict):
            data = item.payload
        elif hasattr(item, "payload") and hasattr(item.payload, "model_dump"):
            data = item.payload.model_dump()
        elif hasattr(item, "data") and isinstance(item.data, dict):
            data = item.data
        elif hasattr(item, "data") and hasattr(item.data, "model_dump"):
            data = item.data.model_dump()

        # Unwrap nested {"data": MetricsPayload | dict} envelope if stored by record_node
        if isinstance(data, dict) and "data" in data and "summary" not in data:
            inner = data["data"]
            if isinstance(inner, dict):
                data = inner
            elif hasattr(inner, "model_dump"):
                data = inner.model_dump()

        # Check explicit baseline indicators
        if data.get("returned_to_baseline") is True:
            has_baseline_return = True
            break
        if data.get("transient_burst") is True:
            has_baseline_return = True
            break

        # Numeric check if flat metric_value/baseline are present
        current_val = data.get("metric_value") or data.get("value")
        baseline_raw = data.get("baseline")
        threshold_raw = data.get("threshold")
        if isinstance(current_val, (int, float)) and isinstance(baseline_raw, (int, float)) and baseline_raw > 0:
            if current_val <= 1.10 * baseline_raw:
                has_baseline_return = True
                break
        if isinstance(current_val, (int, float)) and isinstance(threshold_raw, (int, float)) and threshold_raw > 0:
            if current_val <= 1.10 * threshold_raw:
                has_baseline_return = True
                break

        # MetricsPayload shape from tools/read/metrics.py: summary.last vs baseline.summary.mean (or alert.threshold)
        summary = data.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("last"), (int, float)):
            last_val = float(summary["last"])
            base_val: float | None = None
            if isinstance(baseline_raw, dict):
                b_sum = baseline_raw.get("summary")
                if isinstance(b_sum, dict) and isinstance(b_sum.get("mean"), (int, float)):
                    base_val = float(b_sum["mean"])
            if base_val is None and alert is not None:
                a_dict = alert.model_dump() if isinstance(alert, AlertEvent) else dict(alert)
                if isinstance(a_dict.get("threshold"), (int, float)):
                    base_val = float(a_dict["threshold"])
            if base_val is not None and base_val > 0 and last_val <= 1.10 * base_val:
                has_baseline_return = True
                break
            # Fallback: falling trend returning within 1.10x of initial pre-burst value
            if summary.get("trend") == "falling" and isinstance(summary.get("first"), (int, float)) and summary["first"] > 0:
                if last_val <= 1.10 * float(summary["first"]):
                    has_baseline_return = True
                    break

    return has_baseline_return


def evaluate_gate(
    ledger: HypothesisLedger | dict[str, Any],
    tool_trace: Sequence[Any] | None,
    evidence_store: Sequence[Any] | dict[str, Any] | None,
    alert: AlertEvent | dict[str, Any] | None,
) -> GateResult:
    """Mechanical, deterministic gate validating whether an investigation may conclude.

    Checks:
    - Rule (a): At least two distinct hypotheses ever registered, OR certified false-alarm exception.
      Failure: 'no_rival'.
    - Rule (b): At least one tool call was aimed at refuting/discriminating the leading hypothesis.
      Failure: 'no_discriminating_test'.
    - Cosmetic rivals: A rival was registered but was never tested or never linked.
      Failure: 'rival_not_linked'.
    - Rule (c): All cited supporting evidence exists in store, in incident window, from allowed tools.
      Failure: 'unsupported_citation'.
    - Rule (d): Multi-source independent telemetry (metrics, logs, traces, deploys).
      Failure: 'single_source_support'.
    """
    failures: list[str] = []
    details: dict[str, Any] = {}

    norm_ledger = normalize_ledger(ledger)
    trace_list = [
        item if isinstance(item, dict) else item.model_dump() if hasattr(item, "model_dump") else {}
        for item in (tool_trace or [])
    ]
    ev_map = _index_evidence_store(evidence_store)
    window = _extract_incident_window(alert)

    lead = leading_hypothesis(norm_ledger)
    all_hyps = list(norm_ledger.hypotheses.values())
    riv_list = rivals(norm_ledger)

    if lead is None:
        return GateResult(
            passed=False,
            failures=[GateFailureCode.NO_RIVAL.value],
            details={"error": "No hypotheses formulated on ledger"},
        )

    is_false_alarm_claim = lead.category == "false_alarm"

    # -------------------------------------------------------------
    # Rule (a): Dual Hypothesis or False-Alarm Exception
    # -------------------------------------------------------------
    has_dual_hypothesis = len(all_hyps) >= 2
    valid_false_alarm_exception = False

    if is_false_alarm_claim:
        # Check false-alarm baseline return
        valid_false_alarm_exception = _check_false_alarm_baseline_return(lead, ev_map, alert)
        details["benign_baseline_return"] = valid_false_alarm_exception

    if not has_dual_hypothesis and not valid_false_alarm_exception:
        failures.append(GateFailureCode.NO_RIVAL.value)
        details["rule_a"] = "Fewer than 2 hypotheses formulated and alert does not satisfy false-alarm exception"

    # -------------------------------------------------------------
    # Rule (b): Refutation / Discrimination Test Dispatched
    # -------------------------------------------------------------
    discriminating_calls = [
        call for call in trace_list if is_discriminating_test(call, norm_ledger, lead.id)
    ]
    details["discriminating_calls_count"] = len(discriminating_calls)

    if not discriminating_calls and not (is_false_alarm_claim and valid_false_alarm_exception):
        failures.append(GateFailureCode.NO_DISCRIMINATING_TEST.value)
        details["rule_b"] = "No tool call dispatched aimed at refuting or discriminating hypotheses"

    # -------------------------------------------------------------
    # Cosmetic Rivals Check: rival created but never tested or linked
    # -------------------------------------------------------------
    cosmetic_rivals: list[str] = []
    for r in riv_list:
        was_targeted = any(call.get("tests_hypothesis_id") == r.id for call in trace_list) or len(r.tests_run) > 0
        has_linked_ev = len(r.supporting_evidence_ids) > 0 or len(r.contrary_evidence_ids) > 0
        if not was_targeted and not has_linked_ev:
            cosmetic_rivals.append(r.id)

    if cosmetic_rivals and not is_false_alarm_claim:
        failures.append(GateFailureCode.RIVAL_NOT_LINKED.value)
        details["cosmetic_rivals"] = cosmetic_rivals
        details["rule_cosmetic_rival"] = f"Rival hypotheses {cosmetic_rivals} were never tested or linked"

    # -------------------------------------------------------------
    # Rule (c): Grounded Citations & Incident Window Validity
    # -------------------------------------------------------------
    unsupported_citations: list[str] = []
    out_of_window_citations: list[str] = []
    unauthorized_tool_citations: list[str] = []

    for ev_id in lead.supporting_evidence_ids:
        ev_item = ev_map.get(ev_id)
        if not ev_item:
            unsupported_citations.append(ev_id)
            continue

        # Check authorized tool
        raw_tool = ""
        if isinstance(ev_item, EvidenceItem):
            raw_tool = ev_item.source
        elif isinstance(ev_item, dict):
            raw_tool = ev_item.get("source") or ev_item.get("tool", "")
        elif hasattr(ev_item, "source"):
            raw_tool = str(ev_item.source)
        elif hasattr(ev_item, "tool"):
            raw_tool = str(ev_item.tool)

        if raw_tool.lower().strip() not in ALLOWED_TOOL_SOURCES:
            unauthorized_tool_citations.append(ev_id)

        # Check window overlap
        if window is not None:
            win_start: datetime | None = None
            win_end: datetime | None = None
            if isinstance(ev_item, EvidenceItem):
                win_start = ev_item.timestamp
                win_end = ev_item.timestamp
            elif isinstance(ev_item, dict):
                ws = ev_item.get("window_start") or ev_item.get("timestamp")
                we = ev_item.get("window_end") or ev_item.get("timestamp")
                if isinstance(ws, str):
                    win_start = datetime.fromisoformat(ws)
                elif isinstance(ws, datetime):
                    win_start = ws
                if isinstance(we, str):
                    win_end = datetime.fromisoformat(we)
                elif isinstance(we, datetime):
                    win_end = we
            elif hasattr(ev_item, "window_start") and hasattr(ev_item, "window_end"):
                win_start = ev_item.window_start
                win_end = ev_item.window_end

            if win_start and win_end:
                if win_start.tzinfo is None:
                    win_start = win_start.replace(tzinfo=timezone.utc)
                if win_end.tzinfo is None:
                    win_end = win_end.replace(tzinfo=timezone.utc)

                # Overlap check: win_start <= window[1] and win_end >= window[0]
                if win_end < window[0] or win_start > window[1]:
                    out_of_window_citations.append(ev_id)

    if unsupported_citations or out_of_window_citations or unauthorized_tool_citations:
        failures.append(GateFailureCode.UNSUPPORTED_CITATION.value)
        details["unsupported_citations"] = unsupported_citations
        details["out_of_window_citations"] = out_of_window_citations
        details["unauthorized_tool_citations"] = unauthorized_tool_citations

    # -------------------------------------------------------------
    # Rule (d): Independent Multi-Source Telemetry
    # -------------------------------------------------------------
    supporting_kinds: set[str] = set()
    for ev_id in lead.supporting_evidence_ids:
        ev_item = ev_map.get(ev_id)
        if ev_item:
            sk = _get_source_kind(ev_item)
            if sk:
                supporting_kinds.add(sk)

    details["supporting_kinds"] = list(supporting_kinds)

    if is_false_alarm_claim:
        # False alarm requires metrics return to baseline
        if not valid_false_alarm_exception:
            if GateFailureCode.SINGLE_SOURCE_SUPPORT.value not in failures:
                failures.append(GateFailureCode.SINGLE_SOURCE_SUPPORT.value)
            details["rule_d"] = "False alarm claim requires metrics showing return to baseline post-alert"
    else:
        if len(supporting_kinds) < 2:
            failures.append(GateFailureCode.SINGLE_SOURCE_SUPPORT.value)
            details["rule_d"] = (
                f"Leading hypothesis cites evidence from {len(supporting_kinds)} source kind ({supporting_kinds}); "
                "at least 2 independent kinds (metrics, logs, traces, deploys) are required."
            )

    # Deduplicate failures preserving order
    dedup_failures: list[str] = []
    for f in failures:
        if f not in dedup_failures:
            dedup_failures.append(f)

    passed = len(dedup_failures) == 0
    return GateResult(passed=passed, failures=dedup_failures, details=details)


def resolve_outcome(
    ledger: HypothesisLedger | dict[str, Any],
    gate: GateResult,
    tool_errors: Sequence[Any] | None = None,
    budget_state: dict[str, Any] | None = None,
    alert: AlertEvent | dict[str, Any] | None = None,
) -> Outcome:
    """Pure deterministic outcome resolver mapping state to standardized outcomes per Section 5.

    Decision Order:
    1. Multiple concurrent causes across microservices -> escalate: multiple_concurrent_causes
    2. Tool/backend errors blocking telemetry -> unknown: evidence_unavailable
    3. Gate passed:
       - False alarm verified -> diagnosed: false_alarm_no_action (confidence cap 0.90)
       - Standard root cause -> diagnosed: concluded_by_model (confidence capped deterministically)
    4. Gate failed:
       - Budget exhausted -> unknown: budget_exhausted
       - Contradictory evidence on leading -> unknown: contradictory_evidence
       - Retry limit reached -> unknown: gate_not_satisfied
       - Otherwise -> unknown: insufficient_evidence
    """
    norm_ledger = normalize_ledger(ledger)
    lead = leading_hypothesis(norm_ledger)
    errors = list(tool_errors or [])
    b_state = budget_state or {}

    # 1. Multiple concurrent causes check: 2 or more supported hypotheses on different services
    supported_hyps = [h for h in norm_ledger.hypotheses.values() if h.status == "supported"]
    supported_services = {h.service for h in supported_hyps if h.service}
    if len(supported_services) >= 2:
        return Outcome(
            kind="escalate",
            reason="multiple_concurrent_causes",
            confidence_cap=0.30,
            confidence_label="low",
            details={"supported_services": list(supported_services)},
        )

    # 2. Critical tool errors blocking query
    if errors:
        return Outcome(
            kind="unknown",
            reason="evidence_unavailable",
            confidence_cap=0.30,
            confidence_label="low",
            details={"tool_errors": errors},
        )

    # 3. Gate passed
    if gate.passed and lead is not None:
        if lead.category == "false_alarm":
            return Outcome(
                kind="diagnosed",
                reason="false_alarm_no_action",
                confidence_cap=0.90,
                confidence_label="high",
                details={"category": "false_alarm"},
            )

        cap, label, matched_rule = evaluate_confidence_cap(
            norm_ledger,
            tool_trace=b_state.get("tool_trace"),
            evidence_store=b_state.get("evidence_store"),
            is_false_alarm=False,
            tool_errors=errors,
        )
        return Outcome(
            kind="diagnosed",
            reason="concluded_by_model",
            confidence_cap=cap,
            confidence_label=label,
            details={"cap_rule": matched_rule},
        )

    # 4. Gate failed or no leading hypothesis
    is_budget_exhausted = (
        b_state.get("remaining_calls", 8) <= 0
        or b_state.get("step_cap_reached", False)
        or b_state.get("is_exhausted", False)
    )

    if is_budget_exhausted:
        return Outcome(
            kind="unknown",
            reason="budget_exhausted",
            confidence_cap=0.0,
            confidence_label="none",
            details={"failures": gate.failures},
        )

    if lead is not None and len(lead.contrary_evidence_ids) > 0:
        return Outcome(
            kind="unknown",
            reason="contradictory_evidence",
            confidence_cap=0.30,
            confidence_label="low",
            details={"contrary_evidence_ids": list(lead.contrary_evidence_ids)},
        )

    if b_state.get("gate_rejections_exhausted", False) or "gate_not_satisfied" in gate.failures:
        return Outcome(
            kind="unknown",
            reason="gate_not_satisfied",
            confidence_cap=0.0,
            confidence_label="none",
            details={"failures": gate.failures},
        )

    return Outcome(
        kind="unknown",
        reason="insufficient_evidence",
        confidence_cap=0.60,
        confidence_label="medium",
        details={"failures": gate.failures},
    )
