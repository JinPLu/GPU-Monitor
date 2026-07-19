from __future__ import annotations

from datetime import UTC, datetime

from gpu_broker.schemas import EndpointObservation, ProcessInput, TelemetryInput


def observation(
    endpoint_id: str = "endpoint-a",
    *,
    count: int = 4,
    processes: list[ProcessInput] | None = None,
    prefix: str = "GPU",
) -> EndpointObservation:
    now = datetime.now(UTC)
    return EndpointObservation(
        endpoint_id=endpoint_id,
        observed_at=now,
        boot_id=f"boot-{endpoint_id}",
        host={
            "cpu_count": 64,
            "load_1m": 4.0,
            "memory_total_mib": 262_144,
            "memory_available_mib": 196_608,
        },
        gpus=[
            TelemetryInput(
                gpu_uuid=f"{prefix}-{endpoint_id}-{index}",
                gpu_index=index,
                name="Test GPU",
                total_vram_mib=100_000,
                memory_used_mib=0,
                memory_free_mib=100_000,
                gpu_utilization_pct=0,
                memory_utilization_pct=0,
                temperature_c=35,
                power_watts=100.0,
                pstate="P0",
                health="OK",
            )
            for index in range(count)
        ],
        processes=processes or [],
    )


def process_for_gpu(uuid: str, *, pid: int = 1234) -> ProcessInput:
    return ProcessInput(
        gpu_uuid=uuid,
        pid=pid,
        used_memory_mib=1024,
        executable="/usr/bin/python",
        username="tester",
        process_started_at=datetime.now(UTC),
    )
