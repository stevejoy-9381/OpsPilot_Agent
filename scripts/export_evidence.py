#!/usr/bin/env python3
"""Evidence export utility and degradations hook for OpsPilot lab.

Exports strictly what the agent is allowed to see during an incident window:
- Prometheus range metrics (JSON and CSV per metric)
- Filtered structured logs (JSONL per service)
- Filtered distributed trace spans (JSONL)
- Filtered deployment history (JSON and CSV)

Excludes:
- Vault database files and records
- Scenario definition files and metadata
- Controller endpoints and internal state
- Admin state and control routes
- Load generator summaries
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lab.controller_lib.deploy import DeployRecord, get_deploy_history

STANDARD_LAB_METRICS: list[str] = [
    "http_requests_total",
    "http_request_duration_seconds_bucket",
    "http_request_duration_seconds_count",
    "http_request_duration_seconds_sum",
    "dependency_call_duration_seconds_bucket",
    "dependency_call_duration_seconds_count",
    "dependency_call_duration_seconds_sum",
    "dependency_errors_total",
    "db_pool_in_use",
    "db_pool_size",
    "db_pool_wait_seconds_bucket",
    "db_pool_wait_seconds_count",
    "db_pool_wait_seconds_sum",
]

FORBIDDEN_EXPORT_FILENAMES: set[str] = {
    "vault.db",
    "vault.db-journal",
    "summary.json",
    "truth.json",
}


def parse_timestamp(val: str | datetime | float | int) -> datetime:
    """Parses various timestamp representations into a UTC-aware datetime."""
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val), tz=timezone.utc)
    if isinstance(val, str):
        v = val.strip()
        try:
            # Check numeric epoch
            epoch_num = float(v)
            return datetime.fromtimestamp(epoch_num, tz=timezone.utc)
        except ValueError:
            pass
        # ISO format
        clean_iso = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"Cannot parse timestamp: {val!r}")


def _parse_span_timestamp(span: dict[str, Any]) -> datetime | None:
    """Extracts timestamp from an OpenTelemetry or JSON span representation."""
    # OTel JSON collector format: startTimeUnixNano
    nano = span.get("startTimeUnixNano") or span.get("start_time_unix_nano")
    if nano is not None:
        try:
            sec = int(nano) / 1_000_000_000.0
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (ValueError, TypeError):
            pass

    # Direct ISO ts or timestamp
    ts_val = span.get("ts") or span.get("timestamp") or span.get("start_time")
    if ts_val is not None:
        try:
            return parse_timestamp(ts_val)
        except Exception:
            pass

    return None


def export_prometheus_metrics(
    start: datetime,
    end: datetime,
    metrics_dir: Path,
    prometheus_url: str | None = None,
    mock_data: dict[str, Any] | None = None,
    step: str = "5s",
) -> dict[str, int]:
    """Queries Prometheus range data and exports CSV + JSON per metric."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, int] = {}

    start_epoch = start.timestamp()
    end_epoch = end.timestamp()

    if mock_data is not None:
        for metric_name, data in mock_data.items():
            json_file = metrics_dir / f"{metric_name}.json"
            csv_file = metrics_dir / f"{metric_name}.csv"

            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            series_list = data.get("data", {}).get("result", [])
            row_count = _write_metrics_csv(csv_file, metric_name, series_list)
            summary[metric_name] = row_count
        return summary

    base_url = (prometheus_url or os.getenv("PROMETHEUS_URL") or "http://prometheus:9090").rstrip("/")

    # Discover metrics to query
    metrics_to_query = list(STANDARD_LAB_METRICS)
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{base_url}/api/v1/label/__name__/values")
            if resp.status_code == 200:
                discovered = resp.json().get("data", [])
                for d in discovered:
                    if d not in metrics_to_query:
                        metrics_to_query.append(d)
    except Exception:
        pass

    with httpx.Client(timeout=10.0) as client:
        for metric_name in metrics_to_query:
            query_url = f"{base_url}/api/v1/query_range"
            params: dict[str, str | float] = {
                "query": metric_name,
                "start": str(start_epoch),
                "end": str(end_epoch),
                "step": step,
            }
            try:
                resp = client.get(query_url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    json_file = metrics_dir / f"{metric_name}.json"
                    csv_file = metrics_dir / f"{metric_name}.csv"

                    with open(json_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)

                    series_list = data.get("data", {}).get("result", [])
                    row_count = _write_metrics_csv(csv_file, metric_name, series_list)
                    summary[metric_name] = row_count
            except Exception:
                # Omit metrics that fail or are inactive
                continue

    return summary


def _write_metrics_csv(csv_file: Path, metric_name: str, series_list: list[dict[str, Any]]) -> int:
    """Writes flattened Prometheus series data to CSV."""
    rows = []
    all_label_keys: set[str] = set()

    for series in series_list:
        labels = series.get("metric", {})
        all_label_keys.update(labels.keys())
        values = series.get("values", [])
        for point in values:
            if len(point) >= 2:
                rows.append({"timestamp": point[0], "value": point[1], "labels": labels})

    sorted_label_keys = sorted(all_label_keys - {"__name__"})
    fieldnames = ["metric", "timestamp", "value"] + sorted_label_keys

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row_dict = {
                "metric": metric_name,
                "timestamp": r["timestamp"],
                "value": r["value"],
            }
            for k in sorted_label_keys:
                row_dict[k] = r["labels"].get(k, "")
            writer.writerow(row_dict)

    return len(rows)


def export_logs(
    start: datetime,
    end: datetime,
    telemetry_dir: Path,
    output_logs_dir: Path,
    dropped_services: set[str] | None = None,
) -> dict[str, int]:
    """Filters structured JSONL logs by timestamp window and writes to output directory.
    
    Supports missing_logs degradation hook by dropping specified services.
    """
    output_logs_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, int] = {}
    dropped = dropped_services or set()

    input_logs_dir = telemetry_dir / "logs"
    if not input_logs_dir.exists():
        return summary

    for log_path in input_logs_dir.glob("*.jsonl"):
        service_name = log_path.stem
        if service_name in dropped:
            # Degradation: drop service's logs for the window
            continue

        target_file = output_logs_dir / log_path.name
        matched_lines = 0

        with open(log_path, "r", encoding="utf-8") as src, open(
            target_file, "w", encoding="utf-8"
        ) as dst:
            for line in src:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    record = json.loads(line_str)
                    ts_val = record.get("ts") or record.get("timestamp")
                    if ts_val:
                        dt = parse_timestamp(ts_val)
                        if start <= dt <= end:
                            dst.write(line_str + "\n")
                            matched_lines += 1
                except Exception:
                    continue

        summary[service_name] = matched_lines

    return summary


