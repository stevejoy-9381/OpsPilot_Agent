"""Central bounds and limits configuration for investigation tools.

Loaded from environment variables with safe defaults. Configured in one place only.
"""

import os

from pydantic import BaseModel, ConfigDict, Field


class ToolLimitsConfig(BaseModel):
    """Central configuration for tool limits and budgets."""

    model_config = ConfigDict(frozen=True)

    max_window_seconds: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_WINDOW_SECONDS", "3600")),
        description="Maximum query window duration in seconds (default: 1 hour).",
    )
    max_metric_points: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_METRIC_POINTS", "30")),
        description="Maximum metric datapoints before downsampling is applied.",
    )
    default_log_rows: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_DEFAULT_LOG_ROWS", "25")),
        description="Default number of log rows returned if unconstrained.",
    )
    max_log_rows: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_LOG_ROWS", "50")),
        description="Maximum allowed log rows returned in a single query.",
    )
    max_log_chars_per_line: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_LOG_CHARS_PER_LINE", "250")),
        description="Maximum character length per log message before truncation.",
    )
    default_diff_chars: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_DEFAULT_DIFF_CHARS", "2000")),
        description="Default truncation limit for deployment diff characters.",
    )
    max_diff_chars: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_DIFF_CHARS", "4000")),
        description="Maximum allowed deployment diff characters.",
    )
    max_trace_spans: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_TRACE_SPANS", "20")),
        description="Maximum spans returned for a single trace.",
    )
    target_token_budget: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_TARGET_TOKEN_BUDGET", "1500")),
        description="Target maximum token budget per tool response.",
    )
    max_token_budget: int = Field(
        default_factory=lambda: int(os.environ.get("OPSPILOT_MAX_TOKEN_BUDGET", "2500")),
        description="Hard maximum token ceiling per tool response.",
    )


# Global singleton instance loaded from environment
tool_limits = ToolLimitsConfig()
