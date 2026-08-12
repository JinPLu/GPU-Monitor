"""Per-GPU keepalive helper.

The public helper accepts exactly one typed protocol-v2 request.  A request
names physical GPU UUIDs already selected by ServerPilot; it never accepts an
executable, PID, path, arbitrary environment, or CUDA selector.  Each target
receives a separate CUDA process, so stopping GPU A does not stop GPU B.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from serverpilot.keepalive_protocol import (
    KEEPALIVE_SCHEMA_VERSION,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    validate_gpu_uuid,
)


# These values are fixed server policy, never request inputs.  The worker has
# no steady-state filesystem or network activity: state is read/written only
# during a reconciliation call and runtime CUDA work is fully resident.
TARGET_MEMORY_FRACTION = 0.31
ACTIVE_DUTY_FRACTION = 0.30
DUTY_PERIOD_SECONDS = 0.1
ALLOCATION_CHUNK_BYTES = 256 * 1024 * 1024
COMPUTE_MATRIX_SIZE = 2048
WORKER_READY_TIMEOUT_SECONDS = 35
WORKER_STOP_TIMEOUT_SECONDS = 10
NVIDIA_SMI_TIMEOUT_SECONDS = 10


def default_state_directory() -> Path:
    """Return the helper state directory on the remote endpoint."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "serverpilot" / "keepalive"
    return Path.home() / ".local" / "state" / "serverpilot" / "keepalive"


class KeepaliveProcessProvider(Protocol):
    """Local implementation boundary; no Broker/API value reaches this layer."""

    def start(self, gpu_uuid: str) -> int: ...

    def is_running(self, pid: int) -> bool: ...

    def stop(self, pid: int) -> None: ...


class TorchSubprocessProvider:
    """Start exactly one fixed PyTorch/CUDA worker for one exact GPU UUID."""

    _worker_marker = "serverpilot.server_keepalive"

    def start(self, gpu_uuid: str) -> int:
        gpu_uuid = validate_gpu_uuid(gpu_uuid)
        read_fd, write_fd = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    self._worker_marker,
                    "--internal-worker",
                    "--ready-fd",
                    str(write_fd),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # The worker's CUDA selector is assigned only here, from the
                # helper's validated typed target.  It is not an API/CLI
                # parameter and no caller can add an environment variable.
                env={**os.environ, "CUDA_VISIBLE_DEVICES": gpu_uuid},
                close_fds=True,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
        finally:
            os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], WORKER_READY_TIMEOUT_SECONDS)
            message = os.read(read_fd, 512).decode("utf-8", errors="replace").strip() if ready else ""
        finally:
            os.close(read_fd)
        if message != "READY":
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            detail = message.removeprefix("ERROR:") or "worker readiness timed out"
            raise RuntimeError(f"CUDA keepalive worker could not start: {detail}")
        return process.pid

    def is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def stop(self, pid: int) -> None:
        if not self.is_running(pid):
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + WORKER_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self.is_running(pid):
                return
            time.sleep(0.1)
        if not self.is_running(pid):
            return
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.is_running(pid):
                return
            time.sleep(0.05)
        raise RuntimeError("CUDA keepalive worker did not stop")


