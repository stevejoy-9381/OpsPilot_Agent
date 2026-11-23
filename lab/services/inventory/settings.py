from pydantic import Field

from lab.common.settings import LabSettings


class InventorySettings(LabSettings):
    """Configuration settings for the inventory toy service."""

    service_name: str = Field(default="inventory", validation_alias="SERVICE_NAME")
    version: str = Field(default="v1.0.0", validation_alias="VERSION")

    # Inventory role has read-only access to products and inventory tables
    database_url: str = Field(
        default="postgresql://inventory_ro:inv-ro-local-pw@127.0.0.1:5432/lab",
        validation_alias="LAB_DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        validation_alias="LAB_REDIS_URL",
    )

    db_pool_size: int = Field(default=10, validation_alias="DB_POOL_SIZE")
    redis_timeout_s: float = Field(default=0.150, validation_alias="REDIS_TIMEOUT_S")
    cache_ttl_s: int = Field(default=60, validation_alias="CACHE_TTL_S")
