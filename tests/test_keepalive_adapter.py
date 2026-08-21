from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import signal
import threading
from pathlib import Path
from typing import Any

import pytest
import serverpilot.server_keepalive as keepalive_module

from serverpilot.adapters import (
    AdapterCommandError,
    AdapterRegistryError,
    ServerScriptKeepaliveAdapter,
    endpoint_keepalive_adapter,
)
from serverpilot.config import EndpointConfig
from serverpilot.keepalive_protocol import (
    KEEPALIVE_INSPECT_COMMAND,
    KEEPALIVE_IMPLEMENTATION_VERSION,
    KEEPALIVE_PROTOCOL_INFO_CAPABILITIES,
    KEEPALIVE_SCHEMA_VERSION,
    KeepaliveAttestationRequest,
    KeepaliveAttestationResponse,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
    keepalive_protocol_info,
)
from serverpilot.server_keepalive import (
    ACTIVE_DUTY_FRACTION,
    DUTY_PERIOD_SECONDS,
    TARGET_MEMORY_FRACTION,
    WORKER_PROCESS_MARKER,
    keepalive_target_bytes,
    KeepaliveProcessIdentity,
    LocalKeepaliveController,
    TorchSubprocessProvider,
    default_state_directory,
    handle_attestation,
    handle_request,
)


GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
GPU_C = "GPU-00000000-0000-0000-0000-000000000003"
KNOWN_GPUS = {GPU_A, GPU_B, GPU_C}


def test_torch_provider_resolves_uuid_to_pci_ordered_cuda_ordinal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_query(argument: str) -> str:
        calls.append(argument)
        return (
            f"7, {GPU_A}, 00000000:AF:00.0\n"
            f"3, {GPU_B}, 00000000:01:00.0\n"
        )

    monkeypatch.setattr("serverpilot.server_keepalive._run_nvidia_smi_query", fake_query)
    provider = TorchSubprocessProvider()

    assert provider._cuda_visible_device(GPU_A) == "1"
    assert provider._cuda_visible_device(GPU_B) == "0"
    assert calls == ["--query-gpu=index,uuid,pci.bus_id"]


def test_torch_provider_sets_pci_order_and_derived_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 9876

        @staticmethod
        def poll() -> None:
            return None

    def fake_popen(command: list[str], **options: Any) -> FakeProcess:
        captured["command"] = command
        captured["env"] = options["env"]
        os.write(options["pass_fds"][0], b"READY\n")
        return FakeProcess()

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.Popen", fake_popen)
    identity = KeepaliveProcessIdentity(
        pid=9876,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=1234,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._capture_worker_process_identity", lambda _pid: identity
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._worker_process_matches", lambda observed: observed == identity
    )
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "4"}

    assert provider.start(GPU_A) == identity
    assert captured["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "4"
    assert captured["command"][-2:] == ["--worker-marker", WORKER_PROCESS_MARKER]


def test_torch_provider_reports_unknown_uuid_in_pci_mapping() -> None:
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "0"}

    with pytest.raises(RuntimeError, match="does not contain requested GPU UUID"):
        provider._cuda_visible_device(GPU_B)


def test_driver_pid_attestation_uses_only_the_fixed_compute_process_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_query(query_argument: str) -> str:
        calls.append(query_argument)
        return f"{GPU_A}, 50101\n{GPU_B}, 50102\n"

    monkeypatch.setattr(keepalive_module, "_run_nvidia_smi_query", fake_query)

    assert keepalive_module._resolve_keepalive_driver_pid(GPU_A) == 50101
    assert calls == ["--query-compute-apps=gpu_uuid,pid"]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("No running compute processes found\n", "exactly one process"),
        (f"{GPU_B}, 50102\n", "exactly one process"),
        (f"{GPU_A}, 50101\n{GPU_A}, 50103\n", "exactly one process"),
        (f"{GPU_A}, invalid\n", "query is invalid"),
    ],
)
def test_driver_pid_attestation_fails_closed_for_nonunique_or_malformed_compute_query(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        keepalive_module, "_run_nvidia_smi_query", lambda _query_argument: output
    )

    with pytest.raises(RuntimeError, match=message):
        keepalive_module._resolve_keepalive_driver_pid(GPU_A)


