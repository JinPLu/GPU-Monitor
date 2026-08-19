from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from serverpilot import server_collector
from serverpilot.adapters import HOST_RESOURCES_QUERY, RAW_SSH_OBSERVATION_ADAPTER, RawSSHResult
from serverpilot.collector import (
    COMBINED_QUERY,
    CollectionError,
    GPU_CPU_ONLY,
    GPU_UNAVAILABLE,
    MAX_SERVER_SCRIPT_SNAPSHOT_BYTES,
    SSHCollector,
    parse_gpu_csv,
    parse_host_resource_snapshot,
    HostResourceSnapshot,
    parse_host_resources,
    parse_process_csv,
    parse_server_script_snapshot,
)
from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig
from serverpilot.collector_protocol import (
    SERVER_SCRIPT_REMOTE_COMMAND,
    SERVER_SCRIPT_SCHEMA_VERSION,
)
from serverpilot.importer import import_servers_files, parse_ssh_command


def test_gpu_and_process_csv_parser() -> None:
    samples = parse_gpu_csv(
        "0, GPU-late, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, "
        "00000000:AF:00.0\n"
        "7, GPU-early, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, "
        "00000000:01:00.0\n"
    )
    assert [
        (sample.gpu_uuid, sample.gpu_index, sample.cuda_ordinal) for sample in samples
    ] == [
        ("GPU-early", 7, 0),
        ("GPU-late", 0, 1),
    ]
    assert samples[0].memory_free_mib == 100000
    assert parse_host_resources("64\n262144 196608\n4.25\n") == (64, 4.25, 262144, 196608)
    assert parse_host_resource_snapshot("64\n262144 196608\n4.25\n1000 750\n") == HostResourceSnapshot(
        64,
        4.25,
        1000,
        750,
        262144,
        196608,
        None,
        None,
        None,
        None,
        None,
    )
    processes = parse_process_csv("GPU-0, 123, 1024, python\n")
    assert processes[0].pid == 123
    assert parse_process_csv("No running processes found\n") == []


def test_host_resource_probe_keeps_64bit_ticks_and_optional_cgroup() -> None:
    assert 'printf "%.0f %.0f\\n"' in HOST_RESOURCES_QUERY
    assert "/sys/fs/cgroup/cpu.max" in HOST_RESOURCES_QUERY
    assert "usage_usec" in HOST_RESOURCES_QUERY
    assert parse_host_resource_snapshot(
        "64\n262144 196608\n4.25\n20695902555 10347951278\n"
    ) == HostResourceSnapshot(
        64,
        4.25,
        20_695_902_555,
        10_347_951_278,
        262144,
        196608,
        None,
        None,
        None,
        None,
        None,
    )
    assert parse_host_resource_snapshot(
        "64\n262144 196608\n4.25\n20695902555 10347951278\n3000000 100000 1600000\n"
    ) == HostResourceSnapshot(
        64,
        4.25,
        20_695_902_555,
        10_347_951_278,
        262144,
        196608,
        1_600_000,
        3_000_000,
        100_000,
        None,
        None,
    )
    assert parse_host_resource_snapshot(
        "64\n262144 196608\n4.25\n20695902555 10347951278\nmax 100000 1600000\n"
    ) == HostResourceSnapshot(
        64,
        4.25,
        20_695_902_555,
        10_347_951_278,
        262144,
        196608,
        1_600_000,
        None,
        100_000,
        None,
        None,
    )


def test_host_resource_probe_rejects_invalid_cgroup_line() -> None:
    with pytest.raises(CollectionError, match="unrecognized optional line"):
        parse_host_resource_snapshot(
            "64\n262144 196608\n4.25\n20695902555 10347951278\n3000000 100000\n"
        )


def test_host_resource_probe_parses_cgroup_memory_by_marker() -> None:
    assert "mem %s %s" in HOST_RESOURCES_QUERY
    assert "/sys/fs/cgroup/memory.max" in HOST_RESOURCES_QUERY
    limited = parse_host_resource_snapshot(
        "64\n1029120 921600\n4.25\n20695902555 10347951278\n"
        "3000000 100000 1600000\nmem 261993005056 53687091200\n"
    )
    assert limited.memory_total_mib == 1_029_120
    assert limited.memory_limit_mib == 249_856
    assert limited.memory_current_mib == 51_200
    assert limited.cpu_usage_usec == 1_600_000

    unlimited = parse_host_resource_snapshot(
        "64\n1029120 921600\n4.25\n20695902555 10347951278\n"
        "mem max 53687091200\n3000000 100000 1600000\n"
    )
    assert unlimited.memory_limit_mib is None
    assert unlimited.memory_current_mib == 51_200

    unread = parse_host_resource_snapshot(
        "64\n1029120 921600\n4.25\n20695902555 10347951278\n"
    )
    assert unread.memory_limit_mib is None
    assert unread.memory_current_mib is None


