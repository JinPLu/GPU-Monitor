"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP

from gpu_broker.client import BrokerClient
from gpu_broker.daemon import ensure_broker_ready_for_mcp


MCP_INSTRUCTIONS = (
    "Use gpu-broker for GPU coordination and read-only resource discovery. The Broker is the only "
    "allocation and freshness/probing authority: do not bypass it through SSH, SQLite, inventory, remote "
    "probes, or nvidia-smi. A request or accepted plan to run, continue, or monitor a GPU task makes its "
    "owner-scoped coordination routine once a profile_id, or project_id, gpu_count, and needed thresholds "
    "are recorded. Reuse that contract without asking again; never infer missing inputs from a repository, "
    "task title, free capacity, or defaults. "
    "The normal bare-metal path is gpu_coordination, then gpu_claim_profile or gpu_claim, then execute, "
    "then gpu_release. A queued or null lease is not permission to run. When a held or active lease is "
    "returned, take the placement only from its structured lease.resources[]: each resource provides an "
    "endpoint, gpus, cuda_visible_devices, and commitment. Use the project's normal execution path to start "
    "or stop its workload there. The Broker does not launch or stop that workload. "
    "After it starts, gpu_bind_observed_workload records fresh observed processes; release when it stops "
    "or startup fails. Use the full requested allocation when the workload safely supports it. "
    "Provide an idempotency_key for a mutation when retrying it, and reuse that same caller-chosen key. "
    "The low-level request, activate, release-lease, and bind-workload tools remain compatibility tools; "
    "prefer the normal path. "
    "Endpoints belong to projects. Their owner may add them and retire them into draining; retirement "
    "prevents new placement and never stops an existing workload. "
    "External Slurm clusters such as Hanhai22 are SchedulerTargets, never raw SSH endpoints. Use "
    "gpu_scheduler_targets and gpu_scheduler_access_status to discover a target, then use owner-scoped "
    "submit, status, and cancel operations. "
    "A Slurm PENDING job is not a bare-metal lease; scheduler status and AllocTRES establish its allocation. "
    "VPN access is detection only: access_required means ask the user to connect the approved VPN, then retry. "
    "On macOS this adapter automatically ensures the shared headless loopback daemon before REST calls; "
    "it does not depend on the GUI. If MCP or the service is unavailable, report that state and do not fall "
    "back to a bypass."
)


mcp = FastMCP(
    "gpu-broker",
    json_response=True,
    instructions=MCP_INSTRUCTIONS,
)


def _client(actor_name: str | None = None) -> BrokerClient:
    ensure_broker_ready_for_mcp()
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

    return _client().get("/api/v1/coordination")


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
    return _client().get("/api/v1/workload-profiles", params=params)


@mcp.tool()
def gpu_scheduler_targets() -> dict[str, Any]:
    """List globally registered external schedulers such as Hanhai22.

    Scheduler targets are not raw GPU servers. Their login helpers and access
    hints are metadata; Slurm remains the resource allocator.
    """

    return _client().get("/api/v1/scheduler-targets")


@mcp.tool()
def gpu_scheduler_access_status(target_id: str) -> dict[str, Any]:
    """Check whether an external scheduler is reachable through its approved access path.

    This read-only check does not connect or change VPN state and does not submit a job.
    """

    return _client().get(f"/api/v1/scheduler-targets/{target_id}/access")


@mcp.tool()
def gpu_scheduler_profiles(project_id: str) -> dict[str, Any]:
    """List enabled Slurm profiles explicitly granted to a project."""

    result = _client().get(
        "/api/v1/workload-profiles",
        params={"project_id": project_id},
    )
    result["data"] = [
        profile
        for profile in result.get("data", [])
        if profile.get("runtime_kind") == "slurm"
    ]
    return result


