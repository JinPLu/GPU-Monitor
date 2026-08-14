"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from serverpilot.client import BrokerClient, BrokerClientError
from serverpilot.daemon import ensure_broker_ready_for_mcp


PLACED_LEASE_STATES = {"HELD", "ACTIVE"}
TERMINAL_REQUEST_STATES = {"CANCELLED", "EXPIRED", "REJECTED", "RELEASED"}
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
RESOURCE_MARGINAL_MIN_SAVED_RATIO = 0.10
RESOURCE_MARGINAL_MIN_SAVED_SECONDS = 120
_ROUTINE_MCP_INSTANCE_ID = secrets.token_hex(16)

MCP_INSTRUCTIONS = """常规 GPU 任务三个工具：gpu_status；gpu_apply 自动选卡(task=任务名)，不读取客户端 UI 标题；gpu_release。
ssh=连接；workspace.path（workspace_path）=cwd，以它为工作目录；kind=working_directory、use_as_cwd=true、code_location=not_provided；不得把 workspace_path 当代码仓库路径。
cuda_device_order=PCI_BUS_ID；cuda_visible_devices=租约ordinal；gpus[].gpu_cuda_visible_devices=单卡ordinal。
telemetry 含最近观测/近10分钟均值，status/gpu_apply 决定申请。include_busy=true 看 task/空闲占卡；无容量直接失败，不排队。
失败即 gpu_release。只协调 GPU；勿用 SSH、SQLite、inventory、nvidia-smi 绕过协调。
非 GPU 远端操作（Git 同步）无需 GPU 租约。"""


mcp = FastMCP(
    "serverpilot",
    json_response=True,
    instructions=MCP_INSTRUCTIONS,
)


def _client(actor_name: str | None = None) -> BrokerClient:
    ensure_broker_ready_for_mcp()
    return BrokerClient.from_env(actor=actor_name)


def _routine_client() -> BrokerClient:
    ensure_broker_ready_for_mcp()
    return BrokerClient.from_env(actor="agent")


def _routine_task(task: str | None) -> str:
    if task is None:
        return "未命名任务"
    value = task.strip()
    if not value:
        raise ValueError("提供 task 时不能为空")
    if len(value) > 120:
        raise ValueError("task 最长 120 个字符")
    return value


def _routine_request_key(context: Context | None) -> str | None:
    """Map one MCP invocation to a private, stable REST replay key."""

    if context is None:
        return None
    request_id = f"{_ROUTINE_MCP_INSTANCE_ID}:{context.request_id}".encode("utf-8")
    return f"mcp-request:{hashlib.sha256(request_id).hexdigest()}"


def _routine_ssh(endpoint: Any) -> dict[str, Any] | None:
    """Project one registered endpoint into shell-neutral SSH connection data."""

    if not isinstance(endpoint, dict):
        return None
    host = endpoint.get("host")
    port = endpoint.get("port")
    user = endpoint.get("ssh_user")
    if (
        not isinstance(host, str)
        or not host
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
        or not isinstance(user, str)
        or not user
    ):
        return None
    return {"host": host, "port": port, "user": user}


def _routine_workspace(path: Any) -> dict[str, Any]:
    """Describe the endpoint cwd without implying that it is a code checkout."""

    return {
        "path": path if isinstance(path, str) and path else None,
        "kind": "working_directory",
        "use_as_cwd": True,
        "code_location": "not_provided",
    }