def test_server_collector_assigns_cuda_ordinals_by_pci_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(argument: str) -> str:
        if argument == server_collector.GPU_QUERY:
            return (
                "0, GPU-late, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, "
                "00000000:AF:00.0\n"
                "7, GPU-early, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, "
                "00000000:01:00.0\n"
            )
        if argument == server_collector.PROCESS_QUERY:
            return "No running processes found\n"
        raise AssertionError(argument)

    monkeypatch.setattr(server_collector, "_run_nvidia_smi", fake_query)

    gpu_probe_status, gpus, processes = server_collector._gpu_snapshot()

    assert gpu_probe_status == "gpu"
    assert [
        (gpu["gpu_uuid"], gpu["gpu_index"], gpu["cuda_ordinal"]) for gpu in gpus
    ] == [
        ("GPU-early", 7, 0),
        ("GPU-late", 0, 1),
    ]
    assert processes == []


def test_server_collector_reads_cgroup_usage_and_omits_missing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cpu_max = tmp_path / "cpu.max"
    cpu_stat = tmp_path / "cpu.stat"
    cpu_max.write_text("3000000 100000\n", encoding="utf-8")
    cpu_stat.write_text("usage_usec 1600000\nuser_usec 10\n", encoding="utf-8")
    monkeypatch.setattr(server_collector, "CGROUP_CPU_MAX_PATH", str(cpu_max))
    monkeypatch.setattr(server_collector, "CGROUP_CPU_STAT_PATH", str(cpu_stat))
    assert server_collector._cgroup_cpu_snapshot() == {
        "cpu_usage_usec": 1_600_000,
        "cpu_quota_usec": 3_000_000,
        "cpu_period_usec": 100_000,
    }

    cpu_max.write_text("max 100000\n", encoding="utf-8")
    assert server_collector._cgroup_cpu_snapshot() == {
        "cpu_usage_usec": 1_600_000,
        "cpu_quota_usec": None,
        "cpu_period_usec": 100_000,
    }

    monkeypatch.setattr(server_collector, "CGROUP_CPU_MAX_PATH", str(tmp_path / "missing.max"))
    monkeypatch.setattr(server_collector, "CGROUP_CPU_STAT_PATH", str(tmp_path / "missing.stat"))
    assert server_collector._cgroup_cpu_snapshot() == {}


def test_server_collector_reads_cgroup_memory_and_omits_missing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memory_max = tmp_path / "memory.max"
    memory_current = tmp_path / "memory.current"
    memory_max.write_text("261993005056\n", encoding="utf-8")
    memory_current.write_text("53687091200\n", encoding="utf-8")
    monkeypatch.setattr(server_collector, "CGROUP_MEMORY_MAX_PATH", str(memory_max))
    monkeypatch.setattr(server_collector, "CGROUP_MEMORY_CURRENT_PATH", str(memory_current))
    assert server_collector._cgroup_memory_snapshot() == {
        "memory_limit_mib": 249_856,
        "memory_current_mib": 51_200,
    }

    memory_max.write_text("max\n", encoding="utf-8")
    assert server_collector._cgroup_memory_snapshot() == {
        "memory_limit_mib": None,
        "memory_current_mib": 51_200,
    }

    monkeypatch.setattr(server_collector, "CGROUP_MEMORY_MAX_PATH", str(tmp_path / "missing.max"))
    monkeypatch.setattr(
        server_collector, "CGROUP_MEMORY_CURRENT_PATH", str(tmp_path / "missing.current")
    )
    assert server_collector._cgroup_memory_snapshot() == {}


