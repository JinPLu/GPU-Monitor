from __future__ import annotations

import asyncio
import socket
import subprocess
from pathlib import Path

import pytest

from gpu_broker.collector import (
    COMBINED_QUERY,
    SSHCollector,
    parse_gpu_csv,
    parse_host_resources,
    parse_process_csv,
)
from gpu_broker.config import EndpointConfig, InventoryConfig, ProjectConfig
from gpu_broker.importer import import_servers_files, parse_ssh_command


def test_gpu_and_process_csv_parser() -> None:
    samples = parse_gpu_csv("0, GPU-0, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0\n")
    assert samples[0].gpu_uuid == "GPU-0"
    assert samples[0].memory_free_mib == 100000
    assert parse_host_resources("64\n262144 196608\n4.25\n") == (64, 4.25, 262144, 196608)
    processes = parse_process_csv("GPU-0, 123, 1024, python\n")
    assert processes[0].pid == 123
    assert parse_process_csv("No running processes found\n") == []


def test_fake_collector_never_needs_a_shell(service, inventory) -> None:
    async def fake_runner(endpoint, command):  # type: ignore[no-untyped-def]
        assert endpoint.id == "endpoint-a"
        if command == COMBINED_QUERY:
            return (
                "__GPU_BROKER_GPU__\n"
                "0, GPU-endpoint-a-0, Test GPU, 100000, 0, 100000, 0, 0, 35, 100.0, P0\n"
                "__GPU_BROKER_PROCESSES__\n"
                "__GPU_BROKER_IDENTITY__\n"
                "host-a\nboot-a\n"
                "__GPU_BROKER_HOST_RESOURCES__\n"
                "64\n262144 196608\n4.25\n"
            )
        raise AssertionError(f"unexpected command {command}")

    collector = SSHCollector(inventory, runner=fake_runner)
    result = asyncio.run(collector.collect_once(service, concurrency=1))
    assert result["endpoint-a"]["gpu_count"] == 1
    snapshot = service.snapshot(service.local_actor("human"))["data"]
    assert snapshot["endpoints"][0]["host_telemetry"]["memory_available_mib"] == 196608
    # endpoint-b is intentionally a fake failure; no network access happened.
    assert result["endpoint-b"]["error"] == "AssertionError"


def test_importer_keeps_same_ip_different_ports_distinct(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("# ssh -p 1111 root@10.0.0.1\n# ssh -p 2222 root@10.0.0.1\n", encoding="utf-8")
    second.write_text("# ssh -p 1111 root@10.0.0.1\n", encoding="utf-8")
    report = import_servers_files([first, second], project_ids=["project-a"])
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
            )
        ],
    )
    assert inventory.projects == []
    assert inventory.endpoints[0].project_ids == []
