from __future__ import annotations

import httpx
import pytest

from gpu_broker.client import BrokerClient, BrokerClientError


def test_client_retries_a_transient_gateway_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(502, text="temporarily unavailable"),
            httpx.Response(200, json={"schema_version": "v1", "data": {}}),
        ]
    )
    calls = []

    def request(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr("gpu_broker.client.httpx.request", request)
    monkeypatch.setattr("gpu_broker.client.time.sleep", lambda _seconds: None)

    assert BrokerClient("http://127.0.0.1:8787").get("/api/v1/snapshot") == {
        "schema_version": "v1",
        "data": {},
    }
    assert len(calls) == 2
    assert all(call[1]["trust_env"] is False for call in calls)


def _state(revision: int, current: dict | None = None) -> dict:
    return {
        "schema_version": "v1",
        "snapshot_revision": revision,
        "server_time": "2026-08-06T00:00:00Z",
        "data": {"current": current or {"gpus": [], "leases": [], "requests": []}, "history": {}},
    }


def test_control_plane_state_waits_for_minimum_revision(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(4)),
            httpx.Response(200, json=_state(6)),
        ]
    )
    paths = []
    sleeps = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        paths.append((method, url, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr("gpu_broker.client.httpx.request", request)
    monkeypatch.setattr("gpu_broker.client.time.sleep", lambda seconds: sleeps.append(seconds))

    result = BrokerClient("http://127.0.0.1:8787").control_plane_state(
        minimum_snapshot_revision=5,
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    assert result["snapshot_revision"] == 6
    assert paths == [
        ("GET", "http://127.0.0.1:8787/api/v1/state", None),
        ("GET", "http://127.0.0.1:8787/api/v1/state", None),
    ]
    assert sleeps == [pytest.approx(0.1)]


def test_control_plane_state_rejects_revision_rollback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(7)),
            httpx.Response(200, json=_state(6)),
        ]
    )

    def request(*args, **kwargs):  # type: ignore[no-untyped-def]
        return next(responses)

    monkeypatch.setattr("gpu_broker.client.httpx.request", request)
    client = BrokerClient("http://127.0.0.1:8787")

    assert client.control_plane_state()["snapshot_revision"] == 7
    with pytest.raises(BrokerClientError, match="rolled back"):
        client.control_plane_state()


def test_control_plane_state_retains_observed_revision_after_minimum_timeout(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(9)),
            httpx.Response(200, json=_state(8)),
        ]
    )

    monkeypatch.setattr(
        "gpu_broker.client.httpx.request",
        lambda *args, **kwargs: next(responses),
    )
    client = BrokerClient("http://127.0.0.1:8787")

    with pytest.raises(BrokerClientError, match="below required 10"):
        client.control_plane_state(minimum_snapshot_revision=10, timeout_seconds=0)
    with pytest.raises(BrokerClientError, match="rolled back from 9 to 8"):
        client.control_plane_state()


def test_operational_read_aliases_project_from_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    current = {
        "endpoints": [{"id": "server-a"}],
        "gpus": [
            {"id": "gpu-a", "endpoint_id": "server-a", "state": "AVAILABLE", "processes": []},
            {"id": "gpu-b", "endpoint_id": "server-b", "state": "HELD", "processes": []},
        ],
        "leases": [{"id": "lease-a", "project_id": "project-a"}],
        "requests": [{"id": "req-a", "state": "QUEUED"}, {"id": "req-b", "state": "LEASED"}],
        "reservations": [],
        "resource_providers": [{"id": "provider-a", "provider_type": "host-capacity", "enabled": True}],
        "host_capacity": [{"endpoint": {"id": "server-a"}, "admission_state": "available"}],
        "resource_claims": [
            {
                "id": "claim-a",
                "project_id": "project-a",
                "state": "active",
                "allocations": [{"id": "allocation-a", "claim_id": "claim-a"}],
            }
        ],
        "resource_run_actuals": [{"id": "actual-a", "project_id": "project-a", "task_ref": "task"}],
    }
    calls = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, url))
        return httpx.Response(200, json=_state(11, current))

    monkeypatch.setattr("gpu_broker.client.httpx.request", request)
    client = BrokerClient("http://127.0.0.1:8787")

    assert client.endpoints()["data"] == [{"id": "server-a"}]
    assert client.gpus(only_available=True, compact=True)["data"] == [
        {"id": "gpu-a", "endpoint_id": "server-a", "state": "AVAILABLE"}
    ]
    assert client.requests(queued_only=True)["data"] == [{"id": "req-a", "state": "QUEUED"}]
    assert client.resource_providers(provider_type="host-capacity", enabled=True)["data"][0]["id"] == "provider-a"
    assert client.resource_claims(project_id="project-a", state="ACTIVE")["data"][0]["id"] == "claim-a"
    monitor = client.resource_monitor(project_id="project-a")["data"]
    assert monitor["host_capacity"][0]["endpoint"]["id"] == "server-a"
    assert monitor["allocations"] == [{"id": "allocation-a", "claim_id": "claim-a"}]
    assert all(path == "http://127.0.0.1:8787/api/v1/state" for _, path in calls)
