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
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from gpu_broker import SCHEMA_VERSION, __version__
from gpu_broker.collector import SSHCollector
from gpu_broker.config import Settings, load_inventory
from gpu_broker.database import Database
from gpu_broker.importer import ParsedSSHCommand, parse_ssh_command
from gpu_broker.schemas import (
    ActorCreate,
    AlertAcknowledge,
    EndpointEnabled,
    EndpointObservation,
    EndpointUpsert,
    LeaseBind,
    MaintenanceCreate,
    ProjectUpsert,
    RequestCreate,
    RequestCreateFlat,
    RetentionPrune,
    ReservationCreate,
    SSHCommandCommit,
    SSHCommandRequest,
)
from gpu_broker.service import ActorContext, BrokerError, BrokerService
from gpu_broker.timeutil import json_dump


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


def create_app(settings: Settings) -> FastAPI:
    inventory = load_inventory(settings.inventory_path)
    project_root = settings.project_root or _find_project_root()
    service = BrokerService(Database(settings.database_url, project_root), inventory)
    service.initialize(settings.bootstrap_token)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

    async def collector_loop() -> None:
        collector = SSHCollector(inventory)
        interval = inventory.collector.interval_seconds
        next_prune_at = 0.0
        while True:
            cycle_started = time.monotonic()
            try:
                endpoints = service.collector_endpoints()
                stagger = interval / len(endpoints) if len(endpoints) > 1 else 0.0
                await collector.collect_once(
                    service,
                    endpoints=endpoints,
                    stagger_seconds=stagger,
                )
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
            task = asyncio.create_task(collector_loop(), name="gpu-broker-collector")
        application.state.collector_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="gpu-broker", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.settings = settings
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
                "error": {"code": "validation_error", "message": "invalid request", "details": exc.errors()},
            },
        )

    def api_actor(request: Request) -> ActorContext:
        actor = service.local_actor(request.headers.get("x-gpu-broker-actor", "agent"))
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
        configured = [project.id for project in inventory.projects]
        selected = configured if project_ids is None else project_ids
        unknown = [project_id for project_id in selected if project_id not in configured]
        if unknown:
            raise BrokerError(
                "invalid_endpoint_projects",
                "服务器引用了未配置的项目",
                status_code=422,
                details={"unknown_project_ids": unknown},
            )
        return selected

    def ssh_preview_token(command: str, project_ids: list[str]) -> str:
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

    ApiActor = Annotated[ActorContext, Depends(api_actor)]

    # ---- health and REST read routes ------------------------------------------

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        return {"status": "live", "schema_version": SCHEMA_VERSION, "version": __version__}

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

    @app.get("/api/v1/endpoints")
    def endpoints(actor: ApiActor) -> dict[str, Any]:
        return service.list_endpoints(actor)

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
            headers={"Content-Disposition": "attachment; filename=gpu-broker-events.csv"},
        )

    @app.get("/api/v1/projects")
    def projects(actor: ApiActor) -> dict[str, Any]:
        return service.list_projects(actor)

    @app.get("/api/v1/actors")
    def actors(actor: ApiActor) -> dict[str, Any]:
        return service.list_actors(actor)

    @app.get("/api/v1/config/effective")
    def effective_config(actor: ApiActor) -> dict[str, Any]:
        return service.effective_config(actor)

    @app.get("/api/v1/doctor")
    def doctor(actor: ApiActor) -> dict[str, Any]:
        return service.doctor(actor)

    # ---- REST mutation routes --------------------------------------------------

    @app.post("/api/v1/requests")
    def create_request(
        request_data: RequestCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_request(actor, request_data, idempotency_key=_idempotency_key(idempotency_key))

    @app.post("/api/v1/requests/{request_id}/cancel")
    def cancel_request(
        request_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.cancel_request(actor, request_id, idempotency_key=_idempotency_key(idempotency_key))

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

    @app.post("/api/v1/reservations")
    def create_reservation(
        reservation: ReservationCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_reservation(
            actor, reservation, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/reservations/{reservation_id}/cancel")
    def cancel_reservation(
        reservation_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.cancel_reservation(
            actor, reservation_id, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/maintenance")
    def create_maintenance(
        maintenance_data: MaintenanceCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_maintenance(
            actor, maintenance_data, idempotency_key=_idempotency_key(idempotency_key)
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

    @app.post("/api/v1/projects")
    def upsert_project(
        project_data: ProjectUpsert,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.upsert_project(
            actor, project_data, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/endpoints")
    def upsert_endpoint(
        endpoint_data: EndpointUpsert,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.upsert_endpoint(
            actor,
            endpoint_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/endpoints/{endpoint_id}/enabled")
    def set_endpoint_enabled(
        endpoint_id: str,
        state: EndpointEnabled,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.set_endpoint_enabled(
            actor,
            endpoint_id,
            state,
            idempotency_key=_idempotency_key(idempotency_key),
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

    @app.post("/api/v1/internal/observations")
    def ingest_observation(observation: EndpointObservation, actor: ApiActor) -> dict[str, Any]:
        if actor.role not in {"collector", "admin"}:
            raise BrokerError("collector_role_required", "only collector/admin can submit telemetry", status_code=403)
        return service.ingest_observation(observation)

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
        }

    def ui_reference_data(actor: ActorContext) -> dict[str, Any]:
        """Shared select options for server-rendered human forms.

        The GUI deliberately gets these values through the same filtered read
        models as REST/MCP.  A new form therefore cannot accidentally expose a
        project, endpoint, or GPU the current actor is not allowed to use.
        """
        snapshot = service.snapshot(actor)["data"]
        return {
            "projects": service.list_projects(actor)["data"],
            "endpoints": snapshot["endpoints"],
            "gpus": snapshot["gpus"],
        }

    def page_payload(page: str, actor: ActorContext) -> Any:
        if page == "overview":
            overview = service.snapshot(actor)
            return {
                **overview["data"],
                "snapshot_revision": overview["snapshot_revision"],
                "server_time": overview["server_time"],
                "projects": service.list_projects(actor)["data"],
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
        if page == "projects":
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
        if action == "request":
            duration_hours = _form_int(form, "duration_hours", required=True, minimum=1, maximum=720)
            min_free_gib = _form_int(form, "min_free_vram_gib", minimum=0)
            endpoint_id = _form_value(form, "endpoint_id")
            gpu_ids = _form_list(form, "gpu_ids")
            return {
                "project_id": _form_value(form, "project_id", required=True),
                "task_ref": _form_value(form, "task_ref", required=True),
                "purpose": _form_value(form, "purpose", required=True),
                "gpu_count": len(gpu_ids) or _form_int(form, "gpu_count", required=True, minimum=1),
                "duration_seconds": duration_hours * 3600 if duration_hours is not None else None,
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
        if action == "project":
            return {
                "id": _form_value(form, "id", required=True),
                "display_name": _form_value(form, "display_name", required=True),
                "weight": _form_int(form, "weight", required=True, minimum=1),
                "quota_gpus": _form_int(form, "quota_gpus", minimum=1),
                "concurrency_limit": _form_int(form, "concurrency_limit", minimum=1),
                "enabled": _form_boolean(form, "enabled"),
            }
        if action == "actor":
            return {
                "id": _form_value(form, "id", required=True),
                "display_name": _form_value(form, "display_name", required=True),
                "role": _form_value(form, "role", required=True),
                "project_ids": _form_list(form, "project_ids"),
                "token_label": _form_value(form, "token_label", required=True),
            }
        if action == "endpoint":
            host = _form_value(form, "host", required=True)
            port = _form_int(form, "port", required=True, minimum=1, maximum=65535)
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
                "project_ids": _form_list(form, "project_ids")
                or [project.id for project in inventory.projects],
                "enabled": _form_boolean(form, "enabled"),
            }
        if action == "endpoint-enabled":
            return {
                "endpoint_id": _form_value(form, "endpoint_id", required=True),
                "enabled": _form_boolean(form, "enabled"),
            }
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
            "cancel-request": "/ui/requests",
            "activate-lease": "/ui/leases",
            "renew-lease": "/ui/leases",
            "release-lease": "/ui/leases",
            "bind-workload": "/ui/leases",
            "reservation": "/ui/reservations",
            "cancel-reservation": "/ui/reservations",
            "maintenance": "/ui/maintenance",
            "ack-alert": "/ui/alerts",
            "project": "/ui/projects",
            "actor": "/ui/projects",
            "revoke-token": "/ui/projects",
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
            if action == "request":
                result = service.create_request(actor, parse_ui_request(data), idempotency_key=key)
            elif action == "endpoint":
                result = service.upsert_endpoint(
                    actor,
                    EndpointUpsert.model_validate(data),
                    idempotency_key=key,
                )
            elif action == "endpoint-enabled":
                result = service.set_endpoint_enabled(
                    actor,
                    data["endpoint_id"],
                    EndpointEnabled.model_validate({"enabled": data["enabled"]}),
                    idempotency_key=key,
                )
            elif action == "cancel-request":
                result = service.cancel_request(actor, data["request_id"], idempotency_key=key)
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
            elif action == "reservation":
                result = service.create_reservation(
                    actor, ReservationCreate.model_validate(data), idempotency_key=key
                )
            elif action == "cancel-reservation":
                result = service.cancel_reservation(actor, data["reservation_id"], idempotency_key=key)
            elif action == "maintenance":
                result = service.create_maintenance(
                    actor, MaintenanceCreate.model_validate(data), idempotency_key=key
                )
            elif action == "ack-alert":
                result = service.acknowledge_alert(
                    actor,
                    data["alert_id"],
                    AlertAcknowledge.model_validate({"note": data.get("note")}),
                    idempotency_key=key,
                )
            elif action == "project":
                result = service.upsert_project(actor, ProjectUpsert.model_validate(data), idempotency_key=key)
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

        if action == "request":
            message = "资源请求已获分配，租约等待激活" if result.get("lease") else "资源请求已进入队列"
        else:
            message = f"操作完成（事件 {result.get('event_id', 'no-event')}）"
        notice = quote(message)
        return RedirectResponse(url=f"{route}?notice={notice}", status_code=303)

    return app


def _find_project_root() -> Path:
    """Find the Alembic source tree for source installs and explicit local pilots.

    Production package deployments should set `Settings.project_root` (or
    `GPU_BROKER_PROJECT_ROOT`) to the reviewed release directory.
    """

    configured = os.environ.get("GPU_BROKER_PROJECT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (candidate / "migrations").is_dir():
            return candidate
    raise RuntimeError(
        "cannot locate Alembic migrations; set GPU_BROKER_PROJECT_ROOT to the reviewed gpu-broker release root"
    )
