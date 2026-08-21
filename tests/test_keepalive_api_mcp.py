from __future__ import annotations

import asyncio
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot import API_CAPABILITIES
from serverpilot import mcp_server
from serverpilot.adapters import AdapterCommandError
from serverpilot.api import (
    _keepalive_adapter_failure_code,
    _public_error_message,
    _public_keepalive_result,
    create_app,
)
from serverpilot.config import InventoryConfig, Settings
from serverpilot.keepalive_protocol import (
    KEEPALIVE_WORKER_MARKER,
    KeepaliveAttestationResponse,
    KeepaliveGPUResult,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
)
from serverpilot.mcp_server import mcp
from serverpilot.models import Lease
from serverpilot.schemas import LeaseObservedBind, RequestCreate
from serverpilot.service import BrokerError
from tests.helpers import observation, process_for_gpu


GPU_UUIDS = (
    "GPU-00000000-0000-0000-0000-000000000001",
    "GPU-00000000-0000-0000-0000-000000000002",
)
EIGHT_GPU_UUIDS = tuple(f"GPU-00000000-0000-0000-0000-{index:012d}" for index in range(1, 9))


class FakeKeepaliveAdapter:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, bool, tuple[str, ...]]] = []
        self.attest_calls: list[tuple[str, tuple[str, ...]]] = []
        self.active_pids: dict[str, int] = {}
        self.attested_pids: dict[str, int] = {}
        self.driver_pids: dict[str, int] = {}
        self._next_driver_pid = 4_000

    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        requested = tuple(gpu_uuids)
        self.calls.append((endpoint.id, enabled, requested))
        if self.failure is not None:
            raise self.failure
        results: list[KeepaliveGPUResult] = []
        for gpu_uuid in requested:
            if enabled:
                if gpu_uuid not in self.active_pids:
                    # One helper worker has two relevant identities: a PID in
                    # the helper namespace and the NVIDIA-driver PID which
                    # the collector sees.  A new worker gets fresh values for
                    # both; retaining an old driver PID after a stop would
                    # incorrectly model a valid post-release restore as an
                    # attestation mismatch.
                    self._next_driver_pid += 1
                    driver_pid = self._next_driver_pid
                    self.active_pids[gpu_uuid] = driver_pid
                    self.driver_pids[gpu_uuid] = driver_pid
                    self.attested_pids[gpu_uuid] = 100_000 + driver_pid
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="running",
                        outcome="started",
                    )
                )
            else:
                existed = gpu_uuid in self.active_pids
                self.active_pids.pop(gpu_uuid, None)
                self.attested_pids.pop(gpu_uuid, None)
                self.driver_pids.pop(gpu_uuid, None)
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="stopped",
                        outcome="stopped" if existed else "unchanged",
                    )
                )
        return KeepaliveResponse(enabled=enabled, results=tuple(results))

    async def attest_workers(self, endpoint, gpu_uuids: list[str]) -> KeepaliveAttestationResponse:  # type: ignore[no-untyped-def]
        requested = tuple(gpu_uuids)
        self.attest_calls.append((endpoint.id, requested))
        workers: list[KeepaliveWorkerAttestation] = []
        for gpu_uuid in requested:
            pid = self.attested_pids.get(gpu_uuid, self.active_pids.get(gpu_uuid))
            if pid is None:
                raise AdapterCommandError("missing fake keepalive worker")
            driver_pid = self.driver_pids.get(gpu_uuid, self.active_pids.get(gpu_uuid))
            if driver_pid is None:
                raise AdapterCommandError("missing fake keepalive driver worker")
            workers.append(
                KeepaliveWorkerAttestation(
                    gpu_uuid=gpu_uuid,
                    pid=pid,
                    driver_pid=driver_pid,
                    boot_id=f"boot-{endpoint.id}",
                    start_time_ticks=100_000 + pid,
                    worker_marker=KEEPALIVE_WORKER_MARKER,
                )
            )
        return KeepaliveAttestationResponse(workers=tuple(workers))


class PartiallyFailingStopAdapter(FakeKeepaliveAdapter):
    def __init__(self, fail_gpu_uuid: str) -> None:
        super().__init__()
        self.fail_gpu_uuid = fail_gpu_uuid

    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        if not enabled and gpu_uuids == [self.fail_gpu_uuid]:
            self.calls.append((endpoint.id, enabled, tuple(gpu_uuids)))
            raise AdapterCommandError("one GPU stop failed", uncertain=True)
        return await super().set_enabled(endpoint, enabled, gpu_uuids)


class PartiallyStartingBatchAdapter(FakeKeepaliveAdapter):
    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        requested = tuple(gpu_uuids)
        self.calls.append((endpoint.id, enabled, requested))
        if enabled:
            self.active_pids[requested[0]] = 9_001
            raise AdapterCommandError("batch start failed after first GPU", uncertain=True)
        for gpu_uuid in requested:
            self.active_pids.pop(gpu_uuid, None)
        return KeepaliveResponse(
            enabled=False,
            results=tuple(
                KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid,
                    status="stopped",
                    outcome="stopped" if index == 0 else "unchanged",
                )
                for index, gpu_uuid in enumerate(requested)
            ),
        )


class FakeTargetedCollector:
    def __init__(
        self,
        adapter: FakeKeepaliveAdapter,
        *,
        fail: bool = False,
        unmanaged_gpu_uuids: tuple[str, ...] = (),
        gpu_uuids: tuple[str, ...] = GPU_UUIDS,
    ) -> None:
        self.adapter = adapter
        self.fail = fail
        self.unmanaged_gpu_uuids = unmanaged_gpu_uuids
        self.gpu_uuids = gpu_uuids
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
                count=len(self.gpu_uuids),
                gpu_uuids=list(self.gpu_uuids),
                processes=self.processes(),
                observed_at=datetime.now(UTC),
            )
        )
        return {endpoint_ids[0]: value}


