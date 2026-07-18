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

from gpu_broker.api import create_app
from gpu_broker.client import BrokerClient, BrokerClientError
from gpu_broker.collector import SSHCollector
from gpu_broker.config import ProjectConfig, Settings, load_inventory
from gpu_broker.database import Database
from gpu_broker.importer import import_servers_files, write_inventory
from gpu_broker.schemas import RequestCreate, RequestCreateFlat
from gpu_broker.service import BrokerService


app = typer.Typer(no_args_is_help=True, help="Local shared-GPU status and coordination.")
endpoint_app = typer.Typer(no_args_is_help=True)
gpu_app = typer.Typer(no_args_is_help=True)
request_app = typer.Typer(no_args_is_help=True)
lease_app = typer.Typer(
    no_args_is_help=True,
    help="Update cooperative lease state; never start or stop workloads.",
)
reservation_app = typer.Typer(no_args_is_help=True)
collect_app = typer.Typer(no_args_is_help=True)
app.add_typer(endpoint_app, name="endpoint")
app.add_typer(gpu_app, name="gpu")
app.add_typer(request_app, name="request")
app.add_typer(lease_app, name="lease")
app.add_typer(reservation_app, name="reservation")
app.add_typer(collect_app, name="collect")


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
    db: Annotated[str, typer.Option("--db", help="SQLite path or sqlite:/// URL")] = "state/gpu-broker.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
) -> None:
    """Create or migrate local state. No application key is required."""

    service = BrokerService(Database(_database_url(db), Path.cwd()), load_inventory(inventory))
    service.initialize()
    typer.echo(f"initialized {db}")


@app.command("backup")
def backup(
    db: Annotated[str, typer.Option("--db")] = "state/gpu-broker.sqlite3",
    output: Annotated[Path, typer.Option("--output")] = Path("state/backups/gpu-broker.sqlite3"),
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
    db: Annotated[str, typer.Option("--db")] = "state/gpu-broker.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
) -> None:
    """Run the loopback-only FastAPI server; remote deployment requires separate approval."""

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("non-loopback bind requires an approved production deployment")
    settings = Settings(
        database_url=_database_url(db),
        inventory_path=inventory,
        project_root=Path.cwd(),
        bind_host=host,
        bind_port=port,
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


@app.command("status")
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/snapshot")), as_json)


@endpoint_app.command("list")
def endpoint_list(
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/endpoints")), as_json)


@gpu_app.command("list")
def gpu_list(
    state: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/gpus", params={"state": state} if state else None)), as_json)


@app.command("who")
def who(
    project: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).get("/api/v1/leases"))
    if project:
        response["data"] = [lease for lease in response["data"] if lease["project_id"] == project]
    _print(response, as_json)


def _request_from_file(path: Path) -> RequestCreate:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("request YAML must be a mapping")
    return RequestCreate.model_validate(raw) if "constraints" in raw else RequestCreateFlat.model_validate(raw).canonical()


@request_app.command("create")
def request_create(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
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
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).get("/api/v1/requests"))
    response["data"] = [item for item in response["data"] if item["state"] in {"QUEUED", "PENDING_APPROVAL"}]
    _print(response, as_json)


@request_app.command("cancel")
def request_cancel(
    request_id: str,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/requests/{request_id}/cancel", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("activate")
def lease_activate(lease_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/activate", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("renew")
def lease_renew(lease_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/renew", {}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("release")
def lease_release(lease_id: str, reason: Annotated[str, typer.Option("--reason")], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/release", {"reason": reason}, idempotency_key=secrets.token_hex(16))), as_json)


@lease_app.command("bind")
def lease_bind(lease_id: str, run_id: Annotated[str, typer.Option("--run-id")], process_key: Annotated[list[str], typer.Option("--process-key")]=[], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/bind-workload", {"run_id": run_id, "process_keys": process_key}, idempotency_key=secrets.token_hex(16))), as_json)


@reservation_app.command("list")
def reservation_list(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/reservations")), as_json)


@reservation_app.command("create")
def reservation_create(file: Annotated[Path, typer.Option("--file", exists=True)], as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("reservation YAML must be a mapping")
    _print(_call(lambda: _client(url, actor).post("/api/v1/reservations", raw, idempotency_key=secrets.token_hex(16))), as_json)


@reservation_app.command("cancel")
def reservation_cancel(reservation_id: str, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/reservations/{reservation_id}/cancel", {}, idempotency_key=secrets.token_hex(16))), as_json)


@app.command("history")
def history(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/events")), as_json)


@app.command("doctor")
def doctor(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="GPU_BROKER_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="GPU_BROKER_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/doctor")), as_json)


@collect_app.command("once")
def collect_once(
    db: Annotated[str, typer.Option("--db")] = "state/gpu-broker.sqlite3",
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
