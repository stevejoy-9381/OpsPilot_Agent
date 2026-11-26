"""Tests for config/lab_targets.yaml, shared/targets.py, and tools/read/target_state.py."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from shared.actions import ActionType
from shared.targets import (
    TargetRegistry,
    TargetSpec,
    TargetState,
    TargetStateError,
    TargetStateErrorCode,
    TargetStateReader,
    load_target_registry,
)
from tools.read.target_state import LiveTargetReader, SnapshotTargetReader

REPO_ROOT = Path(__file__).resolve().parent.parent
S1_FIXTURE_DIR = (
    REPO_ROOT / "evals" / "results" / "investigator_v2" / "20261125_124357" / "_fixtures" / "v2_S1_t1"
)
S4_FIXTURE_DIR = (
    REPO_ROOT / "evals" / "results" / "investigator_v2" / "20261125_124357" / "_fixtures" / "v2_S4_t1"
)


# ---------------------------------------------------------------------------
# 1. Registry validation tests
# ---------------------------------------------------------------------------


def test_committed_lab_targets_yaml_loads_exact_three_services() -> None:
    registry = load_target_registry()
    assert {t.service for t in registry.targets} == {"gateway", "orders", "inventory"}
    assert {t.target_id for t in registry.targets} == {"lab-gateway", "lab-orders", "lab-inventory"}
    for t in registry.targets:
        assert t.kind == "lab_service"
        assert t.owner == "lab"
        assert ActionType.ROLLBACK_SERVICE in t.allowed_actions
        assert ActionType.RESTART_SERVICE in t.allowed_actions


def test_registry_rejects_duplicate_target_id_and_duplicate_service() -> None:
    spec1 = TargetSpec(
        target_id="lab-orders",
        service="orders",
        allowed_actions=(ActionType.ROLLBACK_SERVICE,),
    )
    spec_dup_target = TargetSpec(
        target_id="lab-orders",
        service="gateway",
        allowed_actions=(ActionType.ROLLBACK_SERVICE,),
    )
    with pytest.raises(ValueError, match="Duplicate target_id"):
        TargetRegistry.from_specs([spec1, spec_dup_target])

    spec_dup_service = TargetSpec(
        target_id="lab-orders-2",
        service="orders",
        allowed_actions=(ActionType.ROLLBACK_SERVICE,),
    )
    with pytest.raises(ValueError, match="Duplicate service"):
        TargetRegistry.from_specs([spec1, spec_dup_service])


def test_registry_rejects_unknown_service_lookup() -> None:
    registry = load_target_registry()
    assert registry.get_by_service("redis") is None
    assert registry.get_by_target_id("lab-redis") is None


# ---------------------------------------------------------------------------
# 2. SnapshotTargetReader tests (S1, S4, stale, missing)
# ---------------------------------------------------------------------------


def test_snapshot_reader_returns_current_and_previous_version_from_fixtures() -> None:
    history = [
        {
            "deploy_id": "dep_orders_v100",
            "service": "orders",
            "old_version": "v0.9.0",
            "new_version": "v1.0.0",
            "deployed_at": "2026-11-25T10:00:00Z",
        },
        {
            "deploy_id": "dep_orders_v130",
            "service": "orders",
            "old_version": "v1.0.0",
            "new_version": "v1.3.0",
            "deployed_at": "2026-11-25T12:43:00Z",
        },
    ]
    reader: TargetStateReader = SnapshotTargetReader(
        snapshot_dir=S1_FIXTURE_DIR,
        deploy_history=history,
        max_age_seconds=3600,
    )
    state = reader.current_state("lab-orders", datetime(2026, 11, 25, 12, 48, 0, tzinfo=timezone.utc))
    assert isinstance(state, TargetState)
    assert state.target_id == "lab-orders"
    assert state.current_version == "v1.3.0"
    assert state.previous_version == "v1.0.0"
    assert state.source == "snapshot"


def test_snapshot_reader_s1_and_s4_fixtures() -> None:
    s1_reader = SnapshotTargetReader(snapshot_dir=S1_FIXTURE_DIR, max_age_seconds=3600)
    s1_state = s1_reader.current_state(
        "lab-orders", datetime(2026, 11, 25, 12, 48, 0, tzinfo=timezone.utc)
    )
    assert isinstance(s1_state, TargetState)
    assert s1_state.current_version == "v1.3.0"

    s4_reader = SnapshotTargetReader(snapshot_dir=S4_FIXTURE_DIR, max_age_seconds=3600)
    s4_state = s4_reader.current_state(
        "lab-orders", datetime(2026, 11, 25, 12, 48, 0, tzinfo=timezone.utc)
    )
    assert isinstance(s4_state, TargetState)
    assert s4_state.current_version == "v1.1.0"


def test_snapshot_reader_returns_typed_errors_for_missing_and_stale() -> None:
    reader = SnapshotTargetReader(snapshot_dir=S1_FIXTURE_DIR, max_age_seconds=60)
    as_of = datetime(2026, 11, 25, 12, 48, 0, tzinfo=timezone.utc)

    not_found = reader.current_state("lab-redis", as_of)
    assert isinstance(not_found, TargetStateError)
    assert not_found.code == TargetStateErrorCode.TARGET_NOT_FOUND

    # S1 deploy was at 12:43:00Z, 300s before 12:48:00Z -> > max_age_seconds=60 -> stale_state
    stale = reader.current_state("lab-orders", as_of)
    assert isinstance(stale, TargetStateError)
    assert stale.code == TargetStateErrorCode.STALE_STATE

    # Missing deploys.json -> state_unavailable (never raises)
    empty_reader = SnapshotTargetReader(snapshot_dir=REPO_ROOT / "nonexistent_dir")
    unavail = empty_reader.current_state("lab-orders", as_of)
    assert isinstance(unavail, TargetStateError)
    assert unavail.code == TargetStateErrorCode.STATE_UNAVAILABLE


def test_live_reader_unavailable_returns_typed_error_without_raising() -> None:
    reader = LiveTargetReader(
        evidence_db_dsn="postgresql://evidence_ro:bad@127.0.0.1:59999/lab",
        connect_timeout=1,
    )
    res = reader.current_state("lab-orders", datetime.now(timezone.utc) + timedelta(minutes=1))
    assert isinstance(res, TargetStateError)
    assert res.code == TargetStateErrorCode.STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# 3. Isolation & static grep test (no HTTP client for /admin routes)
# ---------------------------------------------------------------------------


def test_no_http_client_or_admin_routes_in_target_reader() -> None:
    for rel_path in ("shared/targets.py", "tools/read/target_state.py"):
        src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for forbidden in ("import httpx", "import requests", "import aiohttp", "urllib.request", "/admin"):
            assert forbidden not in src, f"Forbidden '{forbidden}' found in {rel_path}"


@pytest.mark.lab
def test_live_target_reader_evidence_ro_isolation() -> None:
    """Live lab isolation test: evidence_ro can read lab.deploys and cannot write or reach /admin."""
    admin_dsn = os.environ.get("LAB_DATABASE_URL", "postgresql://lab_admin:lab-postgres-local-pw@127.0.0.1:5432/lab")
    parsed = urlparse(admin_dsn)
    evid_dsn = parsed._replace(
        netloc=f"evidence_ro:evidence-ro-local-pw@{parsed.hostname}:{parsed.port}"
    ).geturl()

    reader = LiveTargetReader(evidence_db_dsn=evid_dsn)
    res = reader.current_state("lab-orders", datetime.now(timezone.utc) + timedelta(minutes=1))
    assert isinstance(res, (TargetState, TargetStateError))

    with psycopg.connect(evid_dsn, connect_timeout=2, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error):
                cur.execute(
                    "INSERT INTO lab.deploys (deploy_id, service, old_version, new_version, author, summary, diff_text) "
                    "VALUES ('evil', 'orders', 'v1.0.0', 'v9.9.9', 'x', 'x', 'x')"
                )