class BlockingKeepaliveAdapter(FakeKeepaliveAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.cleaned = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._release: asyncio.Future[None] | None = None

    async def set_enabled(self, endpoint, enabled: bool, gpu_uuids: list[str]) -> KeepaliveResponse:  # type: ignore[no-untyped-def]
        requested = tuple(gpu_uuids)
        self.calls.append((endpoint.id, enabled, requested))
        if enabled:
            for index, gpu_uuid in enumerate(requested, start=1):
                self.active_pids[gpu_uuid] = 7_000 + index
            self._loop = asyncio.get_running_loop()
            self._release = self._loop.create_future()
            self.started.set()
            await self._release
            raise AdapterCommandError("blocked start failed", uncertain=True)
        for gpu_uuid in requested:
            self.active_pids.pop(gpu_uuid, None)
        self.cleaned.set()
        return KeepaliveResponse(
            enabled=False,
            results=tuple(
                KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid,
                    status="stopped",
                    outcome="unchanged",
                )
                for gpu_uuid in requested
            ),
        )

    def release(self) -> None:
        assert self._loop is not None
        assert self._release is not None
        self._loop.call_soon_threadsafe(
            lambda: (
                self._release is not None
                and not self._release.done()
                and self._release.set_result(None)
            )
        )


class PeriodicFakeCollector:
    def __init__(self) -> None:
        self.calls = 0
        self.call_options: list[tuple[int, float]] = []
        self.second_collection = threading.Event()

    async def collect_once(
        self,
        service,
        *,
        concurrency: int = 5,
        endpoints=None,
        stagger_seconds: float = 0.0,
    ):  # type: ignore[no-untyped-def]
        assert endpoints is not None
        self.calls += 1
        self.call_options.append((concurrency, stagger_seconds))
        results = {
            endpoint.id: service.ingest_observation(
                observation(
                    endpoint.id,
                    count=1,
                    gpu_uuids=[GPU_UUIDS[0]],
                    observed_at=datetime.now(UTC),
                )
            )
            for endpoint in endpoints
        }
        if self.calls >= 2:
            self.second_collection.set()
        return results


class SchedulingCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int, float]] = []
        self.collected = threading.Event()

    async def collect_once(
        self,
        _service,
        *,
        concurrency: int = 5,
        endpoints=None,
        stagger_seconds: float = 0.0,
    ):  # type: ignore[no-untyped-def]
        assert endpoints is not None
        endpoint_ids = [endpoint.id for endpoint in endpoints]
        self.calls.append((endpoint_ids, concurrency, stagger_seconds))
        self.collected.set()
        return {endpoint_id: {"error": "Skipped"} for endpoint_id in endpoint_ids}


class WorkloadConflictCollector(FakeTargetedCollector):
    def __init__(self, adapter: FakeKeepaliveAdapter) -> None:
        super().__init__(adapter)
        self.conflict_created = False

    async def collect_once(
        self,
        service,
        *,
        concurrency: int = 5,
        endpoints=None,
        stagger_seconds: float = 0.0,
    ):  # type: ignore[no-untyped-def]
        if self.adapter.active_pids and not self.conflict_created:
            second_gpu_id = f"endpoint-a:{GPU_UUIDS[1]}"
            service.create_request(
                service.local_actor("agent-a"),
                RequestCreate.model_validate(
                    {
                        "project_id": "project-a",
                        "task_ref": "keepalive-batch-second-gpu-conflict",
                        "purpose": "create a legal conflict after the transition plan",
                        "duration_seconds": 600,
                        "constraints": {
                            "gpu_count": 1,
                            "placement": "exact",
                            "gpu_ids": [second_gpu_id],
                        },
                    }
                ),
                idempotency_key="keepalive-batch-second-gpu-conflict",
                activate_if_allocated=True,
            )
            self.conflict_created = True
        return await super().collect_once(
            service,
            concurrency=concurrency,
            endpoints=endpoints,
            stagger_seconds=stagger_seconds,
        )


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
    configured.endpoints[0].expected_gpu_count = len(collector.gpu_uuids)
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(configured.model_dump(mode="json")), encoding="utf-8")
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
            count=len(collector.gpu_uuids),
            gpu_uuids=list(collector.gpu_uuids),
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
    app, resolved = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
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
        "desired": "ON",
        "actual": "ON",
        "state": "ON",
        "configured": True,
        "active_gpu_count": 1,
        "error_gpu_count": 0,
        "eligible_idle_gpu_count": 0,
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
        "desired": "OFF",
        "actual": "OFF",
        "state": "OFF",
        "configured": True,
        "active_gpu_count": 0,
        "error_gpu_count": 0,
        "eligible_idle_gpu_count": 0,
    }
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))
    assert collector.calls[-1] == (["endpoint-a"], 1)


def test_periodic_collection_does_not_wait_or_queue_duplicate_keepalive_reconcile(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = True
    configured.collector.interval_seconds = 1
    configured.collector.stale_after_seconds = 3
    configured.endpoints = [configured.endpoints[0]]
    configured.endpoints[0].keepalive_adapter_id = "server-script-v1"
    configured.endpoints[0].keepalive_policy = "idle_keepalive"
    configured.endpoints[0].expected_gpu_count = 1
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(configured.model_dump_json(), encoding="utf-8")
    adapter = BlockingKeepaliveAdapter()
    collector = PeriodicFakeCollector()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'periodic-collector.sqlite3'}",
            inventory_path=inventory_path,
            project_root=Path(__file__).resolve().parents[1],
            session_secret="test-secret",
        ),
        collector=collector,  # type: ignore[arg-type]
        keepalive_adapter_resolver=lambda _adapter_id: adapter,
    )

    with TestClient(app):
        assert adapter.started.wait(timeout=2)
        assert collector.second_collection.wait(timeout=2)
        assert collector.call_options[:2] == [(5, 0.0), (5, 0.0)]
        assert adapter.calls == [("endpoint-a", True, (GPU_UUIDS[0],))]
        adapter.release()
        assert adapter.cleaned.wait(timeout=2)

    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
    ]


