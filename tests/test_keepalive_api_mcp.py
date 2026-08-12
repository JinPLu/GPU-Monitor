from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from serverpilot import API_CAPABILITIES
from serverpilot import mcp_server
from serverpilot.adapters import AdapterCommandError
from serverpilot.api import create_app
from serverpilot.config import InventoryConfig, Settings
from serverpilot.keepalive_protocol import (
    KeepaliveGPUResult,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
)
from serverpilot.mcp_server import mcp
from serverpilot.schemas import LeaseObservedBind, RequestCreate
from tests.helpers import observation, process_for_gpu


GPU_UUIDS = (
    "GPU-00000000-0000-0000-0000-000000000001",
    "GPU-00000000-0000-0000-0000-000000000002",
)


class FakeKeepaliveAdapter:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, bool, tuple[str, ...]]] = []
        self.active_pids: dict[str, int] = {}

    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        requested = tuple(gpu_uuids)
        self.calls.append((endpoint.id, enabled, requested))
        if self.failure is not None:
            raise self.failure
        results: list[KeepaliveGPUResult] = []
        for index, gpu_uuid in enumerate(requested, start=1):
            if enabled:
                pid = self.active_pids.setdefault(gpu_uuid, 4_000 + index)
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="running",
                        outcome="started",
                        worker=KeepaliveWorkerAttestation(pid=pid, start_ticks=999),
                    )
                )
            else:
                existed = gpu_uuid in self.active_pids
                self.active_pids.pop(gpu_uuid, None)
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="stopped",
                        outcome="stopped" if existed else "unchanged",
                        worker=None,
                    )
                )
        return KeepaliveResponse(enabled=enabled, results=tuple(results))


class PartiallyFailingStopAdapter(FakeKeepaliveAdapter):
    def __init__(self, fail_gpu_uuid: str) -> None:
        super().__init__()
        self.fail_gpu_uuid = fail_gpu_uuid

    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        if not enabled and gpu_uuids == [self.fail_gpu_uuid]:
            self.calls.append((endpoint.id, enabled, tuple(gpu_uuids)))
            raise AdapterCommandError("one GPU stop failed", uncertain=True)
        return await super().set_enabled(endpoint, enabled, gpu_uuids)


class FakeTargetedCollector:
    def __init__(
        self,
        adapter: FakeKeepaliveAdapter,
        *,
        fail: bool = False,
        unmanaged_gpu_uuids: tuple[str, ...] = (),
    ) -> None:
        self.adapter = adapter
        self.fail = fail
        self.unmanaged_gpu_uuids = unmanaged_gpu_uuids
        self.calls: list[tuple[list[str], int]] = []

    def processes(self) -> list:  # type: ignore[type-arg]
        keepers = [
            process_for_gpu(gpu_uuid, pid=pid)
            for gpu_uuid, pid in sorted(self.adapter.active_pids.items())
        ]
        foreign = [
            process_for_gpu(gpu_uuid, pid=8_000 + index)
            for index, gpu_uuid in enumerate(self.unmanaged_gpu_uuids, start=1)
        ]
        return [*keepers, *foreign]

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
        value = service.ingest_observation(
            observation(
                endpoint_ids[0],
                count=len(GPU_UUIDS),
                gpu_uuids=list(GPU_UUIDS),
                processes=self.processes(),
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
    configured.endpoints[0].expected_gpu_count = len(GPU_UUIDS)
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
    app.state.service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=collector.processes(),
            observed_at=datetime.now(UTC),
        )
    )
    return app, resolved


def _headers(key: str) -> dict[str, str]:
    return {"X-ServerPilot-Actor": "agent-a", "Idempotency-Key": key}