def export_traces(
    start: datetime,
    end: datetime,
    telemetry_dir: Path,
    output_traces_dir: Path,
) -> int:
    """Filters distributed trace spans by timestamp window and writes JSONL."""
    output_traces_dir.mkdir(parents=True, exist_ok=True)
    input_traces_file = telemetry_dir / "traces" / "spans.jsonl"
    target_traces_file = output_traces_dir / "spans.jsonl"

    if not input_traces_file.exists():
        target_traces_file.touch()
        return 0

    matched_spans = 0
    with open(input_traces_file, "r", encoding="utf-8") as src, open(
        target_traces_file, "w", encoding="utf-8"
    ) as dst:
        for line in src:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                span_data = json.loads(line_str)
                # Handle nested resourceSpans if present
                if "resourceSpans" in span_data:
                    # OTel Collector batch wrapper: inspect individual spans
                    for rs in span_data.get("resourceSpans", []):
                        for ss in rs.get("scopeSpans", []):
                            filtered_spans = []
                            for sp in ss.get("spans", []):
                                dt = _parse_span_timestamp(sp)
                                if dt is None or (start <= dt <= end):
                                    filtered_spans.append(sp)
                                    matched_spans += 1
                            ss["spans"] = filtered_spans
                    dst.write(json.dumps(span_data) + "\n")
                else:
                    # Flat span record
                    dt = _parse_span_timestamp(span_data)
                    if dt is None or (start <= dt <= end):
                        dst.write(line_str + "\n")
                        matched_spans += 1
            except Exception:
                continue

    return matched_spans


