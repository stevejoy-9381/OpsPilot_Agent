from pydantic import Field

from lab.common.settings import LabSettings


class OrdersSettings(LabSettings):
    """Configuration settings for the orders toy service."""

    service_name: str = Field(default="orders", validation_alias="SERVICE_NAME")
    version: str = Field(default="v1.0.0", validation_alias="VERSION")

    # Orders role has read on products/inventory, read/write on orders
    database_url: str = Field(
        default="postgresql://orders_rw:orders-rw-local-pw@127.0.0.1:5432/lab",
        validation_alias="LAB_DATABASE_URL",
    )
    inventory_url: str = Field(
        default="http://127.0.0.1:8001",
        validation_alias="INVENTORY_URL",
    )

    db_pool_size: int = Field(default=10, validation_alias="DB_POOL_SIZE")
    db_pool_timeout_s: float = Field(default=1.0, validation_alias="DB_POOL_TIMEOUT_S")
    inventory_timeout_s: float = Field(default=0.5, validation_alias="INVENTORY_TIMEOUT_S")
    inventory_retries: int = Field(default=1, validation_alias="INVENTORY_RETRIES")