def test_keepalive_api_sets_desired_policy_and_reconciles_each_eligible_gpu(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, resolved = _keepalive_app(
        tmp_path, inventory, adapter=adapter, collector=collector
    )
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("keep-on"),
    )

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["keepalive"] == {
        "endpoint_id": "endpoint-a",
        "enabled": True,
        "policy": "idle_keepalive",
        "state": "ACTIVE",
        "configured": True,
        "active_gpu_count": 1,
        "starting_gpu_count": 0,
        "error_gpu_count": 0,
        "legacy_gpu_count": 0,
        "eligible_idle_gpu_count": 0,
        "reasons": ["keepalive startup grace has not completed"],
    }
    serialized = enabled.text.lower()
    assert "lease_id" not in serialized
    assert "pid" not in serialized
    assert "gpu_uuid" not in serialized
    assert adapter.calls == [("endpoint-a", True, (GPU_UUIDS[0],))]
    assert collector.calls == [(["endpoint-a"], 1)]
    assert resolved == ["server-script-v1"]

    disabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers=_headers("keep-off"),
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["keepalive"] == {
        "endpoint_id": "endpoint-a",
        "enabled": False,
        "policy": "disabled",
        "state": "OFF",
        "configured": True,
        "active_gpu_count": 0,
        "starting_gpu_count": 0,
        "error_gpu_count": 0,
        "legacy_gpu_count": 0,
        "eligible_idle_gpu_count": 0,
    }
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))
    assert collector.calls[-1] == (["endpoint-a"], 1)


def test_keepalive_api_starts_sibling_when_one_gpu_has_workload_conflict(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    claimed = service.create_request(
        actor,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "conflict-before-keepalive",
                "purpose": "test one-GPU isolation",
                "duration_seconds": 600,
                "constraints": {"gpu_count": 1, "placement": "pack"},
            }
        ),
        idempotency_key="conflict-before-keepalive-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    started_at = datetime.now(UTC)
    initial = process_for_gpu(GPU_UUIDS[0]).model_copy(update={"process_started_at": started_at})
    service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=[initial],
            observed_at=datetime.now(UTC),
        )
    )
    service.bind_observed_workload(
        actor,
        lease_id,
        LeaseObservedBind(run_id="conflict-before-keepalive-run"),
        idempotency_key="conflict-before-keepalive-bind",
    )
    replacement = initial.model_copy(update={"process_started_at": started_at + timedelta(seconds=10)})
    # A materially changed process identity on the same GPU is observed twice,
    # which is the service's conflict threshold.
    service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=[replacement],
            observed_at=datetime.now(UTC),
        )
    )
    service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=[replacement],
            observed_at=datetime.now(UTC),
        )
    )

    enabled = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("conflict-sibling-keepalive"),
    )

    assert enabled.status_code == 200, enabled.text
    assert adapter.calls == [("endpoint-a", True, (GPU_UUIDS[1],))]
    assert enabled.json()["keepalive"]["active_gpu_count"] == 1


