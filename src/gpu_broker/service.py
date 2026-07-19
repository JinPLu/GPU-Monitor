"""Single-owner domain service for inventory, telemetry, leases and audit events."""

from __future__ import annotations

import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from gpu_broker import SCHEMA_VERSION
from gpu_broker.config import EndpointConfig, InventoryConfig
from gpu_broker.database import Database
from gpu_broker.models import (
    Actor,
    ActorProject,
    Alert,
    AllocationRequest,
    ApiToken,
    AuditEvent,
    Endpoint,
    EndpointTelemetryCurrent,
    GPUDevice,
    IdempotencyRecord,
    Lease,
    LeaseResource,
    MaintenanceWindow,
    ProcessObservation,
    Project,
    ProviderState,
    Reservation,
    Revision,
    TelemetryCurrent,
    TelemetrySnapshot,
    WorkloadBinding,
    WorkloadProfile,
)
from gpu_broker.schemas import (
    ActorCreate,
    AlertAcknowledge,
    EndpointObservation,
    EndpointEnabled,
    EndpointUpsert,
    LeaseBind,
    LeaseObservedBind,
    MaintenanceCreate,
    RetentionPrune,
    RequestCreate,
    ReservationCreate,
    ResourceConstraints,
    WorkloadProfileClaim,
    WorkloadProfileUpsert,
)
from gpu_broker.timeutil import ensure_utc, json_dump, json_load, stable_hash, token_hash, utcnow


ACTIVE_LEASE_STATES = {"HELD", "ACTIVE", "ORPHANED_BUSY", "CONFLICT"}
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
TELEMETRY_HISTORY_INTERVAL_SECONDS = 60
TELEMETRY_HISTORY_RETENTION_SECONDS = 24 * 60 * 60
# The collector derives a process start time from `ps etimes`, which has
# one-second precision and is sampled after the endpoint observation begins.
# Preserve the already-observed identity across this bounded measurement
# jitter; otherwise a healthy long-running process can look new on every
# collection and lose its workload attribution.
PROCESS_START_TIME_JITTER_SECONDS = 2
MUTATING_ROLES = {"allocator", "operator", "admin"}
OPERATOR_ROLES = {"operator", "admin"}
ADMIN_ROLES = {"admin"}

T = TypeVar("T")


class BrokerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ActorContext:
    id: str
    role: str
    project_ids: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


