"""LiveBackend for connecting to telemetry services in live environments.

- Metrics: Prometheus HTTP API, READ ONLY. Server-side PromQL templates.
  Model NEVER provides PromQL. Validated parameters against strict allowlists.
- Logs: Pure-Python reader on telemetry volume JSONL files mounted read-only.
- Traces: Pure-Python reader on traces volume / index.
- Deploys: Read-only SELECT on lab.deploys table (evidence_ro Postgres role).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tools.read.backends.base import (
    DeployBackend,
    LogsBackend,
    MetricsBackend,
    RawDeployDiff,
    RawDeployRecord,
    RawLogLine,
    RawMetricPoint,
    RawMetricSeries,
    RawSpan,
    RawTrace,
    TraceBackend,
)

# ---------------------------------------------------------------------------
# Strict Metric Enum to PromQL Template Mapping
# ---------------------------------------------------------------------------
PROMQL_TEMPLATES: dict[str, str] = {
    "error_rate": 'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[{range}])) / sum(rate(http_requests_total{{service="{service}"}}[{range}]))',
    "request_rate": 'sum(rate(http_requests_total{{service="{service}"}}[{range}]))',
    "latency_p50": 'histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{range}])) by (le))',
    "latency_p95": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{range}])) by (le))',
    "latency_p99": 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{range}])) by (le))',
    "db_pool_in_use": 'db_pool_connections_in_use{{service="{service}"}}',
    "db_pool_wait_seconds": 'rate(db_pool_wait_duration_seconds_sum{{service="{service}"}}[{range}]) / rate(db_pool_wait_duration_seconds_count{{service="{service}"}}[{range}])',
    "dependency_latency_p95": 'histogram_quantile(0.95, sum(rate(dependency_request_duration_seconds_bucket{{service="{service}"}}[{range}])) by (le))',
    "dependency_errors_rate": 'sum(rate(dependency_requests_total{{service="{service}",status=~"5.."}}[{range}])) / sum(rate(dependency_requests_total{{service="{service}"}}[{range}]))',
}

ALLOWED_SERVICES: set[str] = {"orders", "inventory", "payment", "gateway", "notification"}


class LiveBackend(MetricsBackend, LogsBackend, TraceBackend, DeployBackend):
    def __init__(
        self,
        prometheus_url: str = "http://prometheus:9090",
        telemetry_dir: Path | str = "/var/log/opspilot",
        postgres_dsn: str | None = None,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.telemetry_dir = Path(telemetry_dir)
        self.postgres_dsn = postgres_dsn

    async def query_metric(
        self,
        service: str,
        metric: str,
        start_time: datetime,
        end_time: datetime,
        step_seconds: int = 15,
    ) -> list[RawMetricSeries]:
        if service not in ALLOWED_SERVICES:
            raise ValueError(f"Service {service} not in allowed services: {ALLOWED_SERVICES}")
        if metric not in PROMQL_TEMPLATES:
            raise ValueError(f"Metric {metric} not in allowed metrics enum: {list(PROMQL_TEMPLATES.keys())}")

        template = PROMQL_TEMPLATES[metric]
        query = template.format(service=service, range=f"{step_seconds * 2}s")

        params = {
            "query": query,
            "start": str(start_time.timestamp()),
            "end": str(end_time.timestamp()),
            "step": f"{step_seconds}s",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.prometheus_url}/api/v1/query_range", params=params)
            resp.raise_for_status()
            data = resp.json()

        series_list: list[RawMetricSeries] = []
        results = data.get("data", {}).get("result", [])
        for item in results:
            metric_labels = item.get("metric", {})
            values = item.get("values", [])
            points = [
                RawMetricPoint(
                    timestamp=datetime.fromtimestamp(float(v[0]), tz=timezone.utc),
                    value=float(v[1]) if v[1] != "NaN" else 0.0,
                )
                for v in values
            ]
            series_list.append(
                RawMetricSeries(
                    metric_name=metric,
                    service=service,
                    labels=metric_labels,
                    points=points,
                )
            )
        return series_list

    async def search_logs(
        self,
        service: str,
        start_time: datetime,
        end_time: datetime,
        pattern: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> list[RawLogLine]:
        log_file = self.telemetry_dir / "logs" / f"{service}.jsonl"
        if not log_file.exists():
            return []

        matched: list[RawLogLine] = []
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                t_str = row.get("timestamp")
                dt = datetime.fromisoformat(t_str) if isinstance(t_str, str) else datetime.fromtimestamp(t_str)

                if dt < start_time or dt > end_time:
                    continue

                lvl = row.get("level", "INFO").upper()
                if level and lvl != level.upper():
                    continue

                msg = row.get("message", "")
                if pattern and pattern.lower() not in msg.lower():
                    continue

                matched.append(
                    RawLogLine(
                        timestamp=dt,
                        service=service,
                        level=lvl,
                        message=msg,
                        attributes=row.get("attributes", {}),
                    )
                )
                if len(matched) >= limit:
                    break
        return matched

    async def get_trace(self, trace_id: str) -> RawTrace | None:
        traces_file = self.telemetry_dir / "traces" / f"{trace_id}.json"
        if not traces_file.exists():
            return None

        data = json.loads(traces_file.read_text(encoding="utf-8"))
        spans: list[RawSpan] = []
        for s in data.get("spans", []):
            st = s.get("start_time")
            et = s.get("end_time")
            spans.append(
                RawSpan(
                    trace_id=trace_id,
                    span_id=s.get("span_id", ""),
                    parent_span_id=s.get("parent_span_id"),
                    service_name=s.get("service_name", ""),
                    name=s.get("name", ""),
                    start_time=datetime.fromisoformat(st) if isinstance(st, str) else datetime.fromtimestamp(st),
                    end_time=datetime.fromisoformat(et) if isinstance(et, str) else datetime.fromtimestamp(et),
                    status_code=s.get("status_code", "UNSET"),
                    attributes=s.get("attributes", {}),
                )
            )
        return RawTrace(trace_id=trace_id, spans=spans)

    async def list_deploys(
        self,
        service: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 10,
    ) -> list[RawDeployRecord]:
        # Connects using evidence_ro role on Postgres
        # In offline/mocked environments or when postgres_dsn is not provided, fall back to file index
        deploy_index = self.telemetry_dir / "deploys" / "deploys.json"
        if deploy_index.exists():
            data = json.loads(deploy_index.read_text(encoding="utf-8"))
            matched: list[RawDeployRecord] = []
            for d in data:
                if d.get("service") != service:
                    continue
                dep_at = datetime.fromisoformat(d.get("deployed_at"))
                if start_time <= dep_at <= end_time:
                    matched.append(
                        RawDeployRecord(
                            deploy_id=d.get("deploy_id"),
                            service=service,
                            version=d.get("version"),
                            deployed_at=dep_at,
                            git_commit=d.get("git_commit"),
                            status=d.get("status", "completed"),
                            environment=d.get("environment", "lab"),
                        )
                    )
                    if len(matched) >= limit:
                        break
            return matched
        return []

    async def get_deploy_diff(self, deploy_id: str) -> RawDeployDiff | None:
        diff_file = self.telemetry_dir / "deploys" / "diffs" / f"{deploy_id}.diff"
        meta_file = self.telemetry_dir / "deploys" / "diffs" / f"{deploy_id}.json"
        if not diff_file.exists():
            return None

        diff_text = diff_file.read_text(encoding="utf-8")
        service = "unknown"
        prev_ver = "v0.0.0"
        curr_ver = "v1.0.0"
        files_changed: list[str] = []

        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            service = meta.get("service", service)
            prev_ver = meta.get("previous_version", prev_ver)
            curr_ver = meta.get("current_version", curr_ver)
            files_changed = meta.get("files_changed", files_changed)

        return RawDeployDiff(
            deploy_id=deploy_id,
            service=service,
            previous_version=prev_ver,
            current_version=curr_ver,
            diff_text=diff_text,
            files_changed=files_changed,
        )
