#!/usr/bin/env python3
"""Blind Diagnosis Kit for OpsPilot lab scenarios.

Generates blind investigation packets with randomized IDs in shuffled order,
storing the answer key strictly in the SQLite vault.
Scores human diagnosis logs against the vault ground truth and generates
evidence requirements documentation.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evals.graders.diagnosis import grade_diagnosis
from evals.graders.models import Diagnosis, DiagnosisGrade
from lab.controller.models import TruthRecord
from lab.controller.vault import Vault
from lab.controller_lib.deploy import DeployRecord
from scripts.export_evidence import export_evidence

SCENARIO_SPECS: list[dict[str, Any]] = [
    {
        "scenario_id": "S1",
        "fixture_path": "fixtures/scenarios/S1_pool_leak.yml",
        "family": "pool_leak",
        "root_cause_service": "orders",
        "root_cause_category": "bad_deploy",
        "mechanism": "connection_pool_exhaustion",
        "culprit_deploy_id": "deploy_orders_v130",
        "expected_action": {"action": "rollback", "service": "orders"},
        "summary": "Orders connection pool exhaustion on 409 conflict after v1.3.0 deploy",
    },
    {
        "scenario_id": "S2",
        "fixture_path": "fixtures/scenarios/S2_latency_regression.yml",
        "family": "latency_regression",
        "root_cause_service": "inventory",
        "root_cause_category": "bad_deploy",
        "mechanism": "cache_bypass_latency",
        "culprit_deploy_id": "deploy_inventory_v120",
        "expected_action": {"action": "rollback", "service": "inventory"},
        "summary": "Inventory cache bypass and redundant database query after v1.2.0 deploy",
    },
    {
        "scenario_id": "S3",
        "fixture_path": "fixtures/scenarios/S3_dependency_timeout.yml",
        "family": "dependency_failure",
        "root_cause_service": "inventory",
        "root_cause_category": "dependency_failure",
        "mechanism": "dependency_timeout",
        "culprit_deploy_id": None,
        "expected_action": {"action": "remediate_dependency", "target": "redis"},
        "summary": "Inventory Redis latency degradation causing 150ms timeout breaches",
    },
    {
        "scenario_id": "S4",
        "fixture_path": "fixtures/scenarios/S4_dependency_timeout_with_decoy.yml",
        "family": "dependency_failure",
        "root_cause_service": "inventory",
        "root_cause_category": "dependency_failure",
        "mechanism": "dependency_timeout",
        "culprit_deploy_id": None,
        "expected_action": {"action": "remediate_dependency", "target": "redis"},
        "summary": "Redis dependency latency with benign orders v1.1.0 decoy deploy",
    },
    {
        "scenario_id": "S5",
        "fixture_path": "fixtures/scenarios/S5_false_alarm.yml",
        "family": "false_alarm",
        "root_cause_service": None,
        "root_cause_category": "false_alarm",
        "mechanism": "none",
        "culprit_deploy_id": None,
        "expected_action": {"action": "no_action"},
        "summary": "Transient marketing traffic burst within system SLA capacity",
    },
]


def _build_packet_readme(packet_id: str) -> str:
    """Builds neutral packet README listing contained files without diagnostic hints."""
    lines = [
        f"# Blind Investigation Packet: `{packet_id}`",
        "",
        "## Overview",
        "This packet contains observational telemetry and deployment audit data exported",
        "from the OpsPilot synthetic lab during an operational incident window.",
        "Analyze the enclosed telemetry to determine:",
        "1. **Root Cause Service**: Primary culpable microservice (orders, inventory, none).",
        "2. **Category**: `bad_deploy` | `dependency_failure` | `false_alarm` | `unknown`",
        "3. **Mechanism**: `connection_pool_exhaustion` | `cache_bypass_latency` | "
        "`dependency_timeout` | `none` | `unknown`",
        "4. **Culprit Deploy ID**: Deploy ID from `deploys/` (if `bad_deploy`, else null).",
        "5. **Evidence Used**: Metric files, log lines, span IDs, or diff snippets referenced.",
        "",
        "## Contents",
        "- `metrics/`: Time-series range telemetry in JSON and CSV formats",
        "  - `http_requests_total`",
        "  - `http_request_duration_seconds_bucket`",
        "  - `dependency_call_duration_seconds_bucket`",
        "  - `dependency_errors_total`",
        "  - `db_pool_in_use`",
        "  - `db_pool_size`",
        "  - `db_pool_wait_seconds_bucket`",
        "- `logs/`: Filtered structured JSONL application logs",
        "  - `gateway.jsonl`",
        "  - `orders.jsonl`",
        "  - `inventory.jsonl`",
        "- `traces/`: OpenTelemetry distributed span streams",
        "  - `spans.jsonl`",
        "- `deploys/`: Chronological deployment history and changelog audit",
        "  - `deploys.json`",
        "  - `deploys.csv`",
        "",
        "## Logging Your Diagnosis",
        "Record your analysis in `docs/human_diagnosis_log.md`.",
    ]
    return "\n".join(lines) + "\n"


def _generate_synthetic_evidence_for_scenario(
    spec: dict[str, Any],
    start_ts: datetime,
    end_ts: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[DeployRecord]]:
    """Generates realistic evidence data matching the scenario physics."""
    s_id = spec["scenario_id"]
    dur = (end_ts - start_ts).total_seconds()
    mid_ts = start_ts + timedelta(seconds=dur / 2.0)

    # 1. Prometheus Metrics
    prom_data: dict[str, Any] = {}
    if s_id == "S1":
        # Pool leak: db_pool_in_use climbs to capacity (10)
        prom_data = {
            "http_requests_total": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "orders", "route": "/orders", "status": "200"},
                            "values": [
                                [start_ts.timestamp(), "50"],
                                [end_ts.timestamp(), "75"],
                            ],
                        },
                        {
                            "metric": {"service": "orders", "route": "/orders", "status": "503"},
                            "values": [
                                [start_ts.timestamp(), "0"],
                                [end_ts.timestamp(), "45"],
                            ],
                        },
                    ],
                },
            },
            "db_pool_in_use": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "orders"},
                            "values": [
                                [start_ts.timestamp(), "1"],
                                [mid_ts.timestamp(), "6"],
                                [end_ts.timestamp(), "10"],
                            ],
                        },
                        {
                            "metric": {"service": "inventory"},
                            "values": [
                                [start_ts.timestamp(), "1"],
                                [end_ts.timestamp(), "2"],
                            ],
                        },
                    ],
                },
            },
            "db_pool_size": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "orders"},
                            "values": [
                                [start_ts.timestamp(), "10"],
                                [end_ts.timestamp(), "10"],
                            ],
                        },
                        {
                            "metric": {"service": "inventory"},
                            "values": [
                                [start_ts.timestamp(), "10"],
                                [end_ts.timestamp(), "10"],
                            ],
                        },
                    ],
                },
            },
        }
    elif s_id == "S2":
        # Latency regression: inventory p95 elevated, extra db queries
        prom_data = {
            "http_requests_total": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {
                                "service": "inventory",
                                "route": "/stock/{id}",
                                "status": "200",
                            },
                            "values": [
                                [start_ts.timestamp(), "100"],
                                [end_ts.timestamp(), "300"],
                            ],
                        },
                    ],
                },
            },
            "http_request_duration_seconds_bucket": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "inventory", "le": "0.05"},
                            "values": [
                                [start_ts.timestamp(), "80"],
                                [end_ts.timestamp(), "90"],
                            ],
                        },
                        {
                            "metric": {"service": "inventory", "le": "0.5"},
                            "values": [
                                [start_ts.timestamp(), "100"],
                                [end_ts.timestamp(), "290"],
                            ],
                        },
                    ],
                },
            },
            "dependency_call_duration_seconds_bucket": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"dependency": "db", "le": "0.25"},
                            "values": [
                                [start_ts.timestamp(), "50"],
                                [end_ts.timestamp(), "280"],
                            ],
                        },
                    ],
                },
            },
        }
    elif s_id in ("S3", "S4"):
        # Dependency timeout: Redis latency spike to 300ms
        prom_data = {
            "http_requests_total": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "gateway", "route": "/checkout", "status": "200"},
                            "values": [
                                [start_ts.timestamp(), "60"],
                                [end_ts.timestamp(), "110"],
                            ],
                        },
                    ],
                },
            },
            "dependency_call_duration_seconds_bucket": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"dependency": "redis", "le": "0.15"},
                            "values": [
                                [start_ts.timestamp(), "100"],
                                [end_ts.timestamp(), "105"],
                            ],
                        },
                        {
                            "metric": {"dependency": "redis", "le": "0.5"},
                            "values": [
                                [start_ts.timestamp(), "100"],
                                [end_ts.timestamp(), "220"],
                            ],
                        },
                    ],
                },
            },
            "dependency_errors_total": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"dependency": "redis"},
                            "values": [
                                [start_ts.timestamp(), "0"],
                                [end_ts.timestamp(), "18"],
                            ],
                        },
                    ],
                },
            },
        }
    else:
        # S5: False alarm marketing burst
        prom_data = {
            "http_requests_total": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "gateway", "route": "/checkout", "status": "200"},
                            "values": [
                                [start_ts.timestamp(), "20"],
                                [mid_ts.timestamp(), "180"],
                                [end_ts.timestamp(), "220"],
                            ],
                        },
                    ],
                },
            },
            "db_pool_in_use": {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "orders"},
                            "values": [
                                [start_ts.timestamp(), "1"],
                                [mid_ts.timestamp(), "4"],
                                [end_ts.timestamp(), "2"],
                            ],
                        },
                    ],
                },
            },
        }

    # 2. Logs
    logs: list[dict[str, Any]] = []
    if s_id == "S1":
        logs.extend(
            [
                {
                    "ts": (start_ts + timedelta(seconds=20)).isoformat(),
                    "service": "orders",
                    "level": "INFO",
                    "event": "order_conflict",
                    "msg": "Insufficient inventory stock for item",
                },
                {
                    "ts": (start_ts + timedelta(seconds=45)).isoformat(),
                    "service": "orders",
                    "level": "ERROR",
                    "event": "db_pool_timeout",
                    "msg": "Timed out waiting for database connection from pool",
                },
                {
                    "ts": (start_ts + timedelta(seconds=50)).isoformat(),
                    "service": "gateway",
                    "level": "WARN",
                    "event": "upstream_error",
                    "msg": "Upstream orders service connection failed",
                },
            ]
        )
    elif s_id == "S2":
        logs.extend(
            [
                {
                    "ts": (start_ts + timedelta(seconds=25)).isoformat(),
                    "service": "inventory",
                    "level": "INFO",
                    "event": "stock_checked",
                    "msg": "Stock level checked from database",
                },
                {
                    "ts": (start_ts + timedelta(seconds=35)).isoformat(),
                    "service": "inventory",
                    "level": "WARN",
                    "event": "elevated_latency",
                    "msg": "Database query completed with elevated duration",
                },
            ]
        )
    elif s_id in ("S3", "S4"):
        logs.extend(
            [
                {
                    "ts": (start_ts + timedelta(seconds=30)).isoformat(),
                    "service": "inventory",
                    "level": "WARN",
                    "event": "dependency_timeout",
                    "msg": "Redis query exceeded 150ms timeout limit; fallback initiated",
                },
                {
                    "ts": (start_ts + timedelta(seconds=35)).isoformat(),
                    "service": "inventory",
                    "level": "INFO",
                    "event": "datastore_fallback",
                    "msg": "Primary datastore fallback query succeeded",
                },
            ]
        )
    else:
        logs.extend(
            [
                {
                    "ts": (start_ts + timedelta(seconds=15)).isoformat(),
                    "service": "gateway",
                    "level": "INFO",
                    "event": "traffic_surge",
                    "msg": "Incoming request rate elevated due to campaign",
                },
                {
                    "ts": (start_ts + timedelta(seconds=40)).isoformat(),
                    "service": "gateway",
                    "level": "INFO",
                    "event": "traffic_normalizing",
                    "msg": "Request rate returned to baseline",
                },
            ]
        )

    # 3. Spans
    spans: list[dict[str, Any]] = [
        {
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "span_id": "00f067aa0ba902b7",
            "name": "gateway.checkout",
            "start_time": (start_ts + timedelta(seconds=30)).isoformat(),
            "attributes": {"http.status_code": 200, "service.name": "gateway"},
        }
    ]

    # 4. Deploys (benign metadata without forbidden words)
    deploys: list[DeployRecord] = [
        DeployRecord(
            deploy_id="dep-seed-v100",
            service="orders",
            old_version="v0.9.0",
            new_version="v1.0.0",
            deployed_at=start_ts - timedelta(days=2),
            author="synthetic-release@internal",
            summary="Initial baseline service architecture deployment",
            changed_files=["lab/services/orders/main.py"],
            config_changes={},
            diff_text="Initial baseline release",
        )
    ]

    if s_id == "S1":
        deploys.append(
            DeployRecord(
                deploy_id="deploy_orders_v130",
                service="orders",
                old_version="v1.0.0",
                new_version="v1.3.0",
                deployed_at=start_ts + timedelta(seconds=15),
                author="synthetic-release@internal",
                summary="Refactor order transaction flow and conflict resolution handling",
                changed_files=["lab/services/orders/client.py"],
                config_changes={"explicit_tx": True},
                diff_text=(
                    "--- a/client.py\n+++ b/client.py\n"
                    "@@ -200,6 +200,8 @@\n+    conn = await self._acquire()\n"
                    "+    if conflict:\n+        return False"
                ),
            )
        )
    elif s_id == "S2":
        deploys.append(
            DeployRecord(
                deploy_id="deploy_inventory_v120",
                service="inventory",
                old_version="v1.0.0",
                new_version="v1.2.0",
                deployed_at=start_ts + timedelta(seconds=15),
                author="synthetic-release@internal",
                summary="Adjust inventory stock audit consistency check and caching strategy",
                changed_files=["lab/services/inventory/client.py"],
                config_changes={"audit_mode": True},
                diff_text=(
                    "--- a/client.py\n+++ b/client.py\n"
                    "@@ -115,4 +115,6 @@\n-    cached = await self.redis.get(key)\n"
                    "+    # Direct datastore consistency lookup\n"
                    "+    val = await self._query_db(key)"
                ),
            )
        )
    elif s_id == "S4":
        deploys.append(
            DeployRecord(
                deploy_id="decoy_deploy_orders_v110",
                service="orders",
                old_version="v1.0.0",
                new_version="v1.1.0",
                deployed_at=start_ts + timedelta(seconds=5),
                author="synthetic-release@internal",
                summary="Update receipt rendering template formatting and add header metadata",
                changed_files=["lab/services/orders/templates.py"],
                config_changes={"receipt_format": "v2"},
                diff_text=(
                    "--- a/templates.py\n+++ b/templates.py\n"
                    "@@ -10,3 +10,4 @@\n+    receipt_header = 'OpsPilot Receipt'"
                ),
            )
        )

    return prom_data, logs, spans, deploys


def generate_blind_packets(
    output_dir: Path,
    vault_path: str = "vault.db",
    seed: int = 42,
) -> list[str]:
    """Generates 5 blind packets in randomized order and records the key in the vault."""
    random.seed(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    vault = Vault(vault_path)

    # Shuffle scenario specifications
    shuffled_specs = list(SCENARIO_SPECS)
    random.shuffle(shuffled_specs)

    packet_ids: list[str] = []
    log_template_rows: list[str] = []

    now = datetime(2026, 11, 23, 14, 0, 0, tzinfo=timezone.utc)

    for idx, spec in enumerate(shuffled_specs):
        packet_id = f"packet_{uuid_short(idx, seed)}"
        packet_ids.append(packet_id)

        packet_dir = output_dir / packet_id
        packet_dir.mkdir(parents=True, exist_ok=True)

        start_ts = now + timedelta(minutes=idx * 30)
        end_ts = start_ts + timedelta(minutes=5)

        run_id = f"run-blind-{packet_id}"

        # 1. Generate scenario evidence
        prom_data, logs, spans, deploys = _generate_synthetic_evidence_for_scenario(
            spec, start_ts, end_ts
        )

        # Setup temporary telemetry directory outside the packet directory
        mock_telem_dir = output_dir / f".tmp_telem_{packet_id}"
        (mock_telem_dir / "logs").mkdir(parents=True, exist_ok=True)
        (mock_telem_dir / "traces").mkdir(parents=True, exist_ok=True)

        # Write logs
        for svc in ["gateway", "orders", "inventory"]:
            svc_logs = [entry for entry in logs if entry.get("service") == svc]
            log_file = mock_telem_dir / "logs" / f"{svc}.jsonl"
            with open(log_file, "w", encoding="utf-8") as f:
                for entry in svc_logs:
                    f.write(json.dumps(entry) + "\n")

        # Write traces
        traces_file = mock_telem_dir / "traces" / "spans.jsonl"
        with open(traces_file, "w", encoding="utf-8") as f:
            for sp in spans:
                f.write(json.dumps(sp) + "\n")

        # 2. Export evidence into clean packet directory
        export_evidence(
            run_id=run_id,
            start=start_ts,
            end=end_ts,
            output_dir=packet_dir,
            telemetry_dir=mock_telem_dir,
            mock_prom_data=prom_data,
            mock_deploys=deploys,
        )

        # Clean up temporary mock source
        if mock_telem_dir.exists():
            shutil.rmtree(mock_telem_dir, ignore_errors=True)

        # 3. Write neutral README.md inside the packet
        readme_path = packet_dir / "README.md"
        readme_path.write_text(_build_packet_readme(packet_id), encoding="utf-8")

        # 4. Save ground truth and answer key in Vault ONLY
        truth = TruthRecord(
            run_id=run_id,
            scenario_id=spec["scenario_id"],
            family=spec["family"],
            root_cause={
                "service": spec["root_cause_service"],
                "component": spec["root_cause_service"] or "none",
                "mechanism": spec["mechanism"],
            },
            culprit_deploy_id=spec["culprit_deploy_id"],
            injected_at=start_ts.isoformat(),
            expected_action=spec["expected_action"],
            decoys=[],
            health_thresholds={"error_rate": 0.05, "p95_latency_ms": 500.0},
            split_tag="eval",
            created_at=start_ts.isoformat(),
            status="completed",
        )
        vault.record_run(truth)
        vault.record_blind_packet(packet_id, run_id, spec["scenario_id"])

        # Template entry for human diagnosis log
        log_template_rows.append(f"| `{packet_id}` |  |  |  |  |  |  |  |")

    # 5. Initialize or update docs/human_diagnosis_log.md template
    init_diagnosis_log_template(Path("docs/human_diagnosis_log.md"), log_template_rows)

    return packet_ids


def uuid_short(index: int, seed: int) -> str:
    """Generates deterministic short hex identifiers."""
    h = random.Random(seed + index * 997).getrandbits(16)
    return f"{h:04x}"


def init_diagnosis_log_template(log_path: Path, template_rows: list[str]) -> None:
    """Creates initial template for human diagnosis log if not already present."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        # Do not overwrite filled log if present
        return

    header = (
        "# Human Blind Diagnosis Log\n\n"
        "Record your diagnosis for each investigated packet.\n"
        "Allowed categories: `bad_deploy` | `dependency_failure` | `false_alarm` | `unknown`\n"
        "Allowed mechanisms: `connection_pool_exhaustion` | `cache_bypass_latency` | "
        "`dependency_timeout` | `none` | `unknown`\n\n"
        "| packet_id | service | category | mechanism | culprit_deploy_id | "
        "evidence_used | time_spent | confidence |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    content = header + "\n".join(template_rows) + "\n"
    log_path.write_text(content, encoding="utf-8")


def parse_human_diagnosis_log(log_path: Path) -> list[dict[str, Any]]:
    """Parses markdown table or section format from docs/human_diagnosis_log.md."""
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")

    text = log_path.read_text(encoding="utf-8")
    diagnoses: list[dict[str, Any]] = []

    # 1. Try parsing Markdown Table
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(lines) >= 3:
        headers = [h.strip().lower() for h in lines[0].strip("|").split("|")]
        for row_line in lines[2:]:
            cells = [c.strip().strip("`") for c in row_line.strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            row_dict = dict(zip(headers, cells, strict=False))
            pid = row_dict.get("packet_id", "").strip("`").strip()
            if pid and not pid.startswith("---") and not pid.startswith("packet_id"):
                svc_val = (row_dict.get("service") or "").strip()
                if not svc_val or svc_val.lower() in ("none", "null", "n/a"):
                    svc_val = None
                culp_val = (row_dict.get("culprit_deploy_id") or "").strip()
                if not culp_val or culp_val.lower() in ("none", "null", "n/a"):
                    culp_val = None

                diagnoses.append(
                    {
                        "packet_id": pid,
                        "service": svc_val,
                        "category": row_dict.get("category", "unknown"),
                        "mechanism": row_dict.get("mechanism", "unknown"),
                        "culprit_deploy_id": culp_val,
                        "evidence_used": [
                            e.strip()
                            for e in (row_dict.get("evidence_used") or "").split(",")
                            if e.strip()
                        ],
                        "time_spent": row_dict.get("time_spent", ""),
                        "confidence": float(row_dict.get("confidence") or 0.8),
                    }
                )

    # 2. Try parsing Sections if table was empty
    if not diagnoses:
        section_pattern = re.compile(r"##\s*Packet:\s*`?([a-zA-Z0-9_-]+)`?", re.IGNORECASE)
        sections = section_pattern.split(text)
        if len(sections) > 1:
            for i in range(1, len(sections), 2):
                pid = sections[i].strip()
                sec_body = sections[i + 1]
                svc_m = re.search(r"-\s*\*\*service\*\*:\s*([^\n]+)", sec_body, re.I)
                cat_m = re.search(r"-\s*\*\*category\*\*:\s*([^\n]+)", sec_body, re.I)
                mech_m = re.search(r"-\s*\*\*mechanism\*\*:\s*([^\n]+)", sec_body, re.I)
                culp_m = re.search(r"-\s*\*\*culprit_deploy_id\*\*:\s*([^\n]+)", sec_body, re.I)
                ev_m = re.search(r"-\s*\*\*evidence_used\*\*:\s*([^\n]+)", sec_body, re.I)
                conf_m = re.search(r"-\s*\*\*confidence\*\*:\s*([0-9.]+)", sec_body, re.I)
                time_m = re.search(r"-\s*\*\*time_spent\*\*:\s*([^\n]+)", sec_body, re.I)

                svc_val = svc_m.group(1).strip() if svc_m else None
                culp_val = culp_m.group(1).strip() if culp_m else None
                ev_val = (
                    [e.strip() for e in ev_m.group(1).split(",") if e.strip()] if ev_m else []
                )

                diagnoses.append(
                    {
                        "packet_id": pid,
                        "service": svc_val if svc_val not in ("null", "none", "") else None,
                        "category": cat_m.group(1).strip() if cat_m else "unknown",
                        "mechanism": mech_m.group(1).strip() if mech_m else "unknown",
                        "culprit_deploy_id": (
                            culp_val if culp_val not in ("null", "none", "") else None
                        ),
                        "evidence_used": ev_val,
                        "time_spent": time_m.group(1).strip() if time_m else "",
                        "confidence": float(conf_m.group(1)) if conf_m else 0.8,
                    }
                )

    return diagnoses


def score_human_diagnoses(
    log_path: Path,
    vault_path: str = "vault.db",
    output_requirements_path: Path | None = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Scores entries in human_diagnosis_log.md against the secret Vault truth."""
    vault = Vault(vault_path)
    diagnoses = parse_human_diagnosis_log(log_path)
    if not diagnoses:
        print(f"No diagnosis entries found in {log_path}", file=sys.stderr)
        return 0, 0, []

    score_results: list[dict[str, Any]] = []
    total = len(diagnoses)
    correct_count = 0
    missing_evidence_list: list[dict[str, Any]] = []

    print("\n" + "=" * 84)
    print(f"{'PACKET ID':<13} | {'SERVICE':<9} | {'CATEGORY':<18} | {'MECHANISM':<26} | {'RESULT'}")
    print("-" * 84)

    for item in diagnoses:
        pid = item["packet_id"]
        packet_record = vault.get_blind_packet(pid)
        if not packet_record:
            print(f"{pid:<14} | NOT FOUND IN VAULT")
            continue

        run_id = packet_record["run_id"]
        truth: TruthRecord | None = vault.get_run(run_id)
        if not truth:
            print(f"{pid:<14} | TRUTH RECORD MISSING IN VAULT")
            continue

        diag_obj = Diagnosis(
            root_cause_service=item["service"],
            root_cause_category=item["category"],
            mechanism=item["mechanism"],
            culprit_deploy_id=item["culprit_deploy_id"],
            evidence_ids=item["evidence_used"],
            confidence=item["confidence"],
        )

        grade: DiagnosisGrade = grade_diagnosis(truth, diag_obj)
        if grade.correct:
            res_str = "CORRECT"
            correct_count += 1
        elif grade.partial:
            res_str = "PARTIAL"
        elif grade.abstained:
            res_str = "ABSTAINED"
        else:
            res_str = "INCORRECT"

        if not grade.correct:
            missing_evidence_list.append(
                {
                    "packet_id": pid,
                    "family": truth.family,
                    "reasons": grade.reasons,
                }
            )

        print(
            f"{pid:<14} | {str(item['service']):<10} | {item['category']:<20} | "
            f"{item['mechanism']:<28} | {res_str}"
        )
        for r in grade.reasons:
            print(f"   ↳ {r}")

        score_results.append(
            {
                "packet_id": pid,
                "scenario_id": truth.scenario_id,
                "family": truth.family,
                "human_diagnosis": item,
                "grade": grade.model_dump(),
            }
        )

    print("=" * 90)
    acc = (correct_count / total * 100.0) if total > 0 else 0.0
    print(f"OVERALL SCORE: {correct_count}/{total} correct ({acc:.1f}%)\n")

    # 5. Missing evidence and world changes report
    if missing_evidence_list:
        print("\n[DIAGNOSABILITY ISSUES DETECTED]")
        for issue in missing_evidence_list:
            print(f"Packet: {issue['packet_id']} (Family: {issue['family']})")
            print("  Deficiency reasons:", issue["reasons"])
            print(
                "  Proposed world fix: Instrument additional child span traces & "
                "downstream dependency error counters."
            )

    # 6. Generate docs/evidence_requirements.md
    req_path = output_requirements_path or Path("docs/evidence_requirements.md")
    generate_evidence_requirements_doc(req_path, score_results)

    return total, correct_count, score_results


def generate_evidence_requirements_doc(out_path: Path, results: list[dict[str, Any]]) -> None:
    """Generates docs/evidence_requirements.md documenting minimum signals per family."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Evidence Requirements Specification",
        "",
        "This document defines the minimum observational signals required by an investigator",
        "(human or autonomous agent) to diagnose each failure family unambiguously.",
        "These requirements establish the contract for the Feature 2 Investigation Tooling.",
        "",
        "---",
        "",
        "## 1. Summary of Required Signals by Failure Family",
        "",
        "### `pool_leak`",
        "- **Metric Signals**: `db_pool_in_use == db_pool_size`, "
        "`http_requests_total{status='503'}` spike.",
        "- **Log Events**: `db_pool_timeout`, `order_conflict` (409).",
        "- **Trace Indicators**: Elevated `gateway.checkout` duration ending in 503.",
        "- **Deploy Audit Signals**: Recent `orders` deploy modifying database pool handling.",
        "",
        "### `latency_regression`",
        "- **Metric Signals**: Elevated `http_request_duration_seconds{le='0.5'}` p95, zero 5xx.",
        "- **Log Events**: Absence of Redis cache hit logs, elevated database query log frequency.",
        "- **Trace Indicators**: `inventory` span breakdown dominated by downstream DB queries.",
        "- **Deploy Audit Signals**: Recent `inventory` deploy altering caching strategy.",
        "",
        "### `dependency_timeout`",
        "- **Metric Signals**: `dependency_call_duration_seconds{dependency='redis'}` > 150ms.",
        "- **Log Events**: `dependency_timeout` on Redis, datastore fallback log.",
        "- **Trace Indicators**: Parent span delay attributed entirely to child dependency span.",
        "- **Deploy Audit Signals**: Zero deployments in incident window.",
        "",
        "### `dependency_timeout_with_decoy`",
        "- **Metric Signals**: Redis call duration elevated beyond timeout threshold.",
        "- **Log Events**: Redis timeout and fallback logs.",
        "- **Trace Indicators**: Child span latency isolating problem to Redis.",
        "- **Deploy Audit Signals**: Benign deploy on `orders` (decoy) with cosmetic diff.",
        "",
        "### `false_alarm`",
        "- **Metric Signals**: Transient surge in `http_requests_total` rate with 100% 2xx status.",
        "- **Log Events**: Transient traffic increase, zero error or timeout events.",
        "- **Trace Indicators**: Latencies within bounded capacity SLA limits.",
        "- **Deploy Audit Signals**: Zero deployments or configuration changes.",
        "",
        "---",
        "",
        "## 2. Tool Design Implications for Feature 2 Agent Tooling",
        "",
        "1. **Prometheus Range Tool (`query_range`)**:",
        "   - Instant rate/p95 calculations across customizable windows (`5m`, `15m`).",
        "   - Expose connection pool utilization metrics (`db_pool_in_use` vs `db_pool_size`).",
        "2. **Log Search Tool (`search_logs`)**:",
        "   - Must support filtering by service and minimum log level (`WARN`, `ERROR`).",
        "   - Must correlate structured event names (`db_pool_timeout`, `dependency_timeout`).",
        "3. **Trace Inspection Tool (`get_trace`)**:",
        "   - Must return parent-child span hierarchy with per-span duration attribution.",
        "4. **Deploy History Tool (`get_deploy_history`)**:",
        "   - Must present chronological diffs and changed files leading up to the window.",
        "   - Must enable comparing metrics before and after deployment timestamps.",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated evidence requirements specification at: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpsPilot Blind Diagnosis Kit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # generate subcommand
    gen_p = subparsers.add_parser(
        "generate", help="Generate randomized blind packets and answer keys"
    )
    gen_p.add_argument(
        "--output-dir",
        "-o",
        default="blind_packets",
        help="Directory to write randomized packets (default: blind_packets)",
    )
    gen_p.add_argument(
        "--vault-path",
        default="vault.db",
        help="Path to SQLite vault database (default: vault.db)",
    )
    gen_p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic random seed for packet shuffling and IDs (default: 42)",
    )

    # score subcommand
    score_p = subparsers.add_parser("score", help="Score human diagnosis log against the vault")
    score_p.add_argument(
        "--log-file",
        "-l",
        default="docs/human_diagnosis_log.md",
        help="Path to human diagnosis log (default: docs/human_diagnosis_log.md)",
    )
    score_p.add_argument(
        "--vault-path",
        default="vault.db",
        help="Path to SQLite vault database (default: vault.db)",
    )
    score_p.add_argument(
        "--output-requirements",
        default="docs/evidence_requirements.md",
        help="Path to output evidence requirements markdown",
    )

    args = parser.parse_args()

    if args.command == "generate":
        packet_ids = generate_blind_packets(
            output_dir=Path(args.output_dir),
            vault_path=args.vault_path,
            seed=args.seed,
        )
        print(f"Successfully generated {len(packet_ids)} blind packets in: {args.output_dir}")
        for pid in packet_ids:
            print(f"  - {pid}")
        print("Answer key securely saved to Vault only.")
        print("Fill docs/human_diagnosis_log.md then run 'score'.")

    elif args.command == "score":
        total, correct, _ = score_human_diagnoses(
            log_path=Path(args.log_file),
            vault_path=args.vault_path,
            output_requirements_path=Path(args.output_requirements),
        )
        if total == 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
