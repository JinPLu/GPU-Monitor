"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx


class BrokerClientError(RuntimeError):
    pass


class BrokerClient:
    def __init__(self, url: str, actor: str = "agent", *, timeout_seconds: float = 20) -> None:
        if not url.startswith(("http://", "https://")):
            raise BrokerClientError("GPU_BROKER_URL must start with http:// or https://")
        self.url = url.rstrip("/")
        self.actor = actor or "agent"
        self.timeout_seconds = timeout_seconds
        self._last_state_revision: int | None = None

    @classmethod
    def from_env(cls, *, url: str | None = None, actor: str | None = None) -> "BrokerClient":
        return cls(
            url or os.environ.get("GPU_BROKER_URL", "http://127.0.0.1:8787"),
            actor or os.environ.get("GPU_BROKER_ACTOR", "agent"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-GPU-Broker-Actor": self.actor}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        # A loopback service can be briefly unavailable while it restarts. GET
        # requests are safe to retry. Mutations retry only with the caller's
        # idempotency key, so a tool caller can reuse one stable key across its
        # own retries without creating a duplicate claim or release.
        retryable = method.upper() == "GET" or idempotency_key is not None
        attempts = 3 if retryable else 1
        response: httpx.Response | None = None
        last_transport_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                response = httpx.request(
                    method,
                    f"{self.url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.timeout_seconds,
                    # ServerPilot is a local control plane.  MCP processes are
                    # often launched with a minimal environment that omits
                    # NO_PROXY, so httpx would otherwise send loopback calls
                    # through an ambient HTTP proxy and surface its empty 502.
                    trust_env=False,
                )
            except httpx.HTTPError as exc:
                last_transport_error = exc
                if attempt + 1 == attempts:
                    raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
            else:
                if response.status_code not in {502, 503, 504} or attempt + 1 == attempts:
                    break
            time.sleep(0.1 * (attempt + 1))
        if response is None:
            assert last_transport_error is not None
            raise BrokerClientError(f"broker request failed: {type(last_transport_error).__name__}")
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            suffix = " after retry" if attempts > 1 else ""
            raise BrokerClientError(
                f"broker returned non-JSON HTTP {response.status_code}{suffix} ({content_type})"
            ) from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise BrokerClientError(
                f"broker HTTP {response.status_code}: {error.get('code', 'unknown')}: {error.get('message', 'request failed')}"
            )
        if not isinstance(payload, dict):
            raise BrokerClientError("broker returned an invalid JSON envelope")
        return payload

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request("POST", path, json_body=body, idempotency_key=idempotency_key)

    def patch(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request("PATCH", path, json_body=body, idempotency_key=idempotency_key)

    def delete(self, path: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request("DELETE", path, idempotency_key=idempotency_key)

    def control_plane_state(
        self,
        *,
        minimum_snapshot_revision: int | None = None,
        timeout_seconds: float = 0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if minimum_snapshot_revision is not None and (
            isinstance(minimum_snapshot_revision, bool)
            or not isinstance(minimum_snapshot_revision, int)
            or minimum_snapshot_revision < 0
        ):
            raise BrokerClientError("minimum_snapshot_revision must be a non-negative integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
            raise BrokerClientError("timeout_seconds must be a number")
        if isinstance(poll_interval_seconds, bool) or not isinstance(
            poll_interval_seconds, int | float
        ):
            raise BrokerClientError("poll_interval_seconds must be a number")
        timeout_seconds = float(timeout_seconds)
        poll_interval_seconds = float(poll_interval_seconds)
        if not 0 <= timeout_seconds <= 300:
            raise BrokerClientError("timeout_seconds must be between 0 and 300")
        if not 0.05 <= poll_interval_seconds <= 10:
            raise BrokerClientError("poll_interval_seconds must be between 0.05 and 10")

        deadline = time.monotonic() + timeout_seconds
        previous_revision = self._last_state_revision
        while True:
            payload = self.get("/api/v1/state")
            revision = payload.get("snapshot_revision")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise BrokerClientError("broker state returned an invalid snapshot_revision")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
                raise BrokerClientError("broker state returned an invalid current state")
            if previous_revision is not None and revision < previous_revision:
                raise BrokerClientError(
                    f"broker state revision rolled back from {previous_revision} to {revision}"
                )
            previous_revision = revision
            self._last_state_revision = revision
            if minimum_snapshot_revision is None or revision >= minimum_snapshot_revision:
                return payload
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise BrokerClientError(
                    f"broker state revision {revision} is below required {minimum_snapshot_revision}"
                )
            time.sleep(min(poll_interval_seconds, remaining_seconds))

    def _state_data(
        self, **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        payload = self.control_plane_state(**kwargs)
        data = payload["data"]
        history = data.get("history")
        if not isinstance(history, dict):
            raise BrokerClientError("broker state returned an invalid history state")
        return payload, data["current"], history

    def _state_current(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        payload, current, _history = self._state_data(**kwargs)
        return payload, current

    def _state_projection(
        self,
        key: str,
        *,
        data: Any | None = None,
        current: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state is None or current is None:
            state, current = self._state_current()
        if data is None:
            if key not in current:
                raise BrokerClientError(f"broker state is missing current.{key}")
            data = current[key]
        return {
            "schema_version": state.get("schema_version", "v1"),
            "snapshot_revision": state["snapshot_revision"],
            "server_time": state.get("server_time"),
            "data": data,
        }

    def snapshot(
        self,
        *,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        payload, current = self._state_current()
        data = dict(current)
        gpus = list(data.get("gpus", []))
        if endpoint_id:
            gpus = [gpu for gpu in gpus if gpu.get("endpoint_id") == endpoint_id]
        if state:
            gpus = [gpu for gpu in gpus if gpu.get("state") == state]
        if only_available:
            gpus = [gpu for gpu in gpus if gpu.get("state") == "AVAILABLE"]
        if compact:
            gpus = [
                {key: value for key, value in gpu.items() if key not in {"processes", "telemetry_history"}}
                for gpu in gpus
            ]
        data["gpus"] = gpus
        return self._state_projection("current", data=data, current=current, state=payload)

    def endpoints(self) -> dict[str, Any]:
        return self._state_projection("endpoints")

    def endpoint_history(
        self,
        endpoint_id: str,
        *,
        window_seconds: int = 3600,
        points: int = 120,
    ) -> dict[str, Any]:
        return self.get(
            f"/api/v1/endpoints/{endpoint_id}/history",
            params={"window_seconds": window_seconds, "points": points},
        )

    def gpus(
        self,
        *,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(
            compact=compact,
            endpoint_id=endpoint_id,
            state=state,
            only_available=only_available,
        )
        return self._state_projection("gpus", data=snapshot["data"]["gpus"], state=snapshot, current=snapshot["data"])

    def leases(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload, current = self._state_current()
        leases = current.get("leases")
        if leases is None:
            raise BrokerClientError("broker state is missing current.leases")
        if project_id:
            leases = [lease for lease in leases if lease.get("project_id") == project_id]
        return self._state_projection("leases", data=leases, current=current, state=payload)

    def requests(self, *, request_id: str | None = None, queued_only: bool = False) -> dict[str, Any]:
        payload, current = self._state_current()
        requests = current.get("requests")
        if requests is None:
            raise BrokerClientError("broker state is missing current.requests")
        if request_id:
            requests = [request for request in requests if request.get("id") == request_id]
        if queued_only:
            requests = [
                request
                for request in requests
                if request.get("state") in {"QUEUED", "PENDING_APPROVAL"}
            ]
        return self._state_projection("requests", data=requests, current=current, state=payload)

    def reservations(self) -> dict[str, Any]:
        return self._state_projection("reservations")

    def workload_profiles(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload, current = self._state_current()
        profiles = current.get("workload_profiles")
        if profiles is None:
            raise BrokerClientError("broker state is missing current.workload_profiles")
        if project_id:
            profiles = [profile for profile in profiles if profile.get("project_id") == project_id]
        return self._state_projection("workload_profiles", data=profiles, current=current, state=payload)

    def scheduler_targets(self) -> dict[str, Any]:
        return self._state_projection("scheduler_targets")

    def scheduler_jobs(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload, current = self._state_current()
        jobs = current.get("scheduler_jobs")
        if jobs is None:
            raise BrokerClientError("broker state is missing current.scheduler_jobs")
        if project_id:
            jobs = [job for job in jobs if job.get("project_id") == project_id]
        return self._state_projection("scheduler_jobs", data=jobs, current=current, state=payload)

    def scheduler_transfers(
        self,
        *,
        transfer_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        payload, current = self._state_current()
        transfers = current.get("scheduler_transfers")
        if transfers is None:
            raise BrokerClientError("broker state is missing current.scheduler_transfers")
        if transfer_id is not None:
            transfer = next(
                (item for item in transfers if item.get("id") == transfer_id),
                None,
            )
            if transfer is None:
                raise BrokerClientError("scheduler transfer does not exist or is not visible")
            return self._state_projection(
                "scheduler_transfers",
                data=transfer,
                current=current,
                state=payload,
            )
        if project_id:
            transfers = [
                transfer
                for transfer in transfers
                if transfer.get("project_id") == project_id
            ]
        return self._state_projection(
            "scheduler_transfers",
            data=transfers,
            current=current,
            state=payload,
        )

    def coordination(self) -> dict[str, Any]:
        payload, current = self._state_current()
        gpus = current.get("gpus")
        endpoints = current.get("endpoints")
        leases = current.get("leases")
        requests = current.get("requests")
        scheduler_targets = current.get("scheduler_targets")
        scheduler_jobs = current.get("scheduler_jobs")
        if not all(
            isinstance(value, list)
            for value in (gpus, endpoints, leases, requests, scheduler_targets, scheduler_jobs)
        ):
            raise BrokerClientError("broker state is missing coordination source lists")

        gpus_by_id = {gpu["id"]: gpu for gpu in gpus}
        gpus_by_endpoint: dict[str, list[dict[str, Any]]] = {}
        for gpu in gpus:
            gpus_by_endpoint.setdefault(gpu["endpoint_id"], []).append(gpu)

        def average(values: list[int | float | None]) -> float | None:
            present = [float(value) for value in values if value is not None]
            return round(sum(present) / len(present), 1) if present else None

        def gpu_state_counts(values: list[dict[str, Any]]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for gpu in values:
                state = str(gpu.get("state", "UNKNOWN"))
                counts[state] = counts.get(state, 0) + 1
            return dict(sorted(counts.items()))

        def lease_activity(values: list[dict[str, Any]]) -> str:
            states = {gpu.get("state") for gpu in values}
            if states.intersection({"CONFLICT", "ORPHANED_BUSY"}):
                return "needs_attention"
            if "BUSY_UNMANAGED" in states:
                return "unattributed_compute"
            if "RUNNING_MANAGED" in states:
                return "running"
            if values and all(gpu.get("state") == "LEASED_IDLE" for gpu in values):
                return "lease_idle"
            if values and all(gpu.get("state") == "HELD" for gpu in values):
                return "held"
            return "starting"

        lease_cards: list[dict[str, Any]] = []
        consumers_by_endpoint: dict[str, list[dict[str, Any]]] = {}
        agents: dict[str, dict[str, Any]] = {}
        signals: list[dict[str, Any]] = []
        for lease in leases:
            lease_gpus = [gpus_by_id[gpu_id] for gpu_id in lease.get("gpu_ids", []) if gpu_id in gpus_by_id]
            endpoint_ids = sorted({gpu["endpoint_id"] for gpu in lease_gpus})
            state_counts = gpu_state_counts(lease_gpus)
            telemetry = [gpu.get("telemetry") for gpu in lease_gpus if gpu.get("telemetry") is not None]
            card = {
                "lease_id": lease["id"],
                "agent_name": lease.get("actor_id"),
                "project_id": lease.get("project_id"),
                "task": lease.get("task_ref"),
                "state": lease.get("state"),
                "activity": lease_activity(lease_gpus),
                "gpu_count": len(lease_gpus),
                "servers": endpoint_ids,
                "gpu_states": state_counts,
                "observed_gpu_utilization_pct": average(
                    [item.get("gpu_utilization_pct") for item in telemetry]
                ),
                "observed_memory_used_mib": sum(item.get("memory_used_mib") or 0 for item in telemetry),
                "observed_process_count": sum(len(gpu.get("processes", [])) for gpu in lease_gpus),
                "workloads": [
                    {
                        "run_id": workload.get("run_id"),
                        "process_key_count": len(workload.get("process_keys", [])),
                    }
                    for workload in lease.get("workloads", [])
                ],
                "issued_at": lease.get("issued_at"),
                "expires_at": lease.get("expires_at"),
            }
            lease_cards.append(card)
            for endpoint_id in endpoint_ids:
                endpoint_gpu_count = sum(gpu["endpoint_id"] == endpoint_id for gpu in lease_gpus)
                consumers_by_endpoint.setdefault(endpoint_id, []).append(
                    {
                        "lease_id": card["lease_id"],
                        "agent_name": card["agent_name"],
                        "project_id": card["project_id"],
                        "task": card["task"],
                        "gpu_count": endpoint_gpu_count,
                        "activity": card["activity"],
                    }
                )
            agent_name = str(lease.get("actor_id") or "")
            agent = agents.setdefault(
                agent_name,
                {
                    "agent_name": agent_name,
                    "active_leases": 0,
                    "leased_gpus": 0,
                    "managed_running_gpus": 0,
                    "idle_leased_gpus": 0,
                    "projects": set(),
                    "servers": set(),
                },
            )
            agent["active_leases"] += 1
            agent["leased_gpus"] += len(lease_gpus)
            agent["managed_running_gpus"] += state_counts.get("RUNNING_MANAGED", 0)
            agent["idle_leased_gpus"] += state_counts.get("LEASED_IDLE", 0)
            if lease.get("project_id") is not None:
                agent["projects"].add(lease["project_id"])
            agent["servers"].update(endpoint_ids)
            if card["activity"] == "lease_idle":
                signals.append(
                    {
                        "kind": "lease_idle",
                        "severity": "info",
                        "lease_id": lease["id"],
                        "agent_name": agent_name,
                        "message": "active lease has no observed compute process yet",
                    }
                )
            elif card["activity"] == "unattributed_compute":
                signals.append(
                    {
                        "kind": "unattributed_compute",
                        "severity": "warning",
                        "lease_id": lease["id"],
                        "agent_name": agent_name,
                        "message": "compute process is observed but not bound to this lease",
                    }
                )
            elif card["activity"] == "needs_attention":
                signals.append(
                    {
                        "kind": "lease_conflict",
                        "severity": "critical",
                        "lease_id": lease["id"],
                        "agent_name": agent_name,
                        "message": "lease has a process-attribution or expiry conflict",
                    }
                )

        server_cards = []
        for endpoint in endpoints:
            endpoint_gpus = gpus_by_endpoint.get(endpoint["id"], [])
            endpoint_telemetry = [
                gpu.get("telemetry") for gpu in endpoint_gpus if gpu.get("telemetry") is not None
            ]
            state_counts = gpu_state_counts(endpoint_gpus)
            host = endpoint.get("host_telemetry")
            server_cards.append(
                {
                    "server_id": endpoint["id"],
                    "monitor_status": endpoint.get("monitor", {}).get("status"),
                    "host_telemetry": host,
                    "capacity": {
                        "total_gpus": len(endpoint_gpus),
                        "available_gpus": state_counts.get("AVAILABLE", 0),
                        "leased_gpus": sum(1 for gpu in endpoint_gpus if gpu.get("lease") is not None),
                        "managed_running_gpus": state_counts.get("RUNNING_MANAGED", 0),
                        "idle_leased_gpus": state_counts.get("LEASED_IDLE", 0),
                        "unattributed_compute_gpus": state_counts.get("BUSY_UNMANAGED", 0),
                        "gpu_states": state_counts,
                        "observed_gpu_utilization_pct": average(
                            [item.get("gpu_utilization_pct") for item in endpoint_telemetry]
                        ),
                        "available_cpu_cores": round(
                            max(0.0, host["cpu_count"] - host["load_1m"]), 1
                        )
                        if host
                        else None,
                        "available_memory_mib": host["memory_available_mib"] if host else None,
                        "total_system_memory_mib": host["memory_total_mib"] if host else None,
                        "total_vram_mib": sum(gpu.get("total_vram_mib") or 0 for gpu in endpoint_gpus),
                        "observed_memory_used_mib": sum(
                            item.get("memory_used_mib") or 0 for item in endpoint_telemetry
                        ),
                        "observed_memory_free_mib": sum(
                            item.get("memory_free_mib") or 0 for item in endpoint_telemetry
                        ),
                    },
                    "consumers": sorted(
                        consumers_by_endpoint.get(endpoint["id"], []),
                        key=lambda item: (item["agent_name"] or "", item["lease_id"]),
                    ),
                }
            )

        for request in requests:
            signals.append(
                {
                    "kind": "queued_request",
                    "severity": "info",
                    "request_id": request.get("id"),
                    "project_id": request.get("project_id"),
                    "task": request.get("task_ref"),
                    "gpu_count": request.get("constraints", {}).get("gpu_count"),
                    "message": request.get("blocked_reason") or "waiting for scheduler placement",
                }
            )
        active_scheduler_jobs = [
            job
            for job in scheduler_jobs
            if job.get("state") not in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
        ]
        for job in active_scheduler_jobs:
            signals.append(
                {
                    "kind": "external_scheduler_job",
                    "severity": "warning" if job.get("state") in {"UNKNOWN", "ACCESS_REQUIRED"} else "info",
                    "scheduler_job_id": job.get("id"),
                    "target_id": job.get("target_id"),
                    "project_id": job.get("project_id"),
                    "task": job.get("task_ref"),
                    "state": job.get("state"),
                    "message": job.get("error_message")
                    or "external scheduler owns placement; this is not a raw GPU lease",
                }
            )
        agent_cards = [
            {
                **agent,
                "projects": sorted(agent["projects"]),
                "servers": sorted(agent["servers"]),
            }
            for agent in agents.values()
        ]
        agent_cards.sort(key=lambda item: item["agent_name"])
        lease_cards.sort(key=lambda item: (item["agent_name"] or "", item["lease_id"]))
        signals.sort(key=lambda item: (item["severity"], item.get("agent_name", ""), item["kind"]))
        total_telemetry = [gpu.get("telemetry") for gpu in gpus if gpu.get("telemetry") is not None]
        summary = {
            **current.get("summary", {}),
            "active_leases": len(lease_cards),
            "active_agents": len(agent_cards),
            "queued_requests": len(requests),
            "queued_gpus": sum(item.get("constraints", {}).get("gpu_count") or 0 for item in requests),
            "external_scheduler_targets": len(scheduler_targets),
            "external_scheduler_jobs": len(active_scheduler_jobs),
            "external_scheduler_pending_jobs": sum(
                job.get("state") == "PENDING" for job in active_scheduler_jobs
            ),
            "external_scheduler_running_jobs": sum(
                job.get("state") == "RUNNING" for job in active_scheduler_jobs
            ),
            "managed_running_gpus": sum(
                item["gpu_states"].get("RUNNING_MANAGED", 0) for item in lease_cards
            ),
            "idle_leased_gpus": sum(item["gpu_states"].get("LEASED_IDLE", 0) for item in lease_cards),
            "observed_gpu_utilization_pct": average(
                [item.get("gpu_utilization_pct") for item in total_telemetry]
            ),
        }
        return self._state_projection(
            "coordination",
            data={
                "summary": summary,
                "servers": server_cards,
                "agents": agent_cards,
                "leases": lease_cards,
                "queue": requests,
                "scheduler_targets": scheduler_targets,
                "scheduler_jobs": active_scheduler_jobs,
                "signals": signals,
                "guidance": (
                    "This board is read-only. Claims without a requested server are placed by the broker's "
                    "shared scheduler; external Slurm jobs remain separate from raw GPU leases and are "
                    "allocated only when Slurm reports RUNNING with AllocTRES."
                ),
            },
            current=current,
            state=payload,
        )

    def resource_providers(
        self,
        *,
        provider_type: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        payload, current = self._state_current()
        providers = current.get("resource_providers")
        if providers is None:
            raise BrokerClientError("broker state is missing current.resource_providers")
        providers = [
            provider
            for provider in providers
            if (provider_type is None or provider.get("provider_type") == provider_type)
            and (enabled is None or provider.get("enabled") is enabled)
        ]
        return self._state_projection("resource_providers", data=providers, current=current, state=payload)

    def resource_monitor(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload, current, history = self._state_data()
        if isinstance(current.get("resource_monitor"), dict):
            monitor = dict(current["resource_monitor"])
        else:
            claims = current.get("resource_claims", [])
            allocations = current.get("resource_allocations", current.get("allocations"))
            if allocations is None:
                allocations = [
                    allocation
                    for claim in claims
                    for allocation in claim.get("allocations", [])
                ]
            monitor = {
                "summary": current.get("summary", {}),
                "host_capacity": current.get("host_capacity", []),
                "providers": current.get("resource_providers", []),
                "claims": claims,
                "allocations": allocations,
                "plan_evaluations": history.get(
                    "resource_plan_evaluations",
                    current.get("resource_plan_evaluations", []),
                ),
                "actuals": history.get("resource_run_actuals", current.get("resource_run_actuals", [])),
                "admission_boundary": current.get("admission_boundary"),
            }
        if project_id:
            claims = [
                claim for claim in monitor.get("claims", []) if claim.get("project_id") == project_id
            ]
            claim_ids = {claim.get("id") for claim in claims}
            monitor["claims"] = claims
            monitor["allocations"] = [
                allocation
                for allocation in monitor.get("allocations", [])
                if allocation.get("claim_id") in claim_ids
            ]
            monitor["plan_evaluations"] = [
                evaluation
                for evaluation in monitor.get("plan_evaluations", [])
                if evaluation.get("project_id") == project_id
            ]
            monitor["actuals"] = [
                actual for actual in monitor.get("actuals", []) if actual.get("project_id") == project_id
            ]
        return self._state_projection("resource_monitor", data=monitor, current=current, state=payload)

    def resource_claims(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        payload, current = self._state_current()
        claims = current.get("resource_claims")
        if claims is None:
            raise BrokerClientError("broker state is missing current.resource_claims")
        claims = [
            claim
            for claim in claims
            if (project_id is None or claim.get("project_id") == project_id)
            and (state is None or str(claim.get("state", "")).lower() == state.lower())
        ]
        return self._state_projection("resource_claims", data=claims, current=current, state=payload)

    def resource_plan_evaluations(
        self,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self.resource_monitor(project_id=project_id)

    def resource_run_actuals(
        self,
        *,
        project_id: str | None = None,
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        payload, current, history = self._state_data()
        actuals = history.get("resource_run_actuals", current.get("resource_run_actuals"))
        if actuals is None:
            raise BrokerClientError("broker state is missing history.resource_run_actuals")
        actuals = [
            actual
            for actual in actuals
            if (project_id is None or actual.get("project_id") == project_id)
            and (task_ref is None or actual.get("task_ref") == task_ref)
        ]
        return self._state_projection("resource_run_actuals", data=actuals, current=current, state=payload)

    def evaluate_resource_plan(
        self,
        evaluation: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.post(
            "/api/v1/resource-plan-evaluations",
            evaluation,
            idempotency_key=idempotency_key,
        )

    def claim_resource(
        self,
        claim: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.post(
            "/api/v1/resource-claims",
            claim,
            idempotency_key=idempotency_key,
        )

    def release_resource_claim(
        self,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.post(
            f"/api/v1/resource-claims/{claim_id}/release",
            {"reason": reason},
            idempotency_key=idempotency_key,
        )

    def record_resource_run_actual(
        self,
        actual: dict[str, Any],
        *,
        claim_id: str | None = None,
        evaluation_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if claim_id:
            params["claim_id"] = claim_id
        if evaluation_id:
            params["evaluation_id"] = evaluation_id
        return self.request(
            "POST",
            "/api/v1/resource-run-actuals",
            json_body=actual,
            params=params or None,
            idempotency_key=idempotency_key,
        )
