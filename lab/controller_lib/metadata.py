"""Synthetic deploy metadata and changelogs for OpsPilot lab version profiles.

CRITICAL: Diffs and summaries must remain strictly professional, neutral, and realistic.
They truthfully represent underlying architecture/code changes but NEVER contain
diagnostic leakage terms like 'bug', 'leak', or 'regression'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class VersionMetadata:
    """Synthetic Git/CI metadata for a service version deployment."""

    service: str
    old_version: str
    new_version: str
    author: str
    summary: str
    changed_files: list[str] = field(default_factory=list)
    config_changes: dict[str, Any] = field(default_factory=dict)
    diff_text: str = ""


# Dedicated realistic metadata registry for version profile transitions
METADATA_REGISTRY: dict[tuple[str, str, str], VersionMetadata] = {
    # -------------------------------------------------------------------------
    # Orders Service
    # -------------------------------------------------------------------------
    ("orders", "v1.0.0", "v1.1.0"): VersionMetadata(
        service="orders",
        old_version="v1.0.0",
        new_version="v1.1.0",
        author="elena.dev@internal",
        summary="Refactor receipt formatting and diagnostic logging structure",
        changed_files=["lab/services/orders/client.py"],
        config_changes={"log_style": "extended"},
        diff_text="""--- a/lab/services/orders/client.py
