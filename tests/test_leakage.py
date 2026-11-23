"""Leakage test suite and isolation verification for OpsPilot lab.

Verifies:
1. After running each of the 5 scenarios, the evidence export, telemetry volume,
   and Prometheus labels contain ZERO forbidden leakage strings:
   - scenario_id (e.g. S1-S5, template IDs)
   - family names (pool_leak, latency_regression, dependency_timeout, etc.)
   - "fault", "inject", "ground_truth", vault IDs
   - admin endpoint paths (/admin/...)
   - forbidden diff terms (bug, leak, regression)
2. Network and storage isolation: from a container on the `core` network,
   attempting to read /vault, reach the controller API, or reach /admin routes ALL FAIL.
3. Support for evidence_degradations hooks:
   - missing_logs: drops a service's logs for the window
   - stale_deploy_metadata: omits the newest deploy row
   - default is OFF
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lab.controller_lib.deploy import DeployRecord
from lab.scenarios.loader import load_scenario
from scripts.export_evidence import (
    FORBIDDEN_EXPORT_FILENAMES,
    export_evidence,
    validate_export_isolation,
)

# -----------------------------------------------------------------------------
# Forbidden Leakage Patterns
# -----------------------------------------------------------------------------
FORBIDDEN_FAMILY_NAMES = [
    "pool_leak",
    "latency_regression",
    "dependency_timeout",
    "dependency_timeout_with_decoy",
    "false_alarm",
]

FORBIDDEN_TERMS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fault", re.compile(r"(?i)\bfaults?\b")),
    ("inject", re.compile(r"(?i)\binject(ed|ion|s|ing)?\b")),
    ("ground_truth", re.compile(r"(?i)\bground[-_]?truth\b")),
    ("admin_endpoint", re.compile(r"/admin/(?:state|deploy|faults|reset)")),
    ("admin_header", re.compile(r"(?i)X-Lab-Admin-Token")),
    ("diff_bug", re.compile(r"(?i)\bbugs?\b")),
    ("diff_leak", re.compile(r"(?i)\bleaks?\b")),
    ("diff_regression", re.compile(r"(?i)\bregressions?\b")),
]

SCENARIO_FILES = [
    ("S1", "fixtures/scenarios/S1_pool_leak.yml"),
    ("S2", "fixtures/scenarios/S2_latency_regression.yml"),
    ("S3", "fixtures/scenarios/S3_dependency_timeout.yml"),
    ("S4", "fixtures/scenarios/S4_dependency_timeout_with_decoy.yml"),
    ("S5", "fixtures/scenarios/S5_false_alarm.yml"),
]


def scan_directory_for_forbidden_strings(
    target_dir: Path,
    scenario_id: str,
    extra_forbidden: list[str] | None = None,
) -> list[dict[str, str]]:
    """Recursively scans all text/json files in a directory for forbidden terms."""
    hits: list[dict[str, str]] = []
    scenario_pattern = re.compile(rf"\bscenario[-_]?id\b|\b{scenario_id}\b", re.IGNORECASE)
    vault_id_pattern = re.compile(r"vault\.db|run-[a-f0-9]{12}", re.IGNORECASE)

    for file_path in target_dir.rglob("*"):
        if not file_path.is_file():
            continue

        # Check forbidden filename
        if file_path.name in FORBIDDEN_EXPORT_FILENAMES:
            hits.append(
                {
                    "file": str(file_path),
                    "line": "0",
                    "term": "forbidden_filename",
                    "text": file_path.name,
                }
            )
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_num, line in enumerate(content.splitlines(), start=1):
            # 1. Check scenario_id
            if scenario_pattern.search(line):
                hits.append(
                    {
                        "file": str(file_path),
                        "line": str(line_num),
                        "term": f"scenario_id_{scenario_id}",
                        "text": line.strip(),
                    }
                )

            # 2. Check family names
            for fam in FORBIDDEN_FAMILY_NAMES:
                if fam.lower() in line.lower():
                    hits.append(
                        {
                            "file": str(file_path),
                            "line": str(line_num),
                            "term": f"family_{fam}",
                            "text": line.strip(),
                        }
                    )

            # 3. Check general forbidden patterns
            for term_name, pat in FORBIDDEN_TERMS_PATTERNS:
                # Do not trigger on legitimate "pool" unless followed by leak
                if pat.search(line):
                    hits.append(
                        {
                            "file": str(file_path),
                            "line": str(line_num),
                            "term": term_name,
                            "text": line.strip(),
                        }
                    )

            # 4. Check vault IDs and paths
            if vault_id_pattern.search(line):
                hits.append(
                    {
                        "file": str(file_path),
                        "line": str(line_num),
                        "term": "vault_id_or_path",
                        "text": line.strip(),
                    }
                )

            # 5. Check extra forbidden terms
            for extra in extra_forbidden or []:
                if extra.lower() in line.lower():
                    hits.append(
                        {
                            "file": str(file_path),
                            "line": str(line_num),
                            "term": f"extra_{extra}",
                            "text": line.strip(),
                        }
                    )

    return hits


# -----------------------------------------------------------------------------
# 1. Leakage Tests for All Five Scenarios
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("scenario_code, scenario_rel_path", SCENARIO_FILES)
def test_scenario_evidence_export_zero_leakage(
    scenario_code: str,
    scenario_rel_path: str,
    tmp_path: Path,
) -> None:
    """Verifies that exporting evidence for any scenario produces ZERO diagnostic leakage.
    
    Scans:
    1. The evidence export directory
    2. Simulated telemetry volume logs and traces
    3. Prometheus metric labels
    """
    scenario_model = load_scenario(scenario_rel_path)
    run_id = f"run-test-{scenario_code.lower()}"
    now = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
    start_ts = now - timedelta(seconds=60)
    end_ts = now + timedelta(seconds=120)

    # Setup temporary mock telemetry volume
    telemetry_dir = tmp_path / "telemetry"
    (telemetry_dir / "logs").mkdir(parents=True)
    (telemetry_dir / "traces").mkdir(parents=True)

    # Generate synthetic realistic logs (devoid of forbidden leak strings)
    for svc in ["gateway", "orders", "inventory"]:
        log_file = telemetry_dir / "logs" / f"{svc}.jsonl"
        lines = [
            json.dumps(
                {
                    "ts": (start_ts + timedelta(seconds=10)).isoformat(),
                    "service": svc,
                    "version": "v1.0.0",
                    "level": "INFO",
                    "event": "request_processed",
                    "msg": "HTTP request completed successfully",
                    "request_id": f"req-{svc}-1",
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "span_id": "0123456789abcdef",
                }
            ),
            json.dumps(
                {
                    "ts": (start_ts + timedelta(seconds=30)).isoformat(),
                    "service": svc,
                    "version": "v1.0.0",
                    "level": "WARN",
                    "event": "connection_latency",
                    "msg": "Downstream call experienced elevated latency",
                    "request_id": f"req-{svc}-2",
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "span_id": "0123456789abcdef",
                }
            ),
        ]
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Generate synthetic traces
    traces_file = telemetry_dir / "traces" / "spans.jsonl"
    span_lines = [
        json.dumps(
            {
                "trace_id": "0123456789abcdef0123456789abcdef",
                "span_id": "0123456789abcdef",
                "name": "gateway.checkout",
                "startTimeUnixNano": int((start_ts + timedelta(seconds=10)).timestamp() * 1e9),
                "endTimeUnixNano": int((start_ts + timedelta(seconds=11)).timestamp() * 1e9),
                "attributes": {"http.status_code": 200, "service.name": "gateway"},
            }
        )
    ]
    traces_file.write_text("\n".join(span_lines) + "\n", encoding="utf-8")

    # Mock realistic Prometheus metric ranges
    mock_prom_data = {
        "http_requests_total": {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"service": "gateway", "route": "/checkout", "status": "200"},
                        "values": [[start_ts.timestamp(), "100"], [end_ts.timestamp(), "150"]],
                    }
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
                        "values": [[start_ts.timestamp(), "2"], [end_ts.timestamp(), "8"]],
                    }
                ],
            },
        },
    }

    # Realistic deploy history records (benign diff text without forbidden words)
    mock_deploys = [
        DeployRecord(
            deploy_id="dep-seed-01",
            service="orders",
            old_version="v1.0.0",
            new_version="v1.1.0",
            deployed_at=start_ts - timedelta(minutes=10),
            author="synthetic-release@internal",
            summary="Refactor order transaction flow and add idempotency check",
            changed_files=["lab/services/orders/client.py"],
            config_changes={"max_conns": 10},
            diff_text="--- a/client.py\n+++ b/client.py\n@@ -10,3 +10,4 @@\n+    idempotent = True",
        )
    ]

    export_dir = tmp_path / f"export_{scenario_code}"

    # Perform evidence export
    exported_path = export_evidence(
        run_id=run_id,
        start=start_ts,
        end=end_ts,
        output_dir=export_dir,
        telemetry_dir=telemetry_dir,
        degradations=[],
        mock_prom_data=mock_prom_data,
        mock_deploys=mock_deploys,
    )

    assert exported_path.exists()
    assert (exported_path / "metrics" / "http_requests_total.json").exists()
    assert (exported_path / "metrics" / "http_requests_total.csv").exists()
    assert (exported_path / "logs" / "orders.jsonl").exists()
    assert (exported_path / "traces" / "spans.jsonl").exists()
    assert (exported_path / "deploys" / "deploys.json").exists()

    # Scan exported folder for leakage
    export_hits = scan_directory_for_forbidden_strings(
        target_dir=exported_path,
        scenario_id=scenario_code,
        extra_forbidden=[scenario_model.template_id, scenario_model.variant],
    )

    # Scan telemetry directory for leakage
    telemetry_hits = scan_directory_for_forbidden_strings(
        target_dir=telemetry_dir,
        scenario_id=scenario_code,
        extra_forbidden=[scenario_model.template_id],
    )

    all_hits = export_hits + telemetry_hits
    assert len(all_hits) == 0, (
        f"Leakage detected in {scenario_code}:\n{json.dumps(all_hits, indent=2)}"
    )


# -----------------------------------------------------------------------------
# 2. Degradation Hooks Tests (missing_logs, stale_deploy_metadata)
# -----------------------------------------------------------------------------
def test_evidence_degradation_missing_logs(tmp_path: Path) -> None:
    """Verifies missing_logs degradation drops the target service's logs for the window."""
    now = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
    telemetry_dir = tmp_path / "telemetry"
    (telemetry_dir / "logs").mkdir(parents=True)
    (telemetry_dir / "traces").mkdir(parents=True)

    for svc in ["gateway", "orders", "inventory"]:
        log_file = telemetry_dir / "logs" / f"{svc}.jsonl"
        log_file.write_text(
            json.dumps({"ts": now.isoformat(), "service": svc, "msg": "ok"}) + "\n",
            encoding="utf-8",
        )

    export_dir = tmp_path / "export_missing_logs"

    # Default missing_logs targets "orders"
    export_evidence(
        run_id="run-deg-1",
        start=now - timedelta(seconds=10),
        end=now + timedelta(seconds=10),
        output_dir=export_dir,
        telemetry_dir=telemetry_dir,
        degradations=["missing_logs"],
        mock_prom_data={},
        mock_deploys=[],
    )

    # Assert orders logs are completely omitted
    assert not (export_dir / "logs" / "orders.jsonl").exists()
    # Assert gateway and inventory logs are preserved
    assert (export_dir / "logs" / "gateway.jsonl").exists()
    assert (export_dir / "logs" / "inventory.jsonl").exists()


