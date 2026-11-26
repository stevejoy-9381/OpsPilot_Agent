"""Canonical enum definitions for OpsPilot database models and API schemas.

These enums form the single source of truth for database column constraints,
Alembic migration definitions, and Pydantic validation models across the project.
"""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    """RBAC roles for tenant operators and reviewers."""

    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class IncidentSource(str, Enum):
    """Triggering source of an incident."""

    ALERT = "alert"
    SEEDED_CASE = "seeded_case"


class Severity(str, Enum):
    """Incident and alert severity levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


IncidentSeverity = Severity


class IncidentStatusSummary(str, Enum):
    """High-level incident lifecycle summary status."""

    OPEN = "open"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class RunStatus(str, Enum):
    """Deterministic investigation run status state machine from outcome contract."""

    QUEUED = "queued"
    INVESTIGATING = "investigating"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class ToolName(str, Enum):
    """Closed vocabulary of bounded investigation tools."""

    GET_METRICS = "get_metrics"
    SEARCH_LOGS = "search_logs"
    GET_TRACE = "get_trace"
    LIST_DEPLOYS = "list_deploys"
    GET_DEPLOY_DIFF = "get_deploy_diff"


class HypothesisCategory(str, Enum):
    """Hypothesis and diagnosis root cause categories."""

    BAD_DEPLOY = "bad_deploy"
    DEPENDENCY_FAILURE = "dependency_failure"
    FALSE_ALARM = "false_alarm"
    UNKNOWN = "unknown"


class HypothesisMechanism(str, Enum):
    """Hypothesis and diagnosis failure mechanisms."""

    CONNECTION_POOL_EXHAUSTION = "connection_pool_exhaustion"
    CACHE_BYPASS_LATENCY = "cache_bypass_latency"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    NONE = "none"
    UNKNOWN = "unknown"


class HypothesisStatus(str, Enum):
    """Intermediate hypothesis lifecycle validation state."""

    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"


class ActionPlanAction(str, Enum):
    """Canonical recovery action vocabulary matching shared.actions.ActionType."""

    ROLLBACK_SERVICE = "rollback_service"
    RESTART_SERVICE = "restart_service"
    NO_ACTION = "no_action"
    ESCALATE = "escalate"


class ActionPlanStatus(str, Enum):
    """Action plan proposal and review lifecycle states."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    EXECUTED = "executed"
    FAILED = "failed"


class ApprovalDecision(str, Enum):
    """Reviewer decision on an action plan."""

    APPROVE = "approve"
    REJECT = "reject"


class ExecutionStatus(str, Enum):
    """Broker action execution lifecycle states."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    UNCERTAIN = "uncertain"
    RECONCILED = "reconciled"
    FAILED = "failed"
    REFUSED = "refused"
    SUCCEEDED = "succeeded"


class VerificationVerdict(str, Enum):
    """Outcome verification probe verdicts."""

    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    INCONCLUSIVE = "inconclusive"


class RunEventActor(str, Enum):
    """Allowed actor categories in the audit trail."""

    SYSTEM = "system"
    AGENT = "agent"
    BROKER = "broker"
