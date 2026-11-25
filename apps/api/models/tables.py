"""SQLAlchemy 2.x ORM models for OpsPilot core domain tables.

Mapped to schema 'app'. All models inherit from Base.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class with PostgreSQL app schema defaults."""


class TenantModel(Base):
    __tablename__ = "tenants"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    sessions: Mapped[list[SessionModel]] = relationship(
        "SessionModel", back_populates="user", cascade="all, delete-orphan"
    )


class SessionModel(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "app"}

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    user: Mapped[UserModel] = relationship("UserModel", back_populates="sessions")


class IncidentModel(Base):
    __tablename__ = "incidents"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    alert_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="alert")
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_summary: Mapped[str] = mapped_column(String(64), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    runs: Mapped[list[RunModel]] = relationship(
        "RunModel", back_populates="incident", cascade="all, delete-orphan"
    )


class RunModel(Base):
    __tablename__ = "runs"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.incidents.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    remaining_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    max_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    incident: Mapped[IncidentModel] = relationship("IncidentModel", back_populates="runs")
    events: Mapped[list[RunEventModel]] = relationship(
        "RunEventModel", back_populates="run", cascade="all, delete-orphan"
    )
    action_plans: Mapped[list[ActionPlanModel]] = relationship(
        "ActionPlanModel", back_populates="run", cascade="all, delete-orphan"
    )
    observations: Mapped[list[ObservationModel]] = relationship(
        "ObservationModel", back_populates="run", cascade="all, delete-orphan"
    )
    hypotheses: Mapped[list[HypothesisModel]] = relationship(
        "HypothesisModel", back_populates="run", cascade="all, delete-orphan"
    )
    diagnosis: Mapped[DiagnosisModel | None] = relationship(
        "DiagnosisModel", back_populates="run", uselist=False, cascade="all, delete-orphan"
    )


class RunEventModel(Base):
    __tablename__ = "run_events"
    __table_args__ = {"schema": "app"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    run: Mapped[RunModel] = relationship("RunModel", back_populates="events")


class ObservationModel(Base):
    __tablename__ = "observations"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    run: Mapped[RunModel] = relationship("RunModel", back_populates="observations")


class ActionPlanModel(Base):
    __tablename__ = "action_plans"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rationale_evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    rollback_route: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    notes: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    run: Mapped[RunModel] = relationship("RunModel", back_populates="action_plans")
    approvals: Mapped[list[ApprovalModel]] = relationship(
        "ApprovalModel", back_populates="plan", cascade="all, delete-orphan"
    )
    executions: Mapped[list[ActionExecutionModel]] = relationship(
        "ActionExecutionModel", back_populates="plan", cascade="all, delete-orphan"
    )


class ApprovalModel(Base):
    __tablename__ = "approvals"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.action_plans.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.users.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    plan: Mapped[ActionPlanModel] = relationship("ActionPlanModel", back_populates="approvals")


class ActionExecutionModel(Base):
    __tablename__ = "action_executions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.action_plans.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    action_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_state_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    target_state_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    broker_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    plan: Mapped[ActionPlanModel] = relationship("ActionPlanModel", back_populates="executions")


class AllowedTransitionModel(Base):
    __tablename__ = "allowed_transitions"
    __table_args__ = {"schema": "app"}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)


class HypothesisModel(Base):
    __tablename__ = "hypotheses"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    mechanism: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    contrary_evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    run: Mapped[RunModel] = relationship("RunModel", back_populates="hypotheses")


class DiagnosisModel(Base):
    __tablename__ = "diagnoses"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), nullable=False
    )
    root_cause_service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    root_cause_category: Mapped[str] = mapped_column(String(64), nullable=False)
    mechanism: Mapped[str] = mapped_column(String(64), nullable=False)
    culprit_deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    run: Mapped[RunModel] = relationship("RunModel", back_populates="diagnosis")