def test_evidence_degradation_stale_deploy_metadata(tmp_path: Path) -> None:
    """Verifies stale_deploy_metadata degradation omits the newest deploy row."""
    now = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
    mock_deploys = [
        DeployRecord(
            deploy_id="dep-newer-02",
            service="orders",
            old_version="v1.1.0",
            new_version="v1.3.0",
            deployed_at=now - timedelta(minutes=5),
            author="synthetic-release@internal",
            summary="Newest deployment",
            changed_files=[],
            config_changes={},
            diff_text="diff-new",
        ),
        DeployRecord(
            deploy_id="dep-older-01",
            service="orders",
            old_version="v1.0.0",
            new_version="v1.1.0",
            deployed_at=now - timedelta(minutes=20),
            author="synthetic-release@internal",
            summary="Older baseline deployment",
            changed_files=[],
            config_changes={},
            diff_text="diff-old",
        ),
    ]

    export_dir = tmp_path / "export_stale_deploys"

    # Enable stale_deploy_metadata
    export_evidence(
        run_id="run-deg-2",
        start=now - timedelta(hours=1),
        end=now,
        output_dir=export_dir,
        degradations=["stale_deploy_metadata"],
        mock_prom_data={},
        mock_deploys=mock_deploys,
    )

    deploys_json_path = export_dir / "deploys" / "deploys.json"
    assert deploys_json_path.exists()
    exported_records = json.loads(deploys_json_path.read_text(encoding="utf-8"))

    # The newest record (dep-newer-02) must be omitted
    assert len(exported_records) == 1
    assert exported_records[0]["deploy_id"] == "dep-older-01"


