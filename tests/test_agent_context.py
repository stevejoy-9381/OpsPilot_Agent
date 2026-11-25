"""tests/test_agent_context.py

Tests for investigator context builder and prompt version governance.

Verifies:
1. Zero leakage: Assembled context never contains forbidden benchmark strings
   (Feature 1.13 scan across system prompt, user prompt, and topology).
2. Digest compaction: When token budget is exceeded, older entries are compacted
   while protected evidence IDs cited in draft claims are NEVER dropped.
3. Prompt hash: Deterministic SHA-256 hash matches committed value and changes
   whenever the prompt content changes.
4. Token budget cap: Assembled context for a worst-case 8-tool-call run stays
   under the 4,000 token cap using the documented 4 chars/token rough tokenizer.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.graph.context import (
    DEFAULT_MAX_PROMPT_TOKENS,
    EXPECTED_PROMPT_V1_HASH,
    EXPECTED_PROMPT_V2_HASH,
    PROMPT_HASHES_FILE,
    SYSTEM_PROMPT_V1_PATH,
    SYSTEM_PROMPT_V2_PATH,
    LeakageDetectedError,
    build_investigator_context,
    compute_prompt_hash,
    estimate_tokens,
    load_system_prompt,
    scan_for_leakage,
)
from agent.graph.state import ToolCallRecordRef, create_initial_state
from baseline.alerts.models import AlertEvent


@pytest.fixture
def sample_s1_alert() -> AlertEvent:
    return AlertEvent(
        alert_id="alt_orders_932c5e86cb84",
        fired_at=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
        service="gateway",
        rule_id="rule_http_high_error_rate",
        metric="error_rate",
        threshold=0.05,
        observed_value=0.28,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )


def test_system_prompt_and_context_zero_leakage(sample_s1_alert: AlertEvent) -> None:
    """Verifies that system prompt and assembled user context contain zero forbidden benchmark terms."""
    state = create_initial_state(
        run_id="run_zero_leak_01",
        tenant_id="tenant-default",
        incident_id="inc_20261124_001",
        alert=sample_s1_alert,
    )
    assembled = build_investigator_context(state, enforce_zero_leakage=True)

    sys_text = assembled.messages[0].content
    user_text = assembled.messages[1].content

    # Assert scan_for_leakage returns empty list
    assert scan_for_leakage(sys_text) == []
    assert scan_for_leakage(user_text) == []


def test_leakage_detector_triggers_on_forbidden_strings(sample_s1_alert: AlertEvent) -> None:
    """Verifies that scan_for_leakage and build_investigator_context catch forbidden terms."""
    # Direct scan check
    assert len(scan_for_leakage("Investigating pool_leak scenario")) > 0
    assert len(scan_for_leakage("Observed fault in component")) > 0
    assert len(scan_for_leakage("Evaluating against ground_truth")) > 0
    assert len(scan_for_leakage("Running scenario S1")) > 0

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write("System prompt mentioning a software fault and pool_leak.")
        temp_path = Path(f.name)

    try:
        state = create_initial_state(
            run_id="run_leak_test",
            tenant_id="tenant-default",
            incident_id="inc_leak_test",
            alert=sample_s1_alert,
        )
        with pytest.raises(LeakageDetectedError):
            build_investigator_context(
                state, prompt_path=temp_path, enforce_zero_leakage=True
            )
    finally:
        temp_path.unlink(missing_ok=True)


def test_prompt_hash_matches_committed_and_detects_changes() -> None:
    """Verifies that prompt hashes match the committed hashes and change upon edits."""
    prompt_text_v1 = load_system_prompt(SYSTEM_PROMPT_V1_PATH)
    p_hash_v1 = compute_prompt_hash(prompt_text_v1)
    assert p_hash_v1 == EXPECTED_PROMPT_V1_HASH

    prompt_text_v2 = load_system_prompt(SYSTEM_PROMPT_V2_PATH)
    p_hash_v2 = compute_prompt_hash(prompt_text_v2)
    assert p_hash_v2 == EXPECTED_PROMPT_V2_HASH

    # Any modification must produce a different hash
    modified_text = prompt_text_v2 + "\n<!-- Minor prompt revision -->"
    modified_hash = compute_prompt_hash(modified_text)
    assert modified_hash != p_hash_v2


def test_prompt_governance_fails_if_edited_without_changelog_and_hash_bump() -> None:
    """Verifies governance rule: editing a prompt without a hash bump in prompt_hashes.json
    and prompt_changelog.md fails verification.
    """
    assert PROMPT_HASHES_FILE.exists(), f"Committed hash file {PROMPT_HASHES_FILE} is missing!"
    with open(PROMPT_HASHES_FILE, encoding="utf-8") as f:
        committed_hashes = json.load(f)

    changelog_path = Path(__file__).parent.parent / "docs" / "prompt_changelog.md"
    assert changelog_path.exists(), "prompt_changelog.md is missing!"
    changelog_text = changelog_path.read_text(encoding="utf-8")

    prompts_to_check = [
        ("investigator_system_v1.md", SYSTEM_PROMPT_V1_PATH),
        ("investigator_system_v2.md", SYSTEM_PROMPT_V2_PATH),
    ]

    for filename, filepath in prompts_to_check:
        prompt_content = filepath.read_text(encoding="utf-8").strip()
        computed_hash = compute_prompt_hash(prompt_content)

        # 1. Must match committed hash in prompt_hashes.json
        assert filename in committed_hashes, f"{filename} missing from {PROMPT_HASHES_FILE}"
        assert computed_hash == committed_hashes[filename], (
            f"Prompt {filename} was edited without updating committed hash in {PROMPT_HASHES_FILE}! "
            f"Computed: {computed_hash}, Expected: {committed_hashes[filename]}"
        )

        # 2. Must be recorded in prompt_changelog.md
        assert computed_hash in changelog_text, (
            f"Prompt hash {computed_hash} for {filename} is NOT documented in {changelog_path}!"
        )

        # 3. Mutation test: modifying the prompt MUST break comparison against committed hash
        tampered_content = prompt_content + "\n<!-- uncommitted edit -->"
        tampered_hash = compute_prompt_hash(tampered_content)
        assert tampered_hash != committed_hashes[filename]


def test_prompt_v2_zero_leakage() -> None:
    """Verifies that investigator_system_v2.md has ZERO benchmark leakage terms."""
    prompt_text = load_system_prompt(SYSTEM_PROMPT_V2_PATH)
    leaks = scan_for_leakage(prompt_text)
    assert len(leaks) == 0, f"Leakage detected in investigator_system_v2.md: {leaks}"


def test_digest_preserves_cited_ids_under_compaction(sample_s1_alert: AlertEvent) -> None:
    """Verifies that when token cap forces digest compaction, cited evidence IDs are NEVER dropped."""
    state = create_initial_state(
        run_id="run_compact_01",
        tenant_id="tenant-default",
        incident_id="inc_compact_01",
        alert=sample_s1_alert,
        max_calls=8,
    )

    evidence_ids = [f"ev_metric_{i:04d}" for i in range(8)]
    tool_trace = [
        ToolCallRecordRef(
            tool="get_metrics",
            args_summary=f"service=orders, metric=db_pool_in_use, step={i}",
            ok=True,
            evidence_id=f"ev_metric_{i:04d}",
        ).model_dump()
        for i in range(8)
    ]

    state["evidence_ids"] = evidence_ids
    state["tool_trace"] = tool_trace
    state["step_count"] = 8
    state["remaining_calls"] = 0

    # Protected evidence ID cited in draft diagnosis
    protected_id = "ev_metric_0001"  # Older evidence item that would normally be compacted
    state["draft_diagnosis"] = {
        "root_cause_service": "orders",
        "root_cause_category": "bad_deploy",
        "mechanism": "connection_pool_exhaustion",
        "evidence_ids": [protected_id],
        "confidence": 0.9,
    }

    # Force low token cap to trigger compaction
    assembled = build_investigator_context(state, max_prompt_tokens=2500)
    user_content = assembled.messages[1].content

    # Every single evidence ID must still be present in the digest
    for ev_id in evidence_ids:
        assert ev_id in user_content, f"Evidence ID {ev_id} was improperly dropped from digest!"

    # The protected cited ID must NOT have been compacted to '[compacted]'
    lines = user_content.splitlines()
    protected_lines = [line for line in lines if protected_id in line]
    assert len(protected_lines) > 0
    # It must contain its full telemetry data summary rather than [compacted]
    assert "[compacted]" not in protected_lines[0]


def test_worst_case_8_call_run_stays_under_token_cap(sample_s1_alert: AlertEvent) -> None:
    """Verifies that an assembled context for a worst-case 8-call run stays under 4,000 tokens."""
    state = create_initial_state(
        run_id="run_worst_case_token_check",
        tenant_id="tenant-enterprise",
        incident_id="inc_orders_saturation_001",
        alert=sample_s1_alert,
        max_calls=8,
    )

    tools = ["get_metrics", "search_logs", "get_trace", "list_deploys", "get_deploy_diff"]
    evidence_ids = []
    tool_trace = []
    messages = []

    for i in range(8):
        tool = tools[i % len(tools)]
        ev_id = f"ev_{tool}_{i:04x}abcdef"
        evidence_ids.append(ev_id)
        ref = ToolCallRecordRef(
            tool=tool,
            args_summary=f"service=orders, metric=db_pool_in_use, step={i}, window=2026-11-24T12:00:00Z",
            ok=True,
            evidence_id=ev_id,
            approx_tokens=300,
        )
        tool_trace.append(ref.model_dump())
        messages.append(f"Turn {i+1}: Investigated {tool} and observed pertinent telemetry signals.")

    state["evidence_ids"] = evidence_ids
    state["tool_trace"] = tool_trace
    state["step_count"] = 8
    state["remaining_calls"] = 0
    state["messages_summary"] = "\n".join(messages)

    assembled = build_investigator_context(state, max_prompt_tokens=DEFAULT_MAX_PROMPT_TOKENS)

    # State rough tokenizer: len(text) // 4
    rough_tokens = estimate_tokens(
        assembled.messages[0].content + assembled.messages[1].content
    )
    assert rough_tokens == assembled.total_tokens_est

    assert assembled.total_tokens_est <= DEFAULT_MAX_PROMPT_TOKENS, (
        f"Assembled context tokens ({assembled.total_tokens_est}) exceeded cap ({DEFAULT_MAX_PROMPT_TOKENS})"
    )
