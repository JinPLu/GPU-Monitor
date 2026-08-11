"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from serverpilot.client import BrokerClient, codex_coordination_identity
from serverpilot.daemon import ensure_broker_ready_for_mcp


PLACED_LEASE_STATES = {"HELD", "ACTIVE"}
TERMINAL_REQUEST_STATES = {"CANCELLED", "EXPIRED", "REJECTED", "RELEASED"}
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
RESOURCE_MARGINAL_MIN_SAVED_RATIO = 0.10
RESOURCE_MARGINAL_MIN_SAVED_SECONDS = 120

MCP_INSTRUCTIONS = """ServerPilot is the allocation and freshness authority. Do not bypass it through SSH, SQLite,
inventory, remote probes, or nvidia-smi.

ROUTE BY INTENT
- Routine GPU work: use the route below; do not construct a resource contract first.
- Routine GPU: call gpu_apply directly. It supports optionally selecting a server and requests one GPU by default.
  ServerPilot records routine project/task attribution and chooses the actual allocatable GPUs.
- Inspect or diagnose: use gpu_status only when needed; its default response is compact. The legacy gpu_list read is
  available only in the advanced profile.
- Start/stop: a successful apply returns a held or active lease; it fails with no_capacity and creates no queue. Use only
  lease.resources[], including cuda_visible_devices. After start, call gpu_bind_observed_workload (lease_id;
  run_id is optional). Call gpu_release when the workload stops or fails to start.
- Coordinate: use gpu_coordination when you need other agents or leases. A codex://threads/<uuid>
  coordination_uri is an opaque handoff reference, not a URL to open.
- External scheduler: set `SERVERPILOT_MCP_PROFILE=advanced`, then use scheduler tools. External Slurm clusters are
  SchedulerTargets, not SSH endpoints. A Slurm PENDING job is not a bare-metal lease; access_required means ask the
  user to connect the approved VPN.

BOUNDARIES
- ServerPilot never launches or stops workloads. Reuse the same idempotency_key when retrying a mutation.
- Generic-resource, low-level lease/workload, profile, endpoint, and scheduler-profile operations are advanced
  compatibility surfaces, not routine inputs. Endpoint administration and scheduler-job cancellation require
  current-task authorization, a non-empty approval_ref, and a stable idempotency_key. Keepalive policy is per idle GPU.
- On macOS this adapter ensures the shared loopback daemon before REST calls. If it or the service is unavailable,
  report that state; do not fall back to a bypass."""


mcp = FastMCP(
    "serverpilot",
    json_response=True,
    instructions=MCP_INSTRUCTIONS,
)


def _client(actor_name: str | None = None) -> BrokerClient:
    ensure_broker_ready_for_mcp()
    coordination_identity = codex_coordination_identity()
    if coordination_identity is not None:
        # Codex tasks are the actual coordination principals.  Tool arguments
        # such as agent_name predate task URIs and must not collapse distinct
        # tasks onto a shared label such as ``agent`` or ``codex-root``.
        actor_name = coordination_identity[0]
    return BrokerClient.from_env(actor=actor_name)


def _routine_gpu_claim_identifiers(client: BrokerClient, idempotency_key: str) -> tuple[str, str]:
    """Derive bounded, Agent-scoped attribution for the no-setup routine claim."""

    actor = getattr(client, "actor", "agent")
    actor_text = actor if isinstance(actor, str) and actor else "agent"
    project_digest = hashlib.sha256(actor_text.encode("utf-8")).hexdigest()[:24]
    task_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"agent-{project_digest}", f"gpu-apply-{task_digest}"


def _require_request_fields(request: dict[str, Any]) -> None:
    missing = [field for field in ("project_id", "task_ref", "purpose") if not request.get(field)]
    if missing:
        raise ValueError(f"gpu_request requires {', '.join(missing)}")


def _has_resource_quantity(quantities: dict[str, Any]) -> bool:
    return any(
        float(quantities.get(field) or 0) > 0
        for field in ("gpu_count", "cpu_cores", "memory_mib", "nodes", "scheduler_units")
    )