def test_routine_claim_waits_for_inflight_keeper_start_on_same_endpoint(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = BlockingKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            toggle_task = asyncio.create_task(
                client.post(
                    "/api/v1/endpoints/endpoint-a/keepalive",
                    json={"enabled": True},
                    headers=_headers("blocked-start-race"),
                )
            )
            assert await asyncio.to_thread(adapter.started.wait, 2)
            claim_task = asyncio.create_task(
                client.post(
                    "/api/v1/routine/claims",
                    json={
                        "project_id": "project-a",
                        "task_ref": "claim-during-keeper-start",
                        "purpose": "verify keeper start and Agent claim are serialized",
                        "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
                    },
                    headers={"X-ServerPilot-Actor": "agent-a"},
                )
            )
            await asyncio.sleep(0.2)
            assert not claim_task.done()
            adapter.release()
            return await toggle_task, await claim_task

    toggle, claim = asyncio.run(scenario())
    assert toggle.status_code == 503
    assert claim.status_code == 200
    assert claim.json()["lease"]["gpu_ids"] == [
        "endpoint-a:GPU-00000000-0000-0000-0000-000000000001"
    ]


def test_periodic_collection_starts_four_endpoints_together_with_existing_limit(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = True
    configured.collector.interval_seconds = 1
    configured.collector.stale_after_seconds = 3
    base_endpoint = configured.endpoints[0]
    configured.endpoints = [
        base_endpoint.model_copy(
            update={
                "id": f"endpoint-{suffix}",
                "host": f"gpu-{suffix}.example.test",
            }
        )
        for suffix in ("a", "b", "c", "d")
    ]
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(configured.model_dump_json(), encoding="utf-8")
    collector = SchedulingCollector()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'parallel-collector.sqlite3'}",
            inventory_path=inventory_path,
            project_root=Path(__file__).resolve().parents[1],
            session_secret="test-secret",
        ),
        collector=collector,  # type: ignore[arg-type]
    )

    with TestClient(app):
        assert collector.collected.wait(timeout=2)

    assert collector.calls[0] == (
        ["endpoint-a", "endpoint-b", "endpoint-c", "endpoint-d"],
        5,
        0.0,
    )


def test_shutdown_waits_for_inflight_start_cleanup_without_leaving_ownership(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = True
    configured.collector.interval_seconds = 1
    configured.collector.stale_after_seconds = 3
    configured.endpoints = [configured.endpoints[0]]
    configured.endpoints[0].keepalive_adapter_id = "server-script-v1"
    configured.endpoints[0].keepalive_policy = "idle_keepalive"
    configured.endpoints[0].expected_gpu_count = 1
    inventory_path = tmp_path / "shutdown-inflight-inventory.yaml"
    inventory_path.write_text(configured.model_dump_json(), encoding="utf-8")
    adapter = BlockingKeepaliveAdapter()
    collector = PeriodicFakeCollector()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'shutdown-inflight.sqlite3'}",
            inventory_path=inventory_path,
            project_root=Path(__file__).resolve().parents[1],
            session_secret="test-secret",
        ),
        collector=collector,  # type: ignore[arg-type]
        keepalive_adapter_resolver=lambda _adapter_id: adapter,
    )
    client = TestClient(app)
    client.__enter__()
    shutdown_thread: threading.Thread | None = None
    try:
        assert adapter.started.wait(timeout=2)
        assert adapter.active_pids == {GPU_UUIDS[0]: 7_001}
        collector_stopped = threading.Event()
        assert adapter._loop is not None
        adapter._loop.call_soon_threadsafe(
            app.state.collector_task.add_done_callback,
            lambda _task: collector_stopped.set(),
        )
        shutdown_thread = threading.Thread(
            target=lambda: client.__exit__(None, None, None),
            daemon=True,
        )
        shutdown_thread.start()
        assert collector_stopped.wait(timeout=2)
        assert shutdown_thread.is_alive()
        assert adapter.active_pids == {GPU_UUIDS[0]: 7_001}

        adapter.release()
        shutdown_thread.join(timeout=2)
        assert not shutdown_thread.is_alive()
    finally:
        if shutdown_thread is None or shutdown_thread.is_alive():
            adapter.release()
            if shutdown_thread is None:
                client.__exit__(None, None, None)
            else:
                shutdown_thread.join(timeout=2)

    assert adapter.cleaned.is_set()
    assert adapter.active_pids == {}
    with app.state.service.database.session() as session:
        assert session.scalars(select(Lease).where(Lease.kind == "keepalive")).all() == []


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
    replacement = initial.model_copy(
        update={"process_started_at": started_at + timedelta(seconds=10)}
    )
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
    observation_not_before = datetime.now(UTC)
    adapter.active_pids[GPU_UUIDS[0]] = 4_001
    service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=collector.processes(),
            observed_at=datetime.now(UTC),
        )
    )
    begun = service.activate_keepalive(
        actor,
        "endpoint-a",
        "endpoint-a:GPU-00000000-0000-0000-0000-000000000001",
        observation_not_before=observation_not_before,
        idempotency_key="cleanup-activate",
    )
    lease_id = str(begun["keepalive"]["lease_id"])
    adapter.active_pids.clear()
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
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
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
    invalid = client.post(
        path, json={"enabled": True, "gpu_uuids": [GPU_UUIDS[0]]}, headers=_headers("strict")
    )
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
def test_keepalive_api_failures_are_reported_as_errors(
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
    assert summary["desired"] == "ON"
    assert summary["actual"] == "ERROR"
    assert summary["active_gpu_count"] == 0
    assert summary["error_gpu_count"] == len(GPU_UUIDS)
    assert len({reason["reason"] for reason in summary["reasons"]}) == 1
    snapshot = app.state.service.snapshot(app.state.service.local_actor("agent-a"))["data"]
    assert snapshot["summary"]["available_gpus"] == len(GPU_UUIDS)
    assert app.state.service.list_leases(app.state.service.local_actor("agent-a"))["data"] == []


def test_incompatible_helper_is_reported_without_cleanup_mutation(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter(
        failure=AdapterCommandError(
            "keepalive_helper_incompatible: expected schema 3",
            uncertain=False,
        )
    )
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("incompatible-helper"),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "keepalive_helper_incompatible"
    assert adapter.calls == [("endpoint-a", True, GPU_UUIDS)]
    summary = app.state.service.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]
    assert summary["actual"] == "ERROR"
    assert summary["reasons"][0]["reason"] == (
        "远端占卡 helper 版本或能力不匹配；请先完成该服务器的 helper 升级。"
    )


def test_incompatible_helper_on_stop_is_not_collapsed_to_partial_stop(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    class StopIncompatibleAdapter(FakeKeepaliveAdapter):
        async def set_enabled(  # type: ignore[no-untyped-def]
            self, endpoint, enabled: bool, gpu_uuids: list[str]
        ) -> KeepaliveResponse:
            if not enabled:
                self.calls.append((endpoint.id, enabled, tuple(gpu_uuids)))
                raise AdapterCommandError(
                    "keepalive_helper_incompatible: expected schema 3",
                    uncertain=False,
                )
            return await super().set_enabled(endpoint, enabled, gpu_uuids)

    adapter = StopIncompatibleAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("incompatible-stop-on"),
    )
    assert enabled.status_code == 200, enabled.text

    disabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers=_headers("incompatible-stop-off"),
    )
    assert disabled.status_code == 503, disabled.text
    assert disabled.json()["error"]["code"] == "keepalive_helper_incompatible"
    assert adapter.calls == [
        ("endpoint-a", True, GPU_UUIDS),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[1],)),
    ]


