
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LabSettings(BaseSettings):
    """Shared settings for all OpsPilot lab services."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    service_name: str = Field(default="toy-service", validation_alias="SERVICE_NAME")
    version: str = Field(default="v1.0.0", validation_alias="VERSION")
    lab_admin_token: SecretStr = Field(
        default=SecretStr("lab-admin-dev-token"), validation_alias="LAB_ADMIN_TOKEN"
    )

    # Downstream service URLs
    gateway_url: str = Field(default="http://gateway:8000", validation_alias="GATEWAY_URL")
    orders_url: str = Field(default="http://orders:8001", validation_alias="ORDERS_URL")
    inventory_url: str = Field(default="http://inventory:8002", validation_alias="INVENTORY_URL")
    database_url: str = Field(
        default="postgresql+psycopg://opspilot:opspilot_dev_password@127.0.0.1:5432/opspilot",
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", validation_alias="REDIS_URL")

    # Connection pool configuration
    db_pool_min_size: int = Field(default=2, validation_alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, validation_alias="DB_POOL_MAX_SIZE")

    # Timeouts (in seconds)
    default_timeout_s: float = Field(default=5.0, validation_alias="DEFAULT_TIMEOUT_S")
    db_timeout_s: float = Field(default=5.0, validation_alias="DB_TIMEOUT_S")
    redis_timeout_s: float = Field(default=2.0, validation_alias="REDIS_TIMEOUT_S")

    # Telemetry and logging
    telemetry_log_dir: str = Field(
        default="/telemetry/logs", validation_alias="TELEMETRY_LOG_DIR"
    )
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Admin persistence & test-only failure hooks
    admin_database_url: str = Field(
        default="postgresql://controller_admin:controller-admin-local-pw@lab-postgres:5432/lab",
        validation_alias="LAB_ADMIN_DATABASE_URL",
    )
    lab_test_hooks_enabled: bool = Field(
        default=False, validation_alias="LAB_TEST_HOOKS_ENABLED"
    )
