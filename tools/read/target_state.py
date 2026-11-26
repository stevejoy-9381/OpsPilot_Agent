"""Read-only TargetStateReader implementations for OpsPilot.

Isolation invariants:
- SnapshotTargetReader reads strictly from local evidence snapshots and fixtures.
- LiveTargetReader connects strictly using the read-only `evidence_ro` Postgres role
  on `lab.deploys` over the `evidence` network.
- Neither implementation imports an HTTP client or calls any admin, controller,
  vault, or broker endpoint.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from shared.targets import (
    TargetRegistry,
    TargetState,
    TargetStateError,
    TargetStateErrorCode,
    load_target_registry,
)

DEFAULT_TARGET_STATE_MAX_AGE_SECONDS: int = int(
    os.environ.get("TARGET_STATE_MAX_AGE_SECONDS", "3600")
)


def _to_utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        raw = dt.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    else:
        parsed = dt
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _check_freshness(
    target_id: str,
    as_of_utc: datetime,
    read_at_utc: datetime,
    max_age_seconds: int,
) -> TargetStateError | None:
    age_seconds = (as_of_utc - read_at_utc).total_seconds()
    if age_seconds > max_age_seconds:
        return TargetStateError(
            code=TargetStateErrorCode.STALE_STATE,
            target_id=target_id,
            message=(
                f"Target state for '{target_id}' is stale "
                f"(age={age_seconds:.1f}s > max_age={max_age_seconds}s)"
            ),
            details={"age_seconds": age_seconds, "max_age_seconds": max_age_seconds},
        )
    return None


class SnapshotTargetReader:
    """Offline, deterministic TargetStateReader backed by evidence snapshots & fixtures."""

    def __init__(
        self,
        snapshot_dir: Path | str,
        registry: TargetRegistry | None = None,
        deploy_history: list[dict[str, Any]] | None = None,
        last_action_by_target: dict[str, datetime | str] | None = None,
        snapshot_read_at: datetime | str | None = None,
        max_age_seconds: int = DEFAULT_TARGET_STATE_MAX_AGE_SECONDS,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.registry = registry or load_target_registry()
        self.deploy_history = deploy_history
        self.last_action_by_target = last_action_by_target or {}
        self.snapshot_read_at = _to_utc(snapshot_read_at) if snapshot_read_at is not None else None
        self.max_age_seconds = max_age_seconds

    def current_state(
        self,
        target_id: str,
        as_of: datetime,
    ) -> TargetState | TargetStateError:
        as_of_utc = _to_utc(as_of)
        spec = self.registry.get_by_target_id(target_id)
        if spec is None:
            return TargetStateError(
                code=TargetStateErrorCode.TARGET_NOT_FOUND,
                target_id=target_id,
                message=f"Target '{target_id}' is not in lab target registry",
            )

        try:
            records = self._load_records(spec.service, as_of_utc)
        except Exception as exc:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=spec.target_id,
                message=f"Failed to load snapshot deploy state for '{spec.service}': {exc}",
            )

        if not records:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=spec.target_id,
                message=f"No deploy records found for '{spec.service}' on or before {as_of_utc.isoformat()}",
            )

        latest = records[0]
        current_version = str(latest.get("new_version") or latest.get("version") or "").strip()
        if not current_version:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=spec.target_id,
                message=f"Latest deploy record for '{spec.service}' is missing version",
            )

        previous_version_raw = latest.get("old_version")
        previous_version: str | None = (
            str(previous_version_raw).strip() if previous_version_raw is not None else None
        )
        if previous_version is None and len(records) > 1:
            prev_rec = records[1]
            prev_ver = prev_rec.get("new_version") or prev_rec.get("version")
            if prev_ver is not None:
                previous_version = str(prev_ver).strip()

        read_at_utc = (
            self.snapshot_read_at
            if self.snapshot_read_at is not None
            else _to_utc(latest["deployed_at"])
        )
        stale_err = _check_freshness(
            spec.target_id, as_of_utc, read_at_utc, self.max_age_seconds
        )
        if stale_err is not None:
            return stale_err

        raw_last_action = self.last_action_by_target.get(spec.target_id)
        last_action_at = _to_utc(raw_last_action) if raw_last_action is not None else None

        return TargetState(
            target_id=spec.target_id,
            current_version=current_version,
            previous_version=previous_version,
            last_action_at=last_action_at,
            source="snapshot",
            read_at=read_at_utc,
        )

    def _load_records(self, service: str, as_of_utc: datetime) -> list[dict[str, Any]]:
        if self.deploy_history is not None:
            raw_list = list(self.deploy_history)
        else:
            deploys_file = self.snapshot_dir / "deploys" / "deploys.json"
            if not deploys_file.exists():
                return []
            raw_list = json.loads(deploys_file.read_text(encoding="utf-8"))

        filtered: list[dict[str, Any]] = []
        for rec in raw_list:
            if str(rec.get("service", "")).strip().lower() != service:
                continue
            dep_at_raw = rec.get("deployed_at")
            if dep_at_raw is None:
                continue
            dep_at = _to_utc(dep_at_raw)
            if dep_at <= as_of_utc:
                filtered.append({**rec, "_dep_at": dep_at})

        filtered.sort(key=lambda r: r["_dep_at"], reverse=True)
        return filtered


class LiveTargetReader:
    """Read-only TargetStateReader querying lab.deploys via evidence_ro role."""

    def __init__(
        self,
        evidence_db_dsn: str,
        registry: TargetRegistry | None = None,
        max_age_seconds: int = DEFAULT_TARGET_STATE_MAX_AGE_SECONDS,
        connect_timeout: int = 3,
    ) -> None:
        self.evidence_db_dsn = evidence_db_dsn
        self.registry = registry or load_target_registry()
        self.max_age_seconds = max_age_seconds
        self.connect_timeout = connect_timeout

    def current_state(
        self,
        target_id: str,
        as_of: datetime,
    ) -> TargetState | TargetStateError:
        as_of_utc = _to_utc(as_of)
        spec = self.registry.get_by_target_id(target_id)
        if spec is None:
            return TargetStateError(
                code=TargetStateErrorCode.TARGET_NOT_FOUND,
                target_id=target_id,
                message=f"Target '{target_id}' is not in lab target registry",
            )

        try:
            with psycopg.connect(
                self.evidence_db_dsn,
                connect_timeout=self.connect_timeout,
                autocommit=True,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT new_version, old_version, deployed_at, clock_timestamp()
                        FROM lab.deploys
                        WHERE service = %s AND deployed_at <= %s
                        ORDER BY deployed_at DESC
                        LIMIT 1;
                        """,
                        (spec.service, as_of_utc),
                    )
                    row = cur.fetchone()
        except Exception as exc:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=spec.target_id,
                message=f"Failed to query lab.deploys as evidence_ro: {exc}",
            )

        if row is None:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=spec.target_id,
                message=f"No deploy row found in lab.deploys for '{spec.service}'",
            )

        current_version = str(row[0]).strip()
        previous_version = str(row[1]).strip() if row[1] is not None else None
        read_at_utc = _to_utc(row[3])

        stale_err = _check_freshness(
            spec.target_id, as_of_utc, read_at_utc, self.max_age_seconds
        )
        if stale_err is not None:
            return stale_err

        return TargetState(
            target_id=spec.target_id,
            current_version=current_version,
            previous_version=previous_version,
            last_action_at=None,
            source="live_deploys",
            read_at=read_at_utc,
        )
