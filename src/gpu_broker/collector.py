"""Fixed-command, read-only SSH telemetry collector.

No caller-provided shell is accepted. Endpoint host/port/user come only from the
strict inventory config; commands below are immutable allowlisted probes.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable

from gpu_broker.config import EndpointConfig, InventoryConfig
from gpu_broker.schemas import EndpointObservation, ProcessInput, TelemetryInput
from gpu_broker.service import BrokerService
from gpu_broker.timeutil import utcnow


GPU_QUERY = (
    "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
    "utilization.gpu,utilization.memory,temperature.gpu,power.draw,pstate "
    "--format=csv,noheader,nounits"
)
PROCESS_QUERY = (
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name "
    "--format=csv,noheader,nounits"
)
IDENTITY_QUERY = "hostname; cat /proc/sys/kernel/random/boot_id"
HOST_RESOURCES_QUERY = (
    "getconf _NPROCESSORS_ONLN; "
    "awk '/MemTotal:/{total=$2} /MemAvailable:/{available=$2} "
    "END {printf \"%d %d\\n\", total/1024, available/1024}' /proc/meminfo; "
    "cut -d ' ' -f1 /proc/loadavg"
)
GPU_SECTION = "__GPU_BROKER_GPU__"
PROCESS_SECTION = "__GPU_BROKER_PROCESSES__"
IDENTITY_SECTION = "__GPU_BROKER_IDENTITY__"
HOST_RESOURCES_SECTION = "__GPU_BROKER_HOST_RESOURCES__"
COMBINED_QUERY = (
    f"set -e; printf '{GPU_SECTION}\\n'; {GPU_QUERY}; "
    f"printf '{PROCESS_SECTION}\\n'; {PROCESS_QUERY}; "
    f"printf '{IDENTITY_SECTION}\\n'; {IDENTITY_QUERY}; "
    f"printf '{HOST_RESOURCES_SECTION}\\n'; {HOST_RESOURCES_QUERY}"
)


class CollectionError(RuntimeError):
    pass


Runner = Callable[[EndpointConfig, str], Awaitable[str]]


def _value(row: list[str], index: int) -> str | None:
    value = row[index].strip() if index < len(row) else ""
    return None if value in {"", "N/A", "[Not Supported]", "Not Supported"} else value


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.replace("MiB", "").replace("W", "").replace("%", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace("W", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_gpu_csv(raw: str) -> list[TelemetryInput]:
    """Parse the fixed `nvidia-smi --query-gpu` CSV, rejecting unsafe partial rows."""

    samples: list[TelemetryInput] = []
    for row in csv.reader(raw.splitlines()):
        if not row or not any(column.strip() for column in row):
            continue
        index = _int(_value(row, 0))
        uuid = _value(row, 1)
        name = _value(row, 2)
        total = _int(_value(row, 3))
        used = _int(_value(row, 4))
        free = _int(_value(row, 5))
        if index is None or uuid is None or name is None or total is None or used is None or free is None:
            raise CollectionError("nvidia-smi GPU output is missing an identity or memory field")
        samples.append(
            TelemetryInput(
                gpu_index=index,
                gpu_uuid=uuid,
                name=name,
                total_vram_mib=total,
                memory_used_mib=used,
                memory_free_mib=free,
                gpu_utilization_pct=_int(_value(row, 6)),
                memory_utilization_pct=_int(_value(row, 7)),
                temperature_c=_int(_value(row, 8)),
                power_watts=_float(_value(row, 9)),
                pstate=_value(row, 10),
                health="OK",
            )
        )
    if not samples:
        raise CollectionError("nvidia-smi GPU output is empty")
    if len({sample.gpu_uuid for sample in samples}) != len(samples):
        raise CollectionError("nvidia-smi GPU output contains duplicate UUIDs")
    return samples


@dataclass(frozen=True, slots=True)
class ComputeApp:
    gpu_uuid: str
    pid: int
    used_memory_mib: int
    process_name: str


def parse_process_csv(raw: str) -> list[ComputeApp]:
    if raw.strip().lower().startswith("no running processes") or not raw.strip():
        return []
    values: list[ComputeApp] = []
    for row in csv.reader(raw.splitlines()):
        if not row or not any(column.strip() for column in row):
            continue
        gpu_uuid = _value(row, 0)
        pid = _int(_value(row, 1))
        used_memory = _int(_value(row, 2))
        name = _value(row, 3) or "unknown"
        if gpu_uuid is None or pid is None or used_memory is None:
            raise CollectionError("nvidia-smi process output is missing GPU UUID, PID, or memory")
        values.append(ComputeApp(gpu_uuid, pid, used_memory, name))
    return values


def parse_identity(raw: str) -> tuple[str, str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        raise CollectionError("host identity probe did not return hostname and boot id")
    return lines[0], lines[1]


def parse_host_resources(raw: str) -> tuple[int, float, int, int]:
    """Parse CPU capacity/load and Linux MemAvailable from the immutable probe."""

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 3:
        raise CollectionError("host resource probe must return CPU count, memory, and load")
    cpu_count = _int(lines[0])
    memory = lines[1].split()
    load_1m = _float(lines[2])
    if cpu_count is None or len(memory) != 2 or load_1m is None:
        raise CollectionError("host resource probe returned invalid values")
    memory_total = _int(memory[0])
    memory_available = _int(memory[1])
    if (
        cpu_count < 1
        or memory_total is None
        or memory_total < 1
        or memory_available is None
        or memory_available < 0
        or memory_available > memory_total
    ):
        raise CollectionError("host resource probe returned out-of-range values")
    return cpu_count, load_1m, memory_total, memory_available


def parse_combined_probe(raw: str) -> tuple[str, str, str, str]:
    """Split the single fixed SSH probe into GPU, process, identity and host resource output."""

    try:
        gpu_marker, rest = raw.split(GPU_SECTION, maxsplit=1)
        gpu_raw, rest = rest.split(PROCESS_SECTION, maxsplit=1)
        process_raw, rest = rest.split(IDENTITY_SECTION, maxsplit=1)
        identity_raw, host_raw = rest.split(HOST_RESOURCES_SECTION, maxsplit=1)
    except ValueError as exc:
        raise CollectionError("combined SSH probe returned incomplete section markers") from exc
    if gpu_marker.strip():
        raise CollectionError("combined SSH probe returned data before its first section marker")
    return gpu_raw.strip(), process_raw.strip(), identity_raw.strip(), host_raw.strip()


def parse_ps_output(raw: str, observed_at) -> dict[int, tuple[str | None, object, str]]:  # noqa: ANN001
    """Map PID to (username, approximate-start, executable) from fixed `ps` output.

    `etimes` avoids transmitting a full command line. Start time derives from the
    collector's UTC observation time and is used with boot id to avoid PID reuse.
    """

    values: dict[int, tuple[str | None, object, str]] = {}
    for line in raw.splitlines():
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid = _int(parts[0])
        elapsed = _int(parts[2])
        if pid is None or elapsed is None:
            continue
        values[pid] = (parts[1] or None, observed_at - timedelta(seconds=elapsed), parts[3])
    return values


async def default_runner(
    endpoint: EndpointConfig, remote_command: str, connect_timeout_seconds: int = 8
) -> str:
    """Execute one immutable command over SSH without a local shell.

    The remote command is one of this module's constants or a `ps` form whose
    only interpolated values are numeric PIDs parsed from nvidia-smi output.
    """

    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
        "-p",
        str(endpoint.port),
        f"{endpoint.ssh_user}@{endpoint.host}",
        remote_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")[:500]
        raise CollectionError(f"SSH probe failed for {endpoint.id}: {detail or process.returncode}")
    return stdout.decode("utf-8", errors="replace")


class SSHCollector:
    def __init__(self, inventory: InventoryConfig, runner: Runner = default_runner) -> None:
        self.inventory = inventory
        self.runner = runner

    async def _run(self, endpoint: EndpointConfig, command: str) -> str:
        if self.runner is default_runner:
            return await default_runner(
                endpoint, command, self.inventory.collector.ssh_connect_timeout_seconds
            )
        return await self.runner(endpoint, command)

    async def observe_endpoint(self, endpoint: EndpointConfig) -> EndpointObservation:
        observed_at = utcnow()
        gpu_raw, process_raw, identity_raw, host_raw = parse_combined_probe(
            await self._run(endpoint, COMBINED_QUERY)
        )
        gpus = parse_gpu_csv(gpu_raw)
        apps = parse_process_csv(process_raw)
        _hostname, boot_id = parse_identity(identity_raw)
        cpu_count, load_1m, memory_total_mib, memory_available_mib = parse_host_resources(host_raw)
        pids = sorted({app.pid for app in apps})
        details: dict[int, tuple[str | None, object, str]] = {}
        if pids:
            # Values are parsed positive integers only; no client input is interpolated.
            ps_command = "ps -o pid=,user=,etimes=,comm= -p " + ",".join(str(pid) for pid in pids)
            details = parse_ps_output(await self._run(endpoint, ps_command), observed_at)
        processes = []
        for app in apps:
            username, started_at, executable = details.get(
                app.pid, (None, observed_at, app.process_name)
            )
            processes.append(
                ProcessInput(
                    gpu_uuid=app.gpu_uuid,
                    pid=app.pid,
                    used_memory_mib=app.used_memory_mib,
                    executable=executable,
                    username=username,
                    process_started_at=started_at,
                )
            )
        return EndpointObservation(
            endpoint_id=endpoint.id,
            observed_at=observed_at,
            boot_id=boot_id,
            host={
                "cpu_count": cpu_count,
                "load_1m": load_1m,
                "memory_total_mib": memory_total_mib,
                "memory_available_mib": memory_available_mib,
            },
            gpus=gpus,
            processes=processes,
        )

    async def collect_once(
        self,
        service: BrokerService,
        *,
        concurrency: int = 5,
        endpoints: list[EndpointConfig] | None = None,
        stagger_seconds: float = 0.0,
    ) -> dict[str, object]:
        semaphore = asyncio.Semaphore(concurrency)

        async def collect(index: int, endpoint: EndpointConfig) -> tuple[str, dict[str, object]]:
            if stagger_seconds > 0 and index:
                await asyncio.sleep(index * stagger_seconds)
            async with semaphore:
                try:
                    observation = await self.observe_endpoint(endpoint)
                    return endpoint.id, service.ingest_observation(observation)
                except Exception as exc:
                    # Service records only the bounded failure class/message, never SSH secrets.
                    service.record_provider_failure(endpoint.id, f"{type(exc).__name__}: {exc}")
                    return endpoint.id, {"error": type(exc).__name__}

        # DB inventory is the mutable owner after bootstrap; YAML only seeds it.
        selected = endpoints if endpoints is not None else service.collector_endpoints()
        results = await asyncio.gather(
            *(collect(index, endpoint) for index, endpoint in enumerate(selected))
        )
        return {endpoint_id: result for endpoint_id, result in results}
