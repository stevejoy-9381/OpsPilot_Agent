"""SQLite-based secure Vault for ground truth storage.

Mounted ONLY into the controller service on a dedicated volume.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab.controller.models import TruthRecord


class Vault:
    """Encapsulates the SQLite vault storing immutable ground truth records."""

    def __init__(self, db_path: str = "vault.db") -> None:
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self) -> None:
        dir_name = os.path.dirname(self.db_path)
        if dir_name:
            Path(dir_name).mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    family TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    culprit_deploy_id TEXT,
                    injected_at TEXT NOT NULL,
                    expected_action TEXT NOT NULL,
                    decoys TEXT NOT NULL,
                    health_thresholds TEXT NOT NULL,
                    split_tag TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blind_packets (
                    packet_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def record_blind_packet(self, packet_id: str, run_id: str, scenario_id: str) -> None:
        """Stores a blind packet mapping secretly in the vault."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO blind_packets (packet_id, run_id, scenario_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (packet_id, run_id, scenario_id, now),
            )
            conn.commit()

    def get_blind_packet(self, packet_id: str) -> dict[str, Any] | None:
        """Retrieves answer key for a blind packet."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM blind_packets WHERE packet_id = ?", (packet_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def list_blind_packets(self) -> list[dict[str, Any]]:
        """Lists all blind packet answer keys stored in the vault."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM blind_packets ORDER BY created_at ASC")
            return [dict(row) for row in cursor.fetchall()]

    def record_run(self, truth: TruthRecord) -> None:
        """Stores a ground truth record in the vault."""
        root_cause_data = (
            truth.root_cause.model_dump()
            if hasattr(truth.root_cause, "model_dump")
            else truth.root_cause
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id,
                    scenario_id,
                    family,
                    root_cause,
                    culprit_deploy_id,
                    injected_at,
                    expected_action,
                    decoys,
                    health_thresholds,
                    split_tag,
                    created_at,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    truth.run_id,
                    truth.scenario_id,
                    truth.family,
                    json.dumps(root_cause_data),
                    truth.culprit_deploy_id,
                    truth.injected_at,
                    json.dumps(truth.expected_action),
                    json.dumps(truth.decoys),
                    json.dumps(truth.health_thresholds),
                    truth.split_tag,
                    truth.created_at,
                    truth.status,
                ),
            )
            conn.commit()

    def get_run(self, run_id: str) -> TruthRecord | None:
        """Retrieves a truth record by run_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    def update_status(self, run_id: str, status: str) -> None:
        """Updates execution status for a run."""
        with self._get_connection() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
            conn.commit()

    def list_runs(self) -> list[TruthRecord]:
        """Lists all registered runs."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM runs ORDER BY created_at DESC")
            return [self._row_to_record(row) for row in cursor.fetchall()]

    def _row_to_record(self, row: sqlite3.Row) -> TruthRecord:
        data: dict[str, Any] = dict(row)
        data["root_cause"] = json.loads(data["root_cause"])
        data["expected_action"] = json.loads(data["expected_action"])
        data["decoys"] = json.loads(data["decoys"])
        data["health_thresholds"] = json.loads(data["health_thresholds"])
        return TruthRecord(**data)
