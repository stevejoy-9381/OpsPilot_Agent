"""TenantContext model representing the authenticated tenant and optional actor."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    """Carries the authenticated tenant scope and optional actor identity.

    Every repository and service method requires a TenantContext to ensure
    all queries run within the explicit tenant boundary.
    """

    tenant_id: UUID
    user_id: UUID | None = None
    role: str = "viewer"

    @classmethod
    def from_str(
        cls, tenant_id_str: str, user_id_str: str | None = None, role: str = "viewer"
    ) -> TenantContext:
        return cls(
            tenant_id=UUID(tenant_id_str),
            user_id=UUID(user_id_str) if user_id_str else None,
            role=role,
        )
