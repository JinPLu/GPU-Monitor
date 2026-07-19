"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP

from gpu_broker.client import BrokerClient, BrokerClientError


mcp = FastMCP(
    "gpu-broker",
    json_response=True,
    instructions=(
        "Use this MCP only when the user asks to inspect or coordinate GPU resources. When a user or "
        "approved task contract names an enabled workload profile, call gpu_claim_profile; do not select "
        "a profile from a repository, directory, task title, free capacity, or defaults. Otherwise, an "
        "explicitly authorized project task needs project_id, task, and gpu_count for gpu_claim; its task "
        "is recorded as the purpose. Add a server or exact gpu_ids only when the task explicitly requests "
        "that placement. Do not infer project_id or gpu_count from a repository, directory, task title, "
        "free capacity, or defaults. "
        "For cross-agent coordination, use gpu_coordination: it is the broker's shared, read-only board of "
        "server capacity, current lease owners, task visibility, observed process attribution, and queue pressure. "
        "Do not appoint or emulate a separate scheduler; omit server_id unless the task explicitly requires one and "
        "let the broker place routine claims. "
        "Call gpu_claim or gpu_claim_profile atomically: lease=null or a queued response is not permission to "
        "run. Use only GPUs in a returned held or active lease. For an existing lease, activate it before work "
        "when available and call gpu_release after the workload stops or failed startup with the same agent_name "
        "and lease_id where accepted; its default reason is workload_completed. Cancel only explicitly abandoned requests and renew only with "
        "explicit bounded authorization. If a claim is blocked only by project_endpoint_scope, do not infer a "
        "server or change access: use gpu_grant_server_project only with explicit authorization for the named "
        "project and existing server. After an authorized workload has started on the caller's returned lease, call "
        "gpu_bind_observed_workload with the same agent_name and lease_id (a stable run_id is optional) to record only "
        "the already-observed processes; this does not launch or stop anything and prevents false unmanaged-process "
        "conflicts. Reservations, server registration, and changing a server's project access require separate "
        "authorization. This MCP coordinates ownership only; it never authorizes, starts, stops, or preempts "
        "remote work. The loopback actor label is audit metadata, not authentication. If MCP or the service is "
        "unavailable, report that state and do not fall back to SSH, inventory, SQLite, or nvidia-smi."
    ),
)


def _client(actor_name: str | None = None) -> BrokerClient:
    return BrokerClient.from_env(actor=actor_name)


def _require_capability(client: BrokerClient, capability: str) -> None:
    health = client.get("/health/live")
    capabilities = health.get("capabilities")
    if not isinstance(capabilities, list) or capability not in capabilities:
        raise BrokerClientError(
            f"local GPU Broker service lacks '{capability}'; reinstall/update it and restart the loopback service"
        )


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
    """Return shared state, including per-server CPU load and available system memory for placement decisions."""

    params: dict[str, Any] = {"compact": compact, "only_available": only_available}
    if server_id:
        params["endpoint_id"] = server_id
    if state:
        params["state"] = state
    return _client().get("/api/v1/snapshot", params=params)


@mcp.tool()
def gpu_coordination() -> dict[str, Any]:
    """Return the shared broker coordination board for all visible agents and servers.

    The board identifies each lease owner and task, real process attribution,
    per-server capacity, observed GPU use, queued demand, and factual signals
    such as an idle lease or unbound compute process. It is read-only.
    """

    client = _client()
    _require_capability(client, "coordination_board")
    return client.get("/api/v1/coordination")


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
def gpu_list_profiles(project_id: str | None = None) -> dict[str, Any]:
    """List project-visible workload profiles approved for routine GPU claims."""

    params = {"project_id": project_id} if project_id else None
    client = _client()
    _require_capability(client, "workload_profiles")
    return client.get("/api/v1/workload-profiles", params=params)


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
def gpu_bind_observed_workload(
    agent_name: str, lease_id: str, run_id: str | None = None
) -> dict[str, Any]:
    """Record fresh observed processes for an already-started workload on the caller's lease.

    The broker reads only its latest collector observations on the lease's GPUs;
    it neither launches nor changes the remote workload. `run_id` is optional:
    without it, the broker uses a stable identifier derived from the lease.
    """

    client = _client(agent_name)
    _require_capability(client, "coordination_board")
    return client.post(
        f"/api/v1/leases/{lease_id}/bind-observed-workload",
        {"run_id": run_id} if run_id else {},
        idempotency_key=secrets.token_hex(16),
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
    gpu_count: int,
    server_id: str | None = None,
    gpu_ids: list[str] | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Claim GPUs now, or enter the shared queue when they are unavailable."""

    task = task.strip()
    if gpu_count < 1 or not task:
        raise ValueError("task must not be empty and gpu_count must be positive")
    client = _client(agent_name)
    _require_capability(client, "instant_claims")
    exact_gpu_ids = gpu_ids or []
    constraints = {
        "gpu_count": len(exact_gpu_ids) or gpu_count,
        "placement": "exact" if exact_gpu_ids else "pack",
        "endpoint_ids": [server_id] if server_id else [],
        "gpu_ids": exact_gpu_ids,
    }
    return client.post(
        "/api/v1/claims",
        {
            "project_id": project_id,
            "task_ref": task,
            "purpose": (purpose or task).strip(),
            "constraints": constraints,
        },
        idempotency_key=secrets.token_hex(16),
    )


@mcp.tool()
def gpu_claim_profile(agent_name: str, profile_id: str, task: str) -> dict[str, Any]:
    """Claim a human-approved workload profile now; the profile fixes its resource contract."""

    if not profile_id.strip() or not task.strip():
        raise ValueError("profile_id and task must not be empty")
    client = _client(agent_name)
    _require_capability(client, "workload_profiles")
    return client.post(
        f"/api/v1/workload-profiles/{profile_id}/claim",
        {"task_ref": task},
        idempotency_key=secrets.token_hex(16),
    )


@mcp.tool()
def gpu_release(agent_name: str, lease_id: str, reason: str = "workload_completed") -> dict[str, Any]:
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


@mcp.tool()
def gpu_grant_server_project(agent_name: str, project_id: str, server_id: str) -> dict[str, Any]:
    """Grant an explicitly authorized project access to an existing monitored server.

    This is additive: it never removes the server's current project grants. Any
    now-eligible queued request is allocated immediately by the scheduler.
    """

    client = _client(agent_name)
    _require_capability(client, "project_endpoint_grants")
    return client.post(
        f"/api/v1/endpoints/{server_id}/projects",
        {"project_id": project_id},
        idempotency_key=secrets.token_hex(16),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
