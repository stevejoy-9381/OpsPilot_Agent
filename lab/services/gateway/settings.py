from pydantic import Field

from lab.common.settings import LabSettings


class GatewaySettings(LabSettings):
    """Configuration settings for the gateway service."""

    service_name: str = Field(default="gateway", validation_alias="SERVICE_NAME")
    version: str = Field(default="v1.0.0", validation_alias="VERSION")

    orders_url: str = Field(
        default="http://orders:8000",
        validation_alias="ORDERS_URL",
    )
    checkout_timeout_s: float = Field(default=2.0, validation_alias="CHECKOUT_TIMEOUT_S")
