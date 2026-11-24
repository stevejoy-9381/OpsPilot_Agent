"""Tests for tools/read/backends."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

from tools.read.backends.factory import get_backend
from tools.read.backends.live import LiveBackend
from tools.read.backends.snapshot import SnapshotBackend


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    """Create a structured snapshot directory with sample telemetry."""
    # Metrics
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    metric_data = {
        "metric_name": "error_rate",
        "service": "orders",
        "labels": {"job": "orders"},
        "points": [
            {"timestamp": "2026-03-30T10:00:00Z", "value": 0.02},
            {"timestamp": "2026-03-30T10:05:00Z", "value": 0.15},
            {"timestamp": "2026-03-30T10:10:00Z", "value": 0.45},
        ],
    }
    (metrics_dir / "orders_error_rate.json").write_text(json.dumps(metric_data), encoding="utf-8")

    # Logs
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    log_lines = [
        {"timestamp": "2026-03-30T10:01:00Z", "level": "INFO", "message": "Order created 123", "attributes": {}},
        {"timestamp": "2026-03-30T10:05:00Z", "level": "ERROR", "message": "Database connection timeout", "attributes": {}},
    ]
    with open(logs_dir / "orders.jsonl", "w", encoding="utf-8") as f:
        for line in log_lines:
            f.write(json.dumps(line) + "\n")

    # Traces
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir(parents=True)
    trace_data = {
        "trace_id": "tr_abc123",
        "spans": [
            {
                "span_id": "sp_001",
                "parent_span_id": None,
                "service_name": "orders",
                "name": "POST /order",
                "start_time": "2026-03-30T10:05:00Z",
                "end_time": "2026-03-30T10:05:02Z",
                "status_code": "ERROR",
                "attributes": {"http.status_code": 500},
            }
        ],
    }
    (traces_dir / "tr_abc123.json").write_text(json.dumps(trace_data), encoding="utf-8")

    # Deploys
    deploys_dir = tmp_path / "deploys"
    diffs_dir = deploys_dir / "diffs"
    diffs_dir.mkdir(parents=True)
    deploys_data = [
        {
            "deploy_id": "dep_456",
            "service": "orders",
            "version": "v1.2.3",
            "deployed_at": "2026-03-30T09:45:00Z",
            "git_commit": "abcdef123",
            "status": "completed",
            "environment": "lab",
        }
    ]
    (deploys_dir / "deploys.json").write_text(json.dumps(deploys_data), encoding="utf-8")
    (diffs_dir / "dep_456.diff").write_text("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
    (diffs_dir / "dep_456.json").write_text(
        json.dumps(
            {
                "service": "orders",
                "previous_version": "v1.2.2",
                "current_version": "v1.2.3",
                "files_changed": ["app.py"],
            }
        ),
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# SnapshotBackend Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_backend_query_metric(snapshot_dir: Path) -> None:
    backend = SnapshotBackend(snapshot_dir)
    start = datetime(2026, 3, 30, 9, 59, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 8, 0, tzinfo=timezone.utc)

    series = await backend.query_metric("orders", "error_rate", start, end)
    assert len(series) == 1
    assert series[0].metric_name == "error_rate"
    # Points at 10:00 and 10:05 fall within window; 10:10 is excluded
    assert len(series[0].points) == 2
    assert series[0].points[0].value == 0.02
    assert series[0].points[1].value == 0.15


@pytest.mark.asyncio
async def test_snapshot_backend_search_logs(snapshot_dir: Path) -> None:
    backend = SnapshotBackend(snapshot_dir)
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 10, 0, tzinfo=timezone.utc)

    # Search by pattern
    logs = await backend.search_logs("orders", start, end, pattern="timeout")
    assert len(logs) == 1
    assert logs[0].level == "ERROR"
    assert "timeout" in logs[0].message

    # Search by level
    info_logs = await backend.search_logs("orders", start, end, level="INFO")
    assert len(info_logs) == 1
    assert info_logs[0].level == "INFO"


@pytest.mark.asyncio
async def test_snapshot_backend_get_trace(snapshot_dir: Path) -> None:
    backend = SnapshotBackend(snapshot_dir)
    trace = await backend.get_trace("tr_abc123")
    assert trace is not None
    assert trace.trace_id == "tr_abc123"
    assert len(trace.spans) == 1
    assert trace.spans[0].name == "POST /order"


@pytest.mark.asyncio
async def test_snapshot_backend_deploys(snapshot_dir: Path) -> None:
    backend = SnapshotBackend(snapshot_dir)
    start = datetime(2026, 3, 30, 9, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)

    deploys = await backend.list_deploys("orders", start, end)
    assert len(deploys) == 1
    assert deploys[0].deploy_id == "dep_456"

    diff = await backend.get_deploy_diff("dep_456")
    assert diff is not None
    assert diff.previous_version == "v1.2.2"
    assert "--- a/app.py" in diff.diff_text


# ---------------------------------------------------------------------------
# LiveBackend Validation & Mocked Query Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_backend_disallows_unauthorized_service_and_metrics() -> None:
    backend = LiveBackend()
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="not in allowed services"):
        await backend.query_metric("malicious_service", "error_rate", start, end)

    with pytest.raises(ValueError, match="not in allowed metrics enum"):
        await backend.query_metric("orders", "drop_table_users", start, end)


@pytest.mark.asyncio
async def test_live_backend_query_metric_shape_matches_snapshot(snapshot_dir: Path) -> None:
    """Compare LiveBackend with mock Prometheus response against SnapshotBackend."""
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 10, 0, tzinfo=timezone.utc)

    # 1. Query SnapshotBackend
    snap_backend = SnapshotBackend(snapshot_dir)
    snap_series = await snap_backend.query_metric("orders", "error_rate", start, end)

    # 2. Mock Prometheus response for LiveBackend
    mock_resp = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": "orders"},
                    "values": [
                        [str(start.timestamp()), "0.02"],
                        [str(start.timestamp() + 300), "0.15"],
                    ],
                }
            ],
        },
    }

    live_backend = LiveBackend()
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: mock_resp
        mock_get.return_value = mock_response

        live_series = await live_backend.query_metric("orders", "error_rate", start, end)

    assert len(live_series) == len(snap_series) == 1
    assert live_series[0].metric_name == snap_series[0].metric_name == "error_rate"
    assert live_series[0].service == snap_series[0].service == "orders"
    assert len(live_series[0].points) == 2


# ---------------------------------------------------------------------------
# Factory Tests
# ---------------------------------------------------------------------------
def test_backend_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Default is snapshot
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.delenv("EVIDENCE_BACKEND", raising=False)
    backend = get_backend()
    assert isinstance(backend, SnapshotBackend)

    # Explicit live
    monkeypatch.setenv("EVIDENCE_BACKEND", "live")
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    live_backend = get_backend()
    assert isinstance(live_backend, LiveBackend)
    assert live_backend.prometheus_url == "http://127.0.0.1:9090"