def _routine_integer(value: Any) -> int | None:
    """Keep an optional non-negative integer from a broker telemetry payload."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _routine_number(value: Any) -> int | float | None:
    """Keep an optional finite non-negative telemetry measurement."""

    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value if value >= 0 else None


def _routine_recent_telemetry_average(value: Any) -> dict[str, Any] | None:
    """Project a per-GPU rolling average without exposing other snapshot state."""

    if not isinstance(value, dict):
        return None
    window_seconds = _routine_integer(value.get("window_seconds"))
    sample_count = _routine_integer(value.get("sample_count"))
    first_observed_at = value.get("first_observed_at")
    last_observed_at = value.get("last_observed_at")
    return {
        "window_seconds": window_seconds if window_seconds and window_seconds > 0 else None,
        "sample_count": sample_count if sample_count and sample_count > 0 else None,
        "first_observed_at": (
            first_observed_at
            if isinstance(first_observed_at, str) and first_observed_at
            else None
        ),
        "last_observed_at": (
            last_observed_at if isinstance(last_observed_at, str) and last_observed_at else None
        ),
        "memory_used_mib": _routine_number(value.get("memory_used_mib")),
        "memory_free_mib": _routine_number(value.get("memory_free_mib")),
        "memory_used_pct": _routine_number(value.get("memory_used_pct")),
        "gpu_utilization_pct": _routine_number(value.get("gpu_utilization_pct")),
        "memory_utilization_pct": _routine_number(value.get("memory_utilization_pct")),
        "temperature_c": _routine_number(value.get("temperature_c")),
    }


def _routine_gpu_telemetry(gpu: dict[str, Any], *, vram_mib: Any) -> dict[str, Any] | None:
    """Project current GPU telemetry without exposing the full snapshot payload.

    Telemetry is descriptive rather than a scheduling input.  Preserve the
    observation timestamp so callers can distinguish a missing or old sample
    from a lightly loaded device.
    """

    source = gpu.get("telemetry")
    if not isinstance(source, dict):
        return None
    total_vram_mib = _routine_integer(vram_mib)
    memory_used_mib = _routine_integer(source.get("memory_used_mib"))
    memory_used_pct = (
        round(memory_used_mib * 100 / total_vram_mib, 2)
        if memory_used_mib is not None and total_vram_mib and total_vram_mib > 0
        else None
    )
    observed_at = source.get("observed_at")
    return {
        "observed_at": observed_at if isinstance(observed_at, str) and observed_at else None,
        "memory_used_mib": memory_used_mib,
        "memory_free_mib": _routine_integer(source.get("memory_free_mib")),
        "memory_used_pct": memory_used_pct,
        "gpu_utilization_pct": _routine_integer(source.get("gpu_utilization_pct")),
        "memory_utilization_pct": _routine_integer(source.get("memory_utilization_pct")),
        "temperature_c": _routine_integer(source.get("temperature_c")),
        "recent_average": _routine_recent_telemetry_average(source.get("recent_average")),
    }


def _routine_gpu_telemetry_summary(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize exactly the GPUs visible in one routine status response."""

    vram_values = [
        vram_mib for gpu in gpus if (vram_mib := _routine_integer(gpu.get("vram_mib"))) is not None
    ]
    telemetry = [item for gpu in gpus if isinstance(item := gpu.get("telemetry"), dict)]

    def values_for(field: str) -> list[int]:
        return [
            value for item in telemetry if (value := _routine_integer(item.get(field))) is not None
        ]

    memory_used_values = values_for("memory_used_mib")
    memory_free_values = values_for("memory_free_mib")
    gpu_utilization_values = values_for("gpu_utilization_pct")
    memory_utilization_values = values_for("memory_utilization_pct")
    observed_memory_vram = [
        vram_mib
        for gpu in gpus
        if isinstance(gpu.get("telemetry"), dict)
        and _routine_integer(gpu["telemetry"].get("memory_used_mib")) is not None
        and (vram_mib := _routine_integer(gpu.get("vram_mib"))) is not None
    ]
    memory_used_pct = (
        round(sum(memory_used_values) * 100 / sum(observed_memory_vram), 2)
        if memory_used_values and observed_memory_vram and sum(observed_memory_vram) > 0
        else None
    )

    def average(values: list[int]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    return {
        "visible_gpu_count": len(gpus),
        "vram_total_mib": sum(vram_values),
        "average_vram_mib": average(vram_values),
        "telemetry_gpu_count": len(telemetry),
        "memory_used_mib": sum(memory_used_values) if memory_used_values else None,
        "memory_used_gpu_count": len(memory_used_values),
        "memory_free_mib": sum(memory_free_values) if memory_free_values else None,
        "memory_free_gpu_count": len(memory_free_values),
        "memory_used_pct": memory_used_pct,
        "current_average_gpu_utilization_pct": average(gpu_utilization_values),
        "gpu_utilization_gpu_count": len(gpu_utilization_values),
        "current_average_memory_utilization_pct": average(memory_utilization_values),
        "memory_utilization_gpu_count": len(memory_utilization_values),
    }


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
    missing = [
        field
        for field in ("project_id", "task_ref", "purpose", "quantities", "forecast")
        if not claim.get(field)
    ]
    if missing:
        raise ValueError("resource_claim requires " + ", ".join(missing))
    quantities = claim["quantities"]
    if not isinstance(quantities, dict) or not _has_resource_quantity(quantities):
        raise ValueError(
            "resource_claim quantities must request CPU, memory, GPU, nodes, or scheduler units"
        )
    forecast = claim["forecast"]
    if not isinstance(forecast, dict):
        raise ValueError("resource_claim forecast must be a mapping")
    if not isinstance(forecast.get("quantities"), dict) or not forecast.get(
        "predicted_runtime_seconds"
    ):
        raise ValueError(
            "resource_claim forecast requires quantities and predicted_runtime_seconds"
        )


def _require_resource_plan_fields(evaluation: dict[str, Any]) -> None:
    missing = [
        field
        for field in ("project_id", "task_ref", "baseline_runtime_seconds", "candidates")
        if not evaluation.get(field)
    ]
    if missing:
        raise ValueError("resource_evaluate_plan requires " + ", ".join(missing))
    if (
        evaluation.get("marginal_min_saved_ratio", RESOURCE_MARGINAL_MIN_SAVED_RATIO)
        != RESOURCE_MARGINAL_MIN_SAVED_RATIO
    ):
        raise ValueError("resource_evaluate_plan marginal_min_saved_ratio must be 0.10")
    if (
        evaluation.get("marginal_min_saved_seconds", RESOURCE_MARGINAL_MIN_SAVED_SECONDS)
        != RESOURCE_MARGINAL_MIN_SAVED_SECONDS
    ):
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
                f"resource_evaluate_plan candidate {index} requires " + ", ".join(missing_candidate)
            )
        if not isinstance(candidate["quantities"], dict) or not _has_resource_quantity(
            candidate["quantities"]
        ):
            raise ValueError(
                f"resource_evaluate_plan candidate {index} quantities must include a resource"
            )


