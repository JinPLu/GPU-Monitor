from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from serverpilot import API_CAPABILITIES, mcp_server
from serverpilot.adapters import AdapterCommandError
from serverpilot.api import create_app
from serverpilot.config import InventoryConfig, Settings
from serverpilot.keepalive_protocol import (
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
)
from serverpilot.mcp_server import mcp
from tests.helpers import observation, process_for_gpu


class FakeKeepaliveAdapter:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        attested_pid: int = 4321,
    ) -> None:
        self.failure = failure
        self.attested_pid = attested_pid
        self.calls: list[tuple[str, bool]] = []

    async def set_enabled(self, endpoint, enabled: bool) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        self.calls.append((endpoint.id, enabled))
        if self.failure is not None:
            raise self.failure
        worker = (
            KeepaliveWorkerAttestation(pid=self.attested_pid, start_ticks=999)
            if enabled
            else None
        )
        return KeepaliveResponse(
            enabled=enabled,
            changed=True,
            status="running" if enabled else "stopped",
            worker=worker,
        )


class FakeTargetedCollector:
    def __init__(
        self,
        *,
        fail: bool = False,
        observed_pid: int = 4321,
        extra_observed_pids: tuple[int, ...] = (),
        identity_metadata_available: bool = True,
    ) -> None:
        self.fail = fail
        self.observed_pid = observed_pid
        self.extra_observed_pids = extra_observed_pids
        self.identity_metadata_available = identity_metadata_available
        self.calls: list[tuple[list[str], int]] = []
        self.enabled = True

    async def collect_once(
        self,
        service,
        *,
        concurrency: int = 5,
        endpoints=None,
        stagger_seconds: float = 0.0,
    ):  # type: ignore[no-untyped-def]
        assert endpoints is not None
        endpoint_ids = [endpoint.id for endpoint in endpoints]
        self.calls.append((endpoint_ids, concurrency))
        if self.fail:
            return {endpoint_ids[0]: {"error": "FakeFailure"}}
        gpu_uuid = "GPU-endpoint-a-0"
        processes = (
            [
                process_for_gpu(gpu_uuid, pid=pid)
                for pid in (self.observed_pid, *self.extra_observed_pids)
            ]
            if self.enabled
            else []
        )
        if processes and not self.identity_metadata_available:
            processes = [
                process.model_copy(update={"username": None, "executable": "[Not Found]"})
                for process in processes
            ]
        value = service.ingest_observation(
            observation(
                endpoint_ids[0],
                count=1,
                processes=processes,
                observed_at=datetime.now(UTC),
            )
        )
        return {endpoint_ids[0]: value}


def _keepalive_app(
    tmp_path: Path,
    inventory: InventoryConfig,
    *,
    adapter: FakeKeepaliveAdapter,
    collector: FakeTargetedCollector,
):
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = False
    configured.endpoints[0].keepalive_adapter_id = "server-script-v1"
    configured.endpoints[0].expected_gpu_count = 1
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(configured.model_dump(mode="json")), encoding="utf-8"
    )
    resolved: list[str] = []

    def resolve(adapter_id: str):  # type: ignore[no-untyped-def]
        resolved.append(adapter_id)
        return adapter

    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'keepalive-api.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="k" * 32,
        ),
        collector=collector,  # type: ignore[arg-type]
        keepalive_adapter_resolver=resolve,
    )
    app.state.service.ingest_observation(observation(count=1))
    return app, resolved


