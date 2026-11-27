"""TargetClient interface, FakeTarget, and LabAdminClient for the Execution Broker."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

from shared.targets import (
    TargetRegistry,
    TargetState,
    TargetStateError,
    TargetStateErrorCode,
    TargetStateReader,
)


class TargetActionOutcome(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    PRECONDITION_FAILED = "precondition_failed"
    CLIENT_ERROR = "client_error"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class TargetActionResult:
    """Typed, bounded result from a target admin action call."""

    outcome: TargetActionOutcome
    service: str
    action: str
    from_version: str | None = None
    to_version: str | None = None
    current_version: str | None = None
    expected_current_version: str | None = None
    http_status: int | None = None
    detail: str = ""
    applied_at: str | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        """Returns a bounded, token-free dict suitable for action_executions.target_response."""
        return {
            "outcome": self.outcome.value,
            "service": self.service,
            "action": self.action,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "current_version": self.current_version,
            "expected_current_version": self.expected_current_version,
            "http_status": self.http_status,
            "detail": self.detail[:256],
            "applied_at": self.applied_at,
        }


@dataclass(frozen=True)
class TargetActionLookup:
    """Typed result from GET /admin/actions/{action_key}."""

    status: str  # "applied" | "not_applied" | "unknown"
    service: str | None = None
    action: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    applied_at: str | None = None
    detail: str = ""


@runtime_checkable
class TargetClient(TargetStateReader, Protocol):
    """Protocol for reading target state and applying CAS admin actions."""

    def get_state(self, target_id: str) -> TargetState | TargetStateError:
        ...

    def apply_rollback(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
        to_version: str,
    ) -> TargetActionResult:
        ...

    def apply_restart(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
    ) -> TargetActionResult:
        ...

    def get_action_status(self, target_id: str, action_key: str) -> TargetActionLookup:
        ...


@dataclass
class FakeTargetFailureHooks:
    """Failure-injection modes matching Phase 9.3 lab hooks."""

    delay_before_response_ms: int = 0
    drop_response_after_apply: bool = False
    fail_before_apply: bool = False
    timeout_on_call: bool = False
    http_5xx_unparseable: bool = False
    http_4xx_error: bool = False


class FakeTarget:
    """In-memory TargetClient implementing Phase 9.3 CAS + dedup + failure hooks."""

    def __init__(
        self,
        initial_versions: dict[str, str] | None = None,
        registry: TargetRegistry | None = None,
    ) -> None:
        self.versions: dict[str, str] = dict(
            initial_versions
            or {
                "lab-gateway": "v1.0.0",
                "lab-orders": "v1.3.0",
                "lab-inventory": "v1.0.0",
            }
        )
        self.registry = registry
        self.applied_actions: dict[str, TargetActionResult] = {}
        self.restart_counts: dict[str, int] = {}
        self.hooks = FakeTargetFailureHooks()
        self.call_count: int = 0
        self.unavailable: bool = False

    def _service_for(self, target_id: str) -> str:
        if self.registry is not None:
            spec = self.registry.get_by_target_id(target_id)
            if spec is not None:
                return spec.service
        return target_id.removeprefix("lab-")

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        if self.unavailable or target_id not in self.versions:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=target_id,
                message=f"Target '{target_id}' unavailable",
            )
        return TargetState(
            target_id=target_id,
            current_version=self.versions[target_id],
            previous_version="v1.0.0",
            source="fake_target",
            read_at=as_of,
        )

    def get_state(self, target_id: str) -> TargetState | TargetStateError:
        return self.current_state(target_id, datetime.now(timezone.utc))

    def _check_pre_hooks(self, service: str, action: str) -> TargetActionResult | None:
        if self.hooks.delay_before_response_ms > 0:
            time.sleep(self.hooks.delay_before_response_ms / 1000.0)
        if self.hooks.timeout_on_call:
            raise httpx.ReadTimeout("Simulated target read timeout")
        if self.hooks.fail_before_apply:
            return TargetActionResult(
                outcome=TargetActionOutcome.UNCERTAIN,
                service=service,
                action=action,
                http_status=503,
                detail="Service unavailable before apply",
            )
        if self.hooks.http_5xx_unparseable:
            return TargetActionResult(
                outcome=TargetActionOutcome.UNCERTAIN,
                service=service,
                action=action,
                http_status=502,
                detail="Bad gateway (unparseable 5xx body)",
            )
        if self.hooks.http_4xx_error:
            return TargetActionResult(
                outcome=TargetActionOutcome.CLIENT_ERROR,
                service=service,
                action=action,
                http_status=400,
                detail="Invalid target action request",
            )
        return None

    def apply_rollback(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
        to_version: str,
    ) -> TargetActionResult:
        self.call_count += 1
        service = self._service_for(target_id)
        if (hook_res := self._check_pre_hooks(service, "rollback_service")) is not None:
            return hook_res

        if action_key in self.applied_actions:
            prev = self.applied_actions[action_key]
            return TargetActionResult(
                outcome=TargetActionOutcome.ALREADY_APPLIED,
                service=prev.service,
                action=prev.action,
                from_version=prev.from_version,
                to_version=prev.to_version,
                current_version=self.versions.get(target_id),
                http_status=200,
                applied_at=prev.applied_at,
            )

        current = self.versions.get(target_id)
        if current != expected_current_version:
            return TargetActionResult(
                outcome=TargetActionOutcome.PRECONDITION_FAILED,
                service=service,
                action="rollback_service",
                current_version=current,
                expected_current_version=expected_current_version,
                http_status=409,
                detail=f"Version mismatch: expected {expected_current_version}, got {current}",
            )

        self.versions[target_id] = to_version
        now_iso = datetime.now(timezone.utc).isoformat()
        res = TargetActionResult(
            outcome=TargetActionOutcome.APPLIED,
            service=service,
            action="rollback_service",
            from_version=current,
            to_version=to_version,
            current_version=to_version,
            http_status=200,
            applied_at=now_iso,
        )
        self.applied_actions[action_key] = res
        if self.hooks.drop_response_after_apply:
            raise ConnectionResetError("Simulated connection drop after apply")
        return res

    def apply_restart(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
    ) -> TargetActionResult:
        self.call_count += 1
        service = self._service_for(target_id)
        if (hook_res := self._check_pre_hooks(service, "restart_service")) is not None:
            return hook_res

        if action_key in self.applied_actions:
            prev = self.applied_actions[action_key]
            return TargetActionResult(
                outcome=TargetActionOutcome.ALREADY_APPLIED,
                service=prev.service,
                action=prev.action,
                from_version=prev.from_version,
                to_version=prev.to_version,
                current_version=self.versions.get(target_id),
                http_status=200,
                applied_at=prev.applied_at,
            )

        current = self.versions.get(target_id)
        if current != expected_current_version:
            return TargetActionResult(
                outcome=TargetActionOutcome.PRECONDITION_FAILED,
                service=service,
                action="restart_service",
                current_version=current,
                expected_current_version=expected_current_version,
                http_status=409,
                detail=f"Version mismatch: expected {expected_current_version}, got {current}",
            )

        self.restart_counts[target_id] = self.restart_counts.get(target_id, 0) + 1
        now_iso = datetime.now(timezone.utc).isoformat()
        res = TargetActionResult(
            outcome=TargetActionOutcome.APPLIED,
            service=service,
            action="restart_service",
            from_version=current,
            to_version=current,
            current_version=current,
            http_status=200,
            applied_at=now_iso,
        )
        self.applied_actions[action_key] = res
        if self.hooks.drop_response_after_apply:
            raise ConnectionResetError("Simulated connection drop after apply")
        return res

    def get_action_status(self, target_id: str, action_key: str) -> TargetActionLookup:
        if self.unavailable:
            return TargetActionLookup(status="unknown", detail="Target unavailable")
        if action_key in self.applied_actions:
            prev = self.applied_actions[action_key]
            return TargetActionLookup(
                status="applied",
                service=prev.service,
                action=prev.action,
                from_version=prev.from_version,
                to_version=prev.to_version,
                applied_at=prev.applied_at,
            )
        return TargetActionLookup(status="not_applied")


MAX_TARGET_RESPONSE_BYTES = 1_048_576  # 1 MiB cap for hostile target responses


class LabAdminClient:
    """HTTPX client calling lab admin endpoints over lab-admin.

    Base URLs are derived exclusively from TargetRegistry (never from plans or LLM output).
    No redirects, no internal retries, short timeouts, and tokens are never logged.
    """

    def __init__(
        self,
        registry: TargetRegistry,
        admin_token: SecretStr,
        base_urls: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._registry = registry
        self._admin_token = admin_token
        self._base_urls: dict[str, str] = {
            spec.target_id: (
                base_urls[spec.target_id].rstrip("/")
                if base_urls and spec.target_id in base_urls
                else f"http://{spec.service}:8000"
            )
            for spec in registry.targets
        }
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Lab-Admin-Token": self._admin_token.get_secret_value()}

    def _resolve_base_url(self, target_id: str) -> str | None:
        return self._base_urls.get(target_id.strip().lower())

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        base_url = self._resolve_base_url(target_id)
        if base_url is None:
            return TargetStateError(
                code=TargetStateErrorCode.TARGET_NOT_FOUND,
                target_id=target_id,
                message=f"Target '{target_id}' is not in registry",
            )
        try:
            resp = self._client.get(f"{base_url}/admin/state", headers=self._headers())
            if resp.status_code != 200:
                return TargetStateError(
                    code=TargetStateErrorCode.STATE_UNAVAILABLE,
                    target_id=target_id,
                    message=f"GET /admin/state returned HTTP {resp.status_code}",
                )
            data = resp.json()
            return TargetState(
                target_id=target_id,
                current_version=str(data["version"]),
                source="lab_admin_http",
                read_at=as_of,
            )
        except Exception as exc:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=target_id,
                message=f"Target state request failed: {type(exc).__name__}",
            )

    def get_state(self, target_id: str) -> TargetState | TargetStateError:
        return self.current_state(target_id, datetime.now(timezone.utc))

    def _post_action(
        self,
        target_id: str,
        path: str,
        action_name: str,
        payload: dict[str, Any],
    ) -> TargetActionResult:
        spec = self._registry.get_by_target_id(target_id)
        base_url = self._resolve_base_url(target_id)
        service = spec.service if spec is not None else target_id
        if base_url is None:
            return TargetActionResult(
                outcome=TargetActionOutcome.CLIENT_ERROR,
                service=service,
                action=action_name,
                detail=f"Unknown target '{target_id}'",
            )

        try:
            resp = self._client.post(f"{base_url}{path}", headers=self._headers(), json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return TargetActionResult(
                outcome=TargetActionOutcome.UNCERTAIN,
                service=service,
                action=action_name,
                detail=f"Transport error: {type(exc).__name__}",
            )

        if len(resp.content) > MAX_TARGET_RESPONSE_BYTES:
            return TargetActionResult(
                outcome=TargetActionOutcome.UNCERTAIN,
                service=service,
                action=action_name,
                http_status=resp.status_code,
                detail="Target response exceeded max byte limit",
            )

        try:
            body = resp.json()
        except Exception:
            body = None

        if resp.status_code == 200 and isinstance(body, dict):
            resp_key = body.get("action_key")
            expected_key = payload.get("action_key")
            if resp_key is not None and expected_key is not None and str(resp_key) != str(expected_key):
                return TargetActionResult(
                    outcome=TargetActionOutcome.UNCERTAIN,
                    service=service,
                    action=action_name,
                    http_status=resp.status_code,
                    detail="Target response action_key mismatch",
                )
            st = body.get("status")
            if st == "applied":
                return TargetActionResult(
                    outcome=TargetActionOutcome.APPLIED,
                    service=str(body.get("service", service)),
                    action=str(body.get("action", action_name)),
                    from_version=body.get("from_version"),
                    to_version=body.get("to_version"),
                    current_version=body.get("to_version"),
                    http_status=200,
                    applied_at=body.get("applied_at"),
                )
            if st == "already_applied":
                return TargetActionResult(
                    outcome=TargetActionOutcome.ALREADY_APPLIED,
                    service=str(body.get("service", service)),
                    action=str(body.get("action", action_name)),
                    from_version=body.get("from_version"),
                    to_version=body.get("to_version"),
                    current_version=body.get("to_version"),
                    http_status=200,
                    applied_at=body.get("applied_at"),
                )

        if resp.status_code == 409 and isinstance(body, dict) and body.get("status") == "precondition_failed":
            return TargetActionResult(
                outcome=TargetActionOutcome.PRECONDITION_FAILED,
                service=str(body.get("service", service)),
                action=str(body.get("action", action_name)),
                current_version=body.get("current_version"),
                expected_current_version=body.get("expected_current_version"),
                http_status=409,
                detail="Target version precondition failed",
            )

        if 400 <= resp.status_code < 500:
            return TargetActionResult(
                outcome=TargetActionOutcome.CLIENT_ERROR,
                service=service,
                action=action_name,
                http_status=resp.status_code,
                detail=f"Target rejected request with HTTP {resp.status_code}",
            )

        return TargetActionResult(
            outcome=TargetActionOutcome.UNCERTAIN,
            service=service,
            action=action_name,
            http_status=resp.status_code,
            detail=f"Ambiguous target response HTTP {resp.status_code}",
        )

    def apply_rollback(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
        to_version: str,
    ) -> TargetActionResult:
        return self._post_action(
            target_id,
            "/admin/actions/rollback",
            "rollback_service",
            {
                "action_key": action_key,
                "expected_current_version": expected_current_version,
                "to_version": to_version,
            },
        )

    def apply_restart(
        self,
        target_id: str,
        action_key: str,
        expected_current_version: str,
    ) -> TargetActionResult:
        return self._post_action(
            target_id,
            "/admin/actions/restart",
            "restart_service",
            {
                "action_key": action_key,
                "expected_current_version": expected_current_version,
            },
        )

    def get_action_status(self, target_id: str, action_key: str) -> TargetActionLookup:
        base_url = self._resolve_base_url(target_id)
        if base_url is None:
            return TargetActionLookup(status="unknown", detail=f"Unknown target '{target_id}'")
        try:
            resp = self._client.get(
                f"{base_url}/admin/actions/{action_key}", headers=self._headers()
            )
            if resp.status_code != 200:
                return TargetActionLookup(
                    status="unknown", detail=f"HTTP {resp.status_code}"
                )
            if len(resp.content) > MAX_TARGET_RESPONSE_BYTES:
                return TargetActionLookup(
                    status="unknown", detail="Response exceeded max byte limit"
                )
            body = resp.json()
            resp_key = body.get("action_key") if isinstance(body, dict) else None
            if resp_key is not None and str(resp_key) != action_key:
                return TargetActionLookup(
                    status="unknown", detail="Response action_key mismatch"
                )
            return TargetActionLookup(
                status=str(body.get("status", "unknown")),
                service=body.get("service"),
                action=body.get("action"),
                from_version=body.get("from_version"),
                to_version=body.get("to_version"),
                applied_at=body.get("applied_at"),
            )
        except Exception as exc:
            return TargetActionLookup(status="unknown", detail=type(exc).__name__)
