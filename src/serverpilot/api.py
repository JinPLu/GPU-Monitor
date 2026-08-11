"""FastAPI REST, SSE and server-rendered functional GUI surfaces."""

import asyncio
import contextlib
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from serverpilot import API_CAPABILITIES, SCHEMA_VERSION, __version__
from serverpilot.adapters import AdapterCommandError, endpoint_keepalive_adapter
from serverpilot.collector import SSHCollector
from serverpilot.config import Settings, load_inventory
from serverpilot.database import Database
from serverpilot.importer import ParsedSSHCommand, parse_ssh_command
from serverpilot.schemas import (
    ActorCreate,
    AlertAcknowledge,
    EndpointCreate,
    ControlPlaneSnapshot,
    CollectorSettingsUpdate,
    EndpointKeepaliveRequest,
    EndpointUpdate,
    EndpointUpsert,
    LeaseBind,
    LeaseObservedBind,
    RequestCreate,
    RequestCreateFlat,
    RetentionPrune,
    ResourceClaim,
    ResourcePlanEvaluationInput,
    ResourceRunActualInput,
    SchedulerJobCancel,
    SchedulerOneOffSubmit,
    SchedulerProfileSubmit,
    SchedulerTargetUpsert,
    SchedulerUploadRequest,
    SSHCommandCommit,
    SSHCommandRequest,
    SSHCommandsCommit,
    SSHCommandsRequest,
    WorkloadProfileClaim,
    WorkloadProfileUpsert,
)
from serverpilot.service import SYSTEM_ACTOR_ID, ActorContext, BrokerError, BrokerService
from serverpilot.timeutil import json_dump, utcnow


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, actor_id: str) -> None:
        now = time.monotonic()
        hits = self._hits[actor_id]
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            raise BrokerError(
                "rate_limited",
                "rate limit exceeded; retry after one minute",
                status_code=429,
            )
        hits.append(now)


def _idempotency_key(value: str | None) -> str:
    if not value:
        raise BrokerError(
            "idempotency_key_required",
            "Idempotency-Key header is required for every mutation",
            status_code=422,
        )
    return value


def _public_keepalive_result(
    endpoint_id: str,
    keepalive: dict[str, Any],
    *,
    event_id: int | None = None,
    snapshot_revision: int | None = None,
) -> dict[str, Any]:
    """Return policy and aggregate coverage without worker or lease identity.

    The endpoint toggle is intentionally the only public write surface.  The
    service and the sealed adapter exchange lease IDs, GPU UUIDs, and worker
    attestations internally; none of those are meaningful or safe client
    controls, so this projection explicitly allowlists only human-visible
    policy and coverage fields.
    """

    policy = keepalive.get("policy")
    if policy not in {"disabled", "idle_keepalive"}:
        policy = "disabled"
    state = keepalive.get("state")
    if state not in {"OFF", "IDLE", "STARTING", "PARTIAL", "ACTIVE", "ERROR", "LEGACY_STOP_REQUIRED"}:
        state = "ERROR"
    public = {
        "endpoint_id": endpoint_id,
        # Keep the old boolean as a read-only compatibility convenience; the
        # policy is the actual contract from this version forward.
        "enabled": policy == "idle_keepalive",
        "policy": policy,
        "state": state,
        "configured": bool(keepalive.get("configured", False)),
        "active_gpu_count": max(0, int(keepalive.get("active_gpu_count") or 0)),
        "starting_gpu_count": max(0, int(keepalive.get("starting_gpu_count") or 0)),
        "error_gpu_count": max(0, int(keepalive.get("error_gpu_count") or 0)),
        "legacy_gpu_count": max(0, int(keepalive.get("legacy_gpu_count") or 0)),
        "eligible_idle_gpu_count": max(0, int(keepalive.get("eligible_idle_gpu_count") or 0)),
    }
    message = keepalive.get("message")
    if isinstance(message, str) and message:
        public["message"] = message
    reasons = keepalive.get("reasons")
    if isinstance(reasons, list):
        public_reasons: list[str] = []
        for item in reasons:
            if isinstance(item, str):
                public_reasons.append(item)
            elif isinstance(item, dict) and isinstance(item.get("reason"), str):
                # Domain diagnostics may be keyed by an internal GPU ID.  The
                # endpoint API intentionally exposes the explanation only.
                public_reasons.append(item["reason"])
        if public_reasons:
            public["reasons"] = public_reasons[:16]
    return {
        "event_id": event_id,
        "snapshot_revision": snapshot_revision,
        "keepalive": public,
    }