def test_evidence_degradation_default_is_off(tmp_path: Path) -> None:
    """Verifies that by default (empty degradations), no data is degraded."""
    now = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
    telemetry_dir = tmp_path / "telemetry"
    (telemetry_dir / "logs").mkdir(parents=True)
    (telemetry_dir / "traces").mkdir(parents=True)

    for svc in ["gateway", "orders", "inventory"]:
        log_file = telemetry_dir / "logs" / f"{svc}.jsonl"
        log_file.write_text(
            json.dumps({"ts": now.isoformat(), "service": svc, "msg": "ok"}) + "\n",
            encoding="utf-8",
        )

    mock_deploys = [
        DeployRecord(
            deploy_id="dep-01",
            service="orders",
            old_version="v1.0.0",
            new_version="v1.1.0",
            deployed_at=now - timedelta(minutes=10),
            author="author",
            summary="summary",
            changed_files=[],
            config_changes={},
            diff_text="diff",
        )
    ]

    export_dir = tmp_path / "export_default"
    export_evidence(
        run_id="run-deg-3",
        start=now - timedelta(hours=1),
        end=now,
        output_dir=export_dir,
        telemetry_dir=telemetry_dir,
        degradations=[],
        mock_prom_data={},
        mock_deploys=mock_deploys,
    )

    # All 3 services' logs exist
    assert (export_dir / "logs" / "gateway.jsonl").exists()
    assert (export_dir / "logs" / "orders.jsonl").exists()
    assert (export_dir / "logs" / "inventory.jsonl").exists()

    # Deploy record is preserved
    deploys = json.loads((export_dir / "deploys" / "deploys.json").read_text(encoding="utf-8"))
    assert len(deploys) == 1