def _result(
    gpu_uuid: str,
    *,
    enabled: bool,
    outcome: str = "unchanged",
) -> KeepaliveGPUResult:
    return KeepaliveGPUResult(
        gpu_uuid=gpu_uuid,
        status="running" if enabled else "stopped",
        outcome=outcome,  # type: ignore[arg-type]
    )


def test_protocol_uses_only_the_per_gpu_request() -> None:
    request = KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B))
    decoded = KeepaliveRequest.decode(request.encode())

    assert decoded == request
    assert KEEPALIVE_SCHEMA_VERSION == 3
    with pytest.raises(KeepaliveProtocolError, match="expected 3"):
        KeepaliveRequest.decode('{"schema_version":2,"enabled":true}')
    with pytest.raises(KeepaliveProtocolError, match="malformed"):
        KeepaliveRequest.decode('{"schema_version":3,"enabled":true,"gpu_uuids":["0;touch /tmp/x"]}')
    assert KeepaliveRequest.decode(
        '{"schema_version":3,"enabled":true,"gpu_uuids":["' + GPU_A + '"],"note":"unused"}'
    ) == KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A,))


def test_protocol_info_publishes_v3_helper_capabilities() -> None:
    assert keepalive_protocol_info() == {
        "kind": "serverpilot-keepalive",
        "schema_version": 3,
        # Pinned by tests/test_release_metadata.py; this test owns the
        # schema version and capability list, not the release string.
        "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
        "capabilities": [
            "per_gpu_keepalive",
            "pidfd_identity",
            "pci_bus_id",
            "worker_attestation",
        ],
    }


def test_attestation_protocol_is_typed_and_rejects_ambiguous_identity_evidence() -> None:
    request = KeepaliveAttestationRequest(gpu_uuids=(GPU_A,))
    assert KeepaliveAttestationRequest.decode(request.encode()) == request
    with pytest.raises(KeepaliveProtocolError, match="request fields are invalid"):
        KeepaliveAttestationRequest.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "gpu_uuids": [GPU_A],
                    "command": "arbitrary",
                }
            )
        )
    with pytest.raises(KeepaliveProtocolError, match="worker marker is invalid"):
        KeepaliveAttestationResponse.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "workers": [
                        {
                            "gpu_uuid": GPU_A,
                            "pid": 101,
                            "driver_pid": 50_101,
                            "boot_id": "fake-boot-id",
                            "start_time_ticks": 1010,
                            "worker_marker": "foreign-worker",
                        }
                    ],
                }
            )
        )
    with pytest.raises(KeepaliveProtocolError, match="driver PID is invalid"):
        KeepaliveAttestationResponse.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "workers": [
                        {
                            "gpu_uuid": GPU_A,
                            "pid": 101,
                            "driver_pid": 0,
                            "boot_id": "fake-boot-id",
                            "start_time_ticks": 1010,
                            "worker_marker": WORKER_PROCESS_MARKER,
                        }
                    ],
                }
            )
        )


def test_response_decodes_each_gpu_result() -> None:
    valid = KeepaliveResponse(enabled=True, results=(_result(GPU_A, enabled=True),)).encode()
    assert KeepaliveResponse.decode(valid).results[0].gpu_uuid == GPU_A
    decoded = KeepaliveResponse.decode(
        json.dumps(
            {
                "schema_version": 3,
                "enabled": False,
                "results": [
                    {"gpu_uuid": GPU_A, "status": "stopped", "outcome": "unchanged"},
                    {"gpu_uuid": GPU_B, "status": "stopped", "outcome": "stopped"},
                ],
                "note": "unused",
            }
        )
    )
    assert [result.gpu_uuid for result in decoded.results] == [GPU_A, GPU_B]


def test_keepalive_adapter_factory_is_sealed() -> None:
    assert endpoint_keepalive_adapter("server-script-v1").id == "server-script-v1"
    with pytest.raises(AdapterRegistryError, match="does not provide endpoint_keepalive"):
        endpoint_keepalive_adapter("raw-ssh")