def create_app(
    settings: Settings,
    *,
    collector: SSHCollector | None = None,
    keepalive_adapter_resolver: Callable[[str], Any] = endpoint_keepalive_adapter,
) -> FastAPI:
    inventory = load_inventory(settings.inventory_path)
    project_root = settings.project_root or _find_project_root()
    service = BrokerService(Database(settings.database_url, project_root), inventory)
    service.initialize(settings.bootstrap_token)
    shared_collector = collector or SSHCollector(inventory)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
    keepalive_reconcile_locks: dict[str, asyncio.Lock] = {}

    def keepalive_reconcile_lock(endpoint_id: str) -> asyncio.Lock:
        lock = keepalive_reconcile_locks.get(endpoint_id)
        if lock is None:
            lock = asyncio.Lock()
            keepalive_reconcile_locks[endpoint_id] = lock
        return lock

    @contextlib.asynccontextmanager
    async def keepalive_endpoint_locks(endpoint_ids: set[str]) -> AsyncIterator[None]:
        """Hold a stable endpoint set across reclaim and the retrying claim."""

        locks = [keepalive_reconcile_lock(endpoint_id) for endpoint_id in sorted(endpoint_ids)]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    async def collect_keepalive_endpoint(endpoint: Any) -> None:
        """Require a post-action, endpoint-scoped fresh collection."""

        try:
            collected = await shared_collector.collect_once(
                service,
                endpoints=[endpoint],
                concurrency=1,
            )
        except Exception:
            raise BrokerError(
                "keepalive_observation_failed",
                "endpoint keepalive state could not be observed",
                status_code=503,
            ) from None
        endpoint_result = collected.get(endpoint.id)
        if not isinstance(endpoint_result, dict) or "error" in endpoint_result:
            raise BrokerError(
                "keepalive_observation_failed",
                "endpoint keepalive state could not be observed",
                status_code=503,
            )

    def result_workers_by_gpu_uuid(
        adapter_result: Any,
        gpu_uuids: list[str],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        """Defend the API boundary even when a test/different adapter is injected."""

        results = getattr(adapter_result, "results", None)
        if not isinstance(results, tuple) or len(results) != len(gpu_uuids):
            raise BrokerError(
                "keepalive_adapter_failed",
                "endpoint keepalive operation could not be verified",
                status_code=503,
            )
        by_uuid: dict[str, Any] = {}
        expected_status = "running" if enabled else "stopped"
        for result in results:
            gpu_uuid = getattr(result, "gpu_uuid", None)
            status = getattr(result, "status", None)
            if not isinstance(gpu_uuid, str) or gpu_uuid in by_uuid or status != expected_status:
                raise BrokerError(
                    "keepalive_adapter_failed",
                    "endpoint keepalive operation could not be verified",
                    status_code=503,
                )
            by_uuid[gpu_uuid] = result
        if set(by_uuid) != set(gpu_uuids):
            raise BrokerError(
                "keepalive_adapter_failed",
                "endpoint keepalive operation could not be verified",
                status_code=503,
            )
        if enabled and any(getattr(result, "worker", None) is None for result in by_uuid.values()):
            raise BrokerError(
                "keepalive_attestation_missing",
                "endpoint keepalive state could not be verified",
                status_code=503,
            )
        return by_uuid

    async def reconcile_endpoint_keepalive(
        actor: ActorContext,
        endpoint_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Execute a service-produced per-GPU keepalive transition plan.

        Service planning and ownership writes are separated from adapter calls;
        every remote action is followed by a fresh endpoint observation before
        the corresponding lease is confirmed or released.  This callable is
        intentionally endpoint-level so the collector loop and the explicit
        API toggle use the same fail-closed orchestration path.
        """

        async with keepalive_reconcile_lock(endpoint_id):
            endpoint = service.collector_endpoint(endpoint_id)
            plan = service.list_keepalive_transitions(endpoint_id)
            transitions = plan.get("transitions")
            if not isinstance(transitions, list):
                raise BrokerError(
                    "keepalive_transition_plan_invalid",
                    "endpoint keepalive reconciliation plan is invalid",
                    status_code=503,
                )
            transitions = [
                transition
                for transition in transitions
                if isinstance(transition, dict) and transition.get("endpoint_id") == endpoint_id
            ]
            legacy = [
                transition for transition in transitions if transition.get("action") == "stop_legacy_endpoint"
            ]
            if legacy:
                # A v2 exact-GPU helper cannot safely signal an unverified v1
                # whole-endpoint worker.  Do not translate it into arbitrary
                # target UUIDs or make it allocatable based on database state.
                raise BrokerError(
                    "keepalive_legacy_stop_required",
                    "legacy endpoint keepalive requires explicit verified stop before per-GPU reconciliation",
                    status_code=409,
                )
            starts = [transition for transition in transitions if transition.get("action") == "start"]
            stops = [transition for transition in transitions if transition.get("action") == "stop"]
            if starts and stops:
                raise BrokerError(
                    "keepalive_transition_plan_invalid",
                    "endpoint keepalive plan mixes start and stop transitions",
                    status_code=503,
                )
            if not starts and not stops:
                return service.get_endpoint_keepalive_summary(endpoint_id)
            adapter_id = endpoint.keepalive_adapter_id
            if adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint keepalive is not configured",
                    status_code=409,
                )
            try:
                adapter = keepalive_adapter_resolver(adapter_id)
            except (KeyError, ValueError):
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint keepalive adapter is unavailable",
                    status_code=409,
                ) from None

            if starts:
                prepared: list[tuple[dict[str, Any], str]] = []
                for transition in starts:
                    gpu_id = transition.get("gpu_id")
                    gpu_uuid = transition.get("gpu_uuid")
                    if not isinstance(gpu_id, str) or not isinstance(gpu_uuid, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive start target is invalid",
                            status_code=503,
                        )
                    pending = service.begin_keepalive(
                        actor,
                        endpoint_id,
                        gpu_id,
                        idempotency_key=f"{idempotency_key}:start:{gpu_id}",
                    )
                    lease_id = pending.get("keepalive", {}).get("lease_id")
                    if not isinstance(lease_id, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive reservation could not be verified",
                            status_code=503,
                        )
                    prepared.append((transition, lease_id))
                target_uuids = [transition["gpu_uuid"] for transition, _ in prepared]
                try:
                    adapter_result = await adapter.set_enabled(endpoint, True, target_uuids)
                except AdapterCommandError as exc:
                    raise BrokerError(
                        "keepalive_outcome_uncertain" if exc.uncertain else "keepalive_adapter_failed",
                        "endpoint keepalive operation could not be verified",
                        status_code=503,
                    ) from None
                except Exception:
                    raise BrokerError(
                        "keepalive_adapter_failed",
                        "endpoint keepalive operation could not be verified",
                        status_code=503,
                    ) from None
                workers = result_workers_by_gpu_uuid(adapter_result, target_uuids, enabled=True)
                observation_not_before = utcnow()
                await collect_keepalive_endpoint(endpoint)
                for transition, lease_id in prepared:
                    worker = getattr(workers[transition["gpu_uuid"]], "worker", None)
                    pid = getattr(worker, "pid", None)
                    if not isinstance(pid, int) or pid <= 0:
                        raise BrokerError(
                            "keepalive_attestation_missing",
                            "endpoint keepalive state could not be verified",
                            status_code=503,
                        )
                    service.confirm_keepalive(
                        actor,
                        endpoint_id,
                        lease_id,
                        attested_pid=pid,
                        observation_not_before=observation_not_before,
                        idempotency_key=f"{idempotency_key}:confirm:{transition['gpu_id']}",
                    )
            else:
                prepared_stops: list[tuple[dict[str, Any], str]] = []
                for transition in stops:
                    lease_id = transition.get("lease_id")
                    gpu_uuid = transition.get("gpu_uuid")
                    if not isinstance(lease_id, str) or not isinstance(gpu_uuid, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive stop target is invalid",
                            status_code=503,
                        )
                    # Resolve the lease through the service before remote I/O;
                    # a stale plan must never ask the helper to stop a GPU.
                    pending = service.prepare_keepalive_stop(
                        actor,
                        endpoint_id,
                        transition.get("gpu_id"),
                    )
                    resolved_lease_id = pending.get("keepalive", {}).get("lease_id")
                    if resolved_lease_id != lease_id:
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive stop reservation changed before execution",
                            status_code=409,
                        )
                    prepared_stops.append((transition, lease_id))
                target_uuids = [transition["gpu_uuid"] for transition, _ in prepared_stops]
                try:
                    adapter_result = await adapter.set_enabled(endpoint, False, target_uuids)
                except AdapterCommandError as exc:
                    raise BrokerError(
                        "keepalive_outcome_uncertain" if exc.uncertain else "keepalive_adapter_failed",
                        "endpoint keepalive operation could not be verified",
                        status_code=503,
                    ) from None
                except Exception:
                    raise BrokerError(
                        "keepalive_adapter_failed",
                        "endpoint keepalive operation could not be verified",
                        status_code=503,
                    ) from None
                result_workers_by_gpu_uuid(adapter_result, target_uuids, enabled=False)
                observation_not_before = utcnow()
                await collect_keepalive_endpoint(endpoint)
                for transition, lease_id in prepared_stops:
                    service.finalize_keepalive_stop(
                        actor,
                        endpoint_id,
                        lease_id,
                        observation_not_before=observation_not_before,
                        idempotency_key=f"{idempotency_key}:stop:{transition['gpu_id']}",
                    )
            return service.get_endpoint_keepalive_summary(endpoint_id)

    async def reclaim_keepalive_for_claim(
        actor: ActorContext,
        request_data_for_attempt: Callable[[], RequestCreate],
        retry_claim: Callable[[], dict[str, Any]],
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Make only a fully matching, verified keeper placement claimable.

        This is not generic preemption: the service's regular allocator first
        computes one complete placement made solely from ACTIVE per-GPU
        keepalive leases.  The API then stops exactly that physical set,
        verifies it empty, and retries the ordinary immediate claim.  Any
        stale plan, partial match, legacy worker, foreign process, or remote
        uncertainty leaves the claim blocked rather than broadening the set.
        """

        for _attempt in range(3):
            request_data = request_data_for_attempt()
            plan = service.plan_keepalive_reclaim(request_data)
            transitions = plan.get("transitions")
            if plan.get("complete") is not True or not isinstance(transitions, list) or not transitions:
                return None
            targets: list[dict[str, str]] = []
            for transition in transitions:
                if not isinstance(transition, dict) or transition.get("action") != "reclaim":
                    return None
                endpoint_id = transition.get("endpoint_id")
                gpu_id = transition.get("gpu_id")
                gpu_uuid = transition.get("gpu_uuid")
                lease_id = transition.get("lease_id")
                if not all(isinstance(value, str) and value for value in (endpoint_id, gpu_id, gpu_uuid, lease_id)):
                    return None
                targets.append(
                    {
                        "endpoint_id": endpoint_id,
                        "gpu_id": gpu_id,
                        "gpu_uuid": gpu_uuid,
                        "lease_id": lease_id,
                    }
                )
            if len({target["gpu_id"] for target in targets}) != len(targets):
                return None
            endpoint_ids = {target["endpoint_id"] for target in targets}
            async with keepalive_endpoint_locks(endpoint_ids):
                # Keepalive reclamation must use the exact profile contract
                # that will be retried.  Re-read it after taking the endpoint
                # locks so a concurrent profile edit cannot stop workers for
                # stale constraints.
                if request_data_for_attempt() != request_data:
                    continue
                # The selected keeper set is an admission decision made by the
                # service. Re-read it only after the endpoint reconcile locks
                # are held; if it changed, do not infer a replacement set.
                locked_plan = service.plan_keepalive_reclaim(request_data)
                locked_transitions = locked_plan.get("transitions")
                locked_targets = (
                    {
                        (
                            item.get("endpoint_id"),
                            item.get("gpu_id"),
                            item.get("gpu_uuid"),
                            item.get("lease_id"),
                        )
                        for item in locked_transitions
                        if isinstance(item, dict) and item.get("action") == "reclaim"
                    }
                    if locked_plan.get("complete") is True and isinstance(locked_transitions, list)
                    else set()
                )
                requested_targets = {
                    (target["endpoint_id"], target["gpu_id"], target["gpu_uuid"], target["lease_id"])
                    for target in targets
                }
                if locked_targets != requested_targets:
                    # The next loop will acquire the (possibly different)
                    # endpoint lock set before considering the new plan.
                    continue

                by_endpoint: dict[str, list[dict[str, str]]] = defaultdict(list)
                for target in targets:
                    by_endpoint[target["endpoint_id"]].append(target)
                for endpoint_id, endpoint_targets in by_endpoint.items():
                    endpoint = service.collector_endpoint(endpoint_id)
                    adapter_id = endpoint.keepalive_adapter_id
                    if adapter_id is None:
                        return None
                    try:
                        adapter = keepalive_adapter_resolver(adapter_id)
                    except (KeyError, ValueError):
                        return None
                    prepared: list[dict[str, str]] = []
                    for target in endpoint_targets:
                        pending = service.prepare_keepalive_stop(
                            actor,
                            endpoint_id,
                            target["gpu_id"],
                        )
                        observed_lease_id = pending.get("keepalive", {}).get("lease_id")
                        if observed_lease_id != target["lease_id"]:
                            return None
                        prepared.append(target)
                    target_uuids = [target["gpu_uuid"] for target in prepared]
                    try:
                        adapter_result = await adapter.set_enabled(endpoint, False, target_uuids)
                    except AdapterCommandError as exc:
                        raise BrokerError(
                            "keepalive_outcome_uncertain" if exc.uncertain else "keepalive_adapter_failed",
                            "keepalive release for an immediate claim could not be verified",
                            status_code=503,
                        ) from None
                    except Exception:
                        raise BrokerError(
                            "keepalive_adapter_failed",
                            "keepalive release for an immediate claim could not be verified",
                            status_code=503,
                        ) from None
                    result_workers_by_gpu_uuid(adapter_result, target_uuids, enabled=False)
                    observation_not_before = utcnow()
                    await collect_keepalive_endpoint(endpoint)
                    for target in prepared:
                        service.finalize_keepalive_stop(
                            actor,
                            endpoint_id,
                            target["lease_id"],
                            observation_not_before=observation_not_before,
                            idempotency_key=f"{idempotency_key}:reclaim:{target['gpu_id']}",
                        )
                # Keep the endpoint locks through the ordinary claim retry.
                # Otherwise collector reconciliation could observe the fresh
                # empty GPU and restart its keeper in the gap.
                return retry_claim()
        return None

    async def claim_workload_profile_now(
        actor: ActorContext,
        profile_id: str,
        claim: WorkloadProfileClaim,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Claim one profile through the same exact keepalive handoff as /claims."""

        try:
            return service.claim_workload_profile(
                actor,
                profile_id,
                claim,
                idempotency_key=idempotency_key,
            )
        except BrokerError as exc:
            if exc.code != "no_capacity":
                raise
            claimed = await reclaim_keepalive_for_claim(
                actor,
                lambda: service.workload_profile_claim_request(actor, profile_id, claim),
                lambda: service.claim_workload_profile(
                    actor,
                    profile_id,
                    claim,
                    idempotency_key=idempotency_key,
                ),
                idempotency_key=idempotency_key,
            )
            if claimed is None:
                raise exc
            return claimed

    async def collector_loop() -> None:
        next_prune_at = 0.0
        while True:
            interval = service.collector_interval_seconds()
            cycle_started = time.monotonic()
            try:
                endpoints = service.collector_endpoints()
                stagger = interval / len(endpoints) if len(endpoints) > 1 else 0.0
                collected = await shared_collector.collect_once(
                    service,
                    endpoints=endpoints,
                    stagger_seconds=stagger,
                )
                system_actor = ActorContext(
                    id=SYSTEM_ACTOR_ID,
                    role="admin",
                    project_ids=frozenset(),
                )
                async def reconcile_collected(endpoint: Any) -> None:
                    result = collected.get(endpoint.id)
                    if not isinstance(result, dict) or "error" in result:
                        return
                    revision = result.get("snapshot_revision")
                    key_suffix = revision if isinstance(revision, int) else int(time.time())
                    with contextlib.suppress(Exception):
                        await reconcile_endpoint_keepalive(
                            system_actor,
                            endpoint.id,
                            idempotency_key=f"keepalive-reconcile:{endpoint.id}:{key_suffix}",
                        )

                await asyncio.gather(*(reconcile_collected(endpoint) for endpoint in endpoints))
            except Exception:
                # Per-endpoint failures are already recorded by SSHCollector. This
                # protects the service loop from an unexpected local failure.
                pass
            if time.monotonic() >= next_prune_at:
                with contextlib.suppress(Exception):
                    service.prune_telemetry_history()
                next_prune_at = time.monotonic() + 3600
            elapsed = time.monotonic() - cycle_started
            await asyncio.sleep(max(0.25, interval - elapsed))

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI):
        task = None
        if inventory.collector.enabled:
            task = asyncio.create_task(collector_loop(), name="serverpilot-collector")
        application.state.collector_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="ServerPilot", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.settings = settings
    # A narrow integration hook for the collector/recovery path.  It accepts
    # an endpoint only; callers cannot inject a remote target or worker
    # identity, and all execution stays behind the service transition plan.
    app.state.reconcile_endpoint_keepalive = reconcile_endpoint_keepalive
    limiter = RateLimiter(settings.rate_limit_per_minute)
    ssh_preview_secret = secrets.token_bytes(32)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or secrets.token_urlsafe(32))
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )

    @app.middleware("http")
    async def body_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.request_body_limit_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "schema_version": SCHEMA_VERSION,
                    "error": {"code": "body_too_large", "message": "request body is too large"},
                },
            )
        return await call_next(request)

    @app.exception_handler(BrokerError)
    async def broker_error_handler(_request: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "validation_error",
                    "message": "invalid request",
                    "details": jsonable_encoder(exc.errors()),
                },
            },
        )

    def api_actor(request: Request) -> ActorContext:
        actor = service.local_actor(
            request.headers.get("x-serverpilot-actor", "agent"),
            coordination_uri=request.headers.get("x-serverpilot-coordination-uri"),
        )
        limiter.check(actor.id)
        return actor

    def session_actor(request: Request) -> ActorContext:
        actor_id = request.session.get("actor_id", "human")
        request.session.setdefault("actor_id", actor_id)
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        actor = service.local_actor(actor_id)
        limiter.check(actor.id)
        return actor

    def require_session_csrf(request: Request, submitted: str) -> None:
        expected = request.session.get("csrf")
        if not expected or not hmac.compare_digest(submitted, expected):
            raise BrokerError(
                "csrf_failed",
                "表单会话已失效，请刷新页面后重试",
                status_code=403,
            )

    def ssh_projects(project_ids: list[str] | None) -> list[str]:
        # Endpoint project lists are legacy metadata only.  A server is visible
        # to every claim, so SSH registration does not need a project registry.
        return project_ids or []

    def ssh_preview_token(command: str | list[str], project_ids: list[str]) -> str:
        binding = json.dumps(
            {"command": command, "project_ids": project_ids},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(ssh_preview_secret, binding, hashlib.sha256).hexdigest()

    def parsed_ssh_command(command: str) -> ParsedSSHCommand:
        try:
            return parse_ssh_command(command)
        except ValueError as exc:
            raise BrokerError(
                "invalid_ssh_command",
                str(exc),
                status_code=422,
            ) from exc

    def ssh_endpoint_state(
        actor: ActorContext,
        parsed: ParsedSSHCommand,
        endpoint_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        endpoints = service.list_endpoints(actor)["data"]
        same_address = next(
            (
                endpoint
                for endpoint in endpoints
                if (endpoint["host"], endpoint["port"]) == (parsed.host, parsed.port)
            ),
            None,
        )
        id_owner = next(
            (endpoint for endpoint in endpoints if endpoint["id"] == (endpoint_id or parsed.endpoint_id)),
            None,
        )
        id_collision = id_owner if id_owner is not None and id_owner is not same_address else None
        return same_address, id_collision

    def service_contract(method_name: str):  # type: ignore[no-untyped-def]
        method = getattr(service, method_name, None)
        if not callable(method):
            raise BrokerError(
                "contract_missing",
                f"service method {method_name} is not implemented",
                status_code=501,
                details={"method": method_name},
            )
        return method

    ApiActor = Annotated[ActorContext, Depends(api_actor)]

    # ---- health and REST read routes ------------------------------------------

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        return {
            "status": "live",
            "schema_version": SCHEMA_VERSION,
            "version": __version__,
            "capabilities": list(dict.fromkeys((*API_CAPABILITIES, "endpoint_deletion"))),
        }

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        ready = service.database.ready()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "database_ready": ready,
                "inventory_readable": settings.inventory_path.exists(),
                "single_writer": True,
                "daemon_instance_id": settings.daemon_instance_id,
                "process_id": os.getpid(),
            },
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return service.metrics()

    @app.get("/api/v1/snapshot")
    def snapshot(
        actor: ApiActor,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        return service.snapshot(
            actor,
            compact=compact,
            endpoint_id=endpoint_id,
            state=state,
            only_available=only_available,
        )

    @app.get("/api/v1/state", response_model=ControlPlaneSnapshot)
    def control_plane_state(actor: ApiActor) -> dict[str, Any]:
        return service.control_plane_state(actor)

    @app.get("/api/v1/endpoints")
    def endpoints(actor: ApiActor) -> dict[str, Any]:
        return service.list_endpoints(actor)

    @app.get("/api/v1/endpoints/{endpoint_id}/history")
    def endpoint_history(
        endpoint_id: str,
        actor: ApiActor,
        window_seconds: int = 3600,
        points: int = 120,
    ) -> dict[str, Any]:
        return service.endpoint_history(
            actor,
            endpoint_id,
            window_seconds=window_seconds,
            max_points=points,
        )

    @app.get("/api/v1/coordination")
    def coordination(actor: ApiActor) -> dict[str, Any]:
        return service.coordination(actor)

    @app.get("/api/v1/gpus")
    def gpus(
        actor: ApiActor,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        return service.list_gpus(
            actor,
            state=state,
            endpoint_id=endpoint_id,
            only_available=only_available,
            compact=compact,
        )

    @app.get("/api/v1/gpus/{gpu_id}")
    def gpu_detail(gpu_id: str, actor: ApiActor) -> dict[str, Any]:
        values = service.list_gpus(actor)["data"]
        value = next((item for item in values if item["id"] == gpu_id), None)
        if value is None:
            raise BrokerError("gpu_not_found", "GPU is not visible or does not exist", status_code=404)
        return {"schema_version": SCHEMA_VERSION, "data": value}

    @app.get("/api/v1/gpus/{gpu_id}/history")
    def gpu_history(
        gpu_id: str,
        actor: ApiActor,
        window_seconds: int = 3600,
        points: int = 120,
    ) -> dict[str, Any]:
        return service.gpu_history(
            actor,
            gpu_id,
            window_seconds=window_seconds,
            max_points=points,
        )

    @app.get("/api/v1/processes")
    def processes(actor: ApiActor) -> dict[str, Any]:
        return service.list_processes(actor)

    @app.get("/api/v1/requests")
    def requests(actor: ApiActor) -> dict[str, Any]:
        return service.list_requests(actor)

    @app.get("/api/v1/leases")
    def leases(actor: ApiActor) -> dict[str, Any]:
        return service.list_leases(actor)

    @app.get("/api/v1/reservations")
    def reservations(actor: ApiActor) -> dict[str, Any]:
        return service.list_reservations(actor)

    @app.get("/api/v1/maintenance")
    def maintenance(actor: ApiActor) -> dict[str, Any]:
        return service.list_maintenance(actor)

    @app.get("/api/v1/alerts")
    def alerts(actor: ApiActor) -> dict[str, Any]:
        return service.list_alerts(actor)

    @app.get("/api/v1/events")
    def events(actor: ApiActor, after_id: int = 0, limit: int = 200) -> dict[str, Any]:
        return service.list_events(actor, after_id=after_id, limit=limit)

    @app.get("/api/v1/events/export.csv", response_class=PlainTextResponse)
    def export_events(actor: ApiActor, after_id: int = 0) -> PlainTextResponse:
        values = service.list_events(actor, after_id=after_id, limit=1000)["data"]
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["id", "created_at", "actor_id", "action", "resource_type", "resource_id", "result", "summary"],
        )
        writer.writeheader()
        for value in values:
            writer.writerow({**value, "summary": json_dump(value["summary"])})
        return PlainTextResponse(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=serverpilot-events.csv"},
        )

    @app.get("/api/v1/projects")
    def projects(actor: ApiActor) -> dict[str, Any]:
        return service.list_projects(actor)

    @app.get("/api/v1/workload-profiles")
    def workload_profiles(
        actor: ApiActor, project_id: str | None = None
    ) -> dict[str, Any]:
        return service.list_workload_profiles(actor, project_id=project_id)

    @app.get("/api/v1/scheduler-targets")
    def scheduler_targets(actor: ApiActor) -> dict[str, Any]:
        return service.list_scheduler_targets(actor)

    @app.get("/api/v1/scheduler-targets/{target_id}/access")
    def scheduler_target_access(target_id: str, actor: ApiActor) -> dict[str, Any]:
        return service.scheduler_access_status(actor, target_id)

    @app.get("/api/v1/scheduler-jobs")
    def scheduler_jobs(
        actor: ApiActor,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return service.list_scheduler_jobs(actor, project_id=project_id)

    @app.get("/api/v1/scheduler-jobs/{job_id}")
    def scheduler_job(job_id: str, actor: ApiActor) -> dict[str, Any]:
        return service.refresh_scheduler_job(actor, job_id)

    @app.get("/api/v1/scheduler-transfers")
    def scheduler_transfers(
        actor: ApiActor,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return service.list_scheduler_transfers(actor, project_id=project_id)

    @app.get("/api/v1/scheduler-transfers/{transfer_id}")
    def scheduler_transfer(transfer_id: str, actor: ApiActor) -> dict[str, Any]:
        return service.scheduler_transfer_status(actor, transfer_id)

    @app.get("/api/v1/resource-providers")
    def resource_providers(
        actor: ApiActor,
        provider_type: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        return service_contract("list_resource_providers")(
            actor,
            provider_type=provider_type,
            enabled=enabled,
        )

    @app.get("/api/v1/resource-monitor")
    def resource_monitor(
        actor: ApiActor,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return service_contract("resource_monitor")(actor, project_id=project_id)

    @app.get("/api/v1/resource-claims")
    def resource_claims(
        actor: ApiActor,
        project_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        return service_contract("list_resource_claims")(
            actor,
            project_id=project_id,
            state=state,
        )

    @app.get("/api/v1/resource-plan-evaluations")
    def resource_plan_evaluations(
        actor: ApiActor,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return service_contract("list_resource_plan_evaluations")(
            actor,
            project_id=project_id,
        )

    @app.get("/api/v1/resource-run-actuals")
    def resource_run_actuals(
        actor: ApiActor,
        project_id: str | None = None,
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        return service_contract("list_resource_run_actuals")(
            actor,
            project_id=project_id,
            task_ref=task_ref,
        )

    @app.get("/api/v1/actors")
    def actors(actor: ApiActor) -> dict[str, Any]:
        return service.list_actors(actor)

    @app.get("/api/v1/config/effective")
    def effective_config(actor: ApiActor) -> dict[str, Any]:
        return service.effective_config(actor)

    @app.get("/api/v1/settings/collector")
    def collector_settings(actor: ApiActor) -> dict[str, Any]:
        return service.collector_settings(actor)

    @app.get("/api/v1/doctor")
    def doctor(actor: ApiActor) -> dict[str, Any]:
        return service.doctor(actor)

    # ---- REST mutation routes --------------------------------------------------

    @app.patch("/api/v1/settings/collector")
    def update_collector_settings(
        settings_data: CollectorSettingsUpdate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.update_collector_settings(
            actor,
            settings_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/claims")
    async def claim_now(
        request_data: RequestCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Create an immediate claim, reclaiming only its selected keepers once."""

        mutation_key = _idempotency_key(idempotency_key)
        try:
            return service.create_request(
                actor,
                request_data,
                idempotency_key=mutation_key,
                activate_if_allocated=True,
            )
        except BrokerError as exc:
            if exc.code != "no_capacity":
                raise
            claimed = await reclaim_keepalive_for_claim(
                actor,
                lambda: request_data,
                lambda: service.create_request(
                    actor,
                    request_data,
                    idempotency_key=mutation_key,
                    activate_if_allocated=True,
                ),
                idempotency_key=mutation_key,
            )
            if claimed is None:
                raise exc
            return claimed

    @app.post("/api/v1/resource-plan-evaluations")
    def evaluate_resource_plan(
        evaluation: ResourcePlanEvaluationInput,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service_contract("evaluate_resource_plan")(
            actor,
            evaluation,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/resource-claims")
    def create_resource_claim(
        claim: ResourceClaim,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service_contract("claim_resource")(
            actor,
            claim,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/resource-claims/{claim_id}/release")
    def release_resource_claim(
        claim_id: str,
        body: dict[str, str],
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service_contract("release_resource_claim")(
            actor,
            claim_id,
            reason=body.get("reason", "workload_completed"),
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/resource-run-actuals")
    def record_resource_run_actual(
        actual: ResourceRunActualInput,
        actor: ApiActor,
        claim_id: str | None = None,
        evaluation_id: str | None = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service_contract("record_resource_run_actual")(
            actor,
            actual,
            claim_id=claim_id,
            evaluation_id=evaluation_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/leases/{lease_id}/activate")
    def activate_lease(
        lease_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.activate_lease(actor, lease_id, idempotency_key=_idempotency_key(idempotency_key))

    @app.post("/api/v1/leases/{lease_id}/renew")
    def renew_lease(
        lease_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.renew_lease(actor, lease_id, idempotency_key=_idempotency_key(idempotency_key))

    @app.post("/api/v1/leases/{lease_id}/release")
    def release_lease(
        lease_id: str,
        body: dict[str, str],
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.release_lease(
            actor,
            lease_id,
            reason=body.get("reason", ""),
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/leases/{lease_id}/bind-workload")
    def bind_workload(
        lease_id: str,
        binding: LeaseBind,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.bind_workload(
            actor,
            lease_id,
            binding,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/leases/{lease_id}/bind-observed-workload")
    def bind_observed_workload(
        lease_id: str,
        binding: LeaseObservedBind,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.bind_observed_workload(
            actor,
            lease_id,
            binding,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/alerts/{alert_id}/ack")
    def acknowledge_alert(
        alert_id: str,
        acknowledgement: AlertAcknowledge,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.acknowledge_alert(
            actor,
            alert_id,
            acknowledgement,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/workload-profiles")
    def upsert_workload_profile(
        profile_data: WorkloadProfileUpsert,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.upsert_workload_profile(
            actor, profile_data, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/scheduler-targets")
    def upsert_scheduler_target(
        target_data: SchedulerTargetUpsert,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.upsert_scheduler_target(
            actor,
            target_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/workload-profiles/{profile_id}/scheduler-submit")
    def submit_scheduler_profile(
        profile_id: str,
        submission: SchedulerProfileSubmit,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.submit_scheduler_profile(
            actor,
            profile_id,
            submission,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/scheduler-jobs")
    def submit_scheduler_one_off(
        submission: SchedulerOneOffSubmit,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.submit_scheduler_one_off(
            actor,
            submission,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/scheduler-transfers")
    def start_scheduler_upload(
        upload: SchedulerUploadRequest,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.start_scheduler_upload(
            actor,
            upload,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/scheduler-jobs/{job_id}/cancel")
    def cancel_scheduler_job(
        job_id: str,
        cancellation: SchedulerJobCancel,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.cancel_scheduler_job(
            actor,
            job_id,
            cancellation,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/workload-profiles/{profile_id}/claim")
    async def claim_workload_profile(
        profile_id: str,
        claim: WorkloadProfileClaim,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        mutation_key = _idempotency_key(idempotency_key)
        return await claim_workload_profile_now(
            actor,
            profile_id,
            claim,
            idempotency_key=mutation_key,
        )

    @app.post("/api/v1/endpoints")
    def create_endpoint(
        endpoint_data: EndpointCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_endpoint(
            actor,
            endpoint_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.patch("/api/v1/endpoints/{endpoint_id}")
    def update_endpoint(
        endpoint_id: str,
        endpoint_data: EndpointUpdate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.update_endpoint(
            actor,
            endpoint_id,
            endpoint_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/endpoints/{endpoint_id}/retire")
    def retire_endpoint(
        endpoint_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.retire_endpoint(
            actor,
            endpoint_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/endpoints/{endpoint_id}/keepalive")
    async def set_endpoint_keepalive(
        endpoint_id: str,
        state: EndpointKeepaliveRequest,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Set endpoint desired policy, then reconcile independent GPU keepers.

        ``enabled`` is deliberately the only caller input.  It maps to the
        endpoint desired policy and never permits a client to name a worker,
        a GPU UUID, a PID, or arbitrary remote parameters.
        """

        mutation_key = _idempotency_key(idempotency_key)
        policy = "idle_keepalive" if state.enabled else "disabled"
        configured = service.configure_keepalive_policy(
            actor,
            endpoint_id,
            policy,
            idempotency_key=mutation_key,
        )
        reconciled = await reconcile_endpoint_keepalive(
            actor,
            endpoint_id,
            idempotency_key=mutation_key,
        )
        keepalive = reconciled.get("keepalive")
        if not isinstance(keepalive, dict):
            raise BrokerError(
                "keepalive_endpoint_observation_missing",
                "endpoint keepalive state could not be projected after reconciliation",
                status_code=503,
            )
        event_id = configured.get("event_id")
        revision = reconciled.get("snapshot_revision")
        return _public_keepalive_result(
            endpoint_id,
            keepalive,
            event_id=event_id if isinstance(event_id, int) else None,
            snapshot_revision=revision if isinstance(revision, int) else None,
        )

    @app.delete("/api/v1/endpoints/{endpoint_id}")
    def delete_endpoint(
        endpoint_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.pause_endpoint(
            actor,
            endpoint_id,
            idempotency_key=_idempotency_key(idempotency_key),
            _idempotency_action="endpoint.delete",
            _compatibility_alias=True,
        )

    @app.post("/ui/endpoints/ssh/preview")
    def preview_ssh_endpoint(
        preview: SSHCommandRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = session_actor(request)
        require_session_csrf(request, preview.csrf)
        parsed = parsed_ssh_command(preview.command)
        project_ids = ssh_projects(preview.project_ids)
        existing_endpoint, id_collision = ssh_endpoint_state(actor, parsed)
        endpoint_id = existing_endpoint["id"] if existing_endpoint is not None else parsed.endpoint_id
        endpoint = {
            "id": endpoint_id,
            "host": parsed.host,
            "port": parsed.port,
            "ssh_user": parsed.user,
            "ssh_alias": None,
            "labels": ["gpu", "direct-ssh"],
            "storage_group": None,
            "expected_gpu_count": None,
            "expected_gpu_total_vram_mib": None,
            "project_ids": project_ids,
            "enabled": True,
        }
        status = "existing" if existing_endpoint is not None else "id_collision" if id_collision else "new"
        return {
            "data": {
                "status": status,
                "normalized_command": parsed.normalized_command,
                "endpoint": endpoint,
                "existing_endpoint": existing_endpoint,
                "id_collision": id_collision,
                "preview_token": ssh_preview_token(preview.command, project_ids),
            }
        }

    @app.post("/ui/endpoints/ssh/commit")
    def commit_ssh_endpoint(
        commit: SSHCommandCommit,
        request: Request,
    ) -> dict[str, Any]:
        actor = session_actor(request)
        require_session_csrf(request, commit.csrf)
        parsed = parsed_ssh_command(commit.command)
        project_ids = ssh_projects(commit.project_ids)
        expected_token = ssh_preview_token(commit.command, project_ids)
        if not hmac.compare_digest(commit.preview_token, expected_token):
            raise BrokerError(
                "invalid_ssh_preview_token",
                "SSH 命令或项目选择已在预览后改变，请重新检查",
                status_code=409,
            )

        existing_endpoint, _ = ssh_endpoint_state(actor, parsed)
        endpoint_id = commit.endpoint_id or (
            existing_endpoint["id"] if existing_endpoint is not None else parsed.endpoint_id
        )
        existing_endpoint, id_collision = ssh_endpoint_state(actor, parsed, endpoint_id)
        if existing_endpoint is not None and endpoint_id != existing_endpoint["id"]:
            raise BrokerError(
                "endpoint_address_exists",
                "该 host:port 已由另一个服务器 ID 使用",
                status_code=409,
                details={"existing_endpoint": existing_endpoint},
            )
        if id_collision is not None:
            raise BrokerError(
                "endpoint_id_collision",
                "服务器 ID 已用于另一个 host:port；请明确填写其他 ID",
                status_code=409,
                details={"existing_endpoint": id_collision},
            )

        result = service.upsert_endpoint(
            actor,
            EndpointUpsert(
                id=endpoint_id,
                host=parsed.host,
                port=parsed.port,
                ssh_user=parsed.user,
                labels=["gpu", "direct-ssh"],
                storage_group=None,
                expected_gpu_count=None,
                expected_gpu_total_vram_mib=None,
                project_ids=project_ids,
                enabled=True,
            ),
            idempotency_key=secrets.token_hex(16),
        )
        return {"data": result}

    @app.post("/ui/endpoints/ssh/batch/preview")
    def preview_ssh_endpoints(
        preview: SSHCommandsRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = session_actor(request)
        require_session_csrf(request, preview.csrf)
        project_ids = ssh_projects(preview.project_ids)
        endpoints = service.list_endpoints(actor)["data"]
        seen_addresses: set[tuple[str, int]] = set()
        entries: list[dict[str, Any]] = []

        for line_number, command in enumerate(preview.commands, start=1):
            try:
                parsed = parsed_ssh_command(command)
            except BrokerError as exc:
                entries.append({"line": line_number, "command": command, "status": "invalid", "error": exc.message})
                continue
            address = (parsed.host, parsed.port)
            if address in seen_addresses:
                entries.append({"line": line_number, "command": command, "status": "duplicate", "error": "同一 host:port 已在本次粘贴中出现"})
                continue
            seen_addresses.add(address)
            existing = next((endpoint for endpoint in endpoints if (endpoint["host"], endpoint["port"]) == address), None)
            id_owner = next((endpoint for endpoint in endpoints if endpoint["id"] == parsed.endpoint_id), None)
            id_collision = id_owner if id_owner is not None and id_owner is not existing else None
            if id_collision is not None:
                entries.append({"line": line_number, "command": command, "status": "id_collision", "error": "服务器名称与另一台服务器冲突"})
                continue
            endpoint_id = existing["id"] if existing is not None else parsed.endpoint_id
            entries.append(
                {
                    "line": line_number,
                    "command": command,
                    "normalized_command": parsed.normalized_command,
                    "status": "existing" if existing is not None else "new",
                    "endpoint": {"id": endpoint_id, "host": parsed.host, "port": parsed.port, "ssh_user": parsed.user},
                }
            )
        valid_count = sum(entry["status"] in {"new", "existing"} for entry in entries)
        return {
            "data": {
                "entries": entries,
                "valid_count": valid_count,
                "preview_token": ssh_preview_token(preview.commands, project_ids),
            }
        }

    @app.post("/ui/endpoints/ssh/batch/commit")
    def commit_ssh_endpoints(
        commit: SSHCommandsCommit,
        request: Request,
    ) -> dict[str, Any]:
        actor = session_actor(request)
        require_session_csrf(request, commit.csrf)
        project_ids = ssh_projects(commit.project_ids)
        expected_token = ssh_preview_token(commit.commands, project_ids)
        if not hmac.compare_digest(commit.preview_token, expected_token):
            raise BrokerError("invalid_ssh_preview_token", "SSH 命令或项目选择已在预览后改变，请重新检查", status_code=409)

        endpoints = service.list_endpoints(actor)["data"]
        seen_addresses: set[tuple[str, int]] = set()
        results: list[dict[str, Any]] = []
        for line_number, command in enumerate(commit.commands, start=1):
            try:
                parsed = parsed_ssh_command(command)
            except BrokerError as exc:
                results.append({"line": line_number, "status": "invalid", "error": exc.message})
                continue
            address = (parsed.host, parsed.port)
            if address in seen_addresses:
                results.append({"line": line_number, "status": "duplicate", "error": "同一 host:port 已在本次粘贴中出现"})
                continue
            seen_addresses.add(address)
            existing = next((endpoint for endpoint in endpoints if (endpoint["host"], endpoint["port"]) == address), None)
            id_owner = next((endpoint for endpoint in endpoints if endpoint["id"] == parsed.endpoint_id), None)
            if id_owner is not None and id_owner is not existing:
                results.append({"line": line_number, "status": "id_collision", "error": "服务器名称与另一台服务器冲突"})
                continue
            endpoint_id = existing["id"] if existing is not None else parsed.endpoint_id
            try:
                result = service.upsert_endpoint(
                    actor,
                    EndpointUpsert(
                        id=endpoint_id,
                        host=parsed.host,
                        port=parsed.port,
                        ssh_user=parsed.user,
                        labels=["gpu", "direct-ssh"],
                        storage_group=None,
                        expected_gpu_count=None,
                        expected_gpu_total_vram_mib=None,
                        project_ids=project_ids,
                        enabled=True,
                    ),
                    idempotency_key=secrets.token_hex(16),
                )
            except BrokerError as exc:
                results.append({"line": line_number, "status": "error", "error": exc.message})
                continue
            endpoints.append(result["endpoint"])
            results.append({"line": line_number, "status": "updated" if existing is not None else "registered", "endpoint": result["endpoint"]})
        registered_count = sum(result["status"] == "registered" for result in results)
        updated_count = sum(result["status"] == "updated" for result in results)
        return {"data": {"entries": results, "registered_count": registered_count, "updated_count": updated_count}}

    @app.post("/api/v1/actors")
    def create_actor(
        actor_data: ActorCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_actor(actor, actor_data, idempotency_key=_idempotency_key(idempotency_key))

    @app.post("/api/v1/tokens/{token_id}/revoke")
    def revoke_token(
        token_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.revoke_token(actor, token_id, idempotency_key=_idempotency_key(idempotency_key))

    @app.post("/api/v1/retention/prune")
    def prune_telemetry(
        retention: RetentionPrune,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.prune_telemetry(
            actor,
            retention,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/reconcile")
    def reconcile(
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        _idempotency_key(idempotency_key)  # reconciliation is auditable but not re-run by the service yet.
        return service.reconcile(actor)

    # ---- Server-sent event replay ---------------------------------------------

    @app.get("/api/v1/events/stream")
    async def event_stream(
        request: Request,
        after_id: int = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            actor = session_actor(request) if request.session.get("actor_id") else api_actor(request)
        except BrokerError:
            raise

        try:
            replay_cursor = max(after_id, int(last_event_id or "0"))
        except ValueError:
            raise BrokerError("invalid_event_cursor", "Last-Event-ID must be an integer", status_code=422) from None

        async def generator() -> AsyncIterator[str]:
            cursor = replay_cursor
            while True:
                if await request.is_disconnected():
                    return
                values = service.list_events(actor, after_id=cursor, limit=200)["data"]
                for event in values:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: audit\ndata: {json_dump(event)}\n\n"
                if not values:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generator(), media_type="text/event-stream")

    # ---- Functional web GUI ----------------------------------------------------

    def ui_context(request: Request, actor: ActorContext | None, *, page: str, payload: Any = None) -> dict[str, Any]:
        return {
            "request": request,
            "page": page,
            "actor": actor,
            "payload": payload,
            "payload_json": json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            "csrf": request.session.get("csrf"),
            "notice": request.query_params.get("notice"),
            "schema_version": SCHEMA_VERSION,
            "format_constraints": _format_constraints,
        }

    def _format_constraints(constraints: dict[str, Any]) -> str:
        parts = [f"{constraints.get('gpu_count', 1)} GPU"]
        if constraints.get("min_available_cpu_cores") is not None:
            parts.append(f"CPU 可用 {constraints['min_available_cpu_cores']:g} 核")
        if constraints.get("min_available_memory_mib") is not None:
            parts.append(f"内存可用 {constraints['min_available_memory_mib'] / 1024:g} GiB")
        if constraints.get("min_total_vram_mib") is not None:
            parts.append(f"单卡显存总量 {constraints['min_total_vram_mib'] / 1024:g} GiB")
        if constraints.get("min_free_vram_mib") is not None:
            parts.append(f"单卡显存可用 {constraints['min_free_vram_mib'] / 1024:g} GiB")
        endpoint_ids = constraints.get("endpoint_ids") or []
        if endpoint_ids:
            parts.append(f"服务器：{'、'.join(endpoint_ids)}")
        return " · ".join(parts)

    def ui_reference_data(actor: ActorContext) -> dict[str, Any]:
        """Shared select options for server-rendered human forms.

        The GUI deliberately gets these values through the same filtered read
        models as REST/MCP.  A new form therefore cannot accidentally expose a
        project, endpoint, or GPU the current actor is not allowed to use.
        """
        snapshot = service.snapshot(actor)["data"]
        workload_profiles = service.list_workload_profiles(actor)["data"]
        return {
            "endpoints": snapshot["endpoints"],
            "gpus": snapshot["gpus"],
            "workload_profiles": workload_profiles,
            "claimable_workload_profiles": [
                profile for profile in workload_profiles if profile["enabled"]
            ],
        }

    def page_payload(page: str, actor: ActorContext) -> Any:
        if page == "overview":
            overview = service.snapshot(actor)
            return {
                **overview["data"],
                "snapshot_revision": overview["snapshot_revision"],
                "server_time": overview["server_time"],
                **ui_reference_data(actor),
            }
        if page == "gpus":
            return {"gpus": service.list_gpus(actor)["data"]}
        if page == "requests":
            return {
                **ui_reference_data(actor),
                "requests": service.list_requests(actor)["data"],
                "leases": service.list_leases(actor)["data"],
            }
        if page == "leases":
            return {"leases": service.list_leases(actor)["data"]}
        if page == "reservations":
            return {**ui_reference_data(actor), "reservations": service.list_reservations(actor)["data"]}
        if page == "identities":
            return {
                **ui_reference_data(actor),
                "actors": service.list_actors(actor)["data"] if actor.is_admin else [],
            }
        if page == "maintenance":
            return {**ui_reference_data(actor), "maintenance": service.list_maintenance(actor)["data"]}
        if page == "alerts":
            return {"alerts": service.list_alerts(actor)["data"]}
        if page == "audit":
            return {"events": service.list_events(actor)["data"]}
        if page == "doctor":
            return {"doctor": service.doctor(actor)["data"], "config": service.effective_config(actor)["data"]}
        raise BrokerError("page_not_found", "web page does not exist", status_code=404)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui/{page}", response_class=HTMLResponse)
    def web_page(request: Request, page: str = "overview") -> HTMLResponse:
        actor = session_actor(request)
        payload = page_payload(page, actor)
        template = "dashboard.html" if page == "overview" else "page.html"
        return templates.TemplateResponse(
            request,
            template,
            ui_context(request, actor, page=page, payload=payload),
        )

    @app.get("/ui/gpus/{gpu_id}", response_class=HTMLResponse)
    def web_gpu_detail(gpu_id: str, request: Request) -> HTMLResponse:
        actor = session_actor(request)
        data = next((item for item in service.list_gpus(actor)["data"] if item["id"] == gpu_id), None)
        if data is None:
            raise BrokerError("gpu_not_found", "GPU is not visible or does not exist", status_code=404)
        return templates.TemplateResponse(
            request,
            "page.html",
            ui_context(request, actor, page="gpu-detail", payload={"gpu": data}),
        )

    @app.post("/ui/actor")
    async def web_actor(request: Request, actor_id: Annotated[str, Form()]) -> RedirectResponse:
        actor = service.local_actor(actor_id)
        request.session["actor_id"] = actor.id
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

    def parse_ui_request(payload: dict[str, Any]) -> RequestCreate:
        if "constraints" in payload:
            return RequestCreate.model_validate(payload)
        return RequestCreateFlat.model_validate(payload).canonical()

    def _form_value(form: Any, name: str, *, required: bool = False) -> str | None:
        value = form.get(name)
        text = str(value).strip() if value is not None else ""
        if required and not text:
            raise BrokerError("form_field_required", f"请填写 {name}", status_code=422)
        return text or None

    def _form_list(form: Any, name: str) -> list[str]:
        return [str(value).strip() for value in form.getlist(name) if str(value).strip()]

    def _form_int(
        form: Any,
        name: str,
        *,
        required: bool = False,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        value = _form_value(form, name, required=required)
        if value is None:
            return None
        try:
            number = int(value)
        except ValueError as exc:
            raise BrokerError("invalid_form_number", f"{name} 必须是整数", status_code=422) from exc
        if minimum is not None and number < minimum:
            raise BrokerError("invalid_form_number", f"{name} 不能小于 {minimum}", status_code=422)
        if maximum is not None and number > maximum:
            raise BrokerError("invalid_form_number", f"{name} 不能大于 {maximum}", status_code=422)
        return number

    def _form_float(
        form: Any,
        name: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        value = _form_value(form, name)
        if value is None:
            return None
        try:
            number = float(value)
        except ValueError as exc:
            raise BrokerError("invalid_form_number", f"{name} 必须是数字", status_code=422) from exc
        if minimum is not None and number < minimum:
            raise BrokerError("invalid_form_number", f"{name} 不能小于 {minimum:g}", status_code=422)
        if maximum is not None and number > maximum:
            raise BrokerError("invalid_form_number", f"{name} 不能大于 {maximum:g}", status_code=422)
        return number

    def _form_boolean(form: Any, name: str) -> bool:
        value = (_form_value(form, name, required=True) or "").lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
        raise BrokerError("invalid_form_boolean", f"{name} 必须是 true 或 false", status_code=422)

    def _form_timestamp(form: Any, name: str) -> str:
        value = _form_value(form, name, required=True)
        assert value is not None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BrokerError("invalid_form_time", f"{name} 不是有效时间", status_code=422) from exc
        if parsed.tzinfo is not None:
            return parsed.isoformat()
        timezone_name = _form_value(form, "timezone") or "Asia/Shanghai"
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise BrokerError("invalid_form_timezone", "请选择有效时区", status_code=422) from exc
        return parsed.replace(tzinfo=zone).isoformat()

    def _csv_values(value: str | None) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    def ui_form_payload(action: str, form: Any) -> dict[str, Any]:
        """Translate click-first HTML forms into the unchanged domain payloads.

        Adding a human UI action only needs a form plus this explicit mapping;
        validation and authorization still happen in the shared Pydantic/service
        boundary used by REST, CLI and MCP.
        """
        if action == "profile-claim":
            return {
                "profile_id": _form_value(form, "profile_id", required=True),
                "task_ref": _form_value(form, "task_ref", required=True),
            }
        if action in {"request", "quick-claim"}:
            min_available_memory_gib = _form_int(form, "min_available_memory_gib", minimum=0)
            min_total_gib = _form_int(form, "min_total_vram_gib", minimum=1)
            min_free_gib = _form_int(form, "min_free_vram_gib", minimum=0)
            endpoint_id = _form_value(form, "endpoint_id")
            gpu_ids = _form_list(form, "gpu_ids")
            task_ref = _form_value(form, "task_ref", required=True)
            return {
                "project_id": _form_value(form, "project_id", required=True),
                "task_ref": task_ref,
                "purpose": task_ref if action == "quick-claim" else _form_value(form, "purpose", required=True),
                "gpu_count": len(gpu_ids) or _form_int(form, "gpu_count", required=True, minimum=1),
                "min_available_cpu_cores": _form_float(form, "min_available_cpu_cores", minimum=0),
                "min_available_memory_mib": min_available_memory_gib * 1024
                if min_available_memory_gib is not None
                else None,
                "min_total_vram_mib": min_total_gib * 1024 if min_total_gib is not None else None,
                "min_free_vram_mib": min_free_gib * 1024 if min_free_gib is not None else None,
                "placement": "exact" if gpu_ids else (_form_value(form, "placement") or "pack"),
                "endpoint_ids": [endpoint_id] if endpoint_id else [],
                "gpu_ids": gpu_ids,
            }
        if action == "cancel-request":
            return {"request_id": _form_value(form, "request_id", required=True)}
        if action in {"activate-lease", "renew-lease"}:
            return {"lease_id": _form_value(form, "lease_id", required=True)}
        if action == "release-lease":
            return {
                "lease_id": _form_value(form, "lease_id", required=True),
                "reason": _form_value(form, "reason", required=True),
            }
        if action == "bind-workload":
            process_keys = _form_value(form, "process_keys")
            return {
                "lease_id": _form_value(form, "lease_id", required=True),
                "run_id": _form_value(form, "run_id", required=True),
                "process_keys": [
                    item.strip()
                    for item in (process_keys or "").replace("\n", ",").split(",")
                    if item.strip()
                ],
            }
        if action == "reservation":
            return {
                "project_id": _form_value(form, "project_id", required=True),
                "gpu_ids": _form_list(form, "gpu_ids"),
                "start_at": _form_timestamp(form, "start_at"),
                "end_at": _form_timestamp(form, "end_at"),
                "reason": _form_value(form, "reason", required=True),
            }
        if action == "cancel-reservation":
            return {"reservation_id": _form_value(form, "reservation_id", required=True)}
        if action == "maintenance":
            target = _form_value(form, "target", required=True)
            assert target is not None
            target_type, separator, target_id = target.partition("|")
            if not separator or not target_id:
                raise BrokerError("invalid_maintenance_target", "请选择有效的维护对象", status_code=422)
            if target_type not in {"endpoint", "gpu"}:
                raise BrokerError("invalid_maintenance_target", "维护对象必须是 endpoint 或 GPU", status_code=422)
            return {
                "endpoint_id": target_id if target_type == "endpoint" else None,
                "gpu_id": target_id if target_type == "gpu" else None,
                "start_at": _form_timestamp(form, "start_at"),
                "end_at": _form_timestamp(form, "end_at"),
                "reason": _form_value(form, "reason", required=True),
            }
        if action == "ack-alert":
            return {
                "alert_id": _form_value(form, "alert_id", required=True),
                "note": _form_value(form, "note"),
            }
        if action == "workload-profile":
            duration_hours = _form_int(form, "duration_hours", required=True, minimum=1, maximum=720)
            gpu_count = _form_int(form, "gpu_count", required=True, minimum=1)
            min_available_memory_gib = _form_int(form, "min_available_memory_gib", minimum=0)
            min_total_gib = _form_int(form, "min_total_vram_gib", minimum=1)
            min_free_gib = _form_int(form, "min_free_vram_gib", minimum=0)
            assert duration_hours is not None and gpu_count is not None
            return {
                "id": _form_value(form, "id", required=True),
                "project_id": _form_value(form, "project_id", required=True),
                "display_name": _form_value(form, "display_name", required=True),
                "purpose": _form_value(form, "purpose", required=True),
                "duration_seconds": duration_hours * 3600,
                "constraints": {
                    "gpu_count": gpu_count,
                    "min_available_cpu_cores": _form_float(form, "min_available_cpu_cores", minimum=0),
                    "min_available_memory_mib": min_available_memory_gib * 1024
                    if min_available_memory_gib is not None
                    else None,
                    "min_total_vram_mib": min_total_gib * 1024 if min_total_gib is not None else None,
                    "min_free_vram_mib": min_free_gib * 1024 if min_free_gib is not None else None,
                    "placement": "pack",
                    "endpoint_ids": _form_list(form, "endpoint_ids"),
                },
                "enabled": _form_boolean(form, "enabled"),
            }
        if action == "actor":
            return {
                "id": _form_value(form, "id", required=True),
                "display_name": _form_value(form, "display_name", required=True),
                "role": _form_value(form, "role", required=True),
                "token_label": _form_value(form, "token_label", required=True),
            }
        if action == "endpoint":
            host = _form_value(form, "host", required=True)
            port = _form_int(form, "port", required=True, minimum=1, maximum=65535)
            owner_project_id = _form_value(form, "owner_project_id", required=True)
            assert host is not None and port is not None
            generated_id = "server-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:96]
            generated_id = f"{generated_id}-p{port}"
            requested_id = re.sub(
                r"[^a-z0-9-]+", "-", (_form_value(form, "id") or "").lower()
            ).strip("-")
            if requested_id and not requested_id[0].isalpha():
                requested_id = f"server-{requested_id}"
            return {
                "id": requested_id or generated_id,
                "host": host,
                "port": port,
                "ssh_user": _form_value(form, "ssh_user", required=True),
                "labels": _csv_values(_form_value(form, "labels")),
                "storage_group": _form_value(form, "storage_group"),
                "expected_gpu_count": _form_int(form, "expected_gpu_count", minimum=1),
                "expected_gpu_total_vram_mib": _form_int(form, "expected_gpu_total_vram_mib", minimum=1),
                # Project ownership is the endpoint mutation boundary.  The
                # legacy list stays populated for older REST clients.
                "owner_project_id": owner_project_id,
                "project_ids": [owner_project_id],
                "enabled": _form_boolean(form, "enabled"),
            }
        if action == "endpoint-enabled":
            return {
                "endpoint_id": _form_value(form, "endpoint_id", required=True),
                "enabled": _form_boolean(form, "enabled"),
            }
        if action == "delete-endpoint":
            return {"endpoint_id": _form_value(form, "endpoint_id", required=True)}
        if action == "revoke-token":
            return {"token_id": _form_value(form, "token_id", required=True)}
        if action == "reconcile":
            return {}
        if action == "prune-telemetry":
            days = _form_int(form, "retention_days", required=True, minimum=1)
            return {"older_than_seconds": days * 24 * 60 * 60 if days is not None else None}
        raise BrokerError("action_not_found", "web action does not exist", status_code=404)

    @app.post("/ui/action/{action}")
    async def web_action(
        action: str,
        request: Request,
        csrf: Annotated[str, Form()],
        confirmed: Annotated[str | None, Form()] = None,
        payload: Annotated[str | None, Form()] = None,
    ) -> Any:
        routes = {
            "endpoint": "/",
            "endpoint-enabled": "/",
            "request": "/ui/requests",
            "quick-claim": "/ui/requests",
            "profile-claim": "/ui/requests",
            "cancel-request": "/ui/requests",
            "activate-lease": "/ui/leases",
            "renew-lease": "/ui/leases",
            "release-lease": "/ui/leases",
            "bind-workload": "/ui/leases",
            "reservation": "/ui/reservations",
            "cancel-reservation": "/ui/reservations",
            "maintenance": "/ui/maintenance",
            "ack-alert": "/ui/alerts",
            "workload-profile": "/ui/identities",
            "actor": "/ui/identities",
            "revoke-token": "/ui/identities",
            "delete-endpoint": "/",
            "reconcile": "/ui/doctor",
            "prune-telemetry": "/ui/doctor",
        }
        route = routes.get(action, "/")
        try:
            actor = session_actor(request)
            if not csrf or csrf != request.session.get("csrf"):
                raise BrokerError("csrf_failed", "表单会话已失效，请刷新页面后重试", status_code=403)
            if confirmed != "yes":
                raise BrokerError("confirmation_required", "请先确认本次操作的影响范围", status_code=422)
            if payload and payload.strip():
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise BrokerError("invalid_json", "高级模式的 JSON payload 无效", status_code=422) from exc
            else:
                data = ui_form_payload(action, await request.form())
            key = secrets.token_hex(16)
            if action in {"request", "quick-claim"}:
                result = service.create_request(
                    actor,
                    parse_ui_request(data),
                    idempotency_key=key,
                    activate_if_allocated=action == "quick-claim",
                )
            elif action == "profile-claim":
                result = await claim_workload_profile_now(
                    actor,
                    data["profile_id"],
                    WorkloadProfileClaim.model_validate({"task_ref": data["task_ref"]}),
                    idempotency_key=key,
                )
            elif action == "endpoint":
                result = service.upsert_endpoint(
                    actor,
                    EndpointUpsert.model_validate(data),
                    idempotency_key=key,
                )
            elif action == "delete-endpoint":
                result = service.delete_endpoint(
                    actor,
                    data["endpoint_id"],
                    idempotency_key=key,
                )
            elif action == "activate-lease":
                result = service.activate_lease(actor, data["lease_id"], idempotency_key=key)
            elif action == "renew-lease":
                result = service.renew_lease(actor, data["lease_id"], idempotency_key=key)
            elif action == "release-lease":
                result = service.release_lease(
                    actor, data["lease_id"], reason=data["reason"], idempotency_key=key
                )
            elif action == "bind-workload":
                result = service.bind_workload(
                    actor,
                    data["lease_id"],
                    LeaseBind.model_validate({"run_id": data["run_id"], "process_keys": data.get("process_keys", [])}),
                    idempotency_key=key,
                )
            elif action == "ack-alert":
                result = service.acknowledge_alert(
                    actor,
                    data["alert_id"],
                    AlertAcknowledge.model_validate({"note": data.get("note")}),
                    idempotency_key=key,
                )
            elif action == "workload-profile":
                result = service.upsert_workload_profile(
                    actor,
                    WorkloadProfileUpsert.model_validate(data),
                    idempotency_key=key,
                )
            elif action == "actor":
                result = service.create_actor(actor, ActorCreate.model_validate(data), idempotency_key=key)
            elif action == "revoke-token":
                result = service.revoke_token(actor, data["token_id"], idempotency_key=key)
            elif action == "reconcile":
                result = service.reconcile(actor)
            elif action == "prune-telemetry":
                result = service.prune_telemetry(
                    actor,
                    RetentionPrune.model_validate(data),
                    idempotency_key=key,
                )
            else:
                raise BrokerError("action_not_found", "web action does not exist", status_code=404)
        except BrokerError as exc:
            notice = quote(f"未完成：{exc.message}")
            return RedirectResponse(url=f"{route}?notice={notice}", status_code=303)
        except (ValidationError, KeyError, TypeError, ValueError):
            notice = quote("未完成：请检查表单字段后重试")
            return RedirectResponse(url=f"{route}?notice={notice}", status_code=303)

        if action == "actor" and result.get("token"):
            response = templates.TemplateResponse(
                request,
                "token_created.html",
                ui_context(request, actor, page="token-created", payload=result),
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            return response

        if action in {"quick-claim", "profile-claim", "request"}:
            message = (
                "GPU 已申领，待使用；请在项目环境启动任务。未启动不会提前释放，且不会由 ServerPilot 启动远端任务"
                if result.get("lease")
                else "资源请求已进入队列"
            )
        else:
            message = f"操作完成（事件 {result.get('event_id', 'no-event')}）"
        notice = quote(message)
        return RedirectResponse(url=f"{route}?notice={notice}", status_code=303)

    return app


def _find_project_root() -> Path:
    """Find the source release root, falling back to packaged migrations."""

    configured = os.environ.get("SERVERPILOT_PROJECT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (
            candidate / "src" / "serverpilot" / "migrations"
        ).is_dir():
            return candidate
    return Path(__file__).resolve().parent