def _require_endpoint_admin_contract(approval_ref: str, idempotency_key: str) -> None:
    if not isinstance(approval_ref, str) or not approval_ref.strip():
        raise ValueError("服务器管理需要当前任务明确授权，并提供非空 approval_ref")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("服务器管理需要提供稳定且非空的 idempotency_key")


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
    return next(
        (item for item in payload.get("data", []) if item.get("request_id") == request_id), None
    )


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
        counts = gpu_counts.setdefault(
            endpoint_id,
            {
                "total": 0,
                "available": 0,
                "workload_leased": 0,
                "keepalive_owned": 0,
                "verified_keepalive": 0,
            },
        )
        counts["total"] += 1
        keepalive = gpu.get("keepalive")
        if gpu.get("state") in {"AVAILABLE", "KEEPALIVE"}:
            counts["available"] += 1
        lease = gpu.get("lease")
        if isinstance(lease, dict):
            counts["workload_leased"] += 1
        keepalive = gpu.get("keepalive")
        if isinstance(keepalive, dict) and keepalive.get("lease_id"):
            counts["keepalive_owned"] += 1
        if gpu.get("state") == "KEEPALIVE":
            counts["verified_keepalive"] += 1

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
        lease = gpu.get("lease")
        lease = lease if isinstance(lease, dict) else {}
        keepalive = gpu.get("keepalive")
        keepalive = keepalive if isinstance(keepalive, dict) else {}
        compact_gpus.append(
            {
                "id": gpu.get("id"),
                "endpoint_id": gpu.get("endpoint_id"),
                "gpu_index": gpu.get("gpu_index"),
                "name": gpu.get("name"),
                "total_vram_mib": gpu.get("total_vram_mib"),
                "state": gpu.get("state"),
                "state_reason": gpu.get("state_reason"),
                "process_count": gpu.get(
                    "process_count",
                    len(gpu.get("processes", [])) if isinstance(gpu.get("processes"), list) else 0,
                ),
                "owner": gpu.get("owner", lease.get("actor_id")),
                "task_ref": gpu.get("task_ref", lease.get("task_ref")),
                "expires_at": gpu.get("expires_at", lease.get("expires_at")),
            }
            | {
                "telemetry": telemetry,
                "keepalive": {
                    "desired": keepalive.get("desired", "OFF"),
                    "actual": keepalive.get("actual", keepalive.get("state", "OFF")),
                    "state": keepalive.get("state", "OFF"),
                    "reason": keepalive.get("reason"),
                },
            }
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
        counts = gpu_counts.get(
            endpoint_id,
            {
                "total": 0,
                "available": 0,
                "workload_leased": 0,
                "keepalive_owned": 0,
                "verified_keepalive": 0,
            },
        )
        servers.append(
            {
                "server_id": endpoint_id,
                "workspace_path": endpoint.get("workspace_path"),
                "monitor_status": monitor.get("status"),
                "gpu_count": counts["total"],
                "available_gpu_count": counts["available"],
                "workload_leased_gpu_count": counts["workload_leased"],
                "keepalive_owned_gpu_count": counts["keepalive_owned"],
                "verified_keepalive_gpu_count": counts["verified_keepalive"],
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
        "availability_semantics": (
            "可用 GPU 包含空闲卡和已确认的逐卡空闲占卡。真正分配前，ServerPilot 会先停止选中卡的占卡程序，"
            "刷新确认后再走普通申请；任务占用和冲突 GPU 不可用。"
        ),
        "servers": servers,
        "gpus": compact_gpus,
    }
    return {
        "schema_version": payload.get("schema_version", "v1"),
        "snapshot_revision": payload.get("snapshot_revision"),
        "server_time": payload.get("server_time"),
        "data": compact_data,
    }


def _routine_gpu_status(payload: dict[str, Any], *, include_busy: bool) -> dict[str, Any]:
    data = payload.get("data")
    values = data.get("gpus", []) if isinstance(data, dict) else []
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else []
    endpoint_by_id = {
        endpoint.get("id"): endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and endpoint.get("id")
    }
    gpus: list[dict[str, Any]] = []
    for gpu in values:
        if not isinstance(gpu, dict):
            continue
        available = gpu.get("publicly_available")
        status = gpu.get("public_status")
        if not isinstance(available, bool) or not isinstance(status, str) or not status:
            raise ValueError("ServerPilot 返回的 GPU 公开状态无效")
        lease = gpu.get("lease")
        if not available and not include_busy:
            continue
        endpoint = endpoint_by_id.get(gpu.get("endpoint_id"))
        row: dict[str, Any] = {
            "server_id": gpu.get("endpoint_id"),
            "workspace_path": (
                endpoint.get("workspace_path") if isinstance(endpoint, dict) else None
            ),
            "gpu_id": gpu.get("gpu_uuid"),
            "index": gpu.get("gpu_index"),
            "name": gpu.get("name"),
            "vram_mib": gpu.get("total_vram_mib"),
            "status": status,
        }
        row["telemetry"] = _routine_gpu_telemetry(gpu, vram_mib=row["vram_mib"])
        row["workspace"] = _routine_workspace(row["workspace_path"])
        ssh = _routine_ssh(endpoint)
        if ssh is not None:
            row["ssh"] = ssh
        keepalive = gpu.get("keepalive")
        if isinstance(keepalive, dict):
            row["keepalive"] = {
                "desired": keepalive.get("desired", "OFF"),
                "actual": keepalive.get("actual", keepalive.get("state", "OFF")),
            }
        if include_busy:
            row["available"] = available
            if isinstance(lease, dict):
                row["task"] = lease.get("task_ref") or "未命名任务"
        gpus.append(row)
    result: dict[str, Any] = {"gpus": gpus}
    cpu_only_servers: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or endpoint.get("resource_kind") != "cpu_only":
            continue
        monitor = endpoint.get("monitor")
        monitor = monitor if isinstance(monitor, dict) else {}
        host_telemetry = endpoint.get("host_telemetry")
        host_telemetry = host_telemetry if isinstance(host_telemetry, dict) else {}
        cpu_only_servers.append(
            {
                "server_id": endpoint.get("id"),
                "resource_kind": "cpu_only",
                "monitor_status": monitor.get("status"),
                "cpu_count": _routine_integer(host_telemetry.get("cpu_count")),
                "memory_available_mib": _routine_integer(
                    host_telemetry.get("memory_available_mib")
                ),
            }
        )
    if cpu_only_servers:
        result["cpu_only_servers"] = cpu_only_servers
    if gpus:
        result["telemetry_summary"] = _routine_gpu_telemetry_summary(gpus)
    summary = data.get("summary") if isinstance(data, dict) else None
    if isinstance(summary, dict) and summary.get("total_gpus") == 0:
        result["message"] = "无 GPU"
    elif not include_busy and not gpus:
        total_gpus = summary.get("total_gpus") if isinstance(summary, dict) else None
        result["no_capacity"] = {
            "reason": "all_gpus_busy_or_unavailable",
            "message": "当前没有可申请 GPU；可调用 gpu_status(include_busy=true) 查看占用任务或异常状态。",
            "total_gpus": total_gpus if isinstance(total_gpus, int) else None,
        }
    return result


def _routine_gpu_allocation(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a lease for routine use without conflating lease and GPU scope.

    A single-endpoint lease keeps its complete CUDA device set at the top level.
    Each ``gpus`` row carries both the resource-wide selector and its own
    one-GPU selector while retaining the UUID as the physical identity.
    """

    lease = payload.get("lease")
    if not isinstance(lease, dict):
        raise ValueError("ServerPilot 没有返回 GPU 租约")
    rows: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        if not isinstance(resource, dict):
            continue
        endpoint = resource.get("endpoint")
        server_id = endpoint.get("id") if isinstance(endpoint, dict) else None
        workspace_path = endpoint.get("workspace_path") if isinstance(endpoint, dict) else None
        ssh = _routine_ssh(endpoint)
        cuda_visible_devices = resource.get("cuda_visible_devices")
        cuda_device_order = resource.get("cuda_device_order")
        if cuda_device_order != "PCI_BUS_ID" or not isinstance(cuda_visible_devices, str):
            raise ValueError("ServerPilot 返回了无效的 CUDA 执行 selector")
        resource_projection = {
            "server_id": server_id,
            "workspace_path": workspace_path,
            "workspace": _routine_workspace(workspace_path),
            "cuda_visible_devices": cuda_visible_devices,
            "cuda_device_order": cuda_device_order,
        }
        if ssh is not None:
            resource_projection["ssh"] = ssh
        resources.append(resource_projection)
        for gpu in resource.get("gpus", []):
            if isinstance(gpu, dict):
                gpu_index = gpu.get("gpu_index")
                cuda_ordinal = gpu.get("cuda_ordinal")
                if isinstance(cuda_ordinal, bool) or not isinstance(cuda_ordinal, int) or cuda_ordinal < 0:
                    raise ValueError("ServerPilot 返回了无效的 GPU CUDA ordinal")
                row = {
                    "server_id": server_id,
                    "workspace_path": workspace_path,
                    "workspace": _routine_workspace(workspace_path),
                    "gpu_id": gpu.get("gpu_uuid"),
                    "gpu_index": gpu_index,
                    "cuda_ordinal": cuda_ordinal,
                    "cuda_visible_devices": cuda_visible_devices,
                    "gpu_cuda_visible_devices": str(cuda_ordinal),
                    "cuda_device_order": cuda_device_order,
                }
                if ssh is not None:
                    row["ssh"] = ssh
                rows.append(row)
    result: dict[str, Any] = {"lease_id": lease.get("id"), "gpus": rows}
    if len(resources) == 1:
        result.update(resources[0])
    return result


def _compact_coordination(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the shared board to its operational fields."""

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
                "active_leases",
                "active_workload_leases",
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
            "workspace_path": server.get("workspace_path"),
            "monitor_status": server.get("monitor_status"),
            "capacity": {
                key: capacity.get(key)
                for key in (
                    "total_gpus",
                    "available_gpus",
                    "leased_gpus",
                    "workload_leased_gpus",
                    "keepalive_owned_gpus",
                    "verified_keepalive_gpus",
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
def gpu_status(include_busy: bool = False) -> dict[str, Any]:
    """列出 GPU、已识别纯 CPU 服务器和近 10 分钟显存/利用率遥测；include_busy=true 时列出占用任务。"""

    payload = _routine_client().snapshot(
        compact=False,
        only_available=not include_busy,
    )
    return _routine_gpu_status(payload, include_busy=include_busy)


@mcp.tool()
def gpu_coordination(compact: bool = True) -> dict[str, Any]:
    """返回只读协作面板，显示可见 Agent、工作任务租约和服务器容量。"""

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
        profile for profile in result.get("data", []) if profile.get("runtime_kind") == "slurm"
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
    ServerPilot omits the submitted body unless retain_submission_body is explicitly true.
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
        raise ValueError("gpu_scheduler_submit_once requires " + ", ".join(missing))
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
        f"/api/v1/requests/{request_id}/cancel",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_activate_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Record that a held lease is active; it does not launch any command."""

    return _client().post(
        f"/api/v1/leases/{lease_id}/activate",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_renew_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """续期调用者持有的工作任务租约。"""

    return _client().post(
        f"/api/v1/leases/{lease_id}/renew",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
def gpu_release_lease(
    lease_id: str, reason: str, idempotency_key: str | None = None
) -> dict[str, Any]:
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
    """把已启动任务的最新采集进程绑定到调用者租约；不会启动或修改远端任务。"""

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

    if (
        not actual.get("project_id")
        or not actual.get("task_ref")
        or not actual.get("quantities")
        or not actual.get("outcome")
    ):
        raise ValueError(
            "resource_record_actual requires project_id, task_ref, quantities, and outcome"
        )
    if not isinstance(actual["quantities"], dict) or not _has_resource_quantity(
        actual["quantities"]
    ):
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
    task: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """立即申请 GPU；返回 SSH、结构化远端工作目录和 CUDA selector；no_capacity 不排队。"""

    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count 必须是正整数")
    if server_id is not None:
        server_id = server_id.strip()
        if not server_id:
            raise ValueError("提供 server_id 时不能为空")
    constraints: dict[str, Any] = {"gpu_count": gpu_count, "placement": "pack"}
    if server_id is not None:
        constraints["endpoint_ids"] = [server_id]
    task_ref = _routine_task(task)
    body = {
        "project_id": "agent",
        "task_ref": task_ref,
        "purpose": task_ref,
        "constraints": constraints,
    }
    replay_key = _routine_request_key(context) or f"mcp-call:{secrets.token_hex(16)}"
    client = _routine_client()
    try:
        payload = client.post(
            "/api/v1/routine/claims",
            body,
            idempotency_key=replay_key,
        )
    except BrokerClientError as exc:
        if not str(exc).startswith("broker request failed:"):
            raise
        # The broker may have committed before the local HTTP response was
        # interrupted. Retry once with the same key so it cannot allocate a
        # second lease for this tool invocation.
        payload = client.post(
            "/api/v1/routine/claims",
            body,
            idempotency_key=replay_key,
        )
    return _routine_gpu_allocation(payload)


@mcp.tool()
def gpu_release(
    lease_id: str,
) -> dict[str, Any]:
    """释放此前申请的 GPU。"""

    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ValueError("lease_id 不能为空")
    lease_id = lease_id.strip()
    _routine_client().post(f"/api/v1/routine/leases/{lease_id}/release")
    return {"released": True}


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
    workspace_path: str,
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
            "workspace_path": workspace_path,
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
    workspace_path: str | None = None,
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
            "workspace_path": workspace_path,
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
    """经明确授权后开启或关闭服务器的逐卡空闲占卡策略。

    每张空闲 GPU 独立协调；调用者不能传 GPU 目标、PID、shell 片段或 helper 参数。
    Agent 申请和 APP 改派都会复用同一条逐卡让位路径。
    """

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    if type(enabled) is not bool:
        raise ValueError("enabled 必须是布尔值")
    return _client(agent_name).post(
        f"/api/v1/endpoints/{server_id}/keepalive",
        {"enabled": enabled},
        idempotency_key=idempotency_key,
    )


ROUTINE_MCP_TOOL_NAMES = (
    "gpu_status",
    "gpu_apply",
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
