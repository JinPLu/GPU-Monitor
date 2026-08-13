"""SQLAlchemy persistence schema. All mutable state is owned by the control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


LeaseKind = Literal["workload", "keepalive"]
KeepalivePolicy = Literal["disabled", "idle_keepalive"]


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSetting(Base):
    """Small persisted runtime settings owned by the local control plane."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("host", "port", name="uq_endpoint_host_port"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_endpoint_port"),
        CheckConstraint(
            "keepalive_adapter_id IS NULL OR keepalive_adapter_id = 'server-script-v1'",
            name="ck_endpoint_keepalive_adapter",
        ),
        CheckConstraint(
            "keepalive_policy IN ('disabled', 'idle_keepalive')",
            name="ck_endpoint_keepalive_policy",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ssh_alias: Mapped[str | None] = mapped_column(String(120))
    # Remote project/work directory for humans and Agents.  This is metadata
    # only; it never authorizes ServerPilot to launch a workload there.
    workspace_path: Mapped[str | None] = mapped_column(String(2000))
    observation_profile: Mapped[str] = mapped_column(
        String(40), nullable=False, default="linux-nvidia"
    )
    keepalive_adapter_id: Mapped[str | None] = mapped_column(String(40))
    # This is desired state only. Per-GPU keepalive ownership lives in leases;
    # policy changes never silently start, stop, or reclassify a remote worker.
    keepalive_policy: Mapped[KeepalivePolicy] = mapped_column(
        String(32), nullable=False, default="disabled"
    )
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    storage_group: Mapped[str | None] = mapped_column(String(120))
    expected_gpu_count: Mapped[int | None] = mapped_column(Integer)
    expected_gpu_total_vram_mib: Mapped[int | None] = mapped_column(Integer)
    # Optional project attribution for reporting. Endpoint lifecycle operations
    # are shared loopback inventory actions and are not permission-gated by it.
    owner_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
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


class EndpointTelemetryCurrent(Base):
    """Latest host-wide CPU and memory observation for one endpoint."""

    __tablename__ = "endpoint_telemetry_current"

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    load_1m: Mapped[float] = mapped_column(nullable=False)
    cpu_total_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_idle_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_utilization_pct: Mapped[float | None] = mapped_column()
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_available_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class EndpointTelemetrySnapshot(Base):
    """Bounded history of host-wide CPU and memory observations."""

    __tablename__ = "endpoint_telemetry_snapshots"
    __table_args__ = (
        Index("ix_endpoint_telemetry_endpoint_observed", "endpoint_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    load_1m: Mapped[float] = mapped_column(nullable=False)
    cpu_total_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_idle_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_utilization_pct: Mapped[float | None] = mapped_column()
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_available_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class GPUDevice(Base):
    __tablename__ = "gpu_devices"
    __table_args__ = (UniqueConstraint("endpoint_id", "gpu_uuid", name="uq_endpoint_gpu_uuid"),)

    id: Mapped[str] = mapped_column(String(260), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_uuid: Mapped[str] = mapped_column(String(160), nullable=False)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    cuda_ordinal: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    total_vram_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    compute_capability: Mapped[str | None] = mapped_column(String(40))
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KeepaliveCurrent(Base):
    """Current worker state, separate from endpoint desired policy."""

    __tablename__ = "keepalive_current"
    __table_args__ = (
        CheckConstraint("actual IN ('ON', 'OFF', 'ERROR')", name="ck_keepalive_actual"),
    )

    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), primary_key=True
    )
    actual: Mapped[str] = mapped_column(String(16), nullable=False, default="OFF")
    error_reason: Mapped[str | None] = mapped_column(String(1000))
    expected_pid: Mapped[int | None] = mapped_column(Integer)
    expected_boot_id: Mapped[str | None] = mapped_column(String(120))
    expected_process_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class WorkloadProfile(Base):
    """A project-approved, bounded resource contract for routine work."""

    __tablename__ = "workload_profiles"
    __table_args__ = (Index("ix_workload_profiles_project_enabled", "project_id", "enabled"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="direct-gpu")
    scheduler_target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    scheduler_spec_json: Mapped[str | None] = mapped_column(Text)
    scheduler_script: Mapped[str | None] = mapped_column(Text)
    grant_all_projects: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retain_submission_body: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkloadProfileGrant(Base):
    """An explicit project allowed to consume a profile owned by another project."""

    __tablename__ = "workload_profile_grants"

    profile_id: Mapped[str] = mapped_column(
        ForeignKey("workload_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )


class SchedulerTarget(Base):
    """A globally discoverable external scheduler; never a raw GPU endpoint."""

    __tablename__ = "scheduler_targets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    connection_json: Mapped[str] = mapped_column(Text, nullable=False)
    credential_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    access_hint: Mapped[str] = mapped_column(String(2000), nullable=False)
    access_status: Mapped[str | None] = mapped_column(String(40))
    access_message: Mapped[str | None] = mapped_column(String(2000))
    access_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SchedulerJob(Base):
    """ServerPilot-owned record of one job submitted to an external scheduler."""

    __tablename__ = "scheduler_jobs"
    __table_args__ = (
        UniqueConstraint("actor_id", "submission_key", name="uq_scheduler_job_submission"),
        Index("ix_scheduler_job_target_state", "target_id", "state"),
        Index("ix_scheduler_job_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        ForeignKey("scheduler_targets.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(64), index=True)
    submission_key: Mapped[str] = mapped_column(String(255), nullable=False)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    approval_ref: Mapped[str | None] = mapped_column(String(500))
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_body: Mapped[str | None] = mapped_column(Text)
    retain_submission_body: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheduler_job_id: Mapped[str | None] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_state: Mapped[str | None] = mapped_column(String(80))
    allocated_tres_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    node_list: Mapped[str | None] = mapped_column(String(2000))
    stdout_path: Mapped[str | None] = mapped_column(String(2000))
    stderr_path: Mapped[str | None] = mapped_column(String(2000))
    exit_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SchedulerJobEvent(Base):
    """Append-only scheduler state transition history."""

    __tablename__ = "scheduler_job_events"
    __table_args__ = (Index("ix_scheduler_job_event_job_time", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("scheduler_jobs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_state: Mapped[str | None] = mapped_column(String(80))
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SchedulerTransfer(Base):
    """Audited staged upload to an external scheduler's shared filesystem."""

    __tablename__ = "scheduler_transfers"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "submission_key",
            name="uq_scheduler_transfer_submission",
        ),
        Index(
            "ix_scheduler_transfer_project_created",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        ForeignKey("scheduler_targets.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    submission_key: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    local_source_path: Mapped[str] = mapped_column(String(4000), nullable=False)
    remote_directory: Mapped[str] = mapped_column(String(2000), nullable=False)
    remote_staged_path: Mapped[str | None] = mapped_column(String(2000))
    source_size_bytes: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResourceProvider(Base):
    """Typed resource source used by the generic cross-provider scheduler."""

    __tablename__ = "resource_providers"
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('direct-gpu', 'host-capacity', 'scheduler')",
            name="ck_resource_provider_type",
        ),
        CheckConstraint(
            "("
            "provider_type IN ('direct-gpu', 'host-capacity') "
            "AND endpoint_id IS NOT NULL "
            "AND scheduler_target_id IS NULL"
            ") OR ("
            "provider_type = 'scheduler' "
            "AND endpoint_id IS NULL "
            "AND scheduler_target_id IS NOT NULL"
            ")",
            name="ck_resource_provider_native_ref",
        ),
        UniqueConstraint("provider_type", "endpoint_id", name="uq_resource_provider_endpoint"),
        UniqueConstraint(
            "provider_type",
            "scheduler_target_id",
            name="uq_resource_provider_scheduler_target",
        ),
        Index("ix_resource_provider_type_enabled", "provider_type", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(260), primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    scheduler_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("scheduler_targets.id", ondelete="CASCADE"), index=True
    )
    native_ref_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AllocatableUnit(Base):
    """One schedulable capacity unit without changing historical GPU lease rows."""

    __tablename__ = "allocatable_units"
    __table_args__ = (
        CheckConstraint(
            "unit_type IN ('gpu', 'host', 'scheduler-target')",
            name="ck_allocatable_unit_type",
        ),
        CheckConstraint(
            "total_gpu_count >= 0 "
            "AND (total_cpu_cores IS NULL OR total_cpu_cores >= 0) "
            "AND (total_memory_mib IS NULL OR total_memory_mib >= 0) "
            "AND (total_vram_mib IS NULL OR total_vram_mib >= 0)",
            name="ck_allocatable_unit_quantities_nonnegative",
        ),
        CheckConstraint(
            "("
            "unit_type = 'gpu' "
            "AND endpoint_id IS NOT NULL "
            "AND gpu_id IS NOT NULL "
            "AND scheduler_target_id IS NULL "
            "AND total_gpu_count = 1 "
            "AND total_vram_mib > 0"
            ") OR ("
            "unit_type = 'host' "
            "AND endpoint_id IS NOT NULL "
            "AND gpu_id IS NULL "
            "AND scheduler_target_id IS NULL "
            "AND (total_cpu_cores > 0 OR total_memory_mib > 0)"
            ") OR ("
            "unit_type = 'scheduler-target' "
            "AND endpoint_id IS NULL "
            "AND gpu_id IS NULL "
            "AND scheduler_target_id IS NOT NULL"
            ")",
            name="ck_allocatable_unit_native_ref",
        ),
        UniqueConstraint("provider_id", "unit_key", name="uq_allocatable_unit_provider_key"),
        Index("ix_allocatable_unit_type_state", "unit_type", "state"),
    )

    id: Mapped[str] = mapped_column(String(320), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("resource_providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_key: Mapped[str] = mapped_column(String(260), nullable=False)
    unit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    gpu_id: Mapped[str | None] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), index=True
    )
    scheduler_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("scheduler_targets.id", ondelete="CASCADE"), index=True
    )
    total_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cpu_cores: Mapped[float | None] = mapped_column()
    total_memory_mib: Mapped[int | None] = mapped_column(Integer)
    total_vram_mib: Mapped[int | None] = mapped_column(Integer)
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    native_ref_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceClaim(Base):
    """A generic resource request produced by an agent policy decision."""

    __tablename__ = "resource_claims"
    __table_args__ = (
        CheckConstraint(
            "provider_type IS NULL OR provider_type IN ('direct-gpu', 'host-capacity', 'scheduler')",
            name="ck_resource_claim_provider_type",
        ),
        Index("ix_resource_claim_project_state", "project_id", "state"),
        Index("ix_resource_claim_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    provider_type: Mapped[str | None] = mapped_column(String(32))
    requested_quantities_json: Mapped[str] = mapped_column(Text, nullable=False)
    forecast_json: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceAllocation(Base):
    """Generic allocation row linked to native ServerPilot lease or scheduler job when one exists."""

    __tablename__ = "resource_allocations"
    __table_args__ = (
        Index("ix_resource_allocation_claim_state", "claim_id", "state"),
        Index("ix_resource_allocation_unit_state", "unit_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("resource_claims.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        ForeignKey("allocatable_units.id", ondelete="RESTRICT"), nullable=False
    )
    native_lease_id: Mapped[str | None] = mapped_column(ForeignKey("leases.id", ondelete="SET NULL"))
    native_scheduler_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("scheduler_jobs.id", ondelete="SET NULL")
    )
    quantities_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourcePlanEvaluation(Base):
    """Agent-visible marginal-utility decision record."""

    __tablename__ = "resource_plan_evaluations"
    __table_args__ = (
        Index("ix_resource_plan_eval_project_created", "project_id", "created_at"),
        Index("ix_resource_plan_eval_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_claims.id", ondelete="SET NULL"), index=True
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    baseline_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    marginal_min_saved_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    marginal_min_saved_ratio: Mapped[float] = mapped_column(nullable=False, default=0.10)
    selected_candidate_key: Mapped[str | None] = mapped_column(String(120))
    forecast_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourcePlanCandidate(Base):
    """One candidate considered by a marginal-utility resource decision."""

    __tablename__ = "resource_plan_candidates"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "candidate_key",
            name="uq_resource_plan_candidate_eval_key",
        ),
        Index("ix_resource_plan_candidate_selected", "evaluation_id", "selected"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("resource_plan_evaluations.id", ondelete="CASCADE"), nullable=False
    )
    candidate_key: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str | None] = mapped_column(String(32))
    quantities_json: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_saved_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_saved_ratio: Mapped[float] = mapped_column(nullable=False)
    satisfies_marginal_threshold: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))


class ResourceRunActual(Base):
    """Observed runtime result used to calibrate later resource decisions."""

    __tablename__ = "resource_run_actuals"
    __table_args__ = (
        Index("ix_resource_run_actual_project_created", "project_id", "created_at"),
        Index("ix_resource_run_actual_task_ref", "task_ref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_plan_evaluations.id", ondelete="SET NULL"), index=True
    )
    claim_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_claims.id", ondelete="SET NULL"), index=True
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    quantities_json: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    notes_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    coordination_uri: Mapped[str | None] = mapped_column(String(64))
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


class AllocationRequest(Base):
    __tablename__ = "allocation_requests"
    __table_args__ = (Index("ix_request_queue", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # The migration must support databases whose initial revision is generated
    # from the current metadata, so this historical reference is enforced by
    # the service rather than an ALTER TABLE foreign key.
    profile_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # A routine MCP/UI claim is ready to run as soon as the scheduler assigns
    # resources, including after waiting in the shared queue.
    auto_activate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    __table_args__ = (
        Index("ix_lease_state_expiry", "state", "expires_at"),
        CheckConstraint("kind IN ('workload', 'keepalive')", name="ck_lease_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("allocation_requests.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[LeaseKind] = mapped_column(String(16), nullable=False, default="workload")
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="HELD")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class LeaseEndpointCommitment(Base):
    """Per-endpoint CPU and memory promise held for a direct GPU lease.

    A multi-host GPU lease may consume CPU and RAM at every selected endpoint.
    Persisting one row per endpoint makes that admission accounting explicit
    and keeps historical lease records intact after release.
    """

    __tablename__ = "lease_endpoint_commitments"
    __table_args__ = (
        UniqueConstraint("lease_id", "endpoint_id", name="uq_lease_endpoint_commitment"),
        Index("ix_endpoint_commitment_endpoint", "endpoint_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    cpu_cores: Mapped[float] = mapped_column(nullable=False, default=0.0)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
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
