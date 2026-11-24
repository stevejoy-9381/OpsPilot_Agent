"""SnapshotBackend reading exported evidence directories.

Reads evidence directories created by export_evidence.py (pure file reads, offline, deterministic).
Directory layout expected:
  snapshot_dir/
    metrics/<service>_<metric>.json
    logs/<service>.jsonl
    traces/<trace_id>.json
    deploys/deploys.json
    deploys/diffs/<deploy_id>.diff
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

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


class SnapshotBackend(MetricsBackend, LogsBackend, TraceBackend, DeployBackend):
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)

    async def query_metric(
        self,
        service: str,
        metric: str,
        start_time: datetime,
        end_time: datetime,
        step_seconds: int = 15,
    ) -> list[RawMetricSeries]:
        metric_file = self.root_dir / "metrics" / f"{service}_{metric}.json"
        if not metric_file.exists():
            return []

        data = json.loads(metric_file.read_text(encoding="utf-8"))
        series_list: list[RawMetricSeries] = []

        raw_series = data if isinstance(data, list) else data.get("series", [data])
        for s in raw_series:
            points: list[RawMetricPoint] = []
            for p in s.get("points", []):
                t_str = p.get("timestamp")
                val = float(p.get("value", 0.0))
                dt = datetime.fromisoformat(t_str) if isinstance(t_str, str) else datetime.fromtimestamp(t_str)
                if start_time <= dt <= end_time:
                    points.append(RawMetricPoint(timestamp=dt, value=val))

            series_list.append(
                RawMetricSeries(
                    metric_name=s.get("metric_name", metric),
                    service=s.get("service", service),
                    labels=s.get("labels", {}),
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
        log_file = self.root_dir / "logs" / f"{service}.jsonl"
        if not log_file.exists():
            return []

        matched: list[RawLogLine] = []
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    # Robustness requirement 7: ignore / skip corrupt JSONL lines
                    continue
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
        trace_file = self.root_dir / "traces" / f"{trace_id}.json"
        if not trace_file.exists():
            return None

        data = json.loads(trace_file.read_text(encoding="utf-8"))
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
        deploys_file = self.root_dir / "deploys" / "deploys.json"
        if not deploys_file.exists():
            return []

        data = json.loads(deploys_file.read_text(encoding="utf-8"))
        matched: list[RawDeployRecord] = []
        for d in data:
            if d.get("service") != service:
                continue
            dep_at_str = d.get("deployed_at")
            dep_at = (
                datetime.fromisoformat(dep_at_str)
                if isinstance(dep_at_str, str)
                else datetime.fromtimestamp(dep_at_str)
            )
            if start_time <= dep_at <= end_time:
                matched.append(
                    RawDeployRecord(
                        deploy_id=d.get("deploy_id", ""),
                        service=service,
                        version=d.get("version", ""),
                        deployed_at=dep_at,
                        git_commit=d.get("git_commit", ""),
                        status=d.get("status", "completed"),
                        environment=d.get("environment", "lab"),
                    )
                )
                if len(matched) >= limit:
                    break
        return matched

    async def get_deploy_diff(self, deploy_id: str) -> RawDeployDiff | None:
        diff_file = self.root_dir / "deploys" / "diffs" / f"{deploy_id}.diff"
        meta_file = self.root_dir / "deploys" / "diffs" / f"{deploy_id}.json"
        if not diff_file.exists():
            return None

        diff_text = diff_file.read_text(encoding="utf-8")
        service = "unknown"
        prev_ver = "v0.0.0"
        curr_ver = "v1.0.0"
        files_changed = []

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