def test_adapter_uses_fixed_ssh_command_and_exact_json_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any], bytes]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload: bytes = b"") -> tuple[bytes, bytes]:
            calls.append((command, kwargs, payload))
            if command[-1].endswith("--protocol-info"):
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode("utf-8"),
                    b"",
                )
            return KeepaliveResponse(
                enabled=False,
                results=(_result(GPU_A, enabled=False, outcome="stopped"),),
            ).encode(), b""

    command: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **options: Any) -> FakeProcess:
        nonlocal command, kwargs
        command = args
        kwargs = options
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=2202,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    response = asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, False, [GPU_A]))

    assert response.results[0].outcome == "stopped"
    assert calls[0][0] == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        "-p",
        "2202",
        "gpu@gpu.example.test",
        "cd -- /srv/project-a && ./serverpilot-keepalive --protocol-info",
    )
    assert calls[0][2] == b""
    assert calls[1][0] == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        "-p",
        "2202",
        "gpu@gpu.example.test",
        "cd -- /srv/project-a && ./serverpilot-keepalive --schema-version 3",
    )
    assert json.loads(calls[1][2]) == {
        "schema_version": 3,
        "enabled": False,
        "gpu_uuids": [GPU_A],
    }
    assert {"shell", "env", "pid", "path", "argv", "command", "cuda_visible_devices"}.isdisjoint(
        json.loads(calls[1][2])
    )


def test_adapter_rejects_a_v2_response_with_explicit_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[Any, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            # The preflight probe runs before the mutation and receives the
            # old v2 capability record, so the mutation must never be sent.
            return (
                json.dumps(
                    {
                        "kind": "serverpilot-keepalive",
                        "schema_version": 2,
                        "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                        "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                    }
                ).encode("utf-8"),
                b"",
            )

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FakeProcess:
        invocations.append(_args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="schema version mismatch.*expected 3"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, False, [GPU_A]))
    assert len(invocations) == 1
    assert invocations[0][-1].endswith("--protocol-info")


def test_adapter_rejects_unknown_or_mismapped_gpu_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_calls: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            if not process_calls:
                process_calls.append("probe")
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode("utf-8"),
                    b"",
                )
            process_calls.append("mutation")
            return KeepaliveResponse(
                enabled=True,
                results=(_result(GPU_B, enabled=True, outcome="started"),),
            ).encode(), b""

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="exactly") as exc_info:
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A]))
    assert exc_info.value.uncertain is True
    with pytest.raises(ValueError, match="duplicates"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A, GPU_A]))
    with pytest.raises(ValueError, match="malformed"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, ["GPU-A;$(id)"]))


def test_adapter_attests_workers_through_the_fixed_inspection_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], bytes]] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command: tuple[Any, ...]) -> None:
            self.command = command

        async def communicate(self, payload: bytes = b"") -> tuple[bytes, bytes]:
            calls.append((self.command, payload))
            if self.command[-1].endswith("--protocol-info"):
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode(),
                    b"",
                )
            return (
                KeepaliveAttestationResponse(
                    workers=(
                        KeepaliveWorkerAttestation(
                            gpu_uuid=GPU_A,
                            pid=101,
                            driver_pid=50_101,
                            boot_id="fake-boot-id",
                            start_time_ticks=1010,
                            worker_marker=WORKER_PROCESS_MARKER,
                        ),
                    )
                ).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=2202,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    response = asyncio.run(ServerScriptKeepaliveAdapter().attest_workers(endpoint, [GPU_A]))

    assert response.workers[0].pid == 101
    assert response.workers[0].driver_pid == 50_101
    assert calls[1][0][-1] == f"cd -- /srv/project-a && {KEEPALIVE_INSPECT_COMMAND}"
    assert json.loads(calls[1][1]) == {"schema_version": 3, "gpu_uuids": [GPU_A]}
    assert {"shell", "env", "command", "path", "argv", "pid"}.isdisjoint(
        json.loads(calls[1][1])
    )


def test_adapter_rejects_attestation_that_widens_the_requested_gpu_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0

        def __init__(self, command: tuple[Any, ...]) -> None:
            self.command = command

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            if self.command[-1].endswith("--protocol-info"):
                return json.dumps(keepalive_protocol_info()).encode(), b""
            workers = tuple(
                KeepaliveWorkerAttestation(
                    gpu_uuid=gpu_uuid,
                    pid=101 + position,
                    driver_pid=50_101 + position,
                    boot_id="fake-boot-id",
                    start_time_ticks=1010 + position,
                    worker_marker=WORKER_PROCESS_MARKER,
                )
                for position, gpu_uuid in enumerate((GPU_A, GPU_B))
            )
            return KeepaliveAttestationResponse(workers=workers).encode(), b""

    async def fake_create_subprocess_exec(*args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="does not map exactly"):
        asyncio.run(ServerScriptKeepaliveAdapter().attest_workers(endpoint, [GPU_A]))


def test_adapter_attestation_timeout_is_read_only_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungProcess:
        returncode: int | None = None
        killed = False

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            await asyncio.Future()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    process = HungProcess()

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> HungProcess:
        return process

    async def probe_without_ssh(_endpoint: EndpointConfig) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = ServerScriptKeepaliveAdapter()
    adapter.timeout_seconds = 0.01
    monkeypatch.setattr(adapter, "_probe_helper", probe_without_ssh)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="attestation timed out") as failure:
        asyncio.run(adapter.attest_workers(endpoint, [GPU_A]))

    assert failure.value.uncertain is False
    assert process.killed is True