def test_keepalive_api_reports_known_cuda_worker_failure_in_chinese_without_remote_stderr(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    class CudaStartFailingAdapter(FakeKeepaliveAdapter):
        async def set_enabled(  # type: ignore[no-untyped-def]
            self, endpoint, enabled: bool, gpu_uuids: list[str]
        ) -> KeepaliveResponse:
            if enabled:
                self.calls.append((endpoint.id, enabled, tuple(gpu_uuids)))
                raise AdapterCommandError(
                    "serverpilot-keepalive failed: RuntimeError: "
                    "CUDA keepalive worker could not start: RuntimeError: "
                    "exactly one CUDA GPU must be visible",
                    uncertain=True,
                )
            return await super().set_enabled(endpoint, enabled, gpu_uuids)

    adapter = CudaStartFailingAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    response = TestClient(app).post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("known-cuda-worker-failure"),
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "keepalive_cuda_target_unavailable",
        "message": (
            "远端占卡程序已启动，但 PyTorch/CUDA 没有识别出唯一目标 GPU；"
            "请检查这台服务器的 CUDA 运行环境。"
        ),
        "details": {
            "failed_gpu_ids": [
                f"endpoint-a:{GPU_UUIDS[0]}",
                f"endpoint-a:{GPU_UUIDS[1]}",
            ]
        },
    }
    assert "serverpilot-keepalive failed" not in response.text
    assert adapter.calls == [
        ("endpoint-a", True, GPU_UUIDS),
        ("endpoint-a", False, GPU_UUIDS),
    ]


@pytest.mark.parametrize(
    ("remote_failure", "expected_code"),
    [
        ("keepalive_helper_incompatible: expected schema 3", "keepalive_helper_incompatible"),
        ("PyTorch with CUDA support is required", "keepalive_pytorch_cuda_required"),
        (
            "PyTorch CUDA runtime could not initialize the selected GPU",
            "keepalive_cuda_runtime_unavailable",
        ),
        (
            "CUDA error: no kernel image is available for execution on the device",
            "keepalive_cuda_architecture_unsupported",
        ),
        ("keepalive CUDA PCI ordinal mapping is invalid", "keepalive_cuda_index_mapping_failed"),
        (
            "keepalive CUDA PCI ordinal mapping does not contain requested GPU UUID",
            "keepalive_cuda_uuid_not_found",
        ),
        (
            "CUDA visible device count is 8; expected exactly one",
            "keepalive_cuda_target_unavailable",
        ),
    ],
)
def test_keepalive_adapter_failure_preserves_known_cuda_category(
    remote_failure: str, expected_code: str
) -> None:
    assert (
        _keepalive_adapter_failure_code(AdapterCommandError(remote_failure, uncertain=True))
        == expected_code
    )


def test_keepalive_unknown_uuid_has_specific_public_chinese_message() -> None:
    failure = BrokerError(
        "keepalive_cuda_uuid_not_found",
        "remote detail is not public",
        status_code=503,
    )

    assert _public_error_message(failure) == "远端当前 PCI GPU 清单中找不到目标 GPU UUID。"


def test_keepalive_helper_incompatibility_has_specific_public_message() -> None:
    failure = BrokerError(
        "keepalive_helper_incompatible",
        "remote detail is not public",
        status_code=503,
    )

    assert _public_error_message(failure) == (
        "远端占卡 helper 版本或能力不匹配；请先完成该服务器的 helper 升级。"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("policy", "unknown-policy"), ("state", "UNKNOWN")],
)
def test_keepalive_public_protocol_rejects_unknown_values(field: str, value: str) -> None:
    keepalive = {
        "policy": "disabled",
        "state": "OFF",
        "configured": True,
    }
    keepalive[field] = value

    with pytest.raises(BrokerError, match="无法识别") as failure:
        _public_keepalive_result("endpoint-a", keepalive)

    assert failure.value.code == "invalid_keepalive_protocol"


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
    assert adapter.calls == [("endpoint-a", True, GPU_UUIDS)]
    assert collector.calls == [(["endpoint-a"], 1)]