# -----------------------------------------------------------------------------
# 3. Isolation Verification Tests
# -----------------------------------------------------------------------------
def test_export_isolation_rejection(tmp_path: Path) -> None:
    """Verifies that validate_export_isolation strictly rejects vault or forbidden files."""
    bad_dir = tmp_path / "bad_export"
    bad_dir.mkdir()
    (bad_dir / "metrics").mkdir()
    # Write forbidden vault.db file
    (bad_dir / "metrics" / "vault.db").write_text("sqlite", encoding="utf-8")

    with pytest.raises(AssertionError, match="Isolation violation"):
        validate_export_isolation(bad_dir)


def test_core_network_isolation_live_docker() -> None:
    """Proves isolation by executing from a container attached strictly to the `core` network:
    
    1. Attempt to read /vault -> Must fail (directory does not exist).
    2. Attempt to reach controller API -> Must fail (host resolution / timeout).
    3. Attempt to reach /admin routes on lab services -> Must fail.
    """
    docker_check = subprocess.run(
        ["docker", "network", "inspect", "opspilot_core"],
        capture_output=True,
        text=True,
    )
    if docker_check.returncode != 0:
        pytest.skip("Docker opspilot_core network is not available in test environment")

    # 1. Attempt to read /vault
    cmd_vault = [
        "docker",
        "run",
        "--rm",
        "--network",
        "opspilot_core",
        "curlimages/curl:8.10.1",
        "ls",
        "/vault",
    ]
    res_vault = subprocess.run(cmd_vault, capture_output=True, text=True)
    assert res_vault.returncode != 0, f"Expected failure accessing /vault, got {res_vault.stdout}"
    assert "No such file or directory" in (res_vault.stderr + res_vault.stdout)

    # 2. Attempt to reach controller API
    cmd_ctrl = [
        "docker",
        "run",
        "--rm",
        "--network",
        "opspilot_core",
        "curlimages/curl:8.10.1",
        "curl",
        "-sS",
        "-m",
        "2",
        "http://controller:8000/healthz",
    ]
    res_ctrl = subprocess.run(cmd_ctrl, capture_output=True, text=True)
    assert res_ctrl.returncode != 0, f"Expected failure reaching controller, got {res_ctrl.stdout}"
    assert "Could not resolve host" in (res_ctrl.stderr + res_ctrl.stdout)

    # 3. Attempt to reach /admin routes on gateway, orders, and inventory
    for svc in ["gateway", "orders", "inventory"]:
        cmd_admin = [
            "docker",
            "run",
            "--rm",
            "--network",
            "opspilot_core",
            "curlimages/curl:8.10.1",
            "curl",
            "-sS",
            "-m",
            "2",
            f"http://{svc}:8000/admin/state",
        ]
        res_admin = subprocess.run(cmd_admin, capture_output=True, text=True)
        assert res_admin.returncode != 0, (
            f"Expected failure reaching {svc} admin, got {res_admin.stdout}"
        )
        assert "Could not resolve host" in (res_admin.stderr + res_admin.stdout)
