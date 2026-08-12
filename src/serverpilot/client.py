"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

import httpx


class BrokerClientError(RuntimeError):
    pass


def codex_coordination_identity() -> tuple[str, str] | None:
    """Return the task-scoped Actor and URI inherited by a Codex MCP child.

    A thread UUID is a process identity boundary, not user input.  Reject a
    malformed ambient value instead of silently falling back to the shared
    ``agent`` Actor, which would merge unrelated Codex tasks.
    """

    raw_thread_id = os.environ.get("CODEX_THREAD_ID")
    if raw_thread_id is None:
        return None
    try:
        parsed = UUID(raw_thread_id)
    except (ValueError, AttributeError) as exc:
        raise BrokerClientError("CODEX_THREAD_ID must be a canonical UUID") from exc
    canonical = str(parsed)
    if raw_thread_id != canonical:
        raise BrokerClientError("CODEX_THREAD_ID must be a canonical UUID")
    return f"codex-{canonical}", f"codex://threads/{canonical}"


class BrokerClient:
    def __init__(
        self,
        url: str,
        actor: str = "agent",
        *,
        timeout_seconds: float = 20,
        coordination_uri: str | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise BrokerClientError("SERVERPILOT_URL must start with http:// or https://")
        self.url = url.rstrip("/")
        self.actor = actor or "agent"
        self.coordination_uri = coordination_uri
        self.timeout_seconds = timeout_seconds
        self._last_state_revision: int | None = None

    @classmethod
    def from_env(cls, *, url: str | None = None, actor: str | None = None) -> "BrokerClient":
        coordination_identity = codex_coordination_identity()
        configured_actor = actor or os.environ.get("SERVERPILOT_ACTOR")
        if configured_actor is None and coordination_identity is not None:
            configured_actor = coordination_identity[0]
        return cls(
            url or os.environ.get("SERVERPILOT_URL", "http://127.0.0.1:8787"),
            configured_actor or "agent",
            coordination_uri=(coordination_identity[1] if coordination_identity else None),
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
        headers = {"X-ServerPilot-Actor": self.actor}
        if self.coordination_uri:
            headers["X-ServerPilot-Coordination-URI"] = self.coordination_uri
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
            raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            raise BrokerClientError(
                f"broker returned non-JSON HTTP {response.status_code} ({content_type})"
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
        idempotency_key: str | None = None,
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
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        if state:
            params["state"] = state
        return self.get("/api/v1/snapshot", params=params)

    def endpoints(self) -> dict[str, Any]:
        return self.get("/api/v1/endpoints")

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
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if state:
            params["state"] = state
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        # The REST GPU projection is already revision-consistent and returns
        # only the requested GPU list.  Do not fetch /api/v1/state and then
        # discard scheduler/resource/history collections in the MCP client.
        return self.get("/api/v1/gpus", params=params)

    def leases(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload = self.get("/api/v1/leases")
        leases = payload.get("data")
        if not isinstance(leases, list):
            raise BrokerClientError("broker leases response is invalid")
        if project_id:
            leases = [lease for lease in leases if lease.get("project_id") == project_id]
        return {**payload, "data": leases}

    def requests(self, *, request_id: str | None = None, queued_only: bool = False) -> dict[str, Any]:
        payload = self.get("/api/v1/requests")
        requests = payload.get("data")
        if not isinstance(requests, list):
            raise BrokerClientError("broker requests response is invalid")
        if request_id:
            requests = [request for request in requests if request.get("id") == request_id]
        if queued_only:
            requests = [
                request
                for request in requests
                if request.get("state") in {"QUEUED", "PENDING_APPROVAL"}
            ]
        return {**payload, "data": requests}

    def reservations(self) -> dict[str, Any]:
        return self.get("/api/v1/reservations")

    def workload_profiles(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return self.get("/api/v1/workload-profiles", params=params)

    def scheduler_targets(self) -> dict[str, Any]:
        return self.get("/api/v1/scheduler-targets")

    def scheduler_jobs(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return self.get("/api/v1/scheduler-jobs", params=params)

    def scheduler_transfers(
        self,
        *,
        transfer_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if transfer_id is not None:
            return self.get(f"/api/v1/scheduler-transfers/{transfer_id}")
        params = {"project_id": project_id} if project_id else None
        return self.get("/api/v1/scheduler-transfers", params=params)

    def coordination(self) -> dict[str, Any]:
        return self.get("/api/v1/coordination")

    def resource_providers(
        self,
        *,
        provider_type: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if provider_type is not None:
            params["provider_type"] = provider_type
        if enabled is not None:
            params["enabled"] = enabled
        return self.get("/api/v1/resource-providers", params=params or None)

    def resource_monitor(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return self.get("/api/v1/resource-monitor", params=params)

    def resource_claims(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if project_id is not None:
            params["project_id"] = project_id
        if state is not None:
            params["state"] = state
        return self.get("/api/v1/resource-claims", params=params or None)

    def resource_plan_evaluations(
        self,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return self.get("/api/v1/resource-plan-evaluations", params=params)

    def resource_run_actuals(
        self,
        *,
        project_id: str | None = None,
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if project_id is not None:
            params["project_id"] = project_id
        if task_ref is not None:
            params["task_ref"] = task_ref
        return self.get("/api/v1/resource-run-actuals", params=params or None)

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