def test_keepalive_reconcile_starts_eight_gpus_with_one_helper_call_and_collection(
    tmp_path: Path, inventory: InventoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, gpu_uuids=EIGHT_GPU_UUIDS)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor, "endpoint-a", "idle_keepalive", idempotency_key="eight-policy"
    )
    activation_transactions = 0
    original_activate = service.activate_keepalives

    def counted_activate(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal activation_transactions
        original_write = service._write

        def counted_write(operation):  # type: ignore[no-untyped-def]
            nonlocal activation_transactions
            activation_transactions += 1
            return original_write(operation)

        monkeypatch.setattr(service, "_write", counted_write)
        try:
            return original_activate(*args, **kwargs)
        finally:
            monkeypatch.setattr(service, "_write", original_write)

    monkeypatch.setattr(service, "activate_keepalives", counted_activate)

    result = asyncio.run(
        app.state.reconcile_endpoint_keepalive(actor, "endpoint-a", idempotency_key="eight")
    )

    assert result["keepalive"]["active_gpu_count"] == 8
    assert activation_transactions == 1
    assert adapter.calls == [("endpoint-a", True, EIGHT_GPU_UUIDS)]
    assert collector.calls == [(["endpoint-a"], 1)]
    with service.database.session() as session:
        assert len(session.scalars(select(Lease).where(Lease.kind == "keepalive")).all()) == 8


def test_batch_activation_conflict_on_second_gpu_cleans_all_helpers_and_adds_no_keepalive(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = WorkloadConflictCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="atomic-conflict-policy",
    )

    with pytest.raises(BrokerError) as failure:
        asyncio.run(
            app.state.reconcile_endpoint_keepalive(
                actor,
                "endpoint-a",
                idempotency_key="atomic-conflict",
            )
        )

    assert failure.value.code == "keepalive_gpu_ineligible"
    assert adapter.calls == [
        ("endpoint-a", True, GPU_UUIDS),
        ("endpoint-a", False, GPU_UUIDS),
    ]
    assert adapter.active_pids == {}
    with service.database.session() as session:
        assert session.scalars(select(Lease).where(Lease.kind == "keepalive")).all() == []


def test_unexpected_mid_transaction_activation_failure_cleans_batch_and_rolls_back(
    tmp_path: Path, inventory: InventoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="unexpected-activation-policy",
    )
    original_audit = service._audit
    activation_audits = 0

    def fail_second_activation_audit(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal activation_audits
        if kwargs.get("action") == "keepalive.gpu_activated":
            activation_audits += 1
            if activation_audits == 2:
                raise RuntimeError("mid-transaction activation failure")
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(service, "_audit", fail_second_activation_audit)

    with pytest.raises(BrokerError) as failure:
        asyncio.run(
            app.state.reconcile_endpoint_keepalive(
                actor,
                "endpoint-a",
                idempotency_key="unexpected-activation",
            )
        )

    assert failure.value.code == "keepalive_activation_failed"
    assert adapter.calls == [
        ("endpoint-a", True, GPU_UUIDS),
        ("endpoint-a", False, GPU_UUIDS),
    ]
    assert collector.calls == [(["endpoint-a"], 1), (["endpoint-a"], 1)]
    assert adapter.active_pids == {}
    with service.database.session() as session:
        assert session.scalars(select(Lease).where(Lease.kind == "keepalive")).all() == []


def test_partial_batch_start_cleans_all_eight_gpus_with_no_keepalive_lease(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = PartiallyStartingBatchAdapter()
    collector = FakeTargetedCollector(adapter, gpu_uuids=EIGHT_GPU_UUIDS)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    service = app.state.service
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor, "endpoint-a", "idle_keepalive", idempotency_key="partial-batch-policy"
    )

    with pytest.raises(BrokerError, match="空闲占卡未能启动"):
        asyncio.run(
            app.state.reconcile_endpoint_keepalive(
                actor,
                "endpoint-a",
                idempotency_key="partial-batch",
            )
        )

    assert adapter.calls == [
        ("endpoint-a", True, EIGHT_GPU_UUIDS),
        ("endpoint-a", False, EIGHT_GPU_UUIDS),
    ]
    assert collector.calls == [(["endpoint-a"], 1)]
    assert adapter.active_pids == {}
    with service.database.session() as session:
        assert session.scalars(select(Lease).where(Lease.kind == "keepalive")).all() == []


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
    with pytest.raises(BrokerError) as bypassed:
        service.create_request(
            service.local_actor("agent-a"),
            RequestCreate.model_validate(claim_payload),
            idempotency_key="claim-one-keeper-direct-bypass",
            activate_if_allocated=True,
        )
    assert bypassed.value.code == "no_capacity"
    assert adapter.calls == [("endpoint-a", True, (GPU_UUIDS[0],))]

    original_create_request = service.create_request
    original_plan_keepalive_reclaim = service.plan_keepalive_reclaim
    create_snapshots: list[list[tuple[str, bool, tuple[str, ...]]]] = []
    reclaim_plans: list[RequestCreate] = []

    def observed_create_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        create_snapshots.append(list(adapter.calls))
        return original_create_request(*args, **kwargs)

    def observed_plan_keepalive_reclaim(request_data):  # type: ignore[no-untyped-def]
        reclaim_plans.append(request_data)
        return original_plan_keepalive_reclaim(request_data)

    service.create_request = observed_create_request  # type: ignore[method-assign]
    service.plan_keepalive_reclaim = observed_plan_keepalive_reclaim  # type: ignore[method-assign]
    claimed = client.post(
        "/api/v1/claims",
        json=claim_payload,
        headers=_headers("claim-one-keeper"),
    )

    assert claimed.status_code == 200, claimed.text
    claimed_gpu_ids = claimed.json()["lease"]["gpu_ids"]
    assert len(claimed_gpu_ids) == 1
    assert len(reclaim_plans) == 1
    # The reclaim plan chose exactly one currently verified keeper, and the
    # helper never received the sibling GPU as a stop target.
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))
    # The ordinary claim happens while the endpoint reconcile lock is still held. Its
    # observation sees only the targeted stop; no policy-driven start can fit
    # between that stop/fresh-finalization path and ordinary admission.
    assert create_snapshots == [
        [("endpoint-a", True, (GPU_UUIDS[0],))],
        [
            ("endpoint-a", True, (GPU_UUIDS[0],)),
            ("endpoint-a", False, (GPU_UUIDS[0],)),
        ],
    ]
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
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


