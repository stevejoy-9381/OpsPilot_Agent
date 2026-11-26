"""Pydantic settings for the isolated Execution Broker process."""

from __future__ import annotations

import os
import socket
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.targets import DEFAULT_LAB_TARGETS_PATH


class BrokerSettings(BaseSettings):
    """Fail-fast configuration for the Execution Broker.

    Secrets (database_url, lab_admin_token) are required SecretStr fields
    and must be provided via environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    lab_admin_token: SecretStr = Field(validation_alias="LAB_ADMIN_TOKEN")
    lab_targets_path: Path = Field(
        default=DEFAULT_LAB_TARGETS_PATH,
        validation_alias="LAB_TARGETS_PATH",
    )
    poll_interval_seconds: float = Field(
        default=0.5, ge=0.05, validation_alias="BROKER_POLL_INTERVAL_SECONDS"
    )
    heartbeat_seconds: float = Field(
        default=5.0, ge=0.5, validation_alias="BROKER_HEARTBEAT_SECONDS"
    )
    lease_seconds: int = Field(default=30, ge=5, validation_alias="BROKER_LEASE_SECONDS")
    max_attempts: int = Field(default=3, ge=1, validation_alias="BROKER_MAX_ATTEMPTS")
    retry_backoff_seconds: float = Field(
        default=0.25, ge=0.0, validation_alias="BROKER_RETRY_BACKOFF_SECONDS"
    )
    reconcile_deadline_seconds: float = Field(
        default=15.0, ge=0.05, validation_alias="BROKER_RECONCILE_DEADLINE_SECONDS"
    )
    healthz_host: str = Field(default="127.0.0.1", validation_alias="BROKER_HEALTHZ_HOST")
    healthz_port: int = Field(default=8090, validation_alias="BROKER_HEALTHZ_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    broker_id: str = Field(
        default_factory=lambda: f"broker-{socket.gethostname()}-{os.getpid()}",
        validation_alias="BROKER_ID",
    )
