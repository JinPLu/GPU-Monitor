"""Single-owner domain service for inventory, telemetry, leases and audit events."""

from __future__ import annotations

import re
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from serverpilot import SCHEMA_VERSION
from serverpilot.config import RESERVED_SYSTEM_ID, EndpointConfig, InventoryConfig
from serverpilot.database import Database
from serverpilot.models import (
    Actor,
    ActorProject,
    Alert,
    AllocatableUnit,
    AllocationRequest,
    AuditEvent,
    Endpoint,
    EndpointTelemetryCurrent,
    EndpointTelemetrySnapshot,
    GPUDevice,
    IdempotencyRecord,
    KeepaliveCurrent,
    Lease,
    LeaseEndpointCommitment,
    LeaseResource,
    MaintenanceWindow,
    ProcessObservation,
    Project,
    ProviderState,
    Reservation,
    ResourceAllocation,
    ResourceClaim as ResourceClaimModel,
    ResourcePlanCandidate as ResourcePlanCandidateModel,
    ResourcePlanEvaluation,
    ResourceProvider,
    ResourceRunActual,
    Revision,
    RuntimeSetting,
    SchedulerJob,
    SchedulerJobEvent,
    SchedulerTarget,
    SchedulerTransfer,
    TelemetryCurrent,
    TelemetrySnapshot,
    WorkloadBinding,
    WorkloadProfile,
    WorkloadProfileGrant,
)
from serverpilot.schemas import (
    ActorCreate,
    AlertAcknowledge,
    EndpointCreate,
    EndpointObservation,
    EndpointEnabled,
    EndpointUpdate,
    EndpointUpsert,
    LeaseBind,
    LeaseObservedBind,
    MaintenanceCreate,
    RetentionPrune,
    RequestCreate,
    ReservationCreate,
    ResourceClaim as ResourceClaimInput,
    ResourceConstraints,
    ResourcePlanEvaluationInput,
    ResourceQuantities,
    ResourceRunActualInput,
    CollectorSettingsUpdate,
    SchedulerJobCancel,
    SchedulerOneOffSubmit,
    SchedulerProfileSubmit,
    SchedulerTargetUpsert,
    SchedulerUploadRequest,
    WorkloadProfileClaim,
    WorkloadProfileUpsert,
)
from serverpilot.slurm import (
    CommandSlurmProvider,
    SlurmProvider,
    SlurmProviderError,
    broker_job_name,
    broker_state,
)
from serverpilot.timeutil import ensure_utc, json_dump, json_load, utcnow
from serverpilot.planner import ResourcePlanCandidate, select_smallest_useful_plan


ACTIVE_LEASE_STATES = {"HELD", "ACTIVE", "ORPHANED_BUSY", "CONFLICT"}
SYSTEM_ACTOR_ID = RESERVED_SYSTEM_ID
SYSTEM_PROJECT_ID = RESERVED_SYSTEM_ID
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
LEASE_SCOPED_BLOCKING_ALERT_TYPES = {"lease_process_conflict", "orphaned_busy"}
TERMINAL_SCHEDULER_JOB_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
TERMINAL_SCHEDULER_TRANSFER_STATES = {"COMPLETED", "FAILED", "DEFERRED"}
TELEMETRY_HISTORY_INTERVAL_SECONDS = 60
TELEMETRY_HISTORY_RETENTION_SECONDS = 24 * 60 * 60
# The collector derives a process start time from `ps etimes`, which has
# one-second precision and is sampled after the endpoint observation begins.
# Preserve the already-observed identity across this bounded measurement
# jitter; otherwise a healthy long-running process can look new on every
# collection and lose its workload attribution.
PROCESS_START_TIME_JITTER_SECONDS = 2
MUTATING_ROLES = {"allocator", "operator", "admin"}
RESOURCE_SNAPSHOT_HISTORY_LIMIT = 50
COLLECTOR_INTERVAL_PRESETS = {5, 10, 30}
COLLECTOR_INTERVAL_SETTING_KEY = "collector_interval_seconds"
# Routine coordination is cooperative, not an administrative workflow.
# Endpoint inventory is shared on loopback; owner_project_id is attribution,
# not an endpoint-management permission boundary. Lease/request ownership still
# prevents one actor from changing another actor's active resource contract.
OPERATOR_ROLES = MUTATING_ROLES
ADMIN_ROLES = {"admin"}
CUDA_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
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