@mcp.tool()
def gpu_scheduler_submit_profile(
    agent_name: str,
    profile_id: str,
    project_id: str,
    task: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Submit a project-owned Slurm profile for its current task."""

    if not profile_id.strip() or not project_id.strip() or not task.strip():
        raise ValueError("profile_id, project_id and task must not be empty")
    return _client(agent_name).post(
        f"/api/v1/workload-profiles/{profile_id}/scheduler-submit",
        {"project_id": project_id, "task_ref": task},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_scheduler_submit_once(
    agent_name: str,
    request: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Submit one project-owned Slurm script and bounded resource request.

    The request must include target_id, project_id, task_ref, purpose,
    duration_seconds, constraints, scheduler, and script_body.
    Broker stores the script digest by default; retain_submission_body must be
    explicitly true to retain the exact body.
    """

    required = {
        "target_id",
        "project_id",
        "task_ref",
        "purpose",
        "duration_seconds",
        "constraints",
        "scheduler",
        "script_body",
    }
    missing = sorted(field for field in required if not request.get(field))
    if missing:
        raise ValueError(
            "gpu_scheduler_submit_once requires " + ", ".join(missing)
        )
    return _client(agent_name).post(
        "/api/v1/scheduler-jobs",
        request,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_scheduler_job_status(
    agent_name: str,
    job_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Read one live Slurm job or list the Broker's persisted scheduler jobs."""

    client = _client(agent_name)
    if job_id:
        return client.get(f"/api/v1/scheduler-jobs/{job_id}")
    params = {"project_id": project_id} if project_id else None
    return client.get("/api/v1/scheduler-jobs", params=params)


@mcp.tool()
def gpu_scheduler_cancel(
    agent_name: str,
    job_id: str,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Cancel a Slurm job owned by the calling project."""

    if not job_id.strip() or not reason.strip():
        raise ValueError("job_id and reason must not be empty")
    return _client(agent_name).post(
        f"/api/v1/scheduler-jobs/{job_id}/cancel",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_scheduler_upload(
    agent_name: str,
    target_id: str,
    project_id: str,
    local_path: str,
    remote_directory: str,
    approval_ref: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Compatibility helper for the deferred staged-upload API.

    It is intentionally not exposed as an MCP tool. Keep it import-compatible
    while the public scheduler contract does not offer transfer operations.
    """

    if not all(
        value.strip()
        for value in (
            agent_name,
            target_id,
            project_id,
            local_path,
            remote_directory,
            approval_ref,
        )
    ):
        raise ValueError("all staged upload fields must not be empty")
    return _client(agent_name).post(
        "/api/v1/scheduler-transfers",
        {
            "target_id": target_id,
            "project_id": project_id,
            "local_path": local_path,
            "remote_directory": remote_directory,
            "approval_ref": approval_ref,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_scheduler_transfer_status(
    agent_name: str,
    transfer_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility helper for the deferred staged-upload API."""

    client = _client(agent_name)
    if transfer_id:
        return client.get(f"/api/v1/scheduler-transfers/{transfer_id}")
    params = {"project_id": project_id} if project_id else None
    return client.get("/api/v1/scheduler-transfers", params=params)


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
    agent_name: str,
    lease_id: str,
    run_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record fresh observed processes for an already-started workload on the caller's lease.

    The broker reads only its latest collector observations on the lease's GPUs;
    it neither launches nor changes the remote workload. `run_id` is optional:
    without it, the broker uses a stable identifier derived from the lease.
    """

    return _client(agent_name).post(
        f"/api/v1/leases/{lease_id}/bind-observed-workload",
        {"run_id": run_id} if run_id else {},
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
    gpu_count: int,
    server_id: str | None = None,
    gpu_ids: list[str] | None = None,
    min_available_cpu_cores: float | None = None,
    min_available_memory_mib: int | None = None,
    min_free_vram_mib: int | None = None,
    min_total_vram_mib: int | None = None,
    purpose: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Claim GPUs now, or queue. CPU, memory, and VRAM thresholds are absolute values."""

    task = task.strip()
    if gpu_count < 1 or not task:
        raise ValueError("task must not be empty and gpu_count must be positive")
    if min_available_cpu_cores is not None and min_available_cpu_cores < 0:
        raise ValueError("min_available_cpu_cores must be non-negative")
    if min_available_memory_mib is not None and min_available_memory_mib < 0:
        raise ValueError("min_available_memory_mib must be non-negative")
    if min_free_vram_mib is not None and min_free_vram_mib < 0:
        raise ValueError("min_free_vram_mib must be non-negative")
    if min_total_vram_mib is not None and min_total_vram_mib < 1:
        raise ValueError("min_total_vram_mib must be positive")
    exact_gpu_ids = gpu_ids or []
    constraints = {
        "gpu_count": len(exact_gpu_ids) or gpu_count,
        "placement": "exact" if exact_gpu_ids else "pack",
        "endpoint_ids": [server_id] if server_id else [],
        "gpu_ids": exact_gpu_ids,
    }
    if min_available_cpu_cores is not None:
        constraints["min_available_cpu_cores"] = min_available_cpu_cores
    if min_available_memory_mib is not None:
        constraints["min_available_memory_mib"] = min_available_memory_mib
    if min_free_vram_mib is not None:
        constraints["min_free_vram_mib"] = min_free_vram_mib
    if min_total_vram_mib is not None:
        constraints["min_total_vram_mib"] = min_total_vram_mib
    return _client(agent_name).post(
        "/api/v1/claims",
        {
            "project_id": project_id,
            "task_ref": task,
            "purpose": (purpose or task).strip(),
            "constraints": constraints,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_claim_profile(
    agent_name: str,
    profile_id: str,
    task: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Claim a workload profile now; the profile fixes its resource contract."""

    if not profile_id.strip() or not task.strip():
        raise ValueError("profile_id and task must not be empty")
    return _client(agent_name).post(
        f"/api/v1/workload-profiles/{profile_id}/claim",
        {"task_ref": task},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_release(
    agent_name: str,
    lease_id: str,
    reason: str = "workload_completed",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Release a prior claim; this never stops a process on the remote server."""

    return _client(agent_name).post(
        f"/api/v1/leases/{lease_id}/release",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_schedule(
    agent_name: str,
    project_id: str,
    gpu_ids: list[str],
    start_at: str,
    end_at: str,
    reason: str,
    idempotency_key: str | None = None,
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
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_add_server(
    agent_name: str,
    project_id: str,
    host: str,
    port: int = 22,
    ssh_user: str = "root",
    server_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Add a project-owned endpoint to read-only monitoring."""

    if not project_id.strip():
        raise ValueError("project_id must not be empty")
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
            "owner_project_id": project_id,
            "enabled": True,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_delete_server(
    agent_name: str,
    server_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Retire an endpoint into draining; this never stops a remote workload."""

    return _client(agent_name).delete(
        f"/api/v1/endpoints/{server_id}",
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