def test_keepalive_api_disable_without_managed_coverage_never_targets_foreign_gpu(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[0],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers=_headers("stop-foreign"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["keepalive"]["policy"] == "disabled"
    assert adapter.calls == []
    assert collector.calls == []


def test_endpoint_operator_can_clear_empty_internal_keepalive_lease(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor, "endpoint-a", "idle_keepalive", idempotency_key="cleanup-policy-on"
    )
    begun = service.begin_keepalive(
        actor,
        "endpoint-a",
        "endpoint-a:GPU-00000000-0000-0000-0000-000000000001",
        idempotency_key="cleanup-begin",
    )
    lease_id = str(begun["keepalive"]["lease_id"])
    service.configure_keepalive_policy(
        actor, "endpoint-a", "disabled", idempotency_key="cleanup-policy-off"
    )

    response = TestClient(app).post(
        f"/api/v1/endpoints/endpoint-a/leases/{lease_id}/release-empty",
        headers=_headers("cleanup-empty-keepalive"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["released"] is True
    assert response.json()["lease"]["kind"] == "keepalive"
    assert collector.calls[-1] == (["endpoint-a"], 1)


def test_keepalive_stop_releases_empty_sibling_when_another_gpu_stop_is_uncertain(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = PartiallyFailingStopAdapter(GPU_UUIDS[1])
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("partial-stop-on"),
    )
    assert enabled.status_code == 200, enabled.text

    disabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers=_headers("partial-stop-off"),
    )
    assert disabled.status_code == 409, disabled.text
    assert disabled.json()["error"]["code"] == "keepalive_partial_stop"
    assert adapter.calls[-2:] == [
        ("endpoint-a", False, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[1],)),
    ]
    snapshot = client.get(
        "/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}
    ).json()["data"]
    states_by_uuid = {gpu["gpu_uuid"]: gpu["state"] for gpu in snapshot["gpus"]}
    assert states_by_uuid[GPU_UUIDS[0]] == "AVAILABLE"
    assert states_by_uuid[GPU_UUIDS[1]] in {"KEEPALIVE", "CONFLICT"}


def test_keepalive_api_missing_endpoint_does_not_resolve_adapter_or_collect(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    response = TestClient(app).post(
        "/api/v1/endpoints/missing/keepalive",
        json={"enabled": False},
        headers=_headers("missing-stop"),
    )

    assert response.status_code == 404
    assert adapter.calls == []
    assert collector.calls == []


def test_keepalive_api_strict_body_and_mutation_headers(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)
    path = "/api/v1/endpoints/endpoint-a/keepalive"
    assert client.post(path, json={"enabled": True}).status_code == 422
    invalid = client.post(path, json={"enabled": True, "gpu_uuids": [GPU_UUIDS[0]]}, headers=_headers("strict"))
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    policy = client.post(path, json={"policy": "idle_keepalive"}, headers=_headers("strict-policy"))
    assert policy.status_code == 422
    assert policy.json()["error"]["code"] == "validation_error"
    generic_patch = client.patch(
        "/api/v1/endpoints/endpoint-a",
        json={"keepalive_policy": "idle_keepalive"},
        headers=_headers("strict-generic-patch"),
    )
    assert generic_patch.status_code == 422
    assert generic_patch.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("failure_kind", ["adapter", "collector"])
def test_keepalive_api_failures_leave_policy_and_ownership_fail_closed(
    tmp_path: Path, inventory: InventoryConfig, failure_kind: str
) -> None:
    adapter = FakeKeepaliveAdapter(
        failure=AdapterCommandError("remote secret", uncertain=True)
        if failure_kind == "adapter"
        else None
    )
    collector = FakeTargetedCollector(adapter, fail=failure_kind == "collector")
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers(failure_kind),
    )

    assert response.status_code == 503
    assert "remote secret" not in response.text
    summary = app.state.service.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]
    assert summary["policy"] == "idle_keepalive"
    assert summary["starting_gpu_count"] == len(GPU_UUIDS)


def test_keepalive_api_exposes_public_reconcile_hook(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor, "endpoint-a", "idle_keepalive", idempotency_key="hook-policy"
    )

    result = asyncio.run(
        app.state.reconcile_endpoint_keepalive(actor, "endpoint-a", idempotency_key="hook")
    )

    assert result["keepalive"]["active_gpu_count"] == len(GPU_UUIDS)
    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", True, (GPU_UUIDS[1],)),
    ]


