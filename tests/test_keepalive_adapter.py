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
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
)
from serverpilot.server_keepalive import (
    ACTIVE_DUTY_FRACTION,
    TARGET_MEMORY_FRACTION,
    LocalKeepaliveController,
    WorkerIdentity,
    _resolve_observed_host_pid,
    default_state_directory,
    handle_request,
)


def test_protocol_has_one_strict_boolean_request() -> None:
    assert KeepaliveRequest.decode(KeepaliveRequest(enabled=True).encode()).enabled is True
    with pytest.raises(KeepaliveProtocolError, match="fields"):
        KeepaliveRequest.decode('{"schema_version":1,"enabled":true,"pid":123}')
    with pytest.raises(KeepaliveProtocolError, match="boolean"):
        KeepaliveRequest.decode('{"schema_version":1,"enabled":1}')
    with pytest.raises(KeepaliveProtocolError, match="version"):
        KeepaliveRequest.decode('{"schema_version":2,"enabled":true}')


def test_keepalive_adapter_factory_is_sealed() -> None:
    assert endpoint_keepalive_adapter("server-script-v1").id == "server-script-v1"
    with pytest.raises(AdapterRegistryError, match="does not provide endpoint_keepalive"):
        endpoint_keepalive_adapter("raw-ssh")


def test_response_rejects_inconsistent_or_extra_state() -> None:
    with pytest.raises(KeepaliveProtocolError, match="inconsistent"):
        KeepaliveResponse.decode(
            '{"schema_version":1,"enabled":false,"changed":true,"status":"running",'
            '"worker":{"pid":123,"start_ticks":456}}'
        )
    with pytest.raises(KeepaliveProtocolError, match="fields"):
        KeepaliveResponse.decode(
            '{"schema_version":1,"enabled":true,"changed":true,'
            '"status":"running","worker":{"pid":123,"start_ticks":456},"path":"x"}'
        )
    with pytest.raises(KeepaliveProtocolError, match="attestation"):
        KeepaliveResponse.decode(
            '{"schema_version":1,"enabled":true,"changed":true,'
            '"status":"running","worker":null}'
        )


def test_adapter_uses_fixed_ssh_command_and_json_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any], bytes]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            calls.append((command, kwargs, payload))
            return KeepaliveResponse(
                enabled=False, changed=True, status="stopped", worker=None
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
    )

    response = asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, False))

    assert response.status == "stopped"
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
    assert json.loads(calls[0][2]) == {"schema_version": 1, "enabled": False}
    assert {"shell", "env", "pid", "gpu", "path", "argv"}.isdisjoint(
        json.loads(calls[0][2])
    )


def test_adapter_treats_invalid_remote_result_as_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes) -> tuple[bytes, bytes]:
            return b'{"enabled":true}\n', b""

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(id="endpoint-a", host="gpu.example.test", port=22, ssh_user="gpu")

    with pytest.raises(AdapterCommandError) as exc_info:
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True))

    assert exc_info.value.uncertain is True


class FakeProvider:
    def __init__(self) -> None:
        self.running: set[WorkerIdentity] = set()
        self.started = 0
        self.stopped: list[WorkerIdentity] = []

    def start(self) -> WorkerIdentity:
        self.started += 1
        identity = WorkerIdentity(pid=100 + self.started, start_ticks=200 + self.started)
        self.running.add(identity)
        return identity

    def is_running(self, identity: WorkerIdentity) -> bool:
        return identity in self.running

    def stop(self, identity: WorkerIdentity) -> None:
        self.stopped.append(identity)
        self.running.discard(identity)


def test_local_controller_is_idempotent_and_stops_only_its_recorded_worker(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        observed_pid_resolver=lambda: 9001,
    )

    first = controller.set_enabled(True)
    stored_identity = json.loads(
        (tmp_path / "keepalive" / "worker.json").read_text(encoding="utf-8")
    )
    second = controller.set_enabled(True)
    stopped = controller.set_enabled(False)
    stopped_again = controller.set_enabled(False)

    assert (first.changed, second.changed, stopped.changed, stopped_again.changed) == (
        True,
        False,
        True,
        False,
    )
    assert provider.started == 1
    assert stored_identity == {"pid": 101, "start_ticks": 201}
    assert provider.stopped == [WorkerIdentity(pid=101, start_ticks=201)]
    assert not (tmp_path / "keepalive" / "worker.json").exists()
    assert first.worker == KeepaliveWorkerAttestation(pid=9001, start_ticks=201)
    assert second.worker == first.worker


