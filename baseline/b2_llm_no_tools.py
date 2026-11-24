"""B2: No-Tools LLM Baseline Runner.

Executes exactly one structured LLM call given an AlertEvent and static architecture
description (gateway -> orders -> inventory). Zero investigative tools, zero telemetry queries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent.llm.gemini import GeminiClient
from agent.llm.interface import (
    BudgetExceededError,
    LLMClient,
    Message,
    StructuredResult,
    TokenBudgetGuard,
)
from baseline.alerts.models import AlertEvent
from baseline.models import BaselineResult
from evals.graders.models import Diagnosis
from shared.actions import ActionType, RecommendedAction

logger = logging.getLogger("opspilot.baseline.b2")

DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "b2_v1.md"
DEFAULT_PRICING_PATH = Path(__file__).parent / "pricing.yaml"
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
FIXED_TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TOKEN_BUDGET = 4096


class B2LLMResponse(BaseModel):
    """Structured response schema expected from the LLM in B2."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: Diagnosis = Field(
        description="Structured root cause diagnosis (must be unknown if uncertain)",
    )
    action: RecommendedAction = Field(
        description="Recommended action from canonical action vocabulary",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-reported assessment confidence between 0.0 and 1.0",
    )
    uncertainty_note: str = Field(
        max_length=500,
        description="Explanation of uncertainty, missing data, or diagnostic limits",
    )


def load_prompt_template(prompt_path: Path | str | None = None) -> tuple[str, str]:
    """Loads prompt template from disk and computes its SHA-256 hash."""
    path = Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found at {path}")
    content = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return content, content_hash


