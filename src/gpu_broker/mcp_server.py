"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP

from gpu_broker.client import BrokerClient


mcp = FastMCP(
    "gpu-broker",
    json_response=True,
    instructions=(
        "For an already-authorized GPU workload, call gpu_claim with an explicit project_id, a "
        "readable agent_name, task, GPU count, and duration. Use only the GPUs in a returned lease; "
        "without a lease, do not run. After stopping the workload, call gpu_release with the same "
        "agent_name and lease id. If the tool or service is unavailable, do not infer availability "
        "from inventory, SSH, or nvidia-smi. A lease coordinates ownership only; it never authorizes, "
        "starts, stops, or preempts a workload."
    ),
)


def _client(actor_name: str | None = None) -> BrokerClient:
    return BrokerClient.from_env(actor=actor_name)


def _require_request_fields(request: dict[str, Any]) -> None:
    missing = [field for field in ("project_id", "task_ref", "purpose") if not request.get(field)]
    if missing:
        raise ValueError(f"gpu_request requires {', '.join(missing)}")


@mcp.tool()
def gpu_status(
    compact: bool = True,
    server_id: str | None = None,
    state: str | None = None,
    only_available: bool = False,
) -> dict[str, Any]:
    """Return shared state; compact mode is designed for quick Agent scheduling decisions."""

    params: dict[str, Any] = {"compact": compact, "only_available": only_available}
    if server_id:
        params["endpoint_id"] = server_id
    if state:
        params["state"] = state
    return _client().get("/api/v1/snapshot", params=params)


@mcp.tool()
def gpu_list(
    state: str | None = None,
    server_id: str | None = None,
    only_available: bool = False,
    compact: bool = True,
) -> dict[str, Any]:
    """List project-visible GPUs. Availability is derived by the control plane, not inferred by the agent."""

    params: dict[str, Any] = {"compact": compact, "only_available": only_available}
    if state:
        params["state"] = state
    if server_id:
        params["endpoint_id"] = server_id
    return _client().get("/api/v1/gpus", params=params)


@mcp.tool()
def gpu_who(project_id: str | None = None) -> dict[str, Any]:
    """List project-visible leases and workload bindings; returns no SSH or task secrets."""

    result = _client().get("/api/v1/leases")
    if project_id:
        result["data"] = [lease for lease in result["data"] if lease["project_id"] == project_id]
    return result


@mcp.tool()
def gpu_request(request: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    """Submit an atomic GPU request. Required: project_id, task_ref, purpose, and constraints."""

    _require_request_fields(request)
    return _client().post(
        "/api/v1/requests",
        request,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_request_status(request_id: str | None = None) -> dict[str, Any]:
    """List visible requests or return one request by id."""

    result = _client().get("/api/v1/requests")
    if request_id:
        result["data"] = [item for item in result["data"] if item["id"] == request_id]
    return result


@mcp.tool()
def gpu_cancel_request(request_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Cancel the caller's queued request. This does not stop a workload."""

    return _client().post(
        f"/api/v1/requests/{request_id}/cancel", {}, idempotency_key=idempotency_key or secrets.token_hex(16)
    )


@mcp.tool()
def gpu_activate_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Record that a held lease is active; it does not launch any command."""

    return _client().post(
        f"/api/v1/leases/{lease_id}/activate", {}, idempotency_key=idempotency_key or secrets.token_hex(16)
    )


@mcp.tool()
def gpu_renew_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Heartbeat/renew the caller's held or active lease."""

    return _client().post(
        f"/api/v1/leases/{lease_id}/renew", {}, idempotency_key=idempotency_key or secrets.token_hex(16)
    )


@mcp.tool()
def gpu_release_lease(lease_id: str, reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Release a lease cooperatively. Real observed compute processes remain fail-closed."""

    return _client().post(
        f"/api/v1/leases/{lease_id}/release",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_bind_workload(
    lease_id: str,
    run_id: str,
    process_keys: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Bind a lease to a sanitized run/process identity for later reconciliation."""

    return _client().post(
        f"/api/v1/leases/{lease_id}/bind-workload",
        {"run_id": run_id, "process_keys": process_keys or []},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_list_reservations() -> dict[str, Any]:
    """List visible future GPU reservations."""

    return _client().get("/api/v1/reservations")


@mcp.tool()
def gpu_history(after_id: int = 0) -> dict[str, Any]:
    """Read the append-only, redacted audit history for visible resources."""

    return _client().get("/api/v1/events", params={"after_id": after_id})


@mcp.tool()
def gpu_claim(
    agent_name: str,
    project_id: str,
    task: str,
    gpu_count: int = 1,
    hours: int = 4,
    server_id: str | None = None,
    gpu_ids: list[str] | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Claim GPUs now, or enter the shared queue when they are unavailable."""

    if gpu_count < 1 or hours < 1:
        raise ValueError("gpu_count and hours must be positive")
    client = _client(agent_name)
    exact_gpu_ids = gpu_ids or []
    constraints = {
        "gpu_count": len(exact_gpu_ids) or gpu_count,
        "placement": "exact" if exact_gpu_ids else "pack",
        "endpoint_ids": [server_id] if server_id else [],
        "gpu_ids": exact_gpu_ids,
    }
    return client.post(
        "/api/v1/requests",
        {
            "project_id": project_id,
            "task_ref": task,
            "purpose": purpose or task,
            "duration_seconds": hours * 3600,
            "constraints": constraints,
        },
        idempotency_key=secrets.token_hex(16),
    )


@mcp.tool()
def gpu_release(agent_name: str, lease_id: str, reason: str = "finished") -> dict[str, Any]:
    """Release a prior claim; this never stops a process on the remote server."""

    return _client(agent_name).post(
        f"/api/v1/leases/{lease_id}/release",
        {"reason": reason},
        idempotency_key=secrets.token_hex(16),
    )


@mcp.tool()
def gpu_schedule(
    agent_name: str,
    project_id: str,
    gpu_ids: list[str],
    start_at: str,
    end_at: str,
    reason: str,
) -> dict[str, Any]:
    """Reserve specific GPUs for a future ISO-8601 time window."""

    client = _client(agent_name)
    return client.post(
        "/api/v1/reservations",
        {
            "project_id": project_id,
            "gpu_ids": gpu_ids,
            "start_at": start_at,
            "end_at": end_at,
            "reason": reason,
        },
        idempotency_key=secrets.token_hex(16),
    )


@mcp.tool()
def gpu_add_server(
    agent_name: str,
    project_id: str,
    host: str,
    port: int = 22,
    ssh_user: str = "root",
    server_id: str | None = None,
) -> dict[str, Any]:
    """Add an SSH server to continuous read-only GPU monitoring."""

    client = _client(agent_name)
    generated_id = "server-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:96]
    generated_id = f"{generated_id}-p{port}"
    return client.post(
        "/api/v1/endpoints",
        {
            "id": server_id or generated_id,
            "host": host,
            "port": port,
            "ssh_user": ssh_user,
            "project_ids": [project_id],
            "enabled": True,
        },
        idempotency_key=secrets.token_hex(16),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