class FakeProvider:
    def __init__(self) -> None:
        self.running: set[int] = set()
        self.started: list[tuple[str, int]] = []
        self.stopped: list[int] = []

    @staticmethod
    def identity(pid: int) -> KeepaliveProcessIdentity:
        return KeepaliveProcessIdentity(
            pid=pid,
            boot_id="fake-boot-id",
            start_time_ticks=pid * 10,
            worker_marker=WORKER_PROCESS_MARKER,
        )

    def start(self, gpu_uuid: str) -> KeepaliveProcessIdentity:
        pid = 100 + len(self.started) + 1
        self.started.append((gpu_uuid, pid))
        self.running.add(pid)
        return self.identity(pid)

    def is_running(self, identity: KeepaliveProcessIdentity) -> bool:
        return identity.pid in self.running

    def stop(self, identity: KeepaliveProcessIdentity) -> None:
        self.stopped.append(identity.pid)
        self.running.discard(identity.pid)


class FailingSecondProvider(FakeProvider):
    def start(self, gpu_uuid: str) -> KeepaliveProcessIdentity:
        if self.started:
            raise RuntimeError("second worker failed")
        return super().start(gpu_uuid)


def test_local_controller_manages_each_gpu_independently_and_only_stops_own_state(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
        driver_pid_resolver=lambda gpu_uuid: {GPU_A: 50_101, GPU_B: 50_102}[gpu_uuid],
    )

    started = controller.set_enabled(True, [GPU_A, GPU_B])
    disabled_a = controller.set_enabled(False, [GPU_A])
    repeated_b = controller.set_enabled(True, [GPU_B])
    disabled_missing = controller.set_enabled(False, [GPU_C])

    pid_a = provider.started[0][1]
    pid_b = provider.started[1][1]
    assert [result.outcome for result in started.results] == ["started", "started"]
    assert disabled_a.results == (_result(GPU_A, enabled=False, outcome="stopped"),)
    assert repeated_b.results[0].outcome == "unchanged"
    assert disabled_missing.results[0].outcome == "unchanged"
    assert provider.stopped == [pid_a]
    assert provider.is_running(provider.identity(pid_b))
    stored = json.loads((tmp_path / "keepalive" / "workers.v3.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 3
    assert stored["workers"] == [
        {
            "gpu_uuid": GPU_B,
            "pid": pid_b,
            "boot_id": "fake-boot-id",
            "start_time_ticks": pid_b * 10,
            "worker_marker": WORKER_PROCESS_MARKER,
        }
    ]


def test_local_controller_attests_only_its_live_v3_workers(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
        driver_pid_resolver=lambda gpu_uuid: {GPU_A: 50_101, GPU_B: 50_102}[gpu_uuid],
    )
    controller.set_enabled(True, [GPU_A, GPU_B])

    response = handle_attestation(
        KeepaliveAttestationRequest(gpu_uuids=(GPU_B, GPU_A)).encode(), controller=controller
    )

    assert [
        (worker.gpu_uuid, worker.pid, worker.driver_pid, worker.start_time_ticks)
        for worker in response.workers
    ] == [
        (GPU_B, 102, 50_102, 1020),
        (GPU_A, 101, 50_101, 1010),
    ]
    assert all(worker.worker_marker == WORKER_PROCESS_MARKER for worker in response.workers)
    provider.running.remove(102)
    with pytest.raises(RuntimeError, match="recorded worker identity is not running"):
        controller.attest_workers([GPU_B])
    with pytest.raises(RuntimeError, match="does not contain requested GPU UUID"):
        controller.attest_workers([GPU_C])


def test_attestation_rejects_a_state_worker_with_a_foreign_marker(tmp_path: Path) -> None:
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "workers": [
                    {
                        "gpu_uuid": GPU_A,
                        "pid": 101,
                        "boot_id": "fake-boot-id",
                        "start_time_ticks": 1010,
                        "worker_marker": "foreign-worker",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="invalid worker identity"):
        handle_attestation(
            KeepaliveAttestationRequest(gpu_uuids=(GPU_A,)).encode(), controller=controller
        )


def test_local_controller_starts_each_gpu_directly_without_batch_rollback(tmp_path: Path) -> None:
    provider = FailingSecondProvider()
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="second worker failed"):
        controller.set_enabled(True, [GPU_A, GPU_B])

    pid_a = provider.started[0][1]
    assert provider.running == {pid_a}
    stored = json.loads((state_directory / "workers.v3.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 3
    assert stored["workers"] == [
        {
            "gpu_uuid": GPU_A,
            "pid": pid_a,
            "boot_id": "fake-boot-id",
            "start_time_ticks": pid_a * 10,
            "worker_marker": WORKER_PROCESS_MARKER,
        }
    ]


def test_local_controller_starts_batch_workers_concurrently(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentProvider(FakeProvider):
        def start(self, gpu_uuid: str) -> KeepaliveProcessIdentity:
            barrier.wait(timeout=1)
            return super().start(gpu_uuid)

    provider = ConcurrentProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    started = controller.set_enabled(True, [GPU_A, GPU_B])

    assert [result.gpu_uuid for result in started.results] == [GPU_A, GPU_B]
    assert {gpu_uuid for gpu_uuid, _pid in provider.started} == {GPU_A, GPU_B}


def test_local_controller_rejects_a_valid_but_unknown_gpu_before_mutation(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: {GPU_A},
    )

    with pytest.raises(ValueError, match="unknown"):
        controller.set_enabled(True, [GPU_B])

    assert provider.started == []


def test_local_controller_rejects_legacy_v2_worker_state(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v2.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workers": [{"gpu_uuid": GPU_A, "pid": 777}],
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "workers.v2.json").chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="legacy keepalive worker state"):
        controller.set_enabled(False, [GPU_A])

    assert provider.stopped == []
    assert (state_directory / "workers.v2.json").exists()
    assert not (state_directory / "workers.v3.json").exists()


def test_local_controller_rejects_a_pid_only_worker_state_in_v3_container(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "workers": [{"gpu_uuid": GPU_A, "pid": 777}],
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "workers.v3.json").chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="invalid worker identity"):
        controller.set_enabled(False, [GPU_A])

    assert provider.stopped == []
    assert (state_directory / "workers.v3.json").exists()


def test_local_controller_rejects_v2_payload_in_v3_state_path(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps({"schema_version": 2, "workers": []}),
        encoding="utf-8",
    )
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="schema version mismatch.*expected 3"):
        controller.set_enabled(False, [GPU_A])

    assert provider.stopped == []
    assert (state_directory / "workers.v3.json").exists()


def test_torch_provider_does_not_signal_a_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_linux_boot_id", lambda: identity.boot_id
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_start_time_ticks", lambda _pid: 101
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_command",
        lambda _pid: (
            b"python",
            b"-m",
            b"serverpilot.server_keepalive",
            b"--internal-worker",
            b"--worker-marker",
            WORKER_PROCESS_MARKER.encode("ascii"),
        ),
    )
    signal_attempts: list[int] = []

    def forbidden_signal(pid: int, _signal: int) -> None:
        signal_attempts.append(pid)

    monkeypatch.setattr("serverpilot.server_keepalive.os.kill", forbidden_signal)

    provider = TorchSubprocessProvider()
    assert provider.is_running(identity) is False
    provider.stop(identity)

    assert signal_attempts == []


def test_pidfd_syscalls_are_used_when_python_omits_native_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int | None, ...]] = []

    class FakeSyscall:
        restype: Any = None

        def __call__(self, *arguments: Any) -> int:
            values = tuple(getattr(argument, "value", argument) for argument in arguments)
            calls.append(values)
            return 17 if values[0] == keepalive_module.LINUX_PIDFD_OPEN_SYSCALL else 0

    class FakeLibc:
        syscall = FakeSyscall()

    monkeypatch.delattr(keepalive_module.os, "pidfd_open", raising=False)
    monkeypatch.delattr(keepalive_module.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(keepalive_module.sys, "platform", "linux")
    monkeypatch.setattr(keepalive_module.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())

    pidfd = keepalive_module._pidfd_open(4321)
    keepalive_module._pidfd_send_signal(pidfd, signal.SIGTERM)

    assert pidfd == 17
    assert calls == [
        (keepalive_module.LINUX_PIDFD_OPEN_SYSCALL, 4321, 0),
        (keepalive_module.LINUX_PIDFD_SEND_SIGNAL_SYSCALL, 17, signal.SIGTERM, None, 0),
    ]


def test_torch_provider_stops_through_the_pinned_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    opened: list[int] = []
    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr("serverpilot.server_keepalive._worker_process_matches", lambda _item: True)

    def fake_open(pid: int) -> int:
        opened.append(pid)
        return read_fd

    monkeypatch.setattr("serverpilot.server_keepalive._pidfd_open", fake_open)
    monkeypatch.setattr(
        "serverpilot.server_keepalive._pidfd_send_signal",
        lambda pidfd, signal_number: signaled.append((pidfd, signal_number)),
    )

    TorchSubprocessProvider().stop(identity)

    assert opened == [identity.pid]
    assert signaled == [(read_fd, signal.SIGTERM)]


def test_torch_provider_requires_the_fixed_worker_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_linux_boot_id", lambda: identity.boot_id
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_start_time_ticks",
        lambda _pid: identity.start_time_ticks,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_command",
        lambda _pid: (
            b"python",
            b"-m",
            b"serverpilot.server_keepalive",
            b"--internal-worker",
            b"--worker-marker",
            b"foreign-worker",
        ),
    )

    assert TorchSubprocessProvider().is_running(identity) is False


