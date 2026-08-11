"""Human tables and Agent JSON CLI, all operational commands routed through REST."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
import yaml

from serverpilot.api import create_app
from serverpilot.client import BrokerClient, BrokerClientError
from serverpilot.collector import SSHCollector
from serverpilot.config import ProjectConfig, Settings, load_inventory
from serverpilot.daemon import (
    DaemonError,
    MacOSDaemonManager,
    daemon_instance_id_for_paths,
    format_status,
)
from serverpilot.database import Database
from serverpilot.importer import import_servers_files, write_inventory
from serverpilot.schemas import (
    RequestCreate,
    RequestCreateFlat,
    ResourceClaim,
    ResourcePlanEvaluationInput,
    ResourceRunActualInput,
)
from serverpilot.service import BrokerService


app = typer.Typer(
    no_args_is_help=True,
    help="Single-user GPU/CPU coordination across projects and agents.",
)
endpoint_app = typer.Typer(no_args_is_help=True)
gpu_app = typer.Typer(no_args_is_help=True)
request_app = typer.Typer(no_args_is_help=True)
lease_app = typer.Typer(
    no_args_is_help=True,
    help="Update cooperative lease state; never start or stop workloads.",
)
reservation_app = typer.Typer(no_args_is_help=True)
resource_app = typer.Typer(
    no_args_is_help=True,
    help="Cross-project, cross-agent CPU/memory/GPU/scheduler resource contracts.",
)
collect_app = typer.Typer(no_args_is_help=True)
daemon_app = typer.Typer(
    no_args_is_help=True,
    help="Manage the macOS user daemon; data is preserved when the daemon stops.",
)
app.add_typer(endpoint_app, name="endpoint")
app.add_typer(gpu_app, name="gpu")
app.add_typer(lease_app, name="lease")
app.add_typer(resource_app, name="resource")
app.add_typer(collect_app, name="collect")
app.add_typer(daemon_app, name="daemon")


def _database_url(value: str) -> str:
    if value.startswith("sqlite:///"):
        return value
    return f"sqlite:///{Path(value).expanduser().resolve()}"


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    data = value.get("data", value) if isinstance(value, dict) else value
    if isinstance(data, list):
        if not data:
            typer.echo("(empty)")
            return
        if all(isinstance(item, dict) for item in data):
            keys = list(dict.fromkeys(key for item in data for key in item.keys()))
            keys = [key for key in keys if not isinstance(data[0].get(key), (dict, list))][:8]
            widths = {key: min(36, max(len(key), *(len(str(item.get(key, ""))) for item in data))) for key in keys}
            typer.echo("  ".join(key.ljust(widths[key]) for key in keys))
            typer.echo("  ".join("-" * widths[key] for key in keys))
            for item in data:
                typer.echo("  ".join(str(item.get(key, ""))[: widths[key]].ljust(widths[key]) for key in keys))
            return
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _client(url: str | None, actor: str | None) -> BrokerClient:
    try:
        return BrokerClient.from_env(url=url, actor=actor)
    except BrokerClientError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _call(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except BrokerClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command("init")
def init(
    db: Annotated[str, typer.Option("--db", help="SQLite path or sqlite:/// URL")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
) -> None:
    """Create or migrate local state. No application key is required."""

    service = BrokerService(Database(_database_url(db), Path.cwd()), load_inventory(inventory))
    service.initialize()
    typer.echo(f"initialized {db}")


@app.command("backup")
def backup(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    output: Annotated[Path, typer.Option("--output")] = Path("state/backups/serverpilot.sqlite3"),
) -> None:
    """Create a local SQLite backup after a WAL checkpoint; no remote resource is touched."""

    database = Database(_database_url(db), Path.cwd())
    typer.echo(str(database.backup(output)))


@app.command("restore")
def restore(
    source: Annotated[Path, typer.Option("--from", exists=True, readable=True)],
    target: Annotated[Path, typer.Option("--to")],
) -> None:
    """Validate and copy a backup to a new target; never overwrite a live DB."""

    typer.echo(str(Database.restore_to(source, target)))


@app.command("serve")
def serve(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    daemon_instance_id: Annotated[str | None, typer.Option("--daemon-instance-id")] = None,
) -> None:
    """Run the loopback-only FastAPI server; remote deployment requires separate approval."""

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("non-loopback bind requires an approved production deployment")
    if daemon_instance_id is not None:
        if db.startswith("sqlite:///"):
            database_path = Path(db.removeprefix("sqlite:///")).expanduser().resolve()
        elif "://" in db:
            raise typer.BadParameter(
                "--daemon-instance-id requires a local SQLite database path"
            )
        else:
            database_path = Path(db).expanduser().resolve()
        expected_instance_id = daemon_instance_id_for_paths(
            database_path,
            inventory.expanduser().resolve(),
        )
        if daemon_instance_id != expected_instance_id:
            raise typer.BadParameter(
                "--daemon-instance-id does not match --db and --inventory"
            )
        daemon_instance_id = expected_instance_id
    settings = Settings(
        database_url=_database_url(db),
        inventory_path=inventory,
        project_root=Path.cwd(),
        bind_host=host,
        bind_port=port,
        daemon_instance_id=daemon_instance_id,
    )
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
        http="h11",
        ws="none",
    )


def _daemon_call(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except DaemonError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@daemon_app.command("install")
def daemon_install(
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            help="Existing serverpilot project whose inventory/state should be migrated once.",
        ),
    ] = None,
    start: Annotated[bool, typer.Option("--start/--no-start")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Install the macOS user LaunchAgent and preserve/migrate existing local state."""

    result = _daemon_call(
        lambda: MacOSDaemonManager().install(source_root=source_root, start=start)
    )
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("ensure")
def daemon_ensure(
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            help="Existing serverpilot project to migrate when no daemon data exists yet.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ensure the macOS user daemon is installed, running, and ready."""

    result = _daemon_call(lambda: MacOSDaemonManager().ensure(source_root=source_root))
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("status")
def daemon_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report daemon installation, launchd, health, and canonical data paths."""

    result = _daemon_call(lambda: MacOSDaemonManager().status())
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("start")
def daemon_start() -> None:
    """Start the installed macOS user daemon."""

    _daemon_call(lambda: MacOSDaemonManager().start())
    typer.echo("started")


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the macOS user daemon without deleting state or its installation."""

    _daemon_call(lambda: MacOSDaemonManager().stop())
    typer.echo("stopped")


@daemon_app.command("uninstall")
def daemon_uninstall(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove the LaunchAgent while preserving inventory, database, and logs."""

    result = _daemon_call(lambda: MacOSDaemonManager().uninstall())
    typer.echo(format_status(result, as_json=as_json))


@app.command("status")
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).snapshot()), as_json)


@app.command("state")
def state(
    minimum_snapshot_revision: Annotated[
        int | None,
        typer.Option("--minimum-snapshot-revision", min=0, help="Wait for at least this revision."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0, max=300),
    ] = 0,
    poll_interval_seconds: Annotated[
        float,
        typer.Option("--poll-interval-seconds", min=0.05, max=10),
    ] = 0.25,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON")] = True,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    """Return the canonical control-plane state envelope."""

    _print(
        _call(
            lambda: _client(url, actor).control_plane_state(
                minimum_snapshot_revision=minimum_snapshot_revision,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        ),
        as_json,
    )


@endpoint_app.command("list")
def endpoint_list(
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).endpoints()), as_json)


@endpoint_app.command("delete")
def endpoint_delete(
    endpoint_id: str,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    """Deprecated compatibility alias for pause; it never retires a server."""

    _print(
        _call(
            lambda: _client(url, actor).delete(
                f"/api/v1/endpoints/{endpoint_id}",
                idempotency_key=secrets.token_hex(16),
            )
        ),
        as_json,
    )


@endpoint_app.command("pause")
def endpoint_pause(
    endpoint_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    """Block new placement while retaining collection and current leases."""

    _print(
        _call(
            lambda: _client(url, actor).post(
                f"/api/v1/endpoints/{endpoint_id}/pause", {}, idempotency_key=secrets.token_hex(16)
            )
        ),
        as_json,
    )


@endpoint_app.command("resume")
def endpoint_resume(
    endpoint_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    """Resume a draining endpoint."""

    _print(
        _call(
            lambda: _client(url, actor).post(
                f"/api/v1/endpoints/{endpoint_id}/resume", {}, idempotency_key=secrets.token_hex(16)
            )
        ),
        as_json,
    )


@endpoint_app.command("retire")
def endpoint_retire(
    endpoint_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    """Retire a drained endpoint after active leases and pinned queues have cleared."""

    _print(
        _call(
            lambda: _client(url, actor).post(
                f"/api/v1/endpoints/{endpoint_id}/retire", {}, idempotency_key=secrets.token_hex(16)
            )
        ),
        as_json,
    )


@gpu_app.command("list")
def gpu_list(
    state: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).gpus(state=state)), as_json)


@app.command("who")
def who(
    project: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).leases(project_id=project))
    _print(response, as_json)


def _request_from_file(path: Path) -> RequestCreate:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("request YAML must be a mapping")
    return RequestCreate.model_validate(raw) if "constraints" in raw else RequestCreateFlat.model_validate(raw).canonical()


def _mapping_from_file(path: Path, label: str) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter(f"{label} YAML must be a mapping")
    return raw


@request_app.command("create")
def request_create(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    request_data = _request_from_file(file)
    response = _call(
        lambda: _client(url, actor).post(
            "/api/v1/requests", request_data.model_dump(mode="json"), idempotency_key=secrets.token_hex(16)
        )
    )
    _print(response, as_json)


@request_app.command("queue")
def request_queue(
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).requests(queued_only=True))
    _print(response, as_json)


@request_app.command("cancel")
def request_cancel(
    request_id: str,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/requests/{request_id}/cancel", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("activate")
def lease_activate(lease_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/activate", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("renew")
def lease_renew(lease_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/renew", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("release")
def lease_release(lease_id: str, reason: Annotated[str, typer.Option("--reason")], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/release", {"reason": reason}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("bind")
def lease_bind(lease_id: str, run_id: Annotated[str, typer.Option("--run-id")], process_key: Annotated[list[str], typer.Option("--process-key")]=[], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/bind-workload", {"run_id": run_id, "process_keys": process_key}, idempotency_key=secrets.token_hex(16))), as_json)


@reservation_app.command("list")
def reservation_list(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).reservations()), as_json)


@reservation_app.command("create")
def reservation_create(file: Annotated[Path, typer.Option("--file", exists=True)], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("reservation YAML must be a mapping")
    _print(_call(lambda: _client(url, actor).post("/api/v1/reservations", raw, idempotency_key=secrets.token_hex(16))), as_json)


@reservation_app.command("cancel")
def reservation_cancel(reservation_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/reservations/{reservation_id}/cancel", {}, idempotency_key=secrets.token_hex(16))), as_json)


@resource_app.command("providers")
def resource_providers(
    provider_type: Annotated[str | None, typer.Option("--provider-type")] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(
        _call(
            lambda: _client(url, actor).resource_providers(
                provider_type=provider_type,
                enabled=enabled,
            )
        ),
        as_json,
    )


@resource_app.command("monitor")
def resource_monitor(
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(_call(lambda: _client(url, actor).resource_monitor(project_id=project_id)), as_json)


@resource_app.command("claims")
def resource_claims(
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(
        _call(lambda: _client(url, actor).resource_claims(project_id=project_id, state=state)),
        as_json,
    )


@resource_app.command("evaluations")
def resource_evaluations(
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(_call(lambda: _client(url, actor).resource_plan_evaluations(project_id=project_id)), as_json)


@resource_app.command("actuals")
def resource_actuals(
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    task_ref: Annotated[str | None, typer.Option("--task-ref")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(
        _call(lambda: _client(url, actor).resource_run_actuals(project_id=project_id, task_ref=task_ref)),
        as_json,
    )


@resource_app.command("evaluate")
def resource_evaluate(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    evaluation = ResourcePlanEvaluationInput.model_validate(
        _mapping_from_file(file, "resource plan evaluation")
    )
    _print(
        _call(
            lambda: _client(url, actor).evaluate_resource_plan(
                evaluation.model_dump(mode="json"),
                idempotency_key=secrets.token_hex(16),
            )
        ),
        as_json,
    )


@resource_app.command("claim")
def resource_claim(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    claim = ResourceClaim.model_validate(_mapping_from_file(file, "resource claim"))
    _print(
        _call(
            lambda: _client(url, actor).claim_resource(
                claim.model_dump(mode="json"),
                idempotency_key=secrets.token_hex(16),
            )
        ),
        as_json,
    )


@resource_app.command("release")
def resource_release(
    claim_id: str,
    reason: Annotated[str, typer.Option("--reason")] = "workload_completed",
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    _print(
        _call(
            lambda: _client(url, actor).release_resource_claim(
                claim_id,
                reason=reason,
                idempotency_key=secrets.token_hex(16),
            )
        ),
        as_json,
    )


@resource_app.command("record-actual")
def resource_record_actual(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    claim_id: Annotated[str | None, typer.Option("--claim-id")] = None,
    evaluation_id: Annotated[str | None, typer.Option("--evaluation-id")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    actual = ResourceRunActualInput.model_validate(_mapping_from_file(file, "resource run actual"))
    _print(
        _call(
            lambda: _client(url, actor).record_resource_run_actual(
                actual.model_dump(mode="json"),
                claim_id=claim_id,
                evaluation_id=evaluation_id,
                idempotency_key=secrets.token_hex(16),
            )
        ),
        as_json,
    )


@app.command("history")
def history(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/events")), as_json)


@app.command("doctor")
def doctor(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/doctor")), as_json)


@collect_app.command("once")
def collect_once(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
) -> None:
    """Explicitly run fixed, read-only telemetry probes; this command never launches/terminates work."""

    config = load_inventory(inventory)
    service = BrokerService(Database(_database_url(db), Path.cwd()), config)
    service.initialize()
    typer.echo(json.dumps(asyncio.run(SSHCollector(config).collect_once(service)), ensure_ascii=False, indent=2))


@app.command("import-servers")
def import_servers(
    paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    project: Annotated[list[str], typer.Option("--project", help="Project id; repeat for multiple projects.")],
    output: Annotated[Path, typer.Option("--output")] = Path("configs/inventory.yaml"),
) -> None:
    """Parse legacy files, deduplicate only exact host:port, and emit a new global config/report."""

    projects = [ProjectConfig(id=item, display_name=item.replace("-", " ").title()) for item in project]
    report = import_servers_files(
        paths,
        project_ids=project,
    )
    write_inventory(output, report, projects=projects)
    typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
