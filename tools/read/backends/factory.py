"""Backend factory for read tools.

Chooses backend implementation based on EVIDENCE_BACKEND environment variable:
- "snapshot" (default for offline/evals/tests): uses SnapshotBackend
- "live": uses LiveBackend connected to Prometheus, telemetry volume, and Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path
from tools.read.backends.base import DeployBackend, LogsBackend, MetricsBackend, TraceBackend
from tools.read.backends.live import LiveBackend
from tools.read.backends.snapshot import SnapshotBackend


def get_backend() -> SnapshotBackend | LiveBackend:
    backend_type = os.getenv("EVIDENCE_BACKEND", "snapshot").lower().strip()
    if backend_type == "live":
        prom_url = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
        telem_dir = os.getenv("TELEMETRY_DIR", "/var/log/opspilot")
        pg_dsn = os.getenv("POSTGRES_EVIDENCE_RO_DSN")
        return LiveBackend(
            prometheus_url=prom_url,
            telemetry_dir=telem_dir,
            postgres_dsn=pg_dsn,
        )
    else:
        snapshot_dir = os.getenv("SNAPSHOT_DIR", "tests/fixtures/snapshot")
        return SnapshotBackend(root_dir=Path(snapshot_dir))