def test_immediate_claim_stop_failure_does_not_create_workload_lease(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = PartiallyFailingStopAdapter(GPU_UUIDS[0])
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("failed-claim-keeper-on"),
    )
    assert enabled.status_code == 200, enabled.text

    failed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "failed-claim-one-keeper",
            "purpose": "stop failure must not fabricate a lease",
            "constraints": {"gpu_count": 1},
        },
        headers=_headers("failed-claim-one-keeper"),
    )

    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "keepalive_outcome_uncertain"
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    assert snapshot["leases"] == []
    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
    ]


def test_missing_keeper_is_still_publicly_available_and_claimable(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("missing-keeper-on"),
    )
    assert enabled.status_code == 200, enabled.text
    adapter.active_pids.clear()
    app.state.service.ingest_observation(
        observation(
            count=len(GPU_UUIDS),
            gpu_uuids=list(GPU_UUIDS),
            processes=collector.processes(),
        )
    )

    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    missing = next(gpu for gpu in snapshot["gpus"] if gpu["gpu_uuid"] == GPU_UUIDS[0])
    assert missing["keepalive"] == {
        "configured": True,
        "policy": "idle_keepalive",
        "desired": "ON",
        "actual": "OFF",
        "state": "OFF",
        "reason": None,
        "lease_id": missing["keepalive"]["lease_id"],
    }
    assert snapshot["summary"]["available_gpus"] == 1
    assert snapshot["summary"]["claimed_gpus"] == 0
    assert snapshot["resource_projection"]["available"]["gpu_count"] == 1
    assert snapshot["resource_projection"]["claimed"]["gpu_count"] == 0

    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "claim-missing-keeper",
            "purpose": "claim a GPU whose occupancy helper exited",
            "constraints": {"gpu_count": 1},
        },
        headers=_headers("claim-missing-keeper"),
    )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["lease"]["gpu_ids"] == [
        "endpoint-a:GPU-00000000-0000-0000-0000-000000000001"
    ]
    assert adapter.calls[-1] == ("endpoint-a", False, (GPU_UUIDS[0],))


def test_quick_claim_uses_the_same_selected_keeper_handoff(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter, unmanaged_gpu_uuids=(GPU_UUIDS[1],))
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("quick-claim-keeper-on"),
    )
    assert enabled.status_code == 200, enabled.text
    page = client.get("/ui/requests")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf is not None

    claimed = client.post(
        "/ui/action/quick-claim",
        data={
            "project_id": "project-a",
            "task_ref": "quick-claim-one-keeper",
            "gpu_count": "1",
            "placement": "pack",
            "endpoint_id": "",
            "csrf": csrf.group(1),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )

    assert claimed.status_code == 200, claimed.text
    assert "GPU 已申领，待使用" in claimed.text
    assert adapter.calls == [
        ("endpoint-a", True, (GPU_UUIDS[0],)),
        ("endpoint-a", False, (GPU_UUIDS[0],)),
    ]
    workloads = app.state.service.list_leases(app.state.service.local_actor("human"))["data"]
    assert len(workloads) == 1
    assert workloads[0]["gpu_ids"] == ["endpoint-a:GPU-00000000-0000-0000-0000-000000000001"]


def test_release_restores_the_selected_keeper_on_the_next_collection(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("restore-keepers-on"),
    )
    assert enabled.status_code == 200, enabled.text
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "release-then-restore",
            "purpose": "verify next-cycle keeper restoration",
            "constraints": {"gpu_count": 1},
        },
        headers=_headers("release-then-restore"),
    )
    assert claimed.status_code == 200, claimed.text
    claimed_gpu_id = claimed.json()["lease"]["gpu_ids"][0]
    claimed_uuid = next(
        gpu["gpu_uuid"]
        for gpu in client.get(
            "/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}
        ).json()["data"]["gpus"]
        if gpu["id"] == claimed_gpu_id
    )
    assert claimed_uuid not in adapter.active_pids

    released = client.post(
        f"/api/v1/leases/{claimed.json()['lease']['id']}/release",
        json={"reason": "workload completed"},
        headers=_headers("release-before-restore"),
    )
    assert released.status_code == 200, released.text

    async def next_collection() -> None:
        endpoint = app.state.service.collector_endpoint("endpoint-a")
        await collector.collect_once(app.state.service, endpoints=[endpoint])
        await app.state.reconcile_endpoint_keepalive(
            app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="next-collection-restore",
        )

    asyncio.run(next_collection())

    assert set(adapter.active_pids) == set(GPU_UUIDS)
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    assert {gpu["state"] for gpu in snapshot["gpus"]} == {"KEEPALIVE"}


