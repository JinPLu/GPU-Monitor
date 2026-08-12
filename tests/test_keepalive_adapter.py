from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from serverpilot.adapters import (
    AdapterCommandError,
    AdapterRegistryError,
    ServerScriptKeepaliveAdapter,
    endpoint_keepalive_adapter,
)
from serverpilot.config import EndpointConfig
from serverpilot.keepalive_protocol import (
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
)
from serverpilot.server_keepalive import (
    ACTIVE_DUTY_FRACTION,
    DUTY_PERIOD_SECONDS,
    TARGET_MEMORY_FRACTION,
    LocalKeepaliveController,
    TorchSubprocessProvider,
    default_state_directory,
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
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "4"}

    assert provider.start(GPU_A) == 9876
    assert captured["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "4"


def test_torch_provider_reports_unknown_uuid_in_pci_mapping() -> None:
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "0"}

    with pytest.raises(RuntimeError, match="does not contain requested GPU UUID"):
        provider._cuda_visible_device(GPU_B)


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
    with pytest.raises(KeepaliveProtocolError, match="version 2"):
        KeepaliveRequest.decode('{"schema_version":1,"enabled":true}')
    with pytest.raises(KeepaliveProtocolError, match="malformed"):
        KeepaliveRequest.decode('{"schema_version":2,"enabled":true,"gpu_uuids":["0;touch /tmp/x"]}')
    assert KeepaliveRequest.decode(
        '{"schema_version":2,"enabled":true,"gpu_uuids":["' + GPU_A + '"],"note":"unused"}'
    ) == KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A,))


def test_response_decodes_each_gpu_result() -> None:
    valid = KeepaliveResponse(enabled=True, results=(_result(GPU_A, enabled=True),)).encode()
    assert KeepaliveResponse.decode(valid).results[0].gpu_uuid == GPU_A
    decoded = KeepaliveResponse.decode(
        json.dumps(
            {
                "schema_version": 2,
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

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            calls.append((command, kwargs, payload))
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
        "cd -- /srv/project-a && ./serverpilot-keepalive --schema-version 2",
    )
    assert json.loads(calls[0][2]) == {
        "schema_version": 2,
        "enabled": False,
        "gpu_uuids": [GPU_A],
    }
    assert {"shell", "env", "pid", "path", "argv", "command", "cuda_visible_devices"}.isdisjoint(
        json.loads(calls[0][2])
    )


def test_adapter_rejects_unknown_or_mismapped_gpu_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes) -> tuple[bytes, bytes]:
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


class FakeProvider:
    def __init__(self) -> None:
        self.running: set[int] = set()
        self.started: list[tuple[str, int]] = []
        self.stopped: list[int] = []

    def start(self, gpu_uuid: str) -> int:
        pid = 100 + len(self.started) + 1
        self.started.append((gpu_uuid, pid))
        self.running.add(pid)
        return pid

    def is_running(self, pid: int) -> bool:
        return pid in self.running

    def stop(self, pid: int) -> None:
        self.stopped.append(pid)
        self.running.discard(pid)


class FailingSecondProvider(FakeProvider):
    def start(self, gpu_uuid: str) -> int:
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
    assert provider.is_running(pid_b)
    stored = json.loads((tmp_path / "keepalive" / "workers.v2.json").read_text(encoding="utf-8"))
    assert stored["workers"] == [{"gpu_uuid": GPU_B, "pid": pid_b}]


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
    stored = json.loads((state_directory / "workers.v2.json").read_text(encoding="utf-8"))
    assert stored["workers"] == [{"gpu_uuid": GPU_A, "pid": pid_a}]


def test_local_controller_starts_batch_workers_concurrently(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentProvider(FakeProvider):
        def start(self, gpu_uuid: str) -> int:
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


def test_local_controller_forgets_a_recorded_worker_that_is_no_longer_running(tmp_path: Path) -> None:
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

    response = controller.set_enabled(False, [GPU_A])

    assert response.results[0].outcome == "stopped"
    assert provider.stopped == []
    assert not (state_directory / "workers.v2.json").exists()


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
    with pytest.raises(KeepaliveProtocolError, match="version 2"):
        handle_request(b'{"schema_version":1,"enabled":true}', controller=controller)


def test_server_policy_meets_low_impact_targets() -> None:
    assert TARGET_MEMORY_FRACTION >= 0.30
    assert 0.20 <= ACTIVE_DUTY_FRACTION <= 0.35
    assert DUTY_PERIOD_SECONDS == 0.1


def test_default_state_directory_is_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    assert default_state_directory() == xdg_state / "serverpilot" / "keepalive"
    assert not str(default_state_directory()).startswith("/tmp/")

    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    assert default_state_directory() == Path("relative-state/serverpilot/keepalive")