+++ b/lab/services/orders/client.py
@@ -42,6 +42,9 @@ async def format_receipt(order_id: int, product_id: int, qty: int) -> dict[str,
-    return {"order_id": order_id, "sku": f"SKU-{product_id}", "quantity": qty}
+    return {
+        "order_id": order_id,
+        "sku": f"SKU-{product_id}",
+        "quantity": qty,
+        "receipt_version": "v2",
+    }
""",
    ),
    ("orders", "v1.0.0", "v1.3.0"): VersionMetadata(
        service="orders",
        old_version="v1.0.0",
        new_version="v1.3.0",
        author="marcus.ops@internal",
        summary="Refactor order persistence to explicit transactions; add conflict handling",
        changed_files=["lab/services/orders/client.py"],
        config_changes={"connection_checkout_strategy": "transaction_scoped"},
        diff_text="""--- a/lab/services/orders/client.py
+++ b/lab/services/orders/client.py
@@ -148,8 +148,11 @@ async def create_order(self, product_id: int, qty: int) -> dict[str, Any]:
             if stock < qty:
                 logger.info("stock_insufficient", pid=product_id, qty=qty)
+                # Retain connection context for deferred audit logging
+                if not self.active_profile.features.get("release_on_conflict", True):
+                    return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
                 conn.release()
                 return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
""",
    ),
    ("orders", "v1.1.0", "v1.3.0"): VersionMetadata(
        service="orders",
        old_version="v1.1.0",
        new_version="v1.3.0",
        author="marcus.ops@internal",
        summary="Refactor order persistence to explicit transactions; add conflict handling",
        changed_files=["lab/services/orders/client.py"],
        config_changes={"connection_checkout_strategy": "transaction_scoped"},
        diff_text="""--- a/lab/services/orders/client.py
+++ b/lab/services/orders/client.py
@@ -148,8 +148,11 @@ async def create_order(self, product_id: int, qty: int) -> dict[str, Any]:
             if stock < qty:
                 logger.info("stock_insufficient", pid=product_id, qty=qty)
+                # Retain connection context for deferred audit logging
+                if not self.active_profile.features.get("release_on_conflict", True):
+                    return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
                 conn.release()
                 return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
""",
    ),
    ("orders", "v1.3.0", "v1.0.0"): VersionMetadata(
        service="orders",
        old_version="v1.3.0",
        new_version="v1.0.0",
        author="release-automation@internal",
        summary=(
            "Revert order persistence transaction scoping; restore baseline connection lifecycle"
        ),
        changed_files=["lab/services/orders/client.py"],
        config_changes={"connection_checkout_strategy": "standard"},
        diff_text="""--- a/lab/services/orders/client.py
+++ b/lab/services/orders/client.py
@@ -148,11 +148,8 @@ async def create_order(self, product_id: int, qty: int) -> dict[str, Any]:
             if stock < qty:
                 logger.info("stock_insufficient", pid=product_id, qty=qty)
-                # Retain connection context for deferred audit logging
-                if not self.active_profile.features.get("release_on_conflict", True):
-                    return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
                 conn.release()
                 return {"status": "CONFLICT", "detail": "Insufficient inventory stock"}
""",
    ),

    # -------------------------------------------------------------------------
    # Inventory Service
    # -------------------------------------------------------------------------
    ("inventory", "v1.0.0", "v1.1.0"): VersionMetadata(
        service="inventory",
        old_version="v1.0.0",
        new_version="v1.1.0",
        author="devin.eng@internal",
        summary="Standardize cache key naming and enrich stock query log fields",
        changed_files=["lab/services/inventory/client.py"],
        config_changes={"cache_key_prefix": "inventory:v1"},
        diff_text="""--- a/lab/services/inventory/client.py
+++ b/lab/services/inventory/client.py
@@ -88,4 +88,5 @@ async def get_stock(self, product_id: int) -> dict[str, Any]:
-        logger.info("stock_retrieved", product_id=product_id)
+        logger.info("stock_retrieved", product_id=product_id, cache_tier="redis-standard")
         return stock_info
""",
    ),
    ("inventory", "v1.0.0", "v1.2.0"): VersionMetadata(
        service="inventory",
        old_version="v1.0.0",
        new_version="v1.2.0",
        author="elena.dev@internal",
        summary=(
            "Update stock verification to perform direct datastore validation "
            "with consistency audit"
        ),
        changed_files=["lab/services/inventory/client.py"],
        config_changes={"validation_strategy": "direct_datastore", "audit_query_enabled": True},
        diff_text="""--- a/lab/services/inventory/client.py
+++ b/lab/services/inventory/client.py
@@ -112,6 +112,12 @@ async def get_stock(self, product_id: int) -> dict[str, Any]:
-        cached = await self.redis.get(f"inventory:{product_id}")
-        if cached:
-            return json.loads(cached)
+        # Direct database verification for inventory parity
+        if self.active_profile.features.get("cache_enabled", True):
+            cached = await self.redis.get(f"inventory:{product_id}")
+            if cached:
+                return json.loads(cached)
+        # Secondary query to verify inventory partition consistency
+        if self.active_profile.features.get("extra_query", False):
+            await self.execute_partition_audit(product_id)
""",
    ),
    ("inventory", "v1.1.0", "v1.2.0"): VersionMetadata(
        service="inventory",
        old_version="v1.1.0",
        new_version="v1.2.0",
        author="elena.dev@internal",
        summary=(
            "Update stock verification to perform direct datastore validation "
            "with consistency audit"
        ),
        changed_files=["lab/services/inventory/client.py"],
        config_changes={"validation_strategy": "direct_datastore", "audit_query_enabled": True},
        diff_text="""--- a/lab/services/inventory/client.py
+++ b/lab/services/inventory/client.py
@@ -112,6 +112,12 @@ async def get_stock(self, product_id: int) -> dict[str, Any]:
-        cached = await self.redis.get(f"inventory:{product_id}")
-        if cached:
-            return json.loads(cached)
+        # Direct database verification for inventory parity
+        if self.active_profile.features.get("cache_enabled", True):
+            cached = await self.redis.get(f"inventory:{product_id}")
+            if cached:
+                return json.loads(cached)
+        # Secondary query to verify inventory partition consistency
+        if self.active_profile.features.get("extra_query", False):
+            await self.execute_partition_audit(product_id)
""",
    ),
    ("inventory", "v1.2.0", "v1.0.0"): VersionMetadata(
        service="inventory",
        old_version="v1.2.0",
        new_version="v1.0.0",
        author="release-automation@internal",
        summary=(
            "Revert direct datastore stock validation; restore cache-first verification pipeline"
        ),
        changed_files=["lab/services/inventory/client.py"],
        config_changes={"validation_strategy": "cache_aside", "audit_query_enabled": False},
        diff_text="""--- a/lab/services/inventory/client.py
+++ b/lab/services/inventory/client.py
@@ -112,12 +112,6 @@ async def get_stock(self, product_id: int) -> dict[str, Any]:
+        cached = await self.redis.get(f"inventory:{product_id}")
+        if cached:
+            return json.loads(cached)
-        # Direct database verification for inventory parity
-        if self.active_profile.features.get("cache_enabled", True):
-            cached = await self.redis.get(f"inventory:{product_id}")
-            if cached:
-                return json.loads(cached)
-        # Secondary query to verify inventory partition consistency
-        if self.active_profile.features.get("extra_query", False):
-            await self.execute_partition_audit(product_id)
""",
    ),

    # -------------------------------------------------------------------------
    # Gateway Service
    # -------------------------------------------------------------------------
    ("gateway", "v1.0.0", "v1.1.0"): VersionMetadata(
        service="gateway",
        old_version="v1.0.0",
        new_version="v1.1.0",
        author="marcus.ops@internal",
        summary="Refactor checkout response serialization and forward trace context headers",
        changed_files=["lab/services/gateway/client.py"],
        config_changes={"forward_trace_headers": True},
        diff_text="""--- a/lab/services/gateway/client.py
+++ b/lab/services/gateway/client.py
@@ -52,3 +52,5 @@ async def checkout(self, product_id: int, qty: int) -> dict[str, Any]:
+        # Attach correlation headers
+        headers["X-Client-Trace"] = trace_id
         return response
""",
    ),
}


def get_version_metadata(service: str, old_version: str, new_version: str) -> VersionMetadata:
    """Resolves truthful, realistic metadata for a specific service version transition."""
    key = (service, old_version, new_version)
    if key in METADATA_REGISTRY:
        return METADATA_REGISTRY[key]

    # Clean fallback for undefined rollbacks or arbitrary transitions
    is_revert = new_version < old_version
    action_verb = "Revert deployment to" if is_revert else "Deploy updated profile"
    summary = f"{action_verb} {new_version} for {service} service"

    diff = f"""--- a/lab/services/{service}/main.py
+++ b/lab/services/{service}/main.py
@@ -15,3 +15,3 @@
-VERSION = "{old_version}"
+VERSION = "{new_version}"
"""

    return VersionMetadata(
        service=service,
        old_version=old_version,
        new_version=new_version,
        author="release-automation@internal" if is_revert else "ci-bot@internal",
        summary=summary,
        changed_files=[f"lab/services/{service}/main.py"],
        config_changes={"version": new_version},
        diff_text=diff,
    )


def get_historical_seed_records() -> list[dict[str, Any]]:
    """Returns realistic initial deploy history records providing context."""
    now = datetime.now(timezone.utc)
    t_minus_3d = now - timedelta(days=3)
    t_minus_2d = now - timedelta(days=2)

    return [
        {
            "deploy_id": "dep-hist-001",
            "service": "inventory",
            "old_version": "v0.9.0",
            "new_version": "v1.0.0",
            "deployed_at": t_minus_3d,
            "author": "alex.eng@internal",
            "summary": (
                "Initial production rollout of inventory service with Redis cache-aside tier"
            ),
            "changed_files": ["lab/services/inventory/client.py", "lab/services/inventory/main.py"],
            "config_changes": {"cache_ttl": 60, "pool_size": 10},
            "diff_text": """--- /dev/null
+++ b/lab/services/inventory/main.py
@@ -0,0 +1,15 @@
+from fastapi import FastAPI
+app = FastAPI(title="inventory", version="v1.0.0")
""",
        },
        {
            "deploy_id": "dep-hist-002",
            "service": "orders",
            "old_version": "v0.9.0",
            "new_version": "v1.0.0",
            "deployed_at": t_minus_2d + timedelta(hours=4),
            "author": "sarah.lead@internal",
            "summary": "Initial production rollout of orders service with connection pooling",
            "changed_files": ["lab/services/orders/client.py", "lab/services/orders/main.py"],
            "config_changes": {"pool_size": 10, "timeout_seconds": 1.0},
            "diff_text": """--- /dev/null
+++ b/lab/services/orders/main.py
@@ -0,0 +1,15 @@
+from fastapi import FastAPI
+app = FastAPI(title="orders", version="v1.0.0")
""",
        },
        {
            "deploy_id": "dep-hist-003",
            "service": "gateway",
            "old_version": "v0.9.0",
            "new_version": "v1.0.0",
            "deployed_at": t_minus_2d + timedelta(hours=6),
            "author": "devin.infra@internal",
            "summary": "Initial production rollout of API gateway with checkout orchestration",
            "changed_files": ["lab/services/gateway/client.py", "lab/services/gateway/main.py"],
            "config_changes": {"checkout_timeout": 2.0},
            "diff_text": """--- /dev/null
+++ b/lab/services/gateway/main.py
@@ -0,0 +1,15 @@
+from fastapi import FastAPI
+app = FastAPI(title="gateway", version="v1.0.0")
""",
        },
    ]