def _require_resource_claim_fields(claim: dict[str, Any]) -> None:
    missing = [field for field in ("project_id", "task_ref", "purpose", "quantities", "forecast") if not claim.get(field)]
    if missing:
        raise ValueError("resource_claim requires " + ", ".join(missing))
    quantities = claim["quantities"]
    if not isinstance(quantities, dict) or not _has_resource_quantity(quantities):
        raise ValueError("resource_claim quantities must request CPU, memory, GPU, nodes, or scheduler units")
    forecast = claim["forecast"]
    if not isinstance(forecast, dict):
        raise ValueError("resource_claim forecast must be a mapping")
    if not isinstance(forecast.get("quantities"), dict) or not forecast.get("predicted_runtime_seconds"):
        raise ValueError("resource_claim forecast requires quantities and predicted_runtime_seconds")


def _require_resource_plan_fields(evaluation: dict[str, Any]) -> None:
    missing = [
        field
        for field in ("project_id", "task_ref", "baseline_runtime_seconds", "candidates")
        if not evaluation.get(field)
    ]
    if missing:
        raise ValueError("resource_evaluate_plan requires " + ", ".join(missing))
    if evaluation.get("marginal_min_saved_ratio", RESOURCE_MARGINAL_MIN_SAVED_RATIO) != RESOURCE_MARGINAL_MIN_SAVED_RATIO:
        raise ValueError("resource_evaluate_plan marginal_min_saved_ratio must be 0.10")
    if evaluation.get("marginal_min_saved_seconds", RESOURCE_MARGINAL_MIN_SAVED_SECONDS) != RESOURCE_MARGINAL_MIN_SAVED_SECONDS:
        raise ValueError("resource_evaluate_plan marginal_min_saved_seconds must be 120")
    candidates = evaluation["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("resource_evaluate_plan candidates must be a non-empty list")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"resource_evaluate_plan candidate {index} must be a mapping")
        required = (
            "candidate_key",
            "quantities",
            "predicted_runtime_seconds",
            "predicted_saved_seconds",
            "predicted_saved_ratio",
            "satisfies_marginal_threshold",
        )
        missing_candidate = [field for field in required if field not in candidate]
        if missing_candidate:
            raise ValueError(
                f"resource_evaluate_plan candidate {index} requires "
                + ", ".join(missing_candidate)
            )
        if not isinstance(candidate["quantities"], dict) or not _has_resource_quantity(candidate["quantities"]):
            raise ValueError(f"resource_evaluate_plan candidate {index} quantities must include a resource")


def _require_endpoint_admin_contract(approval_ref: str, idempotency_key: str) -> None:
    if not isinstance(approval_ref, str) or not approval_ref.strip():
        raise ValueError("endpoint administration requires a non-empty current-task approval_ref")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("endpoint administration requires a caller-stable non-empty idempotency_key")


def _bounded_seconds(value: float, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _matching_request(payload: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((item for item in payload.get("data", []) if item.get("id") == request_id), None)


def _matching_lease(payload: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((item for item in payload.get("data", []) if item.get("request_id") == request_id), None)


def _compact_gpu_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the placement facts needed by an Agent and drop unrelated state.

    ``/api/v1/snapshot`` is intentionally the desktop's full revision-consistent
    read model.  MCP status calls should not echo its scheduler, generic-resource,
    history, and profile collections into the model context.
    """

    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if not data:
        return payload
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    gpus = data.get("gpus")
    if not isinstance(gpus, list):
        gpus = []
    host_capacity = data.get("host_capacity")
    if not isinstance(host_capacity, list):
        host_capacity = []
    capacity_by_endpoint = {
        item.get("endpoint", {}).get("id"): item
        for item in host_capacity
        if isinstance(item, dict) and isinstance(item.get("endpoint"), dict)
    }
    gpu_counts: dict[str, dict[str, int]] = {}
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        endpoint_id = gpu.get("endpoint_id")
        if not endpoint_id:
            continue
        counts = gpu_counts.setdefault(endpoint_id, {"total": 0, "available": 0})
        counts["total"] += 1
        if gpu.get("state") == "AVAILABLE":
            counts["available"] += 1

    compact_gpus = []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        telemetry = gpu.get("telemetry")
        if isinstance(telemetry, dict):
            telemetry = {
                key: telemetry.get(key)
                for key in (
                    "observed_at",
                    "memory_used_mib",
                    "memory_free_mib",
                    "gpu_utilization_pct",
                    "temperature_c",
                )
            }
        compact_gpus.append(
            {
                key: gpu.get(key)
                for key in (
                    "id",
                    "endpoint_id",
                    "gpu_index",
                    "name",
                    "total_vram_mib",
                    "state",
                    "state_reason",
                    "process_count",
                    "owner",
                    "task_ref",
                    "expires_at",
                )
            }
            | {"telemetry": telemetry}
        )

    servers = []
    for endpoint in data.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = endpoint.get("id")
        monitor = endpoint.get("monitor") if isinstance(endpoint.get("monitor"), dict) else {}
        host = capacity_by_endpoint.get(endpoint_id, {})
        capacity = host.get("capacity") if isinstance(host, dict) else {}
        capacity = capacity if isinstance(capacity, dict) else {}
        counts = gpu_counts.get(endpoint_id, {"total": 0, "available": 0})
        servers.append(
            {
                "server_id": endpoint_id,
                "monitor_status": monitor.get("status"),
                "gpu_count": counts["total"],
                "available_gpu_count": counts["available"],
                "available_cpu_cores": capacity.get("available_cpu_cores"),
                "available_memory_mib": capacity.get("available_memory_mib"),
                "total_memory_mib": capacity.get("total_memory_mib"),
                "last_error": monitor.get("last_error"),
            }
        )

    compact_data = {
        "summary": summary,
        "data_age_seconds": data.get("data_age_seconds"),
        "freshness_seconds": data.get("freshness_seconds"),
        "servers": servers,
        "gpus": compact_gpus,
    }
    return {
        "schema_version": payload.get("schema_version", "v1"),
        "snapshot_revision": payload.get("snapshot_revision"),
        "server_time": payload.get("server_time"),
        "data": compact_data,
    }


def _compact_coordination(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the shared board without dropping actor contact references."""

    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if not data:
        return payload

    def project_agent(agent: Any) -> dict[str, Any]:
        if not isinstance(agent, dict):
            return {}
        return {
            key: agent.get(key)
            for key in (
                "agent_name",
                "coordination_uri",
                "active_leases",
                "leased_gpus",
                "managed_running_gpus",
                "idle_leased_gpus",
                "projects",
                "servers",
            )
        }

    def project_lease(lease: Any) -> dict[str, Any]:
        if not isinstance(lease, dict):
            return {}
        return {
            key: lease.get(key)
            for key in (
                "lease_id",
                "agent_name",
                "coordination_uri",
                "project_id",
                "task",
                "state",
                "activity",
                "gpu_count",
                "servers",
                "observed_process_count",
                "expires_at",
            )
        }

    def project_server(server: Any) -> dict[str, Any]:
        if not isinstance(server, dict):
            return {}
        capacity = server.get("capacity")
        capacity = capacity if isinstance(capacity, dict) else {}
        return {
            "server_id": server.get("server_id"),
            "monitor_status": server.get("monitor_status"),
            "capacity": {
                key: capacity.get(key)
                for key in (
                    "total_gpus",
                    "available_gpus",
                    "leased_gpus",
                    "managed_running_gpus",
                    "idle_leased_gpus",
                    "unattributed_compute_gpus",
                    "gpu_states",
                    "available_cpu_cores",
                    "available_memory_mib",
                    "total_system_memory_mib",
                    "observed_gpu_utilization_pct",
                )
            },
            "consumers": [project_lease(item) for item in server.get("consumers", [])],
        }

    def project_queue(request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {}
        constraints = request.get("constraints")
        constraints = constraints if isinstance(constraints, dict) else {}
        return {
            "id": request.get("id"),
            "project_id": request.get("project_id"),
            "task_ref": request.get("task_ref"),
            "state": request.get("state"),
            "blocked_reason": request.get("blocked_reason"),
            "gpu_count": constraints.get("gpu_count"),
        }

    def project_signal(signal: Any) -> dict[str, Any]:
        if not isinstance(signal, dict):
            return {}
        return {
            key: signal.get(key)
            for key in (
                "kind",
                "severity",
                "lease_id",
                "agent_name",
                "request_id",
                "scheduler_job_id",
                "state",
                "message",
            )
        }

    def project_scheduler_job(job: Any) -> dict[str, Any]:
        if not isinstance(job, dict):
            return {}
        return {
            key: job.get(key)
            for key in (
                "id",
                "target_id",
                "project_id",
                "task_ref",
                "state",
                "scheduler_job_id",
                "allocated_tres",
                "node_list",
                "error_message",
            )
        }

    compact_data: dict[str, Any] = {
        "summary": data.get("summary", {}),
        "agents": [project_agent(item) for item in data.get("agents", [])],
        "leases": [project_lease(item) for item in data.get("leases", [])],
        "servers": [project_server(item) for item in data.get("servers", [])],
        "queue": [project_queue(item) for item in data.get("queue", [])],
        "signals": [project_signal(item) for item in data.get("signals", [])],
        "scheduler_jobs": [project_scheduler_job(item) for item in data.get("scheduler_jobs", [])],
        "guidance": data.get("guidance"),
    }
    return {
        "schema_version": payload.get("schema_version", "v1"),
        "snapshot_revision": payload.get("snapshot_revision"),
        "server_time": payload.get("server_time"),
        "data": compact_data,
    }


@mcp.tool()
def control_plane_state(
    agent_name: str | None = None,
    minimum_snapshot_revision: int | None = None,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    """Return the canonical broker state envelope from one control-plane revision."""

    return _client(agent_name).control_plane_state(
        minimum_snapshot_revision=minimum_snapshot_revision,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


@mcp.tool()
def gpu_status(
    compact: bool = True,
    server_id: str | None = None,
    state: str | None = None,
    only_available: bool = False,
) -> dict[str, Any]:
    """Return the compact placement view; use control_plane_state for explicit full diagnostics."""

    params: dict[str, Any] = {"compact": compact, "only_available": only_available}
    if server_id:
        params["endpoint_id"] = server_id
    if state:
        params["state"] = state
    payload = _client().snapshot(**params)
    return _compact_gpu_status(payload) if compact else payload


@mcp.tool()
def gpu_coordination(compact: bool = True) -> dict[str, Any]:
    """Return the compact coordination board for visible agents, leases, and servers.

    The board identifies each lease owner and task, real process attribution,
    per-server capacity, observed GPU use, queued demand, and factual signals
    such as an idle lease or unbound compute process. It is read-only.
    """

    payload = _client().coordination()
    return _compact_coordination(payload) if compact else payload


@mcp.tool()
def gpu_list(
    state: str | None = None,
    server_id: str | None = None,
    only_available: bool = False,
    compact: bool = True,
) -> dict[str, Any]:
    """Advanced read: list project-visible GPUs from the narrow REST projection."""

    return _client().gpus(
        state=state,
        endpoint_id=server_id,
        only_available=only_available,
        compact=compact,
    )


@mcp.tool()
def gpu_who(project_id: str | None = None) -> dict[str, Any]:
    """List project-visible leases and workload bindings; returns no SSH or task secrets."""

    return _client().leases(project_id=project_id)


@mcp.tool()
def gpu_scheduler_targets() -> dict[str, Any]:
    """List globally registered external scheduler targets.

    Scheduler targets are not raw GPU servers. Their login helpers and access
    hints are metadata; Slurm remains the resource allocator.
    """

    return _client().scheduler_targets()


@mcp.tool()
def gpu_scheduler_access_status(target_id: str) -> dict[str, Any]:
    """Check whether an external scheduler is reachable through its approved access path.

    This read-only check does not connect or change VPN state and does not submit a job.
    """

    return _client().get(f"/api/v1/scheduler-targets/{target_id}/access")


@mcp.tool()
def gpu_scheduler_profiles(project_id: str) -> dict[str, Any]:
    """List enabled Slurm profiles explicitly granted to a project."""

    result = _client().workload_profiles(project_id=project_id)
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
    approval_ref, duration_seconds, constraints, scheduler, and script_body.
    ServerPilot stores the script digest by default; retain_submission_body must be
    explicitly true to retain the exact body.
    """

    required = {
        "target_id",
        "project_id",
        "task_ref",
        "purpose",
        "approval_ref",
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
    """Read one live Slurm job or list the ServerPilot's persisted scheduler jobs."""

    client = _client(agent_name)
    if job_id:
        return client.get(f"/api/v1/scheduler-jobs/{job_id}")
    return client.scheduler_jobs(project_id=project_id)


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
    return client.scheduler_transfers(
        transfer_id=transfer_id,
        project_id=project_id,
    )


def gpu_request(request: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    """Submit an atomic GPU request. Required: project_id, task_ref, purpose, and constraints."""

    _require_request_fields(request)
    return _client().post(
        "/api/v1/requests",
        request,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_request_status(request_id: str | None = None) -> dict[str, Any]:
    """List visible requests or return one request by id."""

    return _client().requests(request_id=request_id)


def gpu_wait_for_claim(
    agent_name: str,
    request_id: str,
    timeout_seconds: float = 25,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Poll visible request and lease state until a prior claim is placed, terminal, or timed out."""

    agent_name = agent_name.strip()
    request_id = request_id.strip()
    if not agent_name or not request_id:
        raise ValueError("agent_name and request_id must not be empty")
    timeout_seconds = _bounded_seconds(
        timeout_seconds, name="timeout_seconds", minimum=0, maximum=300
    )
    poll_interval_seconds = _bounded_seconds(
        poll_interval_seconds, name="poll_interval_seconds", minimum=0.1, maximum=10
    )

    client = _client(agent_name)
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    polls = 0
    request: dict[str, Any] | None = None
    lease: dict[str, Any] | None = None

    while True:
        polls += 1
        state_payload = client.control_plane_state()
        current = state_payload["data"]["current"]
        requests_payload = {
            "schema_version": state_payload.get("schema_version", "v1"),
            "snapshot_revision": state_payload["snapshot_revision"],
            "server_time": state_payload.get("server_time"),
            "data": current.get("requests", []),
        }
        leases_payload = {
            "schema_version": state_payload.get("schema_version", "v1"),
            "snapshot_revision": state_payload["snapshot_revision"],
            "server_time": state_payload.get("server_time"),
            "data": current.get("leases", []),
        }
        request = _matching_request(requests_payload, request_id)
        lease = _matching_lease(leases_payload, request_id)
        elapsed_seconds = round(time.monotonic() - started_at, 3)

        if request is None:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "not_found",
                "request": None,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }
        if lease is not None and lease.get("state") in PLACED_LEASE_STATES:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "allocated",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }
        if request.get("state") in TERMINAL_REQUEST_STATES or (
            lease is not None and lease.get("state") in TERMINAL_LEASE_STATES
        ):
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "terminal",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "timeout",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
        time.sleep(min(poll_interval_seconds, remaining_seconds))


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
    lease_id: str,
    run_id: str | None = None,
    idempotency_key: str | None = None,
    agent_name: str | None = None,
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


def gpu_list_reservations() -> dict[str, Any]:
    """List visible future GPU reservations."""

    return _client().reservations()


@mcp.tool()
def gpu_history(after_id: int = 0, limit: int = 20) -> dict[str, Any]:
    """Read the append-only, redacted audit history for visible resources."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer between 1 and 200")
    return _client().get("/api/v1/events", params={"after_id": after_id, "limit": limit})


@mcp.tool()
def resource_providers(
    agent_name: str | None = None,
    provider_type: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """List generic resource providers: direct GPU, host CPU/memory capacity, and external schedulers."""

    return _client(agent_name).resource_providers(provider_type=provider_type, enabled=enabled)


@mcp.tool()
def resource_monitor(
    agent_name: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return real-time human/agent monitor data for generic resources and active claims."""

    return _client(agent_name).resource_monitor(project_id=project_id)


@mcp.tool()
def resource_claims(
    agent_name: str | None = None,
    project_id: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """List generic resource claims across visible projects and agents."""

    return _client(agent_name).resource_claims(project_id=project_id, state=state)


@mcp.tool()
def resource_evaluate_plan(
    agent_name: str,
    evaluation: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist an explicit marginal-utility resource decision.

    The evaluation must include candidate forecasts. The only accepted expansion
    threshold is 10% remaining-time savings and 120 seconds absolute savings.
    """

    _require_resource_plan_fields(evaluation)
    return _client(agent_name).evaluate_resource_plan(
        evaluation,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def resource_claim(
    agent_name: str,
    claim: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Claim the selected generic resource plan.

    Claims must include explicit quantities and a forecast. The ServerPilot returns
    the placement; a queued or null allocation is not permission to run.
    """

    _require_resource_claim_fields(claim)
    return _client(agent_name).claim_resource(
        claim,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def resource_release(
    agent_name: str,
    claim_id: str,
    reason: str = "workload_completed",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Release a generic resource claim; this never stops remote work."""

    if not claim_id.strip():
        raise ValueError("claim_id must not be empty")
    return _client(agent_name).release_resource_claim(
        claim_id,
        reason=reason,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def resource_record_actual(
    agent_name: str,
    actual: dict[str, Any],
    claim_id: str | None = None,
    evaluation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record observed runtime and outcome for later scheduling calibration and human monitoring."""

    if not actual.get("project_id") or not actual.get("task_ref") or not actual.get("quantities") or not actual.get("outcome"):
        raise ValueError("resource_record_actual requires project_id, task_ref, quantities, and outcome")
    if not isinstance(actual["quantities"], dict) or not _has_resource_quantity(actual["quantities"]):
        raise ValueError("resource_record_actual quantities must include a resource")
    return _client(agent_name).record_resource_run_actual(
        actual,
        claim_id=claim_id,
        evaluation_id=evaluation_id,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


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
    """Advanced compatibility helper for explicit direct-GPU claim contracts."""

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


def gpu_claim_profile(
    profile_id: str,
    task: str,
    idempotency_key: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Advanced compatibility helper for a direct-GPU workload profile."""

    if not profile_id.strip() or not task.strip():
        raise ValueError("profile_id and task must not be empty")
    return _client(agent_name).post(
        f"/api/v1/workload-profiles/{profile_id}/claim",
        {"task_ref": task},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_apply(
    server_id: str | None = None,
    gpu_count: int = 1,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply for one or more GPUs now; ServerPilot chooses the allocatable GPUs.

    `server_id` is an optional server preference, never a GPU selector. On
    success, use only the returned lease resources; `no_capacity` creates no queue
    and does not authorize workload execution.
    """

    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count must be a positive integer")
    if server_id is not None:
        server_id = server_id.strip()
        if not server_id:
            raise ValueError("server_id must not be empty when provided")
    mutation_key = idempotency_key or secrets.token_hex(16)
    client = _client()
    project_id, task_ref = _routine_gpu_claim_identifiers(client, mutation_key)
    constraints: dict[str, Any] = {"gpu_count": gpu_count, "placement": "pack"}
    if server_id is not None:
        constraints["endpoint_ids"] = [server_id]
    return client.post(
        "/api/v1/claims",
        {
            "project_id": project_id,
            "task_ref": task_ref,
            "purpose": "routine Agent GPU allocation",
            "constraints": constraints,
        },
        idempotency_key=mutation_key,
    )


@mcp.tool()
def gpu_release(
    lease_id: str,
    reason: str = "workload_completed",
    idempotency_key: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Release a prior claim; this never stops a process on the remote server."""

    return _client(agent_name).post(
        f"/api/v1/leases/{lease_id}/release",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


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
    approval_ref: str,
    idempotency_key: str,
    port: int = 22,
    ssh_user: str = "root",
    server_id: str | None = None,
    ssh_alias: str | None = None,
    observation_profile: str = "server-script-v1",
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
) -> dict[str, Any]:
    """Create a shared endpoint after explicit current-task human approval."""

    if not project_id.strip():
        raise ValueError("project_id must not be empty")
    _require_endpoint_admin_contract(approval_ref, idempotency_key)
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
            "ssh_alias": ssh_alias,
            "observation_profile": observation_profile,
            "labels": labels or [],
            "storage_group": storage_group,
            "expected_gpu_count": expected_gpu_count,
            "expected_gpu_total_vram_mib": expected_gpu_total_vram_mib,
            "owner_project_id": project_id,
        },
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def gpu_update_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
    ssh_user: str | None = None,
    ssh_alias: str | None = None,
    observation_profile: str | None = None,
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
    owner_project_id: str | None = None,
) -> dict[str, Any]:
    """Update safe endpoint metadata; endpoint id, host, and port are immutable."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    body = {
        key: value
        for key, value in {
            "ssh_user": ssh_user,
            "ssh_alias": ssh_alias,
            "observation_profile": observation_profile,
            "labels": labels,
            "storage_group": storage_group,
            "expected_gpu_count": expected_gpu_count,
            "expected_gpu_total_vram_mib": expected_gpu_total_vram_mib,
            "owner_project_id": owner_project_id,
        }.items()
        if value is not None
    }
    if not body:
        raise ValueError("gpu_update_server requires at least one safe metadata field")
    return _client(agent_name).patch(
        f"/api/v1/endpoints/{server_id}",
        body,
        idempotency_key=idempotency_key,
    )


def gpu_pause_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Pause new placement (active -> draining) without stopping collection or workloads."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/pause", {}, idempotency_key=idempotency_key
    )


def gpu_resume_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Resume a draining endpoint (draining -> active)."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/resume", {}, idempotency_key=idempotency_key
    )


@mcp.tool()
def gpu_set_keepalive(
    agent_name: str,
    server_id: str,
    enabled: bool,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Set the endpoint's explicit idle-GPU keepalive policy after approval.

    This endpoint switch is not a whole-server worker: it reconciles eligible
    GPUs independently and does not accept caller-supplied GPU targets, PIDs,
    shell fragments, or helper settings. Disable it before direct SSH work;
    ServerPilot-managed claim/recovery follows its verified per-GPU handoff.
    """

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/keepalive",
        {"enabled": enabled},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def gpu_retire_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Retire a drained endpoint only after active leases and pinned queue work are clear."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/retire", {}, idempotency_key=idempotency_key
    )


@mcp.tool()
def gpu_delete_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Remove a server after all of its GPU leases have been released."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/retire", {}, idempotency_key=idempotency_key
    )


ROUTINE_MCP_TOOL_NAMES = (
    "gpu_status",
    "gpu_coordination",
    "gpu_apply",
    "gpu_bind_observed_workload",
    "gpu_renew_lease",
    "gpu_release",
)


def _build_routine_mcp() -> FastMCP:
    """Build the small default surface while retaining compatibility tools."""

    routine = FastMCP(
        "serverpilot",
        json_response=True,
        instructions=MCP_INSTRUCTIONS,
    )
    for name in ROUTINE_MCP_TOOL_NAMES:
        tool = mcp._tool_manager._tools[name]
        routine.add_tool(
            tool.fn,
            name=tool.name,
            title=tool.title,
            description=tool.description,
            annotations=tool.annotations,
            icons=tool.icons,
            meta=tool.meta,
        )
    return routine


# ``mcp`` remains the import-compatible full registry for REST/MCP tests and
# advanced callers.  The stdio entry point uses this smaller registry by
# default, so tool discovery is intent-first rather than compatibility-first.
routine_mcp = _build_routine_mcp()


def main() -> None:
    profile = os.environ.get("SERVERPILOT_MCP_PROFILE", "routine").strip().lower()
    if profile == "advanced":
        mcp.run()
        return
    if profile != "routine":
        raise SystemExit("SERVERPILOT_MCP_PROFILE must be 'routine' or 'advanced'")
    routine_mcp.run()


if __name__ == "__main__":
    main()