class LocalKeepaliveController:
    """Idempotently reconcile independent, exact-GPU workers in one call."""

    def __init__(
        self,
        *,
        provider: KeepaliveProcessProvider | None = None,
        state_directory: Path | None = None,
        known_gpu_uuids_resolver: Callable[[], set[str]] | None = None,
    ) -> None:
        self.provider = provider or TorchSubprocessProvider()
        self.state_directory = state_directory or default_state_directory()
        self.known_gpu_uuids_resolver = known_gpu_uuids_resolver or _resolve_known_gpu_uuids

    def set_enabled(self, enabled: bool, gpu_uuids: list[str] | tuple[str, ...]) -> KeepaliveResponse:
        """Start or stop one occupancy worker for each supplied GPU UUID."""

        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        try:
            requested = tuple(validate_gpu_uuid(gpu_uuid) for gpu_uuid in gpu_uuids)
        except TypeError as exc:
            raise ValueError("gpu_uuids must be an iterable of GPU UUID strings") from exc
        if not requested:
            raise ValueError("gpu_uuids cannot be empty")
        known_gpu_uuids = self.known_gpu_uuids_resolver()
        if not set(requested) <= known_gpu_uuids:
            raise ValueError("gpu_uuids contains an unknown GPU UUID")
        self._ensure_state_directory()
        with self._lock():
            identities = self._read_identities()
            if enabled:
                return self._enable(requested, identities)
            return self._disable(requested, identities)

    def _enable(
        self,
        requested: tuple[str, ...],
        identities: dict[str, int],
    ) -> KeepaliveResponse:
        results: list[KeepaliveGPUResult] = []
        for gpu_uuid in requested:
            pid = identities.get(gpu_uuid)
            if pid is not None and self.provider.is_running(pid):
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="running",
                        outcome="unchanged",
                    )
                )
                continue
            if pid is not None:
                identities.pop(gpu_uuid)
                self._write_identities(identities)
            pid = self.provider.start(gpu_uuid)
            identities[gpu_uuid] = pid
            self._write_identities(identities)
            results.append(
                KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid,
                    status="running",
                    outcome="started",
                )
            )
        return KeepaliveResponse(enabled=True, results=tuple(results))

    def _disable(
        self,
        requested: tuple[str, ...],
        identities: dict[str, int],
    ) -> KeepaliveResponse:
        results: list[KeepaliveGPUResult] = []
        for gpu_uuid in requested:
            pid = identities.get(gpu_uuid)
            if pid is None:
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid, status="stopped", outcome="unchanged"
                    )
                )
                continue
            if self.provider.is_running(pid):
                self.provider.stop(pid)
            identities.pop(gpu_uuid)
            self._write_identities(identities)
            results.append(
                KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid, status="stopped", outcome="stopped"
                )
            )
        return KeepaliveResponse(enabled=False, results=tuple(results))

    @property
    def _state_path(self) -> Path:
        return self.state_directory / "workers.v2.json"

    @property
    def _lock_path(self) -> Path:
        return self.state_directory / "control.lock"

    def _ensure_state_directory(self) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_identities(self) -> dict[str, int]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("keepalive worker state is unreadable") from exc
        try:
            workers = value["workers"]
        except (TypeError, KeyError) as exc:
            raise RuntimeError("keepalive worker state has invalid workers") from exc
        if not isinstance(workers, list):
            raise RuntimeError("keepalive worker state has invalid workers")
        identities: dict[str, int] = {}
        for worker in workers:
            try:
                gpu_uuid = validate_gpu_uuid(worker["gpu_uuid"])
                pid = int(worker["pid"])
            except (TypeError, KeyError, ValueError, KeepaliveProtocolError) as exc:
                raise RuntimeError("keepalive worker state has invalid worker") from exc
            identities[gpu_uuid] = pid
        return identities

    def _write_identities(self, identities: dict[str, int]) -> None:
        if not identities:
            self._state_path.unlink(missing_ok=True)
            return
        workers = [
            {"gpu_uuid": gpu_uuid, "pid": pid}
            for gpu_uuid, pid in sorted(identities.items())
        ]
        payload = json.dumps(
            {"schema_version": KEEPALIVE_SCHEMA_VERSION, "workers": workers},
            separators=(",", ":"),
        )
        self._state_path.write_text(payload, encoding="utf-8")


def handle_request(
    payload: bytes,
    *,
    controller: LocalKeepaliveController | None = None,
) -> KeepaliveResponse:
    request = KeepaliveRequest.decode(payload)
    return (controller or LocalKeepaliveController()).set_enabled(request.enabled, request.gpu_uuids)