def test_immediate_claim_reclaims_only_the_selected_verified_keeper_gpu(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("claim-keepers-on"),
    )
    assert enabled.status_code == 200, enabled.text
    assert adapter.calls == [("endpoint-a", True, (GPU_UUIDS[0],))]

    claim_payload = {
        "project_id": "project-a",
        "task_ref": "claim-one-keeper",
        "purpose": "claim one keeper GPU",
        "constraints": {"gpu_count": 1},
    }
    service = app.state.service
    original_create_request = service.create_request
    create_snapshots: list[list[tuple[str, bool, tuple[str, ...]]]] = []

    def observed_create_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        create_snapshots.append(list(adapter.calls))
        return original_create_request(*args, **kwargs)

    service.create_request = observed_create_request  # type: ignore[method-assign]
    claimed = client.post(
        "/api/v1/claims",
        json=claim_payload,
        headers=_headers("claim-one-keeper"),
    )

    assert claimed.status_code == 200, claimed.text
    claimed_gpu_ids = claimed.json()["lease"]["gpu_ids"]
    assert len(claimed_gpu_ids) == 1
    # The reclaim plan chose exactly one currently verified keeper, and the
    # helper never received the sibling GPU as a stop target.
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))
    # The retry happens while the endpoint reconcile lock is still held. Its
    # observation sees only the targeted stop; no policy-driven start can fit
    # between that stop/fresh-finalization path and ordinary admission.
    assert create_snapshots == [
        [("endpoint-a", True, (GPU_UUIDS[0],))],
        [
            ("endpoint-a", True, (GPU_UUIDS[0],)),
            ("endpoint-a", False, (GPU_UUIDS[0],)),
        ],
    ]
    snapshot = client.get(
        "/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}
    ).json()["data"]
    states_by_uuid = {gpu["gpu_uuid"]: gpu["state"] for gpu in snapshot["gpus"]}
    assert states_by_uuid[GPU_UUIDS[0]] == "HELD"
    assert states_by_uuid[GPU_UUIDS[1]] == "BUSY_UNMANAGED"

    repeated = client.post(
        "/api/v1/claims",
        json=claim_payload,
        headers=_headers("claim-one-keeper"),
    )
    assert repeated.status_code == 200
    assert repeated.json() == claimed.json()
    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
    ]


def test_profile_claim_reclaims_only_its_selected_verified_keeper_gpu(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)
    service = app.state.service

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("profile-keepers-on"),
    )
    assert enabled.status_code == 200, enabled.text
    profile = {
        "id": "project-a-default-gpu",
        "project_id": "project-a",
        "display_name": "Default GPU",
        "purpose": "default project GPU task",
        "duration_seconds": 3600,
        "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
        "enabled": True,
    }
    created = client.post(
        "/api/v1/workload-profiles",
        json=profile,
        headers=_headers("profile-upsert"),
    )
    assert created.status_code == 200, created.text

    claim_payload = {"task_ref": "profile-claim-one-keeper"}
    original_profile_claim = service.claim_workload_profile
    claim_snapshots: list[list[tuple[str, bool, tuple[str, ...]]]] = []

    def observed_profile_claim(*args, **kwargs):  # type: ignore[no-untyped-def]
        claim_snapshots.append(list(adapter.calls))
        return original_profile_claim(*args, **kwargs)

    service.claim_workload_profile = observed_profile_claim  # type: ignore[method-assign]
    claimed = client.post(
        "/api/v1/workload-profiles/project-a-default-gpu/claim",
        json=claim_payload,
        headers=_headers("profile-claim-one-keeper"),
    )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["request"]["profile_id"] == "project-a-default-gpu"
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))
    assert claim_snapshots == [
        [("endpoint-a", True, (GPU_UUIDS[0],))],
        [
            ("endpoint-a", True, (GPU_UUIDS[0],)),
            ("endpoint-a", False, (GPU_UUIDS[0],)),
        ],
    ]

    repeated = client.post(
        "/api/v1/workload-profiles/project-a-default-gpu/claim",
        json=claim_payload,
        headers=_headers("profile-claim-one-keeper"),
    )
    assert repeated.status_code == 200
    assert repeated.json() == claimed.json()
    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
    ]


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
            return {
                "keepalive": {
                    "enabled": body["enabled"],
                    "policy": "idle_keepalive" if body["enabled"] else "disabled",
                    "active_gpu_count": 1,
                }
            }

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
    instructions = mcp_server.MCP_INSTRUCTIONS.lower()
    assert "routine gpu work" in instructions
    assert "keepalive" in instructions
