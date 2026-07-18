"""SQLAlchemy persistence schema. All mutable state is owned by the control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("host", "port", name="uq_endpoint_host_port"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_endpoint_port"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ssh_alias: Mapped[str | None] = mapped_column(String(120))
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    storage_group: Mapped[str | None] = mapped_column(String(120))
    expected_gpu_count: Mapped[int | None] = mapped_column(Integer)
    expected_gpu_total_vram_mib: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EndpointProject(Base):
    __tablename__ = "endpoint_projects"

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )


class GPUDevice(Base):
    __tablename__ = "gpu_devices"
    __table_args__ = (UniqueConstraint("endpoint_id", "gpu_uuid", name="uq_endpoint_gpu_uuid"),)

    id: Mapped[str] = mapped_column(String(260), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_uuid: Mapped[str] = mapped_column(String(160), nullable=False)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    total_vram_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    compute_capability: Mapped[str | None] = mapped_column(String(40))
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelemetrySnapshot(Base):
    __tablename__ = "telemetry_snapshots"
    __table_args__ = (Index("ix_telemetry_gpu_observed", "gpu_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_used_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_free_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    memory_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    temperature_c: Mapped[int | None] = mapped_column(Integer)
    power_watts: Mapped[float | None] = mapped_column()
    pstate: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class TelemetryCurrent(Base):
    """Latest observation for one GPU; bounded to exactly one row per device."""

    __tablename__ = "telemetry_current"

    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_used_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_free_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    memory_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    temperature_c: Mapped[int | None] = mapped_column(Integer)
    power_watts: Mapped[float | None] = mapped_column()
    pstate: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class ProcessObservation(Base):
    __tablename__ = "process_observations"
    __table_args__ = (
        UniqueConstraint(
            "gpu_id", "pid", "boot_id", "process_started_at", name="uq_current_process_identity"
        ),
        Index("ix_process_gpu_current", "gpu_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    boot_id: Mapped[str] = mapped_column(String(120), nullable=False)
    process_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    username: Mapped[str | None] = mapped_column(String(120))
    executable: Mapped[str] = mapped_column(String(255), nullable=False)
    used_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quota_gpus: Mapped[int | None] = mapped_column(Integer)
    concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActorProject(Base):
    __tablename__ = "actor_projects"

    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_api_token_hash"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AllocationRequest(Base):
    __tablename__ = "allocation_requests"
    __table_args__ = (Index("ix_request_queue", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    start_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_ref: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="QUEUED")
    priority_class: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    blocked_reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (Index("ix_lease_state_expiry", "state", "expires_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("allocation_requests.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="HELD")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(500))
    issued_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class LeaseResource(Base):
    __tablename__ = "lease_resources"
    __table_args__ = (
        UniqueConstraint("lease_id", "gpu_id", name="uq_lease_gpu"),
        Index(
            "uq_active_lease_resource_gpu",
            "gpu_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkloadBinding(Base):
    __tablename__ = "workload_bindings"
    __table_args__ = (UniqueConstraint("lease_id", "run_id", name="uq_lease_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    process_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (Index("ix_reservation_window", "state", "start_at", "end_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    gpu_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"
    __table_args__ = (Index("ix_maintenance_window", "start_at", "end_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    gpu_id: Mapped[str | None] = mapped_column(ForeignKey("gpu_devices.id", ondelete="CASCADE"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_actor_time", "actor_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(260), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_active", "active", "severity", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(260), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("actor_id", "action", "key", name="uq_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderState(Base):
    __tablename__ = "provider_states"
    __table_args__ = (UniqueConstraint("provider", "endpoint_id", name="uq_provider_endpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