def test_worker_state_write_is_atomic_and_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )
    controller._ensure_state_directory()
    real_fsync = os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr("serverpilot.server_keepalive.os.fsync", recording_fsync)
    controller._write_identities({GPU_A: FakeProvider.identity(101)})

    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    directory_syncs_before_delete = sum(stat.S_ISDIR(mode) for mode in fsynced_modes)

    controller._write_identities({})

    assert not controller._state_path.exists()
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) == directory_syncs_before_delete + 1


def test_worker_state_replace_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )
    controller._ensure_state_directory()
    controller._write_identities({GPU_A: FakeProvider.identity(101)})
    previous_payload = controller._state_path.read_bytes()
    replacement_sources: list[Path] = []

    def fail_replace(source: str | os.PathLike[str], _destination: str | os.PathLike[str]) -> None:
        replacement_sources.append(Path(source))
        raise OSError("simulated replace failure")

    monkeypatch.setattr("serverpilot.server_keepalive.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        controller._write_identities(
            {
                GPU_A: FakeProvider.identity(101),
                GPU_B: FakeProvider.identity(102),
            }
        )

    assert replacement_sources[0].parent == state_directory
    assert controller._state_path.read_bytes() == previous_payload
    assert list(state_directory.glob(".workers.v3.*.tmp")) == []