class BrokerService:
    """The only component allowed to mutate the broker database.

    The service deliberately contains scheduling decisions, derived availability
    and audit writes together so REST, CLI, MCP and GUI cannot drift.
    """

    def __init__(self, database: Database, inventory: InventoryConfig) -> None:
        self.database = database
        self.inventory = inventory

    # ---- initialization, identity and transaction primitives -----------------

    def initialize(self, bootstrap_token: str | None = None, *, sync_inventory: bool = False) -> bool:
        """Initialize persistent state and report whether a bootstrap token was newly created."""
        self.database.migrate()

        def operation(session: Session) -> bool:
            now = utcnow()
            revision = session.get(Revision, 1)
            if revision is None:
                session.add(Revision(id=1, value=0, updated_at=now))
            has_inventory = (session.scalar(select(func.count()).select_from(Endpoint)) or 0) > 0
            if sync_inventory or not has_inventory:
                self._upsert_inventory(session, now)
            bootstrap_created = False
            if bootstrap_token:
                bootstrap_created = self._ensure_bootstrap_admin(session, bootstrap_token, now)
            self._bump_revision(session, now)
            return bootstrap_created

        return self._write(operation)

    def _upsert_inventory(self, session: Session, now: datetime) -> None:
        for configured_project in self.inventory.projects:
            project = session.get(Project, configured_project.id)
            if project is None:
                session.add(
                    Project(
                        id=configured_project.id,
                        display_name=configured_project.display_name,
                        weight=configured_project.weight,
                        quota_gpus=configured_project.quota_gpus,
                        concurrency_limit=configured_project.concurrency_limit,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                project.display_name = configured_project.display_name
                project.weight = configured_project.weight
                project.quota_gpus = configured_project.quota_gpus
                project.concurrency_limit = configured_project.concurrency_limit
                project.updated_at = now

        session.flush()
        for configured_endpoint in self.inventory.endpoints:
            endpoint = session.get(Endpoint, configured_endpoint.id)
            if endpoint is None:
                endpoint = Endpoint(
                    id=configured_endpoint.id,
                    host=configured_endpoint.host,
                    port=configured_endpoint.port,
                    ssh_user=configured_endpoint.ssh_user,
                    ssh_alias=configured_endpoint.ssh_alias,
                    labels_json=json_dump(configured_endpoint.labels),
                    storage_group=configured_endpoint.storage_group,
                    expected_gpu_count=configured_endpoint.expected_gpu_count,
                    expected_gpu_total_vram_mib=configured_endpoint.expected_gpu_total_vram_mib,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
            else:
                if (endpoint.host, endpoint.port) != (
                    configured_endpoint.host,
                    configured_endpoint.port,
                ):
                    raise BrokerError(
                        "endpoint_identity_immutable",
                        f"endpoint {endpoint.id} cannot change host:port; create a new immutable endpoint id",
                        status_code=409,
                    )
                endpoint.ssh_user = configured_endpoint.ssh_user
                endpoint.ssh_alias = configured_endpoint.ssh_alias
                endpoint.labels_json = json_dump(configured_endpoint.labels)
                endpoint.storage_group = configured_endpoint.storage_group
                endpoint.expected_gpu_count = configured_endpoint.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = configured_endpoint.expected_gpu_total_vram_mib
                endpoint.updated_at = now
            session.flush()

    def _ensure_bootstrap_admin(self, session: Session, raw_token: str, now: datetime) -> bool:
        if len(raw_token) < 24:
            raise BrokerError(
                "weak_bootstrap_token",
                "bootstrap token must contain at least 24 characters",
                status_code=422,
            )
        actor_id = "bootstrap-admin"
        actor = session.get(Actor, actor_id)
        if actor is None:
            actor = Actor(
                id=actor_id,
                display_name="Bootstrap administrator",
                role="admin",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(actor)
        existing = session.scalar(
            select(ApiToken).where(ApiToken.actor_id == actor_id, ApiToken.label == "bootstrap")
        )
        if existing is None:
            session.add(
                ApiToken(
                    id=secrets.token_hex(16),
                    actor_id=actor_id,
                    label="bootstrap",
                    token_hash=token_hash(raw_token),
                    created_at=now,
                    expires_at=None,
                    revoked_at=None,
                    last_used_at=None,
                )
            )
            return True
        return False

    def collector_endpoints(self) -> list[EndpointConfig]:
        """Read current control-plane endpoint inventory for fixed-command collection."""

        def operation(session: Session) -> list[EndpointConfig]:
            values: list[EndpointConfig] = []
            endpoints = session.scalars(
                select(Endpoint).where(Endpoint.enabled.is_(True)).order_by(Endpoint.id)
            ).all()
            for endpoint in endpoints:
                values.append(
                    EndpointConfig(
                        id=endpoint.id,
                        host=endpoint.host,
                        port=endpoint.port,
                        ssh_user=endpoint.ssh_user,
                        ssh_alias=endpoint.ssh_alias,
                        labels=json_load(endpoint.labels_json),
                        storage_group=endpoint.storage_group,
                        expected_gpu_count=endpoint.expected_gpu_count,
                        expected_gpu_total_vram_mib=endpoint.expected_gpu_total_vram_mib,
                        project_ids=[],
                    )
                )
            return values

        return self._read(operation)

    def _write(self, operation: Callable[[Session], T]) -> T:
        """Serialize SQLite writes with bounded retry; DB unique indexes remain authoritative."""

        last_error: Exception | None = None
        for attempt in range(12):
            with self.database.session() as session:
                try:
                    session.execute(text("BEGIN IMMEDIATE"))
                    result = operation(session)
                    session.commit()
                    return result
                except OperationalError as exc:
                    session.rollback()
                    last_error = exc
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                except Exception:
                    session.rollback()
                    raise
            time.sleep(min(0.01 * (2**attempt), 0.25))
        raise BrokerError(
            "database_busy",
            "database stayed busy after bounded retry; no allocation was made",
            status_code=503,
            details={"reason": type(last_error).__name__ if last_error else "unknown"},
        )

    def _read(self, operation: Callable[[Session], T]) -> T:
        with self.database.session() as session:
            return operation(session)

    @staticmethod
    def _bump_revision(session: Session, now: datetime) -> int:
        revision = session.get(Revision, 1)
        if revision is None:
            revision = Revision(id=1, value=0, updated_at=now)
            session.add(revision)
            session.flush()
        revision.value += 1
        revision.updated_at = now
        return revision.value

    @staticmethod
    def _revision(session: Session) -> int:
        revision = session.get(Revision, 1)
        return revision.value if revision is not None else 0

    def authenticate(self, raw_token: str) -> ActorContext:
        if not raw_token:
            raise BrokerError("authentication_required", "a bearer token is required", status_code=401)

        def operation(session: Session) -> ActorContext:
            now = utcnow()
            token = session.scalar(
                select(ApiToken).where(
                    ApiToken.token_hash == token_hash(raw_token),
                    ApiToken.revoked_at.is_(None),
                    or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now),
                )
            )
            if token is None:
                raise BrokerError("invalid_token", "token is invalid or revoked", status_code=401)
            actor = session.get(Actor, token.actor_id)
            if actor is None or not actor.enabled:
                raise BrokerError("actor_disabled", "actor is disabled", status_code=403)
            token.last_used_at = now
            project_ids = frozenset(
                session.scalars(select(ActorProject.project_id).where(ActorProject.actor_id == actor.id)).all()
            )
            session.commit()
            return ActorContext(id=actor.id, role=actor.role, project_ids=project_ids)

        return self._read(operation)

    def context_for_actor(self, actor_id: str) -> ActorContext:
        """Resolve a server-side UI session without retaining an API token in the cookie."""

        def operation(session: Session) -> ActorContext:
            actor = session.get(Actor, actor_id)
            if actor is None or not actor.enabled:
                raise BrokerError("actor_disabled", "actor is disabled", status_code=403)
            project_ids = frozenset(
                session.scalars(select(ActorProject.project_id).where(ActorProject.actor_id == actor.id)).all()
            )
            return ActorContext(id=actor.id, role=actor.role, project_ids=project_ids)

        return self._read(operation)

    def local_actor(self, actor_id: str) -> ActorContext:
        """Resolve a human/Agent label for the keyless loopback application.

        The label records who owns a claim; it is not an authentication
        credential. Local actors can coordinate every configured project.
        """

        normalized = actor_id.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", normalized):
            raise BrokerError(
                "invalid_actor_name",
                "actor name must start with a letter and contain 2-128 letters, numbers, '.', '_' or '-'",
                status_code=422,
            )

        def resolve(session: Session) -> ActorContext | None:
            if session.get(Actor, normalized) is None:
                return None
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="admin", project_ids=project_ids)

        existing = self._read(resolve)
        if existing is not None:
            return existing

        def create(session: Session) -> ActorContext:
            if session.get(Actor, normalized) is None:
                now = utcnow()
                session.add(
                    Actor(
                        id=normalized,
                        display_name=normalized,
                        role="admin",
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="admin", project_ids=project_ids)

        return self._write(create)

    @staticmethod
    def _require_role(actor: ActorContext, allowed: set[str]) -> None:
        if actor.role not in allowed:
            raise BrokerError(
                "forbidden_role",
                f"role {actor.role} is not allowed for this operation",
                status_code=403,
            )

    @staticmethod
    def _can_manage_lease(actor: ActorContext, lease: Lease) -> bool:
        return actor.is_admin or actor.role == "operator" or lease.actor_id == actor.id

    # ---- audit, idempotency and serialisation ---------------------------------

    def _audit(
        self,
        session: Session,
        *,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        result: str,
        before: Any = None,
        after: Any = None,
        summary: dict[str, Any] | None = None,
        now: datetime,
    ) -> AuditEvent:
        event = AuditEvent(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            before_hash=stable_hash(before) if before is not None else None,
            after_hash=stable_hash(after) if after is not None else None,
            summary_json=json_dump(summary or {}),
            created_at=now,
        )
        session.add(event)
        session.flush()
        return event

    def _idempotent(
        self,
        session: Session,
        *,
        actor: ActorContext,
        action: str,
        key: str,
    ) -> dict[str, Any] | None:
        if not key or len(key) > 255:
            raise BrokerError(
                "idempotency_key_required",
                "a non-empty Idempotency-Key of at most 255 characters is required",
                status_code=422,
            )
        prior = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.action == action,
                IdempotencyRecord.key == key,
            )
        )
        return json_load(prior.response_json) if prior is not None else None

    def _remember_idempotency(
        self,
        session: Session,
        *,
        actor: ActorContext,
        action: str,
        key: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            IdempotencyRecord(
                actor_id=actor.id,
                action=action,
                key=key,
                response_json=json_dump(response),
                created_at=now,
            )
        )

    @staticmethod
    def _endpoint_dict(endpoint: Endpoint) -> dict[str, Any]:
        return {
            "id": endpoint.id,
            "host": endpoint.host,
            "port": endpoint.port,
            "ssh_user": endpoint.ssh_user,
            "ssh_alias": endpoint.ssh_alias,
            "labels": json_load(endpoint.labels_json),
            "storage_group": endpoint.storage_group,
            "expected_gpu_count": endpoint.expected_gpu_count,
            "expected_gpu_total_vram_mib": endpoint.expected_gpu_total_vram_mib,
            "enabled": endpoint.enabled,
            "created_at": _iso(endpoint.created_at),
            "updated_at": _iso(endpoint.updated_at),
        }

    @staticmethod
    def _project_dict(project: Project) -> dict[str, Any]:
        return {
            "id": project.id,
            "display_name": project.display_name,
            "weight": project.weight,
            "quota_gpus": project.quota_gpus,
            "concurrency_limit": project.concurrency_limit,
            "enabled": project.enabled,
        }

    @staticmethod
    def _workload_profile_dict(profile: WorkloadProfile) -> dict[str, Any]:
        return {
            "id": profile.id,
            "project_id": profile.project_id,
            "display_name": profile.display_name,
            "purpose": profile.purpose,
            "duration_seconds": profile.duration_seconds,
            "constraints": json_load(profile.constraints_json),
            "enabled": profile.enabled,
            "created_at": _iso(profile.created_at),
            "updated_at": _iso(profile.updated_at),
        }

    @staticmethod
    def _actor_dict(actor: Actor, project_ids: Iterable[str]) -> dict[str, Any]:
        return {
            "id": actor.id,
            "display_name": actor.display_name,
            "role": actor.role,
            "enabled": actor.enabled,
            "project_ids": sorted(project_ids),
            "created_at": _iso(actor.created_at),
            "updated_at": _iso(actor.updated_at),
        }

    @staticmethod
    def _request_dict(request: AllocationRequest) -> dict[str, Any]:
        return {
            "id": request.id,
            "actor_id": request.actor_id,
            "project_id": request.project_id,
            "profile_id": request.profile_id,
            "task_ref": request.task_ref,
            "purpose": request.purpose,
            "constraints": json_load(request.constraints_json),
            "duration_seconds": request.duration_seconds,
            "start_after": _iso(request.start_after),
            "deadline": _iso(request.deadline),
            "approval_ref": request.approval_ref,
            "state": request.state,
            "priority_class": request.priority_class,
            "blocked_reason": request.blocked_reason,
            "created_at": _iso(request.created_at),
            "updated_at": _iso(request.updated_at),
        }

    def _lease_dict(
        self,
        session: Session,
        lease: Lease,
        *,
        resources: list[LeaseResource] | None = None,
        bindings: list[WorkloadBinding] | None = None,
        request: AllocationRequest | None = None,
    ) -> dict[str, Any]:
        if resources is None:
            resources = session.scalars(
                select(LeaseResource)
                .where(LeaseResource.lease_id == lease.id)
                .order_by(LeaseResource.gpu_id)
            ).all()
        if bindings is None:
            bindings = session.scalars(
                select(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id)
            ).all()
        if request is None:
            request = session.get(AllocationRequest, lease.request_id)
        return {
            "id": lease.id,
            "request_id": lease.request_id,
            "actor_id": lease.actor_id,
            "project_id": lease.project_id,
            "state": lease.state,
            "gpu_ids": [resource.gpu_id for resource in resources if resource.active],
            "issued_at": _iso(lease.issued_at),
            "expires_at": _iso(lease.expires_at),
            "last_heartbeat_at": _iso(lease.last_heartbeat_at),
            "activated_at": _iso(lease.activated_at),
            "released_at": _iso(lease.released_at),
            "release_reason": lease.release_reason,
            "issued_revision": lease.issued_revision,
            "task_ref": request.task_ref if request else None,
            "purpose": request.purpose if request else None,
            "workloads": [
                {"run_id": binding.run_id, "process_keys": json_load(binding.process_keys_json)}
                for binding in bindings
            ],
        }

    @staticmethod
    def _alert_dict(alert: Alert) -> dict[str, Any]:
        return {
            "id": alert.id,
            "type": alert.alert_type,
            "severity": alert.severity,
            "resource_type": alert.resource_type,
            "resource_id": alert.resource_id,
            "message": alert.message,
            "active": alert.active,
            "first_seen_at": _iso(alert.first_seen_at),
            "last_seen_at": _iso(alert.last_seen_at),
            "acknowledged_at": _iso(alert.acknowledged_at),
            "acknowledged_by": alert.acknowledged_by,
        }

    @staticmethod
    def _reservation_dict(reservation: Reservation) -> dict[str, Any]:
        return {
            "id": reservation.id,
            "actor_id": reservation.actor_id,
            "project_id": reservation.project_id,
            "gpu_ids": json_load(reservation.gpu_ids_json),
            "constraints": json_load(reservation.constraints_json),
            "start_at": _iso(reservation.start_at),
            "end_at": _iso(reservation.end_at),
            "reason": reservation.reason,
            "state": reservation.state,
            "created_at": _iso(reservation.created_at),
        }

    @staticmethod
    def _maintenance_dict(window: MaintenanceWindow) -> dict[str, Any]:
        return {
            "id": window.id,
            "endpoint_id": window.endpoint_id,
            "gpu_id": window.gpu_id,
            "actor_id": window.actor_id,
            "start_at": _iso(window.start_at),
            "end_at": _iso(window.end_at),
            "reason": window.reason,
            "state": window.state,
            "created_at": _iso(window.created_at),
        }

    # ---- read models and derived GPU state ------------------------------------

    def _latest_telemetry(
        self, session: Session, gpu_id: str
    ) -> TelemetryCurrent | TelemetrySnapshot | None:
        current = session.get(TelemetryCurrent, gpu_id)
        if current is not None:
            return current
        return session.scalar(
            select(TelemetrySnapshot)
            .where(TelemetrySnapshot.gpu_id == gpu_id)
            .order_by(TelemetrySnapshot.observed_at.desc(), TelemetrySnapshot.id.desc())
            .limit(1)
        )

    @staticmethod
    def _telemetry_dict(telemetry: TelemetryCurrent | TelemetrySnapshot | None) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "observed_at": _iso(telemetry.observed_at),
            "collected_at": _iso(telemetry.collected_at),
            "memory_used_mib": telemetry.memory_used_mib,
            "memory_free_mib": telemetry.memory_free_mib,
            "gpu_utilization_pct": telemetry.gpu_utilization_pct,
            "memory_utilization_pct": telemetry.memory_utilization_pct,
            "temperature_c": telemetry.temperature_c,
            "power_watts": telemetry.power_watts,
            "pstate": telemetry.pstate,
            "health": telemetry.health,
            "provider": telemetry.provider,
        }

    @staticmethod
    def _host_telemetry_dict(telemetry: EndpointTelemetryCurrent | None) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "observed_at": _iso(telemetry.observed_at),
            "collected_at": _iso(telemetry.collected_at),
            "cpu_count": telemetry.cpu_count,
            "load_1m": telemetry.load_1m,
            "memory_total_mib": telemetry.memory_total_mib,
            "memory_available_mib": telemetry.memory_available_mib,
            "provider": telemetry.provider,
        }

    def _current_processes(self, session: Session, gpu_id: str, now: datetime) -> list[ProcessObservation]:
        cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
        return session.scalars(
            select(ProcessObservation)
            .where(
                ProcessObservation.gpu_id == gpu_id,
                ProcessObservation.active.is_(True),
                ProcessObservation.last_seen_at >= cutoff,
            )
            .order_by(ProcessObservation.pid)
        ).all()

    def _active_lease_for_gpu(self, session: Session, gpu_id: str) -> Lease | None:
        return session.scalar(
            select(Lease)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .where(LeaseResource.gpu_id == gpu_id, LeaseResource.active.is_(True))
            .order_by(Lease.issued_at.desc())
            .limit(1)
        )

    def _maintenance_for_gpu(
        self, session: Session, gpu: GPUDevice, now: datetime
    ) -> MaintenanceWindow | None:
        return session.scalar(
            select(MaintenanceWindow)
            .where(
                MaintenanceWindow.state == "ACTIVE",
                MaintenanceWindow.start_at <= now,
                MaintenanceWindow.end_at > now,
                or_(MaintenanceWindow.gpu_id == gpu.id, MaintenanceWindow.endpoint_id == gpu.endpoint_id),
            )
            .order_by(MaintenanceWindow.start_at.desc())
            .limit(1)
        )

    def _current_reservation_for_gpu(
        self, session: Session, gpu_id: str, now: datetime
    ) -> Reservation | None:
        reservations = session.scalars(
            select(Reservation).where(
                Reservation.state == "ACTIVE",
                Reservation.start_at <= now,
                Reservation.end_at > now,
            )
        ).all()
        return next((item for item in reservations if gpu_id in json_load(item.gpu_ids_json)), None)

    def _gpu_state(self, session: Session, gpu: GPUDevice, now: datetime) -> tuple[str, str | None]:
        endpoint = session.get(Endpoint, gpu.endpoint_id)
        if endpoint is None or not endpoint.enabled or not gpu.enabled:
            return "DISABLED", "endpoint or GPU is disabled"
        maintenance = self._maintenance_for_gpu(session, gpu, now)
        if maintenance is not None:
            return "MAINTENANCE", maintenance.reason
        telemetry = self._latest_telemetry(session, gpu.id)
        if telemetry is None:
            return "UNKNOWN_RECOVERING", "no fresh telemetry after service start"
        age = (now - (_as_utc(telemetry.observed_at) or now)).total_seconds()
        if age > self.inventory.collector.stale_after_seconds:
            return "UNKNOWN_STALE", f"telemetry age {age:.1f}s exceeds stale threshold"
        if telemetry.health.upper() not in {"OK", "HEALTHY"} or gpu.health.upper() not in {
            "OK",
            "HEALTHY",
        }:
            return "UNHEALTHY", telemetry.health
        lease = self._active_lease_for_gpu(session, gpu.id)
        if lease is not None and lease.state == "CONFLICT":
            return "CONFLICT", "lease/process attribution conflict"
        if lease is not None and lease.state == "ORPHANED_BUSY":
            return "ORPHANED_BUSY", "lease expired while a compute process remains"
        processes = self._current_processes(session, gpu.id, now)
        if processes:
            if lease is not None and self._processes_match_binding(session, lease.id, processes):
                return "RUNNING_MANAGED", "bound workload process observed"
            return "BUSY_UNMANAGED", "compute process observed; admission blocked"
        if lease is not None:
            return ("HELD" if lease.state == "HELD" else "LEASED_IDLE"), "exclusive lease active"
        reservation = self._current_reservation_for_gpu(session, gpu.id, now)
        if reservation is not None:
            return "RESERVED", f"reservation {reservation.id} is active"
        return "AVAILABLE", None

    @staticmethod
    def _process_key(process: ProcessObservation) -> str:
        started = _as_utc(process.process_started_at)
        assert started is not None
        return f"{process.gpu_id}:{process.pid}:{process.boot_id}:{int(started.timestamp())}"

    @classmethod
    def _process_dict(cls, process: ProcessObservation) -> dict[str, Any]:
        return {
            "pid": process.pid,
            "boot_id": process.boot_id,
            "process_started_at": _iso(process.process_started_at),
            "process_key": cls._process_key(process),
            "username": process.username,
            "executable": process.executable,
            "used_memory_mib": process.used_memory_mib,
            "observations": process.observations,
            "first_seen_at": _iso(process.first_seen_at),
            "last_seen_at": _iso(process.last_seen_at),
        }

    def _processes_match_binding(
        self, session: Session, lease_id: str, processes: Iterable[ProcessObservation]
    ) -> bool:
        known = self._binding_process_keys(session, lease_id)
        return bool(known) and all(self._process_key(process) in known for process in processes)

    @staticmethod
    def _binding_process_keys(session: Session, lease_id: str) -> set[str]:
        return {
            process_key
            for binding in session.scalars(
                select(WorkloadBinding).where(WorkloadBinding.lease_id == lease_id)
            ).all()
            for process_key in json_load(binding.process_keys_json)
        }

    def _gpu_dict(self, session: Session, gpu: GPUDevice, now: datetime) -> dict[str, Any]:
        telemetry = self._latest_telemetry(session, gpu.id)
        processes = self._current_processes(session, gpu.id, now)
        lease = self._active_lease_for_gpu(session, gpu.id)
        state, reason = self._gpu_state(session, gpu, now)
        return {
            "id": gpu.id,
            "endpoint_id": gpu.endpoint_id,
            "gpu_uuid": gpu.gpu_uuid,
            "gpu_index": gpu.gpu_index,
            "name": gpu.name,
            "total_vram_mib": gpu.total_vram_mib,
            "labels": json_load(gpu.labels_json),
            "health": gpu.health,
            "enabled": gpu.enabled,
            "state": state,
            "state_reason": reason,
            "first_seen_at": _iso(gpu.first_seen_at),
            "last_seen_at": _iso(gpu.last_seen_at),
            "telemetry": self._telemetry_dict(telemetry),
            "processes": [self._process_dict(process) for process in processes],
            "lease": self._lease_dict(session, lease) if lease else None,
        }

    def envelope(self, session: Session, data: Any) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_revision": self._revision(session),
            "server_time": _iso(utcnow()),
            "data": data,
        }

    def snapshot(
        self,
        actor: ActorContext,
        *,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            endpoints = session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
            provider_states = {
                provider_state.endpoint_id: provider_state
                for provider_state in session.scalars(
                    select(ProviderState).where(ProviderState.provider == "raw-ssh")
                ).all()
                if provider_state.endpoint_id is not None
            }
            visible_endpoints = [
                endpoint
                for endpoint in endpoints
                if endpoint_id is None or endpoint.id == endpoint_id
            ]
            visible_ids = {endpoint.id for endpoint in visible_endpoints}
            host_telemetry_by_endpoint = (
                {
                    item.endpoint_id: item
                    for item in session.scalars(
                        select(EndpointTelemetryCurrent).where(
                            EndpointTelemetryCurrent.endpoint_id.in_(visible_ids)
                        )
                    ).all()
                }
                if visible_ids
                else {}
            )
            gpus = (
                session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id.in_(visible_ids))
                    .order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
                ).all()
                if visible_ids
                else []
            )
            gpu_ids = {gpu.id for gpu in gpus}
            gpu_counts: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                gpu_counts[gpu.endpoint_id] += 1

            telemetry_by_gpu: dict[str, TelemetryCurrent | TelemetrySnapshot] = {}
            if gpu_ids:
                telemetry_by_gpu.update(
                    {
                        item.gpu_id: item
                        for item in session.scalars(
                            select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(gpu_ids))
                        ).all()
                    }
                )
                missing = gpu_ids.difference(telemetry_by_gpu)
                if missing:
                    latest_ids = (
                        select(func.max(TelemetrySnapshot.id))
                        .where(TelemetrySnapshot.gpu_id.in_(missing))
                        .group_by(TelemetrySnapshot.gpu_id)
                    )
                    telemetry_by_gpu.update(
                        {
                            item.gpu_id: item
                            for item in session.scalars(
                                select(TelemetrySnapshot).where(TelemetrySnapshot.id.in_(latest_ids))
                            ).all()
                        }
                    )

            process_cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            processes_by_gpu: dict[str, list[ProcessObservation]] = defaultdict(list)
            if gpu_ids:
                for process in session.scalars(
                    select(ProcessObservation)
                    .where(
                        ProcessObservation.gpu_id.in_(gpu_ids),
                        ProcessObservation.active.is_(True),
                        ProcessObservation.last_seen_at >= process_cutoff,
                    )
                    .order_by(ProcessObservation.gpu_id, ProcessObservation.pid)
                ).all():
                    processes_by_gpu[process.gpu_id].append(process)

            all_leases = session.scalars(
                select(Lease)
                .where(Lease.state.in_(ACTIVE_LEASE_STATES))
                .order_by(Lease.issued_at.desc())
            ).all()
            visible_leases = all_leases
            lease_by_id = {lease.id: lease for lease in all_leases}
            lease_ids = set(lease_by_id)
            resources_by_lease: dict[str, list[LeaseResource]] = defaultdict(list)
            lease_by_gpu: dict[str, Lease] = {}
            bindings_by_lease: dict[str, list[WorkloadBinding]] = defaultdict(list)
            if lease_ids:
                for resource in session.scalars(
                    select(LeaseResource)
                    .where(LeaseResource.lease_id.in_(lease_ids))
                    .order_by(LeaseResource.gpu_id)
                ).all():
                    resources_by_lease[resource.lease_id].append(resource)
                    if resource.active:
                        lease_by_gpu[resource.gpu_id] = lease_by_id[resource.lease_id]
                for binding in session.scalars(
                    select(WorkloadBinding).where(WorkloadBinding.lease_id.in_(lease_ids))
                ).all():
                    bindings_by_lease[binding.lease_id].append(binding)

            active_request_ids = {lease.request_id for lease in all_leases}
            requests_by_id = (
                {
                    request.id: request
                    for request in session.scalars(
                        select(AllocationRequest).where(AllocationRequest.id.in_(active_request_ids))
                    ).all()
                }
                if active_request_ids
                else {}
            )
            lease_payloads = {
                lease.id: self._lease_dict(
                    session,
                    lease,
                    resources=resources_by_lease[lease.id],
                    bindings=bindings_by_lease[lease.id],
                    request=requests_by_id.get(lease.request_id),
                )
                for lease in all_leases
            }
            binding_keys = {
                lease_id: {
                    process_key
                    for binding in bindings
                    for process_key in json_load(binding.process_keys_json)
                }
                for lease_id, bindings in bindings_by_lease.items()
            }

            maintenance_by_gpu: dict[str, MaintenanceWindow] = {}
            maintenance_by_endpoint: dict[str, MaintenanceWindow] = {}
            for window in session.scalars(
                select(MaintenanceWindow)
                .where(
                    MaintenanceWindow.state == "ACTIVE",
                    MaintenanceWindow.start_at <= now,
                    MaintenanceWindow.end_at > now,
                )
                .order_by(MaintenanceWindow.start_at.desc())
            ).all():
                if window.gpu_id and window.gpu_id not in maintenance_by_gpu:
                    maintenance_by_gpu[window.gpu_id] = window
                if window.endpoint_id and window.endpoint_id not in maintenance_by_endpoint:
                    maintenance_by_endpoint[window.endpoint_id] = window

            all_reservations = session.scalars(
                select(Reservation)
                .where(Reservation.state == "ACTIVE", Reservation.end_at > now)
                .order_by(Reservation.start_at)
            ).all()
            visible_reservations = all_reservations
            current_reservation_by_gpu: dict[str, Reservation] = {}
            for reservation in all_reservations:
                if (_as_utc(reservation.start_at) or now) <= now:
                    for reserved_gpu_id in json_load(reservation.gpu_ids_json):
                        current_reservation_by_gpu.setdefault(reserved_gpu_id, reservation)

            queued_requests = session.scalars(
                select(AllocationRequest)
                .where(AllocationRequest.state == "QUEUED")
                .order_by(AllocationRequest.created_at)
            ).all()
            endpoint_payloads: list[dict[str, Any]] = []

            def endpoint_snapshot(endpoint: Endpoint) -> dict[str, Any]:
                provider_state = provider_states.get(endpoint.id)
                last_success = _as_utc(provider_state.last_success_at) if provider_state else None
                if not endpoint.enabled:
                    monitor_status = "DISABLED"
                elif provider_state is None:
                    monitor_status = "PENDING"
                elif last_success is None or provider_state.last_error:
                    monitor_status = "ERROR"
                elif now - last_success > timedelta(seconds=self.inventory.collector.stale_after_seconds):
                    monitor_status = "STALE"
                else:
                    monitor_status = "ONLINE"
                return {
                    **self._endpoint_dict(endpoint),
                    "host_telemetry": self._host_telemetry_dict(
                        host_telemetry_by_endpoint.get(endpoint.id)
                    ),
                    "monitor": {
                        "status": monitor_status,
                        "gpu_count": gpu_counts[endpoint.id],
                        "last_success_at": _iso(provider_state.last_success_at) if provider_state else None,
                        "last_attempt_at": _iso(provider_state.last_attempt_at) if provider_state else None,
                        "last_error": provider_state.last_error if provider_state else None,
                    },
                }

            endpoint_payloads = [endpoint_snapshot(endpoint) for endpoint in visible_endpoints]

            def derive_state(gpu: GPUDevice) -> tuple[str, str | None]:
                endpoint = next(item for item in visible_endpoints if item.id == gpu.endpoint_id)
                if not endpoint.enabled or not gpu.enabled:
                    return "DISABLED", "endpoint or GPU is disabled"
                maintenance = maintenance_by_gpu.get(gpu.id) or maintenance_by_endpoint.get(
                    gpu.endpoint_id
                )
                if maintenance is not None:
                    return "MAINTENANCE", maintenance.reason
                telemetry = telemetry_by_gpu.get(gpu.id)
                if telemetry is None:
                    return "UNKNOWN_RECOVERING", "no fresh telemetry after service start"
                age = (now - (_as_utc(telemetry.observed_at) or now)).total_seconds()
                if age > self.inventory.collector.stale_after_seconds:
                    return "UNKNOWN_STALE", f"telemetry age {age:.1f}s exceeds stale threshold"
                if telemetry.health.upper() not in {"OK", "HEALTHY"} or gpu.health.upper() not in {
                    "OK",
                    "HEALTHY",
                }:
                    return "UNHEALTHY", telemetry.health
                lease = lease_by_gpu.get(gpu.id)
                if lease is not None and lease.state == "CONFLICT":
                    return "CONFLICT", "lease/process attribution conflict"
                if lease is not None and lease.state == "ORPHANED_BUSY":
                    return "ORPHANED_BUSY", "lease expired while a compute process remains"
                processes = processes_by_gpu[gpu.id]
                if processes:
                    known = binding_keys.get(lease.id, set()) if lease else set()
                    if lease is not None and known and all(
                        self._process_key(process) in known for process in processes
                    ):
                        return "RUNNING_MANAGED", "bound workload process observed"
                    return "BUSY_UNMANAGED", "compute process observed; admission blocked"
                if lease is not None:
                    return (
                        "HELD" if lease.state == "HELD" else "LEASED_IDLE"
                    ), "exclusive lease active"
                reservation = current_reservation_by_gpu.get(gpu.id)
                if reservation is not None:
                    return "RESERVED", f"reservation {reservation.id} is active"
                return "AVAILABLE", None

            gpu_payloads: list[dict[str, Any]] = []
            for gpu in gpus:
                telemetry = telemetry_by_gpu.get(gpu.id)
                processes = processes_by_gpu[gpu.id]
                lease = lease_by_gpu.get(gpu.id)
                gpu_state, reason = derive_state(gpu)
                payload = {
                    "id": gpu.id,
                    "endpoint_id": gpu.endpoint_id,
                    "gpu_uuid": gpu.gpu_uuid,
                    "gpu_index": gpu.gpu_index,
                    "name": gpu.name,
                    "total_vram_mib": gpu.total_vram_mib,
                    "labels": json_load(gpu.labels_json),
                    "health": gpu.health,
                    "enabled": gpu.enabled,
                    "state": gpu_state,
                    "state_reason": reason,
                    "first_seen_at": _iso(gpu.first_seen_at),
                    "last_seen_at": _iso(gpu.last_seen_at),
                    "telemetry": self._telemetry_dict(telemetry),
                    "processes": [self._process_dict(process) for process in processes],
                    "lease": lease_payloads.get(lease.id) if lease else None,
                }
                gpu_payloads.append(payload)

            all_gpu_payloads = gpu_payloads
            counts = defaultdict(int)
            for gpu in all_gpu_payloads:
                counts[gpu["state"]] += 1
            claimed_states = {"HELD", "LEASED_IDLE", "RUNNING_MANAGED", "ORPHANED_BUSY", "CONFLICT"}
            abnormal_states = {"UNKNOWN_RECOVERING", "UNKNOWN_STALE", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY"}
            summary = {
                "online_servers": sum(
                    endpoint["monitor"]["status"] == "ONLINE" for endpoint in endpoint_payloads
                ),
                "total_servers": len(endpoint_payloads),
                "total_gpus": len(all_gpu_payloads),
                "available_gpus": counts["AVAILABLE"],
                "busy_gpus": counts["BUSY_UNMANAGED"] + counts["RUNNING_MANAGED"],
                "claimed_gpus": sum(counts[item] for item in claimed_states),
                "abnormal_gpus": sum(counts[item] for item in abnormal_states),
            }
            ages = [
                max(0.0, (now - (_as_utc(item.observed_at) or now)).total_seconds())
                for item in telemetry_by_gpu.values()
            ]

            requested_state = "AVAILABLE" if only_available else state.upper() if state else None
            if requested_state:
                gpu_payloads = [item for item in gpu_payloads if item["state"] == requested_state]
            if compact:
                gpu_payloads = [
                    {
                        "id": item["id"],
                        "endpoint_id": item["endpoint_id"],
                        "gpu_index": item["gpu_index"],
                        "name": item["name"],
                        "total_vram_mib": item["total_vram_mib"],
                        "state": item["state"],
                        "state_reason": item["state_reason"],
                        "telemetry": (
                            {
                                "observed_at": item["telemetry"]["observed_at"],
                                "memory_used_mib": item["telemetry"]["memory_used_mib"],
                                "memory_free_mib": item["telemetry"]["memory_free_mib"],
                                "gpu_utilization_pct": item["telemetry"]["gpu_utilization_pct"],
                                "temperature_c": item["telemetry"]["temperature_c"],
                            }
                            if item["telemetry"]
                            else None
                        ),
                        "process_count": len(item["processes"]),
                        "owner": item["lease"]["actor_id"] if item["lease"] else None,
                        "task_ref": item["lease"]["task_ref"] if item["lease"] else None,
                        "expires_at": item["lease"]["expires_at"] if item["lease"] else None,
                    }
                    for item in gpu_payloads
                ]

            visible_gpu_ids = {gpu.id for gpu in gpus}
            data = {
                "summary": summary,
                "data_age_seconds": round(max(ages), 1) if ages else None,
                "endpoints": endpoint_payloads,
                "gpus": gpu_payloads,
                "leases": [
                    lease_payloads[lease.id]
                    for lease in visible_leases
                    if any(
                        resource.active and resource.gpu_id in visible_gpu_ids
                        for resource in resources_by_lease[lease.id]
                    )
                ],
                "requests": [self._request_dict(request) for request in queued_requests],
                "reservations": [self._reservation_dict(item) for item in visible_reservations],
                "freshness_seconds": self.inventory.collector.stale_after_seconds,
                "admission_boundary": "A lease coordinates GPUs only; it does not authorize workload launch.",
            }
            return self.envelope(session, data)

        return self._read(operation)

    def coordination(self, actor: ActorContext) -> dict[str, Any]:
        """Return an agent-readable shared coordination board from one broker snapshot.

        This is intentionally observational: the broker already owns fair queue
        ordering and placement. Agents use this board to see current consumers,
        real process attribution, and remaining capacity without appointing a
        separate scheduler or inspecting servers themselves.
        """

        snapshot = self.snapshot(actor, compact=False)
        data = snapshot["data"]
        gpus: list[dict[str, Any]] = data["gpus"]
        gpus_by_id = {gpu["id"]: gpu for gpu in gpus}
        gpus_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for gpu in gpus:
            gpus_by_endpoint[gpu["endpoint_id"]].append(gpu)

        def average(values: Iterable[int | float | None]) -> float | None:
            present = [float(value) for value in values if value is not None]
            return round(sum(present) / len(present), 1) if present else None

        def gpu_state_counts(values: Iterable[dict[str, Any]]) -> dict[str, int]:
            counts: dict[str, int] = defaultdict(int)
            for gpu in values:
                counts[gpu["state"]] += 1
            return dict(sorted(counts.items()))

        def lease_activity(values: list[dict[str, Any]]) -> str:
            states = {gpu["state"] for gpu in values}
            if states.intersection({"CONFLICT", "ORPHANED_BUSY"}):
                return "needs_attention"
            if "BUSY_UNMANAGED" in states:
                return "unattributed_compute"
            if "RUNNING_MANAGED" in states:
                return "running"
            if values and all(gpu["state"] == "LEASED_IDLE" for gpu in values):
                return "lease_idle"
            if values and all(gpu["state"] == "HELD" for gpu in values):
                return "held"
            return "starting"

        lease_cards: list[dict[str, Any]] = []
        consumers_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
        agents: dict[str, dict[str, Any]] = {}
        signals: list[dict[str, Any]] = []
        for lease in data["leases"]:
            lease_gpus = [gpus_by_id[gpu_id] for gpu_id in lease["gpu_ids"] if gpu_id in gpus_by_id]
            endpoint_ids = sorted({gpu["endpoint_id"] for gpu in lease_gpus})
            activity = lease_activity(lease_gpus)
            state_counts = gpu_state_counts(lease_gpus)
            telemetry = [gpu["telemetry"] for gpu in lease_gpus if gpu["telemetry"] is not None]
            card = {
                "lease_id": lease["id"],
                "agent_name": lease["actor_id"],
                "project_id": lease["project_id"],
                "task": lease["task_ref"],
                "state": lease["state"],
                "activity": activity,
                "gpu_count": len(lease_gpus),
                "servers": endpoint_ids,
                "gpu_states": state_counts,
                "observed_gpu_utilization_pct": average(
                    item["gpu_utilization_pct"] for item in telemetry
                ),
                "observed_memory_used_mib": sum(item["memory_used_mib"] for item in telemetry),
                "observed_process_count": sum(len(gpu["processes"]) for gpu in lease_gpus),
                "workloads": [
                    {"run_id": workload["run_id"], "process_key_count": len(workload["process_keys"])}
                    for workload in lease["workloads"]
                ],
                "issued_at": lease["issued_at"],
                "expires_at": lease["expires_at"],
            }
            lease_cards.append(card)
            for endpoint_id in endpoint_ids:
                endpoint_gpu_count = sum(gpu["endpoint_id"] == endpoint_id for gpu in lease_gpus)
                consumers_by_endpoint[endpoint_id].append(
                    {
                        "lease_id": card["lease_id"],
                        "agent_name": card["agent_name"],
                        "project_id": card["project_id"],
                        "task": card["task"],
                        "gpu_count": endpoint_gpu_count,
                        "activity": card["activity"],
                    }
                )
            agent = agents.setdefault(
                lease["actor_id"],
                {
                    "agent_name": lease["actor_id"],
                    "active_leases": 0,
                    "leased_gpus": 0,
                    "managed_running_gpus": 0,
                    "idle_leased_gpus": 0,
                    "projects": set(),
                    "servers": set(),
                },
            )
            agent["active_leases"] += 1
            agent["leased_gpus"] += len(lease_gpus)
            agent["managed_running_gpus"] += state_counts.get("RUNNING_MANAGED", 0)
            agent["idle_leased_gpus"] += state_counts.get("LEASED_IDLE", 0)
            agent["projects"].add(lease["project_id"])
            agent["servers"].update(endpoint_ids)
            if activity == "lease_idle":
                signals.append(
                    {
                        "kind": "lease_idle",
                        "severity": "info",
                        "lease_id": lease["id"],
                        "agent_name": lease["actor_id"],
                        "message": "active lease has no observed compute process yet",
                    }
                )
            elif activity == "unattributed_compute":
                signals.append(
                    {
                        "kind": "unattributed_compute",
                        "severity": "warning",
                        "lease_id": lease["id"],
                        "agent_name": lease["actor_id"],
                        "message": "compute process is observed but not bound to this lease",
                    }
                )
            elif activity == "needs_attention":
                signals.append(
                    {
                        "kind": "lease_conflict",
                        "severity": "critical",
                        "lease_id": lease["id"],
                        "agent_name": lease["actor_id"],
                        "message": "lease has a process-attribution or expiry conflict",
                    }
                )

        server_cards: list[dict[str, Any]] = []
        for endpoint in data["endpoints"]:
            endpoint_gpus = gpus_by_endpoint[endpoint["id"]]
            endpoint_telemetry = [gpu["telemetry"] for gpu in endpoint_gpus if gpu["telemetry"] is not None]
            state_counts = gpu_state_counts(endpoint_gpus)
            server_cards.append(
                {
                    "server_id": endpoint["id"],
                    "monitor_status": endpoint["monitor"]["status"],
                    "host_telemetry": endpoint["host_telemetry"],
                    "capacity": {
                        "total_gpus": len(endpoint_gpus),
                        "available_gpus": state_counts.get("AVAILABLE", 0),
                        "leased_gpus": sum(1 for gpu in endpoint_gpus if gpu["lease"] is not None),
                        "managed_running_gpus": state_counts.get("RUNNING_MANAGED", 0),
                        "idle_leased_gpus": state_counts.get("LEASED_IDLE", 0),
                        "unattributed_compute_gpus": state_counts.get("BUSY_UNMANAGED", 0),
                        "gpu_states": state_counts,
                        "observed_gpu_utilization_pct": average(
                            item["gpu_utilization_pct"] for item in endpoint_telemetry
                        ),
                        "observed_memory_used_mib": sum(
                            item["memory_used_mib"] for item in endpoint_telemetry
                        ),
                    },
                    "consumers": sorted(
                        consumers_by_endpoint[endpoint["id"]],
                        key=lambda item: (item["agent_name"], item["lease_id"]),
                    ),
                }
            )

        for request in data["requests"]:
            signals.append(
                {
                    "kind": "queued_request",
                    "severity": "info",
                    "request_id": request["id"],
                    "project_id": request["project_id"],
                    "task": request["task_ref"],
                    "gpu_count": request["constraints"]["gpu_count"],
                    "message": request["blocked_reason"] or "waiting for scheduler placement",
                }
            )

        agent_cards = [
            {
                **agent,
                "projects": sorted(agent["projects"]),
                "servers": sorted(agent["servers"]),
            }
            for agent in agents.values()
        ]
        agent_cards.sort(key=lambda item: item["agent_name"])
        lease_cards.sort(key=lambda item: (item["agent_name"], item["lease_id"]))
        signals.sort(key=lambda item: (item["severity"], item.get("agent_name", ""), item["kind"]))
        total_telemetry = [gpu["telemetry"] for gpu in gpus if gpu["telemetry"] is not None]
        coordination_summary = {
            **data["summary"],
            "active_leases": len(lease_cards),
            "active_agents": len(agent_cards),
            "queued_requests": len(data["requests"]),
            "queued_gpus": sum(item["constraints"]["gpu_count"] for item in data["requests"]),
            "managed_running_gpus": sum(
                item["gpu_states"].get("RUNNING_MANAGED", 0) for item in lease_cards
            ),
            "idle_leased_gpus": sum(item["gpu_states"].get("LEASED_IDLE", 0) for item in lease_cards),
            "observed_gpu_utilization_pct": average(
                item["gpu_utilization_pct"] for item in total_telemetry
            ),
        }
        return {
            **snapshot,
            "data": {
                "summary": coordination_summary,
                "servers": server_cards,
                "agents": agent_cards,
                "leases": lease_cards,
                "queue": data["requests"],
                "signals": signals,
                "guidance": (
                    "This board is read-only. Claims without a requested server are placed by the broker's "
                    "shared scheduler; agents should not appoint or emulate a separate scheduler."
                ),
            },
        }

    def list_endpoints(self, actor: ActorContext) -> dict[str, Any]:
        snapshot = self.snapshot(actor)
        return {**snapshot, "data": snapshot["data"]["endpoints"]}

    def list_gpus(
        self,
        actor: ActorContext,
        *,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(
            actor,
            state=state,
            endpoint_id=endpoint_id,
            only_available=only_available,
            compact=compact,
        )
        values = snapshot["data"]["gpus"]
        return {**snapshot, "data": values}

    def gpu_history(
        self,
        actor: ActorContext,
        gpu_id: str,
        *,
        window_seconds: int = 3600,
        max_points: int = 120,
    ) -> dict[str, Any]:
        """Return a bounded, downsampled series only when a GPU detail view asks for it."""

        if not 300 <= window_seconds <= TELEMETRY_HISTORY_RETENTION_SECONDS:
            raise BrokerError(
                "invalid_history_window",
                "history window must be between 5 minutes and 24 hours",
                status_code=422,
            )
        if not 10 <= max_points <= 120:
            raise BrokerError(
                "invalid_history_points",
                "history points must be between 10 and 120",
                status_code=422,
            )

        def operation(session: Session) -> dict[str, Any]:
            gpu = session.get(GPUDevice, gpu_id)
            if gpu is None:
                raise BrokerError("gpu_not_found", "GPU is not visible or does not exist", status_code=404)
            now = utcnow()
            cutoff = now - timedelta(seconds=window_seconds)
            samples: list[TelemetryCurrent | TelemetrySnapshot] = list(
                session.scalars(
                    select(TelemetrySnapshot)
                    .where(
                        TelemetrySnapshot.gpu_id == gpu_id,
                        TelemetrySnapshot.observed_at >= cutoff,
                    )
                    .order_by(TelemetrySnapshot.observed_at, TelemetrySnapshot.id)
                ).all()
            )
            current = session.get(TelemetryCurrent, gpu_id)
            if current is not None and (
                not samples
                or (_as_utc(current.observed_at) or now)
                > (_as_utc(samples[-1].observed_at) or now)
            ):
                samples.append(current)

            buckets: list[list[TelemetryCurrent | TelemetrySnapshot]]
            if len(samples) <= max_points:
                buckets = [[sample] for sample in samples]
            else:
                buckets = [
                    samples[index * len(samples) // max_points : (index + 1) * len(samples) // max_points]
                    for index in range(max_points)
                ]

            def average(bucket: list[TelemetryCurrent | TelemetrySnapshot], name: str) -> float | None:
                values = [getattr(sample, name) for sample in bucket]
                present = [float(value) for value in values if value is not None]
                return round(sum(present) / len(present), 2) if present else None

            points = []
            for bucket in buckets:
                used = average(bucket, "memory_used_mib")
                points.append(
                    {
                        "observed_at": _iso(bucket[-1].observed_at),
                        "gpu_utilization_pct": average(bucket, "gpu_utilization_pct"),
                        "memory_used_pct": (
                            round((used or 0) * 100 / gpu.total_vram_mib, 2)
                            if gpu.total_vram_mib
                            else None
                        ),
                        "memory_used_mib": used,
                        "temperature_c": average(bucket, "temperature_c"),
                        "power_watts": average(bucket, "power_watts"),
                    }
                )
            return self.envelope(
                session,
                {
                    "gpu_id": gpu.id,
                    "endpoint_id": gpu.endpoint_id,
                    "gpu_index": gpu.gpu_index,
                    "window_seconds": window_seconds,
                    "point_count": len(points),
                    "points": points,
                },
            )

        return self._read(operation)

    def prune_telemetry_history(
        self, older_than_seconds: int = TELEMETRY_HISTORY_RETENTION_SECONDS
    ) -> int:
        """Internal hourly retention pass; current telemetry, leases and audit are untouched."""

        cutoff = utcnow() - timedelta(seconds=older_than_seconds)

        def operation(session: Session) -> int:
            result = session.execute(
                delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
            )
            return max(0, result.rowcount or 0)

        return self._write(operation)

    # ---- collector input and telemetry / process reconciliation ----------------

    def ingest_observation(
        self, observation: EndpointObservation, *, provider: str = "raw-ssh"
    ) -> dict[str, Any]:
        """Persist one all-or-nothing read-only endpoint observation.

        Collector data is accepted only from an internal collector call in the
        pilot. The HTTP layer never exposes arbitrary remote command execution.
        """

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            endpoint = session.get(Endpoint, observation.endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "collector reported an unknown endpoint", status_code=404)
            revision = self._bump_revision(session, now)
            observed_at = ensure_utc(observation.observed_at)
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            if host_telemetry is None:
                host_telemetry = EndpointTelemetryCurrent(
                    endpoint_id=endpoint.id,
                    observed_at=observed_at,
                    collected_at=now,
                    cpu_count=observation.host.cpu_count,
                    load_1m=observation.host.load_1m,
                    memory_total_mib=observation.host.memory_total_mib,
                    memory_available_mib=observation.host.memory_available_mib,
                    provider=provider,
                )
                session.add(host_telemetry)
            else:
                host_telemetry.observed_at = observed_at
                host_telemetry.collected_at = now
                host_telemetry.cpu_count = observation.host.cpu_count
                host_telemetry.load_1m = observation.host.load_1m
                host_telemetry.memory_total_mib = observation.host.memory_total_mib
                host_telemetry.memory_available_mib = observation.host.memory_available_mib
                host_telemetry.provider = provider
            gpu_ids = [f"{endpoint.id}:{sample.gpu_uuid}" for sample in observation.gpus]
            existing_gpus = {
                gpu.id: gpu
                for gpu in session.scalars(
                    select(GPUDevice).where(GPUDevice.id.in_(gpu_ids))
                ).all()
            }
            current_telemetry = {
                item.gpu_id: item
                for item in session.scalars(
                    select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(gpu_ids))
                ).all()
            }
            latest_history = {
                gpu_id: _as_utc(latest_observed_at)
                for gpu_id, latest_observed_at in session.execute(
                    select(TelemetrySnapshot.gpu_id, func.max(TelemetrySnapshot.observed_at))
                    .where(TelemetrySnapshot.gpu_id.in_(gpu_ids))
                    .group_by(TelemetrySnapshot.gpu_id)
                ).all()
            }
            by_uuid: dict[str, GPUDevice] = {}
            history_points_written = 0
            for sample in observation.gpus:
                gpu_id = f"{endpoint.id}:{sample.gpu_uuid}"
                gpu = existing_gpus.get(gpu_id)
                if gpu is None:
                    gpu = GPUDevice(
                        id=gpu_id,
                        endpoint_id=endpoint.id,
                        gpu_uuid=sample.gpu_uuid,
                        gpu_index=sample.gpu_index,
                        name=sample.name,
                        total_vram_mib=sample.total_vram_mib,
                        labels_json="[]",
                        health=sample.health,
                        enabled=True,
                        first_seen_at=observed_at,
                        last_seen_at=observed_at,
                    )
                    session.add(gpu)
                else:
                    gpu.gpu_index = sample.gpu_index
                    gpu.name = sample.name
                    gpu.total_vram_mib = sample.total_vram_mib
                    gpu.health = sample.health
                    gpu.last_seen_at = observed_at
                by_uuid[sample.gpu_uuid] = gpu
                current = current_telemetry.get(gpu_id)
                if current is None:
                    current = TelemetryCurrent(
                        gpu_id=gpu_id,
                        observed_at=observed_at,
                        collected_at=now,
                        memory_used_mib=sample.memory_used_mib,
                        memory_free_mib=sample.memory_free_mib,
                        gpu_utilization_pct=sample.gpu_utilization_pct,
                        memory_utilization_pct=sample.memory_utilization_pct,
                        temperature_c=sample.temperature_c,
                        power_watts=sample.power_watts,
                        pstate=sample.pstate,
                        health=sample.health,
                        provider=provider,
                    )
                    session.add(current)
                else:
                    current.observed_at = observed_at
                    current.collected_at = now
                    current.memory_used_mib = sample.memory_used_mib
                    current.memory_free_mib = sample.memory_free_mib
                    current.gpu_utilization_pct = sample.gpu_utilization_pct
                    current.memory_utilization_pct = sample.memory_utilization_pct
                    current.temperature_c = sample.temperature_c
                    current.power_watts = sample.power_watts
                    current.pstate = sample.pstate
                    current.health = sample.health
                    current.provider = provider
                last_history_at = latest_history.get(gpu_id)
                if last_history_at is None or (
                    observed_at - last_history_at
                ).total_seconds() >= TELEMETRY_HISTORY_INTERVAL_SECONDS:
                    session.add(
                        TelemetrySnapshot(
                            gpu_id=gpu_id,
                            observed_at=observed_at,
                            collected_at=now,
                            memory_used_mib=sample.memory_used_mib,
                            memory_free_mib=sample.memory_free_mib,
                            gpu_utilization_pct=sample.gpu_utilization_pct,
                            memory_utilization_pct=sample.memory_utilization_pct,
                            temperature_c=sample.temperature_c,
                            power_watts=sample.power_watts,
                            pstate=sample.pstate,
                            health=sample.health,
                            provider=provider,
                        )
                    )
                    history_points_written += 1
            session.flush()
            observed_process_keys: set[tuple[str, int, str, datetime]] = set()
            for process in observation.processes:
                gpu = by_uuid.get(process.gpu_uuid)
                if gpu is None:
                    self._upsert_alert(
                        session,
                        alert_type="unknown_process_gpu",
                        severity="warning",
                        resource_type="endpoint",
                        resource_id=endpoint.id,
                        message="collector reported a compute process for an unobserved GPU UUID",
                        now=now,
                    )
                    continue
                started_at = ensure_utc(process.process_started_at)
                current = session.scalar(
                    select(ProcessObservation).where(
                        ProcessObservation.gpu_id == gpu.id,
                        ProcessObservation.pid == process.pid,
                        ProcessObservation.boot_id == observation.boot_id,
                        ProcessObservation.process_started_at == started_at,
                    )
                )
                if current is None:
                    # `ps etimes` is intentionally used instead of a full
                    # process command line, but it makes the calculated
                    # start timestamp susceptible to a one-second boundary
                    # race. Reuse only the immediately-active identity for
                    # the same GPU/PID/boot when the derived times are very
                    # close; a materially different start time remains a
                    # new, fail-closed process identity.
                    candidate = session.scalar(
                        select(ProcessObservation)
                        .where(
                            ProcessObservation.gpu_id == gpu.id,
                            ProcessObservation.pid == process.pid,
                            ProcessObservation.boot_id == observation.boot_id,
                            ProcessObservation.active.is_(True),
                        )
                        .order_by(ProcessObservation.last_seen_at.desc())
                    )
                    candidate_started_at = _as_utc(candidate.process_started_at) if candidate else None
                    if (
                        candidate is not None
                        and candidate_started_at is not None
                        and abs((candidate_started_at - started_at).total_seconds())
                        <= PROCESS_START_TIME_JITTER_SECONDS
                    ):
                        current = candidate
                        started_at = candidate_started_at
                key = (gpu.id, process.pid, observation.boot_id, started_at)
                observed_process_keys.add(key)
                if current is None:
                    session.add(
                        ProcessObservation(
                            endpoint_id=endpoint.id,
                            gpu_id=gpu.id,
                            pid=process.pid,
                            boot_id=observation.boot_id,
                            process_started_at=started_at,
                            username=process.username,
                            executable=self._sanitize_executable(process.executable),
                            used_memory_mib=process.used_memory_mib,
                            first_seen_at=now,
                            last_seen_at=now,
                            observations=1,
                            active=True,
                        )
                    )
                else:
                    current.username = process.username
                    current.executable = self._sanitize_executable(process.executable)
                    current.used_memory_mib = process.used_memory_mib
                    current.last_seen_at = now
                    current.observations += 1
                    current.active = True
            session.flush()
            current_processes = session.scalars(
                select(ProcessObservation).where(
                    ProcessObservation.endpoint_id == endpoint.id,
                    ProcessObservation.active.is_(True),
                )
            ).all()
            for prior in current_processes:
                key = (prior.gpu_id, prior.pid, prior.boot_id, _as_utc(prior.process_started_at))
                if key not in observed_process_keys:
                    prior.active = False
            provider_state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == provider, ProviderState.endpoint_id == endpoint.id
                )
            )
            recovered = provider_state is not None and provider_state.last_error is not None
            if provider_state is None:
                session.add(
                    ProviderState(
                        provider=provider,
                        endpoint_id=endpoint.id,
                        last_success_at=now,
                        last_attempt_at=now,
                        last_error=None,
                        revision=revision,
                    )
                )
            else:
                provider_state.last_success_at = now
                provider_state.last_attempt_at = now
                provider_state.last_error = None
                provider_state.revision = revision
            if recovered:
                for alert in session.scalars(
                    select(Alert).where(
                        Alert.alert_type == "collector_unreachable",
                        Alert.resource_type == "endpoint",
                        Alert.resource_id == endpoint.id,
                        Alert.active.is_(True),
                    )
                ).all():
                    alert.active = False
                    alert.last_seen_at = now
            self._reconcile_leases(session, now, actor_id=f"collector:{endpoint.id}")
            # A fresh observation can make a previously fail-closed request eligible.
            self._allocate_queued(session, now, revision)
            event = None
            if recovered:
                event = self._audit(
                    session,
                    actor_id=f"collector:{endpoint.id}",
                    action="telemetry.recovered",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    after={
                        "gpu_count": len(observation.gpus),
                        "process_count": len(observation.processes),
                    },
                    summary={"provider": provider, "revision": revision},
                    now=now,
                )
            return {
                "event_id": event.id if event else None,
                "snapshot_revision": revision,
                "endpoint_id": endpoint.id,
                "gpu_count": len(observation.gpus),
                "process_count": len(observation.processes),
                "history_points_written": history_points_written,
            }

        return self._write(operation)

    @staticmethod
    def _sanitize_executable(value: str) -> str:
        # Collector never stores cmdline/cwd/environment. Keep a bounded basename-like label only.
        stripped = value.replace("\\x00", " ").replace("\n", " ").strip()
        return stripped.rsplit("/", maxsplit=1)[-1][:255] or "unknown"

    def record_provider_failure(self, endpoint_id: str, message: str, *, provider: str = "raw-ssh") -> None:
        def operation(session: Session) -> None:
            now = utcnow()
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "collector reported an unknown endpoint", status_code=404)
            revision = self._bump_revision(session, now)
            state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == provider, ProviderState.endpoint_id == endpoint_id
                )
            )
            first_failure = state is None or state.last_error is None
            if state is None:
                session.add(
                    ProviderState(
                        provider=provider,
                        endpoint_id=endpoint_id,
                        last_success_at=None,
                        last_attempt_at=now,
                        last_error=message[:1000],
                        revision=revision,
                    )
                )
            else:
                state.last_attempt_at = now
                state.last_error = message[:1000]
                state.revision = revision
            self._upsert_alert(
                session,
                alert_type="collector_unreachable",
                severity="warning",
                resource_type="endpoint",
                resource_id=endpoint_id,
                message="collector failed; endpoint will fail closed once telemetry becomes stale",
                now=now,
            )
            if first_failure:
                self._audit(
                    session,
                    actor_id=f"collector:{endpoint_id}",
                    action="telemetry.failed",
                    resource_type="endpoint",
                    resource_id=endpoint_id,
                    result="failure",
                    summary={"provider": provider, "message": message[:300]},
                    now=now,
                )

        self._write(operation)

    def _upsert_alert(
        self,
        session: Session,
        *,
        alert_type: str,
        severity: str,
        resource_type: str,
        resource_id: str,
        message: str,
        now: datetime,
    ) -> Alert:
        alert = session.scalar(
            select(Alert).where(
                Alert.alert_type == alert_type,
                Alert.resource_type == resource_type,
                Alert.resource_id == resource_id,
                Alert.active.is_(True),
            )
        )
        if alert is None:
            alert = Alert(
                id=secrets.token_hex(16),
                alert_type=alert_type,
                severity=severity,
                resource_type=resource_type,
                resource_id=resource_id,
                message=message[:1000],
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
            session.add(alert)
        else:
            alert.severity = severity
            alert.message = message[:1000]
            alert.last_seen_at = now
        return alert

    # ---- atomic scheduling -----------------------------------------------------

    def _project_usage(self, session: Session) -> tuple[dict[str, int], dict[str, int]]:
        gpu_usage: dict[str, int] = defaultdict(int)
        lease_usage: dict[str, int] = defaultdict(int)
        active_leases = session.scalars(
            select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))
        ).all()
        for lease in active_leases:
            lease_usage[lease.project_id] += 1
            gpu_usage[lease.project_id] += len(
                session.scalars(
                    select(LeaseResource.gpu_id).where(
                        LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                    )
                ).all()
            )
        return gpu_usage, lease_usage

    def _project_can_allocate(
        self,
        session: Session,
        project: Project,
        constraints: ResourceConstraints,
        gpu_usage: dict[str, int],
        lease_usage: dict[str, int],
    ) -> str | None:
        if not project.enabled:
            return "project is disabled"
        if project.quota_gpus is not None and gpu_usage[project.id] + constraints.gpu_count > project.quota_gpus:
            return f"project GPU quota {project.quota_gpus} would be exceeded"
        if project.concurrency_limit is not None and lease_usage[project.id] >= project.concurrency_limit:
            return f"project concurrency limit {project.concurrency_limit} is reached"
        return None

    @staticmethod
    def _ensure_claim_project(session: Session, project_id: str, now: datetime) -> Project:
        """Persist a neutral project tag only because request rows reference it.

        A project id is supplied by the claimant; it is not an enrollment or
        endpoint-access check.  Configured projects can still carry optional
        fairness or quota policy, while first use gets the neutral defaults.
        """

        project = session.get(Project, project_id)
        if project is not None:
            return project
        project = Project(
            id=project_id,
            display_name=project_id,
            weight=1,
            quota_gpus=None,
            concurrency_limit=None,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()
        return project

    def _reservation_blocks_gpu(
        self,
        session: Session,
        gpu_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> bool:
        reservations = session.scalars(
            select(Reservation).where(
                Reservation.state == "ACTIVE",
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
        ).all()
        return any(gpu_id in json_load(reservation.gpu_ids_json) for reservation in reservations)

    def _eligible_gpus(
        self,
        session: Session,
        *,
        request: AllocationRequest,
        now: datetime,
    ) -> tuple[list[GPUDevice], dict[str, int]]:
        constraints = ResourceConstraints.model_validate(json_load(request.constraints_json))
        lease_end = now + timedelta(seconds=request.duration_seconds)
        excluded: dict[str, int] = defaultdict(int)
        values: list[GPUDevice] = []
        all_gpus = session.scalars(select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)).all()
        for gpu in all_gpus:
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None:
                excluded["missing_endpoint"] += 1
                continue
            if constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                excluded["endpoint_allowlist"] += 1
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                excluded["endpoint_denylist"] += 1
                continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                excluded["gpu_allowlist"] += 1
                continue
            if gpu.id in constraints.deny_gpu_ids:
                excluded["gpu_denylist"] += 1
                continue
            endpoint_labels = set(json_load(endpoint.labels_json))
            gpu_labels = set(json_load(gpu.labels_json))
            if not set(constraints.endpoint_labels).issubset(endpoint_labels):
                excluded["endpoint_labels"] += 1
                continue
            if not set(constraints.gpu_labels).issubset(gpu_labels):
                excluded["gpu_labels"] += 1
                continue
            if constraints.min_total_vram_mib and gpu.total_vram_mib < constraints.min_total_vram_mib:
                excluded["total_vram"] += 1
                continue
            state, _reason = self._gpu_state(session, gpu, now)
            if state != "AVAILABLE":
                excluded[state.lower()] += 1
                continue
            telemetry = self._latest_telemetry(session, gpu.id)
            assert telemetry is not None  # AVAILABLE implies fresh telemetry
            if (
                constraints.min_free_vram_mib is not None
                and telemetry.memory_free_mib < constraints.min_free_vram_mib
            ):
                excluded["free_vram"] += 1
                continue
            if self._reservation_blocks_gpu(session, gpu.id, start=now, end=lease_end):
                excluded["future_reservation"] += 1
                continue
            values.append(gpu)
        return values, dict(excluded)

    @staticmethod
    def _select_resources(
        candidates: list[GPUDevice], constraints: ResourceConstraints
    ) -> list[GPUDevice] | None:
        if len(candidates) < constraints.gpu_count:
            return None
        by_endpoint: dict[str, list[GPUDevice]] = defaultdict(list)
        for gpu in candidates:
            by_endpoint[gpu.endpoint_id].append(gpu)
        for values in by_endpoint.values():
            values.sort(key=lambda item: item.gpu_index)

        if constraints.placement == "exact":
            by_id = {gpu.id: gpu for gpu in candidates}
            try:
                return [by_id[gpu_id] for gpu_id in constraints.gpu_ids]
            except KeyError:
                return None

        per_node = constraints.gpus_per_node
        if per_node is not None:
            hosts = [
                (endpoint_id, values)
                for endpoint_id, values in sorted(by_endpoint.items())
                if len(values) >= per_node
            ]
            if len(hosts) < constraints.nodes:
                return None
            if constraints.placement == "spread":
                hosts.sort(key=lambda item: (len(item[1]), item[0]))
            selected: list[GPUDevice] = []
            for _endpoint_id, values in hosts[: constraints.nodes]:
                selected.extend(values[:per_node])
            return selected if len(selected) == constraints.gpu_count else None

        if constraints.same_host:
            hosts = [
                (endpoint_id, values)
                for endpoint_id, values in sorted(by_endpoint.items())
                if len(values) >= constraints.gpu_count
            ]
            if not hosts:
                return None
            if constraints.placement == "spread":
                hosts.sort(key=lambda item: (len(item[1]), item[0]))
            return hosts[0][1][: constraints.gpu_count]

        if constraints.placement == "pack":
            ordered = [gpu for _endpoint, values in sorted(by_endpoint.items()) for gpu in values]
            return ordered[: constraints.gpu_count]

        # spread: take one GPU per endpoint in rounds, then fill deterministically.
        selected = []
        queues = [values[:] for _endpoint, values in sorted(by_endpoint.items())]
        while queues and len(selected) < constraints.gpu_count:
            next_queues: list[list[GPUDevice]] = []
            for values in queues:
                if len(selected) >= constraints.gpu_count:
                    break
                if values:
                    selected.append(values.pop(0))
                if values:
                    next_queues.append(values)
            queues = next_queues
        return selected if len(selected) == constraints.gpu_count else None

    def _queue_candidates(self, session: Session, now: datetime) -> list[AllocationRequest]:
        queued = session.scalars(
            select(AllocationRequest)
            .where(AllocationRequest.state == "QUEUED")
            .order_by(AllocationRequest.created_at, AllocationRequest.id)
        ).all()
        valid: list[AllocationRequest] = []
        for request in queued:
            if request.deadline is not None and (_as_utc(request.deadline) or now) <= now:
                request.state = "EXPIRED"
                request.blocked_reason = "deadline passed before allocation"
                request.updated_at = now
                self._audit(
                    session,
                    actor_id=request.actor_id,
                    action="request.expired",
                    resource_type="request",
                    resource_id=request.id,
                    result="success",
                    after={"state": request.state},
                    summary={"reason": request.blocked_reason},
                    now=now,
                )
                continue
            if request.start_after is not None and (_as_utc(request.start_after) or now) > now:
                request.blocked_reason = "waiting for start_after"
                request.updated_at = now
                continue
            valid.append(request)
        return valid

    def _fair_order(
        self,
        session: Session,
        requests: list[AllocationRequest],
        now: datetime,
    ) -> list[AllocationRequest]:
        """Explainable weighted fair order: least active GPUs per project weight, then aging."""

        gpu_usage, _lease_usage = self._project_usage(session)
        projects = {project.id: project for project in session.scalars(select(Project)).all()}
        by_project: dict[str, list[AllocationRequest]] = defaultdict(list)
        for request in requests:
            by_project[request.project_id].append(request)
        ordered: list[AllocationRequest] = []
        while by_project:
            choices: list[tuple[float, float, str]] = []
            for project_id, entries in by_project.items():
                project = projects.get(project_id)
                if project is None:
                    continue
                oldest = _as_utc(entries[0].created_at) or now
                age_seconds = max(0.0, (now - oldest).total_seconds())
                choices.append((gpu_usage[project_id] / project.weight, -age_seconds, project_id))
            if not choices:
                break
            _ratio, _aging, selected_project = min(choices)
            selected_request = by_project[selected_project].pop(0)
            ordered.append(selected_request)
            # Virtual usage makes the order deficit-like: a project cannot win
            # every tie merely because several of its requests were submitted first.
            gpu_usage[selected_project] += ResourceConstraints.model_validate(
                json_load(selected_request.constraints_json)
            ).gpu_count
            if not by_project[selected_project]:
                del by_project[selected_project]
        return ordered

    def _allocate_queued(self, session: Session, now: datetime, revision: int) -> list[str]:
        allocated: list[str] = []
        for request in self._fair_order(session, self._queue_candidates(session, now), now):
            project = session.get(Project, request.project_id)
            if project is None:
                request.state = "REJECTED"
                request.blocked_reason = "project no longer exists"
                request.updated_at = now
                continue
            constraints = ResourceConstraints.model_validate(json_load(request.constraints_json))
            gpu_usage, lease_usage = self._project_usage(session)
            policy_block = self._project_can_allocate(
                session, project, constraints, gpu_usage, lease_usage
            )
            if policy_block:
                request.blocked_reason = policy_block
                request.updated_at = now
                continue
            candidates, excluded = self._eligible_gpus(
                session, request=request, now=now
            )
            resources = self._select_resources(candidates, constraints)
            if resources is None:
                top_exclusions = ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(excluded.items(), key=lambda item: (-item[1], item[0]))[:3]
                )
                blocked_reason = (
                    f"insufficient eligible GPUs: need {constraints.gpu_count}, have {len(candidates)}"
                )
                if top_exclusions:
                    blocked_reason += f"; blocked by {top_exclusions}"
                changed = request.blocked_reason != blocked_reason
                request.blocked_reason = blocked_reason
                request.updated_at = now
                if changed:
                    self._audit(
                        session,
                        actor_id=request.actor_id,
                        action="scheduler.blocked",
                        resource_type="request",
                        resource_id=request.id,
                        result="success",
                        after={"blocked_reason": request.blocked_reason},
                        summary={"excluded": excluded},
                        now=now,
                    )
                continue
            lease = Lease(
                id=secrets.token_hex(16),
                request_id=request.id,
                actor_id=request.actor_id,
                project_id=request.project_id,
                state="ACTIVE" if request.auto_activate else "HELD",
                issued_at=now,
                expires_at=now + timedelta(seconds=request.duration_seconds),
                last_heartbeat_at=now,
                activated_at=now if request.auto_activate else None,
                released_at=None,
                release_reason=None,
                issued_revision=revision,
            )
            session.add(lease)
            session.flush()
            for gpu in resources:
                session.add(LeaseResource(lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None))
            request.state = "ACTIVE" if request.auto_activate else "LEASED"
            request.blocked_reason = None
            request.updated_at = now
            self._audit(
                session,
                actor_id=request.actor_id,
                action="lease.issued",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"gpu_ids": [gpu.id for gpu in resources], "state": lease.state},
                summary={
                    "request_id": request.id,
                    "project_id": request.project_id,
                    "candidate_count": len(candidates),
                    "excluded": excluded,
                    "placement": constraints.placement,
                    "gang_size": len(resources),
                },
                now=now,
            )
            allocated.append(lease.id)
        return allocated

    def _create_request_in_session(
        self,
        session: Session,
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str,
        idempotency_action: str,
        activate_if_allocated: bool,
        profile_id: str | None = None,
        idempotency_checked: bool = False,
    ) -> dict[str, Any]:
        if not idempotency_checked:
            existing = self._idempotent(
                session, actor=actor, action=idempotency_action, key=idempotency_key
            )
            if existing is not None:
                return existing
        now = utcnow()
        project = self._ensure_claim_project(session, request_data.project_id, now)
        if not project.enabled:
            raise BrokerError("project_disabled", "project is disabled", status_code=409)
        revision = self._bump_revision(session, now)
        request = AllocationRequest(
            id=secrets.token_hex(16),
            actor_id=actor.id,
            project_id=request_data.project_id,
            profile_id=profile_id,
            auto_activate=activate_if_allocated,
            task_ref=request_data.task_ref,
            purpose=request_data.purpose,
            constraints_json=json_dump(request_data.constraints.model_dump(mode="json")),
            duration_seconds=request_data.duration_seconds,
            expected_duration_seconds=None,
            start_after=ensure_utc(request_data.start_after) if request_data.start_after else None,
            deadline=ensure_utc(request_data.deadline) if request_data.deadline else None,
            approval_ref=request_data.approval_ref,
            state="QUEUED",
            priority_class="normal",
            blocked_reason=None,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        summary = {"project_id": request.project_id, "task_ref": request.task_ref}
        if profile_id is not None:
            summary["profile_id"] = profile_id
        event = self._audit(
            session,
            actor_id=actor.id,
            action="request.created",
            resource_type="request",
            resource_id=request.id,
            result="success",
            after=self._request_dict(request),
            summary=summary,
            now=now,
        )
        session.flush()
        self._allocate_queued(session, now, revision)
        lease = session.scalar(select(Lease).where(Lease.request_id == request.id))
        if lease is not None and activate_if_allocated and lease.state == "HELD":
            before_lease = self._lease_dict(session, lease)
            lease.state = "ACTIVE"
            lease.activated_at = now
            lease.last_heartbeat_at = now
            request.state = "ACTIVE"
            request.updated_at = now
            activation_summary = {"activation": "immediate_claim"}
            if profile_id is not None:
                activation_summary["profile_id"] = profile_id
            self._audit(
                session,
                actor_id=actor.id,
                action="lease.activated",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before_lease,
                after=self._lease_dict(session, lease),
                summary=activation_summary,
                now=now,
            )
        result = {
            "event_id": event.id,
            "snapshot_revision": revision,
            "request": self._request_dict(request),
            "lease": self._lease_dict(session, lease) if lease else None,
            "authority": "GPU lease only; workload launch still requires the applicable project/owner authorization.",
        }
        self._remember_idempotency(
            session,
            actor=actor,
            action=idempotency_action,
            key=idempotency_key,
            response=result,
            now=now,
        )
        return result

    def create_request(
        self,
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str,
        activate_if_allocated: bool = False,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            return self._create_request_in_session(
                session,
                actor,
                request_data,
                idempotency_key=idempotency_key,
                idempotency_action="request.create",
                activate_if_allocated=activate_if_allocated,
            )

        return self._write(operation)

    def cancel_request(
        self, actor: ActorContext, request_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="request.cancel", key=idempotency_key
            )
            if existing is not None:
                return existing
            request = session.get(AllocationRequest, request_id)
            if request is None:
                raise BrokerError("request_not_found", "request does not exist", status_code=404)
            if not actor.is_admin and actor.role != "operator" and request.actor_id != actor.id:
                raise BrokerError("request_forbidden", "cannot cancel another actor's request", status_code=403)
            if request.state not in {"QUEUED", "PENDING_APPROVAL"}:
                raise BrokerError(
                    "request_not_cancellable",
                    f"request in state {request.state} cannot be cancelled",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._request_dict(request)
            request.state = "CANCELLED"
            request.blocked_reason = "cancelled by actor"
            request.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="request.cancelled",
                resource_type="request",
                resource_id=request.id,
                result="success",
                before=before,
                after=self._request_dict(request),
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "request": self._request_dict(request),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="request.cancel",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def activate_lease(
        self, actor: ActorContext, lease_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="lease.activate", key=idempotency_key
            )
            if existing is not None:
                return existing
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            if not self._can_manage_lease(actor, lease):
                raise BrokerError("lease_forbidden", "cannot activate another actor's lease", status_code=403)
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_activatable",
                    f"lease in state {lease.state} cannot be activated",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "ACTIVE"
            lease.activated_at = lease.activated_at or now
            lease.last_heartbeat_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "ACTIVE"
                request.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.activated",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "authority": "Activation records lease use; it does not launch a workload.",
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.activate",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def renew_lease(
        self, actor: ActorContext, lease_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="lease.renew", key=idempotency_key)
            if existing is not None:
                return existing
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            if not self._can_manage_lease(actor, lease):
                raise BrokerError("lease_forbidden", "cannot renew another actor's lease", status_code=403)
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_renewable",
                    f"lease in state {lease.state} cannot be renewed",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            issued_at = _as_utc(lease.issued_at) or now
            expires_at = _as_utc(lease.expires_at) or now
            duration = max(60, int((expires_at - issued_at).total_seconds()))
            renewed_expiry = max(expires_at, now) + timedelta(seconds=duration)
            reserved_gpu_ids = [
                resource.gpu_id
                for resource in session.scalars(
                    select(LeaseResource).where(
                        LeaseResource.lease_id == lease.id,
                        LeaseResource.active.is_(True),
                    )
                ).all()
                if self._reservation_blocks_gpu(session, resource.gpu_id, start=now, end=renewed_expiry)
            ]
            if reserved_gpu_ids:
                raise BrokerError(
                    "lease_renewal_conflicts_with_reservation",
                    "lease renewal would overlap a future GPU reservation",
                    status_code=409,
                    details={"gpu_ids": reserved_gpu_ids},
                )
            lease.expires_at = renewed_expiry
            lease.last_heartbeat_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.renewed",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.renew",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def release_lease(
        self,
        actor: ActorContext,
        lease_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)
        if not reason.strip():
            raise BrokerError("release_reason_required", "a release reason is required", status_code=422)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="lease.release", key=idempotency_key)
            if existing is not None:
                return existing
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            if not self._can_manage_lease(actor, lease):
                raise BrokerError("lease_forbidden", "cannot release another actor's lease", status_code=403)
            if lease.state in TERMINAL_LEASE_STATES:
                raise BrokerError("lease_already_released", "lease is already terminal", status_code=409)
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = reason.strip()[:500]
            for resource in session.scalars(
                select(LeaseResource).where(LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True))
            ).all():
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.released",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"reason": lease.release_reason},
                now=now,
            )
            # A retained/unknown process still blocks eligibility after this release.
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.release",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def bind_workload(
        self,
        actor: ActorContext,
        lease_id: str,
        binding: LeaseBind,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="lease.bind", key=idempotency_key)
            if existing is not None:
                return existing
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            if not self._can_manage_lease(actor, lease):
                raise BrokerError("lease_forbidden", "cannot bind another actor's lease", status_code=403)
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError("lease_not_bindable", "only held or active leases can be bound", status_code=409)
            now = utcnow()
            revision = self._bump_revision(session, now)
            existing_binding = session.scalar(
                select(WorkloadBinding).where(
                    WorkloadBinding.lease_id == lease.id, WorkloadBinding.run_id == binding.run_id
                )
            )
            if existing_binding is None:
                existing_binding = WorkloadBinding(
                    lease_id=lease.id,
                    run_id=binding.run_id,
                    process_keys_json=json_dump(binding.process_keys),
                    created_at=now,
                )
                session.add(existing_binding)
            else:
                existing_binding.process_keys_json = json_dump(binding.process_keys)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": binding.run_id, "process_key_count": len(binding.process_keys)},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.bind",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def bind_observed_workload(
        self,
        actor: ActorContext,
        lease_id: str,
        binding: LeaseObservedBind,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bind fresh collector-observed processes for the caller's active lease.

        This never starts, stops, or selects a remote process. It only records
        the identities already observed on every GPU held by the specified
        lease, allowing the regular reconciliation loop to distinguish the
        caller's workload from an unmanaged process.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="lease.bind_observed", key=idempotency_key
            )
            if existing is not None:
                return existing
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            if not self._can_manage_lease(actor, lease):
                raise BrokerError("lease_forbidden", "cannot bind another actor's lease", status_code=403)
            if lease.state not in {"HELD", "ACTIVE", "CONFLICT"}:
                raise BrokerError(
                    "lease_not_bindable",
                    "only held, active, or attribution-conflicted leases can be bound",
                    status_code=409,
                )
            was_conflict = lease.state == "CONFLICT"
            conflict_before = self._lease_dict(session, lease) if was_conflict else None
            gpu_ids = session.scalars(
                select(LeaseResource.gpu_id).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            if not gpu_ids:
                raise BrokerError("lease_has_no_resources", "lease has no active GPU resources", status_code=409)
            now = utcnow()
            cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            processes = session.scalars(
                select(ProcessObservation)
                .where(
                    ProcessObservation.gpu_id.in_(gpu_ids),
                    ProcessObservation.active.is_(True),
                    ProcessObservation.last_seen_at >= cutoff,
                )
                .order_by(ProcessObservation.gpu_id, ProcessObservation.pid)
            ).all()
            observed_gpu_ids = {process.gpu_id for process in processes}
            missing_gpu_ids = sorted(set(gpu_ids).difference(observed_gpu_ids))
            if missing_gpu_ids:
                raise BrokerError(
                    "workload_process_not_observed",
                    "cannot bind observed workload until every leased GPU has a fresh compute process",
                    status_code=409,
                    details={"missing_gpu_ids": missing_gpu_ids},
                )
            process_keys = sorted({self._process_key(process) for process in processes})
            run_id = binding.run_id or f"lease:{lease.id}"
            revision = self._bump_revision(session, now)
            existing_binding = session.scalar(
                select(WorkloadBinding).where(
                    WorkloadBinding.lease_id == lease.id, WorkloadBinding.run_id == run_id
                )
            )
            if existing_binding is None:
                session.add(
                    WorkloadBinding(
                        lease_id=lease.id,
                        run_id=run_id,
                        process_keys_json=json_dump(process_keys),
                        created_at=now,
                    )
                )
            else:
                existing_binding.process_keys_json = json_dump(process_keys)
            if was_conflict:
                # A lease owner explicitly attesting the current, freshly
                # observed process identities is the safe recovery action for
                # an attribution conflict. It changes no remote workload;
                # the lease remains blocked by any future unknown process.
                lease.state = "ACTIVE" if lease.activated_at is not None else "HELD"
                for alert in session.scalars(
                    select(Alert).where(
                        Alert.alert_type == "lease_process_conflict",
                        Alert.resource_type == "lease",
                        Alert.resource_id == lease.id,
                        Alert.active.is_(True),
                    )
                ).all():
                    alert.active = False
                    alert.last_seen_at = now
                self._audit(
                    session,
                    actor_id=actor.id,
                    action="lease.conflict_resolved",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=conflict_before,
                    after=self._lease_dict(session, lease),
                    summary={"source": "collector_observed", "run_id": run_id},
                    now=now,
                )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": run_id, "process_key_count": len(process_keys)},
                summary={
                    "source": "collector_observed",
                    "gpu_count": len(gpu_ids),
                    "resolved_conflict": was_conflict,
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "conflict_resolved": was_conflict,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.bind_observed",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def _reconcile_leases(self, session: Session, now: datetime, *, actor_id: str) -> None:
        """Fail closed on expiry and unexpected compute processes; never kill/restart anything."""

        leases = session.scalars(select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))).all()
        expired_released = False
        for lease in leases:
            resources = session.scalars(
                select(LeaseResource).where(LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True))
            ).all()
            processes = [
                process
                for resource in resources
                for process in self._current_processes(session, resource.gpu_id, now)
            ]
            expires_at = _as_utc(lease.expires_at) or now
            if lease.state in {"HELD", "ACTIVE"} and expires_at <= now:
                before = self._lease_dict(session, lease)
                if processes:
                    lease.state = "ORPHANED_BUSY"
                    self._upsert_alert(
                        session,
                        alert_type="orphaned_busy",
                        severity="critical",
                        resource_type="lease",
                        resource_id=lease.id,
                        message="lease expired but a real compute process is still observed; resource remains blocked",
                        now=now,
                    )
                else:
                    lease.state = "EXPIRED_EMPTY"
                    lease.released_at = now
                    lease.release_reason = "expired without observed process"
                    for resource in resources:
                        resource.active = False
                        resource.released_at = now
                    request = session.get(AllocationRequest, lease.request_id)
                    if request is not None:
                        request.state = "EXPIRED"
                        request.updated_at = now
                    expired_released = True
                self._audit(
                    session,
                    actor_id=actor_id,
                    action="lease.expiry_reconciled",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=before,
                    after=self._lease_dict(session, lease),
                    now=now,
                )
            elif lease.state in {"HELD", "ACTIVE"} and processes:
                known_process_keys = self._binding_process_keys(session, lease.id)
                if known_process_keys and not all(
                    self._process_key(process) in known_process_keys for process in processes
                ) and any(
                    process.observations >= 2 for process in processes
                ):
                    before = self._lease_dict(session, lease)
                    lease.state = "CONFLICT"
                    self._upsert_alert(
                        session,
                        alert_type="lease_process_conflict",
                        severity="critical",
                        resource_type="lease",
                        resource_id=lease.id,
                        message="observed compute process does not match workload binding; new allocation is blocked",
                        now=now,
                    )
                    self._audit(
                        session,
                        actor_id=actor_id,
                        action="lease.conflict_detected",
                        resource_type="lease",
                        resource_id=lease.id,
                        result="success",
                        before=before,
                        after=self._lease_dict(session, lease),
                        now=now,
                    )
        if expired_released:
            self._allocate_queued(session, now, self._revision(session))

    def reconcile(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            revision = self._bump_revision(session, now)
            self._reconcile_leases(session, now, actor_id=actor.id)
            self._allocate_queued(session, now, revision)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reconciliation.run",
                resource_type="cluster",
                resource_id="global",
                result="success",
                summary={"revision": revision},
                now=now,
            )
            return {"event_id": event.id, "snapshot_revision": revision}

        return self._write(operation)

    # ---- reservation, maintenance, alert and administration -------------------

    def _reservation_candidate_gpus(
        self,
        session: Session,
        *,
        constraints: ResourceConstraints,
        start_at: datetime,
        end_at: datetime,
    ) -> list[str] | None:
        """Resolve a constraint reservation to stable GPU identities at creation time."""

        candidates: list[GPUDevice] = []
        for gpu in session.scalars(select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)).all():
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None or not endpoint.enabled or not gpu.enabled:
                continue
            if constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                continue
            if gpu.id in constraints.deny_gpu_ids:
                continue
            if constraints.min_total_vram_mib and gpu.total_vram_mib < constraints.min_total_vram_mib:
                continue
            if not set(constraints.endpoint_labels).issubset(set(json_load(endpoint.labels_json))):
                continue
            if not set(constraints.gpu_labels).issubset(set(json_load(gpu.labels_json))):
                continue
            if self._reservation_blocks_gpu(session, gpu.id, start=start_at, end=end_at):
                continue
            candidates.append(gpu)
        selected = self._select_resources(candidates, constraints)
        return [gpu.id for gpu in selected] if selected else None

    def create_reservation(
        self,
        actor: ActorContext,
        reservation_data: ReservationCreate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="reservation.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            project = self._ensure_claim_project(session, reservation_data.project_id, now)
            start_at = ensure_utc(reservation_data.start_at)
            end_at = ensure_utc(reservation_data.end_at)
            gpu_ids = list(reservation_data.gpu_ids)
            if not gpu_ids:
                assert reservation_data.constraints is not None
                selected = self._reservation_candidate_gpus(
                    session,
                    constraints=reservation_data.constraints,
                    start_at=start_at,
                    end_at=end_at,
                )
                if selected is None:
                    raise BrokerError(
                        "reservation_capacity_unavailable",
                        "no complete stable GPU set satisfies the future reservation constraints",
                        status_code=409,
                    )
                gpu_ids = selected
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None:
                    raise BrokerError("gpu_not_found", f"GPU {gpu_id} does not exist", status_code=404)
                if self._reservation_blocks_gpu(session, gpu_id, start=start_at, end=end_at):
                    raise BrokerError(
                        "reservation_conflict",
                        f"GPU {gpu_id} overlaps an existing reservation",
                        status_code=409,
                    )
                leases = session.scalars(
                    select(Lease)
                    .join(LeaseResource, LeaseResource.lease_id == Lease.id)
                    .where(
                        LeaseResource.gpu_id == gpu_id,
                        LeaseResource.active.is_(True),
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                    )
                ).all()
                if any((_as_utc(lease.expires_at) or now) > start_at for lease in leases):
                    raise BrokerError(
                        "reservation_active_lease_conflict",
                        f"GPU {gpu_id} has an active lease that overlaps the reservation start",
                        status_code=409,
                    )
            revision = self._bump_revision(session, now)
            reservation = Reservation(
                id=secrets.token_hex(16),
                actor_id=actor.id,
                project_id=project.id,
                gpu_ids_json=json_dump(gpu_ids),
                constraints_json=json_dump(
                    reservation_data.constraints.model_dump(mode="json")
                    if reservation_data.constraints
                    else {}
                ),
                start_at=start_at,
                end_at=end_at,
                reason=reservation_data.reason,
                state="ACTIVE",
                created_at=now,
            )
            session.add(reservation)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reservation.created",
                resource_type="reservation",
                resource_id=reservation.id,
                result="success",
                after=self._reservation_dict(reservation),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "reservation": self._reservation_dict(reservation),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="reservation.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def cancel_reservation(
        self, actor: ActorContext, reservation_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="reservation.cancel", key=idempotency_key
            )
            if existing is not None:
                return existing
            reservation = session.get(Reservation, reservation_id)
            if reservation is None:
                raise BrokerError("reservation_not_found", "reservation does not exist", status_code=404)
            if reservation.state != "ACTIVE":
                raise BrokerError("reservation_not_cancellable", "reservation is not active", status_code=409)
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._reservation_dict(reservation)
            reservation.state = "CANCELLED"
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reservation.cancelled",
                resource_type="reservation",
                resource_id=reservation.id,
                result="success",
                before=before,
                after=self._reservation_dict(reservation),
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "reservation": self._reservation_dict(reservation),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="reservation.cancel",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def create_maintenance(
        self,
        actor: ActorContext,
        maintenance: MaintenanceCreate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="maintenance.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            endpoint_id = maintenance.endpoint_id
            gpu_id = maintenance.gpu_id
            if endpoint_id and session.get(Endpoint, endpoint_id) is None:
                raise BrokerError("endpoint_not_found", "maintenance endpoint does not exist", status_code=404)
            if gpu_id:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None:
                    raise BrokerError("gpu_not_found", "maintenance GPU does not exist", status_code=404)
                endpoint_id = None
            revision = self._bump_revision(session, now)
            window = MaintenanceWindow(
                id=secrets.token_hex(16),
                endpoint_id=endpoint_id,
                gpu_id=gpu_id,
                actor_id=actor.id,
                start_at=ensure_utc(maintenance.start_at),
                end_at=ensure_utc(maintenance.end_at),
                reason=maintenance.reason,
                state="ACTIVE",
                created_at=now,
            )
            session.add(window)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="maintenance.created",
                resource_type="maintenance",
                resource_id=window.id,
                result="success",
                after=self._maintenance_dict(window),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "maintenance": self._maintenance_dict(window),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="maintenance.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def acknowledge_alert(
        self,
        actor: ActorContext,
        alert_id: str,
        acknowledgement: AlertAcknowledge,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="alert.ack", key=idempotency_key)
            if existing is not None:
                return existing
            alert = session.get(Alert, alert_id)
            if alert is None:
                raise BrokerError("alert_not_found", "alert does not exist", status_code=404)
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._alert_dict(alert)
            alert.acknowledged_at = now
            alert.acknowledged_by = actor.id
            event = self._audit(
                session,
                actor_id=actor.id,
                action="alert.acknowledged",
                resource_type="alert",
                resource_id=alert.id,
                result="success",
                before=before,
                after=self._alert_dict(alert),
                summary={"note": acknowledgement.note} if acknowledgement.note else {},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "alert": self._alert_dict(alert),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="alert.ack",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def prune_telemetry(
        self,
        actor: ActorContext,
        retention: RetentionPrune,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Explicit retention action; current telemetry, audit and leases are never deleted."""

        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="telemetry.prune", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            cutoff = now - timedelta(seconds=retention.older_than_seconds)
            revision = self._bump_revision(session, now)
            deleted = session.execute(
                delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
            ).rowcount or 0
            event = self._audit(
                session,
                actor_id=actor.id,
                action="telemetry.pruned",
                resource_type="telemetry",
                resource_id="history",
                result="success",
                after={"deleted_count": deleted, "cutoff": _iso(cutoff)},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "deleted_count": deleted,
                "cutoff": _iso(cutoff),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="telemetry.prune",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def _validate_workload_profile_endpoints(
        self,
        session: Session,
        *,
        constraints: ResourceConstraints,
    ) -> None:
        missing = [
            endpoint_id for endpoint_id in constraints.endpoint_ids if session.get(Endpoint, endpoint_id) is None
        ]
        if missing:
            raise BrokerError(
                "endpoint_not_found",
                f"workload profile references unknown endpoints: {missing}",
                status_code=404,
            )

    def upsert_workload_profile(
        self,
        actor: ActorContext,
        profile_data: WorkloadProfileUpsert,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Store an admin-approved routine workload contract for one project."""

        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="workload_profile.upsert", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            self._ensure_claim_project(session, profile_data.project_id, now)
            self._validate_workload_profile_endpoints(
                session,
                constraints=profile_data.constraints,
            )
            revision = self._bump_revision(session, now)
            profile = session.get(WorkloadProfile, profile_data.id)
            before = self._workload_profile_dict(profile) if profile else None
            if profile is None:
                profile = WorkloadProfile(
                    id=profile_data.id,
                    project_id=profile_data.project_id,
                    display_name=profile_data.display_name,
                    purpose=profile_data.purpose,
                    duration_seconds=profile_data.duration_seconds,
                    constraints_json=json_dump(profile_data.constraints.model_dump(mode="json")),
                    enabled=profile_data.enabled,
                    created_at=now,
                    updated_at=now,
                )
                session.add(profile)
            else:
                if profile.project_id != profile_data.project_id:
                    raise BrokerError(
                        "workload_profile_project_immutable",
                        "existing workload profile cannot move to another project",
                        status_code=409,
                    )
                profile.display_name = profile_data.display_name
                profile.purpose = profile_data.purpose
                profile.duration_seconds = profile_data.duration_seconds
                profile.constraints_json = json_dump(profile_data.constraints.model_dump(mode="json"))
                profile.enabled = profile_data.enabled
                profile.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="workload_profile.upserted",
                resource_type="workload_profile",
                resource_id=profile.id,
                result="success",
                before=before,
                after=self._workload_profile_dict(profile),
                summary={"project_id": profile.project_id},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "workload_profile": self._workload_profile_dict(profile),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="workload_profile.upsert",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def claim_workload_profile(
        self,
        actor: ActorContext,
        profile_id: str,
        claim: WorkloadProfileClaim,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Claim a pre-approved contract without re-supplying its resource fields."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="workload_profile.claim", key=idempotency_key
            )
            if existing is not None:
                return existing
            profile = session.get(WorkloadProfile, profile_id)
            if profile is None:
                raise BrokerError("workload_profile_not_found", "workload profile does not exist", status_code=404)
            if not profile.enabled:
                raise BrokerError(
                    "workload_profile_disabled", "workload profile is disabled", status_code=409
                )
            request_data = RequestCreate.model_validate(
                {
                    "project_id": profile.project_id,
                    "task_ref": claim.task_ref,
                    "purpose": profile.purpose,
                    "duration_seconds": profile.duration_seconds,
                    "constraints": json_load(profile.constraints_json),
                }
            )
            return self._create_request_in_session(
                session,
                actor,
                request_data,
                idempotency_key=idempotency_key,
                idempotency_action="workload_profile.claim",
                activate_if_allocated=True,
                profile_id=profile.id,
                idempotency_checked=True,
            )

        return self._write(operation)

    @staticmethod
    def _constraints_reference_endpoint(
        constraints_json: str,
        *,
        endpoint_id: str,
        gpu_ids: set[str],
    ) -> bool:
        constraints = json_load(constraints_json)
        if not isinstance(constraints, dict):
            return False
        endpoint_ids = constraints.get("endpoint_ids") or []
        allowed_gpu_ids = constraints.get("gpu_ids") or []
        return endpoint_id in endpoint_ids or bool(gpu_ids.intersection(allowed_gpu_ids))

    def upsert_endpoint(
        self, actor: ActorContext, endpoint_data: EndpointUpsert, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Create/update inventory metadata while keeping endpoint id and host:port immutable."""

        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.upsert", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            revision = self._bump_revision(session, now)
            endpoint = session.get(Endpoint, endpoint_data.id)
            before = self._endpoint_dict(endpoint) if endpoint else None
            same_address = session.scalar(
                select(Endpoint).where(
                    Endpoint.host == endpoint_data.host,
                    Endpoint.port == endpoint_data.port,
                )
            )
            if same_address is not None and same_address.id != endpoint_data.id:
                raise BrokerError(
                    "endpoint_address_exists",
                    "an immutable endpoint already owns this host:port",
                    status_code=409,
                )
            if endpoint is None:
                endpoint = Endpoint(
                    id=endpoint_data.id,
                    host=endpoint_data.host,
                    port=endpoint_data.port,
                    ssh_user=endpoint_data.ssh_user,
                    ssh_alias=endpoint_data.ssh_alias,
                    labels_json=json_dump(endpoint_data.labels),
                    storage_group=endpoint_data.storage_group,
                    expected_gpu_count=endpoint_data.expected_gpu_count,
                    expected_gpu_total_vram_mib=endpoint_data.expected_gpu_total_vram_mib,
                    enabled=endpoint_data.enabled,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
            else:
                if (endpoint.host, endpoint.port) != (endpoint_data.host, endpoint_data.port):
                    raise BrokerError(
                        "endpoint_identity_immutable",
                        "existing endpoint id cannot change host:port; create a new endpoint id",
                        status_code=409,
                    )
                endpoint.ssh_user = endpoint_data.ssh_user
                endpoint.ssh_alias = endpoint_data.ssh_alias
                endpoint.labels_json = json_dump(endpoint_data.labels)
                endpoint.storage_group = endpoint_data.storage_group
                endpoint.expected_gpu_count = endpoint_data.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = endpoint_data.expected_gpu_total_vram_mib
                endpoint.enabled = endpoint_data.enabled
                endpoint.updated_at = now
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.upserted",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                before=before,
                after=self._endpoint_dict(endpoint),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint": self._endpoint_dict(endpoint),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.upsert",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def delete_endpoint(
        self,
        actor: ActorContext,
        endpoint_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Remove an endpoint and current observations without touching remote workloads."""

        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.delete", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            now = utcnow()
            gpu_ids = set(
                session.scalars(
                    select(GPUDevice.id).where(GPUDevice.endpoint_id == endpoint.id)
                ).all()
            )
            active_lease_count = (
                session.scalar(
                    select(func.count())
                    .select_from(LeaseResource)
                    .join(Lease, Lease.id == LeaseResource.lease_id)
                    .where(
                        LeaseResource.gpu_id.in_(gpu_ids),
                        LeaseResource.active.is_(True),
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                    )
                )
                if gpu_ids
                else 0
            )
            if active_lease_count:
                raise BrokerError(
                    "endpoint_has_active_leases",
                    "endpoint has active leases; release or reconcile them before deleting the server",
                    status_code=409,
                    details={"active_lease_count": active_lease_count},
                )
            lease_history_count = (
                session.scalar(
                    select(func.count())
                    .select_from(LeaseResource)
                    .where(LeaseResource.gpu_id.in_(gpu_ids))
                )
                if gpu_ids
                else 0
            )
            if lease_history_count:
                raise BrokerError(
                    "endpoint_has_lease_history",
                    "endpoint has lease history; disable it instead to preserve historical lease records",
                    status_code=409,
                    details={"lease_resource_count": lease_history_count},
                )
            blocking_requests = [
                request.id
                for request in session.scalars(
                    select(AllocationRequest)
                    .where(AllocationRequest.state.in_({"QUEUED", "PENDING_APPROVAL"}))
                    .order_by(AllocationRequest.created_at)
                ).all()
                if self._constraints_reference_endpoint(
                    request.constraints_json,
                    endpoint_id=endpoint.id,
                    gpu_ids=gpu_ids,
                )
            ]
            if blocking_requests:
                raise BrokerError(
                    "endpoint_referenced_by_requests",
                    "endpoint is referenced by queued requests; cancel or edit those requests before deleting",
                    status_code=409,
                    details={"request_ids": blocking_requests[:20]},
                )
            blocking_profiles = [
                profile.id
                for profile in session.scalars(
                    select(WorkloadProfile)
                    .where(WorkloadProfile.enabled.is_(True))
                    .order_by(WorkloadProfile.id)
                ).all()
                if self._constraints_reference_endpoint(
                    profile.constraints_json,
                    endpoint_id=endpoint.id,
                    gpu_ids=gpu_ids,
                )
            ]
            if blocking_profiles:
                raise BrokerError(
                    "endpoint_referenced_by_profiles",
                    "endpoint is referenced by enabled workload profiles; update or disable them before deleting",
                    status_code=409,
                    details={"profile_ids": blocking_profiles[:20]},
                )
            blocking_reservations = [
                reservation.id
                for reservation in session.scalars(
                    select(Reservation)
                    .where(Reservation.state == "ACTIVE", Reservation.end_at > now)
                    .order_by(Reservation.start_at)
                ).all()
                if gpu_ids.intersection(json_load(reservation.gpu_ids_json))
                or self._constraints_reference_endpoint(
                    reservation.constraints_json,
                    endpoint_id=endpoint.id,
                    gpu_ids=gpu_ids,
                )
            ]
            if blocking_reservations:
                raise BrokerError(
                    "endpoint_referenced_by_reservations",
                    "endpoint is referenced by active or future reservations; cancel them before deleting",
                    status_code=409,
                    details={"reservation_ids": blocking_reservations[:20]},
                )

            revision = self._bump_revision(session, now)
            before = self._endpoint_dict(endpoint)
            alert_filters = [(Alert.resource_type == "endpoint") & (Alert.resource_id == endpoint.id)]
            if gpu_ids:
                alert_filters.append((Alert.resource_type == "gpu") & Alert.resource_id.in_(gpu_ids))
            session.execute(delete(Alert).where(or_(*alert_filters)))
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.deleted",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                before=before,
                after={"deleted": True, "gpu_ids": sorted(gpu_ids)},
                summary={"gpu_count": len(gpu_ids)},
                now=now,
            )
            session.delete(endpoint)
            try:
                session.flush()
            except IntegrityError as exc:
                raise BrokerError(
                    "endpoint_delete_restricted",
                    "endpoint is still referenced by protected history; disable it instead",
                    status_code=409,
                ) from exc
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint_id": endpoint_id,
                "deleted_gpu_count": len(gpu_ids),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.delete",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def set_endpoint_enabled(
        self,
        actor: ActorContext,
        endpoint_id: str,
        state: EndpointEnabled,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.enabled", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._endpoint_dict(endpoint)
            endpoint.enabled = state.enabled
            endpoint.updated_at = now
            if not state.enabled:
                active = session.scalar(
                    select(func.count())
                    .select_from(LeaseResource)
                    .join(Lease, Lease.id == LeaseResource.lease_id)
                    .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
                    .where(
                        GPUDevice.endpoint_id == endpoint.id,
                        LeaseResource.active.is_(True),
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                    )
                )
                if active:
                    self._upsert_alert(
                        session,
                        alert_type="disabled_endpoint_has_lease",
                        severity="warning",
                        resource_type="endpoint",
                        resource_id=endpoint.id,
                        message="endpoint was disabled while active leases remain; no process was stopped",
                        now=now,
                    )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.enabled_changed",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                before=before,
                after=self._endpoint_dict(endpoint),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint": self._endpoint_dict(endpoint),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.enabled",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def create_actor(
        self, actor: ActorContext, actor_data: ActorCreate, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="actor.create", key=idempotency_key)
            if existing is not None:
                return existing
            if session.get(Actor, actor_data.id) is not None:
                raise BrokerError("actor_exists", "actor id already exists", status_code=409)
            unknown_projects = [
                project_id for project_id in actor_data.project_ids if session.get(Project, project_id) is None
            ]
            if unknown_projects:
                raise BrokerError(
                    "project_not_found",
                    f"actor references unknown projects: {unknown_projects}",
                    status_code=404,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            created = Actor(
                id=actor_data.id,
                display_name=actor_data.display_name,
                role=actor_data.role,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(created)
            for project_id in actor_data.project_ids:
                session.add(ActorProject(actor_id=created.id, project_id=project_id))
            raw_token = secrets.token_urlsafe(32)
            token = ApiToken(
                id=secrets.token_hex(16),
                actor_id=created.id,
                label=actor_data.token_label,
                token_hash=token_hash(raw_token),
                created_at=now,
                expires_at=None,
                revoked_at=None,
                last_used_at=None,
            )
            session.add(token)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="actor.created",
                resource_type="actor",
                resource_id=created.id,
                result="success",
                after=self._actor_dict(created, actor_data.project_ids),
                summary={"role": created.role, "token_label": token.label},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "actor": self._actor_dict(created, actor_data.project_ids),
                # Display exactly once in an authenticated response. Never persist it in an audit/event/DB.
                "token": raw_token,
            }
            # An idempotent retry deliberately does not re-show the secret; operator must rotate instead.
            self._remember_idempotency(
                session,
                actor=actor,
                action="actor.create",
                key=idempotency_key,
                response={**result, "token": "REDACTED_AFTER_FIRST_RESPONSE"},
                now=now,
            )
            return result

        return self._write(operation)

    def revoke_token(
        self, actor: ActorContext, token_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(session, actor=actor, action="token.revoke", key=idempotency_key)
            if existing is not None:
                return existing
            token = session.get(ApiToken, token_id)
            if token is None:
                raise BrokerError("token_not_found", "token does not exist", status_code=404)
            now = utcnow()
            revision = self._bump_revision(session, now)
            token.revoked_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="token.revoked",
                resource_type="token",
                resource_id=token.id,
                result="success",
                summary={"actor_id": token.actor_id, "label": token.label},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "token_id": token.id,
                "revoked_at": _iso(token.revoked_at),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="token.revoke",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    # ---- filtered read surfaces ------------------------------------------------

    def list_requests(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            requests = session.scalars(
                select(AllocationRequest).order_by(AllocationRequest.created_at.desc())
            ).all()
            visible = [self._request_dict(request) for request in requests]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_leases(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            leases = session.scalars(select(Lease).order_by(Lease.issued_at.desc())).all()
            visible = [self._lease_dict(session, lease) for lease in leases]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_processes(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            values = []
            for process in session.scalars(
                select(ProcessObservation)
                .where(ProcessObservation.active.is_(True))
                .order_by(ProcessObservation.last_seen_at.desc())
            ).all():
                values.append(
                    {
                        "id": process.id,
                        "endpoint_id": process.endpoint_id,
                        "gpu_id": process.gpu_id,
                        "pid": process.pid,
                        "boot_id": process.boot_id,
                        "process_started_at": _iso(process.process_started_at),
                        "process_key": self._process_key(process),
                        "username": process.username,
                        "executable": process.executable,
                        "used_memory_mib": process.used_memory_mib,
                        "observations": process.observations,
                        "first_seen_at": _iso(process.first_seen_at),
                        "last_seen_at": _iso(process.last_seen_at),
                        "fresh": (_as_utc(process.last_seen_at) or now)
                        >= now - timedelta(seconds=self.inventory.collector.stale_after_seconds),
                    }
                )
            return self.envelope(session, values)

        return self._read(operation)

    def list_reservations(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            values = [
                self._reservation_dict(reservation)
                for reservation in session.scalars(
                    select(Reservation).order_by(Reservation.start_at, Reservation.id)
                ).all()
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def list_maintenance(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            values = [
                self._maintenance_dict(window)
                for window in session.scalars(
                    select(MaintenanceWindow).order_by(MaintenanceWindow.start_at, MaintenanceWindow.id)
                ).all()
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def list_alerts(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            visible = [
                self._alert_dict(alert)
                for alert in session.scalars(
                    select(Alert).order_by(Alert.active.desc(), Alert.last_seen_at.desc())
                ).all()
            ]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_events(
        self, actor: ActorContext, *, after_id: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise BrokerError("invalid_limit", "limit must be between 1 and 1000", status_code=422)

        def operation(session: Session) -> dict[str, Any]:
            events = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.id > after_id)
                .order_by(AuditEvent.id)
                .limit(limit)
            ).all()
            values = []
            for event in events:
                # Non-admins see their own event stream plus events for their project leases/requests.
                visible = actor.is_admin or event.actor_id == actor.id
                if not visible and event.resource_type == "lease":
                    lease = session.get(Lease, event.resource_id)
                    visible = lease is not None and lease.project_id in actor.project_ids
                if not visible and event.resource_type == "request":
                    request = session.get(AllocationRequest, event.resource_id)
                    visible = request is not None and request.project_id in actor.project_ids
                if not visible and event.resource_type == "workload_profile":
                    profile = session.get(WorkloadProfile, event.resource_id)
                    visible = profile is not None and profile.project_id in actor.project_ids
                if not visible:
                    continue
                values.append(
                    {
                        "id": event.id,
                        "actor_id": event.actor_id,
                        "action": event.action,
                        "resource_type": event.resource_type,
                        "resource_id": event.resource_id,
                        "result": event.result,
                        "before_hash": event.before_hash,
                        "after_hash": event.after_hash,
                        "summary": json_load(event.summary_json),
                        "created_at": _iso(event.created_at),
                    }
                )
            return self.envelope(session, values)

        return self._read(operation)

    def list_projects(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            values = [
                self._project_dict(project)
                for project in session.scalars(select(Project).order_by(Project.id)).all()
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def list_workload_profiles(
        self, actor: ActorContext, *, project_id: str | None = None
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            profiles = session.scalars(
                select(WorkloadProfile).order_by(WorkloadProfile.project_id, WorkloadProfile.id)
            ).all()
            values = [
                self._workload_profile_dict(profile)
                for profile in profiles
                if (project_id is None or profile.project_id == project_id)
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def list_actors(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            values = []
            for item in session.scalars(select(Actor).order_by(Actor.id)).all():
                project_ids = session.scalars(
                    select(ActorProject.project_id).where(ActorProject.actor_id == item.id)
                ).all()
                tokens = session.scalars(
                    select(ApiToken).where(ApiToken.actor_id == item.id).order_by(ApiToken.created_at.desc())
                ).all()
                values.append(
                    {
                        **self._actor_dict(item, project_ids),
                        "tokens": [
                            {
                                "id": token.id,
                                "label": token.label,
                                "created_at": _iso(token.created_at),
                                "expires_at": _iso(token.expires_at),
                                "revoked_at": _iso(token.revoked_at),
                                "last_used_at": _iso(token.last_used_at),
                            }
                            for token in tokens
                        ],
                    }
                )
            return self.envelope(session, values)

        return self._read(operation)

    def effective_config(self, actor: ActorContext) -> dict[str, Any]:
        # Inventory carries no secrets; this safe view intentionally excludes runtime env and token hashes.
        self._require_role(actor, {"viewer", "allocator", "operator", "admin"})

        def operation(session: Session) -> dict[str, Any]:
            endpoints = []
            for endpoint in session.scalars(select(Endpoint).order_by(Endpoint.id)).all():
                endpoints.append(self._endpoint_dict(endpoint))
            return self.envelope(
                session,
                {
                    "bootstrap_inventory": self.inventory.model_dump(mode="json"),
                    "database_inventory": {"endpoints": endpoints},
                    "scheduler": {
                        "exclusive_lease": True,
                        "auto_preemption": False,
                        "backfill_default": False,
                        "stale_after_seconds": self.inventory.collector.stale_after_seconds,
                    },
                    "runtime": {"backend": "sqlite-wal", "single_writer": True},
                },
            )

        return self._read(operation)

    def doctor(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, {"viewer", "allocator", "operator", "admin"})

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            gpus = session.scalars(select(GPUDevice)).all()
            stale = sum(
                1
                for gpu in gpus
                if self._gpu_state(session, gpu, now)[0] in {"UNKNOWN_STALE", "UNKNOWN_RECOVERING"}
            )
            provider_states = session.scalars(select(ProviderState).order_by(ProviderState.endpoint_id)).all()
            return self.envelope(
                session,
                {
                    "database_ready": self.database.ready(),
                    "snapshot_revision": self._revision(session),
                    "inventory_endpoints": session.scalar(select(func.count()).select_from(Endpoint)),
                    "discovered_gpus": len(gpus),
                    "stale_or_recovering_gpus": stale,
                    "collector_enabled": self.inventory.collector.enabled,
                    "providers": [
                        {
                            "provider": state.provider,
                            "endpoint_id": state.endpoint_id,
                            "last_success_at": _iso(state.last_success_at),
                            "last_attempt_at": _iso(state.last_attempt_at),
                            "has_error": state.last_error is not None,
                            "revision": state.revision,
                        }
                        for state in provider_states
                    ],
                },
            )

        return self._read(operation)

    def metrics(self) -> str:
        """Small Prometheus-compatible exposition without exposing secrets or task purposes."""

        def operation(session: Session) -> str:
            now = utcnow()
            gpus = session.scalars(select(GPUDevice)).all()
            states: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                states[self._gpu_state(session, gpu, now)[0]] += 1
            active_leases = session.scalar(
                select(func.count()).select_from(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))
            )
            queued = session.scalar(
                select(func.count()).select_from(AllocationRequest).where(AllocationRequest.state == "QUEUED")
            )
            lines = [
                "# HELP gpu_broker_gpus Number of GPUs by derived state",
                "# TYPE gpu_broker_gpus gauge",
            ]
            lines.extend(
                f'gpu_broker_gpus{{state="{state}"}} {count}' for state, count in sorted(states.items())
            )
            lines.extend(
                [
                    "# HELP gpu_broker_active_leases Number of active exclusive leases",
                    "# TYPE gpu_broker_active_leases gauge",
                    f"gpu_broker_active_leases {active_leases or 0}",
                    "# HELP gpu_broker_queued_requests Number of queued allocation requests",
                    "# TYPE gpu_broker_queued_requests gauge",
                    f"gpu_broker_queued_requests {queued or 0}",
                    "# HELP gpu_broker_snapshot_revision Monotonic control-plane revision",
                    "# TYPE gpu_broker_snapshot_revision gauge",
                    f"gpu_broker_snapshot_revision {self._revision(session)}",
                    "",
                ]
            )
            return "\n".join(lines)

        return self._read(operation)

    def backup(self, actor: ActorContext, destination: str) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)
        path = self.database.backup(destination=Path(destination))
        return {"path": str(path), "created_at": _iso(utcnow())}
