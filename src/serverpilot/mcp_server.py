"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import math
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

MCP_INSTRUCTIONS = (
    "Use serverpilot for GPU, CPU, memory, scheduler work, and read-only resource discovery. The ServerPilot is the only "
    "allocation and freshness/probing authority: do not bypass it through SSH, SQLite, inventory, remote "
    "probes, or nvidia-smi. A request or accepted plan to run, continue, or monitor a resource task makes its "
    "owner-scoped claim routine once a profile_id, or project_id, resource quantities, and needed thresholds "
    "are recorded. Reuse that contract without asking again; never infer missing inputs from a repository, "
    "task title, free capacity, or defaults. "
    "For cross-project and cross-agent resource scheduling, prefer resource_providers and resource_monitor for "
    "discovery, then resource_evaluate_plan with explicit candidate forecasts, then resource_claim for the "
    "selected smallest useful plan. Start from the smallest feasible CPU, memory, GPU, node, or scheduler "
    "quantity and expand only when the next candidate is forecast to save at least 10% remaining time and "
    "at least 120 seconds. Record rejected expansions and actual runtime with resource_record_actual so humans "
    "can monitor decisions and outcomes in real time. A zero-GPU CPU/memory or scheduler plan is valid when "
    "its resource quantities are explicit. "
    "The normal bare-metal path is gpu_claim_profile or gpu_claim. A claim either returns resources now or fails without creating a queue, "
    "then project execution on the returned held or active lease.resources[], then "
    "gpu_bind_observed_workload, then gpu_release. Use gpu_coordination when shared state would help, but "
    "ordinary pre-approved profile claims and explicit-contract claims do not require a separate "
    "coordination read first. When a held or active lease "
    "is returned, take the placement only from its structured lease.resources[]: each resource provides an "
    "endpoint, gpus, cuda_visible_devices, and commitment. Use the project's normal execution path to start "
    "or stop its workload there. The ServerPilot does not launch or stop that workload. "
    "After it starts, gpu_bind_observed_workload records fresh observed processes; release when it stops "
    "or startup fails. Use the full requested allocation when the workload safely supports it. "
    "Provide an idempotency_key for a mutation when retrying it, and reuse that same caller-chosen key. "
    "The low-level activate, release-lease, and bind-workload tools remain advanced "
    "compatibility tools; prefer the normal path. "
    "Endpoints are shared loopback inventory; project ownership is optional attribution. Authorized "
    "actors may administer them only with a current-task approval_ref and caller-stable idempotency_key. "
    "A server can be removed after its active leases are released. Removing it never stops an existing workload. "
    "Endpoint keepalive is an explicit opt-in policy, controlled once per server but executed independently "
    "per idle GPU. gpu_set_keepalive enabled=true reconciles eligible idle GPUs now; ServerPilot-managed "
    "claims may reclaim only their selected verified keepalive GPU and released idle GPUs can rejoin the "
    "policy. Direct SSH work is outside that handoff and requires disabling the endpoint policy first. "
    "External Slurm clusters are SchedulerTargets, never raw SSH endpoints. Use "
    "gpu_scheduler_targets and gpu_scheduler_access_status to discover a target, then use owner-scoped "
    "submit, status, and cancel operations. "
    "A Slurm PENDING job is not a bare-metal lease; scheduler status and AllocTRES establish its allocation. "
    "VPN access is detection only: access_required means ask the user to connect the approved VPN, then retry. "
    "On macOS this adapter automatically ensures the shared headless loopback daemon before REST calls; "
    "it does not depend on the GUI. If MCP or the service is unavailable, report that state and do not fall "
    "back to a bypass."
)


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
    """Return shared state, including per-server CPU load and available system memory for placement decisions."""

    params: dict[str, Any] = {"compact": compact, "only_available": only_available}
    if server_id:
        params["endpoint_id"] = server_id
    if state:
        params["state"] = state
    return _client().snapshot(**params)


@mcp.tool()
def gpu_coordination() -> dict[str, Any]:
    """Return the shared broker coordination board for all visible agents and servers.

    The board identifies each lease owner and task, real process attribution,
    per-server capacity, observed GPU use, queued demand, and factual signals
    such as an idle lease or unbound compute process. It is read-only.
    """

    return _client().coordination()


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
def gpu_list_profiles(project_id: str | None = None) -> dict[str, Any]:
    """List project-visible workload profiles approved for routine GPU claims."""

    return _client().workload_profiles(project_id=project_id)


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
def gpu_history(after_id: int = 0) -> dict[str, Any]:
    """Read the append-only, redacted audit history for visible resources."""

    return _client().get("/api/v1/events", params={"after_id": after_id})


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
    """Claim GPUs now or fail with no_capacity; no queue is created. Thresholds are absolute."""

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
    profile_id: str,
    task: str,
    idempotency_key: str | None = None,
    agent_name: str | None = None,
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