def test_routine_agent_path_handles_keepalive_on_and_off(
    tmp_path: Path, inventory: InventoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    rest = TestClient(app)
    headers = {"X-ServerPilot-Actor": "agent"}

    class RoutineClient:
        def snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
            # Mirror BrokerClient.snapshot: an unset endpoint_id is dropped
            # rather than sent as an empty value the broker would filter on.
            params = {key: value for key, value in kwargs.items() if value is not None}
            response = rest.get("/api/v1/snapshot", params=params, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        def post(self, path, body=None, *, idempotency_key=None):  # type: ignore[no-untyped-def]
            request_headers = dict(headers)
            if idempotency_key is not None:
                request_headers["Idempotency-Key"] = idempotency_key
            response = rest.post(path, json=body, headers=request_headers)
            assert response.status_code == 200, response.text
            return response.json()

    monkeypatch.setattr(mcp_server, "_routine_client", RoutineClient)

    enabled = rest.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("agent-path-on"),
    )
    assert enabled.status_code == 200, enabled.text
    status_on = mcp_server.gpu_status()
    assert len(status_on["gpus"]) == len(GPU_UUIDS)
    assert {gpu["status"] for gpu in status_on["gpus"]} == {"可用 · 空闲占卡"}
    assert {tuple(gpu["keepalive"].values()) for gpu in status_on["gpus"]} == {("ON", "ON")}

    # Simulate the exact production failure: the workload has already been
    # released and the helper restarted its own workers, so their PIDs no
    # longer match the persisted keeper identities.  The collector must keep
    # these GPUs fail-closed until the helper attests them; an Agent must not
    # see a foreign workload or reclaim them by bypassing the normal path.
    calls_before_recovery = list(adapter.calls)
    adapter.active_pids = {gpu_uuid: pid + 10_000 for gpu_uuid, pid in adapter.active_pids.items()}
    # The helper may run in a PID namespace: its own sealed PID differs from
    # the NVIDIA driver PID that the normal collector observes.  Recovery is
    # allowed only because the independently attested driver PID still agrees
    # with that collector observation.
    adapter.attested_pids = {gpu_uuid: pid + 20_000 for gpu_uuid, pid in adapter.active_pids.items()}
    adapter.driver_pids = dict(adapter.active_pids)

    async def collect_restarted_workers() -> None:
        endpoint = app.state.service.collector_endpoint("endpoint-a")
        await collector.collect_once(app.state.service, endpoints=[endpoint])

    asyncio.run(collect_restarted_workers())
    unavailable = mcp_server.gpu_status()
    assert unavailable["gpus"] == []
    assert unavailable["no_capacity"]["reason"] == "all_gpus_busy_or_unavailable"
    error_status = mcp_server.gpu_status(include_busy=True)
    assert {gpu["available"] for gpu in error_status["gpus"]} == {False}
    assert {tuple(gpu["keepalive"].values()) for gpu in error_status["gpus"]} == {("ON", "ERROR")}
    assert {gpu["status"] for gpu in error_status["gpus"]} == {"占卡校验失败，暂不可申请"}

    async def recover_restarted_workers() -> None:
        await app.state.reconcile_endpoint_keepalive(
            app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="agent-path-worker-recovery",
        )

    asyncio.run(recover_restarted_workers())
    assert adapter.calls == calls_before_recovery
    assert adapter.attest_calls[-1] == ("endpoint-a", GPU_UUIDS)
    recovered_status = mcp_server.gpu_status()
    assert len(recovered_status["gpus"]) == len(GPU_UUIDS)
    assert {gpu["status"] for gpu in recovered_status["gpus"]} == {"可用 · 空闲占卡"}

    allocation_on = mcp_server.gpu_apply(
        server_id="endpoint-a", gpu_count=1, task="Agent 占卡开启申请验收"
    )
    assert len(allocation_on["gpus"]) == 1
    assert allocation_on["workspace_path"]
    assert allocation_on["cuda_visible_devices"]
    selected_uuid = allocation_on["gpus"][0]["gpu_id"]
    assert selected_uuid not in adapter.active_pids
    assert len(adapter.active_pids) == len(GPU_UUIDS) - 1
    mcp_server.gpu_release(allocation_on["lease_id"])

    async def restore_then_disable() -> None:
        endpoint = app.state.service.collector_endpoint("endpoint-a")
        await collector.collect_once(app.state.service, endpoints=[endpoint])
        await app.state.reconcile_endpoint_keepalive(
            app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="agent-path-restore",
        )

    asyncio.run(restore_then_disable())
    assert set(adapter.active_pids) == set(GPU_UUIDS)
    # Restoring the selected GPU starts a *new* helper worker.  Its namespace
    # PID is intentionally different, while the attested driver-visible PID
    # tracks the collector's new process identity.
    assert adapter.attested_pids[selected_uuid] != adapter.active_pids[selected_uuid]
    assert adapter.driver_pids[selected_uuid] == adapter.active_pids[selected_uuid]
    disabled = rest.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": False},
        headers=_headers("agent-path-off"),
    )
    assert disabled.status_code == 200, disabled.text
    status_off = mcp_server.gpu_status()
    assert len(status_off["gpus"]) == len(GPU_UUIDS)
    assert {gpu["status"] for gpu in status_off["gpus"]} == {"可用 · 未开启占卡"}
    assert {tuple(gpu["keepalive"].values()) for gpu in status_off["gpus"]} == {("OFF", "OFF")}

    allocation_off = mcp_server.gpu_apply(
        server_id="endpoint-a", gpu_count=1, task="Agent 占卡关闭申请验收"
    )
    assert len(allocation_off["gpus"]) == 1
    assert adapter.active_pids == {}
    mcp_server.gpu_release(allocation_off["lease_id"])


