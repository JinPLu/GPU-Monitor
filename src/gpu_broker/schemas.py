"""External contracts. Unknown fields are rejected so admission is never guessed."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResourceConstraints(StrictModel):
    gpu_count: int = Field(ge=1, le=1024)
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
    project_id: str = Field(min_length=2, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_seconds: int = Field(default=3600, ge=60, le=60 * 60 * 24 * 30)
    expected_duration_seconds: int | None = Field(default=None, ge=60, le=60 * 60 * 24 * 30)
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
        if self.expected_duration_seconds and self.expected_duration_seconds > self.duration_seconds:
            raise ValueError("expected_duration_seconds must not exceed duration_seconds")
        return self


class RequestCreateFlat(StrictModel):
    """CLI-friendly request form that is converted to the canonical nested schema."""

    project_id: str = Field(min_length=2, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    gpu_count: int = Field(ge=1)
    duration_seconds: int = Field(default=3600, ge=60)
    expected_duration_seconds: int | None = Field(default=None, ge=60)
    start_after: datetime | None = None
    deadline: datetime | None = None
    approval_ref: str | None = Field(default=None, max_length=500)
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


class ReservationCreate(StrictModel):
    project_id: str = Field(min_length=2, max_length=64)
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


class ProjectUpsert(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    weight: int = Field(default=1, ge=1, le=1000)
    quota_gpus: int | None = Field(default=None, ge=1)
    concurrency_limit: int | None = Field(default=None, ge=1)
    enabled: bool = True


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
    project_ids: list[str] = Field(min_length=1)
    enabled: bool = True

    @field_validator("labels", "project_ids")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("endpoint lists must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("endpoint list values must not be empty")
        return values


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


class EndpointObservation(StrictModel):
    endpoint_id: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    boot_id: str = Field(min_length=1, max_length=120)
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