def _run_nvidia_smi_query(query_argument: str) -> str:
    try:
        result = subprocess.run(
            ["nvidia-smi", query_argument, "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("nvidia-smi host PID verification failed") from exc
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi host PID verification failed")
    return result.stdout


def _resolve_known_gpu_uuids() -> set[str]:
    """Return the helper host's complete physical GPU UUID set for one action."""

    gpu_uuids = {
        validate_gpu_uuid(line.strip())
        for line in _run_nvidia_smi_query("--query-gpu=uuid").splitlines()
        if line.strip()
    }
    if not gpu_uuids:
        raise RuntimeError("keepalive helper could not verify any physical GPU UUIDs")
    return gpu_uuids


def _run_cuda_worker(ready_fd: int) -> None:
    """Run one fixed worker against the single GPU made visible by its provider."""

    ready = os.fdopen(ready_fd, "wb", buffering=0)
    try:
        cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible_devices is None:
            raise RuntimeError("CUDA_VISIBLE_DEVICES must be set only by the keepalive provider")
        validate_gpu_uuid(cuda_visible_devices)
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch with CUDA support is required") from exc
        torch.set_num_threads(1)
        with contextlib.suppress(RuntimeError):
            torch.set_num_interop_threads(1)
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("exactly one CUDA GPU must be visible")

        device = torch.device("cuda:0")
        properties = torch.cuda.get_device_properties(device)
        target_bytes = math.ceil(properties.total_memory * TARGET_MEMORY_FRACTION)
        free_bytes, _ = torch.cuda.mem_get_info(device)
        # Reserve fixed VRAM separately from the resident compute buffers so
        # the duty loop itself does not allocate or write/cache on each tick.
        if free_bytes < target_bytes + 128 * 1024 * 1024:
            raise RuntimeError("target GPU lacks memory for fixed keepalive target")
        held_allocations: list[Any] = []
        remaining = target_bytes
        while remaining > 0:
            size = min(remaining, ALLOCATION_CHUNK_BYTES)
            held_allocations.append(torch.empty(size, dtype=torch.uint8, device=device))
            remaining -= size
        resident_compute_buffers = (
            torch.randn((COMPUTE_MATRIX_SIZE, COMPUTE_MATRIX_SIZE), dtype=torch.float16, device=device),
            torch.randn((COMPUTE_MATRIX_SIZE, COMPUTE_MATRIX_SIZE), dtype=torch.float16, device=device),
            torch.empty((COMPUTE_MATRIX_SIZE, COMPUTE_MATRIX_SIZE), dtype=torch.float16, device=device),
        )
        torch.cuda.synchronize(device)
        ready.write(b"READY\n")
    except Exception as exc:
        ready.write(f"ERROR:{type(exc).__name__}: {exc}\n".encode("utf-8", errors="replace")[:500])
        ready.close()
        raise SystemExit(1) from exc
    finally:
        if not ready.closed:
            ready.close()

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    left, right, output = resident_compute_buffers
    while not stop_event.is_set():
        period_started = time.monotonic()
        active_until = period_started + DUTY_PERIOD_SECONDS * ACTIVE_DUTY_FRACTION
        while time.monotonic() < active_until and not stop_event.is_set():
            torch.mm(left, right, out=output)
            torch.cuda.synchronize(device)
        # A 100ms duty cycle avoids busy spin during the remaining 70ms and
        # caps host scheduling pressure without disk/network polling.
        stop_event.wait(max(0.0, period_started + DUTY_PERIOD_SECONDS - time.monotonic()))
    del held_allocations, resident_compute_buffers


def main() -> None:
    parser = argparse.ArgumentParser(description="reconcile sealed ServerPilot per-GPU keepalive workers")
    parser.add_argument("--schema-version", type=int)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.internal_worker:
        if arguments.schema_version is not None or arguments.ready_fd is None:
            parser.error("invalid internal worker invocation")
        _run_cuda_worker(arguments.ready_fd)
        return
    if arguments.schema_version != KEEPALIVE_SCHEMA_VERSION or arguments.ready_fd is not None:
        parser.error(f"only schema version {KEEPALIVE_SCHEMA_VERSION} is supported")
    try:
        payload = sys.stdin.buffer.read()
        response = handle_request(payload)
        sys.stdout.buffer.write(response.encode())
    except Exception as exc:
        print(f"serverpilot-keepalive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
