# Routes

## Runtime

- API host is FastAPI in `src/gpu_broker/api.py`.
- Static UI assets are mounted at `/static`.
- The macOS host uses `http://127.0.0.1:8787/` as the document URL.

## User-facing routes

| URL | Entry template | Shared layout | Purpose |
| --- | --- | --- | --- |
| `/` | `dashboard.html` | `base.html` | Resource overview / server and GPU status |
| `/ui/gpus` | `page.html` | `base.html` | GPU state list |
| `/ui/gpus/{gpu_id}` | `page.html` | `base.html` | GPU detail |
| `/ui/requests` | `page.html` | `base.html` | Claims and queue |
| `/ui/reservations` | `page.html` | `base.html` | Reservations |
| `/ui/identities` | `page.html` | `base.html` | Actors and profiles |
| `/ui/maintenance` | `page.html` | `base.html` | Maintenance windows |
| `/ui/alerts` | `page.html` | `base.html` | Alerts |
| `/ui/audit` | `page.html` | `base.html` | Audit history |
| `/ui/doctor` | `page.html` | `base.html` | Settings / Doctor |
| `/ui/login` | `login.html` | `base.html` | Local token login |

## Relevant server configuration and route code

### App and static mounting

```python
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

```

### Web page payload / routes / actions

```python
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
            min_free_gib = _form_int(form, "min_free_vram_gib", minimum=0)
            endpoint_id = _form_value(form, "endpoint_id")
            gpu_ids = _form_list(form, "gpu_ids")
            task_ref = _form_value(form, "task_ref", required=True)
            return {
                "project_id": _form_value(form, "project_id", required=True),
                "task_ref": task_ref,
                "purpose": task_ref if action == "quick-claim" else _form_value(form, "purpose", required=True),
                "gpu_count": len(gpu_ids) or _form_int(form, "gpu_count", required=True, minimum=1),
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
            assert duration_hours is not None and gpu_count is not None
            return {
                "id": _form_value(form, "id", required=True),
                "project_id": _form_value(form, "project_id", required=True),
                "display_name": _form_value(form, "display_name", required=True),
                "purpose": _form_value(form, "purpose", required=True),
                "duration_seconds": duration_hours * 3600,
                "constraints": {
                    "gpu_count": gpu_count,
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
                # Kept in the REST schema for old clients, but endpoint project
                # labels no longer affect placement and the GUI never asks for them.
                "project_ids": [],
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

```

### Token-created response branch

```python
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

        if action in {"quick-claim", "profile-claim"}:
            message = "GPU 已认领并登记为使用中；不会启动远端任务" if result.get("lease") else "资源请求已进入队列"
        elif action == "request":
            message = "资源请求已获分配，租约等待激活" if result.get("lease") else "资源请求已进入队列"
        else:
            message = f"操作完成（事件 {result.get('event_id', 'no-event')}）"
        notice = quote(message)
        return RedirectResponse(url=f"{route}?notice={notice}", status_code=303)

    return app


def _find_project_root() -> Path:
    """Find the source release root, falling back to packaged migrations."""

    configured = os.environ.get("GPU_BROKER_PROJECT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (
            candidate / "src" / "gpu_broker" / "migrations"
        ).is_dir():
            return candidate
    return Path(__file__).resolve().parent

```