def test_fake_collector_never_needs_a_shell(service, inventory) -> None:
    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        if command == COMBINED_QUERY:
            return (
                "__SERVERPILOT_GPU__\n"
                "0, GPU-endpoint-a-0, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, 00000000:01:00.0\n"
                "__SERVERPILOT_PROCESSES__\n"
                "__SERVERPILOT_PROCESS_DETAILS__\n"
                "__SERVERPILOT_IDENTITY__\n"
                "host-a\nboot-a\n"
                "__SERVERPILOT_HOST_RESOURCES__\n"
                "64\n262144 196608\n4.25\n1000 750\n"
            )
        raise AssertionError(f"unexpected command {command}")

    collector = SSHCollector(inventory, runner=fake_runner)
    result = asyncio.run(collector.collect_once(service, concurrency=1))
    assert result["endpoint-a"]["gpu_count"] == 1
    snapshot = service.snapshot(service.local_actor("human"))["data"]
    assert snapshot["endpoints"][0]["host_telemetry"]["memory_available_mib"] == 196608
    assert snapshot["endpoints"][0]["host_telemetry"]["cpu_total_ticks"] == 1000
    assert snapshot["endpoints"][0]["host_telemetry"]["cpu_idle_ticks"] == 750
    # endpoint-b is intentionally a fake failure; no network access happened.
    assert result["endpoint-b"]["error"] == "AssertionError"


def test_collector_imports_process_details_with_one_combined_ssh(
    inventory: InventoryConfig,
) -> None:
    calls: list[str] = []

    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        calls.append(command)
        return (
            "__SERVERPILOT_GPU__\n"
            "0, GPU-endpoint-a-0, Test GPU, 100000, 1024, 98976, 10, 2, 35, 100.0, P0, 00000000:01:00.0\n"
            "__SERVERPILOT_PROCESSES__\n"
            "GPU-endpoint-a-0, 123, 1024, python-from-smi\n"
            "__SERVERPILOT_PROCESS_DETAILS__\n"
            "123 alice 42 python3\n"
            "__SERVERPILOT_IDENTITY__\n"
            "host-a\nboot-a\n"
            "__SERVERPILOT_HOST_RESOURCES__\n"
            "64\n262144 196608\n4.25\n1000 750\n"
        )

    observation = asyncio.run(
        SSHCollector(inventory, runner=fake_runner).observe_endpoint(inventory.endpoints[0])
    )

    assert calls == [COMBINED_QUERY]
    assert len(observation.processes) == 1
    process = observation.processes[0]
    assert process.pid == 123
    assert process.username == "alice"
    assert process.executable == "python3"
    assert process.process_started_at == observation.observed_at - timedelta(seconds=42)


def test_collector_without_processes_uses_one_combined_ssh(
    inventory: InventoryConfig,
) -> None:
    calls: list[str] = []

    async def fake_runner(_endpoint, command):  # type: ignore[no-untyped-def]
        calls.append(command)
        return (
            "__SERVERPILOT_GPU__\n"
            "0, GPU-endpoint-a-0, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0, 00000000:01:00.0\n"
            "__SERVERPILOT_PROCESSES__\n"
            "__SERVERPILOT_PROCESS_DETAILS__\n"
            "__SERVERPILOT_IDENTITY__\n"
            "host-a\nboot-a\n"
            "__SERVERPILOT_HOST_RESOURCES__\n"
            "64\n262144 196608\n4.25\n1000 750\n"
        )

    observation = asyncio.run(
        SSHCollector(inventory, runner=fake_runner).observe_endpoint(inventory.endpoints[0])
    )

    assert calls == [COMBINED_QUERY]
    assert observation.processes == []


def test_hung_probe_timeout_is_recorded_as_endpoint_failure(
    monkeypatch: pytest.MonkeyPatch,
    service,
    inventory: InventoryConfig,
) -> None:
    async def fake_run_probe(*_args, **_kwargs) -> RawSSHResult:  # type: ignore[no-untyped-def]
        raise TimeoutError("SSH observation timed out after 8 seconds for endpoint-a")

    monkeypatch.setattr(RAW_SSH_OBSERVATION_ADAPTER, "run_probe", fake_run_probe)
    collector = SSHCollector(inventory)

    result = asyncio.run(
        collector.collect_once(service, endpoints=[inventory.endpoints[0]], concurrency=1)
    )
    endpoint = service.snapshot(service.local_actor("human"))["data"]["endpoints"][0]

    assert result == {"endpoint-a": {"error": "TimeoutError"}}
    assert endpoint["monitor"]["status"] == "ERROR"
    assert endpoint["monitor"]["last_attempt_at"] is not None
    assert endpoint["monitor"]["last_success_at"] is None
    assert endpoint["monitor"]["last_error"] == (
        "TimeoutError: SSH observation timed out after 8 seconds for endpoint-a"
    )


