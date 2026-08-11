from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
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
    KEEPALIVE_REMOTE_COMMAND,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
    LegacyKeepaliveRequest,
)
from serverpilot.server_keepalive import (
    ACTIVE_DUTY_FRACTION,
    DUTY_PERIOD_SECONDS,
    TARGET_MEMORY_FRACTION,
    LocalKeepaliveController,
    WorkerIdentity,
    _resolve_observed_host_pid,
    default_state_directory,
    handle_request,
)


GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
GPU_C = "GPU-00000000-0000-0000-0000-000000000003"
KNOWN_GPUS = {GPU_A, GPU_B, GPU_C}


def _result(
    gpu_uuid: str,
    *,
    enabled: bool,
    outcome: str = "unchanged",
    pid: int = 9001,
) -> KeepaliveGPUResult:
    return KeepaliveGPUResult(
        gpu_uuid=gpu_uuid,
        status="running" if enabled else "stopped",
        outcome=outcome,  # type: ignore[arg-type]
        worker=(KeepaliveWorkerAttestation(pid=pid, start_ticks=201) if enabled else None),
    )


def test_protocol_is_strict_per_gpu_and_retains_v1_decode_only() -> None:
    request = KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B))
    decoded = KeepaliveRequest.decode(request.encode())

    assert decoded == request
    legacy = KeepaliveRequest.decode('{"schema_version":1,"enabled":true}')
    assert legacy == LegacyKeepaliveRequest(enabled=True)
    with pytest.raises(KeepaliveProtocolError, match="duplicates"):
        KeepaliveRequest.decode(
            '{"schema_version":2,"enabled":true,"gpu_uuids":["' + GPU_A + '","' + GPU_A + '"]}'
        )
    with pytest.raises(KeepaliveProtocolError, match="malformed"):
        KeepaliveRequest.decode('{"schema_version":2,"enabled":true,"gpu_uuids":["0;touch /tmp/x"]}')
    with pytest.raises(KeepaliveProtocolError, match="fields"):
        KeepaliveRequest.decode(
            '{"schema_version":2,"enabled":true,"gpu_uuids":["' + GPU_A + '"],"env":{"X":1}}'
        )
    with pytest.raises(KeepaliveProtocolError, match="duplicate fields"):
        KeepaliveRequest.decode(
            '{"schema_version":2,"enabled":true,"enabled":false,"gpu_uuids":["' + GPU_A + '"]}'
        )


def test_response_rejects_unknown_fields_duplicates_and_inconsistent_workers() -> None:
    valid = KeepaliveResponse(enabled=True, results=(_result(GPU_A, enabled=True),)).encode()
    assert KeepaliveResponse.decode(valid).results[0].gpu_uuid == GPU_A
    with pytest.raises(KeepaliveProtocolError, match="fields"):
        KeepaliveResponse.decode(
            '{"schema_version":2,"enabled":true,"results":[],"path":"/tmp"}'
        )
    with pytest.raises(KeepaliveProtocolError, match="duplicate"):
        KeepaliveResponse.decode(
            json.dumps(
                {
                    "schema_version": 2,
                    "enabled": False,
                    "results": [
                        {"gpu_uuid": GPU_A, "status": "stopped", "outcome": "unchanged", "worker": None},
                        {"gpu_uuid": GPU_A, "status": "stopped", "outcome": "unchanged", "worker": None},
                    ],
                }
            )
        )
    with pytest.raises(KeepaliveProtocolError, match="lacks worker"):
        KeepaliveResponse.decode(
            json.dumps(
                {
                    "schema_version": 2,
                    "enabled": True,
                    "results": [
                        {"gpu_uuid": GPU_A, "status": "running", "outcome": "started", "worker": None}
                    ],
                }
            )
        )


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
    endpoint = EndpointConfig(id="endpoint-a", host="gpu.example.test", port=2202, ssh_user="gpu")

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
        KEEPALIVE_REMOTE_COMMAND,
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
    endpoint = EndpointConfig(id="endpoint-a", host="gpu.example.test", port=22, ssh_user="gpu")

    with pytest.raises(AdapterCommandError, match="exactly") as exc_info:
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A]))
    assert exc_info.value.uncertain is True
    with pytest.raises(ValueError, match="duplicates"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A, GPU_A]))
    with pytest.raises(ValueError, match="malformed"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, ["GPU-A;$(id)"]))


