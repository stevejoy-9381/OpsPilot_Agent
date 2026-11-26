from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_user: str = Field(default="opspilot", alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(default=SecretStr(""), alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="opspilot", alias="POSTGRES_DB")
    database_url: SecretStr = Field(default=..., alias="DATABASE_URL")
    redis_url: SecretStr = Field(default=..., alias="REDIS_URL")
    gemini_api_key: SecretStr = Field(default=SecretStr(""), alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="replace-with-supported-model", alias="GEMINI_MODEL")
    max_tool_calls: int = Field(default=8, alias="MAX_TOOL_CALLS")
    run_timeout_seconds: int = Field(default=120, alias="RUN_TIMEOUT_SECONDS")
    approval_ttl_seconds: int = Field(default=900, alias="APPROVAL_TTL_SECONDS")
    target_state_max_age_seconds: int = Field(
        default=30, alias="TARGET_STATE_MAX_AGE_SECONDS"
    )
    enforce_separation_of_duties: bool = Field(
        default=False, alias="ENFORCE_SEPARATION_OF_DUTIES"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