def test_collector_keeps_unconfirmed_gpu_probe_online_for_host_telemetry(
    service, inventory
) -> None:
    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        assert command == COMBINED_QUERY
        return (
            "__SERVERPILOT_GPU__\n"
            f"{GPU_UNAVAILABLE}\n"
            "__SERVERPILOT_PROCESSES__\n"
            "__SERVERPILOT_PROCESS_DETAILS__\n"
            "__SERVERPILOT_IDENTITY__\n"
            "cpu-host\ncpu-boot\n"
            "__SERVERPILOT_HOST_RESOURCES__\n"
            "32\n131072 98304\n2.5\n"
        )

    collector = SSHCollector(inventory, runner=fake_runner)
    observation = asyncio.run(collector.observe_endpoint(inventory.endpoints[0]))

    assert observation.gpus == []
    assert observation.processes == []
    assert observation.observation_complete is False
    assert observation.gpu_probe_status == "unknown"
    assert observation.host.cpu_count == 32
    assert observation.host.memory_available_mib == 98304

    result = service.ingest_observation(observation)
    assert result["gpu_count"] == 0
    snapshot = service.snapshot(service.local_actor("human"))["data"]
    endpoint = next(value for value in snapshot["endpoints"] if value["id"] == "endpoint-a")
    assert endpoint["monitor"]["status"] == "ONLINE"
    assert endpoint["resource_kind"] == "unknown"
    assert endpoint["host_telemetry"]["cpu_count"] == 32
    assert endpoint["host_telemetry"]["memory_available_mib"] == 98304


def test_collector_keeps_cpu_only_endpoint_online_when_nvidia_smi_returns_no_rows(
    service, inventory
) -> None:
    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        assert command == COMBINED_QUERY
        return (
            "__SERVERPILOT_GPU__\n"
            f"{GPU_CPU_ONLY}\n"
            "__SERVERPILOT_PROCESSES__\n"
            "__SERVERPILOT_PROCESS_DETAILS__\n"
            "__SERVERPILOT_IDENTITY__\n"
            "cpu-host\ncpu-boot\n"
            "__SERVERPILOT_HOST_RESOURCES__\n"
            "16\n65536 49152\n1.25\n"
        )

    collector = SSHCollector(inventory, runner=fake_runner)
    observation = asyncio.run(collector.observe_endpoint(inventory.endpoints[0]))

    assert observation.gpus == []
    assert observation.processes == []
    assert observation.observation_complete is True
    assert observation.gpu_probe_status == "cpu_only"
    assert observation.host.cpu_count == 16
    assert observation.host.memory_available_mib == 49152

    service.ingest_observation(observation)
    snapshot = service.snapshot(service.local_actor("human"))["data"]
    endpoint = next(value for value in snapshot["endpoints"] if value["id"] == "endpoint-a")
    assert endpoint["monitor"]["status"] == "ONLINE"
    assert endpoint["resource_kind"] == "cpu_only"
    assert endpoint["monitor"]["last_error"] is None
    assert endpoint["host_telemetry"]["memory_available_mib"] == 49152


def _server_script_snapshot(*, gpu_probe_available: bool = True) -> dict[str, object]:
    gpus: list[dict[str, object]] = []
    processes: list[dict[str, object]] = []
    if gpu_probe_available:
        gpus = [
            {
                "gpu_index": 0,
                "cuda_ordinal": 0,
                "gpu_uuid": "GPU-script-0",
                "name": "Script GPU",
                "total_vram_mib": 80_000,
                "memory_used_mib": 1_024,
                "memory_free_mib": 78_976,
                "gpu_utilization_pct": 10,
                "memory_utilization_pct": 2,
                "temperature_c": 40,
                "power_watts": 125.5,
                "pstate": "P0",
                "health": "OK",
            }
        ]
        processes = [
            {
                "gpu_uuid": "GPU-script-0",
                "pid": 123,
                "used_memory_mib": 1_024,
                "executable": "python",
                "username": "gpu",
                "process_started_at": "2026-08-10T00:00:00+00:00",
            }
        ]
    return {
        "schema_version": SERVER_SCRIPT_SCHEMA_VERSION,
        "identity": {"hostname": "script-host", "boot_id": "script-boot"},
        "host": {
            "cpu_count": 64,
            "load_1m": 1.25,
            "cpu_total_ticks": 1000,
            "cpu_idle_ticks": 750,
            "memory_total_mib": 262_144,
            "memory_available_mib": 196_608,
        },
        "gpu_probe_available": gpu_probe_available,
        "gpus": gpus,
        "processes": processes,
    }