def test_handle_request_returns_each_requested_gpu(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    response = handle_request(
        KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B)).encode(), controller=controller
    )

    assert [(result.gpu_uuid, result.outcome) for result in response.results] == [
        (GPU_A, "started"),
        (GPU_B, "started"),
    ]
    with pytest.raises(KeepaliveProtocolError, match="expected 3"):
        handle_request(b'{"schema_version":2,"enabled":true}', controller=controller)


def test_server_policy_holds_eighty_percent_of_cuda_visible_memory() -> None:
    assert TARGET_MEMORY_FRACTION == 0.80
    assert ACTIVE_DUTY_FRACTION == 0.80
    assert DUTY_PERIOD_SECONDS == 0.1
    eighty_gib = 80 * 1024 * 1024 * 1024
    ninety_six_gib = 96 * 1024 * 1024 * 1024
    assert keepalive_target_bytes(eighty_gib) == 64 * 1024 * 1024 * 1024
    assert keepalive_target_bytes(ninety_six_gib) == math.ceil(ninety_six_gib * 0.80)


def test_default_state_directory_is_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    assert default_state_directory() == xdg_state / "serverpilot" / "keepalive"
    assert not str(default_state_directory()).startswith("/tmp/")

    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    assert default_state_directory() == Path("relative-state/serverpilot/keepalive")
