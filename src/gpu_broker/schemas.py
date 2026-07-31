"""External contracts. Unknown fields are rejected so admission is never guessed."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DEFAULT_LEASE_WINDOW_SECONDS = 8 * 60 * 60


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResourceConstraints(StrictModel):
    gpu_count: int = Field(ge=1, le=1024)
    # A direct-GPU claim reserves these amounts at *each endpoint selected for
    # the gang*.  Per-endpoint semantics prevent a multi-host request from
    # silently dividing a host requirement across machines.
    cpu_cores: float | None = Field(default=None, gt=0, le=4096)
    memory_mib: int | None = Field(default=None, gt=0, le=16 * 1024 * 1024)
    min_available_cpu_cores: float | None = Field(default=None, ge=0)
    min_available_memory_mib: int | None = Field(default=None, ge=0)
    min_total_vram_mib: int | None = Field(default=None, ge=1)
    min_free_vram_mib: int | None = Field(default=None, ge=0)
    nodes: int = Field(default=1, ge=1, le=1024)
    gpus_per_node: int | None = Field(default=None, ge=1, le=1024)
    same_host: bool = False
    placement: Literal["pack", "spread", "exact"] = "pack"
    endpoint_labels: list[str] = Field(default_factory=list)
    gpu_labels: list[str] = Field(default_factory=list)
    endpoint_ids: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    deny_endpoint_ids: list[str] = Field(default_factory=list)
    deny_gpu_ids: list[str] = Field(default_factory=list)
    allow_conservative_backfill: bool = False

    @field_validator(
        "endpoint_labels",
        "gpu_labels",
        "endpoint_ids",
        "gpu_ids",
        "deny_endpoint_ids",
        "deny_gpu_ids",
    )
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("constraint lists must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("constraint list values must not be empty")
        return values

    @model_validator(mode="after")
    def validate_topology(self) -> "ResourceConstraints":
        if self.nodes > 1 and self.gpus_per_node is None:
            raise ValueError("nodes > 1 requires explicit gpus_per_node")
        if self.gpus_per_node is not None and self.gpus_per_node * self.nodes != self.gpu_count:
            raise ValueError("gpu_count must equal nodes * gpus_per_node when gpus_per_node is set")
        if self.same_host and self.nodes != 1:
            raise ValueError("same_host requires nodes=1")
        if self.placement == "exact" and not self.gpu_ids:
            raise ValueError("exact placement requires stable gpu_ids")
        if self.gpu_ids and len(self.gpu_ids) != self.gpu_count:
            raise ValueError("gpu_ids must contain exactly gpu_count values")
        overlap = set(self.gpu_ids).intersection(self.deny_gpu_ids)
        if overlap:
            raise ValueError(f"gpu ids appear in both allow and deny constraints: {sorted(overlap)}")
        return self


class RequestCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_seconds: int = Field(default=DEFAULT_LEASE_WINDOW_SECONDS, ge=60, le=60 * 60 * 24 * 30)
    start_after: datetime | None = None
    deadline: datetime | None = None
    approval_ref: str | None = Field(default=None, max_length=500)
    constraints: ResourceConstraints

    @model_validator(mode="after")
    def validate_times(self) -> "RequestCreate":
        if self.start_after and self.start_after.tzinfo is None:
            raise ValueError("start_after must include a timezone")
        if self.deadline and self.deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        if self.start_after and self.deadline and self.deadline <= self.start_after:
            raise ValueError("deadline must be after start_after")
        return self


class RequestCreateFlat(StrictModel):
    """CLI-friendly request form that is converted to the canonical nested schema."""

    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    gpu_count: int = Field(ge=1)
    duration_seconds: int = Field(default=DEFAULT_LEASE_WINDOW_SECONDS, ge=60)
    start_after: datetime | None = None
    deadline: datetime | None = None
    approval_ref: str | None = Field(default=None, max_length=500)
    min_available_cpu_cores: float | None = Field(default=None, ge=0)
    min_available_memory_mib: int | None = Field(default=None, ge=0)
    min_total_vram_mib: int | None = Field(default=None, ge=1)
    min_free_vram_mib: int | None = Field(default=None, ge=0)
    nodes: int = Field(default=1, ge=1)
    gpus_per_node: int | None = Field(default=None, ge=1)
    same_host: bool = False
    placement: Literal["pack", "spread", "exact"] = "pack"
    endpoint_labels: list[str] = Field(default_factory=list)
    gpu_labels: list[str] = Field(default_factory=list)
    endpoint_ids: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    deny_endpoint_ids: list[str] = Field(default_factory=list)
    deny_gpu_ids: list[str] = Field(default_factory=list)
    allow_conservative_backfill: bool = False

    def canonical(self) -> RequestCreate:
        data = self.model_dump()
        constraint_fields = set(ResourceConstraints.model_fields)
        constraints = {key: data.pop(key) for key in list(data) if key in constraint_fields}
        return RequestCreate.model_validate({**data, "constraints": constraints})


class LeaseBind(StrictModel):
    run_id: str = Field(min_length=1, max_length=255)
    process_keys: list[str] = Field(default_factory=list)

    @field_validator("process_keys")
    @classmethod
    def process_key_count(cls, value: list[str]) -> list[str]:
        if len(value) > 1024:
            raise ValueError("too many process keys")
        return value


class LeaseObservedBind(StrictModel):
    """Bind every fresh observed process on a lease to one already-started run."""

    run_id: str | None = Field(default=None, min_length=1, max_length=255)


class ReservationCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    gpu_ids: list[str] = Field(default_factory=list)
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=1, max_length=1000)
    constraints: ResourceConstraints | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "ReservationCreate":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("reservation times must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("reservation end_at must be after start_at")
        if not self.gpu_ids and self.constraints is None:
            raise ValueError("reservation requires gpu_ids or constraints")
        return self


class MaintenanceCreate(StrictModel):
    endpoint_id: str | None = None
    gpu_id: str | None = None
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_target_and_window(self) -> "MaintenanceCreate":
        if (self.endpoint_id is None) == (self.gpu_id is None):
            raise ValueError("maintenance must target exactly one endpoint_id or gpu_id")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("maintenance times must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("maintenance end_at must be after start_at")
        return self


class WorkloadProfileUpsert(StrictModel):
    """Admin-defined resource contract used by routine project workloads."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    project_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_seconds: int = Field(ge=60, le=60 * 60 * 24 * 30)
    constraints: ResourceConstraints
    runtime_kind: Literal["direct-gpu", "slurm"] = "direct-gpu"
    scheduler_target_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9-]{1,63}$"
    )
    scheduler: "SlurmJobSpec | None" = None
    scheduler_script: str | None = Field(default=None, min_length=1, max_length=128_000)
    grant_project_ids: list[str] = Field(default_factory=list, max_length=256)
    grant_all_projects: bool = False
    retain_submission_body: bool = False
    enabled: bool = True

    @field_validator("grant_project_ids")
    @classmethod
    def unique_grant_projects(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("grant_project_ids must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("grant_project_ids must contain non-empty values")
        return values

    @model_validator(mode="after")
    def validate_placement(self) -> "WorkloadProfileUpsert":
        if self.constraints.gpu_ids or self.constraints.placement == "exact":
            raise ValueError("workload profile cannot pin exact gpu_ids")
        if self.runtime_kind == "direct-gpu":
            if self.scheduler_target_id or self.scheduler or self.scheduler_script:
                raise ValueError(
                    "direct-gpu workload profile cannot define scheduler fields"
                )
        elif not self.scheduler_target_id or self.scheduler is None or not self.scheduler_script:
            raise ValueError(
                "slurm workload profile requires scheduler_target_id, scheduler and scheduler_script"
            )
        return self


class WorkloadProfileClaim(StrictModel):
    task_ref: str = Field(min_length=1, max_length=255)


class SlurmJobSpec(StrictModel):
    """Bounded Slurm flags controlled by the Broker, not raw command-line input."""

    partition: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    qos: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    gpu_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    cpu_cores: int = Field(default=1, ge=1, le=4096)
    memory_mib: int = Field(default=1024, ge=1, le=16 * 1024 * 1024)
    nodes: int = Field(default=1, ge=1, le=1024)
    tasks_per_node: int = Field(default=1, ge=1, le=4096)
    working_directory: str = Field(min_length=1, max_length=2000)
    stdout_pattern: str = Field(default="gpu-broker-%j.out", min_length=1, max_length=2000)
    stderr_pattern: str = Field(default="gpu-broker-%j.err", min_length=1, max_length=2000)

    @field_validator("working_directory", "stdout_pattern", "stderr_pattern")
    @classmethod
    def safe_remote_path(cls, value: str) -> str:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("remote paths must be single-line values without NUL bytes")
        return value


class SchedulerUploadConfig(StrictModel):
    """Non-secret SSH mux metadata used only after the access helper authenticates."""

    ssh_host: str = Field(pattern=r"^[A-Za-z0-9.-]{1,253}$")
    ssh_user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    control_path: str = Field(min_length=1, max_length=1000)

    @field_validator("control_path")
    @classmethod
    def absolute_control_path(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value or "\n" in value:
            raise ValueError("control_path must be an absolute single-line path")
        return value


class SchedulerTargetUpsert(StrictModel):
    """Admin-owned external scheduler connection metadata; contains no secret values."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    adapter: Literal["slurm-command"] = "slurm-command"
    command_prefix: list[str] = Field(min_length=1, max_length=16)
    upload: SchedulerUploadConfig | None = None
    credential_refs: dict[str, str] = Field(default_factory=dict)
    capabilities: list[
        Literal["access-status", "submit", "status", "cancel", "data-transfer"]
    ] = Field(default_factory=lambda: ["access-status", "submit", "status", "cancel"])
    access_hint: str = Field(min_length=1, max_length=2000)
    enabled: bool = True

    @field_validator("command_prefix", "capabilities")
    @classmethod
    def unique_non_empty_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("target lists must not contain duplicates")
        if any(not value or "\x00" in value for value in values):
            raise ValueError("target lists must contain non-empty values without NUL bytes")
        return values

    @field_validator("credential_refs")
    @classmethod
    def credential_references_only(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or not value for key, value in values.items()):
            raise ValueError("credential references must use non-empty keys and values")
        forbidden = {"password", "secret", "token", "otp", "totp"}
        if any(key.lower() in forbidden for key in values):
            raise ValueError("store credential references, never credential values")
        return values

    @model_validator(mode="after")
    def upload_matches_capability(self) -> "SchedulerTargetUpsert":
        has_capability = "data-transfer" in self.capabilities
        if has_capability != (self.upload is not None):
            raise ValueError(
                "data-transfer capability requires upload metadata and vice versa"
            )
        return self


class SchedulerProfileSubmit(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)


class SchedulerOneOffSubmit(StrictModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_seconds: int = Field(ge=60, le=60 * 60 * 24 * 30)
    constraints: ResourceConstraints
    scheduler: SlurmJobSpec
    script_body: str = Field(min_length=1, max_length=128_000)
    retain_submission_body: bool = False

    @model_validator(mode="after")
    def validate_scheduler_constraints(self) -> "SchedulerOneOffSubmit":
        if self.constraints.gpu_ids or self.constraints.endpoint_ids:
            raise ValueError(
                "external scheduler submissions cannot pin Broker endpoint_ids or gpu_ids"
            )
        return self


class SchedulerJobCancel(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


class SchedulerUploadRequest(StrictModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    project_id: str = Field(min_length=1, max_length=64)
    local_path: str = Field(min_length=1, max_length=4000)
    remote_directory: str = Field(
        pattern=r"^/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$",
        max_length=2000,
    )
    approval_ref: str = Field(min_length=1, max_length=500)

    @field_validator("local_path")
    @classmethod
    def local_path_is_absolute(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value or "\n" in value:
            raise ValueError("local_path must be an absolute single-line path")
        return value


class EndpointUpsert(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,127}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    ssh_user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_alias: str | None = Field(default=None, min_length=1, max_length=120)
    labels: list[str] = Field(default_factory=list)
    storage_group: str | None = Field(default=None, max_length=120)
    expected_gpu_count: int | None = Field(default=None, ge=1, le=1024)
    expected_gpu_total_vram_mib: int | None = Field(default=None, ge=1)
    owner_project_id: str | None = Field(default=None, min_length=1, max_length=64)
    lifecycle_state: Literal["active", "draining", "retired"] | None = None
    # Kept only for one-project legacy imports.  Endpoint ownership is now one
    # project, rather than a scheduler placement allowlist.
    project_ids: list[str] = Field(default_factory=list)
    enabled: bool | None = None

    @field_validator("labels", "project_ids")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("endpoint lists must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("endpoint list values must not be empty")
        return values

    @model_validator(mode="after")
    def resolve_owner(self) -> "EndpointUpsert":
        if self.owner_project_id and self.project_ids and self.project_ids != [self.owner_project_id]:
            raise ValueError("project_ids may only repeat owner_project_id for legacy imports")
        if self.owner_project_id is None and len(self.project_ids) == 1:
            self.owner_project_id = self.project_ids[0]
        return self


class EndpointEnabled(StrictModel):
    enabled: bool


class SSHCommandRequest(BaseModel):
    """Raw GUI SSH input; command whitespace is preserved for token binding."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=512)
    project_ids: list[str] | None = Field(default=None, min_length=1)
    csrf: str = Field(min_length=1, max_length=256)

    @field_validator("project_ids")
    @classmethod
    def unique_projects(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (len(values) != len(set(values)) or any(not value for value in values)):
            raise ValueError("project_ids must contain unique non-empty values")
        return values


class SSHCommandCommit(SSHCommandRequest):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    endpoint_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,127}$")


class SSHCommandsRequest(StrictModel):
    """Line-oriented SSH commands pasted from the GUI; each line is parsed independently."""

    commands: list[str] = Field(min_length=1, max_length=100)
    project_ids: list[str] | None = Field(default=None, min_length=1)
    csrf: str = Field(min_length=1, max_length=256)

    @field_validator("commands")
    @classmethod
    def command_lengths(cls, values: list[str]) -> list[str]:
        if any(not command or len(command) > 512 for command in values):
            raise ValueError("each SSH command must be between 1 and 512 characters")
        return values

    @field_validator("project_ids")
    @classmethod
    def unique_projects(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (len(values) != len(set(values)) or any(not value for value in values)):
            raise ValueError("project_ids must contain unique non-empty values")
        return values


class SSHCommandsCommit(SSHCommandsRequest):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class ActorCreate(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{1,127}$")
    display_name: str = Field(min_length=1, max_length=160)
    role: Literal["viewer", "allocator", "operator", "admin", "collector"]
    project_ids: list[str] = Field(default_factory=list)
    token_label: str = Field(default="generated", min_length=1, max_length=120)


class TelemetryInput(StrictModel):
    gpu_uuid: str = Field(min_length=1, max_length=160)
    gpu_index: int = Field(ge=0, le=1024)
    name: str = Field(min_length=1, max_length=255)
    total_vram_mib: int = Field(ge=1)
    memory_used_mib: int = Field(ge=0)
    memory_free_mib: int = Field(ge=0)
    gpu_utilization_pct: int | None = Field(default=None, ge=0, le=100)
    memory_utilization_pct: int | None = Field(default=None, ge=0, le=100)
    temperature_c: int | None = Field(default=None, ge=-100, le=300)
    power_watts: float | None = Field(default=None, ge=0)
    pstate: str | None = Field(default=None, max_length=32)
    health: str = Field(default="OK", min_length=1, max_length=32)


class ProcessInput(StrictModel):
    gpu_uuid: str = Field(min_length=1, max_length=160)
    pid: int = Field(ge=1, le=2**31 - 1)
    used_memory_mib: int = Field(ge=0)
    executable: str = Field(min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=120)
    process_started_at: datetime

    @field_validator("process_started_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("process_started_at must include a timezone")
        return value


class HostTelemetryInput(StrictModel):
    """Host-wide telemetry captured with the GPU sample for one endpoint."""

    cpu_count: int = Field(ge=1, le=1_048_576)
    load_1m: float = Field(ge=0)
    memory_total_mib: int = Field(ge=1)
    memory_available_mib: int = Field(ge=0)

    @model_validator(mode="after")
    def available_memory_is_bounded(self) -> "HostTelemetryInput":
        if self.memory_available_mib > self.memory_total_mib:
            raise ValueError("memory_available_mib must not exceed memory_total_mib")
        return self


class EndpointObservation(StrictModel):
    endpoint_id: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    boot_id: str = Field(min_length=1, max_length=120)
    host: HostTelemetryInput
    gpus: list[TelemetryInput]
    processes: list[ProcessInput] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def observed_timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class AlertAcknowledge(StrictModel):
    note: str | None = Field(default=None, max_length=1000)


class RetentionPrune(StrictModel):
    older_than_seconds: int = Field(ge=60, le=60 * 60 * 24 * 3650)