def test_local_controller_replaces_stale_owned_state_without_signalling_it(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "worker.json"
    state_path.write_text(
        '{"pid":777,"start_ticks":888}', encoding="utf-8"
    )
    state_path.chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        observed_pid_resolver=lambda: 9001,
    )

    response = controller.set_enabled(True)

    assert response == KeepaliveResponse(
        enabled=True,
        changed=True,
        status="running",
        worker=KeepaliveWorkerAttestation(pid=9001, start_ticks=201),
    )
    assert provider.stopped == []
    assert provider.started == 1


def test_request_has_no_identity_and_response_attests_only_helper_worker(tmp_path: Path) -> None:
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=tmp_path / "keepalive",
        observed_pid_resolver=lambda: 9001,
    )
    payload = KeepaliveRequest(enabled=True).encode()

    encoded_response = handle_request(payload, controller=controller).encode()

    assert b"pid" not in payload
    assert json.loads(encoded_response)["worker"] == {"pid": 9001, "start_ticks": 201}


def test_local_controller_cleans_up_when_host_pid_cannot_be_verified(tmp_path: Path) -> None:
    provider = FakeProvider()

    def fail_verification() -> int:
        raise RuntimeError("ambiguous GPU process identity")

    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        observed_pid_resolver=fail_verification,
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        controller.set_enabled(True)

    assert provider.stopped == [WorkerIdentity(pid=101, start_ticks=201)]
    assert provider.running == set()
    assert not (tmp_path / "keepalive" / "worker.json").exists()


def test_host_pid_resolver_requires_one_pid_covering_every_visible_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    outputs = iter(
        [
            "GPU-0\nGPU-1\n",
            "GPU-0, 3331894\nGPU-1, 3331894\n",
        ]
    )

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args[0])
        return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.run", fake_run)

    assert _resolve_observed_host_pid() == 3_331_894
    assert calls == [
        ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader,nounits"],
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        ],
    ]


@pytest.mark.parametrize(
    "process_output",
    [
        "GPU-0, 3331894\n",
        "GPU-0, 3331894\nGPU-1, 3331894\nGPU-0, 8888888\n",
    ],
)
def test_host_pid_resolver_rejects_partial_or_additional_compute_processes(
    monkeypatch: pytest.MonkeyPatch,
    process_output: str,
) -> None:
    outputs = iter(["GPU-0\nGPU-1\n", process_output])

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="unique compute PID"):
        _resolve_observed_host_pid()


def test_server_policy_meets_v1_keepalive_targets() -> None:
    assert TARGET_MEMORY_FRACTION >= 0.30
    assert 0.30 <= ACTIVE_DUTY_FRACTION <= 0.40


def test_default_state_directory_is_persistent_and_code_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    assert default_state_directory() == xdg_state / "serverpilot" / "keepalive"
    assert not str(default_state_directory()).startswith("/tmp/")

    monkeypatch.setenv("XDG_STATE_HOME", "relative/or/../unsafe")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_state_directory() == (
        tmp_path / "home" / ".local" / "state" / "serverpilot" / "keepalive"
    )


def test_default_state_directory_is_created_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    controller = LocalKeepaliveController(provider=FakeProvider())

    controller.set_enabled(False)

    details = controller.state_directory.lstat()
    assert details.st_uid == os.getuid()
    assert stat.S_IMODE(details.st_mode) == 0o700


def test_controller_rejects_symlinked_state_file(tmp_path: Path) -> None:
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    target = tmp_path / "foreign-state"
    target.write_text('{"pid":777,"start_ticks":888}', encoding="utf-8")
    (state_directory / "worker.json").symlink_to(target)
    controller = LocalKeepaliveController(
        provider=FakeProvider(), state_directory=state_directory
    )

    with pytest.raises(RuntimeError, match="unreadable"):
        controller.set_enabled(False)


def test_controller_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    target = tmp_path / "foreign-directory"
    target.mkdir(mode=0o700)
    state_directory = tmp_path / "keepalive"
    state_directory.symlink_to(target, target_is_directory=True)
    controller = LocalKeepaliveController(
        provider=FakeProvider(), state_directory=state_directory
    )

    with pytest.raises(RuntimeError, match="private and owned"):
        controller.set_enabled(False)