def _server_script_endpoint(inventory: InventoryConfig) -> EndpointConfig:
    return inventory.endpoints[0].model_copy(update={"observation_profile": "server-script-v1"})


def test_server_script_collector_uses_one_immutable_entry_and_imports_processes(
    inventory: InventoryConfig,
) -> None:
    calls: list[str] = []

    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        calls.append(command)
        return json.dumps(_server_script_snapshot())

    observation = asyncio.run(
        SSHCollector(inventory, runner=fake_runner).observe_endpoint(_server_script_endpoint(inventory))
    )

    assert calls == [SERVER_SCRIPT_REMOTE_COMMAND]
    assert observation.observation_complete is True
    assert observation.gpus[0].gpu_uuid == "GPU-script-0"
    assert observation.processes[0].pid == 123
    assert observation.processes[0].username == "gpu"


def test_server_script_collector_keeps_cpu_only_host_online(
    inventory: InventoryConfig,
) -> None:
    async def fake_runner(_endpoint, command):  # type: ignore[no-untyped-def]
        assert command == SERVER_SCRIPT_REMOTE_COMMAND
        return json.dumps(_server_script_snapshot(gpu_probe_available=False))

    observation = asyncio.run(
        SSHCollector(inventory, runner=fake_runner).observe_endpoint(_server_script_endpoint(inventory))
    )

    assert observation.observation_complete is False
    assert observation.gpus == []
    assert observation.processes == []
    assert observation.host.cpu_count == 64


def test_raw_ssh_host_snapshot_forwards_cgroup_line(inventory: InventoryConfig) -> None:
    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        assert command == COMBINED_QUERY
        return (
            "__SERVERPILOT_GPU__\n"
            f"{GPU_CPU_ONLY}\n"
            "__SERVERPILOT_PROCESSES__\n"
            "__SERVERPILOT_PROCESS_DETAILS__\n"
            "__SERVERPILOT_IDENTITY__\n"
            "host-a\nboot-a\n"
            "__SERVERPILOT_HOST_RESOURCES__\n"
            "64\n262144 196608\n4.25\n20695902555 10347951278\n"
            "3000000 100000 1600000\nmem 261993005056 53687091200\n"
        )

    observation = asyncio.run(
        SSHCollector(inventory, runner=fake_runner).observe_endpoint(inventory.endpoints[0])
    )

    assert observation.host.cpu_total_ticks == 20_695_902_555
    assert observation.host.cpu_idle_ticks == 10_347_951_278
    assert observation.host.cpu_usage_usec == 1_600_000
    assert observation.host.cpu_quota_usec == 3_000_000
    assert observation.host.cpu_period_usec == 100_000
    assert observation.host.memory_limit_mib == 249_856
    assert observation.host.memory_current_mib == 51_200


def test_server_script_parser_accepts_optional_cgroup_fields() -> None:
    snapshot = _server_script_snapshot()
    snapshot["host"]["cpu_usage_usec"] = 1_600_000
    snapshot["host"]["cpu_quota_usec"] = 3_000_000
    snapshot["host"]["cpu_period_usec"] = 100_000
    observed_at = datetime(2026, 8, 10, tzinfo=timezone.utc)
    observation = parse_server_script_snapshot(
        json.dumps(snapshot),
        endpoint_id="endpoint-a",
        observed_at=observed_at,
    )
    assert observation.host.cpu_usage_usec == 1_600_000
    assert observation.host.cpu_quota_usec == 3_000_000
    assert observation.host.cpu_period_usec == 100_000

    snapshot["host"]["cpu_quota_usec"] = None
    snapshot["host"]["memory_limit_mib"] = 249_856
    snapshot["host"]["memory_current_mib"] = 51_200
    observation = parse_server_script_snapshot(
        json.dumps(snapshot),
        endpoint_id="endpoint-a",
        observed_at=observed_at,
    )
    assert observation.host.cpu_quota_usec is None
    assert observation.host.memory_limit_mib == 249_856
    assert observation.host.memory_current_mib == 51_200


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"schema_version": 1}),
        '{"schema_version":1,"schema_version":1}',
        json.dumps({**_server_script_snapshot(), "unexpected": True}),
    ],
)
def test_server_script_parser_rejects_malformed_or_unsafe_snapshot(raw: str) -> None:
    with pytest.raises(CollectionError):
        parse_server_script_snapshot(
            raw,
            endpoint_id="endpoint-a",
            observed_at=datetime.now(timezone.utc),
        )


