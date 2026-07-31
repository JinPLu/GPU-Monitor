from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gpu_broker import mcp_server
from gpu_broker.mcp_server import mcp


class FakeClient:
    def __init__(
        self,
        requests_by_poll: list[list[dict[str, Any]]],
        leases_by_poll: list[list[dict[str, Any]]],
    ) -> None:
        self.requests_by_poll = requests_by_poll
        self.leases_by_poll = leases_by_poll
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.poll_index = -1

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        if path == "/api/v1/requests":
            self.poll_index += 1
            index = min(self.poll_index, len(self.requests_by_poll) - 1)
            return {"schema_version": "v1", "data": self.requests_by_poll[index]}
        if path == "/api/v1/leases":
            index = min(max(self.poll_index, 0), len(self.leases_by_poll) - 1)
            return {"schema_version": "v1", "data": self.leases_by_poll[index]}
        raise AssertionError(f"unexpected path: {path}")


def _request(state: str = "QUEUED") -> dict[str, Any]:
    return {"id": "req-1", "state": state, "project_id": "project-a"}


def _lease(state: str = "HELD") -> dict[str, Any]:
    return {
        "id": "lease-1",
        "request_id": "req-1",
        "state": state,
        "resources": [
            {
                "endpoint": {"id": "server-a", "host": "127.0.0.1", "port": 22, "ssh_user": "u"},
                "gpus": [{"id": "gpu-a", "gpu_uuid": "GPU-a", "gpu_index": 0}],
                "cuda_visible_devices": "GPU-a",
                "commitment": {"cpu_cores": 8.0, "memory_mib": 32768},
            }
        ],
    }


def test_wait_for_claim_returns_held_or_active_matching_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        [[_request("QUEUED")], [_request("LEASED")]],
        [[], [_lease("HELD")]],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: client)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = mcp_server.gpu_wait_for_claim(
        "agent-a",
        "req-1",
        timeout_seconds=5,
        poll_interval_seconds=0.1,
    )

    assert result["state"] == "allocated"
    assert result["request"]["state"] == "LEASED"
    assert result["lease"]["state"] == "HELD"
    assert result["polls"] == 2
    assert sleeps == [pytest.approx(0.1)]
    assert client.calls == [
        ("GET", "/api/v1/requests", None),
        ("GET", "/api/v1/leases", None),
        ("GET", "/api/v1/requests", None),
        ("GET", "/api/v1/leases", None),
    ]


def test_wait_for_claim_returns_terminal_request_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([[_request("CANCELLED")]], [[]])
    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: client)

    result = mcp_server.gpu_wait_for_claim("agent-a", "req-1")

    assert result["state"] == "terminal"
    assert result["request"]["state"] == "CANCELLED"
    assert result["lease"] is None
    assert client.calls == [
        ("GET", "/api/v1/requests", None),
        ("GET", "/api/v1/leases", None),
    ]


def test_wait_for_claim_timeout_keeps_last_request_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([[_request("QUEUED")]], [[]])
    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: client)

    result = mcp_server.gpu_wait_for_claim("agent-a", "req-1", timeout_seconds=0)

    assert result["state"] == "timeout"
    assert result["request"]["state"] == "QUEUED"
    assert result["lease"] is None
    assert result["polls"] == 1
    assert client.calls == [
        ("GET", "/api/v1/requests", None),
        ("GET", "/api/v1/leases", None),
    ]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"agent_name": " ", "request_id": "req-1"}, "must not be empty"),
        ({"agent_name": "agent-a", "request_id": " "}, "must not be empty"),
        ({"agent_name": "agent-a", "request_id": "req-1", "timeout_seconds": 301}, "between"),
        (
            {"agent_name": "agent-a", "request_id": "req-1", "poll_interval_seconds": 0},
            "between",
        ),
    ],
)
def test_wait_for_claim_validates_bounded_inputs(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        mcp_server.gpu_wait_for_claim(**kwargs)


def test_wait_for_claim_is_exposed_as_mcp_tool() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "gpu_wait_for_claim" in by_name
    assert {"agent_name", "request_id"}.issubset(
        by_name["gpu_wait_for_claim"].inputSchema["required"]
    )
    assert "timeout_seconds" not in by_name["gpu_wait_for_claim"].inputSchema["required"]
    assert "poll_interval_seconds" not in by_name["gpu_wait_for_claim"].inputSchema["required"]
