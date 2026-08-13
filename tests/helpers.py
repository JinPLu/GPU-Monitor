from __future__ import annotations

from datetime import UTC, datetime

from serverpilot.schemas import EndpointObservation, ProcessInput, TelemetryInput


def observation(
    endpoint_id: str = "endpoint-a",
    *,
    count: int = 4,
    processes: list[ProcessInput] | None = None,
    prefix: str = "GPU",
    gpu_uuids: list[str] | None = None,
    observation_complete: bool = True,
    observed_at: datetime | None = None,
    host: dict[str, object] | None = None,
) -> EndpointObservation:
    now = observed_at or datetime.now(UTC)
    uuids = (
        gpu_uuids
        if gpu_uuids is not None
        else [f"{prefix}-{endpoint_id}-{index}" for index in range(count)]
    )
    host_payload = {
        "cpu_count": 64,
        "load_1m": 4.0,
        "memory_total_mib": 262_144,
        "memory_available_mib": 196_608,
    }
    if host is not None:
        host_payload.update(host)
    return EndpointObservation(
        endpoint_id=endpoint_id,
        observed_at=now,
        boot_id=f"boot-{endpoint_id}",
        host=host_payload,
        gpus=[
            TelemetryInput(
                gpu_uuid=gpu_uuid,
                gpu_index=index,
                cuda_ordinal=index,
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
            for index, gpu_uuid in enumerate(uuids)
        ],
        processes=processes or [],
        observation_complete=observation_complete,
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
