from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from gpu_broker.adapters import (
    ADAPTER_REGISTRY,
    AdapterRegistryError,
    RAW_SSH_COMBINED_QUERY,
    RawSSHObservationAdapter,
    SlurmCommandSchedulerAdapter,
)
from gpu_broker.config import EndpointConfig
from gpu_broker.slurm import CommandSlurmProvider, SlurmProviderError


def test_registry_is_sealed_to_known_adapters() -> None:
    assert ADAPTER_REGISTRY.ids() == ("raw-ssh", "slurm-command")
    assert ADAPTER_REGISTRY.require_capability("raw-ssh", "observation").id == "raw-ssh"
    with pytest.raises(AdapterRegistryError, match="unknown adapter"):
        ADAPTER_REGISTRY.get("unknown")
    with pytest.raises(AdapterRegistryError, match="does not provide scheduler"):
        ADAPTER_REGISTRY.require_capability("raw-ssh", "scheduler")


def test_operation_schema_is_registered_metadata_with_task_approval() -> None:
    definition = ADAPTER_REGISTRY.require_capability("slurm-command", "operation")
    schemas = definition.operation_schema()
    assert {schema["id"] for schema in schemas} == {
        "scheduler.submit",
        "scheduler.cancel",
        "scheduler.upload",
    }
    forbidden = {"argv", "shell", "env", "agent_target", "password", "secret", "token"}
    for schema in schemas:
        assert schema["executes"] is False
        assert schema["approval"] == {
            "required": True,
            "field": "approval_ref",
            "current_task_only": True,
        }
        parameter_names = {parameter["name"] for parameter in schema["parameters"]}
        assert "approval_ref" in parameter_names
        assert forbidden.isdisjoint(parameter_names)


def test_provisioning_preview_is_non_executable() -> None:
    definition = ADAPTER_REGISTRY.require_capability(
        "slurm-command", "provisioning-preview"
    )
    assert definition.provisioning_previews
    assert all(not preview.executable for preview in definition.provisioning_previews)


def test_raw_ssh_adapter_runs_fixed_ssh_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok\n", b""

    async def fake_create_subprocess_exec(*command: Any, **_kwargs: Any) -> FakeProcess:
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(id="endpoint-a", host="gpu.example.test", port=2202, ssh_user="gpu")

    result = asyncio.run(
        RawSSHObservationAdapter().run_probe(
            endpoint,
            probe="endpoint-telemetry",
            connect_timeout_seconds=7,
        )
    )

    assert result.stdout == "ok\n"
    assert calls == [
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=7",
            "-p",
            "2202",
            "gpu@gpu.example.test",
            RAW_SSH_COMBINED_QUERY,
        )
    ]


def test_raw_ssh_adapter_rejects_arbitrary_or_invalid_probes() -> None:
    adapter = RawSSHObservationAdapter()
    endpoint = EndpointConfig(id="endpoint-a", host="gpu.example.test", port=2202, ssh_user="gpu")

    with pytest.raises(ValueError, match="unknown raw SSH probe"):
        asyncio.run(adapter.run_probe(endpoint, probe="arbitrary", connect_timeout_seconds=7))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive integer"):
        asyncio.run(
            adapter.run_probe(
                endpoint,
                probe="process-details",
                connect_timeout_seconds=7,
                process_ids=(0,),
            )
        )


def test_slurm_command_adapter_preserves_runner_contract() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="\x1b[31mok\r\n", stderr="")

    output = SlurmCommandSchedulerAdapter(runner=runner).run(
        {"command_prefix": ["helper", "1"]},
        ["sinfo", "-h"],
        mutating=False,
        timeout_seconds=3,
    )

    assert output == "ok"
    assert calls == [
        (
            ["helper", "1", "sinfo -h"],
            {"check": False, "capture_output": True, "text": True, "timeout": 3},
        )
    ]


def test_command_slurm_provider_uses_adapter_errors() -> None:
    provider = CommandSlurmProvider(runner=lambda *_args, **_kwargs: None)

    with pytest.raises(SlurmProviderError, match="invalid command_prefix") as exc_info:
        provider._run({"command_prefix": []}, ["sinfo"], mutating=False)

    assert exc_info.value.access_required is False
    assert exc_info.value.uncertain is False