def load_pricing_config(pricing_path: Path | str | None = None) -> dict[str, Any]:
    """Loads model pricing configuration from YAML."""
    path = Path(pricing_path) if pricing_path else DEFAULT_PRICING_PATH
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def calculate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    pricing_config: dict[str, Any] | None = None,
) -> float:
    """Calculates financial cost in USD from token counts and pricing config."""
    cfg = pricing_config or load_pricing_config()
    models = cfg.get("models", {})
    fallback = {"input_cost_per_1m": 0.10, "output_cost_per_1m": 0.40}
    rates = models.get(model_name) or models.get("default", fallback)
    in_rate = float(rates.get("input_cost_per_1m", 0.10))
    out_rate = float(rates.get("output_cost_per_1m", 0.40))
    cost = (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
    return round(float(cost), 6)


def build_b2_prompt(alert: AlertEvent, prompt_path: Path | str | None = None) -> tuple[str, str]:
    """Renders the B2 prompt template with the AlertEvent payload."""
    template, p_hash = load_prompt_template(prompt_path)
    alert_json = json.dumps(alert.model_dump(mode="json"), indent=2)
    rendered = template.replace("{alert_event_json}", alert_json)
    return rendered, p_hash


def run_b2(
    alert: AlertEvent,
    client: LLMClient | None = None,
    scenario_id: str | None = None,
    prompt_path: Path | str | None = None,
    pricing_path: Path | str | None = None,
    model_name: str | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    budget_guard: TokenBudgetGuard | None = None,
    trial: int = 1,
) -> BaselineResult:
    """Executes B2 No-Tools LLM baseline inference for a single alert event.

    Args:
        alert: Triggering incident alert.
        client: LLMClient implementation (defaults to live GeminiClient if API key present).
        scenario_id: Optional scenario identifier for reporting.
        prompt_path: Optional override path to b2_v1.md.
        pricing_path: Optional override path to pricing.yaml.
        model_name: Override model identifier (defaults to GEMINI_MODEL or gemini-2.0-flash).
        max_output_tokens: Output token limit cap.
        timeout_s: Hard execution timeout in seconds.
        budget_guard: TokenBudgetGuard instance.
        trial: Trial index.

    Returns:
        BaselineResult capturing diagnosis, action, tokens, latency, cost, and errors.
    """
    t0 = time.perf_counter()
    target_model = model_name or DEFAULT_MODEL
    guard = budget_guard or TokenBudgetGuard(max_tokens=DEFAULT_TOKEN_BUDGET)

    # 1. Initialize client if not provided
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable is not set. "
                "Live LLM execution cannot proceed without a valid API key."
            )
        client = GeminiClient(
            api_key=SecretStr(api_key),
            model_name=target_model,
            budget_guard=guard,
        )

    # 2. Render prompt & capture hash
    rendered_prompt, prompt_hash = build_b2_prompt(alert, prompt_path)
    messages = [Message(role="user", content=rendered_prompt)]

    # 3. Invoke structured generation
    tokens_in = 0
    tokens_out = 0
    latency_ms = 0.0
    error_type: str | None = None

    try:
        res: StructuredResult[B2LLMResponse] = client.generate_structured(
            messages=messages,
            response_schema=B2LLMResponse,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )
        tokens_in = res.token_usage.input_tokens
        tokens_out = res.token_usage.output_tokens
        latency_ms = res.latency_ms

        if res.is_success and res.parsed is not None:
            parsed = res.parsed
            cost = calculate_cost(
                target_model, tokens_in, tokens_out, load_pricing_config(pricing_path)
            )
            return BaselineResult(
                scenario_id=scenario_id,
                baseline_name="B2",
                alert=alert,
                diagnosis=parsed.diagnosis,
                action=parsed.action,
                tool_calls=[],
                evidence_ids=parsed.diagnosis.evidence_ids,
                rule_matched=None,
                duration_seconds=round(time.perf_counter() - t0, 5),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=round(latency_ms, 2),
                error_type=None,
                uncertainty_note=parsed.uncertainty_note,
                prompt_hash=prompt_hash,
                trial=trial,
            )

        # Handle structured generation error
        if res.error:
            raw_err = res.error.error_type
            if raw_err == "schema_validation_failed":
                error_type = "schema_failure"
            elif raw_err == "timeout":
                error_type = "timeout"
            else:
                error_type = "provider_error"
            err_msg = res.error.message
        else:
            error_type = "provider_error"
            err_msg = "Unknown structured generation error"

    except BudgetExceededError as exc:
        error_type = "provider_error"
        err_msg = f"Token budget exceeded: {exc}"
    except TimeoutError as exc:
        error_type = "timeout"
        err_msg = f"Execution timed out: {exc}"
    except Exception as exc:
        error_type = "provider_error"
        err_msg = f"Unexpected provider error: {exc}"

    # Default abstain & escalate fallback when error occurs
    diag = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        culprit_deploy_id=None,
        evidence_ids=[],
        confidence=0.0,
    )
    act = RecommendedAction(
        action=ActionType.ESCALATE,
        service=None,
        rationale_ids=[],
        notes=f"B2 {error_type}: {err_msg}"[:300],
    )
    cost = calculate_cost(target_model, tokens_in, tokens_out, load_pricing_config(pricing_path))

    return BaselineResult(
        scenario_id=scenario_id,
        baseline_name="B2",
        alert=alert,
        diagnosis=diag,
        action=act,
        tool_calls=[],
        evidence_ids=[],
        rule_matched=None,
        duration_seconds=round(time.perf_counter() - t0, 5),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        latency_ms=round(latency_ms, 2),
        error_type=error_type,
        uncertainty_note=f"Error encountered: {err_msg}",
        prompt_hash=prompt_hash,
        trial=trial,
    )


def run_b2_trials(
    alert: AlertEvent,
    trials: int = 1,
    client: LLMClient | None = None,
    scenario_id: str | None = None,
    **kwargs: Any,
) -> list[BaselineResult]:
    """Runs B2 for N trials (default 1) and returns all results."""
    results: list[BaselineResult] = []
    for t in range(1, trials + 1):
        res = run_b2(alert, client=client, scenario_id=scenario_id, trial=t, **kwargs)
        results.append(res)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="OpsPilot B2 No-Tools LLM Baseline Runner")
    parser.add_argument("--scenario", default="all", help="Scenario ID (S1-S5 or all)")
    parser.add_argument(
        "--trials", type=int, default=1, help="Number of trials per scenario (default: 1)"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model identifier")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "ERROR: GEMINI_API_KEY environment variable is not set. Live execution halted.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"B2 Runner initialized with model={args.model}, trials={args.trials}")


if __name__ == "__main__":
    main()