def test_server_script_parser_rejects_oversized_or_duplicate_gpu_identities() -> None:
    oversized = "x" * (MAX_SERVER_SCRIPT_SNAPSHOT_BYTES + 1)
    with pytest.raises(CollectionError, match="stdout limit"):
        parse_server_script_snapshot(
            oversized,
            endpoint_id="endpoint-a",
            observed_at=datetime.now(timezone.utc),
        )

    duplicate = _server_script_snapshot()
    gpus = duplicate["gpus"]
    assert isinstance(gpus, list)
    gpus.append(dict(gpus[0]))
    with pytest.raises(CollectionError, match="duplicate GPU"):
        parse_server_script_snapshot(
            json.dumps(duplicate),
            endpoint_id="endpoint-a",
            observed_at=datetime.now(timezone.utc),
        )


def test_importer_keeps_same_ip_different_ports_distinct(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("# ssh -p 1111 root@10.0.0.1\n# ssh -p 2222 root@10.0.0.1\n", encoding="utf-8")
    second.write_text("# ssh -p 1111 root@10.0.0.1\n", encoding="utf-8")
    report = import_servers_files(
        [first, second],
        project_ids=["project-a"],
        workspace_path="/srv/imported-project",
    )
    assert [endpoint.port for endpoint in report.endpoints] == [1111, 2222]
    assert report.duplicate_addresses == ["10.0.0.1:1111"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ssh gpu@GPU-HOST", ("gpu", "gpu-host", 22, "server-gpu-host-p22")),
        ("  ssh   -p 2202   root@10.0.0.2  ", ("root", "10.0.0.2", 2202, "server-10-0-0-2-p2202")),
        ("ssh _svc-user@node-1.example.com", ("_svc-user", "node-1.example.com", 22, "server-node-1-example-com-p22")),
    ],
)
def test_strict_ssh_command_parser_accepts_only_destination_form(
    command: str,
    expected: tuple[str, str, int, str],
) -> None:
    parsed = parse_ssh_command(command)
    assert (parsed.user, parsed.host, parsed.port, parsed.endpoint_id) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ssh -v gpu@host",
        "ssh -p22 gpu@host",
        "ssh -o BatchMode=yes gpu@host",
        "ssh gpu@host uptime",
        "ssh gpu@host # comment",
        "# ssh gpu@host",
        "ssh gpu@host\n",
        "ssh\tgpu@host",
        "ssh://gpu@host",
        "ssh gpu@[::1]",
        "ssh gpu@::1",
        "ssh gpu@host;whoami",
        "ssh gpu@host|cat",
        "ssh host",
        "ssh @host",
        "ssh 1gpu@host",
        "ssh gpu@bad_host",
        "ssh gpu@-host",
        "ssh gpu@host-",
        "ssh gpu@999.1.1.1",
        "ssh -p 0 gpu@host",
        "ssh -p 65536 gpu@host",
        "ssh -p port gpu@host",
        "ssh gpu@host other@host",
    ],
)
def test_strict_ssh_command_parser_rejects_unsafe_or_ambiguous_forms(command: str) -> None:
    with pytest.raises(ValueError):
        parse_ssh_command(command)


def test_strict_ssh_command_parser_has_no_external_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("strict parsing must not perform external I/O")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(Path, "read_text", unexpected)
    assert parse_ssh_command("ssh gpu@host").host == "host"


def test_inventory_allows_no_initial_endpoints() -> None:
    inventory = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A")],
        endpoints=[],
    )
    assert inventory.endpoints == []


def test_inventory_allows_no_projects_or_endpoint_project_scope() -> None:
    inventory = InventoryConfig(
        schema_version=1,
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                workspace_path="/srv/project-a",
            )
        ],
    )
    assert inventory.projects == []
    assert inventory.endpoints[0].project_ids == []
