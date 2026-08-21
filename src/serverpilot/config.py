"""Strict, secret-free configuration for the global inventory and local service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigurationError(ValueError):
    """Raised when a config is incomplete or has unknown/invalid values."""


KeepaliveAdapterId = Literal["server-script-v1"]
KeepalivePolicy = Literal["disabled", "idle_keepalive"]
RESERVED_SYSTEM_ID = "serverpilot-system"


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=10, ge=1, le=3600)
    stale_after_seconds: int = Field(default=30, ge=2, le=86400)
    ssh_connect_timeout_seconds: int = Field(default=8, ge=1, le=120)

    @model_validator(mode="after")
    def stale_after_interval(self) -> "CollectorConfig":
        if self.stale_after_seconds < self.interval_seconds:
            raise ValueError("stale_after_seconds must be >= interval_seconds")
        return self


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    weight: int = Field(default=1, ge=1, le=1000)
    quota_gpus: int | None = Field(default=None, ge=1)
    concurrency_limit: int | None = Field(default=None, ge=1)


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,127}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    ssh_user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_alias: str | None = Field(default=None, min_length=1, max_length=120)
    # Nullable only while projecting a migrated legacy database row. Inventory
    # validation below requires every configured endpoint to name the path.
    workspace_path: str | None = Field(default=None, min_length=1, max_length=2000)
    # A closed, code-owned profile chooses the fixed read-only probe and
    # parser.  It is deliberately not a command, shell fragment, key path, or
    # SSH option supplied by inventory.
    observation_profile: Literal["linux-nvidia", "linux-host", "server-script-v1"] = "linux-nvidia"
    # Optional sealed lifecycle adapter. None means keepalive is completely off.
    keepalive_adapter_id: KeepaliveAdapterId | None = None
    # Desired policy only; actual ownership remains a per-GPU lease and starts
    # disabled for both new and migrated endpoints.
    keepalive_policy: KeepalivePolicy = "disabled"
    labels: list[str] = Field(default_factory=list)
    storage_group: str | None = Field(default=None, max_length=120)
    expected_gpu_count: int | None = Field(default=None, ge=1, le=1024)
    expected_gpu_total_vram_mib: int | None = Field(default=None, ge=1)
    # Kept as a tolerated legacy inventory field.  Endpoint access is global;
    # a claim's project_id is not pre-registered or scoped to a server.
    project_ids: list[str] = Field(default_factory=list)

    @field_validator("labels", "project_ids")
    @classmethod
    def unique_nonempty_values(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list values must be non-empty")
        if len(values) != len(set(values)):
            raise ValueError("list values must not contain duplicates")
        return values

    @field_validator("workspace_path")
    @classmethod
    def valid_workspace_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("workspace_path must be an absolute single-line path")
        return value

    @model_validator(mode="after")
    def idle_keepalive_requires_adapter(self) -> "EndpointConfig":
        if self.keepalive_policy == "idle_keepalive" and self.keepalive_adapter_id is None:
            raise ValueError("idle_keepalive requires a sealed keepalive adapter")
        return self


class InventoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    # Accepted for existing inventory files only. The service no longer reads
    # this value; HELD leases stay until explicit expiry, release, or reconcile.
    held_lease_startup_grace_seconds: int = Field(default=120, ge=1, le=3600)
    # Two-phase reclaim of workload leases that hold GPUs without running
    # anything.  Both windows count only continuously observed idle time: a
    # stale observation resets the clock, so a collector outage can never be
    # mistaken for an idle workload.
    idle_lease_alert_seconds: int = Field(default=600, ge=60, le=86400)
    idle_lease_reclaim_seconds: int = Field(default=3600, ge=120, le=604800)
    # Project policies are optional.  The broker creates a neutral record when
    # a claim first uses an otherwise unknown project_id.
    projects: list[ProjectConfig] = Field(default_factory=list)
    endpoints: list[EndpointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def reclaim_follows_alert(self) -> "InventoryConfig":
        if self.idle_lease_reclaim_seconds < self.idle_lease_alert_seconds:
            raise ValueError(
                "idle_lease_reclaim_seconds must be >= idle_lease_alert_seconds"
            )
        return self

    @model_validator(mode="after")
    def validate_identity_and_project_references(self) -> "InventoryConfig":
        project_ids = [project.id for project in self.projects]
        if RESERVED_SYSTEM_ID in project_ids:
            raise ValueError("the ServerPilot internal project id is reserved")
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project ids must be unique")
        endpoint_ids = [endpoint.id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("endpoint ids must be unique")
        endpoint_addresses = [(endpoint.host, endpoint.port) for endpoint in self.endpoints]
        if len(endpoint_addresses) != len(set(endpoint_addresses)):
            raise ValueError("host:port endpoint identities must be unique")
        if any(RESERVED_SYSTEM_ID in endpoint.project_ids for endpoint in self.endpoints):
            raise ValueError("the ServerPilot internal project id is reserved")
        if any(endpoint.workspace_path is None for endpoint in self.endpoints):
            raise ValueError("every configured endpoint requires workspace_path")
        return self


def load_inventory(path: Path) -> InventoryConfig:
    """Load YAML with strict schema validation and no implicit defaults for required facts."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read inventory {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"inventory {path} must be a mapping")
    try:
        return InventoryConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid inventory {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the loopback control plane."""

    database_url: str
    inventory_path: Path
    project_root: Path | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787
    daemon_instance_id: str | None = None
    session_secret: str | None = None
    request_body_limit_bytes: int = 256_000
    rate_limit_per_minute: int = 120

    @classmethod
    def from_env(
        cls,
        *,
        database_url: str | None = None,
        inventory_path: Path | None = None,
    ) -> "Settings":
        default_root = Path.cwd()
        raw_database = database_url or os.environ.get(
            "SERVERPILOT_DATABASE_URL", f"sqlite:///{default_root / 'state' / 'serverpilot.sqlite3'}"
        )
        raw_inventory = inventory_path or Path(
            os.environ.get("SERVERPILOT_INVENTORY", default_root / "configs" / "inventory.yaml")
        )
        host = os.environ.get("SERVERPILOT_BIND_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(
            "SERVERPILOT_ALLOW_NON_LOOPBACK"
        ):
            raise ConfigurationError(
                "refusing non-loopback bind without SERVERPILOT_ALLOW_NON_LOOPBACK=1 and separate deployment approval"
            )
        try:
            port = int(os.environ.get("SERVERPILOT_BIND_PORT", "8787"))
        except ValueError as exc:
            raise ConfigurationError("SERVERPILOT_BIND_PORT must be an integer") from exc
        return cls(
            database_url=raw_database,
            inventory_path=Path(raw_inventory),
            bind_host=host,
            bind_port=port,
            session_secret=os.environ.get("SERVERPILOT_SESSION_SECRET"),
        )