def test_keepalive_api_enable_disable_is_targeted_and_hides_attestation(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector()
    app, resolved = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": "keep-on"}

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["keepalive"] == {
        "endpoint_id": "endpoint-a",
        "enabled": True,
        "state": "ACTIVE",
    }
    serialized = enabled.text.lower()
    assert "lease_id" not in serialized
    assert "pid" not in serialized
    assert "start_ticks" not in serialized
    assert adapter.calls == [("endpoint-a", True)]
    assert collector.calls == [(["endpoint-a"], 1)]
    assert resolved == ["server-script-v1"]

    collector.enabled = False
    disabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers={**headers, "Idempotency-Key": "keep-off"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["keepalive"] == {
        "endpoint_id": "endpoint-a",
        "enabled": False,
        "state": "OFF",
    }
    assert "lease_id" not in disabled.text.lower()
    assert adapter.calls[-1] == ("endpoint-a", False)
    assert collector.calls[-1] == (["endpoint-a"], 1)

    repeated = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers={**headers, "Idempotency-Key": "keep-off-again"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["keepalive"]["state"] == "OFF"
    assert adapter.calls == [
        ("endpoint-a", True),
        ("endpoint-a", False),
        ("endpoint-a", False),
    ]
    assert collector.calls == [
        (["endpoint-a"], 1),
        (["endpoint-a"], 1),
        (["endpoint-a"], 1),
    ]


def test_keepalive_api_accepts_namespace_translated_worker_pid(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter(attested_pid=3_331_894)
    collector = FakeTargetedCollector(
        observed_pid=3_331_894,
        identity_metadata_available=False,
    )
    app, _ = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers={
            "X-ServerPilot-Actor": "agent-a",
            "Idempotency-Key": "namespace-translated",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["keepalive"]["state"] == "ACTIVE"


def test_keepalive_api_rejects_additional_namespace_hidden_pid(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter(attested_pid=3_331_894)
    collector = FakeTargetedCollector(
        observed_pid=3_331_894,
        extra_observed_pids=(8_888_888,),
        identity_metadata_available=False,
    )
    app, _ = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers={
            "X-ServerPilot-Actor": "agent-a",
            "Idempotency-Key": "namespace-hidden-foreign",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "keepalive_foreign_process"


def test_keepalive_api_disable_without_lease_refuses_residual_process(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector()
    collector.enabled = True
    app, _ = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers={"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": "stop-orphan"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "keepalive_process_still_running"
    assert adapter.calls == [("endpoint-a", False)]
    assert collector.calls == [(["endpoint-a"], 1)]


def test_keepalive_api_disable_missing_endpoint_is_404_without_remote_call(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector()
    app, _ = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )

    response = TestClient(app).post(
        "/api/v1/endpoints/missing/keepalive",
        json={"enabled": False},
        headers={"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": "missing-stop"},
    )

    assert response.status_code == 404
    assert adapter.calls == []
    assert collector.calls == []


def test_keepalive_api_strict_body_and_mutation_headers(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    app, _ = _keepalive_app(
        tmp_path,
        inventory,
        adapter=FakeKeepaliveAdapter(),
        collector=FakeTargetedCollector(),
    )
    client = TestClient(app)
    path = "/api/v1/endpoints/endpoint-a/keepalive"
    assert client.post(path, json={"enabled": True}).status_code == 422
    invalid = client.post(
        path,
        json={"enabled": True, "pid": 1},
        headers={"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": "strict"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("failure_kind", ["adapter", "collector"])
def test_keepalive_api_failures_leave_ownership_fail_closed(
    tmp_path: Path, inventory: InventoryConfig, failure_kind: str
) -> None:
    adapter = FakeKeepaliveAdapter(
        failure=(
            AdapterCommandError("remote secret", uncertain=True)
            if failure_kind == "adapter"
            else None
        )
    )
    collector = FakeTargetedCollector(fail=failure_kind == "collector")
    app, _ = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )
    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers={"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": failure_kind},
    )
    assert response.status_code == 503
    assert "remote secret" not in response.text
    prepared = app.state.service.prepare_keepalive_stop(
        app.state.service.local_actor("agent-a"), "endpoint-a"
    )
    assert prepared["keepalive"]["lease_id"] is not None
    assert prepared["keepalive"]["state"] == "HELD"


def test_keepalive_capability_and_mcp_schema_and_delegation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert "endpoint_keepalive" in API_CAPABILITIES
    tools = asyncio.run(mcp.list_tools())
    tool = next(item for item in tools if item.name == "gpu_set_keepalive")
    assert set(tool.inputSchema["required"]) == {
        "agent_name",
        "server_id",
        "enabled",
        "approval_ref",
        "idempotency_key",
    }

    calls = []

    class FakeClient:
        def post(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append((path, body, idempotency_key))
            return {"keepalive": {"enabled": body["enabled"], "state": "ACTIVE"}}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())
    with pytest.raises(ValueError, match="approval_ref"):
        mcp_server.gpu_set_keepalive("agent", "endpoint-a", True, "", "stable")
    with pytest.raises(ValueError, match="idempotency_key"):
        mcp_server.gpu_set_keepalive("agent", "endpoint-a", True, "approved", "")
    result = mcp_server.gpu_set_keepalive(
        "agent", "endpoint-a", False, "approved-task", "stable-key"
    )
    assert result["keepalive"]["enabled"] is False
    assert calls == [
        (
            "/api/v1/endpoints/endpoint-a/keepalive",
            {"enabled": False},
            "stable-key",
        )
    ]
    assert "never an automatic" in mcp_server.MCP_INSTRUCTIONS.lower()