def _external_datetime(value: str | None) -> datetime | None:
    if not value or value in {"Unknown", "N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return ensure_utc(parsed)


class BrokerService:
    """The only component allowed to mutate the broker database.

    The service deliberately contains scheduling decisions, derived availability
    and audit writes together so REST, CLI, MCP and GUI cannot drift.
    """

    def __init__(
        self,
        database: Database,
        inventory: InventoryConfig,
        slurm_provider: SlurmProvider | None = None,
    ) -> None:
        self.database = database
        self.inventory = inventory
        self.slurm_provider = slurm_provider or CommandSlurmProvider()
        self._collector_settings_lock = threading.Lock()
        self._scheduler_submit_lock = threading.Lock()
        self._scheduler_transfer_lock = threading.Lock()

    # ---- initialization, identity and transaction primitives -----------------

    def initialize(self, *, sync_inventory: bool = False) -> None:
        """Initialize the loopback control-plane state."""
        self.database.migrate()

        def operation(session: Session) -> None:
            now = utcnow()
            revision = session.get(Revision, 1)
            if revision is None:
                session.add(Revision(id=1, value=0, updated_at=now))
            self._load_runtime_collector_settings(session)
            has_inventory = (session.scalar(select(func.count()).select_from(Endpoint)) or 0) > 0
            has_unowned_endpoints = (
                session.scalar(
                    select(func.count())
                    .select_from(Endpoint)
                    .where(Endpoint.owner_project_id.is_(None))
                )
                or 0
            ) > 0
            if sync_inventory or not has_inventory or has_unowned_endpoints:
                self._upsert_inventory(session, now)
            self._ensure_system_identity(session, now)
            self._defer_interrupted_scheduler_transfers(session, now)
            self._resolve_stale_lease_alerts(session, now)
            self._bump_revision(session, now)

        self._write(operation)

    def _load_runtime_collector_settings(self, session: Session) -> None:
        setting = session.get(RuntimeSetting, COLLECTOR_INTERVAL_SETTING_KEY)
        if setting is None:
            return
        try:
            interval_seconds = int(setting.value)
        except ValueError:
            return
        if interval_seconds not in COLLECTOR_INTERVAL_PRESETS:
            return
        self.inventory.collector.interval_seconds = interval_seconds
        self.inventory.collector.stale_after_seconds = interval_seconds * 3

    def collector_interval_seconds(self) -> int:
        return self.inventory.collector.interval_seconds

    def collector_settings(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, {"viewer", "allocator", "operator", "admin"})

        def operation(session: Session) -> dict[str, Any]:
            return self.envelope(
                session,
                {
                    "interval_seconds": self.inventory.collector.interval_seconds,
                    "stale_after_seconds": self.inventory.collector.stale_after_seconds,
                    "allowed_intervals": sorted(COLLECTOR_INTERVAL_PRESETS),
                },
            )

        return self._read(operation)

    def update_collector_settings(
        self,
        actor: ActorContext,
        settings: CollectorSettingsUpdate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> tuple[dict[str, Any], bool]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="collector.settings.updated",
                key=idempotency_key,
            )
            if existing is not None:
                return existing, False
            now = utcnow()
            before = {
                "interval_seconds": self.inventory.collector.interval_seconds,
                "stale_after_seconds": self.inventory.collector.stale_after_seconds,
            }
            setting = session.get(RuntimeSetting, COLLECTOR_INTERVAL_SETTING_KEY)
            if setting is None:
                setting = RuntimeSetting(
                    key=COLLECTOR_INTERVAL_SETTING_KEY,
                    value=str(settings.interval_seconds),
                    updated_at=now,
                )
                session.add(setting)
            else:
                setting.value = str(settings.interval_seconds)
                setting.updated_at = now
            revision = self._bump_revision(session, now)
            after = {
                "interval_seconds": settings.interval_seconds,
                "stale_after_seconds": settings.interval_seconds * 3,
                "allowed_intervals": sorted(COLLECTOR_INTERVAL_PRESETS),
            }
            event = self._audit(
                session,
                actor_id=actor.id,
                action="collector.settings.updated",
                resource_type="runtime_setting",
                resource_id=COLLECTOR_INTERVAL_SETTING_KEY,
                result="success",
                before=before,
                after=after,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "settings": after,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="collector.settings.updated",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result, True

        with self._collector_settings_lock:
            result, applied = self._write(operation)
            if applied:
                self.inventory.collector.interval_seconds = settings.interval_seconds
                self.inventory.collector.stale_after_seconds = settings.interval_seconds * 3
            return result

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
                    workspace_path=configured_endpoint.workspace_path,
                    observation_profile=configured_endpoint.observation_profile,
                    keepalive_adapter_id=configured_endpoint.keepalive_adapter_id,
                    keepalive_policy=configured_endpoint.keepalive_policy,
                    labels_json=json_dump(configured_endpoint.labels),
                    storage_group=configured_endpoint.storage_group,
                    expected_gpu_count=configured_endpoint.expected_gpu_count,
                    expected_gpu_total_vram_mib=configured_endpoint.expected_gpu_total_vram_mib,
                    owner_project_id=(
                        configured_endpoint.project_ids[0]
                        if configured_endpoint.project_ids
                        else None
                    ),
                    lifecycle_state="active",
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
                protected_values = {
                    "ssh_user": configured_endpoint.ssh_user,
                    "ssh_alias": configured_endpoint.ssh_alias,
                    "workspace_path": configured_endpoint.workspace_path,
                    "observation_profile": configured_endpoint.observation_profile,
                    "keepalive_adapter_id": configured_endpoint.keepalive_adapter_id,
                }
                changed_protected_fields = sorted(
                    field
                    for field, value in protected_values.items()
                    if value != getattr(endpoint, field)
                )
                if (
                    changed_protected_fields
                    and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
                ):
                    raise BrokerError(
                        "keepalive_endpoint_connection_in_use",
                        "stop the active endpoint keepalive before synchronizing changed connection or verification settings",
                        status_code=409,
                        details={"fields": changed_protected_fields},
                    )
                endpoint.ssh_user = configured_endpoint.ssh_user
                endpoint.ssh_alias = configured_endpoint.ssh_alias
                endpoint.workspace_path = configured_endpoint.workspace_path
                endpoint.observation_profile = configured_endpoint.observation_profile
                if (
                    endpoint.keepalive_policy == "idle_keepalive"
                    and configured_endpoint.keepalive_adapter_id is None
                ):
                    raise BrokerError(
                        "keepalive_adapter_required",
                        "disable idle keepalive before removing its sealed endpoint adapter",
                        status_code=409,
                    )
                endpoint.keepalive_adapter_id = configured_endpoint.keepalive_adapter_id
                # Inventory's default starts new endpoints safely OFF. It must
                # not silently undo a policy written by the dedicated endpoint
                # control when the inventory did not name a policy.
                if "keepalive_policy" in configured_endpoint.model_fields_set:
                    endpoint.keepalive_policy = configured_endpoint.keepalive_policy
                endpoint.labels_json = json_dump(configured_endpoint.labels)
                endpoint.storage_group = configured_endpoint.storage_group
                endpoint.expected_gpu_count = configured_endpoint.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = (
                    configured_endpoint.expected_gpu_total_vram_mib
                )
                if endpoint.owner_project_id is None and configured_endpoint.project_ids:
                    endpoint.owner_project_id = configured_endpoint.project_ids[0]
                endpoint.updated_at = now
            session.flush()

    @staticmethod
    def _ensure_system_identity(session: Session, now: datetime) -> tuple[Actor, Project]:
        """Create the tokenless identity reserved for internal keepalive records."""

        project = session.get(Project, SYSTEM_PROJECT_ID)
        actor = session.get(Actor, SYSTEM_ACTOR_ID)
        if project is not None or actor is not None:
            actor_project_ids = (
                set(
                    session.scalars(
                        select(ActorProject.project_id).where(
                            ActorProject.actor_id == SYSTEM_ACTOR_ID
                        )
                    ).all()
                )
                if actor is not None
                else set()
            )
            foreign_system_membership = session.scalar(
                select(ActorProject.actor_id)
                .where(
                    ActorProject.project_id == SYSTEM_PROJECT_ID,
                    ActorProject.actor_id != SYSTEM_ACTOR_ID,
                )
                .limit(1)
            )
            identity_is_exact = (
                project is not None
                and actor is not None
                and project.display_name == "ServerPilot internal"
                and project.weight == 1
                and project.quota_gpus is None
                and project.concurrency_limit is None
                and project.enabled
                and actor.display_name == "ServerPilot internal"
                and actor.role == "operator"
                and actor.enabled
                and actor_project_ids.issubset({SYSTEM_PROJECT_ID})
                and foreign_system_membership is None
            )
            if not identity_is_exact:
                raise BrokerError(
                    "reserved_system_identity_conflict",
                    "existing data conflicts with the tokenless ServerPilot internal identity",
                    status_code=409,
                )
        if project is None:
            project = Project(
                id=SYSTEM_PROJECT_ID,
                display_name="ServerPilot internal",
                weight=1,
                quota_gpus=None,
                concurrency_limit=None,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(project)
        if actor is None:
            actor = Actor(
                id=SYSTEM_ACTOR_ID,
                display_name="ServerPilot internal",
                role="operator",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(actor)
        session.flush()
        membership = session.get(
            ActorProject,
            {"actor_id": SYSTEM_ACTOR_ID, "project_id": SYSTEM_PROJECT_ID},
        )
        if membership is None:
            session.add(ActorProject(actor_id=SYSTEM_ACTOR_ID, project_id=SYSTEM_PROJECT_ID))
        return actor, project

    def collector_endpoints(self) -> list[EndpointConfig]:
        """Read current control-plane endpoint inventory for fixed-command collection."""

        def operation(session: Session) -> list[EndpointConfig]:
            values: list[EndpointConfig] = []
            endpoints = session.scalars(
                select(Endpoint)
                .where(Endpoint.lifecycle_state.in_({"active", "draining"}))
                .order_by(Endpoint.id)
            ).all()
            for endpoint in endpoints:
                values.append(
                    EndpointConfig(
                        id=endpoint.id,
                        host=endpoint.host,
                        port=endpoint.port,
                        ssh_user=endpoint.ssh_user,
                        ssh_alias=endpoint.ssh_alias,
                        workspace_path=endpoint.workspace_path,
                        observation_profile=endpoint.observation_profile,
                        keepalive_adapter_id=endpoint.keepalive_adapter_id,
                        keepalive_policy=endpoint.keepalive_policy,
                        labels=json_load(endpoint.labels_json),
                        storage_group=endpoint.storage_group,
                        expected_gpu_count=endpoint.expected_gpu_count,
                        expected_gpu_total_vram_mib=endpoint.expected_gpu_total_vram_mib,
                        project_ids=[],
                    )
                )
            return values

        return self._read(operation)

    def collector_endpoint(self, endpoint_id: str) -> EndpointConfig:
        """Return one sealed endpoint configuration for targeted verification.

        A stop verification must remain possible after an endpoint is paused,
        so this single-endpoint path does not apply the regular collector filter.
        """

        def operation(session: Session) -> EndpointConfig:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_collectable", "endpoint does not exist", status_code=404
                )
            return EndpointConfig(
                id=endpoint.id,
                host=endpoint.host,
                port=endpoint.port,
                ssh_user=endpoint.ssh_user,
                ssh_alias=endpoint.ssh_alias,
                workspace_path=endpoint.workspace_path,
                observation_profile=endpoint.observation_profile,
                keepalive_adapter_id=endpoint.keepalive_adapter_id,
                keepalive_policy=endpoint.keepalive_policy,
                labels=json_load(endpoint.labels_json),
                storage_group=endpoint.storage_group,
                expected_gpu_count=endpoint.expected_gpu_count,
                expected_gpu_total_vram_mib=endpoint.expected_gpu_total_vram_mib,
                project_ids=[],
            )

        return self._read(operation)

    def _write(self, operation: Callable[[Session], T]) -> T:
        """Execute one serialized SQLite write."""

        with self.database.session() as session:
            try:
                session.execute(text("BEGIN IMMEDIATE"))
                result = operation(session)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise

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

    def context_for_actor(self, actor_id: str) -> ActorContext:
        """Resolve a previously registered loopback actor."""

        def operation(session: Session) -> ActorContext:
            if actor_id == SYSTEM_ACTOR_ID:
                raise BrokerError(
                    "reserved_system_identity",
                    "the ServerPilot internal identity cannot be resolved for public use",
                    status_code=403,
                )
            actor = session.get(Actor, actor_id)
            if actor is None or not actor.enabled:
                raise BrokerError("actor_disabled", "actor is disabled", status_code=403)
            project_ids = frozenset(
                session.scalars(
                    select(ActorProject.project_id).where(ActorProject.actor_id == actor.id)
                ).all()
            )
            return ActorContext(id=actor.id, role=actor.role, project_ids=project_ids)

        return self._read(operation)

    def local_actor(self, actor_id: str) -> ActorContext:
        """Resolve a cooperative loopback label, never an administrator.

        The label is intentionally not a credential.  It records ownership so
        one local actor cannot mutate another actor's lease, request, or Slurm
        job.  Existing inventory projects are visible as cooperative project
        memberships; authenticated actors retain their explicit memberships.
        """

        normalized = actor_id.strip()
        if normalized == SYSTEM_ACTOR_ID:
            raise BrokerError(
                "reserved_system_identity",
                "the ServerPilot internal identity cannot be used as a local actor",
                status_code=403,
            )
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", normalized):
            raise BrokerError(
                "invalid_actor_name",
                "actor name must start with a letter and contain 2-128 letters, numbers, '.', '_' or '-'",
                status_code=422,
            )

        def resolve(session: Session) -> ActorContext | None:
            stored_actor = session.get(Actor, normalized)
            if stored_actor is None:
                return None
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="allocator", project_ids=project_ids)

        existing = self._read(resolve)
        if existing is not None:
            return existing

        def create(session: Session) -> ActorContext:
            stored_actor = session.get(Actor, normalized)
            if stored_actor is None:
                now = utcnow()
                stored_actor = Actor(
                    id=normalized,
                    display_name=normalized,
                    role="allocator",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(stored_actor)
                session.flush()
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="allocator", project_ids=project_ids)

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
        return lease.actor_id == actor.id

    @staticmethod
    def _reject_generic_keepalive_mutation(lease: Lease) -> None:
        if lease.kind == "keepalive":
            raise BrokerError(
                "keepalive_requires_dedicated_operation",
                "internal keepalive ownership can only be changed by the dedicated keepalive operation",
                status_code=409,
            )

    @staticmethod
    def _active_keepalive_for_endpoint(session: Session, endpoint_id: str) -> str | None:
        return session.scalar(
            select(Lease.id)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                Lease.kind == "keepalive",
                Lease.state.in_(ACTIVE_LEASE_STATES),
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
            .limit(1)
        )

    @staticmethod
    def _can_manage_endpoint(actor: ActorContext, _endpoint: Endpoint) -> bool:
        # Endpoint inventory is a shared loopback control-plane resource.  The
        # actor label records who made the change for audit, but endpoint
        # lifecycle operations are intentionally not project-permission gated.
        return actor.role in MUTATING_ROLES

    @classmethod
    def _require_endpoint_manager(cls, actor: ActorContext, endpoint: Endpoint) -> None:
        if not cls._can_manage_endpoint(actor, endpoint):
            raise BrokerError(
                "endpoint_forbidden",
                "this actor cannot manage server inventory",
                status_code=403,
            )

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
            before_json=json_dump(before) if before is not None else None,
            after_json=json_dump(after) if after is not None else None,
            summary_json=json_dump(summary or {}),
            created_at=now,
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _resolve_lease_alerts(session: Session, lease_id: str, now: datetime) -> int:
        """Close blocking alerts once their lease can no longer own resources."""

        alerts = session.scalars(
            select(Alert).where(
                Alert.alert_type.in_(LEASE_SCOPED_BLOCKING_ALERT_TYPES),
                Alert.resource_type == "lease",
                Alert.resource_id == lease_id,
                Alert.active.is_(True),
            )
        ).all()
        for alert in alerts:
            alert.active = False
            alert.last_seen_at = now
        return len(alerts)

    @classmethod
    def _resolve_stale_lease_alerts(cls, session: Session, now: datetime) -> int:
        """Repair alerts whose lease is terminal, absent, or owns no live resource."""

        alerts = session.scalars(
            select(Alert).where(
                Alert.alert_type.in_(LEASE_SCOPED_BLOCKING_ALERT_TYPES),
                Alert.resource_type == "lease",
                Alert.active.is_(True),
            )
        ).all()
        if not alerts:
            return 0
        lease_ids = {alert.resource_id for alert in alerts}
        leases = {
            lease.id: lease
            for lease in session.scalars(select(Lease).where(Lease.id.in_(lease_ids))).all()
        }
        live_resource_lease_ids = set(
            session.scalars(
                select(LeaseResource.lease_id).where(
                    LeaseResource.lease_id.in_(lease_ids),
                    LeaseResource.active.is_(True),
                )
            ).all()
        )
        resolved = 0
        for lease_id in lease_ids:
            lease = leases.get(lease_id)
            if (
                lease is None
                or lease.state in TERMINAL_LEASE_STATES
                or lease_id not in live_resource_lease_ids
            ):
                resolved += cls._resolve_lease_alerts(session, lease_id, now)
        return resolved

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
        return (
            self._with_committed_revision(json_load(prior.response_json))
            if prior is not None
            else None
        )

    @staticmethod
    def _with_committed_revision(response: dict[str, Any]) -> dict[str, Any]:
        revision = response.get("snapshot_revision")
        if revision is not None:
            response.setdefault("committed", {"snapshot_revision": revision})
        return response

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
        self._with_committed_revision(response)
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
            "workspace_path": endpoint.workspace_path,
            "observation_profile": endpoint.observation_profile,
            "keepalive_adapter_id": endpoint.keepalive_adapter_id,
            "keepalive_policy": endpoint.keepalive_policy,
            "labels": json_load(endpoint.labels_json),
            "storage_group": endpoint.storage_group,
            "expected_gpu_count": endpoint.expected_gpu_count,
            "expected_gpu_total_vram_mib": endpoint.expected_gpu_total_vram_mib,
            "owner_project_id": endpoint.owner_project_id,
            "lifecycle_state": endpoint.lifecycle_state,
            "enabled": endpoint.lifecycle_state == "active",
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
    def _workload_profile_dict(
        profile: WorkloadProfile,
        grant_project_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        return {
            "id": profile.id,
            "project_id": profile.project_id,
            "display_name": profile.display_name,
            "purpose": profile.purpose,
            "duration_seconds": profile.duration_seconds,
            "constraints": json_load(profile.constraints_json),
            "runtime_kind": profile.runtime_kind,
            "scheduler_target_id": profile.scheduler_target_id,
            "scheduler": (
                json_load(profile.scheduler_spec_json)
                if profile.scheduler_spec_json is not None
                else None
            ),
            "grant_project_ids": sorted(grant_project_ids),
            "grant_all_projects": profile.grant_all_projects,
            "retain_submission_body": profile.retain_submission_body,
            "enabled": profile.enabled,
            "created_at": _iso(profile.created_at),
            "updated_at": _iso(profile.updated_at),
        }

    @staticmethod
    def _scheduler_target_dict(target: SchedulerTarget) -> dict[str, Any]:
        connection = json_load(target.connection_json)
        transport_profile = (
            connection.get("transport_profile") if isinstance(connection, dict) else None
        )
        inspection_profile = (
            connection.get("inspection_profile") if isinstance(connection, dict) else None
        )
        return {
            "id": target.id,
            "display_name": target.display_name,
            "kind": "external-scheduler",
            "adapter": target.adapter,
            "transport_profile": transport_profile,
            "inspection_profile": inspection_profile,
            "credential_refs": json_load(target.credential_refs_json),
            "capabilities": json_load(target.capabilities_json),
            "access_hint": target.access_hint,
            "last_access": {
                "status": target.access_status or "unknown",
                "message": target.access_message,
                "checked_at": _iso(target.access_checked_at),
            },
            "data_transfer": (
                {
                    "mode": "staged-upload",
                    "overwrite_existing_destination": False,
                }
                if "data-transfer" in json_load(target.capabilities_json)
                else None
            ),
            "enabled": target.enabled,
            "created_at": _iso(target.created_at),
            "updated_at": _iso(target.updated_at),
        }

    @staticmethod
    def _scheduler_job_dict(
        job: SchedulerJob,
        events: Iterable[SchedulerJobEvent] = (),
    ) -> dict[str, Any]:
        return {
            "id": job.id,
            "target_id": job.target_id,
            "actor_id": job.actor_id,
            "project_id": job.project_id,
            "profile_id": job.profile_id,
            "task_ref": job.task_ref,
            "purpose": job.purpose,
            "approval_ref": job.approval_ref,
            "request": json_load(job.request_json),
            "script_body_retained": job.script_body is not None and job.retain_submission_body,
            "scheduler_job_id": job.scheduler_job_id,
            "state": job.state,
            "raw_state": job.raw_state,
            "allocated_tres": json_load(job.allocated_tres_json),
            "node_list": job.node_list,
            "stdout_path": job.stdout_path,
            "stderr_path": job.stderr_path,
            "exit_code": job.exit_code,
            "error_message": job.error_message,
            "submitted_at": _iso(job.submitted_at),
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "created_at": _iso(job.created_at),
            "updated_at": _iso(job.updated_at),
            "events": [
                {
                    "id": event.id,
                    "state": event.state,
                    "raw_state": event.raw_state,
                    "detail": json_load(event.detail_json),
                    "created_at": _iso(event.created_at),
                }
                for event in events
            ],
        }

    @staticmethod
    def _scheduler_transfer_dict(transfer: SchedulerTransfer) -> dict[str, Any]:
        return {
            "id": transfer.id,
            "target_id": transfer.target_id,
            "actor_id": transfer.actor_id,
            "project_id": transfer.project_id,
            "approval_ref": transfer.approval_ref,
            "local_source_path": transfer.local_source_path,
            "remote_directory": transfer.remote_directory,
            "remote_staged_path": transfer.remote_staged_path,
            "source_size_bytes": transfer.source_size_bytes,
            "state": transfer.state,
            "error_message": transfer.error_message,
            "created_at": _iso(transfer.created_at),
            "updated_at": _iso(transfer.updated_at),
            "completed_at": _iso(transfer.completed_at),
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
        active_resources = [resource for resource in resources if resource.active]
        gpu_by_id = (
            {
                gpu.id: gpu
                for gpu in session.scalars(
                    select(GPUDevice).where(
                        GPUDevice.id.in_([resource.gpu_id for resource in active_resources])
                    )
                ).all()
            }
            if active_resources
            else {}
        )
        endpoints = (
            {
                endpoint.id: endpoint
                for endpoint in session.scalars(
                    select(Endpoint).where(
                        Endpoint.id.in_({gpu.endpoint_id for gpu in gpu_by_id.values()})
                    )
                ).all()
            }
            if gpu_by_id
            else {}
        )
        commitments = {
            commitment.endpoint_id: commitment
            for commitment in session.scalars(
                select(LeaseEndpointCommitment).where(LeaseEndpointCommitment.lease_id == lease.id)
            ).all()
        }
        by_endpoint: dict[str, list[GPUDevice]] = defaultdict(list)
        absent_gpu_ids: list[str] = []
        for resource in active_resources:
            gpu = gpu_by_id.get(resource.gpu_id)
            if gpu is not None:
                if gpu.present:
                    by_endpoint[gpu.endpoint_id].append(gpu)
                else:
                    absent_gpu_ids.append(gpu.id)
        executable_resources: list[dict[str, Any]] = []
        if not absent_gpu_ids:
            for endpoint_id, gpus in sorted(by_endpoint.items()):
                endpoint = endpoints.get(endpoint_id)
                if endpoint is None:
                    continue
                gpus.sort(key=lambda item: item.gpu_index)
                selectors = [gpu.gpu_uuid for gpu in gpus]
                # Admission rejects unsafe selectors, but never emit an unvalidated
                # value should historical telemetry predate that invariant.
                if not all(CUDA_SELECTOR_RE.fullmatch(selector) for selector in selectors):
                    continue
                commitment = commitments.get(endpoint_id)
                executable_resources.append(
                    {
                        "endpoint": {
                            "id": endpoint.id,
                            "host": endpoint.host,
                            "port": endpoint.port,
                            "ssh_user": endpoint.ssh_user,
                            "workspace_path": endpoint.workspace_path,
                        },
                        "gpus": [
                            {
                                "id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "gpu_index": gpu.gpu_index,
                            }
                            for gpu in gpus
                        ],
                        "cuda_visible_devices": ",".join(selectors),
                        "commitment": {
                            "cpu_cores": commitment.cpu_cores if commitment else 0.0,
                            "memory_mib": commitment.memory_mib if commitment else 0,
                        },
                    }
                )
        return {
            "id": lease.id,
            "request_id": lease.request_id,
            "actor_id": lease.actor_id,
            "project_id": lease.project_id,
            "kind": lease.kind,
            "state": lease.state,
            "gpu_ids": [resource.gpu_id for resource in active_resources],
            "absent_gpu_ids": absent_gpu_ids,
            "resources": executable_resources,
            "issued_at": _iso(lease.issued_at),
            "expires_at": _iso(lease.expires_at),
            "last_heartbeat_at": _iso(lease.last_heartbeat_at),
            "activated_at": _iso(lease.activated_at),
            "released_at": _iso(lease.released_at),
            "release_reason": lease.release_reason,
            "issued_revision": lease.issued_revision,
            "task_ref": request.task_ref if request else None,
            "purpose": request.purpose if request else None,
            "workloads": (
                []
                if lease.kind == "keepalive"
                else [
                    {"run_id": binding.run_id, "process_keys": json_load(binding.process_keys_json)}
                    for binding in bindings
                ]
            ),
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
    def _telemetry_dict(
        telemetry: TelemetryCurrent | TelemetrySnapshot | None,
    ) -> dict[str, Any] | None:
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
    def _host_telemetry_dict(
        telemetry: EndpointTelemetryCurrent | EndpointTelemetrySnapshot | None,
    ) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "observed_at": _iso(telemetry.observed_at),
            "collected_at": _iso(telemetry.collected_at),
            "cpu_count": telemetry.cpu_count,
            "load_1m": telemetry.load_1m,
            "cpu_utilization_pct": telemetry.cpu_utilization_pct,
            "cpu_total_ticks": telemetry.cpu_total_ticks,
            "cpu_idle_ticks": telemetry.cpu_idle_ticks,
            "memory_total_mib": telemetry.memory_total_mib,
            "memory_available_mib": telemetry.memory_available_mib,
            "provider": telemetry.provider,
        }

    @staticmethod
    def _host_cpu_utilization_pct(
        previous: EndpointTelemetryCurrent | None,
        *,
        cpu_total_ticks: int | None,
        cpu_idle_ticks: int | None,
    ) -> float | None:
        if (
            previous is None
            or previous.cpu_total_ticks is None
            or previous.cpu_idle_ticks is None
            or cpu_total_ticks is None
            or cpu_idle_ticks is None
        ):
            return None
        total_delta = cpu_total_ticks - previous.cpu_total_ticks
        idle_delta = cpu_idle_ticks - previous.cpu_idle_ticks
        if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
            return None
        return round((total_delta - idle_delta) * 100 / total_delta, 2)

    @staticmethod
    def _resource_quantities_dict(quantities: ResourceQuantities) -> dict[str, Any]:
        return quantities.model_dump(mode="json")

    @staticmethod
    def _provider_dict(provider: ResourceProvider) -> dict[str, Any]:
        return {
            "id": provider.id,
            "provider_type": provider.provider_type,
            "display_name": provider.display_name,
            "endpoint_id": provider.endpoint_id,
            "scheduler_target_id": provider.scheduler_target_id,
            "native_ref": json_load(provider.native_ref_json),
            "metadata": json_load(provider.metadata_json),
            "enabled": provider.enabled,
            "created_at": _iso(provider.created_at),
            "updated_at": _iso(provider.updated_at),
        }

    @staticmethod
    def _allocatable_unit_dict(unit: AllocatableUnit) -> dict[str, Any]:
        return {
            "id": unit.id,
            "provider_id": unit.provider_id,
            "unit_key": unit.unit_key,
            "unit_type": unit.unit_type,
            "endpoint_id": unit.endpoint_id,
            "gpu_id": unit.gpu_id,
            "scheduler_target_id": unit.scheduler_target_id,
            "total_gpu_count": unit.total_gpu_count,
            "total_cpu_cores": unit.total_cpu_cores,
            "total_memory_mib": unit.total_memory_mib,
            "total_vram_mib": unit.total_vram_mib,
            "labels": json_load(unit.labels_json),
            "native_ref": json_load(unit.native_ref_json),
            "state": unit.state,
            "enabled": unit.enabled,
            "created_at": _iso(unit.created_at),
            "updated_at": _iso(unit.updated_at),
        }

    @staticmethod
    def _resource_claim_dict(claim: ResourceClaimModel) -> dict[str, Any]:
        return {
            "id": claim.id,
            "actor_id": claim.actor_id,
            "project_id": claim.project_id,
            "task_ref": claim.task_ref,
            "purpose": claim.purpose,
            "provider_type": claim.provider_type,
            "requested_quantities": json_load(claim.requested_quantities_json),
            "forecast": json_load(claim.forecast_json) if claim.forecast_json else None,
            "state": claim.state,
            "created_at": _iso(claim.created_at),
            "updated_at": _iso(claim.updated_at),
        }

    @staticmethod
    def _resource_allocation_dict(
        allocation: ResourceAllocation,
        *,
        unit: AllocatableUnit | None = None,
    ) -> dict[str, Any]:
        return {
            "id": allocation.id,
            "claim_id": allocation.claim_id,
            "unit_id": allocation.unit_id,
            "unit_type": unit.unit_type if unit else None,
            "endpoint_id": unit.endpoint_id if unit else None,
            "gpu_id": unit.gpu_id if unit else None,
            "scheduler_target_id": unit.scheduler_target_id if unit else None,
            "native_lease_id": allocation.native_lease_id,
            "native_scheduler_job_id": allocation.native_scheduler_job_id,
            "quantities": json_load(allocation.quantities_json),
            "state": allocation.state,
            "created_at": _iso(allocation.created_at),
            "updated_at": _iso(allocation.updated_at),
        }

    @staticmethod
    def _resource_plan_evaluation_dict(
        evaluation: ResourcePlanEvaluation,
        candidates: Iterable[ResourcePlanCandidateModel] = (),
    ) -> dict[str, Any]:
        return {
            "id": evaluation.id,
            "claim_id": evaluation.claim_id,
            "actor_id": evaluation.actor_id,
            "project_id": evaluation.project_id,
            "task_ref": evaluation.task_ref,
            "baseline_runtime_seconds": evaluation.baseline_runtime_seconds,
            "marginal_min_saved_seconds": evaluation.marginal_min_saved_seconds,
            "marginal_min_saved_ratio": evaluation.marginal_min_saved_ratio,
            "selected_candidate_key": evaluation.selected_candidate_key,
            "forecast": json_load(evaluation.forecast_json),
            "created_at": _iso(evaluation.created_at),
            "candidates": [
                {
                    "candidate_key": candidate.candidate_key,
                    "provider_type": candidate.provider_type,
                    "quantities": json_load(candidate.quantities_json),
                    "predicted_runtime_seconds": candidate.predicted_runtime_seconds,
                    "predicted_saved_seconds": candidate.predicted_saved_seconds,
                    "predicted_saved_ratio": candidate.predicted_saved_ratio,
                    "satisfies_marginal_threshold": candidate.satisfies_marginal_threshold,
                    "selected": candidate.selected,
                    "rejection_reason": candidate.rejection_reason,
                }
                for candidate in candidates
            ],
        }

    @staticmethod
    def _resource_actual_dict(actual: ResourceRunActual) -> dict[str, Any]:
        return {
            "id": actual.id,
            "evaluation_id": actual.evaluation_id,
            "claim_id": actual.claim_id,
            "actor_id": actual.actor_id,
            "project_id": actual.project_id,
            "task_ref": actual.task_ref,
            "started_at": _iso(actual.started_at),
            "completed_at": _iso(actual.completed_at),
            "actual_duration_seconds": actual.actual_duration_seconds,
            "quantities": json_load(actual.quantities_json),
            "outcome": actual.outcome,
            "notes": json_load(actual.notes_json),
            "created_at": _iso(actual.created_at),
        }

    def _current_processes(
        self, session: Session, gpu_id: str, now: datetime
    ) -> list[ProcessObservation]:
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

    def _resources_have_fresh_telemetry(
        self, session: Session, resources: Iterable[LeaseResource], now: datetime
    ) -> bool:
        cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
        for resource in resources:
            gpu = session.get(GPUDevice, resource.gpu_id)
            if gpu is None or not gpu.present:
                return False
            telemetry = session.get(TelemetryCurrent, resource.gpu_id)
            observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
            if observed_at is None or observed_at < cutoff:
                return False
        return True

    def _keepalive_ttl_seconds(self) -> int:
        """Return the compatibility duration stored on the internal request.

        The request schema still requires a positive duration, but keepalive
        ownership itself is persistent and therefore has no lease expiry.  A
        missed collection may change the current process state; it must never
        erase the fact that ServerPilot owns this worker.
        """

        return max(60, self.inventory.collector.stale_after_seconds * 3)

    @staticmethod
    def _keepalive_summary(lease: Lease, endpoint_id: str) -> dict[str, Any]:
        return {
            "endpoint_id": endpoint_id,
            "enabled": lease.state == "ACTIVE",
            "lease_id": lease.id,
            "state": lease.state,
        }

    def _validate_keepalive_observation(
        self,
        session: Session,
        *,
        endpoint_id: str,
        gpu_ids: Iterable[str],
        observation_not_before: datetime,
    ) -> list[ProcessObservation]:
        """Require one post-operation endpoint snapshot covering every leased GPU."""

        threshold = ensure_utc(observation_not_before)
        host = session.get(EndpointTelemetryCurrent, endpoint_id)
        host_observed_at = _as_utc(host.observed_at) if host is not None else None
        host_collected_at = _as_utc(host.collected_at) if host is not None else None
        provider_state = session.scalar(
            select(ProviderState).where(
                ProviderState.provider == "raw-ssh",
                ProviderState.endpoint_id == endpoint_id,
            )
        )
        last_success_at = (
            _as_utc(provider_state.last_success_at) if provider_state is not None else None
        )
        if (
            host_observed_at is None
            or host_collected_at is None
            or host_collected_at < threshold
            or provider_state is None
            or provider_state.last_error is not None
            or last_success_at is None
            or last_success_at < threshold
        ):
            raise BrokerError(
                "keepalive_observation_stale",
                "a complete endpoint observation after the keepalive operation is required",
                status_code=409,
            )
        processes: list[ProcessObservation] = []
        for gpu_id in gpu_ids:
            gpu = session.get(GPUDevice, gpu_id)
            telemetry = session.get(TelemetryCurrent, gpu_id)
            telemetry_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
            telemetry_collected_at = (
                _as_utc(telemetry.collected_at) if telemetry is not None else None
            )
            if (
                gpu is None
                or gpu.endpoint_id != endpoint_id
                or not gpu.present
                or telemetry_at is None
                or telemetry_at != host_observed_at
                or telemetry_collected_at is None
                or telemetry_collected_at < threshold
            ):
                raise BrokerError(
                    "keepalive_observation_incomplete",
                    "the post-operation observation did not cover every endpoint GPU",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )
            processes.extend(self._current_processes(session, gpu_id, utcnow()))
        return processes

    def _record_observed_keepalive(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observation_complete: bool,
        now: datetime,
    ) -> None:
        """Record liveness without turning it into expiring ownership."""

        if not observation_complete:
            return
        leases = session.scalars(
            select(Lease)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                Lease.kind == "keepalive",
                Lease.state == "ACTIVE",
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
            .distinct()
        ).all()
        for lease in leases:
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            if len(resources) != 1:
                continue
            processes = [
                process
                for resource in resources
                for process in self._current_processes(session, resource.gpu_id, now)
            ]
            resource = resources[0]
            current = session.get(KeepaliveCurrent, resource.gpu_id)
            expected_key = self._keepalive_expected_process_key(current)
            observed_keys = {self._process_key(process) for process in processes}
            if expected_key is not None and observed_keys == {expected_key}:
                lease.last_heartbeat_at = now
                lease.expires_at = None
                self._set_keepalive_current(session, resource.gpu_id, "ON", now=now)
            elif processes:
                self._set_keepalive_current(
                    session,
                    resource.gpu_id,
                    "ERROR",
                    error_reason=(
                        "占卡进程身份尚未建立"
                        if expected_key is None
                        else "检测到不属于占卡程序的进程"
                    ),
                    now=now,
                )
            else:
                self._set_keepalive_current(session, resource.gpu_id, "OFF", now=now)

    # ---- per-GPU keepalive policy and ownership --------------------------------

    @staticmethod
    def _keepalive_resources(session: Session, lease_id: str) -> list[LeaseResource]:
        return session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id,
                LeaseResource.active.is_(True),
            )
        ).all()

    def _keepalive_gpu_status(
        self,
        session: Session,
        gpu: GPUDevice,
        lease: Lease | None,
        now: datetime,
    ) -> tuple[str, str | None]:
        """Describe keepalive ownership without exposing the system actor."""

        current = session.get(KeepaliveCurrent, gpu.id)
        if lease is None or lease.kind != "keepalive":
            if current is not None:
                return current.actual, current.error_reason
            return "OFF", None
        resources = self._keepalive_resources(session, lease.id)
        if len(resources) != 1 or resources[0].gpu_id != gpu.id:
            return "ERROR", "占卡记录没有准确对应这一张 GPU"
        if lease.state != "ACTIVE":
            return "ERROR", "占卡记录未处于可用状态"
        if current is not None:
            return current.actual, current.error_reason
        processes = self._current_processes(session, gpu.id, now)
        return ("ON", None) if processes else ("OFF", None)

    @staticmethod
    def _set_keepalive_current(
        session: Session,
        gpu_id: str,
        actual: str,
        *,
        error_reason: str | None = None,
        expected_process: ProcessObservation | None = None,
        now: datetime,
    ) -> None:
        if actual not in {"ON", "OFF", "ERROR"}:
            raise ValueError("invalid keepalive actual state")
        current = session.get(KeepaliveCurrent, gpu_id)
        if current is None:
            session.add(
                KeepaliveCurrent(
                    gpu_id=gpu_id,
                    actual=actual,
                    error_reason=error_reason,
                    expected_pid=(expected_process.pid if expected_process is not None else None),
                    expected_boot_id=(
                        expected_process.boot_id if expected_process is not None else None
                    ),
                    expected_process_started_at=(
                        expected_process.process_started_at
                        if expected_process is not None
                        else None
                    ),
                    updated_at=now,
                )
            )
            return
        current.actual = actual
        current.error_reason = error_reason
        if expected_process is not None:
            current.expected_pid = expected_process.pid
            current.expected_boot_id = expected_process.boot_id
            current.expected_process_started_at = expected_process.process_started_at
        current.updated_at = now

    @staticmethod
    def _keepalive_expected_process_key(current: KeepaliveCurrent | None) -> str | None:
        if (
            current is None
            or current.expected_pid is None
            or current.expected_boot_id is None
            or current.expected_process_started_at is None
        ):
            return None
        started = _as_utc(current.expected_process_started_at)
        assert started is not None
        return (
            f"{current.gpu_id}:{current.expected_pid}:"
            f"{current.expected_boot_id}:{int(started.timestamp())}"
        )

    @staticmethod
    def _policy_keepalive_status(
        endpoint: Endpoint,
        gpu_state: str,
        state: str,
        reason: str | None,
    ) -> tuple[str, str | None]:
        """Keep desired policy independent from the current worker state."""

        return state, reason

    @staticmethod
    def _gpu_payload_is_publicly_available(gpu: dict[str, Any]) -> bool:
        if gpu.get("state") in {"AVAILABLE", "KEEPALIVE"}:
            return True
        return False

    @classmethod
    def _gpu_public_projection(
        cls,
        gpu: dict[str, Any],
        *,
        monitor_status: str,
    ) -> dict[str, Any]:
        """Return the canonical short capacity and Chinese status projection."""

        available = cls._gpu_payload_is_publicly_available(gpu)
        keepalive = gpu.get("keepalive")
        keepalive = keepalive if isinstance(keepalive, dict) else {}
        if monitor_status in {"ERROR", "STALE"}:
            status = "连接失败"
        elif available and (gpu.get("state") == "KEEPALIVE" or keepalive.get("actual") == "ON"):
            status = "可用 · 空闲占卡"
        elif available and keepalive.get("actual") == "ERROR":
            status = f"可用 · 占卡异常：{keepalive.get('reason') or '未知原因'}"
        elif available and keepalive.get("desired") == "ON":
            status = "可用 · 占卡未运行"
        elif available:
            status = "可用 · 未开启占卡"
        elif gpu.get("lease") is not None or gpu.get("state") in {
            "BUSY_UNMANAGED",
            "HELD",
            "LEASED_IDLE",
            "RUNNING_MANAGED",
            "ORPHANED_BUSY",
            "CONFLICT",
        }:
            status = "任务使用中"
        else:
            status = "连接失败"
        return {
            "publicly_available": available,
            "public_status": status,
        }

    @staticmethod
    def _keepalive_aggregate(
        endpoint: Endpoint,
        gpu_keepalive: Iterable[dict[str, Any]],
        *,
        eligible_idle_gpu_count: int,
    ) -> dict[str, Any]:
        values = list(gpu_keepalive)
        status_counts: dict[str, int] = defaultdict(int)
        reasons: list[dict[str, str]] = []
        for item in values:
            status = str(item["state"])
            status_counts[status] += 1
            reason = item.get("reason")
            if reason:
                reasons.append({"gpu_id": str(item["gpu_id"]), "reason": str(reason)})

        active = status_counts["ON"]
        errors = status_counts["ERROR"]
        if errors:
            state = "ERROR"
        elif active:
            state = "ON"
        else:
            state = "OFF"
        desired = "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF"
        return {
            "configured": endpoint.keepalive_adapter_id is not None,
            "policy": endpoint.keepalive_policy,
            "desired": desired,
            "actual": state,
            "state": state,
            "active_gpu_count": active,
            "error_gpu_count": errors,
            "eligible_idle_gpu_count": eligible_idle_gpu_count,
            "reasons": reasons,
        }

    def _endpoint_keepalive_summary(
        self, session: Session, endpoint: Endpoint, now: datetime
    ) -> dict[str, Any]:
        gpus = session.scalars(
            select(GPUDevice)
            .where(GPUDevice.endpoint_id == endpoint.id)
            .order_by(GPUDevice.gpu_index)
        ).all()
        statuses = []
        eligible_idle_gpu_count = 0
        for gpu in gpus:
            lease = self._active_lease_for_gpu(session, gpu.id)
            gpu_state, _gpu_reason = self._gpu_state(session, gpu, now)
            state, reason = self._keepalive_gpu_status(session, gpu, lease, now)
            state, reason = self._policy_keepalive_status(
                endpoint,
                gpu_state,
                state,
                reason,
            )
            statuses.append(
                {
                    "gpu_id": gpu.id,
                    "state": state,
                    "reason": reason,
                }
            )
            if (
                endpoint.keepalive_policy == "idle_keepalive"
                and endpoint.keepalive_adapter_id is not None
                and gpu_state == "AVAILABLE"
            ):
                eligible_idle_gpu_count += 1
        return self._keepalive_aggregate(
            endpoint,
            statuses,
            eligible_idle_gpu_count=eligible_idle_gpu_count,
        )

    def configure_keepalive_policy(
        self,
        actor: ActorContext,
        endpoint_id: str,
        policy: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist desired per-GPU policy; remote execution is deliberately external.

        Disabling does not release leases or signal helpers. Callers must first
        use the stop transitions and fresh collector evidence, preventing a
        policy write from making an unobserved helper allocatable.
        """

        self._require_role(actor, MUTATING_ROLES)
        if policy not in {"disabled", "idle_keepalive"}:
            raise BrokerError(
                "invalid_keepalive_policy",
                "keepalive policy must be disabled or idle_keepalive",
                status_code=422,
            )

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.policy.configure", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = endpoint.keepalive_policy
            before_adapter = endpoint.keepalive_adapter_id
            # ``server-script-v1`` is a sealed, code-owned helper.  Enabling
            # the user-facing occupancy action is the explicit authorization
            # to attach that helper; callers never supply an adapter, command,
            # path, or remote arguments.  This also repairs endpoints created
            # by older GUI builds that selected server-script-v1 observation
            # but left the adapter column empty.
            adapter_changed = policy == "idle_keepalive" and endpoint.keepalive_adapter_id is None
            if adapter_changed:
                endpoint.keepalive_adapter_id = "server-script-v1"
            changed = before != policy or adapter_changed
            if changed:
                endpoint.keepalive_policy = policy
                endpoint.updated_at = now
                if policy == "disabled":
                    for gpu in session.scalars(
                        select(GPUDevice).where(GPUDevice.endpoint_id == endpoint.id)
                    ).all():
                        lease = self._active_lease_for_gpu(session, gpu.id)
                        if lease is None or lease.kind != "keepalive":
                            self._set_keepalive_current(session, gpu.id, "OFF", now=now)
                revision = self._bump_revision(session, now)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="keepalive.policy_configured",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before={"policy": before, "keepalive_adapter_id": before_adapter},
                    after={
                        "policy": policy,
                        "keepalive_adapter_id": endpoint.keepalive_adapter_id,
                    },
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "changed": changed,
                "keepalive": self._endpoint_keepalive_summary(session, endpoint, now),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.policy.configure",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def list_keepalive_transitions(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Return policy-driven start/stop work without starting remote processes.

        The result is a point-in-time work list. A start is written only after
        the helper starts and a fresh observation sees its process, so this
        planner never creates a persistent half-started keepalive lease.
        """

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            endpoints = (
                [session.get(Endpoint, endpoint_id)]
                if endpoint_id is not None
                else session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
            )
            if endpoint_id is not None and endpoints[0] is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            transitions: list[dict[str, Any]] = []
            for endpoint in endpoints:
                assert endpoint is not None
                gpus = session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id == endpoint.id)
                    .order_by(GPUDevice.gpu_index)
                ).all()
                by_id = {gpu.id: gpu for gpu in gpus}
                active_leases = session.scalars(
                    select(Lease)
                    .join(LeaseResource, LeaseResource.lease_id == Lease.id)
                    .where(
                        Lease.kind == "keepalive",
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                        LeaseResource.active.is_(True),
                        LeaseResource.gpu_id.in_(by_id) if by_id else text("0 = 1"),
                    )
                    .distinct()
                ).all()
                for lease in active_leases:
                    resources = self._keepalive_resources(session, lease.id)
                    owned = [by_id[item.gpu_id] for item in resources if item.gpu_id in by_id]
                    if len(owned) != 1 or len(resources) != 1:
                        continue
                    gpu = owned[0]
                    if endpoint.keepalive_policy == "disabled":
                        transitions.append(
                            {
                                "action": "stop",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "lease_id": lease.id,
                                "state": lease.state,
                            }
                        )
                if endpoint.keepalive_policy != "idle_keepalive":
                    continue
                if endpoint.keepalive_adapter_id is None:
                    for gpu in gpus:
                        transitions.append(
                            {
                                "action": "ineligible",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "reason": "endpoint has no sealed keepalive adapter configured",
                            }
                        )
                    continue
                for gpu in gpus:
                    lease = self._active_lease_for_gpu(session, gpu.id)
                    if lease is not None:
                        current = session.get(KeepaliveCurrent, gpu.id)
                        if lease.kind == "keepalive" and (
                            lease.state != "ACTIVE"
                            or not self._current_processes(session, gpu.id, now)
                            or self._keepalive_expected_process_key(current) is None
                        ):
                            transitions.append(
                                {
                                    "action": "start",
                                    "endpoint_id": endpoint.id,
                                    "gpu_id": gpu.id,
                                    "gpu_uuid": gpu.gpu_uuid,
                                }
                            )
                        continue
                    state, reason = self._gpu_state(session, gpu, now)
                    if state == "AVAILABLE":
                        transitions.append(
                            {
                                "action": "start",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                            }
                        )
                    elif reason:
                        # Rejections are useful to a reconciliation loop but
                        # do not become executable work.
                        transitions.append(
                            {
                                "action": "ineligible",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "state": state,
                                "reason": reason,
                            }
                        )
            return {"snapshot_revision": self._revision(session), "transitions": transitions}

        return self._read(operation)

    def keepalive_transition_plan(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Compatibility alias for the daemon/API reconciliation hook."""

        return self.list_keepalive_transitions(endpoint_id)

    def desired_keepalive_candidates(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Return only currently eligible idle GPU start candidates."""

        plan = self.list_keepalive_transitions(endpoint_id)
        return {
            "snapshot_revision": plan["snapshot_revision"],
            "candidates": [item for item in plan["transitions"] if item["action"] == "start"],
        }

    def get_endpoint_keepalive_summary(self, endpoint_id: str) -> dict[str, Any]:
        """Return the public no-actor aggregate after a transition batch."""

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            return {
                "snapshot_revision": self._revision(session),
                "keepalive": self._endpoint_keepalive_summary(session, endpoint, utcnow()),
            }

        return self._read(operation)

    def set_keepalive_error(
        self,
        endpoint_id: str,
        gpu_ids: Iterable[str],
        reason: str,
    ) -> None:
        """Persist an operation failure as current state without changing policy."""

        def operation(session: Session) -> None:
            now = utcnow()
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is not None and gpu.endpoint_id == endpoint_id:
                    self._set_keepalive_current(
                        session,
                        gpu.id,
                        "ERROR",
                        error_reason=reason,
                        now=now,
                    )

        self._write(operation)

    def activate_keepalive(
        self,
        actor: ActorContext,
        endpoint_id: str,
        gpu_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record one keepalive only after its process is freshly observed."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.activate_gpu", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            if endpoint.keepalive_policy != "idle_keepalive":
                raise BrokerError(
                    "keepalive_policy_disabled",
                    "per-GPU keepalive may start only under idle_keepalive policy",
                    status_code=409,
                )
            if endpoint.keepalive_adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint has no sealed keepalive adapter configured",
                    status_code=409,
                )
            if endpoint.lifecycle_state != "active" or not endpoint.enabled:
                raise BrokerError(
                    "endpoint_not_active",
                    "keepalive requires an active, enabled endpoint",
                    status_code=409,
                )
            gpu = session.get(GPUDevice, gpu_id)
            if gpu is None or gpu.endpoint_id != endpoint.id:
                raise BrokerError(
                    "keepalive_gpu_mismatch",
                    "GPU does not belong to the endpoint",
                    status_code=409,
                )
            existing_lease = self._active_lease_for_gpu(session, gpu.id)
            if existing_lease is not None and existing_lease.kind != "keepalive":
                raise BrokerError(
                    "keepalive_gpu_ineligible",
                    "a workload already owns the target GPU",
                    status_code=409,
                    details={"gpu_id": gpu.id},
                )
            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint_id,
                gpu_ids=[gpu.id],
                observation_not_before=observation_not_before,
            )
            if not processes:
                raise BrokerError(
                    "keepalive_process_missing",
                    "fresh collection did not find the occupancy process",
                    status_code=409,
                )
            if len(processes) != 1:
                raise BrokerError(
                    "keepalive_process_conflict",
                    "fresh collection found additional processes on the occupancy GPU",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            if existing_lease is None:
                request = AllocationRequest(
                    id=secrets.token_hex(16),
                    actor_id=SYSTEM_ACTOR_ID,
                    project_id=SYSTEM_PROJECT_ID,
                    profile_id=None,
                    auto_activate=False,
                    task_ref=f"keepalive:{endpoint.id}:{gpu.id}",
                    purpose="ServerPilot idle GPU keepalive",
                    constraints_json=json_dump(
                        {"gpu_count": 1, "endpoint_ids": [endpoint.id], "gpu_ids": [gpu.id]}
                    ),
                    duration_seconds=self._keepalive_ttl_seconds(),
                    expected_duration_seconds=None,
                    start_after=None,
                    deadline=None,
                    approval_ref=None,
                    state="ACTIVE",
                    priority_class="keepalive",
                    blocked_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(request)
                lease = Lease(
                    id=secrets.token_hex(16),
                    request_id=request.id,
                    actor_id=SYSTEM_ACTOR_ID,
                    project_id=SYSTEM_PROJECT_ID,
                    kind="keepalive",
                    state="ACTIVE",
                    issued_at=now,
                    expires_at=None,
                    last_heartbeat_at=now,
                    activated_at=now,
                    released_at=None,
                    release_reason=None,
                    issued_revision=revision,
                )
                session.add(lease)
                session.flush()
                session.add(
                    LeaseResource(lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None)
                )
            else:
                lease = existing_lease
                resources = self._keepalive_resources(session, lease.id)
                if len(resources) != 1 or resources[0].gpu_id != gpu.id:
                    raise BrokerError(
                        "keepalive_gpu_invalid",
                        "per-GPU keepalive record does not match the target GPU",
                        status_code=409,
                    )
                lease.state = "ACTIVE"
                lease.activated_at = lease.activated_at or now
                lease.last_heartbeat_at = now
                lease.expires_at = None
                request = session.get(AllocationRequest, lease.request_id)
                if request is not None:
                    request.state = "ACTIVE"
                    request.updated_at = now
            self._set_keepalive_current(
                session,
                gpu.id,
                "ON",
                expected_process=processes[0],
                now=now,
            )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="keepalive.gpu_activated",
                resource_type="gpu",
                resource_id=gpu.id,
                result="success",
                summary={"lease_id": lease.id, "endpoint_id": endpoint.id},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "keepalive": self._keepalive_lease_summary(lease, endpoint.id, gpu.id),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.activate_gpu",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def activate_keepalives(
        self,
        actor: ActorContext,
        endpoint_id: str,
        gpu_ids: list[str],
        *,
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically record one freshly observed keepalive for every target GPU."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.activate_gpus", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            if endpoint.keepalive_policy != "idle_keepalive":
                raise BrokerError(
                    "keepalive_policy_disabled",
                    "per-GPU keepalive may start only under idle_keepalive policy",
                    status_code=409,
                )
            if endpoint.keepalive_adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint has no sealed keepalive adapter configured",
                    status_code=409,
                )
            if endpoint.lifecycle_state != "active" or not endpoint.enabled:
                raise BrokerError(
                    "endpoint_not_active",
                    "keepalive requires an active, enabled endpoint",
                    status_code=409,
                )
            if not gpu_ids or len(gpu_ids) != len(set(gpu_ids)):
                raise BrokerError(
                    "keepalive_gpu_invalid",
                    "keepalive targets must be a non-empty set of GPUs",
                    status_code=409,
                )

            targets: list[tuple[GPUDevice, Lease | None]] = []
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None or gpu.endpoint_id != endpoint.id:
                    raise BrokerError(
                        "keepalive_gpu_mismatch",
                        "GPU does not belong to the endpoint",
                        status_code=409,
                    )
                existing_lease = self._active_lease_for_gpu(session, gpu.id)
                if existing_lease is not None and existing_lease.kind != "keepalive":
                    raise BrokerError(
                        "keepalive_gpu_ineligible",
                        "a workload already owns the target GPU",
                        status_code=409,
                        details={"gpu_id": gpu.id},
                    )
                if existing_lease is not None:
                    resources = self._keepalive_resources(session, existing_lease.id)
                    if len(resources) != 1 or resources[0].gpu_id != gpu.id:
                        raise BrokerError(
                            "keepalive_gpu_invalid",
                            "per-GPU keepalive record does not match the target GPU",
                            status_code=409,
                        )
                targets.append((gpu, existing_lease))

            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint_id,
                gpu_ids=gpu_ids,
                observation_not_before=observation_not_before,
            )
            observed_gpu_ids = {process.gpu_id for process in processes}
            missing_gpu_ids = [gpu_id for gpu_id in gpu_ids if gpu_id not in observed_gpu_ids]
            if missing_gpu_ids:
                raise BrokerError(
                    "keepalive_process_missing",
                    "fresh collection did not find every occupancy process",
                    status_code=409,
                    details={"gpu_ids": missing_gpu_ids},
                )
            processes_by_gpu: dict[str, list[ProcessObservation]] = defaultdict(list)
            for process in processes:
                processes_by_gpu[process.gpu_id].append(process)
            conflicted_gpu_ids = [
                gpu_id for gpu_id in gpu_ids if len(processes_by_gpu[gpu_id]) != 1
            ]
            if conflicted_gpu_ids:
                raise BrokerError(
                    "keepalive_process_conflict",
                    "fresh collection found additional processes on an occupancy GPU",
                    status_code=409,
                    details={"gpu_ids": conflicted_gpu_ids},
                )

            now = utcnow()
            revision = self._bump_revision(session, now)
            keepalives: list[dict[str, Any]] = []
            event_ids: list[int] = []
            for gpu, existing_lease in targets:
                if existing_lease is None:
                    request = AllocationRequest(
                        id=secrets.token_hex(16),
                        actor_id=SYSTEM_ACTOR_ID,
                        project_id=SYSTEM_PROJECT_ID,
                        profile_id=None,
                        auto_activate=False,
                        task_ref=f"keepalive:{endpoint.id}:{gpu.id}",
                        purpose="ServerPilot idle GPU keepalive",
                        constraints_json=json_dump(
                            {"gpu_count": 1, "endpoint_ids": [endpoint.id], "gpu_ids": [gpu.id]}
                        ),
                        duration_seconds=self._keepalive_ttl_seconds(),
                        expected_duration_seconds=None,
                        start_after=None,
                        deadline=None,
                        approval_ref=None,
                        state="ACTIVE",
                        priority_class="keepalive",
                        blocked_reason=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(request)
                    lease = Lease(
                        id=secrets.token_hex(16),
                        request_id=request.id,
                        actor_id=SYSTEM_ACTOR_ID,
                        project_id=SYSTEM_PROJECT_ID,
                        kind="keepalive",
                        state="ACTIVE",
                        issued_at=now,
                        expires_at=None,
                        last_heartbeat_at=now,
                        activated_at=now,
                        released_at=None,
                        release_reason=None,
                        issued_revision=revision,
                    )
                    session.add(lease)
                    session.flush()
                    session.add(
                        LeaseResource(
                            lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None
                        )
                    )
                else:
                    lease = existing_lease
                    lease.state = "ACTIVE"
                    lease.activated_at = lease.activated_at or now
                    lease.last_heartbeat_at = now
                    lease.expires_at = None
                    request = session.get(AllocationRequest, lease.request_id)
                    if request is not None:
                        request.state = "ACTIVE"
                        request.updated_at = now
                self._set_keepalive_current(
                    session,
                    gpu.id,
                    "ON",
                    expected_process=processes_by_gpu[gpu.id][0],
                    now=now,
                )
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="keepalive.gpu_activated",
                    resource_type="gpu",
                    resource_id=gpu.id,
                    result="success",
                    summary={"lease_id": lease.id, "endpoint_id": endpoint.id},
                    now=now,
                )
                event_ids.append(event.id)
                keepalives.append(self._keepalive_lease_summary(lease, endpoint.id, gpu.id))
            result = {
                "event_ids": event_ids,
                "snapshot_revision": revision,
                "keepalives": keepalives,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.activate_gpus",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    @staticmethod
    def _keepalive_lease_summary(lease: Lease, endpoint_id: str, gpu_id: str) -> dict[str, Any]:
        return {
            "endpoint_id": endpoint_id,
            "gpu_id": gpu_id,
            "enabled": lease.state == "ACTIVE",
            "lease_id": lease.id,
            "state": lease.state,
        }

    def prepare_keepalive_stop(
        self, actor: ActorContext, endpoint_id: str, gpu_id: str
    ) -> dict[str, Any]:
        """Resolve one per-GPU occupancy stop target."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            gpu = session.get(GPUDevice, gpu_id)
            if gpu is None or gpu.endpoint_id != endpoint.id:
                raise BrokerError(
                    "keepalive_gpu_mismatch", "GPU does not belong to the endpoint", status_code=409
                )
            lease = self._active_lease_for_gpu(session, gpu.id)
            if lease is None or lease.kind != "keepalive":
                return {
                    "event_id": None,
                    "snapshot_revision": self._revision(session),
                    "keepalive": {
                        "endpoint_id": endpoint.id,
                        "gpu_id": gpu.id,
                        "enabled": False,
                        "lease_id": None,
                        "state": "OFF",
                    },
                }
            return {
                "event_id": None,
                "snapshot_revision": self._revision(session),
                "keepalive": self._keepalive_lease_summary(lease, endpoint.id, gpu.id),
            }

        return self._read(operation)

    def finalize_keepalive_stop(
        self,
        actor: ActorContext,
        endpoint_id: str,
        lease_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Release one stopped GPU occupancy worker."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            if idempotency_key is not None:
                existing = self._idempotent(
                    session,
                    actor=actor,
                    action="keepalive.finalize_stop_gpu",
                    key=idempotency_key,
                )
                if existing is not None:
                    return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            lease = session.get(Lease, lease_id)
            if lease is None or lease.kind != "keepalive":
                raise BrokerError(
                    "keepalive_not_found", "keepalive lease does not exist", status_code=404
                )
            if lease.state not in {"HELD", "ACTIVE", "CONFLICT", "ORPHANED_BUSY"}:
                raise BrokerError(
                    "keepalive_not_stoppable",
                    "keepalive lease is already terminal",
                    status_code=409,
                )
            resources = self._keepalive_resources(session, lease.id)
            if len(resources) != 1:
                raise BrokerError(
                    "keepalive_gpu_invalid",
                    "keepalive lease has an invalid active resource scope",
                    status_code=409,
                )
            gpus = [session.get(GPUDevice, resource.gpu_id) for resource in resources]
            if any(gpu is None or gpu.endpoint_id != endpoint.id for gpu in gpus):
                raise BrokerError(
                    "keepalive_endpoint_mismatch",
                    "keepalive lease does not belong to this endpoint",
                    status_code=409,
                )
            gpu_ids = [resource.gpu_id for resource in resources]
            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint.id,
                gpu_ids=gpu_ids,
                observation_not_before=observation_not_before,
            )
            if processes:
                raise BrokerError(
                    "keepalive_process_still_running",
                    "keepalive ownership remains held while target processes are observed",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = "keepalive stopped and target GPU observed empty"
            for resource in resources:
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            self._set_keepalive_current(session, gpu_ids[0], "OFF", now=now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="keepalive.gpu_stopped",
                resource_type="gpu",
                resource_id=gpu_ids[0],
                result="success",
                summary={"lease_id": lease.id},
                now=now,
            )
            self._allocate_queued(session, now, revision)
            keepalive = self._keepalive_lease_summary(lease, endpoint.id, gpu_ids[0])
            result = {"event_id": event.id, "snapshot_revision": revision, "keepalive": keepalive}
            if idempotency_key is not None:
                self._remember_idempotency(
                    session,
                    actor=actor,
                    action="keepalive.finalize_stop_gpu",
                    key=idempotency_key,
                    response=result,
                    now=now,
                )
            return result

        return self._write(operation)

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
                or_(
                    MaintenanceWindow.gpu_id == gpu.id,
                    MaintenanceWindow.endpoint_id == gpu.endpoint_id,
                ),
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
        if endpoint is None or not gpu.enabled:
            return "DISABLED", "endpoint or GPU is disabled"
        if not gpu.present:
            return "UNKNOWN_STALE", "GPU absent from latest complete endpoint observation"
        if endpoint.lifecycle_state == "draining":
            return "DRAINING", "endpoint is draining and blocks new claims"
        if not endpoint.enabled:
            return "DISABLED", "endpoint is disabled"
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
        if lease is not None and lease.kind == "keepalive":
            keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
            if keepalive_state == "ON":
                return "KEEPALIVE", "per-GPU keepalive is active"
            if keepalive_state == "OFF" and not self._current_processes(session, gpu.id, now):
                return "AVAILABLE", None
            return "CONFLICT", keepalive_reason
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
        endpoint = session.get(Endpoint, gpu.endpoint_id)
        assert endpoint is not None
        keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
        keepalive_state, keepalive_reason = self._policy_keepalive_status(
            endpoint,
            state,
            keepalive_state,
            keepalive_reason,
        )
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
            "present": gpu.present,
            "state": state,
            "state_reason": reason,
            "first_seen_at": _iso(gpu.first_seen_at),
            "last_seen_at": _iso(gpu.last_seen_at),
            "absent_at": _iso(gpu.absent_at),
            "telemetry": self._telemetry_dict(telemetry),
            "processes": [self._process_dict(process) for process in processes],
            "lease": (
                self._lease_dict(session, lease)
                if lease is not None and lease.kind != "keepalive"
                else None
            ),
            "keepalive": {
                "configured": endpoint.keepalive_adapter_id is not None,
                "policy": endpoint.keepalive_policy,
                "desired": "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF",
                "actual": keepalive_state,
                "state": keepalive_state,
                "reason": keepalive_reason,
                "lease_id": lease.id if lease is not None and lease.kind == "keepalive" else None,
            },
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
            endpoint_gpus = (
                session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id.in_(visible_ids))
                    .order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
                ).all()
                if visible_ids
                else []
            )
            gpus = [gpu for gpu in endpoint_gpus if gpu.present]
            absent_gpu_ids = [gpu.id for gpu in endpoint_gpus if not gpu.present]
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
                                select(TelemetrySnapshot).where(
                                    TelemetrySnapshot.id.in_(latest_ids)
                                )
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
            visible_leases = [lease for lease in all_leases if lease.kind != "keepalive"]
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
                        select(AllocationRequest).where(
                            AllocationRequest.id.in_(active_request_ids)
                        )
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
            current_reservation_by_gpu: dict[str, Reservation] = {}
            endpoint_payloads: list[dict[str, Any]] = []

            def endpoint_snapshot(endpoint: Endpoint) -> dict[str, Any]:
                provider_state = provider_states.get(endpoint.id)
                last_success = _as_utc(provider_state.last_success_at) if provider_state else None
                if endpoint.lifecycle_state == "draining":
                    monitor_status = "DRAINING"
                elif not endpoint.enabled:
                    monitor_status = "DISABLED"
                elif provider_state is None:
                    monitor_status = "PENDING"
                elif (
                    provider_state.last_error == "incomplete endpoint observation"
                    and gpu_counts[endpoint.id] == 0
                    and endpoint.expected_gpu_count is None
                    and host_telemetry_by_endpoint.get(endpoint.id) is not None
                ):
                    # A host-only probe remains useful and healthy even when
                    # NVIDIA runtime data is unavailable.  Keepalive-enabled
                    # GPU endpoints still require a complete observation.
                    monitor_status = "ONLINE"
                elif last_success is None or provider_state.last_error:
                    monitor_status = "ERROR"
                elif now - last_success > timedelta(
                    seconds=self.inventory.collector.stale_after_seconds
                ):
                    monitor_status = "STALE"
                else:
                    monitor_status = "ONLINE"
                return {
                    **self._endpoint_dict(endpoint),
                    # Filled from the final per-GPU projection below, so a
                    # busy sibling GPU never invalidates an independent
                    # keepalive worker.
                    "keepalive": self._keepalive_aggregate(endpoint, (), eligible_idle_gpu_count=0),
                    "host_telemetry": self._host_telemetry_dict(
                        host_telemetry_by_endpoint.get(endpoint.id)
                    ),
                    "monitor": {
                        "status": monitor_status,
                        "gpu_count": gpu_counts[endpoint.id],
                        "absent_gpu_count": sum(
                            1
                            for gpu in endpoint_gpus
                            if gpu.endpoint_id == endpoint.id and not gpu.present
                        ),
                        "last_success_at": _iso(provider_state.last_success_at)
                        if provider_state
                        else None,
                        "last_attempt_at": _iso(provider_state.last_attempt_at)
                        if provider_state
                        else None,
                        "last_error": provider_state.last_error if provider_state else None,
                    },
                }

            endpoint_payloads = [endpoint_snapshot(endpoint) for endpoint in visible_endpoints]

            keepalive_current_by_gpu = {
                current.gpu_id: current
                for current in session.scalars(
                    select(KeepaliveCurrent).where(KeepaliveCurrent.gpu_id.in_(gpu_ids))
                    if gpu_ids
                    else select(KeepaliveCurrent).where(text("0 = 1"))
                ).all()
            }

            def derive_keepalive(gpu: GPUDevice, lease: Lease | None) -> tuple[str, str | None]:
                current = keepalive_current_by_gpu.get(gpu.id)
                if lease is None or lease.kind != "keepalive":
                    return (
                        (current.actual, current.error_reason)
                        if current is not None
                        else ("OFF", None)
                    )
                resources = [
                    resource for resource in resources_by_lease[lease.id] if resource.active
                ]
                if len(resources) != 1 or resources[0].gpu_id != gpu.id:
                    return "ERROR", "占卡记录没有准确对应这一张 GPU"
                if lease.state != "ACTIVE":
                    return "ERROR", "占卡记录未处于可用状态"
                if current is not None:
                    return current.actual, current.error_reason
                if processes_by_gpu[gpu.id]:
                    return "ERROR", "占卡进程身份尚未建立"
                return "OFF", None

            def derive_state(gpu: GPUDevice) -> tuple[str, str | None]:
                endpoint = next(item for item in visible_endpoints if item.id == gpu.endpoint_id)
                if not gpu.enabled:
                    return "DISABLED", "endpoint or GPU is disabled"
                if not gpu.present:
                    return "UNKNOWN_STALE", "GPU absent from latest complete endpoint observation"
                if endpoint.lifecycle_state == "draining":
                    return "DRAINING", "endpoint is draining and blocks new claims"
                if not endpoint.enabled:
                    return "DISABLED", "endpoint is disabled"
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
                if lease is not None and lease.kind == "keepalive":
                    keepalive_state, keepalive_reason = derive_keepalive(gpu, lease)
                    if keepalive_state == "ON":
                        return "KEEPALIVE", "occupancy is active"
                    if keepalive_state == "OFF" and not processes_by_gpu[gpu.id]:
                        return "AVAILABLE", None
                    return "CONFLICT", keepalive_reason
                processes = processes_by_gpu[gpu.id]
                if processes:
                    known = binding_keys.get(lease.id, set()) if lease else set()
                    if (
                        lease is not None
                        and known
                        and all(self._process_key(process) in known for process in processes)
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
                keepalive_state, keepalive_reason = derive_keepalive(gpu, lease)
                endpoint = next(item for item in visible_endpoints if item.id == gpu.endpoint_id)
                keepalive_state, keepalive_reason = self._policy_keepalive_status(
                    endpoint,
                    gpu_state,
                    keepalive_state,
                    keepalive_reason,
                )
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
                    "present": gpu.present,
                    "state": gpu_state,
                    "state_reason": reason,
                    "first_seen_at": _iso(gpu.first_seen_at),
                    "last_seen_at": _iso(gpu.last_seen_at),
                    "absent_at": _iso(gpu.absent_at),
                    "telemetry": self._telemetry_dict(telemetry),
                    "processes": [self._process_dict(process) for process in processes],
                    "lease": (
                        lease_payloads.get(lease.id)
                        if lease is not None and lease.kind != "keepalive"
                        else None
                    ),
                    "keepalive": {
                        "configured": endpoint.keepalive_adapter_id is not None,
                        "policy": endpoint.keepalive_policy,
                        "desired": (
                            "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF"
                        ),
                        "actual": keepalive_state,
                        "state": keepalive_state,
                        "reason": keepalive_reason,
                        "lease_id": (
                            lease.id if lease is not None and lease.kind == "keepalive" else None
                        ),
                    },
                }
                gpu_payloads.append(payload)

            endpoint_monitor_by_id = {
                endpoint["id"]: endpoint["monitor"]["status"] for endpoint in endpoint_payloads
            }
            for payload in gpu_payloads:
                payload.update(
                    self._gpu_public_projection(
                        payload,
                        monitor_status=endpoint_monitor_by_id[payload["endpoint_id"]],
                    )
                )

            endpoint_by_id = {endpoint.id: endpoint for endpoint in visible_endpoints}
            for endpoint_payload in endpoint_payloads:
                endpoint_id_value = endpoint_payload["id"]
                endpoint = endpoint_by_id[endpoint_id_value]
                endpoint_gpu_payloads = [
                    item for item in gpu_payloads if item["endpoint_id"] == endpoint_id_value
                ]
                endpoint_payload["keepalive"] = self._keepalive_aggregate(
                    endpoint,
                    [
                        {
                            "gpu_id": item["id"],
                            "state": item["keepalive"]["state"],
                            "reason": item["keepalive"]["reason"],
                        }
                        for item in endpoint_gpu_payloads
                    ],
                    eligible_idle_gpu_count=(
                        sum(item["state"] == "AVAILABLE" for item in endpoint_gpu_payloads)
                        if (
                            endpoint.keepalive_policy == "idle_keepalive"
                            and endpoint.keepalive_adapter_id is not None
                        )
                        else 0
                    ),
                )

            running_lease_ids = {
                item["lease"]["id"]
                for item in gpu_payloads
                if item["state"] == "RUNNING_MANAGED" and item["lease"] is not None
            }
            for lease_id, payload in lease_payloads.items():
                payload["runtime_state"] = (
                    "RUNNING" if lease_id in running_lease_ids else "ASSIGNED"
                )

            all_gpu_payloads = gpu_payloads
            counts = defaultdict(int)
            for gpu in all_gpu_payloads:
                counts[gpu["state"]] += 1
            workload_claimed_gpu_count = sum(item["lease"] is not None for item in all_gpu_payloads)
            keepalive_owned_gpu_count = sum(
                item["keepalive"]["lease_id"] is not None for item in all_gpu_payloads
            )
            claimed_states = {
                "HELD",
                "LEASED_IDLE",
                "RUNNING_MANAGED",
                "ORPHANED_BUSY",
                "CONFLICT",
            }
            abnormal_states = {
                "UNKNOWN_RECOVERING",
                "UNKNOWN_STALE",
                "UNHEALTHY",
                "CONFLICT",
                "ORPHANED_BUSY",
            }
            endpoint_attention_statuses = {"ERROR", "STALE"}
            endpoint_attention_status_counts: dict[str, int] = defaultdict(int)
            for endpoint in endpoint_payloads:
                status = endpoint["monitor"]["status"]
                if status in endpoint_attention_statuses:
                    endpoint_attention_status_counts[status] += 1
            gpu_attention_states = abnormal_states | {"BUSY_UNMANAGED"}
            gpu_attention_state_counts = {
                state: counts[state] for state in sorted(gpu_attention_states) if counts[state]
            }
            attention_endpoint_count = sum(endpoint_attention_status_counts.values())
            attention_gpu_count = sum(gpu_attention_state_counts.values())
            summary = {
                "online_servers": sum(
                    endpoint["monitor"]["status"] == "ONLINE" for endpoint in endpoint_payloads
                ),
                "total_servers": len(endpoint_payloads),
                "total_gpus": len(all_gpu_payloads),
                "available_gpus": sum(
                    self._gpu_payload_is_publicly_available(item) for item in all_gpu_payloads
                ),
                "busy_gpus": counts["BUSY_UNMANAGED"] + counts["RUNNING_MANAGED"],
                "claimed_gpus": sum(
                    item["state"] in claimed_states
                    and not self._gpu_payload_is_publicly_available(item)
                    for item in all_gpu_payloads
                ),
                "workload_claimed_gpus": workload_claimed_gpu_count,
                "keepalive_owned_gpus": keepalive_owned_gpu_count,
                "verified_keepalive_gpus": counts["KEEPALIVE"],
                "abnormal_gpus": sum(counts[item] for item in abnormal_states),
                "attention": {
                    "endpoint_count": attention_endpoint_count,
                    "endpoint_status_counts": dict(
                        sorted(endpoint_attention_status_counts.items())
                    ),
                    "gpu_count": attention_gpu_count,
                    "absent_gpu_count": len(absent_gpu_ids),
                    "gpu_state_counts": gpu_attention_state_counts,
                    "unmanaged_gpu_count": counts["BUSY_UNMANAGED"],
                    "total_resource_count": attention_endpoint_count
                    + attention_gpu_count
                    + len(absent_gpu_ids),
                },
            }
            if not absent_gpu_ids:
                summary["attention"].pop("absent_gpu_count")
            ages = [
                max(0.0, (now - (_as_utc(item.observed_at) or now)).total_seconds())
                for item in telemetry_by_gpu.values()
            ]

            requested_state = "AVAILABLE" if only_available else state.upper() if state else None
            if requested_state == "AVAILABLE":
                gpu_payloads = [
                    item for item in gpu_payloads if self._gpu_payload_is_publicly_available(item)
                ]
            elif requested_state:
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
                        "publicly_available": item["publicly_available"],
                        "public_status": item["public_status"],
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

            visible_gpu_ids = {gpu.id for gpu in endpoint_gpus}

            # The desktop consumes one revision-consistent snapshot.  Include the
            # generic CPU/memory resource records here instead of making the GUI
            # stitch together several REST reads from different revisions.
            providers = [
                provider
                for provider in session.scalars(
                    select(ResourceProvider).order_by(ResourceProvider.id)
                ).all()
                if provider.endpoint_id is None or provider.endpoint_id in visible_ids
            ]
            provider_ids = {provider.id for provider in providers}
            units = (
                session.scalars(
                    select(AllocatableUnit)
                    .where(AllocatableUnit.provider_id.in_(provider_ids))
                    .order_by(AllocatableUnit.id)
                ).all()
                if provider_ids
                else []
            )
            unit_by_id = {unit.id: unit for unit in units}
            units_by_provider: dict[str, list[AllocatableUnit]] = defaultdict(list)
            for unit in units:
                units_by_provider[unit.provider_id].append(unit)

            resource_claim_query = select(ResourceClaimModel).where(
                ResourceClaimModel.state.in_(("active", "blocked"))
            )
            if not actor.is_admin:
                resource_claim_query = resource_claim_query.where(
                    or_(
                        ResourceClaimModel.actor_id == actor.id,
                        ResourceClaimModel.project_id.in_(actor.project_ids),
                    )
                )
            visible_resource_claims = session.scalars(
                resource_claim_query.order_by(ResourceClaimModel.created_at.desc())
            ).all()
            visible_resource_claim_ids = {claim.id for claim in visible_resource_claims}
            all_resource_allocations = session.scalars(
                select(ResourceAllocation).order_by(
                    ResourceAllocation.created_at.desc(), ResourceAllocation.id.desc()
                )
            ).all()
            allocations_by_claim: dict[str, list[ResourceAllocation]] = defaultdict(list)
            active_allocations_by_provider: dict[str, list[ResourceAllocation]] = defaultdict(list)
            for allocation in all_resource_allocations:
                if allocation.claim_id in visible_resource_claim_ids:
                    allocations_by_claim[allocation.claim_id].append(allocation)
                unit = unit_by_id.get(allocation.unit_id)
                if allocation.state == "active" and unit is not None:
                    active_allocations_by_provider[unit.provider_id].append(allocation)

            def summed_quantities(values: Iterable[dict[str, Any]]) -> dict[str, Any]:
                total = {
                    "cpu_cores": 0.0,
                    "memory_mib": 0,
                    "gpu_count": 0,
                    "nodes": 0,
                    "scheduler_units": 0,
                }
                for value in values:
                    total["cpu_cores"] += float(value.get("cpu_cores") or 0.0)
                    total["memory_mib"] += int(value.get("memory_mib") or 0)
                    total["gpu_count"] += int(value.get("gpu_count") or 0)
                    total["nodes"] += int(value.get("nodes") or value.get("node_count") or 0)
                    total["scheduler_units"] += int(value.get("scheduler_units") or 0)
                total["cpu_cores"] = round(total["cpu_cores"], 1)
                return total

            endpoint_payload_by_id = {item["id"]: item for item in endpoint_payloads}
            direct_commitments_by_endpoint = self._endpoint_commitment_usage(session)
            resource_provider_payloads: list[dict[str, Any]] = []
            for provider in providers:
                payload = self._provider_dict(provider)
                provider_units = units_by_provider.get(provider.id, [])
                total = summed_quantities(
                    {
                        "cpu_cores": unit.total_cpu_cores,
                        "memory_mib": unit.total_memory_mib,
                        "gpu_count": unit.total_gpu_count,
                    }
                    for unit in provider_units
                    if unit.enabled
                )
                committed = summed_quantities(
                    json_load(allocation.quantities_json)
                    for allocation in active_allocations_by_provider.get(provider.id, [])
                )
                available = {
                    "cpu_cores": max(0.0, total["cpu_cores"] - committed["cpu_cores"]),
                    "memory_mib": max(0, total["memory_mib"] - committed["memory_mib"]),
                    "gpu_count": max(0, total["gpu_count"] - committed["gpu_count"]),
                    "nodes": max(0, total["nodes"] - committed["nodes"]),
                    "scheduler_units": max(
                        0, total["scheduler_units"] - committed["scheduler_units"]
                    ),
                }
                endpoint = endpoint_payload_by_id.get(provider.endpoint_id or "")
                telemetry = host_telemetry_by_endpoint.get(provider.endpoint_id or "")
                if provider.provider_type == "host-capacity" and endpoint is not None:
                    direct_cpu, direct_memory = direct_commitments_by_endpoint.get(
                        provider.endpoint_id or "", (0.0, 0)
                    )
                    total.update(
                        cpu_cores=float(telemetry.cpu_count if telemetry else 0.0),
                        memory_mib=int(telemetry.memory_total_mib if telemetry else 0),
                    )
                    committed.update(
                        cpu_cores=round(committed["cpu_cores"] + direct_cpu, 1),
                        memory_mib=committed["memory_mib"] + direct_memory,
                    )
                    available.update(
                        cpu_cores=round(
                            max(
                                0.0,
                                (telemetry.cpu_count - telemetry.load_1m if telemetry else 0.0)
                                - committed["cpu_cores"],
                            ),
                            1,
                        ),
                        memory_mib=max(
                            0,
                            (telemetry.memory_available_mib if telemetry else 0)
                            - committed["memory_mib"],
                        ),
                    )
                    payload["state"] = endpoint["monitor"]["status"]
                    payload["observed_at"] = _iso(telemetry.observed_at) if telemetry else None
                elif endpoint is not None:
                    payload["state"] = endpoint["monitor"]["status"]
                else:
                    payload["state"] = "PENDING" if provider.enabled else "DISABLED"
                payload.update(total=total, committed=committed, available=available)
                resource_provider_payloads.append(payload)

            host_capacity_payloads: list[dict[str, Any]] = []
            host_provider_by_endpoint = {
                provider.endpoint_id: provider
                for provider in providers
                if provider.provider_type == "host-capacity" and provider.endpoint_id is not None
            }
            for endpoint in visible_endpoints:
                telemetry = host_telemetry_by_endpoint.get(endpoint.id)
                endpoint_payload = endpoint_payload_by_id[endpoint.id]
                monitor_status = endpoint_payload["monitor"]["status"]
                observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
                telemetry_stale = (
                    telemetry is None
                    or observed_at is None
                    or now - observed_at
                    > timedelta(seconds=self.inventory.collector.stale_after_seconds)
                )
                provider = host_provider_by_endpoint.get(endpoint.id)
                unit = (
                    next(
                        (
                            candidate
                            for candidate in units_by_provider.get(provider.id, [])
                            if candidate.unit_type == "host"
                        ),
                        None,
                    )
                    if provider is not None
                    else None
                )
                direct_cpu, direct_memory = direct_commitments_by_endpoint.get(
                    endpoint.id, (0.0, 0)
                )
                generic = summed_quantities(
                    json_load(allocation.quantities_json)
                    for allocation in active_allocations_by_provider.get(
                        provider.id if provider is not None else "", []
                    )
                )
                generic_cpu = generic["cpu_cores"]
                generic_memory = generic["memory_mib"]
                if telemetry is None:
                    available_cpu = None
                    available_memory = None
                else:
                    available_cpu = max(
                        0.0,
                        telemetry.cpu_count - telemetry.load_1m - direct_cpu - generic_cpu,
                    )
                    available_memory = max(
                        0,
                        telemetry.memory_available_mib - direct_memory - generic_memory,
                    )
                monitor_reason = {
                    "DRAINING": "endpoint is draining and blocks new claims",
                    "DISABLED": "endpoint is disabled",
                    "PENDING": "no successful collector observation",
                    "STALE": "collector success is stale",
                }.get(monitor_status)
                if monitor_status == "ERROR":
                    monitor_reason = (
                        endpoint_payload["monitor"].get("last_error")
                        or "collector has no successful observation"
                    )
                admission_state = "available"
                admission_reason = None
                if monitor_status != "ONLINE":
                    admission_state = "blocked"
                    admission_reason = monitor_reason or monitor_status.lower()
                elif telemetry_stale:
                    admission_state = "blocked"
                    admission_reason = "host telemetry is stale"
                elif available_cpu == 0 and available_memory == 0:
                    admission_state = "blocked"
                    admission_reason = "no uncommitted CPU or memory capacity"
                host_capacity_payloads.append(
                    {
                        "provider": self._provider_dict(provider) if provider is not None else None,
                        "unit": self._allocatable_unit_dict(unit) if unit is not None else None,
                        "endpoint": self._endpoint_dict(endpoint),
                        "monitor_status": monitor_status,
                        "admission_state": admission_state,
                        "admission_reason": admission_reason,
                        "telemetry": self._host_telemetry_dict(telemetry),
                        "capacity": {
                            "total_cpu_cores": telemetry.cpu_count if telemetry else None,
                            "observed_available_cpu_cores": round(
                                max(0.0, telemetry.cpu_count - telemetry.load_1m), 1
                            )
                            if telemetry
                            else None,
                            "available_cpu_cores": round(available_cpu, 1)
                            if available_cpu is not None
                            else None,
                            "total_memory_mib": telemetry.memory_total_mib if telemetry else None,
                            "observed_available_memory_mib": telemetry.memory_available_mib
                            if telemetry
                            else None,
                            "available_memory_mib": available_memory,
                            "committed_cpu_cores": round(direct_cpu + generic_cpu, 1),
                            "committed_memory_mib": direct_memory + generic_memory,
                            "direct_lease_cpu_cores": round(direct_cpu, 1),
                            "direct_lease_memory_mib": direct_memory,
                            "generic_claim_cpu_cores": round(generic_cpu, 1),
                            "generic_claim_memory_mib": generic_memory,
                        },
                    }
                )

            resource_claim_payloads: list[dict[str, Any]] = []
            for claim in visible_resource_claims:
                claim_allocations = allocations_by_claim.get(claim.id, [])
                active_claim_allocations = [
                    allocation for allocation in claim_allocations if allocation.state == "active"
                ]
                native_lease_ids = sorted(
                    {
                        allocation.native_lease_id
                        for allocation in claim_allocations
                        if allocation.native_lease_id is not None
                    }
                )
                native_request_ids = sorted(
                    {
                        lease_by_id[lease_id].request_id
                        for lease_id in native_lease_ids
                        if lease_id in lease_by_id
                    }
                )
                allocation_quantities = summed_quantities(
                    json_load(allocation.quantities_json) for allocation in active_claim_allocations
                )
                payload = self._resource_claim_dict(claim)
                payload.update(
                    quantities=(
                        allocation_quantities
                        if active_claim_allocations
                        else json_load(claim.requested_quantities_json)
                    ),
                    native_lease_ids=native_lease_ids,
                    native_request_ids=native_request_ids,
                    runtime_state=(
                        "RUNNING"
                        if running_lease_ids.intersection(native_lease_ids)
                        else "ASSIGNED"
                        if claim.state == "active"
                        else "REQUESTED"
                        if claim.state == "blocked"
                        else claim.state.upper()
                    ),
                    allocations=[
                        self._resource_allocation_dict(
                            allocation,
                            unit=unit_by_id.get(allocation.unit_id),
                        )
                        for allocation in claim_allocations
                    ],
                )
                resource_claim_payloads.append(payload)

            evaluation_query = select(ResourcePlanEvaluation)
            if not actor.is_admin:
                evaluation_query = evaluation_query.where(
                    or_(
                        ResourcePlanEvaluation.actor_id == actor.id,
                        ResourcePlanEvaluation.project_id.in_(actor.project_ids),
                    )
                )
            evaluations = session.scalars(
                evaluation_query.order_by(ResourcePlanEvaluation.created_at.desc()).limit(
                    RESOURCE_SNAPSHOT_HISTORY_LIMIT
                )
            ).all()
            candidates_by_evaluation: dict[str, list[ResourcePlanCandidateModel]] = defaultdict(
                list
            )
            if evaluations:
                for candidate in session.scalars(
                    select(ResourcePlanCandidateModel)
                    .where(
                        ResourcePlanCandidateModel.evaluation_id.in_(
                            [evaluation.id for evaluation in evaluations]
                        )
                    )
                    .order_by(
                        ResourcePlanCandidateModel.evaluation_id,
                        ResourcePlanCandidateModel.id,
                    )
                ).all():
                    candidates_by_evaluation[candidate.evaluation_id].append(candidate)
            actual_query = select(ResourceRunActual)
            if not actor.is_admin:
                actual_query = actual_query.where(
                    or_(
                        ResourceRunActual.actor_id == actor.id,
                        ResourceRunActual.project_id.in_(actor.project_ids),
                    )
                )
            actuals = session.scalars(
                actual_query.order_by(ResourceRunActual.created_at.desc()).limit(
                    RESOURCE_SNAPSHOT_HISTORY_LIMIT
                )
            ).all()

            alert_payloads = [
                self._alert_dict(alert)
                for alert in session.scalars(
                    select(Alert)
                    .where(Alert.active.is_(True))
                    .order_by(Alert.severity, Alert.last_seen_at.desc(), Alert.id)
                ).all()
            ]
            scheduler_target_payloads = [
                self._scheduler_target_dict(target)
                for target in session.scalars(
                    select(SchedulerTarget)
                    .where(SchedulerTarget.enabled.is_(True))
                    .order_by(SchedulerTarget.id)
                ).all()
            ]
            scheduler_jobs = [
                job
                for job in session.scalars(
                    select(SchedulerJob)
                    .where(SchedulerJob.state.not_in(TERMINAL_SCHEDULER_JOB_STATES))
                    .order_by(SchedulerJob.created_at.desc())
                ).all()
                if self._scheduler_job_visible(actor, job)
            ]
            job_events_by_id: dict[str, list[SchedulerJobEvent]] = defaultdict(list)
            if scheduler_jobs:
                for event in session.scalars(
                    select(SchedulerJobEvent)
                    .where(SchedulerJobEvent.job_id.in_([job.id for job in scheduler_jobs]))
                    .order_by(SchedulerJobEvent.job_id, SchedulerJobEvent.id)
                ).all():
                    job_events_by_id[event.job_id].append(event)
            scheduler_transfer_payloads = [
                self._scheduler_transfer_dict(transfer)
                for transfer in session.scalars(
                    select(SchedulerTransfer)
                    .where(SchedulerTransfer.state.not_in(TERMINAL_SCHEDULER_TRANSFER_STATES))
                    .order_by(SchedulerTransfer.created_at.desc())
                ).all()
                if self._scheduler_transfer_visible(actor, transfer)
            ]
            profiles = session.scalars(
                select(WorkloadProfile)
                .where(WorkloadProfile.enabled.is_(True))
                .order_by(WorkloadProfile.project_id, WorkloadProfile.id)
            ).all()
            grants_by_profile_id: dict[str, list[str]] = defaultdict(list)
            if profiles:
                for grant in session.scalars(
                    select(WorkloadProfileGrant)
                    .where(
                        WorkloadProfileGrant.profile_id.in_([profile.id for profile in profiles])
                    )
                    .order_by(WorkloadProfileGrant.profile_id, WorkloadProfileGrant.project_id)
                ).all():
                    grants_by_profile_id[grant.profile_id].append(grant.project_id)
            workload_profile_payloads = []
            for profile in profiles:
                grants = grants_by_profile_id[profile.id]
                if actor.is_admin:
                    profile_visible = True
                else:
                    profile_visible = any(
                        profile.project_id == project_id
                        or profile.grant_all_projects
                        or project_id in grants
                        for project_id in actor.project_ids
                    )
                if profile_visible:
                    workload_profile_payloads.append(self._workload_profile_dict(profile, grants))

            visible_lease_payloads = [
                lease_payloads[lease.id]
                for lease in visible_leases
                if any(
                    resource.active and resource.gpu_id in visible_gpu_ids
                    for resource in resources_by_lease[lease.id]
                )
            ]
            resource_actual_payloads = [self._resource_actual_dict(actual) for actual in actuals]
            resource_usage_revision = str(self._revision(session))
            active_host_capacity = [
                card for card in host_capacity_payloads if card["admission_state"] == "available"
            ]
            resource_projection = {
                "capacity": {
                    "gpu_count": len(all_gpu_payloads),
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["total_cpu_cores"] or 0.0
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["total_memory_mib"] or 0 for card in host_capacity_payloads
                    ),
                    "vram_mib": sum(gpu["total_vram_mib"] for gpu in all_gpu_payloads),
                },
                "used": {
                    "gpu_count": counts["BUSY_UNMANAGED"] + counts["RUNNING_MANAGED"],
                    "cpu_cores": round(
                        sum(
                            (card["telemetry"] or {}).get("load_1m") or 0.0
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        max(
                            0,
                            (card["capacity"]["total_memory_mib"] or 0)
                            - (card["capacity"]["observed_available_memory_mib"] or 0),
                        )
                        for card in host_capacity_payloads
                    ),
                    "vram_mib": sum(
                        (gpu["telemetry"] or {}).get("memory_used_mib") or 0
                        for gpu in all_gpu_payloads
                    ),
                },
                "claimed": {
                    "gpu_count": summary["claimed_gpus"],
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["committed_cpu_cores"] or 0.0
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["committed_memory_mib"] or 0
                        for card in host_capacity_payloads
                    ),
                    "lease_count": len(visible_lease_payloads),
                    "claim_count": len(resource_claim_payloads),
                },
                "available": {
                    "gpu_count": summary["available_gpus"],
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["available_cpu_cores"] or 0.0
                            for card in active_host_capacity
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["available_memory_mib"] or 0
                        for card in active_host_capacity
                    ),
                    "host_capacity_units": len(active_host_capacity),
                },
                "attention": summary["attention"],
                "semantics": {
                    "available_is_authoritative": True,
                    "used_and_claimed_may_overlap": True,
                    "fail_closed_states": [
                        "BUSY_UNMANAGED",
                        "CONFLICT",
                        "MAINTENANCE",
                        "ORPHANED_BUSY",
                        "UNKNOWN_RECOVERING",
                        "UNKNOWN_STALE",
                        "UNHEALTHY",
                    ],
                },
            }

            data = {
                "summary": summary,
                "resource_projection": resource_projection,
                "data_age_seconds": round(max(ages), 1) if ages else None,
                "endpoints": endpoint_payloads,
                "gpus": gpu_payloads,
                "absent_gpu_ids": absent_gpu_ids,
                "leases": visible_lease_payloads,
                "requests": [],
                "reservations": [],
                "maintenance": [],
                "alerts": alert_payloads,
                "resource_providers": resource_provider_payloads,
                "allocatable_units": [
                    {
                        **self._allocatable_unit_dict(unit),
                        "quantities": {
                            "cpu_cores": unit.total_cpu_cores or 0.0,
                            "memory_mib": unit.total_memory_mib or 0,
                            "gpu_count": unit.total_gpu_count,
                        },
                    }
                    for unit in units
                ],
                "host_capacity": host_capacity_payloads,
                "resource_claims": resource_claim_payloads,
                "scheduler_targets": scheduler_target_payloads,
                "scheduler_jobs": [
                    self._scheduler_job_dict(job, job_events_by_id[job.id])
                    for job in scheduler_jobs
                ],
                "scheduler_transfers": scheduler_transfer_payloads,
                "workload_profiles": workload_profile_payloads,
                "resource_plan_evaluations": [
                    self._resource_plan_evaluation_dict(
                        evaluation,
                        candidates_by_evaluation[evaluation.id],
                    )
                    for evaluation in evaluations
                ],
                "resource_run_actuals": resource_actual_payloads,
                "resource_usage_revision": resource_usage_revision,
                "freshness_seconds": self.inventory.collector.stale_after_seconds,
                "admission_boundary": "ServerPilot 只记录资源分配，不会在服务器上启动或停止任务。",
            }
            return self.envelope(session, data)

        return self._read(operation)

    def control_plane_state(self, actor: ActorContext) -> dict[str, Any]:
        snapshot = self.snapshot(actor)
        data = snapshot["data"]
        current_keys = (
            "summary",
            "resource_projection",
            "data_age_seconds",
            "freshness_seconds",
            "endpoints",
            "gpus",
            "absent_gpu_ids",
            "leases",
            "requests",
            "reservations",
            "maintenance",
            "alerts",
            "resource_providers",
            "allocatable_units",
            "host_capacity",
            "resource_claims",
            "scheduler_targets",
            "scheduler_jobs",
            "scheduler_transfers",
            "workload_profiles",
            "admission_boundary",
        )
        history_keys = (
            "resource_plan_evaluations",
            "resource_run_actuals",
        )
        current = {key: data[key] for key in current_keys}
        if not actor.is_admin:

            def visible_project_item(item: dict[str, Any]) -> bool:
                return (
                    item.get("actor_id") == actor.id or item.get("project_id") in actor.project_ids
                )

            current["leases"] = [
                lease for lease in current["leases"] if visible_project_item(lease)
            ]
            current["requests"] = [
                request for request in current["requests"] if visible_project_item(request)
            ]
            current["reservations"] = [
                reservation
                for reservation in current["reservations"]
                if visible_project_item(reservation)
            ]
            visible_lease_ids = {lease["id"] for lease in current["leases"]}
            current["gpus"] = [
                {
                    **gpu,
                    "lease": (
                        gpu["lease"]
                        if gpu.get("lease") is None or gpu["lease"]["id"] in visible_lease_ids
                        else None
                    ),
                }
                for gpu in current["gpus"]
            ]
        return {
            "schema_version": snapshot["schema_version"],
            "snapshot_revision": snapshot["snapshot_revision"],
            "server_time": snapshot["server_time"],
            "data": {
                "current": current,
                "history": {key: data[key] for key in history_keys},
            },
        }

    def coordination(self, actor: ActorContext) -> dict[str, Any]:
        """Return an agent-readable shared coordination board from one broker snapshot.

        This is intentionally observational: the broker already owns fair queue
        ordering and placement. Agents use this board to see current consumers,
        real process attribution, and remaining capacity without appointing a
        separate scheduler or inspecting servers themselves.
        """

        snapshot = self.snapshot(actor, compact=False)
        data = snapshot["data"]
        scheduler_targets = self.list_scheduler_targets(actor)["data"]
        scheduler_jobs = self.list_scheduler_jobs(actor)["data"]
        active_scheduler_jobs = [
            job
            for job in scheduler_jobs
            if job["state"] not in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
        ]
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
                    {
                        "run_id": workload["run_id"],
                        "process_key_count": len(workload["process_keys"]),
                    }
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
                    "active_workload_leases": 0,
                    "leased_gpus": 0,
                    "managed_running_gpus": 0,
                    "idle_leased_gpus": 0,
                    "projects": set(),
                    "servers": set(),
                },
            )
            agent["active_leases"] += 1
            agent["active_workload_leases"] += 1
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
            endpoint_telemetry = [
                gpu["telemetry"] for gpu in endpoint_gpus if gpu["telemetry"] is not None
            ]
            state_counts = gpu_state_counts(endpoint_gpus)
            workload_leased_gpu_count = sum(gpu["lease"] is not None for gpu in endpoint_gpus)
            keepalive_owned_gpu_count = sum(
                gpu["keepalive"]["lease_id"] is not None for gpu in endpoint_gpus
            )
            host = endpoint["host_telemetry"]
            server_cards.append(
                {
                    "server_id": endpoint["id"],
                    "workspace_path": endpoint["workspace_path"],
                    "monitor_status": endpoint["monitor"]["status"],
                    "host_telemetry": host,
                    "capacity": {
                        "total_gpus": len(endpoint_gpus),
                        "available_gpus": sum(
                            self._gpu_payload_is_publicly_available(gpu) for gpu in endpoint_gpus
                        ),
                        # Compatibility: leased_gpus has always meant visible
                        # workload leases. Internal keepalive ownership is
                        # intentionally counted separately below.
                        "leased_gpus": workload_leased_gpu_count,
                        "workload_leased_gpus": workload_leased_gpu_count,
                        "keepalive_owned_gpus": keepalive_owned_gpu_count,
                        "verified_keepalive_gpus": state_counts.get("KEEPALIVE", 0),
                        "managed_running_gpus": state_counts.get("RUNNING_MANAGED", 0),
                        "idle_leased_gpus": state_counts.get("LEASED_IDLE", 0),
                        "unattributed_compute_gpus": state_counts.get("BUSY_UNMANAGED", 0),
                        "gpu_states": state_counts,
                        "observed_gpu_utilization_pct": average(
                            item["gpu_utilization_pct"] for item in endpoint_telemetry
                        ),
                        "available_cpu_cores": round(
                            max(0.0, host["cpu_count"] - host["load_1m"]), 1
                        )
                        if host
                        else None,
                        "available_memory_mib": host["memory_available_mib"] if host else None,
                        "total_system_memory_mib": host["memory_total_mib"] if host else None,
                        "total_vram_mib": sum(gpu["total_vram_mib"] for gpu in endpoint_gpus),
                        "observed_memory_used_mib": sum(
                            item["memory_used_mib"] for item in endpoint_telemetry
                        ),
                        "observed_memory_free_mib": sum(
                            item["memory_free_mib"] for item in endpoint_telemetry
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
        for job in active_scheduler_jobs:
            signals.append(
                {
                    "kind": "external_scheduler_job",
                    "severity": (
                        "warning" if job["state"] in {"UNKNOWN", "ACCESS_REQUIRED"} else "info"
                    ),
                    "scheduler_job_id": job["id"],
                    "target_id": job["target_id"],
                    "project_id": job["project_id"],
                    "task": job["task_ref"],
                    "state": job["state"],
                    "message": (
                        job["error_message"]
                        or "external scheduler owns placement; this is not a raw GPU lease"
                    ),
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
            # Compatibility: active_leases remains the visible workload lease
            # count. Keepalive ownership is reported by keepalive_owned_gpus.
            "active_leases": len(lease_cards),
            "active_workload_leases": len(lease_cards),
            "active_agents": len(agent_cards),
            "queued_requests": len(data["requests"]),
            "queued_gpus": sum(item["constraints"]["gpu_count"] for item in data["requests"]),
            "external_scheduler_targets": len(scheduler_targets),
            "external_scheduler_jobs": len(active_scheduler_jobs),
            "external_scheduler_pending_jobs": sum(
                job["state"] == "PENDING" for job in active_scheduler_jobs
            ),
            "external_scheduler_running_jobs": sum(
                job["state"] == "RUNNING" for job in active_scheduler_jobs
            ),
            "managed_running_gpus": sum(
                item["gpu_states"].get("RUNNING_MANAGED", 0) for item in lease_cards
            ),
            "idle_leased_gpus": sum(
                item["gpu_states"].get("LEASED_IDLE", 0) for item in lease_cards
            ),
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
                "scheduler_targets": scheduler_targets,
                "scheduler_jobs": active_scheduler_jobs,
                "signals": signals,
                "guidance": (
                    "这是只读协作面板。active_leases 只统计工作任务租约；"
                    "keepalive_owned_gpus 统计内部空闲占卡。available_gpus 同时包含空闲 GPU 和"
                    "已确认的逐卡占卡 GPU；真正分配前会停止选中卡的占卡程序、刷新确认，"
                    "再进入普通申请路径。任务占用和冲突 GPU 不可用。"
                ),
            },
        }

    def list_resources(self, actor: ActorContext) -> dict[str, Any]:
        """Return a unified read-only resource board for agents and humans."""

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            host_capacity = self._host_capacity_cards(session, now)
            claims = session.scalars(
                select(ResourceClaimModel).order_by(ResourceClaimModel.created_at.desc())
            ).all()
            allocations = session.execute(
                select(ResourceAllocation, AllocatableUnit)
                .join(AllocatableUnit, AllocatableUnit.id == ResourceAllocation.unit_id)
                .order_by(ResourceAllocation.created_at.desc(), ResourceAllocation.id.desc())
            ).all()
            evaluations = session.scalars(
                select(ResourcePlanEvaluation).order_by(ResourcePlanEvaluation.created_at.desc())
            ).all()
            candidates_by_evaluation: dict[str, list[ResourcePlanCandidateModel]] = defaultdict(
                list
            )
            if evaluations:
                for candidate in session.scalars(
                    select(ResourcePlanCandidateModel)
                    .where(
                        ResourcePlanCandidateModel.evaluation_id.in_(
                            [evaluation.id for evaluation in evaluations]
                        )
                    )
                    .order_by(
                        ResourcePlanCandidateModel.evaluation_id, ResourcePlanCandidateModel.id
                    )
                ).all():
                    candidates_by_evaluation[candidate.evaluation_id].append(candidate)
            active_claims = [claim for claim in claims if claim.state == "active"]
            active_allocations = [
                (allocation, unit)
                for allocation, unit in allocations
                if allocation.state == "active"
            ]
            summary = {
                "resource_provider_count": session.scalar(
                    select(func.count()).select_from(ResourceProvider)
                )
                or 0,
                "host_capacity_units": len(host_capacity),
                "available_host_capacity_units": sum(
                    card["admission_state"] == "available" for card in host_capacity
                ),
                "active_resource_claims": len(active_claims),
                "active_resource_allocations": len(active_allocations),
                "available_cpu_cores": round(
                    sum(
                        card["capacity"]["available_cpu_cores"] or 0.0
                        for card in host_capacity
                        if card["admission_state"] == "available"
                    ),
                    1,
                ),
                "available_memory_mib": sum(
                    card["capacity"]["available_memory_mib"] or 0
                    for card in host_capacity
                    if card["admission_state"] == "available"
                ),
            }
            return self.envelope(
                session,
                {
                    "summary": summary,
                    "host_capacity": host_capacity,
                    "claims": [self._resource_claim_dict(claim) for claim in claims],
                    "allocations": [
                        self._resource_allocation_dict(allocation, unit=unit)
                        for allocation, unit in allocations
                    ],
                    "plan_evaluations": [
                        self._resource_plan_evaluation_dict(
                            evaluation,
                            candidates_by_evaluation[evaluation.id],
                        )
                        for evaluation in evaluations
                    ],
                    "admission_boundary": (
                        "Generic host-capacity claims coordinate CPU and memory only; they do not "
                        "authorize workload launch or remote shell execution."
                    ),
                },
            )

        return self._read(operation)

    # Public resource contracts are thin projections over the one domain-owned
    # monitor.  Keeping them here avoids a second scheduler state path in the
    # REST/MCP adapters while retaining the compatibility GPU endpoints.
    def resource_monitor(
        self, actor: ActorContext, *, project_id: str | None = None
    ) -> dict[str, Any]:
        data = self.list_resources(actor)
        if project_id is None:
            return data
        monitor = data["data"]
        monitor["claims"] = [
            claim for claim in monitor["claims"] if claim["project_id"] == project_id
        ]
        monitor["allocations"] = [
            allocation
            for allocation in monitor["allocations"]
            if allocation["claim_id"] in {claim["id"] for claim in monitor["claims"]}
        ]
        monitor["plan_evaluations"] = [
            evaluation
            for evaluation in monitor["plan_evaluations"]
            if evaluation["project_id"] == project_id
        ]
        return data

    def list_resource_providers(
        self,
        actor: ActorContext,
        *,
        provider_type: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            providers = session.scalars(
                select(ResourceProvider).order_by(ResourceProvider.id)
            ).all()
            data = [
                {
                    "id": provider.id,
                    "provider_type": provider.provider_type,
                    "display_name": provider.display_name,
                    "endpoint_id": provider.endpoint_id,
                    "scheduler_target_id": provider.scheduler_target_id,
                    "native_ref": json_load(provider.native_ref_json),
                    "enabled": provider.enabled,
                    "created_at": _iso(provider.created_at),
                    "updated_at": _iso(provider.updated_at),
                }
                for provider in providers
                if (provider_type is None or provider.provider_type == provider_type)
                and (enabled is None or provider.enabled is enabled)
            ]
            return self.envelope(session, data)

        return self._read(operation)

    def list_resource_claims(
        self,
        actor: ActorContext,
        *,
        project_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            claims = session.scalars(
                select(ResourceClaimModel).order_by(ResourceClaimModel.created_at.desc())
            ).all()
            data = [
                self._resource_claim_dict(claim)
                for claim in claims
                if (
                    actor.is_admin
                    or claim.actor_id == actor.id
                    or claim.project_id in actor.project_ids
                )
                and (project_id is None or claim.project_id == project_id)
                and (state is None or claim.state == state)
            ]
            return self.envelope(session, data)

        return self._read(operation)

    def list_resource_plan_evaluations(
        self, actor: ActorContext, *, project_id: str | None = None
    ) -> dict[str, Any]:
        return self.resource_monitor(actor, project_id=project_id)

    def list_resource_run_actuals(
        self,
        actor: ActorContext,
        *,
        project_id: str | None = None,
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            actuals = session.scalars(
                select(ResourceRunActual).order_by(ResourceRunActual.created_at.desc())
            ).all()
            data = [
                self._resource_actual_dict(actual)
                for actual in actuals
                if (
                    actor.is_admin
                    or actual.actor_id == actor.id
                    or actual.project_id in actor.project_ids
                )
                and (project_id is None or actual.project_id == project_id)
                and (task_ref is None or actual.task_ref == task_ref)
            ]
            return self.envelope(session, data)

        return self._read(operation)

    def claim_resource(
        self, actor: ActorContext, claim_data: ResourceClaimInput, *, idempotency_key: str
    ) -> dict[str, Any]:
        return self.create_resource_claim(actor, claim_data, idempotency_key=idempotency_key)

    def create_resource_claim(
        self,
        actor: ActorContext,
        claim_data: ResourceClaimInput,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="resource_claim.create",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            now = utcnow()
            project = self._ensure_claim_project(session, claim_data.project_id, now)
            if not project.enabled:
                raise BrokerError("project_disabled", "project is disabled", status_code=409)
            if (
                claim_data.quantities.gpu_count
                or claim_data.quantities.nodes
                or claim_data.quantities.scheduler_units
            ):
                raise BrokerError(
                    "generic_provider_not_implemented",
                    "generic direct-gpu and scheduler claims are exposed through the existing GPU and Slurm APIs",
                    status_code=422,
                )
            provider_type = claim_data.provider_type or "host-capacity"
            if provider_type != "host-capacity":
                raise BrokerError(
                    "unsupported_resource_provider",
                    "this service method currently allocates host-capacity claims only",
                    status_code=422,
                )
            revision = self._bump_revision(session, now)
            selected, candidates = self._select_host_capacity_unit(session, claim_data, now=now)
            if selected is None:
                raise BrokerError(
                    "no_capacity",
                    "no idle CPU or memory capacity matches this claim",
                    status_code=409,
                )
            claim = ResourceClaimModel(
                id=secrets.token_hex(16),
                actor_id=actor.id,
                project_id=claim_data.project_id,
                task_ref=claim_data.task_ref,
                purpose=claim_data.purpose,
                provider_type="host-capacity",
                requested_quantities_json=json_dump(
                    self._resource_quantities_dict(claim_data.quantities)
                ),
                forecast_json=(
                    json_dump(claim_data.forecast.model_dump(mode="json"))
                    if claim_data.forecast is not None
                    else None
                ),
                state="active",
                created_at=now,
                updated_at=now,
            )
            session.add(claim)
            session.flush()
            allocation = None
            if selected is not None and selected["unit"] is not None:
                allocation = ResourceAllocation(
                    claim_id=claim.id,
                    unit_id=selected["unit"]["id"],
                    native_lease_id=None,
                    native_scheduler_job_id=None,
                    quantities_json=json_dump(
                        self._resource_quantities_dict(claim_data.quantities)
                    ),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
                session.add(allocation)
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action=("resource_claim.allocated"),
                resource_type="resource_claim",
                resource_id=claim.id,
                result="success",
                after=self._resource_claim_dict(claim),
                summary={
                    "project_id": claim.project_id,
                    "task_ref": claim.task_ref,
                    "provider_type": claim.provider_type,
                    "selected_unit_id": selected["unit"]["id"]
                    if selected and selected["unit"]
                    else None,
                    "candidate_count": len(candidates),
                    "excluded": {
                        item["endpoint"]["id"]: item["excluded_reason"]
                        for item in candidates
                        if item["excluded_reason"]
                    },
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "claim": self._resource_claim_dict(claim),
                "allocation": (
                    self._resource_allocation_dict(
                        allocation,
                        unit=session.get(AllocatableUnit, allocation.unit_id),
                    )
                    if allocation is not None
                    else None
                ),
                "candidates": candidates,
                "authority": (
                    "Host-capacity allocation coordinates CPU and memory only; workload launch "
                    "still requires the applicable project/owner authorization."
                ),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="resource_claim.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def release_resource_claim(
        self,
        actor: ActorContext,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)
        if not reason.strip():
            raise BrokerError(
                "release_reason_required", "a release reason is required", status_code=422
            )

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="resource_claim.release",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            claim = session.get(ResourceClaimModel, claim_id)
            if claim is None:
                raise BrokerError(
                    "resource_claim_not_found", "resource claim does not exist", status_code=404
                )
            if claim.actor_id != actor.id and not actor.is_admin:
                raise BrokerError(
                    "resource_claim_forbidden",
                    "cannot release another actor's resource claim",
                    status_code=403,
                )
            if claim.state in {"released", "cancelled"}:
                raise BrokerError(
                    "resource_claim_already_released",
                    "resource claim is already terminal",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._resource_claim_dict(claim)
            claim.state = "released"
            claim.updated_at = now
            rows = session.execute(
                select(ResourceAllocation, AllocatableUnit)
                .join(AllocatableUnit, AllocatableUnit.id == ResourceAllocation.unit_id)
                .where(ResourceAllocation.claim_id == claim.id)
            ).all()
            for allocation, _unit in rows:
                if allocation.state == "active":
                    allocation.state = "released"
                    allocation.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="resource_claim.released",
                resource_type="resource_claim",
                resource_id=claim.id,
                result="success",
                before=before,
                after=self._resource_claim_dict(claim),
                summary={"reason": reason.strip()[:500]},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "claim": self._resource_claim_dict(claim),
                "allocations": [
                    self._resource_allocation_dict(allocation, unit=unit)
                    for allocation, unit in rows
                ],
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="resource_claim.release",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def evaluate_resource_plan(
        self,
        actor: ActorContext,
        evaluation_data: ResourcePlanEvaluationInput,
        *,
        idempotency_key: str,
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="resource_plan.evaluate",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            now = utcnow()
            project = self._ensure_claim_project(session, evaluation_data.project_id, now)
            if not project.enabled:
                raise BrokerError("project_disabled", "project is disabled", status_code=409)
            planner_candidates = [
                ResourcePlanCandidate(
                    id=candidate.candidate_key,
                    provider_kind=candidate.provider_type or "host-capacity",
                    predicted_remaining_seconds=candidate.predicted_runtime_seconds,
                    forecast_basis="agent supplied resource frontier",
                    cpu_cores=candidate.quantities.cpu_cores,
                    memory_mib=candidate.quantities.memory_mib,
                    gpu_count=candidate.quantities.gpu_count,
                    nodes=candidate.quantities.nodes + candidate.quantities.scheduler_units,
                )
                for candidate in evaluation_data.candidates
            ]
            selection = select_smallest_useful_plan(
                planner_candidates,
                min_saved_fraction=evaluation_data.marginal_min_saved_ratio,
                min_saved_seconds=evaluation_data.marginal_min_saved_seconds,
            )
            selected_key = selection.selected.id
            revision = self._bump_revision(session, now)
            evaluation = ResourcePlanEvaluation(
                id=secrets.token_hex(16),
                claim_id=claim_id,
                actor_id=actor.id,
                project_id=evaluation_data.project_id,
                task_ref=evaluation_data.task_ref,
                baseline_runtime_seconds=evaluation_data.baseline_runtime_seconds,
                marginal_min_saved_seconds=evaluation_data.marginal_min_saved_seconds,
                marginal_min_saved_ratio=evaluation_data.marginal_min_saved_ratio,
                selected_candidate_key=selected_key,
                forecast_json=json_dump(
                    {
                        "policy": "smallest-feasible-with-marginal-benefit",
                        "decision_thresholds": {
                            "min_saved_ratio": evaluation_data.marginal_min_saved_ratio,
                            "min_saved_seconds": evaluation_data.marginal_min_saved_seconds,
                        },
                    }
                ),
                created_at=now,
            )
            session.add(evaluation)
            session.flush()
            decision_by_key = {decision.candidate_id: decision for decision in selection.decisions}
            stored_candidates: list[ResourcePlanCandidateModel] = []
            for input_candidate in evaluation_data.candidates:
                decision = decision_by_key.get(input_candidate.candidate_key)
                selected = input_candidate.candidate_key == selected_key
                predicted_saved_seconds = max(
                    0,
                    evaluation_data.baseline_runtime_seconds
                    - input_candidate.predicted_runtime_seconds,
                )
                predicted_saved_ratio = (
                    predicted_saved_seconds / evaluation_data.baseline_runtime_seconds
                )
                satisfies = decision.selected if decision is not None else False
                rejection_reason = None
                if not selected:
                    rejection_reason = (
                        decision.reason
                        if decision is not None
                        else "not-reached-after-marginal-stop"
                    )
                candidate = ResourcePlanCandidateModel(
                    evaluation_id=evaluation.id,
                    candidate_key=input_candidate.candidate_key,
                    provider_type=input_candidate.provider_type,
                    quantities_json=json_dump(input_candidate.quantities.model_dump(mode="json")),
                    predicted_runtime_seconds=input_candidate.predicted_runtime_seconds,
                    predicted_saved_seconds=predicted_saved_seconds,
                    predicted_saved_ratio=predicted_saved_ratio,
                    satisfies_marginal_threshold=satisfies,
                    selected=selected,
                    rejection_reason=rejection_reason,
                )
                session.add(candidate)
                stored_candidates.append(candidate)
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="resource_plan.evaluated",
                resource_type="resource_plan",
                resource_id=evaluation.id,
                result="success",
                after=self._resource_plan_evaluation_dict(evaluation, stored_candidates),
                summary={
                    "project_id": evaluation.project_id,
                    "task_ref": evaluation.task_ref,
                    "selected_candidate_key": selected_key,
                    "candidate_count": len(stored_candidates),
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "evaluation": self._resource_plan_evaluation_dict(evaluation, stored_candidates),
                "decisions": [
                    {
                        "candidate_key": decision.candidate_id,
                        "selected": decision.selected,
                        "reason": decision.reason,
                        "saved_seconds": decision.saved_seconds,
                        "saved_ratio": decision.saved_fraction,
                    }
                    for decision in selection.decisions
                ],
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="resource_plan.evaluate",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def record_resource_run_actual(
        self,
        actor: ActorContext,
        actual_data: ResourceRunActualInput,
        *,
        idempotency_key: str,
        evaluation_id: str | None = None,
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="resource_run_actual.record",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            now = utcnow()
            project = self._ensure_claim_project(session, actual_data.project_id, now)
            if not project.enabled:
                raise BrokerError("project_disabled", "project is disabled", status_code=409)
            if (
                evaluation_id is not None
                and session.get(ResourcePlanEvaluation, evaluation_id) is None
            ):
                raise BrokerError(
                    "resource_plan_not_found",
                    "resource plan evaluation does not exist",
                    status_code=404,
                )
            if claim_id is not None and session.get(ResourceClaimModel, claim_id) is None:
                raise BrokerError(
                    "resource_claim_not_found",
                    "resource claim does not exist",
                    status_code=404,
                )
            revision = self._bump_revision(session, now)
            actual = ResourceRunActual(
                evaluation_id=evaluation_id,
                claim_id=claim_id,
                actor_id=actor.id,
                project_id=actual_data.project_id,
                task_ref=actual_data.task_ref,
                started_at=ensure_utc(actual_data.started_at),
                completed_at=(
                    ensure_utc(actual_data.completed_at)
                    if actual_data.completed_at is not None
                    else None
                ),
                actual_duration_seconds=actual_data.actual_duration_seconds,
                quantities_json=json_dump(actual_data.quantities.model_dump(mode="json")),
                outcome=actual_data.outcome,
                notes_json=json_dump({}),
                created_at=now,
            )
            session.add(actual)
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="resource_run_actual.recorded",
                resource_type="resource_run_actual",
                resource_id=str(actual.id),
                result="success",
                after=self._resource_actual_dict(actual),
                summary={"project_id": actual.project_id, "task_ref": actual.task_ref},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "actual": self._resource_actual_dict(actual),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="resource_run_actual.record",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

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
                raise BrokerError(
                    "gpu_not_found", "GPU is not visible or does not exist", status_code=404
                )
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
            current_observed_at = _as_utc(current.observed_at) if current is not None else None
            if (
                current is not None
                and current_observed_at is not None
                and current_observed_at >= cutoff
                and (not samples or current_observed_at > (_as_utc(samples[-1].observed_at) or now))
            ):
                samples.append(current)

            buckets: list[list[TelemetryCurrent | TelemetrySnapshot]]
            if len(samples) <= max_points:
                buckets = [[sample] for sample in samples]
            else:
                buckets = [
                    samples[
                        index * len(samples) // max_points : (index + 1)
                        * len(samples)
                        // max_points
                    ]
                    for index in range(max_points)
                ]

            def average(
                bucket: list[TelemetryCurrent | TelemetrySnapshot], name: str
            ) -> float | None:
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

    def endpoint_history(
        self,
        actor: ActorContext,
        endpoint_id: str,
        *,
        window_seconds: int = 3600,
        max_points: int = 120,
    ) -> dict[str, Any]:
        """Return bounded host CPU/load/memory history for one endpoint detail view."""

        if window_seconds not in {3600, 21_600, TELEMETRY_HISTORY_RETENTION_SECONDS}:
            raise BrokerError(
                "invalid_history_window",
                "endpoint history window must be one of 1h, 6h or 24h",
                status_code=422,
            )
        if not 10 <= max_points <= 120:
            raise BrokerError(
                "invalid_history_points",
                "history points must be between 10 and 120",
                status_code=422,
            )

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_found",
                    "endpoint is not visible or does not exist",
                    status_code=404,
                )
            now = utcnow()
            cutoff = now - timedelta(seconds=window_seconds)
            samples: list[EndpointTelemetryCurrent | EndpointTelemetrySnapshot] = list(
                session.scalars(
                    select(EndpointTelemetrySnapshot)
                    .where(
                        EndpointTelemetrySnapshot.endpoint_id == endpoint_id,
                        EndpointTelemetrySnapshot.observed_at >= cutoff,
                    )
                    .order_by(
                        EndpointTelemetrySnapshot.observed_at,
                        EndpointTelemetrySnapshot.id,
                    )
                ).all()
            )
            current = session.get(EndpointTelemetryCurrent, endpoint_id)
            current_observed_at = _as_utc(current.observed_at) if current is not None else None
            if (
                current is not None
                and current_observed_at is not None
                and current_observed_at >= cutoff
                and (not samples or current_observed_at > (_as_utc(samples[-1].observed_at) or now))
            ):
                samples.append(current)

            def buckets_for(values: list[Any]) -> list[list[Any]]:
                if len(values) <= max_points:
                    return [[value] for value in values]
                return [
                    values[
                        index * len(values) // max_points : (index + 1) * len(values) // max_points
                    ]
                    for index in range(max_points)
                ]

            buckets = buckets_for(samples)

            def average(bucket: list[Any], name: str) -> float | None:
                values = [getattr(sample, name) for sample in bucket]
                present = [float(value) for value in values if value is not None]
                return round(sum(present) / len(present), 2) if present else None

            points = []
            for bucket in buckets:
                memory_total = average(bucket, "memory_total_mib")
                memory_available = average(bucket, "memory_available_mib")
                points.append(
                    {
                        "observed_at": _iso(bucket[-1].observed_at),
                        "cpu_count": average(bucket, "cpu_count"),
                        "load_1m": average(bucket, "load_1m"),
                        "cpu_utilization_pct": average(bucket, "cpu_utilization_pct"),
                        "memory_total_mib": memory_total,
                        "memory_available_mib": memory_available,
                        "memory_used_pct": (
                            round(
                                (memory_total - (memory_available or 0)) * 100 / memory_total,
                                2,
                            )
                            if memory_total
                            else None
                        ),
                    }
                )

            # GPU history is deliberately returned as endpoint-scoped series rather
            # than asking the desktop to fan out into one request per GPU.  The
            # identity and historical source remain the canonical GPU device and
            # telemetry snapshot tables; this is only a bounded read projection.
            devices = list(
                session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id == endpoint.id)
                    .order_by(GPUDevice.gpu_index, GPUDevice.id)
                ).all()
            )
            device_ids = [device.id for device in devices]
            readings_by_gpu: dict[str, list[TelemetryCurrent | TelemetrySnapshot]] = {
                device.id: [] for device in devices
            }
            if device_ids:
                historical_readings = session.scalars(
                    select(TelemetrySnapshot)
                    .where(
                        TelemetrySnapshot.gpu_id.in_(device_ids),
                        TelemetrySnapshot.observed_at >= cutoff,
                    )
                    .order_by(
                        TelemetrySnapshot.gpu_id,
                        TelemetrySnapshot.observed_at,
                        TelemetrySnapshot.id,
                    )
                ).all()
                for reading in historical_readings:
                    readings_by_gpu[reading.gpu_id].append(reading)

                current_readings = session.scalars(
                    select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(device_ids))
                ).all()
                for reading in current_readings:
                    observed_at = _as_utc(reading.observed_at)
                    history = readings_by_gpu[reading.gpu_id]
                    if (
                        observed_at is not None
                        and observed_at >= cutoff
                        and (not history or observed_at > (_as_utc(history[-1].observed_at) or now))
                    ):
                        history.append(reading)

            gpu_series = []
            for device in devices:
                readings = readings_by_gpu[device.id]
                series_points = []
                for bucket in buckets_for(readings):
                    memory_used = average(bucket, "memory_used_mib")
                    series_points.append(
                        {
                            "observed_at": _iso(bucket[-1].observed_at),
                            "gpu_utilization_pct": average(bucket, "gpu_utilization_pct"),
                            "memory_used_pct": (
                                round((memory_used or 0) * 100 / device.total_vram_mib, 2)
                                if device.total_vram_mib
                                else None
                            ),
                            "memory_used_mib": memory_used,
                            "memory_total_mib": device.total_vram_mib,
                        }
                    )
                gpu_series.append(
                    {
                        "gpu_id": device.id,
                        "gpu_uuid": device.gpu_uuid,
                        "gpu_index": device.gpu_index,
                        "label": f"GPU {device.gpu_index}",
                        "points": series_points,
                    }
                )

            return self.envelope(
                session,
                {
                    "endpoint_id": endpoint.id,
                    "window_seconds": window_seconds,
                    "point_count": len(points),
                    "points": points,
                    "gpu_series": gpu_series,
                },
            )

        return self._read(operation)

    def prune_telemetry_history(
        self, older_than_seconds: int = TELEMETRY_HISTORY_RETENTION_SECONDS
    ) -> int:
        """Internal hourly retention pass; current telemetry, leases and audit are untouched."""

        cutoff = utcnow() - timedelta(seconds=older_than_seconds)

        def operation(session: Session) -> int:
            gpu_result = session.execute(
                delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
            )
            endpoint_result = session.execute(
                delete(EndpointTelemetrySnapshot).where(
                    EndpointTelemetrySnapshot.observed_at < cutoff
                )
            )
            return max(0, gpu_result.rowcount or 0) + max(0, endpoint_result.rowcount or 0)

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
                raise BrokerError(
                    "endpoint_not_found", "collector reported an unknown endpoint", status_code=404
                )
            observed_gpu_ids = {f"{endpoint.id}:{sample.gpu_uuid}" for sample in observation.gpus}
            observed_at = ensure_utc(observation.observed_at)
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            current_observed_at = _as_utc(host_telemetry.observed_at) if host_telemetry else None
            if current_observed_at is not None and observed_at < current_observed_at:
                return {
                    "event_id": None,
                    "snapshot_revision": self._revision(session),
                    "endpoint_id": endpoint.id,
                    "gpu_count": len(observation.gpus),
                    "absent_gpu_count": 0,
                    "observation_complete": observation.observation_complete,
                    "process_count": len(observation.processes),
                    "history_points_written": 0,
                    "endpoint_history_points_written": 0,
                    "ignored": True,
                    "ignore_reason": "stale_observation",
                    "current_observed_at": _iso(current_observed_at),
                }
            revision = self._bump_revision(session, now)
            host_cpu_total_ticks = observation.host.cpu_total_ticks
            host_cpu_idle_ticks = observation.host.cpu_idle_ticks
            host_cpu_utilization_pct = self._host_cpu_utilization_pct(
                host_telemetry,
                cpu_total_ticks=host_cpu_total_ticks,
                cpu_idle_ticks=host_cpu_idle_ticks,
            )
            latest_endpoint_history_at = _as_utc(
                session.scalar(
                    select(func.max(EndpointTelemetrySnapshot.observed_at)).where(
                        EndpointTelemetrySnapshot.endpoint_id == endpoint.id
                    )
                )
            )
            endpoint_history_points_written = 0
            if (
                latest_endpoint_history_at is None
                or (observed_at - latest_endpoint_history_at).total_seconds()
                >= TELEMETRY_HISTORY_INTERVAL_SECONDS
            ):
                session.add(
                    EndpointTelemetrySnapshot(
                        endpoint_id=endpoint.id,
                        observed_at=observed_at,
                        collected_at=now,
                        cpu_count=observation.host.cpu_count,
                        load_1m=observation.host.load_1m,
                        cpu_total_ticks=host_cpu_total_ticks,
                        cpu_idle_ticks=host_cpu_idle_ticks,
                        cpu_utilization_pct=host_cpu_utilization_pct,
                        memory_total_mib=observation.host.memory_total_mib,
                        memory_available_mib=observation.host.memory_available_mib,
                        provider=provider,
                    )
                )
                endpoint_history_points_written = 1
            if host_telemetry is None:
                host_telemetry = EndpointTelemetryCurrent(
                    endpoint_id=endpoint.id,
                    observed_at=observed_at,
                    collected_at=now,
                    cpu_count=observation.host.cpu_count,
                    load_1m=observation.host.load_1m,
                    cpu_total_ticks=host_cpu_total_ticks,
                    cpu_idle_ticks=host_cpu_idle_ticks,
                    cpu_utilization_pct=host_cpu_utilization_pct,
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
                host_telemetry.cpu_total_ticks = host_cpu_total_ticks
                host_telemetry.cpu_idle_ticks = host_cpu_idle_ticks
                host_telemetry.cpu_utilization_pct = host_cpu_utilization_pct
                host_telemetry.memory_total_mib = observation.host.memory_total_mib
                host_telemetry.memory_available_mib = observation.host.memory_available_mib
                host_telemetry.provider = provider
            self._ensure_host_capacity_unit(session, endpoint, host_telemetry, now=now)
            gpu_ids = list(observed_gpu_ids)
            existing_gpus = {
                gpu.id: gpu
                for gpu in session.scalars(select(GPUDevice).where(GPUDevice.id.in_(gpu_ids))).all()
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
                        present=True,
                        first_seen_at=observed_at,
                        last_seen_at=observed_at,
                        absent_at=None,
                    )
                    session.add(gpu)
                else:
                    gpu.gpu_index = sample.gpu_index
                    gpu.name = sample.name
                    gpu.total_vram_mib = sample.total_vram_mib
                    gpu.health = sample.health
                    gpu.present = True
                    gpu.last_seen_at = observed_at
                    gpu.absent_at = None
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
                if (
                    last_history_at is None
                    or (observed_at - last_history_at).total_seconds()
                    >= TELEMETRY_HISTORY_INTERVAL_SECONDS
                ):
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
            absent_gpu_count = 0
            if observation.observation_complete:
                prior_gpus = session.scalars(
                    select(GPUDevice).where(GPUDevice.endpoint_id == endpoint.id)
                ).all()
                for gpu in prior_gpus:
                    if gpu.id not in observed_gpu_ids and gpu.present:
                        gpu.present = False
                        gpu.absent_at = observed_at
                        absent_gpu_count += 1
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
                    candidate_started_at = (
                        _as_utc(candidate.process_started_at) if candidate else None
                    )
                    # `nvidia-smi` can report a host PID that is not visible to
                    # the endpoint's `ps` namespace.  The sealed collector
                    # marks that case with a missing username and has no real
                    # start time, so its conservative fallback is the current
                    # observation time.  Reuse only the *currently active*
                    # GPU/PID/boot identity in that case.  If the process
                    # disappears for one complete observation the candidate is
                    # inactive and a reused PID becomes a new, fail-closed
                    # identity as intended.
                    process_identity_metadata_missing = process.username is None
                    if (
                        candidate is not None
                        and candidate_started_at is not None
                        and (
                            process_identity_metadata_missing
                            or abs((candidate_started_at - started_at).total_seconds())
                            <= PROCESS_START_TIME_JITTER_SECONDS
                        )
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
            if observation.observation_complete:
                current_processes = session.scalars(
                    select(ProcessObservation).where(
                        ProcessObservation.endpoint_id == endpoint.id,
                        ProcessObservation.active.is_(True),
                    )
                ).all()
                for prior in current_processes:
                    key = (
                        prior.gpu_id,
                        prior.pid,
                        prior.boot_id,
                        _as_utc(prior.process_started_at),
                    )
                    if key not in observed_process_keys:
                        prior.active = False
            provider_state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == provider, ProviderState.endpoint_id == endpoint.id
                )
            )
            recovered = (
                observation.observation_complete
                and provider_state is not None
                and provider_state.last_error is not None
            )
            incomplete_error = (
                None if observation.observation_complete else "incomplete endpoint observation"
            )
            if provider_state is None:
                session.add(
                    ProviderState(
                        provider=provider,
                        endpoint_id=endpoint.id,
                        last_success_at=now if observation.observation_complete else None,
                        last_attempt_at=now,
                        last_error=incomplete_error,
                        revision=revision,
                    )
                )
            else:
                if observation.observation_complete:
                    provider_state.last_success_at = now
                provider_state.last_attempt_at = now
                provider_state.last_error = incomplete_error
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
            self._record_observed_keepalive(
                session,
                endpoint_id=endpoint.id,
                observation_complete=observation.observation_complete,
                now=now,
            )
            self._bind_new_workload_processes(
                session,
                endpoint_id=endpoint.id,
                observation_complete=observation.observation_complete,
                now=now,
            )
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
                "absent_gpu_count": absent_gpu_count,
                "observation_complete": observation.observation_complete,
                "process_count": len(observation.processes),
                "history_points_written": history_points_written,
                "endpoint_history_points_written": endpoint_history_points_written,
                "ignored": False,
            }

        return self._write(operation)

    def _bind_new_workload_processes(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observation_complete: bool,
        now: datetime,
    ) -> None:
        """Attach a complete observed workload cohort to its routine lease.

        A routine Agent may restart a failed worker without releasing the GPU
        lease.  When a complete endpoint observation shows that every old
        bound process has gone and every leased GPU has a wholly new process
        cohort, keep the durable task ownership and replace its observed
        process keys.  Mixed old/new cohorts remain fail closed because they
        can represent an unrelated process entering an assigned GPU.
        """

        lease_ids = session.scalars(
            select(LeaseResource.lease_id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
        ).all()
        if not lease_ids:
            return
        leases = session.scalars(
            select(Lease).where(
                Lease.id.in_(lease_ids),
                Lease.kind == "workload",
                Lease.state.in_({"HELD", "ACTIVE", "CONFLICT"}),
            )
        ).all()
        for lease in leases:
            bindings = session.scalars(
                select(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id)
            ).all()
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            processes_by_gpu = {
                resource.gpu_id: self._current_processes(session, resource.gpu_id, now)
                for resource in resources
            }
            resource_endpoint_ids = {
                gpu.endpoint_id
                for resource in resources
                if (gpu := session.get(GPUDevice, resource.gpu_id)) is not None
            }
            legacy_binding_was_collector_owned = False
            if len(bindings) == 1 and bindings[0].run_id == f"lease:{lease.id}":
                legacy_binding_events = session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "lease.workload_bound",
                        AuditEvent.resource_type == "lease",
                        AuditEvent.resource_id == lease.id,
                    )
                    .order_by(AuditEvent.id.desc())
                ).all()
                latest_legacy_binding_event = next(
                    (
                        event
                        for event in legacy_binding_events
                        if json_load(event.summary_json).get("run_id") in {None, bindings[0].run_id}
                    ),
                    None,
                )
                legacy_binding_was_collector_owned = bool(
                    latest_legacy_binding_event is not None
                    and json_load(latest_legacy_binding_event.summary_json).get("source")
                    == "collector_observed"
                    and latest_legacy_binding_event.actor_id == f"collector:{endpoint_id}"
                )
            collector_owned_routine_binding = bool(
                len(bindings) == 1
                and (
                    bindings[0].run_id == f"collector:lease:{lease.id}"
                    or legacy_binding_was_collector_owned
                )
                and lease.project_id == "agent"
                and resource_endpoint_ids == {endpoint_id}
            )
            if (
                resources
                and observation_complete
                and collector_owned_routine_binding
                and lease.state == "CONFLICT"
                and all(not processes_by_gpu[resource.gpu_id] for resource in resources)
            ):
                before = self._lease_dict(session, lease)
                lease.state = "ACTIVE"
                lease.last_heartbeat_at = now
                request = session.get(AllocationRequest, lease.request_id)
                if request is not None:
                    request.state = "ACTIVE"
                    request.updated_at = now
                self._resolve_lease_alerts(session, lease.id, now)
                self._audit(
                    session,
                    actor_id=f"collector:{endpoint_id}",
                    action="lease.conflict_resolved",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=before,
                    after=self._lease_dict(session, lease),
                    summary={"source": "complete_empty_routine_retry_window"},
                    now=now,
                )
                continue
            if not resources or any(
                not processes_by_gpu[resource.gpu_id] for resource in resources
            ):
                continue
            process_keys = sorted(
                {
                    self._process_key(process)
                    for processes in processes_by_gpu.values()
                    for process in processes
                }
            )
            known_process_keys = {
                process_key
                for binding in bindings
                for process_key in json_load(binding.process_keys_json)
            }
            replacing_complete_routine_cohort = bool(
                collector_owned_routine_binding
                and observation_complete
                and known_process_keys.isdisjoint(process_keys)
            )
            if bindings and not replacing_complete_routine_cohort:
                continue
            run_id = f"collector:lease:{lease.id}"
            before = self._lease_dict(session, lease)
            if replacing_complete_routine_cohort:
                bindings[0].process_keys_json = json_dump(process_keys)
                bindings[0].created_at = now
                self._resolve_lease_alerts(session, lease.id, now)
            else:
                session.add(
                    WorkloadBinding(
                        lease_id=lease.id,
                        run_id=run_id,
                        process_keys_json=json_dump(process_keys),
                        created_at=now,
                    )
                )
            lease.state = "ACTIVE"
            lease.activated_at = now
            lease.last_heartbeat_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "ACTIVE"
                request.updated_at = now
            actor_id = f"collector:{endpoint_id}"
            self._audit(
                session,
                actor_id=actor_id,
                action="lease.activated",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"activation": "observed_workload_bind", "run_id": run_id},
                now=now,
            )
            self._audit(
                session,
                actor_id=actor_id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": run_id, "process_key_count": len(process_keys)},
                summary={
                    "source": (
                        "collector_observed_process_turnover"
                        if replacing_complete_routine_cohort
                        else "collector_observed"
                    ),
                    "gpu_count": len(resources),
                    "resolved_conflict": replacing_complete_routine_cohort
                    and before.get("state") == "CONFLICT",
                },
                now=now,
            )

    @staticmethod
    def _sanitize_executable(value: str) -> str:
        # Collector never stores cmdline/cwd/environment. Keep a bounded basename-like label only.
        stripped = value.replace("\\x00", " ").replace("\n", " ").strip()
        return stripped.rsplit("/", maxsplit=1)[-1][:255] or "unknown"

    def record_provider_failure(
        self, endpoint_id: str, message: str, *, provider: str = "raw-ssh"
    ) -> None:
        def operation(session: Session) -> None:
            now = utcnow()
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_found", "collector reported an unknown endpoint", status_code=404
                )
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
            select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES), Lease.kind == "workload")
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
        if (
            project.quota_gpus is not None
            and gpu_usage[project.id] + constraints.gpu_count > project.quota_gpus
        ):
            return f"project GPU quota {project.quota_gpus} would be exceeded"
        if (
            project.concurrency_limit is not None
            and lease_usage[project.id] >= project.concurrency_limit
        ):
            return f"project concurrency limit {project.concurrency_limit} is reached"
        return None

    @staticmethod
    def _ensure_claim_project(session: Session, project_id: str, now: datetime) -> Project:
        """Persist a neutral project tag only because request rows reference it.

        A project id is supplied by the claimant; it is not an enrollment or
        endpoint-access check.  Configured projects can still carry optional
        fairness or quota policy, while first use gets the neutral defaults.
        """

        if project_id == SYSTEM_PROJECT_ID:
            raise BrokerError(
                "reserved_project_id",
                "the ServerPilot internal project cannot be used by workload claims",
                status_code=422,
            )
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

    @staticmethod
    def _endpoint_commitment_usage(
        session: Session,
    ) -> dict[str, tuple[float, int]]:
        """Return active direct-lease commitments keyed by endpoint identity."""

        usage: dict[str, tuple[float, int]] = {}
        commitments = session.scalars(
            select(LeaseEndpointCommitment)
            .join(Lease, Lease.id == LeaseEndpointCommitment.lease_id)
            .where(Lease.state.in_(ACTIVE_LEASE_STATES))
        ).all()
        for commitment in commitments:
            cpu, memory = usage.get(commitment.endpoint_id, (0.0, 0))
            usage[commitment.endpoint_id] = (
                cpu + commitment.cpu_cores,
                memory + commitment.memory_mib,
            )
        return usage

    @staticmethod
    def _active_generic_host_usage(session: Session) -> dict[str, tuple[float, int]]:
        usage: dict[str, tuple[float, int]] = {}
        rows = session.execute(
            select(ResourceAllocation, AllocatableUnit)
            .join(AllocatableUnit, AllocatableUnit.id == ResourceAllocation.unit_id)
            .where(
                ResourceAllocation.state == "active",
                AllocatableUnit.unit_type == "host",
                AllocatableUnit.endpoint_id.is_not(None),
            )
        ).all()
        for allocation, unit in rows:
            if unit.endpoint_id is None:
                continue
            quantities = json_load(allocation.quantities_json)
            cpu, memory = usage.get(unit.endpoint_id, (0.0, 0))
            usage[unit.endpoint_id] = (
                cpu + float(quantities.get("cpu_cores") or 0.0),
                memory + int(quantities.get("memory_mib") or 0),
            )
        return usage

    def _endpoint_monitor_status(
        self,
        session: Session,
        endpoint: Endpoint,
        now: datetime,
    ) -> tuple[str, str | None]:
        provider_state = session.scalar(
            select(ProviderState).where(
                ProviderState.provider == "raw-ssh",
                ProviderState.endpoint_id == endpoint.id,
            )
        )
        last_success = _as_utc(provider_state.last_success_at) if provider_state else None
        if endpoint.lifecycle_state == "draining":
            return "DRAINING", "endpoint is draining and blocks new claims"
        if not endpoint.enabled:
            return "DISABLED", "endpoint is disabled"
        if provider_state is None:
            return "PENDING", "no successful collector observation"
        if last_success is None or provider_state.last_error:
            return "ERROR", provider_state.last_error or "collector has no successful observation"
        if now - last_success > timedelta(seconds=self.inventory.collector.stale_after_seconds):
            return "STALE", "collector success is stale"
        return "ONLINE", None

    def _ensure_host_capacity_unit(
        self,
        session: Session,
        endpoint: Endpoint,
        telemetry: EndpointTelemetryCurrent,
        *,
        now: datetime,
    ) -> AllocatableUnit:
        provider_id = f"host-capacity:endpoint:{endpoint.id}"
        provider = session.get(ResourceProvider, provider_id)
        if provider is None:
            provider = ResourceProvider(
                id=provider_id,
                provider_type="host-capacity",
                display_name=f"{endpoint.id} host capacity",
                endpoint_id=endpoint.id,
                scheduler_target_id=None,
                native_ref_json=json_dump({"endpoint_id": endpoint.id}),
                metadata_json=json_dump({}),
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(provider)
        else:
            provider.display_name = f"{endpoint.id} host capacity"
            provider.native_ref_json = json_dump({"endpoint_id": endpoint.id})
            provider.updated_at = now

        unit_id = f"{provider_id}:host"
        unit = session.get(AllocatableUnit, unit_id)
        if unit is None:
            unit = AllocatableUnit(
                id=unit_id,
                provider_id=provider_id,
                unit_key="host",
                unit_type="host",
                endpoint_id=endpoint.id,
                gpu_id=None,
                scheduler_target_id=None,
                total_gpu_count=0,
                total_cpu_cores=float(telemetry.cpu_count),
                total_memory_mib=telemetry.memory_total_mib,
                total_vram_mib=None,
                labels_json=endpoint.labels_json,
                native_ref_json=json_dump({"endpoint_id": endpoint.id}),
                state="available",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(unit)
        else:
            unit.total_cpu_cores = float(telemetry.cpu_count)
            unit.total_memory_mib = telemetry.memory_total_mib
            unit.labels_json = endpoint.labels_json
            unit.native_ref_json = json_dump({"endpoint_id": endpoint.id})
            unit.state = "available"
            unit.updated_at = now
        session.flush()
        return unit

    def _host_capacity_cards(
        self,
        session: Session,
        now: datetime,
        *,
        ensure_units: bool = False,
    ) -> list[dict[str, Any]]:
        direct_usage = self._endpoint_commitment_usage(session)
        generic_usage = self._active_generic_host_usage(session)
        values: list[dict[str, Any]] = []
        endpoints = session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
        for endpoint in endpoints:
            telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            monitor_status, monitor_reason = self._endpoint_monitor_status(session, endpoint, now)
            unit = None
            if ensure_units:
                unit = (
                    self._ensure_host_capacity_unit(session, endpoint, telemetry, now=now)
                    if telemetry is not None
                    else None
                )
            elif telemetry is not None:
                unit = session.get(AllocatableUnit, f"host-capacity:endpoint:{endpoint.id}:host")
            observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
            telemetry_stale = (
                telemetry is None
                or observed_at is None
                or now - observed_at
                > timedelta(seconds=self.inventory.collector.stale_after_seconds)
            )
            direct_cpu, direct_memory = direct_usage.get(endpoint.id, (0.0, 0))
            generic_cpu, generic_memory = generic_usage.get(endpoint.id, (0.0, 0))
            if telemetry is None:
                available_cpu = None
                available_memory = None
            else:
                observed_available_cpu = max(0.0, telemetry.cpu_count - telemetry.load_1m)
                available_cpu = max(0.0, observed_available_cpu - direct_cpu - generic_cpu)
                available_memory = max(
                    0,
                    telemetry.memory_available_mib - direct_memory - generic_memory,
                )
            admission_state = "available"
            admission_reason = None
            if monitor_status != "ONLINE":
                admission_state = "blocked"
                admission_reason = monitor_reason or monitor_status.lower()
            elif telemetry_stale:
                admission_state = "blocked"
                admission_reason = "host telemetry is stale"
            elif available_cpu == 0 and available_memory == 0:
                admission_state = "blocked"
                admission_reason = "no uncommitted CPU or memory capacity"
            provider = session.get(ResourceProvider, unit.provider_id) if unit is not None else None
            values.append(
                {
                    "provider": self._provider_dict(provider) if provider is not None else None,
                    "unit": self._allocatable_unit_dict(unit) if unit is not None else None,
                    "endpoint": self._endpoint_dict(endpoint),
                    "monitor_status": monitor_status,
                    "admission_state": admission_state,
                    "admission_reason": admission_reason,
                    "telemetry": self._host_telemetry_dict(telemetry),
                    "capacity": {
                        "total_cpu_cores": telemetry.cpu_count if telemetry else None,
                        "observed_available_cpu_cores": (
                            round(max(0.0, telemetry.cpu_count - telemetry.load_1m), 1)
                            if telemetry
                            else None
                        ),
                        "available_cpu_cores": round(available_cpu, 1)
                        if available_cpu is not None
                        else None,
                        "total_memory_mib": telemetry.memory_total_mib if telemetry else None,
                        "observed_available_memory_mib": (
                            telemetry.memory_available_mib if telemetry else None
                        ),
                        "available_memory_mib": available_memory,
                        "committed_cpu_cores": round(direct_cpu + generic_cpu, 1),
                        "committed_memory_mib": direct_memory + generic_memory,
                        "direct_lease_cpu_cores": round(direct_cpu, 1),
                        "direct_lease_memory_mib": direct_memory,
                        "generic_claim_cpu_cores": round(generic_cpu, 1),
                        "generic_claim_memory_mib": generic_memory,
                    },
                }
            )
        return values

    def _select_host_capacity_unit(
        self,
        session: Session,
        claim_data: ResourceClaimInput,
        *,
        now: datetime,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        candidates: list[dict[str, Any]] = []
        for card in self._host_capacity_cards(session, now, ensure_units=True):
            capacity = card["capacity"]
            excluded_reason = card["admission_reason"]
            eligible = card["admission_state"] == "available"
            if eligible and claim_data.quantities.cpu_cores:
                if capacity["available_cpu_cores"] < claim_data.quantities.cpu_cores:
                    eligible = False
                    excluded_reason = "insufficient_cpu"
            if eligible and claim_data.quantities.memory_mib:
                if capacity["available_memory_mib"] < claim_data.quantities.memory_mib:
                    eligible = False
                    excluded_reason = "insufficient_memory"
            candidates.append({**card, "eligible": eligible, "excluded_reason": excluded_reason})
        eligible_candidates = [candidate for candidate in candidates if candidate["eligible"]]
        if not eligible_candidates:
            return None, candidates
        eligible_candidates.sort(
            key=lambda item: (
                item["capacity"]["available_cpu_cores"] or 0.0,
                item["capacity"]["available_memory_mib"] or 0,
                item["endpoint"]["id"],
            )
        )
        return eligible_candidates[0], candidates

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
        include_reclaimable_keepalive: bool = False,
    ) -> tuple[list[GPUDevice], dict[str, int]]:
        constraints = ResourceConstraints.model_validate(json_load(request.constraints_json))
        lease_end = now + timedelta(seconds=request.duration_seconds)
        excluded: dict[str, int] = defaultdict(int)
        values: list[GPUDevice] = []
        commitment_usage = self._endpoint_commitment_usage(session)
        all_gpus = session.scalars(
            select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
        ).all()
        for gpu in all_gpus:
            if not gpu.present:
                excluded["absent"] += 1
                continue
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None:
                excluded["missing_endpoint"] += 1
                continue
            if endpoint.lifecycle_state != "active":
                excluded["endpoint_lifecycle"] += 1
                continue
            if constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                excluded["endpoint_allowlist"] += 1
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                excluded["endpoint_denylist"] += 1
                continue
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            if (
                constraints.cpu_cores is not None or constraints.memory_mib is not None
            ) and host_telemetry is None:
                excluded["host_telemetry"] += 1
                continue
            if host_telemetry is not None:
                committed_cpu, committed_memory = commitment_usage.get(endpoint.id, (0.0, 0))
                if (
                    constraints.cpu_cores is not None
                    and committed_cpu + constraints.cpu_cores > host_telemetry.cpu_count
                ):
                    excluded["committed_cpu"] += 1
                    continue
                if (
                    constraints.memory_mib is not None
                    and committed_memory + constraints.memory_mib > host_telemetry.memory_total_mib
                ):
                    excluded["committed_memory"] += 1
                    continue
            if constraints.min_available_cpu_cores is not None:
                if host_telemetry is None:
                    excluded["host_telemetry"] += 1
                    continue
                available_cpu_cores = max(0.0, host_telemetry.cpu_count - host_telemetry.load_1m)
                if available_cpu_cores < constraints.min_available_cpu_cores:
                    excluded["available_cpu"] += 1
                    continue
            if constraints.min_available_memory_mib is not None:
                if host_telemetry is None:
                    excluded["host_telemetry"] += 1
                    continue
                if host_telemetry.memory_available_mib < constraints.min_available_memory_mib:
                    excluded["available_memory"] += 1
                    continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                excluded["gpu_allowlist"] += 1
                continue
            if not CUDA_SELECTOR_RE.fullmatch(gpu.gpu_uuid):
                excluded["invalid_cuda_selector"] += 1
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
            if (
                constraints.min_total_vram_mib
                and gpu.total_vram_mib < constraints.min_total_vram_mib
            ):
                excluded["total_vram"] += 1
                continue
            state, _reason = self._gpu_state(session, gpu, now)
            reclaimable_keepalive = False
            occupying_lease = self._active_lease_for_gpu(session, gpu.id)
            if occupying_lease is not None and occupying_lease.kind == "keepalive":
                if not include_reclaimable_keepalive:
                    excluded["keepalive"] += 1
                    continue
                status, _keepalive_reason = self._keepalive_gpu_status(
                    session, gpu, occupying_lease, now
                )
                if occupying_lease.state != "ACTIVE" or status not in {"ON", "OFF"}:
                    excluded["keepalive_not_verified"] += 1
                    continue
                reclaimable_keepalive = True
            elif state != "AVAILABLE":
                # This opt-in is used only by the pure reclaim planner. A
                # normal claim path never treats keepalive capacity as free:
                # an adapter stop plus fresh empty observation must occur
                # before the caller runs its ordinary claim.
                if not include_reclaimable_keepalive or state not in {"KEEPALIVE", "CONFLICT"}:
                    excluded[state.lower()] += 1
                    continue
                excluded[state.lower()] += 1
                continue
            telemetry = self._latest_telemetry(session, gpu.id)
            assert telemetry is not None  # AVAILABLE implies fresh telemetry
            if (
                constraints.min_free_vram_mib is not None
                and telemetry.memory_free_mib < constraints.min_free_vram_mib
                and not reclaimable_keepalive
            ):
                excluded["free_vram"] += 1
                continue
            if self._reservation_blocks_gpu(session, gpu.id, start=now, end=lease_end):
                excluded["future_reservation"] += 1
                continue
            values.append(gpu)
        return values, dict(excluded)

    def plan_keepalive_reclaim(self, request_data: RequestCreate) -> dict[str, Any]:
        """Purely plan exact verified keepalive GPUs that could satisfy a claim.

        The planner deliberately does not alter a lease or adapter. It uses
        the allocator's own filter and topology selector with ``KEEPALIVE``
        admitted only as a temporary planning candidate. A caller may reclaim
        only the returned per-GPU set, obtain fresh empty telemetry, finalize
        those leases, and then run the ordinary claim through its normal
        admission path.
        """

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            request = AllocationRequest(
                id="keepalive-reclaim-plan",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=request_data.project_id,
                profile_id=None,
                auto_activate=False,
                task_ref=request_data.task_ref,
                purpose=request_data.purpose,
                constraints_json=json_dump(request_data.constraints.model_dump(mode="json")),
                duration_seconds=request_data.duration_seconds,
                expected_duration_seconds=None,
                start_after=None,
                deadline=None,
                approval_ref=request_data.approval_ref,
                state="QUEUED",
                priority_class="normal",
                blocked_reason=None,
                created_at=now,
                updated_at=now,
            )
            candidates, excluded = self._eligible_gpus(
                session,
                request=request,
                now=now,
                include_reclaimable_keepalive=True,
            )
            selected = self._select_resources(candidates, request_data.constraints)
            if selected is None:
                return {
                    "snapshot_revision": self._revision(session),
                    "complete": False,
                    "transitions": [],
                    "excluded": excluded,
                }
            transitions: list[dict[str, Any]] = []
            for gpu in selected:
                lease = self._active_lease_for_gpu(session, gpu.id)
                if lease is None or lease.kind != "keepalive":
                    continue
                status, _reason = self._keepalive_gpu_status(session, gpu, lease, now)
                if lease.state != "ACTIVE" or status not in {"ON", "OFF"}:
                    return {
                        "snapshot_revision": self._revision(session),
                        "complete": False,
                        "transitions": [],
                        "excluded": {**excluded, "keepalive_not_verified": 1},
                    }
                transitions.append(
                    {
                        "action": "reclaim",
                        "endpoint_id": gpu.endpoint_id,
                        "gpu_id": gpu.id,
                        "gpu_uuid": gpu.gpu_uuid,
                        "lease_id": lease.id,
                    }
                )
            return {
                "snapshot_revision": self._revision(session),
                "complete": True,
                "transitions": transitions,
                "excluded": excluded,
            }

        return self._read(operation)

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
            .where(
                AllocationRequest.state == "QUEUED",
                AllocationRequest.priority_class == "normal",
            )
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

    def _allocate_queued(
        self,
        session: Session,
        now: datetime,
        revision: int,
        *,
        request_ids: set[str] | None = None,
    ) -> list[str]:
        # ServerPilot no longer keeps a waiting queue. Allocation is attempted
        # only for the request being created; lifecycle hooks must not revive
        # historical QUEUED rows from older versions.
        if not request_ids:
            return []
        allocated: list[str] = []
        requests = [
            request for request in self._queue_candidates(session, now) if request.id in request_ids
        ]
        for request in self._fair_order(session, requests, now):
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
            candidates, excluded = self._eligible_gpus(session, request=request, now=now)
            resources = self._select_resources(candidates, constraints)
            if resources is None:
                top_exclusions = ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(
                        excluded.items(), key=lambda item: (-item[1], item[0])
                    )[:3]
                )
                blocked_reason = f"insufficient eligible GPUs: need {constraints.gpu_count}, have {len(candidates)}"
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
                kind="workload",
                state="HELD",
                issued_at=now,
                expires_at=now + timedelta(seconds=request.duration_seconds),
                last_heartbeat_at=now,
                activated_at=None,
                released_at=None,
                release_reason=None,
                issued_revision=revision,
            )
            session.add(lease)
            session.flush()
            for gpu in resources:
                session.add(
                    LeaseResource(lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None)
                )
            for endpoint_id in sorted({gpu.endpoint_id for gpu in resources}):
                session.add(
                    LeaseEndpointCommitment(
                        lease_id=lease.id,
                        endpoint_id=endpoint_id,
                        cpu_cores=constraints.cpu_cores or 0.0,
                        memory_mib=constraints.memory_mib or 0,
                        created_at=now,
                    )
                )
            request.state = "LEASED"
            request.blocked_reason = None
            request.updated_at = now
            self._audit(
                session,
                actor_id=request.actor_id,
                action="lease.issued",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={
                    "gpu_ids": [gpu.id for gpu in resources],
                    "state": lease.state,
                    "endpoint_commitments": {
                        endpoint_id: {
                            "cpu_cores": constraints.cpu_cores or 0.0,
                            "memory_mib": constraints.memory_mib or 0,
                        }
                        for endpoint_id in sorted({gpu.endpoint_id for gpu in resources})
                    },
                },
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
        idempotency_key: str | None,
        idempotency_action: str,
        activate_if_allocated: bool,
        persistent_lease: bool = False,
        profile_id: str | None = None,
        idempotency_checked: bool = False,
    ) -> dict[str, Any]:
        if not idempotency_checked and idempotency_key is not None:
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
        self._allocate_queued(session, now, revision, request_ids={request.id})
        lease = session.scalar(select(Lease).where(Lease.request_id == request.id))
        if lease is None:
            constraints = request_data.constraints
            raise BrokerError(
                "no_capacity",
                f"没有满足本次申请的可用 GPU（需要 {constraints.gpu_count} 张）",
                status_code=409,
            )
        if persistent_lease:
            lease.expires_at = None
        result = {
            "event_id": event.id,
            "snapshot_revision": revision,
            "request": self._request_dict(request),
            "lease": self._lease_dict(session, lease) if lease else None,
            "authority": "这里只分配 GPU；启动任务仍需遵守项目或资源所有者的授权。",
        }
        if idempotency_key is not None:
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
        idempotency_key: str | None,
        activate_if_allocated: bool = False,
        persistent_lease: bool = False,
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
                persistent_lease=persistent_lease,
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
            if request.actor_id != actor.id:
                raise BrokerError(
                    "request_forbidden", "cannot cancel another actor's request", status_code=403
                )
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
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.activate", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "cannot activate another actor's lease", status_code=403
                )
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
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.renew", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "不能续期其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_renewable",
                    "当前状态不能续期这个 GPU 租约",
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
                if self._reservation_blocks_gpu(
                    session, resource.gpu_id, start=now, end=renewed_expiry
                )
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
        idempotency_key: str | None,
        operator_override: bool = False,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)
        if operator_override and actor.role not in {"operator", "admin"}:
            raise BrokerError(
                "operator_role_required",
                "human lease correction requires an operator role",
                status_code=403,
            )
        if not reason.strip():
            raise BrokerError("release_reason_required", "释放 GPU 时需要提供原因", status_code=422)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            if idempotency_key is not None:
                existing = self._idempotent(
                    session, actor=actor, action="lease.release", key=idempotency_key
                )
                if existing is not None:
                    return existing
            if not self._can_manage_lease(actor, lease) and not operator_override:
                raise BrokerError(
                    "lease_forbidden", "不能释放其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state in TERMINAL_LEASE_STATES:
                raise BrokerError(
                    "lease_already_released", "这个 GPU 租约已经结束", status_code=409
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = reason.strip()[:500]
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all():
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
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
            if idempotency_key is not None:
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

    def _workload_reassignment_targets(
        self,
        session: Session,
        lease_id: str,
        gpu_ids: list[str],
    ) -> tuple[Lease, list[LeaseResource], list[GPUDevice]]:
        lease = session.get(Lease, lease_id)
        if lease is None:
            raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
        if lease.kind != "workload" or lease.state not in ACTIVE_LEASE_STATES:
            raise BrokerError(
                "workload_lease_required",
                "only a current workload assignment can be moved",
                status_code=409,
            )
        current_resources = session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease.id,
                LeaseResource.active.is_(True),
            )
        ).all()
        if len(gpu_ids) != len(current_resources) or len(set(gpu_ids)) != len(gpu_ids):
            raise BrokerError(
                "gpu_count_mismatch",
                "select the same number of distinct GPUs as the current task",
                status_code=422,
                details={"gpu_count": len(current_resources)},
            )
        target_gpus = [session.get(GPUDevice, gpu_id) for gpu_id in gpu_ids]
        if any(gpu is None for gpu in target_gpus):
            raise BrokerError(
                "gpu_not_found",
                "one or more selected GPUs do not exist",
                status_code=404,
            )
        return lease, current_resources, [gpu for gpu in target_gpus if gpu is not None]

    def keepalive_reclaim_request_for_reassignment(
        self,
        actor: ActorContext,
        lease_id: str,
        gpu_ids: list[str],
    ) -> RequestCreate | None:
        """Describe only the selected occupancy GPUs that block an APP move."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> RequestCreate | None:
            lease, _current_resources, _target_gpus = self._workload_reassignment_targets(
                session, lease_id, gpu_ids
            )
            occupied = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.gpu_id.in_(gpu_ids),
                    LeaseResource.active.is_(True),
                    LeaseResource.lease_id != lease.id,
                )
            ).all()
            if not occupied:
                return None
            keeper_gpu_ids: list[str] = []
            for resource in occupied:
                occupying_lease = session.get(Lease, resource.lease_id)
                if occupying_lease is None or occupying_lease.kind != "keepalive":
                    raise BrokerError(
                        "gpu_already_assigned",
                        "move or release the task already assigned to the selected GPU first",
                        status_code=409,
                        details={"gpu_ids": sorted({item.gpu_id for item in occupied})},
                    )
                keeper_gpu_ids.append(resource.gpu_id)
            return RequestCreate.model_validate(
                {
                    "project_id": lease.project_id,
                    "task_ref": f"reassign:{lease.id}",
                    "purpose": "APP task GPU reassignment",
                    "constraints": {
                        "gpu_count": len(keeper_gpu_ids),
                        "gpu_ids": sorted(keeper_gpu_ids),
                        "placement": "exact",
                    },
                }
            )

        return self._read(operation)

    def reassign_lease_gpus(
        self,
        actor: ActorContext,
        lease_id: str,
        gpu_ids: list[str],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Move a workload assignment to the exact GPUs chosen in the APP.

        This changes ServerPilot's assignment only. The operator or owning Agent
        restarts the workload with the returned CUDA selectors.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="lease.gpus.reassign", key=idempotency_key
            )
            if existing is not None:
                return existing
            lease, current_resources, target_gpus = self._workload_reassignment_targets(
                session, lease_id, gpu_ids
            )
            occupied = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.gpu_id.in_(gpu_ids),
                    LeaseResource.active.is_(True),
                    LeaseResource.lease_id != lease.id,
                )
            ).all()
            if occupied:
                raise BrokerError(
                    "gpu_already_assigned",
                    "move or release the task already assigned to the selected GPU first",
                    status_code=409,
                    details={"gpu_ids": sorted({item.gpu_id for item in occupied})},
                )

            now = utcnow()
            before = self._lease_dict(session, lease)
            for resource in current_resources:
                resource.active = False
                resource.released_at = now
            session.flush()
            for gpu_id in gpu_ids:
                resource = session.scalar(
                    select(LeaseResource).where(
                        LeaseResource.lease_id == lease.id,
                        LeaseResource.gpu_id == gpu_id,
                    )
                )
                if resource is None:
                    session.add(
                        LeaseResource(
                            lease_id=lease.id,
                            gpu_id=gpu_id,
                            active=True,
                            released_at=None,
                        )
                    )
                else:
                    resource.active = True
                    resource.released_at = None
            session.execute(delete(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id))
            session.execute(
                delete(LeaseEndpointCommitment).where(LeaseEndpointCommitment.lease_id == lease.id)
            )
            request = session.get(AllocationRequest, lease.request_id)
            constraints = (
                ResourceConstraints.model_validate(json_load(request.constraints_json))
                if request is not None
                else None
            )
            endpoint_ids = sorted({gpu.endpoint_id for gpu in target_gpus})
            for endpoint_id in endpoint_ids:
                session.add(
                    LeaseEndpointCommitment(
                        lease_id=lease.id,
                        endpoint_id=endpoint_id,
                        cpu_cores=constraints.cpu_cores
                        if constraints and constraints.cpu_cores
                        else 0.0,
                        memory_mib=constraints.memory_mib
                        if constraints and constraints.memory_mib
                        else 0,
                        created_at=now,
                    )
                )
            if request is not None and constraints is not None:
                request.constraints_json = json_dump(
                    constraints.model_copy(
                        update={
                            "gpu_ids": list(gpu_ids),
                            "endpoint_ids": endpoint_ids,
                            "placement": "exact",
                        }
                    ).model_dump(mode="json")
                )
                request.updated_at = now
            revision = self._bump_revision(session, now)
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.gpus_reassigned",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"gpu_ids": gpu_ids},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "restart_required": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.gpus.reassign",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def release_empty_conflicted_lease(
        self,
        actor: ActorContext,
        endpoint_id: str,
        lease_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Release one empty workload or keepalive lease after fresh evidence.

        A conflict is deliberately not auto-cleared by collection: it means a
        prior lease/process binding diverged, and the original actor may have
        left a real workload behind.  Endpoint operators get this narrow
        recovery path only after the REST layer has collected the endpoint and
        this method verifies a complete, fresh, process-free observation for
        every GPU still owned by the lease. GPU utilization alone is never
        treated as proof of emptiness. The same proof applies to internal
        per-GPU keepalive leases so a failed stop cannot permanently wedge a
        GPU that a human needs to recover.
        """

        self._require_role(actor, MUTATING_ROLES)
        barrier = ensure_utc(observation_not_before)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="lease.empty.release",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            if lease.kind not in {"workload", "keepalive"} or lease.state not in {
                "HELD",
                "ACTIVE",
                "ORPHANED_BUSY",
                "CONFLICT",
            }:
                raise BrokerError(
                    "empty_workload_lease_required",
                    "only a held, active, orphaned, or conflicted workload/keepalive lease can be cleared here",
                    status_code=409,
                )
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            if not resources:
                raise BrokerError(
                    "lease_has_no_resources",
                    "the lease has no active GPU resources",
                    status_code=409,
                )
            gpu_ids = {resource.gpu_id for resource in resources}
            gpus = [session.get(GPUDevice, gpu_id) for gpu_id in gpu_ids]
            if any(gpu is None or gpu.endpoint_id != endpoint_id for gpu in gpus):
                raise BrokerError(
                    "lease_endpoint_mismatch",
                    "the lease does not belong entirely to this server",
                    status_code=409,
                )

            now = utcnow()
            cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            host = session.get(EndpointTelemetryCurrent, endpoint_id)
            provider_state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == "raw-ssh",
                    ProviderState.endpoint_id == endpoint_id,
                )
            )
            host_collected_at = _as_utc(host.collected_at) if host is not None else None
            last_success_at = (
                _as_utc(provider_state.last_success_at) if provider_state is not None else None
            )
            if (
                host_collected_at is None
                or host_collected_at < barrier
                or host_collected_at < cutoff
                or provider_state is None
                or provider_state.last_error is not None
                or last_success_at is None
                or last_success_at < barrier
            ):
                raise BrokerError(
                    "conflict_observation_stale",
                    "需要一次完整且最新的服务器采集后才能释放空闲占用",
                    status_code=409,
                )

            for gpu in gpus:
                assert gpu is not None
                telemetry = session.get(TelemetryCurrent, gpu.id)
                telemetry_collected_at = (
                    _as_utc(telemetry.collected_at) if telemetry is not None else None
                )
                observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
                if (
                    not gpu.present
                    or telemetry_collected_at is None
                    or telemetry_collected_at < barrier
                    or observed_at is None
                    or observed_at < cutoff
                ):
                    raise BrokerError(
                        "conflict_observation_incomplete",
                        "最新采集没有完整覆盖这张 GPU，暂不释放占用",
                        status_code=409,
                        details={"gpu_id": gpu.id},
                    )
                processes = self._current_processes(session, gpu.id, now)
                if processes:
                    raise BrokerError(
                        "conflict_process_present",
                        "仍观察到运行中的进程，不能释放这张 GPU 的占用",
                        status_code=409,
                        details={"gpu_id": gpu.id, "process_count": len(processes)},
                    )

            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = "empty fresh observation cleared endpoint ownership"
            for resource in resources:
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.empty_cleared",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={
                    "source": "endpoint_operator",
                    "endpoint_id": endpoint_id,
                    "lease_kind": lease.kind,
                },
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "released": True,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.empty.release",
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
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.bind", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "不能绑定其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_bindable", "only held or active leases can be bound", status_code=409
                )
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
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.bind_observed", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "cannot bind another actor's lease", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE", "CONFLICT"}:
                raise BrokerError(
                    "lease_not_bindable",
                    "当前状态不能绑定这个 GPU 租约",
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
                raise BrokerError("lease_has_no_resources", "这个租约没有活动 GPU", status_code=409)
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
                    "每张已分配 GPU 都检测到新的任务进程后才能完成绑定",
                    status_code=409,
                    details={"missing_gpu_ids": missing_gpu_ids},
                )
            process_keys = sorted({self._process_key(process) for process in processes})
            run_id = binding.run_id or f"explicit:lease:{lease.id}"
            revision = self._bump_revision(session, now)
            if binding.run_id is None:
                collector_bindings = session.scalars(
                    select(WorkloadBinding).where(
                        WorkloadBinding.lease_id == lease.id,
                        WorkloadBinding.run_id.in_(
                            {f"collector:lease:{lease.id}", f"lease:{lease.id}"}
                        ),
                    )
                ).all()
                for collector_binding in collector_bindings:
                    session.delete(collector_binding)
                if collector_bindings:
                    session.flush()
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
            should_promote = lease.state in {"HELD", "CONFLICT"}
            if was_conflict:
                # A lease owner explicitly confirming the current, freshly
                # observed process identities is the safe recovery action for
                # an attribution conflict. It changes no remote workload;
                # the lease remains blocked by any future unknown process.
                lease.state = "HELD"
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
            if should_promote:
                before_activation = (
                    conflict_before if was_conflict else self._lease_dict(session, lease)
                )
                lease.state = "ACTIVE"
                lease.activated_at = lease.activated_at or now
                lease.last_heartbeat_at = now
                request = session.get(AllocationRequest, lease.request_id)
                if request is not None:
                    request.state = "ACTIVE"
                    request.updated_at = now
                self._audit(
                    session,
                    actor_id=actor.id,
                    action="lease.activated",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=before_activation,
                    after=self._lease_dict(session, lease),
                    summary={"activation": "observed_workload_bind", "run_id": run_id},
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

        self._resolve_stale_lease_alerts(session, now)
        leases = session.scalars(select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))).all()
        expired_released = False
        for lease in leases:
            if lease.kind == "keepalive":
                # Keepalive is durable controller ownership, not a timed user
                # workload. Current worker liveness is projected separately as
                # ON/OFF/ERROR and the endpoint policy drives reconciliation.
                lease.expires_at = None
                continue
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            processes = [
                process
                for resource in resources
                for process in self._current_processes(session, resource.gpu_id, now)
            ]
            expires_at = _as_utc(lease.expires_at)
            if lease.state in {"HELD", "ACTIVE"} and expires_at is not None and expires_at <= now:
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
                    summary={"reason": lease.release_reason},
                    now=now,
                )
            elif lease.state in {"HELD", "ACTIVE"} and processes:
                known_process_keys = self._binding_process_keys(session, lease.id)
                unexpected_processes = [
                    process
                    for process in processes
                    if self._process_key(process) not in known_process_keys
                ]
                if (
                    known_process_keys
                    and unexpected_processes
                    and any(process.observations >= 2 for process in unexpected_processes)
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
        self._resolve_stale_lease_alerts(session, now)
        if expired_released:
            self._allocate_queued(session, now, self._revision(session))

    def reconcile(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

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
        for gpu in session.scalars(
            select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
        ).all():
            if not gpu.present:
                continue
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None or not endpoint.enabled or not gpu.enabled:
                continue
            if constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                continue
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            if constraints.min_available_cpu_cores is not None:
                if host_telemetry is None:
                    continue
                if (
                    max(0.0, host_telemetry.cpu_count - host_telemetry.load_1m)
                    < constraints.min_available_cpu_cores
                ):
                    continue
            if constraints.min_available_memory_mib is not None:
                if (
                    host_telemetry is None
                    or host_telemetry.memory_available_mib < constraints.min_available_memory_mib
                ):
                    continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                continue
            if gpu.id in constraints.deny_gpu_ids:
                continue
            if (
                constraints.min_total_vram_mib
                and gpu.total_vram_mib < constraints.min_total_vram_mib
            ):
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
                    raise BrokerError(
                        "gpu_not_found", f"GPU {gpu_id} does not exist", status_code=404
                    )
                if not gpu.present:
                    raise BrokerError(
                        "gpu_absent",
                        f"GPU {gpu_id} is absent from the latest complete endpoint observation",
                        status_code=409,
                    )
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
                if any(
                    (expires_at := _as_utc(lease.expires_at)) is None or expires_at > start_at
                    for lease in leases
                ):
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
                raise BrokerError(
                    "reservation_not_found", "reservation does not exist", status_code=404
                )
            if reservation.actor_id != actor.id:
                raise BrokerError(
                    "reservation_forbidden",
                    "cannot cancel another actor's reservation",
                    status_code=403,
                )
            if reservation.state != "ACTIVE":
                raise BrokerError(
                    "reservation_not_cancellable", "reservation is not active", status_code=409
                )
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
            if endpoint_id:
                endpoint = session.get(Endpoint, endpoint_id)
                if endpoint is None:
                    raise BrokerError(
                        "endpoint_not_found", "maintenance endpoint does not exist", status_code=404
                    )
                self._require_endpoint_manager(actor, endpoint)
            if gpu_id:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None:
                    raise BrokerError(
                        "gpu_not_found", "maintenance GPU does not exist", status_code=404
                    )
                endpoint = session.get(Endpoint, gpu.endpoint_id)
                if endpoint is None:
                    raise BrokerError(
                        "endpoint_not_found", "GPU endpoint does not exist", status_code=404
                    )
                self._require_endpoint_manager(actor, endpoint)
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
            existing = self._idempotent(
                session, actor=actor, action="alert.ack", key=idempotency_key
            )
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

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="telemetry.prune", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            cutoff = now - timedelta(seconds=retention.older_than_seconds)
            revision = self._bump_revision(session, now)
            gpu_deleted = (
                session.execute(
                    delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
                ).rowcount
                or 0
            )
            endpoint_deleted = (
                session.execute(
                    delete(EndpointTelemetrySnapshot).where(
                        EndpointTelemetrySnapshot.observed_at < cutoff
                    )
                ).rowcount
                or 0
            )
            deleted = gpu_deleted + endpoint_deleted
            event = self._audit(
                session,
                actor_id=actor.id,
                action="telemetry.pruned",
                resource_type="telemetry",
                resource_id="history",
                result="success",
                after={
                    "deleted_count": deleted,
                    "gpu_deleted_count": gpu_deleted,
                    "endpoint_deleted_count": endpoint_deleted,
                    "cutoff": _iso(cutoff),
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "deleted_count": deleted,
                "gpu_deleted_count": gpu_deleted,
                "endpoint_deleted_count": endpoint_deleted,
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
            endpoint_id
            for endpoint_id in constraints.endpoint_ids
            if session.get(Endpoint, endpoint_id) is None
        ]
        if missing:
            raise BrokerError(
                "endpoint_not_found",
                f"workload profile references unknown endpoints: {missing}",
                status_code=404,
            )

    @staticmethod
    def _workload_profile_grants(session: Session, profile_id: str) -> list[str]:
        return list(
            session.scalars(
                select(WorkloadProfileGrant.project_id)
                .where(WorkloadProfileGrant.profile_id == profile_id)
                .order_by(WorkloadProfileGrant.project_id)
            ).all()
        )

    @classmethod
    def _profile_allows_project(
        cls,
        session: Session,
        profile: WorkloadProfile,
        project_id: str,
    ) -> bool:
        return (
            profile.project_id == project_id
            or profile.grant_all_projects
            or project_id in cls._workload_profile_grants(session, profile.id)
        )

    def upsert_workload_profile(
        self,
        actor: ActorContext,
        profile_data: WorkloadProfileUpsert,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Store an admin-approved routine workload contract for one project."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="workload_profile.upsert", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            self._ensure_claim_project(session, profile_data.project_id, now)
            for project_id in profile_data.grant_project_ids:
                self._ensure_claim_project(session, project_id, now)
            if profile_data.runtime_kind == "direct-gpu":
                self._validate_workload_profile_endpoints(
                    session,
                    constraints=profile_data.constraints,
                )
            else:
                target = session.get(SchedulerTarget, profile_data.scheduler_target_id)
                if target is None:
                    raise BrokerError(
                        "scheduler_target_not_found",
                        "scheduler target does not exist",
                        status_code=404,
                    )
                if not target.enabled:
                    raise BrokerError(
                        "scheduler_target_disabled",
                        "scheduler target is disabled",
                        status_code=409,
                    )
            revision = self._bump_revision(session, now)
            profile = session.get(WorkloadProfile, profile_data.id)
            before = (
                self._workload_profile_dict(
                    profile,
                    self._workload_profile_grants(session, profile.id),
                )
                if profile
                else None
            )
            if profile is None:
                profile = WorkloadProfile(
                    id=profile_data.id,
                    project_id=profile_data.project_id,
                    display_name=profile_data.display_name,
                    purpose=profile_data.purpose,
                    duration_seconds=profile_data.duration_seconds,
                    constraints_json=json_dump(profile_data.constraints.model_dump(mode="json")),
                    runtime_kind=profile_data.runtime_kind,
                    scheduler_target_id=profile_data.scheduler_target_id,
                    scheduler_spec_json=(
                        json_dump(profile_data.scheduler.model_dump(mode="json"))
                        if profile_data.scheduler is not None
                        else None
                    ),
                    scheduler_script=profile_data.scheduler_script,
                    grant_all_projects=profile_data.grant_all_projects,
                    retain_submission_body=profile_data.retain_submission_body,
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
                profile.constraints_json = json_dump(
                    profile_data.constraints.model_dump(mode="json")
                )
                profile.runtime_kind = profile_data.runtime_kind
                profile.scheduler_target_id = profile_data.scheduler_target_id
                profile.scheduler_spec_json = (
                    json_dump(profile_data.scheduler.model_dump(mode="json"))
                    if profile_data.scheduler is not None
                    else None
                )
                profile.scheduler_script = profile_data.scheduler_script
                profile.grant_all_projects = profile_data.grant_all_projects
                profile.retain_submission_body = profile_data.retain_submission_body
                profile.enabled = profile_data.enabled
                profile.updated_at = now
            session.flush()
            session.execute(
                delete(WorkloadProfileGrant).where(WorkloadProfileGrant.profile_id == profile.id)
            )
            for project_id in profile_data.grant_project_ids:
                if project_id != profile.project_id:
                    session.add(
                        WorkloadProfileGrant(
                            profile_id=profile.id,
                            project_id=project_id,
                        )
                    )
            session.flush()
            after = self._workload_profile_dict(
                profile,
                self._workload_profile_grants(session, profile.id),
            )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="workload_profile.upserted",
                resource_type="workload_profile",
                resource_id=profile.id,
                result="success",
                before=before,
                after=after,
                summary={
                    "project_id": profile.project_id,
                    "runtime_kind": profile.runtime_kind,
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "workload_profile": after,
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

    @staticmethod
    def _workload_profile_request_data(
        session: Session,
        profile_id: str,
        claim: WorkloadProfileClaim,
    ) -> tuple[WorkloadProfile, RequestCreate]:
        """Resolve one direct-GPU profile to its exact claim input."""

        profile = session.get(WorkloadProfile, profile_id)
        if profile is None:
            raise BrokerError(
                "workload_profile_not_found", "workload profile does not exist", status_code=404
            )
        if not profile.enabled:
            raise BrokerError(
                "workload_profile_disabled", "workload profile is disabled", status_code=409
            )
        if profile.runtime_kind != "direct-gpu":
            raise BrokerError(
                "scheduler_profile_requires_submit",
                "external scheduler profiles must use the scheduler submit operation",
                status_code=409,
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
        return profile, request_data

    def workload_profile_claim_request(
        self,
        actor: ActorContext,
        profile_id: str,
        claim: WorkloadProfileClaim,
    ) -> RequestCreate:
        """Read the direct-GPU request fixed by a workload profile.

        This supports the API's exact keepalive reclaim proof.  It never
        creates a request or chooses capacity on its own.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> RequestCreate:
            _profile, request_data = self._workload_profile_request_data(session, profile_id, claim)
            return request_data

        return self._read(operation)

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
            profile, request_data = self._workload_profile_request_data(session, profile_id, claim)
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

    def upsert_scheduler_target(
        self,
        actor: ActorContext,
        target_data: SchedulerTargetUpsert,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Register non-secret metadata for a globally discoverable scheduler."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="scheduler_target.upsert",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            now = utcnow()
            target = session.get(SchedulerTarget, target_data.id)
            before = self._scheduler_target_dict(target) if target else None
            connection = {
                "transport_profile": target_data.transport_profile,
                "inspection_profile": target_data.inspection_profile,
                "upload": (
                    target_data.upload.model_dump(mode="json")
                    if target_data.upload is not None
                    else None
                ),
            }
            if target is None:
                target = SchedulerTarget(
                    id=target_data.id,
                    display_name=target_data.display_name,
                    adapter=target_data.adapter,
                    connection_json=json_dump(connection),
                    credential_refs_json=json_dump(target_data.credential_refs),
                    capabilities_json=json_dump(target_data.capabilities),
                    access_hint=target_data.access_hint,
                    enabled=target_data.enabled,
                    created_at=now,
                    updated_at=now,
                )
                session.add(target)
            else:
                target.display_name = target_data.display_name
                target.adapter = target_data.adapter
                target.connection_json = json_dump(connection)
                target.credential_refs_json = json_dump(target_data.credential_refs)
                target.capabilities_json = json_dump(target_data.capabilities)
                target.access_hint = target_data.access_hint
                target.enabled = target_data.enabled
                target.updated_at = now
            revision = self._bump_revision(session, now)
            after = self._scheduler_target_dict(target)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="scheduler_target.upserted",
                resource_type="scheduler_target",
                resource_id=target.id,
                result="success",
                before=before,
                after=after,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "scheduler_target": after,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="scheduler_target.upsert",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def list_scheduler_targets(self, actor: ActorContext) -> dict[str, Any]:
        """List scheduler identities globally without treating login nodes as endpoints."""

        def operation(session: Session) -> dict[str, Any]:
            values = [
                self._scheduler_target_dict(target)
                for target in session.scalars(
                    select(SchedulerTarget).order_by(SchedulerTarget.id)
                ).all()
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def _scheduler_target_context(
        self,
        target_id: str,
        *,
        required_capability: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        def operation(
            session: Session,
        ) -> tuple[dict[str, Any], dict[str, Any], int]:
            target = session.get(SchedulerTarget, target_id)
            if target is None:
                raise BrokerError(
                    "scheduler_target_not_found",
                    "scheduler target does not exist",
                    status_code=404,
                )
            if not target.enabled:
                raise BrokerError(
                    "scheduler_target_disabled",
                    "scheduler target is disabled",
                    status_code=409,
                )
            capabilities = json_load(target.capabilities_json)
            if required_capability is not None and required_capability not in capabilities:
                raise BrokerError(
                    "scheduler_capability_disabled",
                    f"scheduler target does not allow {required_capability}",
                    status_code=409,
                )
            connection = json_load(target.connection_json)
            if not isinstance(connection, dict):
                raise BrokerError(
                    "scheduler_target_invalid",
                    "scheduler target connection metadata is invalid",
                    status_code=500,
                )
            return self._scheduler_target_dict(target), connection, self._revision(session)

        return self._read(operation)

    def _defer_interrupted_scheduler_transfers(self, session: Session, now: datetime) -> None:
        """Make restart-interrupted staged uploads terminal and non-resumable.

        An upload may have reached the remote stage just as the daemon exits.
        Retrying it automatically could overwrite intent or duplicate data, so
        recovery is an explicit fresh, approved transfer instead.
        """

        transfers = session.scalars(
            select(SchedulerTransfer).where(SchedulerTransfer.state == "TRANSFERRING")
        ).all()
        for transfer in transfers:
            transfer.state = "DEFERRED"
            transfer.error_message = (
                "daemon restart interrupted staged upload; transfer was not resumed automatically"
            )
            transfer.updated_at = now
            transfer.completed_at = now
            self._audit(
                session,
                actor_id=transfer.actor_id,
                action="scheduler_transfer.deferred",
                resource_type="scheduler_transfer",
                resource_id=transfer.id,
                result="success",
                after=self._scheduler_transfer_dict(transfer),
                summary={"reason": "daemon_restart", "requires_new_approved_transfer": True},
                now=now,
            )

    def scheduler_access_status(
        self,
        actor: ActorContext,
        target_id: str,
    ) -> dict[str, Any]:
        target, connection, revision = self._scheduler_target_context(
            target_id,
            required_capability="access-status",
        )
        access = self.slurm_provider.access_status(connection)
        if access.get("status") != "ready":
            access["access_hint"] = target["access_hint"]

        def record(
            session: Session,
        ) -> tuple[dict[str, Any], int]:
            stored = session.get(SchedulerTarget, target_id)
            assert stored is not None
            now = utcnow()
            stored.access_status = str(access.get("status") or "unavailable")[:40]
            message = access.get("message")
            stored.access_message = str(message)[:2000] if message else None
            stored.access_checked_at = now
            stored.updated_at = now
            return self._scheduler_target_dict(stored), self._bump_revision(session, now)

        target, revision = self._write(record)
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_revision": revision,
            "target": target,
            "access": access,
        }

    def _perform_scheduler_upload(self, transfer_id: str) -> None:
        def resolve(
            session: Session,
        ) -> tuple[SchedulerTransfer, dict[str, Any]]:
            transfer = session.get(SchedulerTransfer, transfer_id)
            if transfer is None:
                raise BrokerError(
                    "scheduler_transfer_not_found",
                    "scheduler transfer does not exist",
                    status_code=404,
                )
            target = session.get(SchedulerTarget, transfer.target_id)
            if target is None:
                raise BrokerError(
                    "scheduler_target_not_found",
                    "scheduler target does not exist",
                    status_code=404,
                )
            connection = json_load(target.connection_json)
            assert isinstance(connection, dict)
            if transfer.state != "TRANSFERRING":
                raise BrokerError(
                    "scheduler_transfer_not_runnable",
                    "only a newly started transfer may run",
                    status_code=409,
                )
            return transfer, connection

        transfer, connection = self._read(resolve)
        try:
            remote_staged_path = self.slurm_provider.upload(
                connection,
                local_path=Path(transfer.local_source_path),
                remote_directory=transfer.remote_directory,
                transfer_id=transfer.id,
            )
        except SlurmProviderError as exc:
            state = (
                "UNKNOWN"
                if exc.uncertain
                else "ACCESS_REQUIRED"
                if exc.access_required
                else "FAILED"
            )
            message = str(exc)[:2000]

            def record_failure(session: Session) -> None:
                current = session.get(SchedulerTransfer, transfer_id)
                if current is None:
                    return
                now = utcnow()
                current.state = state
                current.error_message = message
                current.updated_at = now
                if state == "UNKNOWN":
                    current.completed_at = now
                self._audit(
                    session,
                    actor_id=current.actor_id,
                    action="scheduler_transfer.failed",
                    resource_type="scheduler_transfer",
                    resource_id=current.id,
                    result="failure",
                    after=self._scheduler_transfer_dict(current),
                    summary={"state": state},
                    now=now,
                )
                self._bump_revision(session, now)

            self._write(record_failure)
            return

        def record_success(session: Session) -> None:
            current = session.get(SchedulerTransfer, transfer_id)
            if current is None:
                return
            now = utcnow()
            current.state = "COMPLETED"
            current.remote_staged_path = remote_staged_path
            current.error_message = None
            current.updated_at = now
            current.completed_at = now
            payload = self._scheduler_transfer_dict(current)
            self._audit(
                session,
                actor_id=current.actor_id,
                action="scheduler_transfer.completed",
                resource_type="scheduler_transfer",
                resource_id=current.id,
                result="success",
                after=payload,
                summary={
                    "target_id": current.target_id,
                    "project_id": current.project_id,
                    "remote_staged_path": remote_staged_path,
                },
                now=now,
            )
            self._bump_revision(session, now)

        self._write(record_success)

    def start_scheduler_upload(
        self,
        actor: ActorContext,
        request: SchedulerUploadRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start an approved upload into a unique non-overwriting remote stage."""

        self._require_role(actor, MUTATING_ROLES)
        target, connection, _revision = self._scheduler_target_context(
            request.target_id,
            required_capability="data-transfer",
        )
        access = self.slurm_provider.access_status(connection)
        if access.get("status") != "ready":
            raise BrokerError(
                "access_required",
                "scheduler access is not ready; connect the approved VPN and retry",
                status_code=409,
                details={
                    "target_id": request.target_id,
                    "access": access,
                    "access_hint": target["access_hint"],
                },
            )
        try:
            local_path = Path(request.local_path).resolve(strict=True)
        except OSError as exc:
            raise BrokerError(
                "local_source_not_found",
                "local upload source does not exist",
                status_code=404,
            ) from exc
        if not (local_path.is_file() or local_path.is_dir()):
            raise BrokerError(
                "local_source_unsupported",
                "local upload source must be a regular file or directory",
                status_code=422,
            )
        if not re.fullmatch(r"[A-Za-z0-9._@+-]{1,255}", local_path.name):
            raise BrokerError(
                "local_source_name_unsupported",
                "local source basename contains unsupported characters",
                status_code=422,
            )

        def prepare(session: Session) -> tuple[str, bool]:
            self._idempotent(
                session,
                actor=actor,
                action="scheduler_transfer.start",
                key=idempotency_key,
            )
            existing = session.scalar(
                select(SchedulerTransfer).where(
                    SchedulerTransfer.actor_id == actor.id,
                    SchedulerTransfer.submission_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.target_id != request.target_id
                    or existing.project_id != request.project_id
                    or existing.local_source_path != str(local_path)
                    or existing.remote_directory != request.remote_directory
                ):
                    raise BrokerError(
                        "idempotency_conflict",
                        "idempotency key was already used for different upload input",
                        status_code=409,
                    )
                return existing.id, False
            now = utcnow()
            self._ensure_claim_project(session, request.project_id, now)
            transfer = SchedulerTransfer(
                id=secrets.token_hex(16),
                target_id=request.target_id,
                actor_id=actor.id,
                project_id=request.project_id,
                submission_key=idempotency_key,
                approval_ref=request.approval_ref,
                local_source_path=str(local_path),
                remote_directory=request.remote_directory,
                remote_staged_path=None,
                source_size_bytes=local_path.stat().st_size if local_path.is_file() else None,
                state="TRANSFERRING",
                error_message=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            session.add(transfer)
            revision = self._bump_revision(session, now)
            self._audit(
                session,
                actor_id=actor.id,
                action="scheduler_transfer.started",
                resource_type="scheduler_transfer",
                resource_id=transfer.id,
                result="success",
                after=self._scheduler_transfer_dict(transfer),
                summary={
                    "target_id": transfer.target_id,
                    "project_id": transfer.project_id,
                    "snapshot_revision": revision,
                },
                now=now,
            )
            return transfer.id, True

        with self._scheduler_transfer_lock:
            transfer_id, created = self._write(prepare)
            if created:
                threading.Thread(
                    target=self._perform_scheduler_upload,
                    args=(transfer_id,),
                    name=f"serverpilot-upload-{transfer_id[:8]}",
                    daemon=True,
                ).start()

        return self.scheduler_transfer_status(actor, transfer_id)

    @staticmethod
    def _scheduler_transfer_visible(
        actor: ActorContext,
        transfer: SchedulerTransfer,
    ) -> bool:
        return transfer.actor_id == actor.id

    def scheduler_transfer_status(
        self,
        actor: ActorContext,
        transfer_id: str,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            transfer = session.get(SchedulerTransfer, transfer_id)
            if transfer is None:
                raise BrokerError(
                    "scheduler_transfer_not_found",
                    "scheduler transfer does not exist",
                    status_code=404,
                )
            if not self._scheduler_transfer_visible(actor, transfer):
                raise BrokerError(
                    "scheduler_transfer_forbidden",
                    "scheduler transfer is not visible to this actor",
                    status_code=403,
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "snapshot_revision": self._revision(session),
                "scheduler_transfer": self._scheduler_transfer_dict(transfer),
            }

        return self._read(operation)

    def list_scheduler_transfers(
        self,
        actor: ActorContext,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            transfers = session.scalars(
                select(SchedulerTransfer).order_by(SchedulerTransfer.created_at.desc())
            ).all()
            values = [
                self._scheduler_transfer_dict(transfer)
                for transfer in transfers
                if self._scheduler_transfer_visible(actor, transfer)
                and (project_id is None or transfer.project_id == project_id)
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def _scheduler_job_payload(
        self,
        session: Session,
        job: SchedulerJob,
    ) -> dict[str, Any]:
        events = session.scalars(
            select(SchedulerJobEvent)
            .where(SchedulerJobEvent.job_id == job.id)
            .order_by(SchedulerJobEvent.id)
        ).all()
        return self._scheduler_job_dict(job, events)

    @staticmethod
    def _scheduler_job_visible(actor: ActorContext, job: SchedulerJob) -> bool:
        return job.actor_id == actor.id

    def _submit_scheduler_job(
        self,
        actor: ActorContext,
        *,
        target_id: str,
        project_id: str,
        profile_id: str | None,
        task_ref: str,
        purpose: str,
        approval_ref: str | None,
        request: dict[str, Any],
        script_body: str,
        retain_submission_body: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)
        target, connection, _revision = self._scheduler_target_context(
            target_id,
            required_capability="submit",
        )
        access = self.slurm_provider.access_status(connection)
        if access.get("status") != "ready":
            raise BrokerError(
                "access_required",
                "scheduler access is not ready; connect the approved VPN and retry",
                status_code=409,
                details={
                    "target_id": target_id,
                    "access": access,
                    "access_hint": target["access_hint"],
                },
            )

        def prepare(
            session: Session,
        ) -> tuple[str, bool, bool, dict[str, Any] | None]:
            prior = self._idempotent(
                session,
                actor=actor,
                action="scheduler_job.submit",
                key=idempotency_key,
            )
            if prior is not None:
                return prior["scheduler_job"]["id"], False, False, prior
            existing_job = session.scalar(
                select(SchedulerJob).where(
                    SchedulerJob.actor_id == actor.id,
                    SchedulerJob.submission_key == idempotency_key,
                )
            )
            if existing_job is not None:
                if (
                    existing_job.target_id != target_id
                    or existing_job.project_id != project_id
                    or existing_job.task_ref != task_ref
                    or existing_job.request_json != json_dump(request)
                ):
                    raise BrokerError(
                        "idempotency_conflict",
                        "idempotency key was already used for different scheduler input",
                        status_code=409,
                    )
                retryable = existing_job.state in {
                    "SUBMITTING",
                    "ACCESS_REQUIRED",
                    "FAILED",
                }
                recover_unknown = existing_job.state == "UNKNOWN"
                if retryable and existing_job.state != "SUBMITTING":
                    now = utcnow()
                    existing_job.state = "SUBMITTING"
                    existing_job.error_message = None
                    existing_job.updated_at = now
                    session.add(
                        SchedulerJobEvent(
                            job_id=existing_job.id,
                            state="SUBMITTING",
                            raw_state=existing_job.raw_state,
                            detail_json=json_dump({"retry": True}),
                            created_at=now,
                        )
                    )
                    self._bump_revision(session, now)
                return (
                    existing_job.id,
                    retryable,
                    recover_unknown,
                    None,
                )
            now = utcnow()
            self._ensure_claim_project(session, project_id, now)
            job = SchedulerJob(
                id=secrets.token_hex(16),
                target_id=target_id,
                actor_id=actor.id,
                project_id=project_id,
                profile_id=profile_id,
                submission_key=idempotency_key,
                task_ref=task_ref,
                purpose=purpose,
                approval_ref=approval_ref,
                request_json=json_dump(request),
                script_body=script_body if retain_submission_body else None,
                retain_submission_body=retain_submission_body,
                scheduler_job_id=None,
                state="SUBMITTING",
                raw_state=None,
                allocated_tres_json="{}",
                node_list=None,
                stdout_path=None,
                stderr_path=None,
                exit_code=None,
                error_message=None,
                submitted_at=None,
                started_at=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.add(
                SchedulerJobEvent(
                    job_id=job.id,
                    state="SUBMITTING",
                    raw_state=None,
                    detail_json=json_dump({"target_id": target_id}),
                    created_at=now,
                )
            )
            self._bump_revision(session, now)
            return job.id, True, False, None

        job_id, should_submit, recover_unknown, prior_response = self._write(prepare)
        if prior_response is not None:
            return prior_response
        if recover_unknown:
            try:
                recovered_submission = self.slurm_provider.find_by_name(
                    connection,
                    broker_job_name(job_id),
                )
            except SlurmProviderError as exc:
                raise BrokerError(
                    "scheduler_recovery_failed",
                    str(exc),
                    status_code=409 if exc.access_required else 502,
                    details={"scheduler_job_id": job_id, "state": "UNKNOWN"},
                ) from exc
            if recovered_submission is None:

                def unknown_result(session: Session) -> dict[str, Any]:
                    job = session.get(SchedulerJob, job_id)
                    assert job is not None
                    return {
                        "snapshot_revision": self._revision(session),
                        "scheduler_job": self._scheduler_job_payload(session, job),
                    }

                return self._read(unknown_result)
            submission = recovered_submission
            submission_recovered = True
        if not should_submit:

            def existing_result(session: Session) -> dict[str, Any]:
                job = session.get(SchedulerJob, job_id)
                assert job is not None
                return {
                    "snapshot_revision": self._revision(session),
                    "scheduler_job": self._scheduler_job_payload(session, job),
                }

            if not recover_unknown:
                return self._read(existing_result)

        if should_submit:
            try:
                recovered = self.slurm_provider.find_by_name(
                    connection,
                    broker_job_name(job_id),
                )
                submission = recovered or self.slurm_provider.submit(
                    connection,
                    broker_job_id=job_id,
                    request=request,
                    script_body=script_body,
                )
                submission_recovered = recovered is not None
            except SlurmProviderError as exc:
                failure_state = (
                    "UNKNOWN"
                    if exc.uncertain
                    else "ACCESS_REQUIRED"
                    if exc.access_required
                    else "FAILED"
                )
                failure_message = str(exc)[:2000]

                def record_failure(session: Session) -> None:
                    job = session.get(SchedulerJob, job_id)
                    assert job is not None
                    now = utcnow()
                    job.state = failure_state
                    job.error_message = failure_message
                    job.updated_at = now
                    session.add(
                        SchedulerJobEvent(
                            job_id=job.id,
                            state=failure_state,
                            raw_state=None,
                            detail_json=json_dump({"message": job.error_message}),
                            created_at=now,
                        )
                    )
                    self._audit(
                        session,
                        actor_id=actor.id,
                        action="scheduler_job.submit_unknown"
                        if failure_state == "UNKNOWN"
                        else "scheduler_job.submit_failed",
                        resource_type="scheduler_job",
                        resource_id=job.id,
                        result="failure",
                        after=self._scheduler_job_dict(job),
                        summary={
                            "state": failure_state,
                            "resubmission_blocked": failure_state == "UNKNOWN",
                        },
                        now=now,
                    )
                    self._bump_revision(session, now)

                self._write(record_failure)
                raise BrokerError(
                    "scheduler_submission_unknown"
                    if failure_state == "UNKNOWN"
                    else "access_required"
                    if exc.access_required
                    else "scheduler_submit_failed",
                    str(exc),
                    status_code=409 if failure_state in {"UNKNOWN", "ACCESS_REQUIRED"} else 502,
                    details={"scheduler_job_id": job_id, "state": failure_state},
                ) from exc

        def complete(session: Session) -> dict[str, Any]:
            job = session.get(SchedulerJob, job_id)
            assert job is not None
            now = utcnow()
            raw_state = submission.raw_state
            job.scheduler_job_id = submission.scheduler_job_id
            job.raw_state = raw_state
            job.state = "PENDING" if raw_state == "SUBMITTED" else broker_state(raw_state)
            job.submitted_at = now
            job.updated_at = now
            job.error_message = None
            scheduler = request["scheduler"]
            job.stdout_path = scheduler["stdout_pattern"].replace("%j", submission.scheduler_job_id)
            job.stderr_path = scheduler["stderr_pattern"].replace("%j", submission.scheduler_job_id)
            if not job.retain_submission_body:
                job.script_body = None
            session.add(
                SchedulerJobEvent(
                    job_id=job.id,
                    state=job.state,
                    raw_state=raw_state,
                    detail_json=json_dump({"scheduler_job_id": submission.scheduler_job_id}),
                    created_at=now,
                )
            )
            revision = self._bump_revision(session, now)
            payload = self._scheduler_job_payload(session, job)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="scheduler_job.recovered"
                if submission_recovered
                else "scheduler_job.submitted",
                resource_type="scheduler_job",
                resource_id=job.id,
                result="success",
                after=payload,
                summary={
                    "target_id": target_id,
                    "project_id": project_id,
                    "scheduler_job_id": submission.scheduler_job_id,
                    "recovered": submission_recovered,
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "scheduler_job": payload,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="scheduler_job.submit",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(complete)

    def submit_scheduler_profile(
        self,
        actor: ActorContext,
        profile_id: str,
        submission: SchedulerProfileSubmit,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        def resolve(
            session: Session,
        ) -> tuple[WorkloadProfile, dict[str, Any], dict[str, Any], str]:
            profile = session.get(WorkloadProfile, profile_id)
            if profile is None:
                raise BrokerError(
                    "workload_profile_not_found",
                    "workload profile does not exist",
                    status_code=404,
                )
            if not profile.enabled:
                raise BrokerError(
                    "workload_profile_disabled",
                    "workload profile is disabled",
                    status_code=409,
                )
            if profile.runtime_kind != "slurm":
                raise BrokerError(
                    "workload_profile_runtime_mismatch",
                    "workload profile is not an external scheduler profile",
                    status_code=409,
                )
            if not self._profile_allows_project(
                session,
                profile,
                submission.project_id,
            ):
                raise BrokerError(
                    "workload_profile_project_forbidden",
                    "project is not granted this workload profile",
                    status_code=403,
                )
            assert profile.scheduler_target_id is not None
            assert profile.scheduler_spec_json is not None
            assert profile.scheduler_script is not None
            request = {
                "duration_seconds": profile.duration_seconds,
                "constraints": json_load(profile.constraints_json),
                "scheduler": json_load(profile.scheduler_spec_json),
            }
            return (
                profile,
                request,
                self._workload_profile_dict(
                    profile,
                    self._workload_profile_grants(session, profile.id),
                ),
                profile.scheduler_script,
            )

        profile, request, _profile_payload, script_body = self._read(resolve)
        assert profile.scheduler_target_id is not None
        with self._scheduler_submit_lock:
            return self._submit_scheduler_job(
                actor,
                target_id=profile.scheduler_target_id,
                project_id=submission.project_id,
                profile_id=profile.id,
                task_ref=submission.task_ref,
                purpose=profile.purpose,
                approval_ref=f"profile:{profile.id}",
                request=request,
                script_body=script_body,
                retain_submission_body=profile.retain_submission_body,
                idempotency_key=idempotency_key,
            )

    def submit_scheduler_one_off(
        self,
        actor: ActorContext,
        submission: SchedulerOneOffSubmit,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = submission.model_dump(mode="json")
        script_body = payload.pop("script_body")
        retain_submission_body = payload.pop("retain_submission_body")
        target_id = payload.pop("target_id")
        project_id = payload.pop("project_id")
        task_ref = payload.pop("task_ref")
        purpose = payload.pop("purpose")
        approval_ref = payload.pop("approval_ref")
        with self._scheduler_submit_lock:
            return self._submit_scheduler_job(
                actor,
                target_id=target_id,
                project_id=project_id,
                profile_id=None,
                task_ref=task_ref,
                purpose=purpose,
                approval_ref=approval_ref,
                request=payload,
                script_body=script_body,
                retain_submission_body=retain_submission_body,
                idempotency_key=idempotency_key,
            )

    def list_scheduler_jobs(
        self,
        actor: ActorContext,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            jobs = session.scalars(
                select(SchedulerJob).order_by(SchedulerJob.created_at.desc())
            ).all()
            values = [
                self._scheduler_job_payload(session, job)
                for job in jobs
                if self._scheduler_job_visible(actor, job)
                and (project_id is None or job.project_id == project_id)
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def refresh_scheduler_job(
        self,
        actor: ActorContext,
        job_id: str,
    ) -> dict[str, Any]:
        def resolve(
            session: Session,
        ) -> tuple[SchedulerJob, dict[str, Any], int]:
            job = session.get(SchedulerJob, job_id)
            if job is None:
                raise BrokerError(
                    "scheduler_job_not_found",
                    "scheduler job does not exist",
                    status_code=404,
                )
            if not self._scheduler_job_visible(actor, job):
                raise BrokerError(
                    "scheduler_job_forbidden",
                    "scheduler job is not visible to this actor",
                    status_code=403,
                )
            target = session.get(SchedulerTarget, job.target_id)
            if target is None:
                raise BrokerError(
                    "scheduler_target_not_found",
                    "scheduler target does not exist",
                    status_code=404,
                )
            connection = json_load(target.connection_json)
            assert isinstance(connection, dict)
            return job, connection, self._revision(session)

        job, connection, revision = self._read(resolve)
        if job.scheduler_job_id is None or job.state in {
            "FAILED",
            "UNKNOWN",
            "ACCESS_REQUIRED",
        }:

            def current(session: Session) -> dict[str, Any]:
                current_job = session.get(SchedulerJob, job_id)
                assert current_job is not None
                return {
                    "schema_version": SCHEMA_VERSION,
                    "snapshot_revision": revision,
                    "scheduler_job": self._scheduler_job_payload(session, current_job),
                }

            return self._read(current)
        try:
            observation = self.slurm_provider.query(
                connection,
                job.scheduler_job_id,
            )
        except SlurmProviderError as exc:
            if exc.access_required:
                raise BrokerError(
                    "access_required",
                    str(exc),
                    status_code=409,
                    details={"scheduler_job_id": job.id},
                ) from exc
            raise BrokerError(
                "scheduler_status_failed",
                str(exc),
                status_code=502,
                details={"scheduler_job_id": job.id},
            ) from exc

        def update(session: Session) -> dict[str, Any]:
            current_job = session.get(SchedulerJob, job_id)
            assert current_job is not None
            now = utcnow()
            previous_state = current_job.state
            current_job.state = observation["state"]
            current_job.raw_state = observation["raw_state"]
            current_job.allocated_tres_json = json_dump(observation.get("allocated_tres") or {})
            current_job.node_list = observation.get("node_list")
            current_job.exit_code = observation.get("exit_code")
            current_job.started_at = (
                _external_datetime(observation.get("started_at")) or current_job.started_at
            )
            current_job.completed_at = (
                _external_datetime(observation.get("completed_at")) or current_job.completed_at
            )
            if (
                current_job.state
                in {
                    "COMPLETED",
                    "FAILED",
                    "CANCELLED",
                    "TIMEOUT",
                }
                and current_job.completed_at is None
            ):
                current_job.completed_at = now
            current_job.updated_at = now
            if current_job.state != previous_state or current_job.raw_state != job.raw_state:
                session.add(
                    SchedulerJobEvent(
                        job_id=current_job.id,
                        state=current_job.state,
                        raw_state=current_job.raw_state,
                        detail_json=json_dump(
                            {
                                "allocated_tres": observation.get("allocated_tres") or {},
                                "node_list": current_job.node_list,
                            }
                        ),
                        created_at=now,
                    )
                )
                revision_value = self._bump_revision(session, now)
            else:
                revision_value = self._revision(session)
            return {
                "schema_version": SCHEMA_VERSION,
                "snapshot_revision": revision_value,
                "scheduler_job": self._scheduler_job_payload(
                    session,
                    current_job,
                ),
            }

        return self._write(update)

    def cancel_scheduler_job(
        self,
        actor: ActorContext,
        job_id: str,
        cancellation: SchedulerJobCancel,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def resolve(
            session: Session,
        ) -> tuple[SchedulerJob, dict[str, Any], dict[str, Any] | None]:
            prior = self._idempotent(
                session,
                actor=actor,
                action="scheduler_job.cancel",
                key=idempotency_key,
            )
            job = session.get(SchedulerJob, job_id)
            if job is None:
                raise BrokerError(
                    "scheduler_job_not_found",
                    "scheduler job does not exist",
                    status_code=404,
                )
            if job.actor_id != actor.id:
                raise BrokerError(
                    "scheduler_job_forbidden",
                    "only the job owner or an authorized operator may cancel it",
                    status_code=403,
                )
            target = session.get(SchedulerTarget, job.target_id)
            if target is None:
                raise BrokerError(
                    "scheduler_target_not_found",
                    "scheduler target does not exist",
                    status_code=404,
                )
            connection = json_load(target.connection_json)
            assert isinstance(connection, dict)
            return job, connection, prior

        job, connection, prior = self._read(resolve)
        if prior is not None:
            return prior
        if job.scheduler_job_id is None:
            raise BrokerError(
                "scheduler_job_not_submitted",
                "scheduler job has no external Job ID to cancel",
                status_code=409,
            )
        try:
            self.slurm_provider.cancel(connection, job.scheduler_job_id)
        except SlurmProviderError as exc:
            raise BrokerError(
                "access_required" if exc.access_required else "scheduler_cancel_failed",
                str(exc),
                status_code=409 if exc.access_required else 502,
                details={"scheduler_job_id": job.id},
            ) from exc

        def complete(session: Session) -> dict[str, Any]:
            current_job = session.get(SchedulerJob, job_id)
            assert current_job is not None
            now = utcnow()
            current_job.state = "CANCEL_REQUESTED"
            current_job.raw_state = "CANCEL_REQUESTED"
            current_job.updated_at = now
            session.add(
                SchedulerJobEvent(
                    job_id=current_job.id,
                    state=current_job.state,
                    raw_state=current_job.raw_state,
                    detail_json=json_dump({"reason": cancellation.reason}),
                    created_at=now,
                )
            )
            revision_value = self._bump_revision(session, now)
            payload = self._scheduler_job_payload(session, current_job)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="scheduler_job.cancel_requested",
                resource_type="scheduler_job",
                resource_id=current_job.id,
                result="success",
                before=self._scheduler_job_dict(job),
                after=payload,
                summary={"reason": cancellation.reason},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision_value,
                "scheduler_job": payload,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="scheduler_job.cancel",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(complete)

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

    def create_endpoint(
        self, actor: ActorContext, endpoint_data: EndpointCreate, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Create one active endpoint; identity is never updated in place."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            if endpoint_data.owner_project_id == SYSTEM_PROJECT_ID:
                raise BrokerError(
                    "reserved_project_id",
                    "the ServerPilot internal project cannot own an endpoint",
                    status_code=422,
                )
            if session.get(Endpoint, endpoint_data.id) is not None:
                raise BrokerError("endpoint_exists", "endpoint id already exists", status_code=409)
            same_address = session.scalar(
                select(Endpoint).where(
                    Endpoint.host == endpoint_data.host,
                    Endpoint.port == endpoint_data.port,
                )
            )
            if same_address is not None:
                raise BrokerError(
                    "endpoint_address_exists",
                    "an immutable endpoint already owns this host:port",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            endpoint = Endpoint(
                id=endpoint_data.id,
                host=endpoint_data.host,
                port=endpoint_data.port,
                ssh_user=endpoint_data.ssh_user,
                ssh_alias=endpoint_data.ssh_alias,
                workspace_path=endpoint_data.workspace_path,
                observation_profile=endpoint_data.observation_profile,
                keepalive_adapter_id=endpoint_data.keepalive_adapter_id,
                keepalive_policy=endpoint_data.keepalive_policy,
                labels_json=json_dump(endpoint_data.labels),
                storage_group=endpoint_data.storage_group,
                expected_gpu_count=endpoint_data.expected_gpu_count,
                expected_gpu_total_vram_mib=endpoint_data.expected_gpu_total_vram_mib,
                owner_project_id=endpoint_data.owner_project_id,
                lifecycle_state="active",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(endpoint)
            session.flush()
            payload = self._endpoint_dict(endpoint)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.created",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                after=payload,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "changed": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def update_endpoint(
        self,
        actor: ActorContext,
        endpoint_id: str,
        endpoint_data: EndpointUpdate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Update only safe monitoring metadata on an active or draining endpoint."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.update", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            before = self._endpoint_dict(endpoint)
            fields = endpoint_data.model_fields_set
            values = endpoint_data.model_dump()
            if values.get("owner_project_id") == SYSTEM_PROJECT_ID:
                raise BrokerError(
                    "reserved_project_id",
                    "the ServerPilot internal project cannot own an endpoint",
                    status_code=422,
                )
            protected_keepalive_fields = {
                "host",
                "port",
                "keepalive_adapter_id",
                "ssh_user",
                "ssh_alias",
                "workspace_path",
                "observation_profile",
            }
            changed_protected_fields = sorted(
                field
                for field in fields.intersection(protected_keepalive_fields)
                if values[field] != getattr(endpoint, field)
            )
            if (
                changed_protected_fields
                and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
            ):
                raise BrokerError(
                    "keepalive_endpoint_connection_in_use",
                    "stop the active endpoint keepalive before changing its connection or verification settings",
                    status_code=409,
                    details={"fields": changed_protected_fields},
                )
            if (
                "keepalive_adapter_id" in fields
                and values["keepalive_adapter_id"] is None
                and endpoint.keepalive_policy == "idle_keepalive"
            ):
                raise BrokerError(
                    "keepalive_adapter_required",
                    "disable idle keepalive before removing its sealed endpoint adapter",
                    status_code=409,
                )
            changed = False
            for field in fields:
                value = values[field]
                attribute = "labels_json" if field == "labels" else field
                if field == "labels":
                    value = json_dump(value)
                if getattr(endpoint, attribute) != value:
                    setattr(endpoint, attribute, value)
                    changed = True
            now = utcnow()
            if changed:
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._endpoint_dict(endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.updated",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "changed": changed,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.update",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def pause_endpoint(
        self,
        actor: ActorContext,
        endpoint_id: str,
        *,
        idempotency_key: str,
        _idempotency_action: str = "endpoint.pause",
        _compatibility_alias: bool = False,
    ) -> dict[str, Any]:
        """Move active -> draining without changing collection or existing leases."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action=_idempotency_action, key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = self._endpoint_dict(endpoint)
            changed = endpoint.lifecycle_state == "active"
            if changed:
                endpoint.lifecycle_state = "draining"
                endpoint.enabled = False
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._endpoint_dict(endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.paused",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    summary={"collection_continues": True, "history_retained": True},
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "endpoint_id": endpoint.id,
                "changed": changed,
                "history_retained": True,
            }
            if _compatibility_alias:
                result["deprecated_compatibility_alias"] = "DELETE /api/v1/endpoints/{endpoint_id}"
            self._remember_idempotency(
                session,
                actor=actor,
                action=_idempotency_action,
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def resume_endpoint(
        self, actor: ActorContext, endpoint_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Move draining -> active."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.resume", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = self._endpoint_dict(endpoint)
            changed = endpoint.lifecycle_state == "draining"
            if changed:
                endpoint.lifecycle_state = "active"
                endpoint.enabled = True
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._endpoint_dict(endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.resumed",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "endpoint_id": endpoint.id,
                "changed": changed,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.resume",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def upsert_endpoint(
        self, actor: ActorContext, endpoint_data: EndpointUpsert, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Create/update inventory metadata while keeping endpoint id and host:port immutable."""

        self._require_role(actor, MUTATING_ROLES)

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
                lifecycle_state = endpoint_data.lifecycle_state or "active"
                if lifecycle_state != "active":
                    raise BrokerError(
                        "endpoint_lifecycle_invalid_transition",
                        "a new endpoint must begin active",
                        status_code=409,
                    )
                endpoint = Endpoint(
                    id=endpoint_data.id,
                    host=endpoint_data.host,
                    port=endpoint_data.port,
                    ssh_user=endpoint_data.ssh_user,
                    ssh_alias=endpoint_data.ssh_alias,
                    workspace_path=endpoint_data.workspace_path,
                    observation_profile=endpoint_data.observation_profile,
                    keepalive_adapter_id=endpoint_data.keepalive_adapter_id,
                    keepalive_policy=endpoint_data.keepalive_policy,
                    labels_json=json_dump(endpoint_data.labels),
                    storage_group=endpoint_data.storage_group,
                    expected_gpu_count=endpoint_data.expected_gpu_count,
                    expected_gpu_total_vram_mib=endpoint_data.expected_gpu_total_vram_mib,
                    owner_project_id=endpoint_data.owner_project_id,
                    lifecycle_state=lifecycle_state,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
            else:
                self._require_endpoint_manager(actor, endpoint)
                if (endpoint.host, endpoint.port) != (endpoint_data.host, endpoint_data.port):
                    raise BrokerError(
                        "endpoint_identity_immutable",
                        "existing endpoint id cannot change host:port; create a new endpoint id",
                        status_code=409,
                    )
                protected_values = {
                    "ssh_user": endpoint_data.ssh_user,
                    "ssh_alias": endpoint_data.ssh_alias,
                    "workspace_path": endpoint_data.workspace_path,
                    "observation_profile": endpoint_data.observation_profile,
                    "keepalive_adapter_id": endpoint_data.keepalive_adapter_id,
                }
                changed_protected_fields = sorted(
                    field
                    for field, value in protected_values.items()
                    if value != getattr(endpoint, field)
                )
                if (
                    changed_protected_fields
                    and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
                ):
                    raise BrokerError(
                        "keepalive_endpoint_connection_in_use",
                        "stop the active endpoint keepalive before changing its connection or verification settings",
                        status_code=409,
                        details={"fields": changed_protected_fields},
                    )
                if endpoint_data.owner_project_id is not None:
                    endpoint.owner_project_id = endpoint_data.owner_project_id
                endpoint.workspace_path = endpoint_data.workspace_path
                requested_lifecycle = endpoint_data.lifecycle_state
                if requested_lifecycle is None and endpoint_data.enabled is False:
                    requested_lifecycle = "draining"
                if (
                    requested_lifecycle is not None
                    and requested_lifecycle != endpoint.lifecycle_state
                ):
                    allowed_transitions = {
                        "active": {"draining"},
                        "draining": set(),
                    }
                    if requested_lifecycle not in allowed_transitions[endpoint.lifecycle_state]:
                        raise BrokerError(
                            "endpoint_lifecycle_invalid_transition",
                            "endpoint lifecycle can transition only from active to draining",
                            status_code=409,
                        )
                    endpoint.lifecycle_state = requested_lifecycle
                endpoint.ssh_user = endpoint_data.ssh_user
                endpoint.ssh_alias = endpoint_data.ssh_alias
                endpoint.observation_profile = endpoint_data.observation_profile
                endpoint.keepalive_adapter_id = endpoint_data.keepalive_adapter_id
                endpoint.keepalive_policy = endpoint_data.keepalive_policy
                endpoint.labels_json = json_dump(endpoint_data.labels)
                endpoint.storage_group = endpoint_data.storage_group
                endpoint.expected_gpu_count = endpoint_data.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = endpoint_data.expected_gpu_total_vram_mib
                endpoint.enabled = endpoint.lifecycle_state == "active"
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

    def set_endpoint_enabled(
        self,
        actor: ActorContext,
        endpoint_id: str,
        state: EndpointEnabled,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Deprecated boolean compatibility facade for explicit pause/resume."""

        if state.enabled:
            return self.resume_endpoint(actor, endpoint_id, idempotency_key=idempotency_key)
        return self.pause_endpoint(actor, endpoint_id, idempotency_key=idempotency_key)

    def create_actor(
        self, actor: ActorContext, actor_data: ActorCreate, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="actor.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            if actor_data.id == SYSTEM_ACTOR_ID or SYSTEM_PROJECT_ID in actor_data.project_ids:
                raise BrokerError(
                    "reserved_system_identity",
                    "the ServerPilot internal identity cannot be created or assigned by API",
                    status_code=422,
                )
            if session.get(Actor, actor_data.id) is not None:
                raise BrokerError("actor_exists", "actor id already exists", status_code=409)
            unknown_projects = [
                project_id
                for project_id in actor_data.project_ids
                if session.get(Project, project_id) is None
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
            event = self._audit(
                session,
                actor_id=actor.id,
                action="actor.created",
                resource_type="actor",
                resource_id=created.id,
                result="success",
                after=self._actor_dict(created, actor_data.project_ids),
                summary={"role": created.role},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "actor": self._actor_dict(created, actor_data.project_ids),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="actor.create",
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
            visible = [
                self._request_dict(request) for request in requests if request.actor_id == actor.id
            ]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_leases(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            leases = session.scalars(select(Lease).order_by(Lease.issued_at.desc())).all()
            visible = [
                self._lease_dict(session, lease) for lease in leases if lease.actor_id == actor.id
            ]
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
                    select(MaintenanceWindow).order_by(
                        MaintenanceWindow.start_at, MaintenanceWindow.id
                    )
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
        self,
        actor: ActorContext,
        *,
        after_id: int = 0,
        limit: int = 200,
        latest_first: bool = False,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise BrokerError("invalid_limit", "limit must be between 1 and 1000", status_code=422)

        def operation(session: Session) -> dict[str, Any]:
            events = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.id > after_id)
                .order_by(AuditEvent.id.desc() if latest_first else AuditEvent.id)
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
                if (
                    not visible
                    and event.resource_type == "endpoint"
                    and event.action in {"telemetry.failed", "telemetry.recovered"}
                ):
                    endpoint = session.get(Endpoint, event.resource_id)
                    visible = endpoint is not None and actor.role in {
                        "viewer",
                        "allocator",
                        "operator",
                        "admin",
                    }
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
                        "before": json_load(event.before_json) if event.before_json else None,
                        "after": json_load(event.after_json) if event.after_json else None,
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
                for project in session.scalars(
                    select(Project).where(Project.id != SYSTEM_PROJECT_ID).order_by(Project.id)
                ).all()
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
            values = []
            for profile in profiles:
                grants = self._workload_profile_grants(session, profile.id)
                if project_id is not None:
                    visible = (
                        profile.project_id == project_id
                        or profile.grant_all_projects
                        or project_id in grants
                    )
                elif actor.is_admin:
                    visible = True
                else:
                    visible = any(
                        profile.project_id == candidate
                        or profile.grant_all_projects
                        or candidate in grants
                        for candidate in actor.project_ids
                    )
                if visible:
                    values.append(self._workload_profile_dict(profile, grants))
            return self.envelope(session, values)

        return self._read(operation)

    def list_actors(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            values = []
            for item in session.scalars(
                select(Actor).where(Actor.id != SYSTEM_ACTOR_ID).order_by(Actor.id)
            ).all():
                project_ids = session.scalars(
                    select(ActorProject.project_id).where(ActorProject.actor_id == item.id)
                ).all()
                values.append(self._actor_dict(item, project_ids))
            return self.envelope(session, values)

        return self._read(operation)

    def effective_config(self, actor: ActorContext) -> dict[str, Any]:
        # Inventory carries no secrets; this view excludes runtime environment values.
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
            present_gpus = [gpu for gpu in gpus if gpu.present]
            stale = sum(
                1
                for gpu in present_gpus
                if self._gpu_state(session, gpu, now)[0] in {"UNKNOWN_STALE", "UNKNOWN_RECOVERING"}
            )
            provider_states = session.scalars(
                select(ProviderState).order_by(ProviderState.endpoint_id)
            ).all()
            return self.envelope(
                session,
                {
                    "database_ready": self.database.ready(),
                    "snapshot_revision": self._revision(session),
                    "inventory_endpoints": session.scalar(
                        select(func.count()).select_from(Endpoint)
                    ),
                    "discovered_gpus": len(present_gpus),
                    "historical_gpu_records": len(gpus),
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
            gpus = session.scalars(select(GPUDevice).where(GPUDevice.present.is_(True))).all()
            states: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                states[self._gpu_state(session, gpu, now)[0]] += 1
            active_leases = session.scalar(
                select(func.count()).select_from(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))
            )
            queued = session.scalar(
                select(func.count())
                .select_from(AllocationRequest)
                .where(AllocationRequest.state == "QUEUED")
            )
            lines = [
                "# HELP serverpilot_gpus Number of GPUs by derived state",
                "# TYPE serverpilot_gpus gauge",
            ]
            lines.extend(
                f'serverpilot_gpus{{state="{state}"}} {count}'
                for state, count in sorted(states.items())
            )
            lines.extend(
                [
                    "# HELP serverpilot_active_leases Number of active exclusive leases",
                    "# TYPE serverpilot_active_leases gauge",
                    f"serverpilot_active_leases {active_leases or 0}",
                    "# HELP serverpilot_queued_requests Number of queued allocation requests",
                    "# TYPE serverpilot_queued_requests gauge",
                    f"serverpilot_queued_requests {queued or 0}",
                    "# HELP serverpilot_snapshot_revision Monotonic control-plane revision",
                    "# TYPE serverpilot_snapshot_revision gauge",
                    f"serverpilot_snapshot_revision {self._revision(session)}",
                    "",
                ]
            )
            return "\n".join(lines)

        return self._read(operation)

    def backup(self, actor: ActorContext, destination: str) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)
        path = self.database.backup(destination=Path(destination))
        return {"path": str(path), "created_at": _iso(utcnow())}