def export_deploy_history(
    end: datetime,
    output_deploys_dir: Path,
    database_url: str | None = None,
    stale_metadata: bool = False,
    mock_deploys: list[Any] | None = None,
) -> int:
    """Exports deployment history up to the end timestamp.
    
    Supports stale_deploy_metadata degradation hook by omitting the newest deploy row.
    """
    output_deploys_dir.mkdir(parents=True, exist_ok=True)

    records: list[DeployRecord] = []
    if mock_deploys is not None:
        for d in mock_deploys:
            if isinstance(d, DeployRecord):
                records.append(d)
            elif isinstance(d, dict):
                records.append(
                    DeployRecord(
                        deploy_id=d["deploy_id"],
                        service=d["service"],
                        old_version=d["old_version"],
                        new_version=d["new_version"],
                        deployed_at=parse_timestamp(d["deployed_at"]),
                        author=d["author"],
                        summary=d["summary"],
                        changed_files=d["changed_files"],
                        config_changes=d["config_changes"],
                        diff_text=d["diff_text"],
                    )
                )
    else:
        try:
            records = get_deploy_history(database_url=database_url, limit=100)
        except Exception:
            records = []

    # Filter to deployed_at <= end
    filtered: list[DeployRecord] = [
        r for r in records if r.deployed_at <= end
    ]

    # Sort descending by deployed_at
    filtered.sort(key=lambda x: x.deployed_at, reverse=True)

    # Degradation: stale_deploy_metadata omits the newest deploy row
    if stale_metadata and filtered:
        filtered = filtered[1:]

    # Write deploys.json
    json_path = output_deploys_dir / "deploys.json"
    dict_records = [r.to_dict() for r in filtered]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(dict_records, f, indent=2)

    # Write deploys.csv
    csv_path = output_deploys_dir / "deploys.csv"
    fieldnames = [
        "deploy_id",
        "service",
        "old_version",
        "new_version",
        "deployed_at",
        "author",
        "summary",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in filtered:
            writer.writerow(
                {
                    "deploy_id": r.deploy_id,
                    "service": r.service,
                    "old_version": r.old_version,
                    "new_version": r.new_version,
                    "deployed_at": r.deployed_at.isoformat(),
                    "author": r.author,
                    "summary": r.summary,
                }
            )

    return len(filtered)


def validate_export_isolation(output_dir: Path) -> None:
    """Verifies that the export folder strictly contains only agent-visible telemetry."""
    for root, _, files in os.walk(output_dir):
        for file in files:
            # Check forbidden filename
            if file in FORBIDDEN_EXPORT_FILENAMES or file.endswith(".db"):
                raise AssertionError(f"Isolation violation: forbidden file found in export: {file}")
            # Ensure allowed directories only
            rel_path = Path(root).relative_to(output_dir)
            top_level = rel_path.parts[0] if rel_path.parts else file
            if top_level not in ("metrics", "logs", "traces", "deploys"):
                unexpected = rel_path / file
                raise AssertionError(
                    f"Isolation violation: unexpected file or directory in export: {unexpected}"
                )


def export_evidence(
    run_id: str,
    start: str | datetime | float | int,
    end: str | datetime | float | int,
    output_dir: str | Path | None = None,
    telemetry_dir: str | Path | None = None,
    prometheus_url: str | None = None,
    database_url: str | None = None,
    degradations: list[Any] | None = None,
    mock_prom_data: dict[str, Any] | None = None,
    mock_deploys: list[Any] | None = None,
) -> Path:
    """Exports all agent-visible evidence for the specified run and incident window."""
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)

    if end_dt < start_dt:
        raise ValueError(f"End timestamp {end_dt} precedes start timestamp {start_dt}")

    if output_dir is None:
        base_output = Path(f"evidence_exports/{run_id}").resolve()
    elif isinstance(output_dir, Path):
        base_output = output_dir.resolve()
    else:
        base_output = Path(output_dir).resolve()
    base_output.mkdir(parents=True, exist_ok=True)

    if telemetry_dir is None:
        telemetry_path = Path(os.getenv("TELEMETRY_DIR", "/telemetry")).resolve()
    elif isinstance(telemetry_dir, Path):
        telemetry_path = telemetry_dir.resolve()
    else:
        telemetry_path = Path(telemetry_dir).resolve()

    # Process degradation hooks
    dropped_log_services: set[str] = set()
    stale_deploys: bool = False

    for deg in degradations or []:
        if isinstance(deg, str):
            deg_lower = deg.lower().strip()
            if deg_lower == "missing_logs":
                # Default dropped service is orders
                dropped_log_services.add("orders")
            elif deg_lower.startswith("missing_logs:"):
                dropped_log_services.add(deg_lower.split(":", 1)[1])
            elif deg_lower == "stale_deploy_metadata":
                stale_deploys = True
        elif isinstance(deg, dict):
            deg_type = deg.get("type", "").lower()
            if deg_type == "missing_logs":
                svc = deg.get("service", "orders")
                dropped_log_services.add(svc)
            elif deg_type == "stale_deploy_metadata":
                stale_deploys = True

    # 1. Prometheus Range Data
    export_prometheus_metrics(
        start=start_dt,
        end=end_dt,
        metrics_dir=base_output / "metrics",
        prometheus_url=prometheus_url,
        mock_data=mock_prom_data,
    )

    # 2. Structured Logs
    export_logs(
        start=start_dt,
        end=end_dt,
        telemetry_dir=telemetry_path,
        output_logs_dir=base_output / "logs",
        dropped_services=dropped_log_services,
    )

    # 3. Traces
    export_traces(
        start=start_dt,
        end=end_dt,
        telemetry_dir=telemetry_path,
        output_traces_dir=base_output / "traces",
    )

    # 4. Deploy History
    export_deploy_history(
        end=end_dt,
        output_deploys_dir=base_output / "deploys",
        database_url=database_url,
        stale_metadata=stale_deploys,
        mock_deploys=mock_deploys,
    )

    # 5. Isolation Guarantee
    validate_export_isolation(base_output)

    return base_output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export agent-visible evidence for an OpsPilot incident window."
    )
    parser.add_argument("run_id", help="Identifier of the scenario execution run")
    parser.add_argument("--start", required=True, help="Incident window start (ISO or epoch)")
    parser.add_argument("--end", required=True, help="Incident window end (ISO or epoch)")
    parser.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Target output directory (default: evidence_exports/<run_id>)",
    )
    parser.add_argument(
        "--telemetry-dir",
        default=None,
        help="Path to telemetry storage volume (default: /telemetry or $TELEMETRY_DIR)",
    )
    parser.add_argument(
        "--prometheus-url",
        default=None,
        help="Prometheus API base URL (default: http://prometheus:9090)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection string for lab deploys",
    )
    parser.add_argument(
        "--degradations",
        nargs="*",
        default=[],
        help="Degradation hooks (missing_logs, stale_deploy_metadata)",
    )

    args = parser.parse_args()

    try:
        out_path = export_evidence(
            run_id=args.run_id,
            start=args.start,
            end=args.end,
            output_dir=args.output_dir,
            telemetry_dir=args.telemetry_dir,
            prometheus_url=args.prometheus_url,
            database_url=args.database_url,
            degradations=args.degradations,
        )
        print(f"Evidence successfully exported to: {out_path}")
    except Exception as e:
        print(f"Error exporting evidence: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
