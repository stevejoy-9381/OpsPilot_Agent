"""Live integration tests for investigation tools against running lab containers.

Marked @pytest.mark.lab to run in GitHub Codespaces or CI lab workflows.
Skips automatically when running locally without a live Prometheus/Postgres lab.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
import pytest
import httpx

from tools.common.models import ToolContext, ToolSuccess
from tools.read.backends.live import LiveBackend
from tools.read.deploys import get_deploy_diff, list_deploys
from tools.read.logs import search_logs
from tools.read.metrics import get_metrics
from tools.read.traces import get_trace


def _is_lab_reachable() -> bool:
    prom_url = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    try:
        resp = httpx.get(f"{prom_url}/-/ready", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(
        not _is_lab_reachable(),
        reason="Live lab environment not running (requires Codespaces / Docker runtime).",
    ),
]


@pytest.fixture
def live_tool_env() -> tuple[LiveBackend, ToolContext]:
    backend = LiveBackend(
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
        telemetry_dir=os.getenv("TELEMETRY_DIR", "/var/log/opspilot"),
    )
    ctx = ToolContext(
        run_id="run_live_001",
        tenant_id="tenant_live",
        incident_id="inc_live_s1",
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
        allowed_services={"orders", "inventory", "gateway"},
        remaining_calls=8,
    )
    return backend, ctx


@pytest.mark.asyncio
async def test_live_get_metrics_shape(live_tool_env: Any) -> None:
    backend, ctx = live_tool_env
    res = await get_metrics(ctx=ctx, service="orders", metric="request_rate", backend=backend)
    assert isinstance(res, ToolSuccess)
    assert res.data.metric == "request_rate"
    assert res.data.service == "orders"
    assert res.units == "requests_per_second"


@pytest.mark.asyncio
async def test_live_search_logs_shape(live_tool_env: Any) -> None:
    backend, ctx = live_tool_env
    res = await search_logs(ctx=ctx, service="orders", limit=5, backend=backend)
    assert isinstance(res, ToolSuccess)
    assert res.data.service == "orders"
    assert isinstance(res.data.rows, list)
    assert hasattr(res.data, "aggregation")