class FakeProvider:
    def __init__(self) -> None:
        self.running: set[WorkerIdentity] = set()
        self.started: list[tuple[str, WorkerIdentity]] = []
        self.stopped: list[WorkerIdentity] = []

    def start(self, gpu_uuid: str) -> WorkerIdentity:
        identity = WorkerIdentity(pid=100 + len(self.started) + 1, start_ticks=200 + len(self.started) + 1)
        self.started.append((gpu_uuid, identity))
        self.running.add(identity)
        return identity

    def is_running(self, identity: WorkerIdentity) -> bool:
        return identity in self.running

    def stop(self, identity: WorkerIdentity) -> None:
        self.stopped.append(identity)
        self.running.discard(identity)


def test_local_controller_manages_each_gpu_independently_and_only_stops_own_state(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        observed_pid_resolver=lambda gpu_uuid: {GPU_A: 9001, GPU_B: 9002}[gpu_uuid],
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    started = controller.set_enabled(True, [GPU_A, GPU_B])
    disabled_a = controller.set_enabled(False, [GPU_A])
    repeated_b = controller.set_enabled(True, [GPU_B])
    disabled_missing = controller.set_enabled(False, [GPU_C])

    identity_a = provider.started[0][1]
    identity_b = provider.started[1][1]
    assert [result.outcome for result in started.results] == ["started", "started"]
    assert disabled_a.results == (_result(GPU_A, enabled=False, outcome="stopped"),)
    assert repeated_b.results[0].outcome == "unchanged"
    assert disabled_missing.results[0].outcome == "unchanged"
    assert provider.stopped == [identity_a]
    assert provider.is_running(identity_b)
    stored = json.loads((tmp_path / "keepalive" / "workers.v2.json").read_text(encoding="utf-8"))
    assert stored["workers"] == [{"gpu_uuid": GPU_B, "pid": identity_b.pid, "start_ticks": identity_b.start_ticks}]


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


def test_local_controller_never_signals_stale_or_unrecorded_identity(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v2.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workers": [{"gpu_uuid": GPU_A, "pid": 777, "start_ticks": 888}],
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "workers.v2.json").chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        observed_pid_resolver=lambda _gpu_uuid: 9001,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    response = controller.set_enabled(False, [GPU_A])

    assert response.results[0].outcome == "stopped"
    assert provider.stopped == []
    assert not (state_directory / "workers.v2.json").exists()


def test_handle_request_rejects_v1_and_attests_each_requested_gpu(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        observed_pid_resolver=lambda gpu_uuid: {GPU_A: 9101, GPU_B: 9102}[gpu_uuid],
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    response = handle_request(
        KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B)).encode(), controller=controller
    )

    assert [(result.gpu_uuid, result.worker.pid if result.worker else None) for result in response.results] == [
        (GPU_A, 9101),
        (GPU_B, 9102),
    ]
    with pytest.raises(KeepaliveProtocolError, match="v1 cannot"):
        handle_request(b'{"schema_version":1,"enabled":true}', controller=controller)


def test_host_pid_resolver_checks_only_one_target_gpu_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args[0])
        return subprocess.CompletedProcess(
            [], 0, stdout=f"{GPU_A}, 3331894\n{GPU_B}, 7777777\n", stderr=""
        )

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.run", fake_run)

    assert _resolve_observed_host_pid(GPU_A) == 3_331_894
    assert calls == [["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"]]


@pytest.mark.parametrize("process_output", ["", f"{GPU_A}, 3331894\n{GPU_A}, 7777777\n"])
def test_host_pid_resolver_rejects_absent_or_ambiguous_target_process(
    monkeypatch: pytest.MonkeyPatch,
    process_output: str,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=process_output, stderr="")

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="unique compute PID"):
        _resolve_observed_host_pid(GPU_A)


def test_server_policy_meets_low_impact_targets() -> None:
    assert TARGET_MEMORY_FRACTION >= 0.30
    assert 0.20 <= ACTIVE_DUTY_FRACTION <= 0.35
    assert DUTY_PERIOD_SECONDS == 0.1


def test_default_state_directory_is_persistent_and_code_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    assert default_state_directory() == xdg_state / "serverpilot" / "keepalive"
    assert not str(default_state_directory()).startswith("/tmp/")

    monkeypatch.setenv("XDG_STATE_HOME", "relative/or/../unsafe")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_state_directory() == tmp_path / "home" / ".local" / "state" / "serverpilot" / "keepalive"


def test_controller_rejects_symlinked_state_file_and_private_directory(tmp_path: Path) -> None:
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    target = tmp_path / "foreign-state"
    target.write_text("{}", encoding="utf-8")
    (state_directory / "workers.v2.json").symlink_to(target)
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="unreadable"):
        controller.set_enabled(False, [GPU_A])

    assert stat.S_IMODE(state_directory.stat().st_mode) == 0o700
    assert state_directory.stat().st_uid == os.getuid()
