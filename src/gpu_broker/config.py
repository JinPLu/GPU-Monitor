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
    labels: list[str] = Field(default_factory=list)
    storage_group: str | None = Field(default=None, max_length=120)
    expected_gpu_count: int | None = Field(default=None, ge=1, le=1024)
    expected_gpu_total_vram_mib: int | None = Field(default=None, ge=1)
    project_ids: list[str] = Field(default_factory=list, min_length=1)

    @field_validator("labels", "project_ids")
    @classmethod
    def unique_nonempty_values(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list values must be non-empty")
        if len(values) != len(set(values)):
            raise ValueError("list values must not contain duplicates")
        return values


class InventoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    projects: list[ProjectConfig] = Field(min_length=1)
    endpoints: list[EndpointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_and_project_references(self) -> "InventoryConfig":
        project_ids = [project.id for project in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project ids must be unique")
        endpoint_ids = [endpoint.id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("endpoint ids must be unique")
        endpoint_addresses = [(endpoint.host, endpoint.port) for endpoint in self.endpoints]
        if len(endpoint_addresses) != len(set(endpoint_addresses)):
            raise ValueError("host:port endpoint identities must be unique")
        unknown = {
            project_id
            for endpoint in self.endpoints
            for project_id in endpoint.project_ids
            if project_id not in project_ids
        }
        if unknown:
            raise ValueError(f"endpoint references unknown project ids: {sorted(unknown)}")
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
    """Runtime settings. Secrets are supplied only by environment or CLI, never YAML."""

    database_url: str
    inventory_path: Path
    project_root: Path | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787
    bootstrap_token: str | None = None
    session_secret: str | None = None
    request_body_limit_bytes: int = 256_000
    rate_limit_per_minute: int = 120

    @classmethod
    def from_env(
        cls,
        *,
        database_url: str | None = None,
        inventory_path: Path | None = None,
        bootstrap_token: str | None = None,
    ) -> "Settings":
        default_root = Path.cwd()
        raw_database = database_url or os.environ.get(
            "GPU_BROKER_DATABASE_URL", f"sqlite:///{default_root / 'state' / 'gpu-broker.sqlite3'}"
        )
        raw_inventory = inventory_path or Path(
            os.environ.get("GPU_BROKER_INVENTORY", default_root / "configs" / "inventory.yaml")
        )
        host = os.environ.get("GPU_BROKER_BIND_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(
            "GPU_BROKER_ALLOW_NON_LOOPBACK"
        ):
            raise ConfigurationError(
                "refusing non-loopback bind without GPU_BROKER_ALLOW_NON_LOOPBACK=1 and separate deployment approval"
            )
        try:
            port = int(os.environ.get("GPU_BROKER_BIND_PORT", "8787"))
        except ValueError as exc:
            raise ConfigurationError("GPU_BROKER_BIND_PORT must be an integer") from exc
        return cls(
            database_url=raw_database,
            inventory_path=Path(raw_inventory),
            bind_host=host,
            bind_port=port,
            bootstrap_token=bootstrap_token or os.environ.get("GPU_BROKER_BOOTSTRAP_TOKEN"),
            session_secret=os.environ.get("GPU_BROKER_SESSION_SECRET"),
        )
