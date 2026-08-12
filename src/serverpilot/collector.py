"""Fixed-command, read-only SSH telemetry collector.

No caller-provided shell is accepted. Endpoint host/port/user come only from the
strict inventory config; commands below are immutable allowlisted probes.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from serverpilot.adapters import (
    GPU_SECTION,
    GPU_UNAVAILABLE,
    HOST_RESOURCES_SECTION,
    IDENTITY_SECTION,
    PROCESS_DETAILS_SECTION,
    PROCESS_SECTION,
    RAW_SSH_COMBINED_QUERY,
    RAW_SSH_OBSERVATION_ADAPTER,
    MAX_RAW_SSH_STDOUT_BYTES,
    RawSSHProbe,
)
from serverpilot.config import EndpointConfig, InventoryConfig
from serverpilot.collector_protocol import (
    SERVER_SCRIPT_REMOTE_COMMAND,
    SERVER_SCRIPT_SCHEMA_VERSION,
)
from serverpilot.schemas import EndpointObservation, ProcessInput, TelemetryInput
from serverpilot.service import BrokerService
from serverpilot.timeutil import utcnow


# Compatibility alias for deterministic test runners. Production collection
# passes a typed probe kind to the sealed adapter instead of a command string.
COMBINED_QUERY = RAW_SSH_COMBINED_QUERY

# The adapter enforces this bound for real SSH subprocesses.  The collector
# repeats it for injected runners so tests and future adapters cannot bypass
# the protocol's memory/fail-closed boundary.
MAX_SERVER_SCRIPT_SNAPSHOT_BYTES = MAX_RAW_SSH_STDOUT_BYTES
MAX_SERVER_SCRIPT_GPU_COUNT = 1024
MAX_SERVER_SCRIPT_PROCESS_COUNT = 16_384


class CollectionError(RuntimeError):
    pass


Runner = Callable[[EndpointConfig, str], Awaitable[str]]


def _no_duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CollectionError(f"server collector JSON contains duplicate key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise CollectionError(f"server collector JSON contains invalid numeric constant: {value}")


def _mapping(value: Any, *, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectionError(f"server collector {label} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unexpected = sorted(actual - keys)
        raise CollectionError(
            f"server collector {label} has invalid fields "
            f"(missing={missing}, unexpected={unexpected})"
        )
    return value


def _list(value: Any, *, label: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CollectionError(f"server collector {label} must be a list of at most {maximum} values")
    return value


def _text(value: Any, *, label: str, maximum: int, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CollectionError(f"server collector {label} must be a non-empty string")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CollectionError(f"server collector {label} contains unsafe whitespace or control characters")
    return value


def _integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int | None = None,
    nullable: bool = False,
) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise CollectionError(f"server collector {label} must be an integer in range")
    return value


def _number(
    value: Any,
    *,
    label: str,
    minimum: float,
    maximum: float | None = None,
    nullable: bool = False,
) -> float | None:
    if value is None and nullable:
        return None
    if type(value) not in {int, float}:
        raise CollectionError(f"server collector {label} must be a JSON number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise CollectionError(f"server collector {label} must be a finite number in range")
    return number


def _timestamp(value: Any, *, label: str) -> datetime:
    text = _text(value, label=label, maximum=64)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionError(f"server collector {label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CollectionError(f"server collector {label} must include a timezone")
    return parsed


def parse_server_script_snapshot(
    raw: str,
    *,
    endpoint_id: str,
    observed_at: datetime,
) -> EndpointObservation:
    """Validate a schema-v1 server-script snapshot before building domain input.

    This accepts one JSON object only.  It rejects duplicate object keys,
    unknown fields, type coercion, unbounded collections, duplicate GPU/process
    identities, and GPU/process relationships that the broker cannot safely
    attribute to one endpoint.
    """

    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CollectionError("server collector stdout is not valid Unicode") from exc
    if len(encoded) > MAX_SERVER_SCRIPT_SNAPSHOT_BYTES:
        raise CollectionError("server collector snapshot exceeded the stdout limit")
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_no_duplicate_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, CollectionError) as exc:
        raise CollectionError(f"server collector returned invalid JSON: {exc}") from exc
    snapshot = _mapping(
        decoded,
        label="snapshot",
        keys={"schema_version", "identity", "host", "gpu_probe_available", "gpus", "processes"},
    )
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != SERVER_SCRIPT_SCHEMA_VERSION
    ):
        raise CollectionError(
            f"server collector schema_version must be {SERVER_SCRIPT_SCHEMA_VERSION}"
        )
    if type(snapshot["gpu_probe_available"]) is not bool:
        raise CollectionError("server collector gpu_probe_available must be a boolean")

    identity = _mapping(snapshot["identity"], label="identity", keys={"hostname", "boot_id"})
    _text(identity["hostname"], label="identity.hostname", maximum=253)
    boot_id = _text(identity["boot_id"], label="identity.boot_id", maximum=120)
    assert boot_id is not None
    host = _mapping(
        snapshot["host"],
        label="host",
        keys={
            "cpu_count",
            "load_1m",
            "cpu_total_ticks",
            "cpu_idle_ticks",
            "memory_total_mib",
            "memory_available_mib",
        },
    )
    cpu_count = _integer(host["cpu_count"], label="host.cpu_count", minimum=1, maximum=1_048_576)
    load_1m = _number(host["load_1m"], label="host.load_1m", minimum=0)
    cpu_total_ticks = _integer(
        host["cpu_total_ticks"], label="host.cpu_total_ticks", minimum=0
    )
    cpu_idle_ticks = _integer(host["cpu_idle_ticks"], label="host.cpu_idle_ticks", minimum=0)
    memory_total_mib = _integer(
        host["memory_total_mib"], label="host.memory_total_mib", minimum=1
    )
    memory_available_mib = _integer(
        host["memory_available_mib"], label="host.memory_available_mib", minimum=0
    )
    assert (
        cpu_count is not None
        and load_1m is not None
        and cpu_total_ticks is not None
        and cpu_idle_ticks is not None
        and memory_total_mib is not None
        and memory_available_mib is not None
    )
    if cpu_idle_ticks > cpu_total_ticks or memory_available_mib > memory_total_mib:
        raise CollectionError("server collector host telemetry is internally inconsistent")

    gpus: list[TelemetryInput] = []
    gpu_uuids: set[str] = set()
    gpu_indexes: set[int] = set()
    gpu_totals: dict[str, int] = {}
    for position, value in enumerate(
        _list(snapshot["gpus"], label="gpus", maximum=MAX_SERVER_SCRIPT_GPU_COUNT)
    ):
        gpu = _mapping(
            value,
            label=f"gpus[{position}]",
            keys={
                "gpu_index",
                "gpu_uuid",
                "name",
                "total_vram_mib",
                "memory_used_mib",
                "memory_free_mib",
                "gpu_utilization_pct",
                "memory_utilization_pct",
                "temperature_c",
                "power_watts",
                "pstate",
                "health",
            },
        )
        gpu_index = _integer(gpu["gpu_index"], label=f"gpus[{position}].gpu_index", minimum=0, maximum=1024)
        gpu_uuid = _text(gpu["gpu_uuid"], label=f"gpus[{position}].gpu_uuid", maximum=160)
        name = _text(gpu["name"], label=f"gpus[{position}].name", maximum=255)
        total = _integer(gpu["total_vram_mib"], label=f"gpus[{position}].total_vram_mib", minimum=1)
        used = _integer(gpu["memory_used_mib"], label=f"gpus[{position}].memory_used_mib", minimum=0)
        free = _integer(gpu["memory_free_mib"], label=f"gpus[{position}].memory_free_mib", minimum=0)
        gpu_utilization = _integer(
            gpu["gpu_utilization_pct"],
            label=f"gpus[{position}].gpu_utilization_pct",
            minimum=0,
            maximum=100,
            nullable=True,
        )
        memory_utilization = _integer(
            gpu["memory_utilization_pct"],
            label=f"gpus[{position}].memory_utilization_pct",
            minimum=0,
            maximum=100,
            nullable=True,
        )
        temperature = _integer(
            gpu["temperature_c"],
            label=f"gpus[{position}].temperature_c",
            minimum=-100,
            maximum=300,
            nullable=True,
        )
        power = _number(
            gpu["power_watts"], label=f"gpus[{position}].power_watts", minimum=0, nullable=True
        )
        pstate = _text(gpu["pstate"], label=f"gpus[{position}].pstate", maximum=32, nullable=True)
        health = _text(gpu["health"], label=f"gpus[{position}].health", maximum=32)
        assert (
            gpu_index is not None
            and gpu_uuid is not None
            and name is not None
            and total is not None
            and used is not None
            and free is not None
            and health is not None
        )
        if gpu_uuid in gpu_uuids or gpu_index in gpu_indexes:
            raise CollectionError("server collector snapshot contains duplicate GPU identities")
        if used > total or free > total:
            raise CollectionError("server collector GPU memory telemetry is internally inconsistent")
        gpu_uuids.add(gpu_uuid)
        gpu_indexes.add(gpu_index)
        gpu_totals[gpu_uuid] = total
        gpus.append(
            TelemetryInput(
                gpu_index=gpu_index,
                gpu_uuid=gpu_uuid,
                name=name,
                total_vram_mib=total,
                memory_used_mib=used,
                memory_free_mib=free,
                gpu_utilization_pct=gpu_utilization,
                memory_utilization_pct=memory_utilization,
                temperature_c=temperature,
                power_watts=power,
                pstate=pstate,
                health=health,
            )
        )

    processes: list[ProcessInput] = []
    process_identities: set[tuple[str, int]] = set()
    for position, value in enumerate(
        _list(snapshot["processes"], label="processes", maximum=MAX_SERVER_SCRIPT_PROCESS_COUNT)
    ):
        process = _mapping(
            value,
            label=f"processes[{position}]",
            keys={
                "gpu_uuid",
                "pid",
                "used_memory_mib",
                "executable",
                "username",
                "process_started_at",
            },
        )
        gpu_uuid = _text(process["gpu_uuid"], label=f"processes[{position}].gpu_uuid", maximum=160)
        pid = _integer(process["pid"], label=f"processes[{position}].pid", minimum=1, maximum=2**31 - 1)
        used_memory_mib = _integer(
            process["used_memory_mib"], label=f"processes[{position}].used_memory_mib", minimum=0
        )
        executable = _text(process["executable"], label=f"processes[{position}].executable", maximum=255)
        username = _text(
            process["username"], label=f"processes[{position}].username", maximum=120, nullable=True
        )
        process_started_at = _timestamp(
            process["process_started_at"], label=f"processes[{position}].process_started_at"
        )
        assert gpu_uuid is not None and pid is not None and used_memory_mib is not None and executable is not None
        identity_key = (gpu_uuid, pid)
        if (
            gpu_uuid not in gpu_uuids
            or identity_key in process_identities
            or used_memory_mib > gpu_totals[gpu_uuid]
        ):
            raise CollectionError("server collector snapshot contains invalid or duplicate process identities")
        process_identities.add(identity_key)
        processes.append(
            ProcessInput(
                gpu_uuid=gpu_uuid,
                pid=pid,
                used_memory_mib=used_memory_mib,
                executable=executable,
                username=username,
                process_started_at=process_started_at,
            )
        )

    gpu_probe_available = snapshot["gpu_probe_available"]
    if not gpu_probe_available and (gpus or processes):
        raise CollectionError("server collector marked GPU probe unavailable but returned GPU data")
    if gpu_probe_available and not gpus:
        raise CollectionError("server collector marked GPU probe available but returned no GPUs")
    return EndpointObservation(
        endpoint_id=endpoint_id,
        observed_at=observed_at,
        boot_id=boot_id,
        host={
            "cpu_count": cpu_count,
            "load_1m": load_1m,
            "cpu_total_ticks": cpu_total_ticks,
            "cpu_idle_ticks": cpu_idle_ticks,
            "memory_total_mib": memory_total_mib,
            "memory_available_mib": memory_available_mib,
        },
        gpus=gpus,
        processes=processes,
        observation_complete=gpu_probe_available,
    )


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


def parse_host_resource_snapshot(raw: str) -> tuple[int, float, int | None, int | None, int, int]:
    """Parse CPU capacity/load and Linux MemAvailable from the immutable probe."""

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) not in {3, 4}:
        raise CollectionError("host resource probe must return CPU count, memory, and load")
    cpu_count = _int(lines[0])
    memory = lines[1].split()
    load_1m = _float(lines[2])
    cpu_total_ticks: int | None = None
    cpu_idle_ticks: int | None = None
    if len(lines) == 4:
        cpu_ticks = lines[3].split()
        if len(cpu_ticks) != 2:
            raise CollectionError("host resource probe returned invalid CPU ticks")
        cpu_total_ticks = _int(cpu_ticks[0])
        cpu_idle_ticks = _int(cpu_ticks[1])
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
        or (cpu_total_ticks is not None and cpu_total_ticks < 0)
        or (cpu_idle_ticks is not None and cpu_idle_ticks < 0)
        or (
            cpu_total_ticks is not None
            and cpu_idle_ticks is not None
            and cpu_idle_ticks > cpu_total_ticks
        )
    ):
        raise CollectionError("host resource probe returned out-of-range values")
    return cpu_count, load_1m, cpu_total_ticks, cpu_idle_ticks, memory_total, memory_available


def parse_host_resources(raw: str) -> tuple[int, float, int, int]:
    cpu_count, load_1m, _cpu_total_ticks, _cpu_idle_ticks, memory_total, memory_available = (
        parse_host_resource_snapshot(raw)
    )
    return cpu_count, load_1m, memory_total, memory_available


def parse_combined_probe(raw: str) -> tuple[str, str, str, str, str]:
    """Split one fixed SSH probe into GPU, process, details, identity, and host output."""

    try:
        gpu_marker, rest = raw.split(GPU_SECTION, maxsplit=1)
        gpu_raw, rest = rest.split(PROCESS_SECTION, maxsplit=1)
        process_raw, rest = rest.split(PROCESS_DETAILS_SECTION, maxsplit=1)
        process_details_raw, rest = rest.split(IDENTITY_SECTION, maxsplit=1)
        identity_raw, host_raw = rest.split(HOST_RESOURCES_SECTION, maxsplit=1)
    except ValueError as exc:
        raise CollectionError("combined SSH probe returned incomplete section markers") from exc
    if gpu_marker.strip():
        raise CollectionError("combined SSH probe returned data before its first section marker")
    return (
        gpu_raw.strip(),
        process_raw.strip(),
        process_details_raw.strip(),
        identity_raw.strip(),
        host_raw.strip(),
    )


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
    endpoint: EndpointConfig,
    probe: RawSSHProbe,
    connect_timeout_seconds: int = 8,
) -> str:
    """Execute a sealed, read-only SSH probe without a local shell."""

    result = await RAW_SSH_OBSERVATION_ADAPTER.run_probe(
        endpoint,
        probe=probe,
        connect_timeout_seconds=connect_timeout_seconds,
    )
    if result.stdout_truncated or result.stderr_truncated:
        raise CollectionError(f"SSH probe output exceeded bounded limits for {endpoint.id}")
    if result.returncode != 0:
        detail = result.stderr.strip().replace("\n", " ")[:500]
        raise CollectionError(f"SSH probe failed for {endpoint.id}: {detail or result.returncode}")
    return result.stdout


class SSHCollector:
    def __init__(self, inventory: InventoryConfig, runner: Runner = default_runner) -> None:
        self.inventory = inventory
        self.runner = runner

    async def _run(
        self,
        endpoint: EndpointConfig,
        command: str,
        *,
        probe: RawSSHProbe,
    ) -> str:
        if self.runner is default_runner:
            output = await default_runner(
                endpoint,
                probe,
                self.inventory.collector.ssh_connect_timeout_seconds,
            )
        else:
            output = await self.runner(endpoint, command)
        try:
            output_size = len(output.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise CollectionError("SSH probe output is not valid Unicode") from exc
        if output_size > MAX_SERVER_SCRIPT_SNAPSHOT_BYTES:
            raise CollectionError("SSH probe output exceeded the stdout limit")
        return output

    async def observe_endpoint(self, endpoint: EndpointConfig) -> EndpointObservation:
        observed_at = utcnow()
        if endpoint.observation_profile == "server-script-v1":
            return parse_server_script_snapshot(
                await self._run(
                    endpoint,
                    SERVER_SCRIPT_REMOTE_COMMAND,
                    probe="endpoint-telemetry",
                ),
                endpoint_id=endpoint.id,
                observed_at=observed_at,
            )
        gpu_raw, process_raw, process_details_raw, identity_raw, host_raw = parse_combined_probe(
            await self._run(endpoint, COMBINED_QUERY, probe="endpoint-telemetry")
        )
        # Some CPU-only hosts expose an `nvidia-smi` wrapper that succeeds but
        # emits no rows. Treat that shape exactly like an unavailable NVIDIA
        # runtime so the already-valid host CPU/memory observation remains
        # useful, while any previously known GPU inventory stays fail-closed.
        gpu_probe_available = bool(gpu_raw.strip()) and GPU_UNAVAILABLE not in gpu_raw.splitlines()
        gpus = parse_gpu_csv(gpu_raw) if gpu_probe_available else []
        apps = parse_process_csv(process_raw) if gpu_probe_available else []
        _hostname, boot_id = parse_identity(identity_raw)
        (
            cpu_count,
            load_1m,
            cpu_total_ticks,
            cpu_idle_ticks,
            memory_total_mib,
            memory_available_mib,
        ) = parse_host_resource_snapshot(host_raw)
        details = parse_ps_output(process_details_raw, observed_at)
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
                "cpu_total_ticks": cpu_total_ticks,
                "cpu_idle_ticks": cpu_idle_ticks,
                "memory_total_mib": memory_total_mib,
                "memory_available_mib": memory_available_mib,
            },
            gpus=gpus,
            processes=processes,
            # A missing host-side NVIDIA runtime must not prevent CPU and memory
            # monitoring, but it also must not mark previously known GPUs absent.
            observation_complete=gpu_probe_available,
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