def test_keepalive_recovery_rejects_mismatched_helper_attestation(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    """A collector process is not enough to re-adopt a keeper after restart."""

    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)
    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("attestation-mismatch-on"),
    )
    assert enabled.status_code == 200, enabled.text
    calls_before_recovery = list(adapter.calls)
    adapter.active_pids = {gpu_uuid: pid + 10_000 for gpu_uuid, pid in adapter.active_pids.items()}
    adapter.driver_pids = {gpu_uuid: pid + 1 for gpu_uuid, pid in adapter.active_pids.items()}

    async def collect_then_recover() -> None:
        endpoint = app.state.service.collector_endpoint("endpoint-a")
        await collector.collect_once(app.state.service, endpoints=[endpoint])
        await app.state.reconcile_endpoint_keepalive(
            app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="attestation-mismatch-recover",
        )

    with pytest.raises(BrokerError, match="sealed keepalive worker identity") as failure:
        asyncio.run(collect_then_recover())
    assert failure.value.code == "keepalive_confirmation_mismatch"
    # Recovery is read-only.  A mismatched helper proof never stops an
    # existing worker or turns the GPU into immediately claimable capacity.
    assert adapter.calls == calls_before_recovery
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    assert {gpu["state"] for gpu in snapshot["gpus"]} == {"CONFLICT"}
    assert {gpu["keepalive"]["actual"] for gpu in snapshot["gpus"]} == {"ERROR"}


def test_keepalive_recovery_never_adopts_additional_foreign_processes(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    """A verified keeper plus any extra process remains fail-closed."""

    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)
    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("foreign-process-on"),
    )
    assert enabled.status_code == 200, enabled.text
    calls_before_recovery = list(adapter.calls)
    attestations_before_recovery = list(adapter.attest_calls)
    collector.unmanaged_gpu_uuids = GPU_UUIDS

    async def collect_then_reconcile() -> dict:
        endpoint = app.state.service.collector_endpoint("endpoint-a")
        await collector.collect_once(app.state.service, endpoints=[endpoint])
        return await app.state.reconcile_endpoint_keepalive(
            app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="foreign-process-reconcile",
        )

    reconciled = asyncio.run(collect_then_reconcile())
    assert reconciled["keepalive"]["actual"] == "ERROR"
    # The planner emits only an ineligible outcome: no helper attestation,
    # stop, or restart is attempted around a potentially real workload.
    assert adapter.calls == calls_before_recovery
    assert adapter.attest_calls == attestations_before_recovery
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    assert {gpu["state"] for gpu in snapshot["gpus"]} == {"CONFLICT"}
    assert {gpu["public_status"] for gpu in snapshot["gpus"]} == {"占卡校验失败，暂不可申请"}


def test_restart_preserves_keepalive_ownership_without_remote_churn(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    first_app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    first_client = TestClient(first_app)

    enabled = first_client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("restart-recovery-on"),
    )
    assert enabled.status_code == 200, enabled.text
    assert set(adapter.active_pids) == set(GPU_UUIDS)
    calls_before_restart = list(adapter.calls)
    with first_app.state.service.database.session() as session:
        lease_ids_before_restart = set(
            session.scalars(
                select(Lease.id).where(Lease.kind == "keepalive", Lease.state == "ACTIVE")
            ).all()
        )

    restarted_app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)

    reconciled = asyncio.run(
        restarted_app.state.reconcile_endpoint_keepalive(
            restarted_app.state.service.local_actor("agent-a"),
            "endpoint-a",
            idempotency_key="restart-preserve",
        )
    )

    assert adapter.calls == calls_before_restart
    assert set(adapter.active_pids) == set(GPU_UUIDS)
    assert reconciled["keepalive"]["desired"] == "ON"
    assert reconciled["keepalive"]["actual"] == "ON"
    with restarted_app.state.service.database.session() as session:
        active_keepers = session.scalars(
            select(Lease).where(Lease.kind == "keepalive", Lease.state == "ACTIVE")
        ).all()
        assert len(active_keepers) == len(GPU_UUIDS)
        assert {lease.id for lease in active_keepers} == lease_ids_before_restart
        assert {lease.expires_at for lease in active_keepers} == {None}


def test_app_reassignment_stops_the_selected_keeper_before_moving_the_task(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    adapter = FakeKeepaliveAdapter()
    collector = FakeTargetedCollector(adapter)
    app, _ = _keepalive_app(tmp_path, inventory, adapter=adapter, collector=collector)
    client = TestClient(app)

    enabled = client.post(
        "/api/v1/endpoints/endpoint-a/keepalive",
        json={"enabled": True},
        headers=_headers("reassign-keepers-on"),
    )
    assert enabled.status_code == 200, enabled.text
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "reassign-from-keeper-a",
            "purpose": "create a workload beside one keeper",
            "constraints": {"gpu_count": 1},
        },
        headers=_headers("reassign-initial-claim"),
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["lease"]
    original_gpu_id = lease["gpu_ids"][0]
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    keeper = next(gpu for gpu in snapshot["gpus"] if gpu["state"] == "KEEPALIVE")

    moved = client.patch(
        f"/api/v1/leases/{lease['id']}/gpus",
        json={"gpu_ids": [keeper["id"]]},
        headers=_headers("reassign-to-keeper"),
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["restart_required"] is True
    assert moved.json()["lease"]["gpu_ids"] == [keeper["id"]]
    assert adapter.calls[-1] == ("endpoint-a", False, (keeper["gpu_uuid"],))
    after = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "agent-a"}).json()[
        "data"
    ]
    states_by_id = {gpu["id"]: gpu["state"] for gpu in after["gpus"]}
    assert states_by_id[original_gpu_id] == "AVAILABLE"
    assert states_by_id[keeper["id"]] == "HELD"


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
    assert "常规 gpu 任务" in instructions
    assert "空闲占卡" in instructions
    assert "code_location=not_provided" in instructions
    assert "不得把 workspace_path 当代码仓库路径" in instructions
    assert "以它为工作目录" in instructions
