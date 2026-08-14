"""Reference implementation of the fixed ``serverpilot-collect`` entry point.

It is intended to run *on* an observed server.  The broker invokes it with
the current fixed schema version and accepts only the single JSON object emitted here.
Deployments may maintain an equivalent local wrapper for a containerized GPU
runtime or a custom executable prefix, provided that wrapper preserves the
same fixed CLI and JSON contract.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from serverpilot.collector_protocol import SERVER_SCRIPT_SCHEMA_VERSION


GPU_QUERY = (
    "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
    "utilization.gpu,utilization.memory,temperature.gpu,power.draw,pstate,pci.bus_id"
)
PROCESS_QUERY = "--query-compute-apps=gpu_uuid,pid,used_memory,process_name"
NVIDIA_FORMAT = "--format=csv,noheader,nounits"
_PCI_BUS_ID_PATTERN = re.compile(
    r"^(?P<domain>[0-9A-Fa-f]{4,8}):(?P<bus>[0-9A-Fa-f]{2}):"
    r"(?P<device>[0-9A-Fa-f]{2})\.(?P<function>[0-7])$"
)


def _parse_value(value: str) -> str | None:
    cleaned = value.strip()
    return None if cleaned in {"", "N/A", "[Not Supported]", "Not Supported"} else cleaned


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.replace("MiB", "").replace("%", "").strip()))
    except ValueError:
        return None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace("W", "").strip())
    except ValueError:
        return None


def _pci_bus_key(value: str | None) -> tuple[int, int, int, int]:
    match = _PCI_BUS_ID_PATTERN.fullmatch(value or "")
    if match is None:
        raise RuntimeError("invalid nvidia-smi GPU PCI bus ID")
    return tuple(
        int(match.group(name), 16) for name in ("domain", "bus", "device", "function")
    )


def _run_nvidia_smi(*arguments: str) -> str | None:
    """Run the fixed local NVIDIA query, returning ``None`` if unavailable.

    This reference implementation deliberately discovers only ``nvidia-smi``
    on the server's PATH.  A site that needs Docker or a vendor-specific
    prefix changes its *local* fixed entry script, not central endpoint config.
    """

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *arguments, NVIDIA_FORMAT],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _read_required(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _host_snapshot() -> dict[str, int | float]:
    memory: dict[str, int] = {}
    for line in _read_required("/proc/meminfo").splitlines():
        key, separator, rest = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable"}:
            value = rest.split(maxsplit=1)[0] if rest.split() else ""
            parsed = _integer(value)
            if parsed is not None:
                memory[key] = parsed // 1024
    if {"MemTotal", "MemAvailable"} - memory.keys():
        raise RuntimeError("missing Linux memory telemetry")

    load_1m = float(_read_required("/proc/loadavg").split(maxsplit=1)[0])
    cpu_fields = _read_required("/proc/stat").splitlines()[0].split()
    if not cpu_fields or cpu_fields[0] != "cpu" or len(cpu_fields) < 6:
        raise RuntimeError("invalid Linux CPU telemetry")
    cpu_ticks = [_integer(value) for value in cpu_fields[1:]]
    if any(value is None for value in cpu_ticks):
        raise RuntimeError("invalid Linux CPU ticks")
    tick_values = [value for value in cpu_ticks if value is not None]
    return {
        "cpu_count": os.cpu_count() or 1,
        "load_1m": load_1m,
        "cpu_total_ticks": sum(tick_values),
        "cpu_idle_ticks": tick_values[3] + tick_values[4],
        "memory_total_mib": memory["MemTotal"],
        "memory_available_mib": memory["MemAvailable"],
    }


def _gpu_snapshot() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    raw_gpus = _run_nvidia_smi(GPU_QUERY)
    if raw_gpus is None:
        return ("cpu_only" if shutil.which("nvidia-smi") is None else "unknown"), [], []
    if not raw_gpus.strip():
        return "cpu_only", [], []

    observed_gpus: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    seen_indices: set[int] = set()
    seen_uuids: set[str] = set()
    seen_bus_ids: set[tuple[int, int, int, int]] = set()
    for row in csv.reader(raw_gpus.splitlines()):
        if not row or not any(value.strip() for value in row):
            continue
        values = [_parse_value(value) for value in row]
        if len(values) != 12:
            raise RuntimeError("unexpected nvidia-smi GPU column count")
        nvidia_index = _integer(values[0])
        bus_id = _pci_bus_key(values[11])
        total = _integer(values[3])
        used = _integer(values[4])
        free = _integer(values[5])
        if (
            nvidia_index is None
            or values[1] is None
            or values[2] is None
            or total is None
            or used is None
            or free is None
        ):
            raise RuntimeError("incomplete nvidia-smi GPU telemetry")
        if nvidia_index in seen_indices or values[1] in seen_uuids or bus_id in seen_bus_ids:
            raise RuntimeError("duplicate nvidia-smi GPU identity")
        seen_indices.add(nvidia_index)
        seen_uuids.add(values[1])
        seen_bus_ids.add(bus_id)
        observed_gpus.append(
            (
                bus_id,
                {
                    "gpu_uuid": values[1],
                    "gpu_index": nvidia_index,
                    "name": values[2],
                    "total_vram_mib": total,
                    "memory_used_mib": used,
                    "memory_free_mib": free,
                    "gpu_utilization_pct": _integer(values[6]),
                    "memory_utilization_pct": _integer(values[7]),
                    "temperature_c": _integer(values[8]),
                    "power_watts": _number(values[9]),
                    "pstate": values[10],
                    "health": "OK",
                },
            )
        )
    gpus = [
        {"cuda_ordinal": ordinal, **gpu}
        for ordinal, (_bus_id, gpu) in enumerate(sorted(observed_gpus))
    ]

    raw_processes = _run_nvidia_smi(PROCESS_QUERY)
    if raw_processes is None:
        raise RuntimeError("nvidia-smi process query failed")
    if raw_processes.strip().lower().startswith("no running processes"):
        return "gpu", gpus, []
    processes: list[dict[str, Any]] = []
    for row in csv.reader(raw_processes.splitlines()):
        if not row or not any(value.strip() for value in row):
            continue
        values = [_parse_value(value) for value in row]
        if len(values) != 4:
            raise RuntimeError("unexpected nvidia-smi process column count")
        pid = _integer(values[1])
        used = _integer(values[2])
        if values[0] is None or pid is None or used is None:
            raise RuntimeError("incomplete nvidia-smi process telemetry")
        processes.append(
            {
                "gpu_uuid": values[0],
                "pid": pid,
                "used_memory_mib": used,
                "executable": values[3] or "unknown",
            }
        )
    return "gpu", gpus, _with_process_details(processes)


def _with_process_details(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not processes:
        return []
    observed_at = datetime.now(timezone.utc)
    pids = sorted({process["pid"] for process in processes})
    details: dict[int, tuple[str | None, datetime, str]] = {}
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=,user=,etimes=,comm=", "-p", ",".join(str(pid) for pid in pids)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        for line in completed.stdout.splitlines():
            parts = line.strip().split(maxsplit=3)
            if len(parts) != 4:
                continue
            pid = _integer(parts[0])
            elapsed = _integer(parts[2])
            if pid is not None and elapsed is not None and elapsed >= 0:
                details[pid] = (parts[1] or None, observed_at - timedelta(seconds=elapsed), parts[3])
    for process in processes:
        username, started_at, executable = details.get(
            process["pid"], (None, observed_at, process["executable"])
        )
        process["username"] = username
        process["process_started_at"] = started_at.isoformat()
        process["executable"] = executable
    return processes


def collect_snapshot() -> dict[str, Any]:
    """Collect one current-schema snapshot without caller-controlled I/O."""

    gpu_probe_status, gpus, processes = _gpu_snapshot()
    return {
        "schema_version": SERVER_SCRIPT_SCHEMA_VERSION,
        "identity": {
            "hostname": socket.gethostname(),
            "boot_id": _read_required("/proc/sys/kernel/random/boot_id").strip(),
        },
        "host": _host_snapshot(),
        "gpu_probe_available": gpu_probe_status == "gpu",
        "gpu_probe_status": gpu_probe_status,
        "gpus": gpus,
        "processes": processes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="emit one ServerPilot collector JSON snapshot")
    parser.add_argument("--schema-version", required=True, type=int)
    arguments = parser.parse_args()
    if arguments.schema_version != SERVER_SCRIPT_SCHEMA_VERSION:
        parser.error(f"only schema version {SERVER_SCRIPT_SCHEMA_VERSION} is supported")
    try:
        json.dump(collect_snapshot(), sys.stdout, separators=(",", ":"), allow_nan=False)
        sys.stdout.write("\n")
    except Exception as exc:
        print(f"serverpilot-collect failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
