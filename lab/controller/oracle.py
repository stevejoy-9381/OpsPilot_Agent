"""Prometheus-backed Oracle diagnostic evaluator.

Used strictly by evaluation graders to assess service health against
scenario-specific thresholds.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx

from lab.controller.models import HealthThresholds, OracleResult


class Oracle:
    """Evaluates service health metrics against scenario thresholds."""

    def __init__(self, prometheus_url: str = "http://prometheus:9090") -> None:
        self.prometheus_url = prometheus_url.rstrip("/")

    async def query_promql(self, query: str) -> float | None:
        """Executes a scalar or vector PromQL query and extracts the float result."""
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params={"query": query})
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if data.get("status") != "success":
                    return None

                result = data.get("data", {}).get("result", [])
                if not result:
                    return None

                # Vector result: [{"metric": {...}, "value": [timestamp, "string_val"]}]
                val_str = result[0].get("value", [None, None])[1]
                if val_str is None:
                    return None
                val = float(val_str)
                if math.isnan(val) or math.isinf(val):
                    return None
                return val
        except Exception:
            return None

    async def oracle_health(
        self,
        thresholds: HealthThresholds,
        window: str = "1m",
    ) -> OracleResult:
        """Queries Prometheus for gateway error rate and p95 latency to evaluate health."""
        # Query 1: Gateway Error Rate
        error_rate_query = (
            f'sum(rate(http_requests_total{{service="gateway", status=~"5.."}}[{window}])) '
            f'/ sum(rate(http_requests_total{{service="gateway"}}[{window}]))'
        )
        # Query 2: Gateway p95 Latency (converted to ms)
        p95_query = (
            f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
            f'{{service="gateway"}}[{window}])) by (le)) * 1000'
        )

        error_rate_val = await self.query_promql(error_rate_query)
        p95_latency_val = await self.query_promql(p95_query)

        # Default to 0.0 if no traffic or errors
        error_rate = error_rate_val if error_rate_val is not None else 0.0
        p95_latency_ms = p95_latency_val if p95_latency_val is not None else 0.0

        is_healthy = (
            error_rate <= thresholds.error_rate
            and p95_latency_ms <= thresholds.p95_latency_ms
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        return OracleResult(
            healthy=is_healthy,
            error_rate=round(error_rate, 4),
            p95_latency_ms=round(p95_latency_ms, 2),
            thresholds={
                "error_rate": thresholds.error_rate,
                "p95_latency_ms": thresholds.p95_latency_ms,
            },
            window=window,
            timestamp=now_iso,
            details={
                "error_rate_query": error_rate_query,
                "p95_query": p95_query,
                "error_rate_breached": error_rate > thresholds.error_rate,
                "p95_latency_breached": p95_latency_ms > thresholds.p95_latency_ms,
            },
        )
