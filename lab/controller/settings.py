"""OpsPilot Lab Scenario Controller Configuration Settings."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControllerSettings(BaseSettings):
    """Runtime configuration for the isolated lab scenario controller."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    service_name: str = "controller"
    version: str = "v1.0.0"

    # Controller runner token (for starting runs, stopping, resetting)
    controller_token: SecretStr = Field(
        default=SecretStr("super-secret-controller-token"),
        validation_alias="CONTROLLER_TOKEN",
    )

    # Harness secret token (strictly for evaluators/graders to retrieve ground truth)
    harness_token: SecretStr = Field(
        default=SecretStr("super-secret-harness-token"),
        validation_alias="HARNESS_TOKEN",
    )

    # Secret token for authenticating against microservices' /admin endpoints
    lab_admin_token: SecretStr = Field(
        default=SecretStr("super-secret-admin-token"),
        validation_alias="LAB_ADMIN_TOKEN",
    )

    # Dedicated SQLite database vault path
    vault_db_path: str = Field(
        default="vault.db",
        validation_alias="VAULT_DB_PATH",
    )

    # Administrative and Prometheus URLs on the lab / lab-admin network
    gateway_url: str = Field(default="http://gateway:8000", validation_alias="GATEWAY_URL")
    orders_url: str = Field(default="http://orders:8000", validation_alias="ORDERS_URL")
    inventory_url: str = Field(default="http://inventory:8000", validation_alias="INVENTORY_URL")
    prometheus_url: str = Field(default="http://prometheus:9090", validation_alias="PROMETHEUS_URL")

    # Lab database URL for recording deploys via lab.controller_lib.deploy
    database_url: str = Field(
        default="postgresql://lab_admin:lab-postgres-local-pw@lab-postgres:5432/lab",
        validation_alias="LAB_DATABASE_URL",
    )
