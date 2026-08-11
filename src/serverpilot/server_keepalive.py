"""Reference implementation of the fixed ``serverpilot-keepalive`` helper.

The public helper reads one strict JSON request from stdin.  It never accepts
a path, command, PID, environment, or GPU selector from its caller.  Its local
state directory and CUDA policy are code-owned, and the stop path verifies the
recorded process identity before sending any signal.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from serverpilot.keepalive_protocol import (
    KEEPALIVE_SCHEMA_VERSION,
    MAX_KEEPALIVE_MESSAGE_BYTES,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
)


# Protocol-v1 policy is deliberately fixed on the server.  It is not part of
# the REST/MCP request and cannot be changed by an Agent.
TARGET_MEMORY_FRACTION = 0.31
ACTIVE_DUTY_FRACTION = 0.35
DUTY_PERIOD_SECONDS = 1.0
ALLOCATION_CHUNK_BYTES = 256 * 1024 * 1024
WORKER_READY_TIMEOUT_SECONDS = 35
WORKER_STOP_TIMEOUT_SECONDS = 10
NVIDIA_SMI_TIMEOUT_SECONDS = 10
MAX_NVIDIA_SMI_OUTPUT_BYTES = 64 * 1024


def default_state_directory() -> Path:
    """Return the code-owned per-user state path used by the remote helper."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute() and ".." not in candidate.parts:
            return candidate / "serverpilot" / "keepalive"
    return Path.home() / ".local" / "state" / "serverpilot" / "keepalive"


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    pid: int
    start_ticks: int


class KeepaliveProcessProvider(Protocol):
    """Local implementation boundary; never exposed through Broker APIs."""

    def start(self) -> WorkerIdentity: ...

    def is_running(self, identity: WorkerIdentity) -> bool: ...

    def stop(self, identity: WorkerIdentity) -> None: ...


class TorchSubprocessProvider:
    """Run the fixed whole-endpoint PyTorch/CUDA worker when available."""

    _worker_marker = "serverpilot.server_keepalive"

    def start(self) -> WorkerIdentity:
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
        start_ticks = _process_start_ticks(process.pid)
        if start_ticks is None:
            process.terminate()
            raise RuntimeError("CUDA keepalive worker identity could not be verified")
        return WorkerIdentity(pid=process.pid, start_ticks=start_ticks)

    def is_running(self, identity: WorkerIdentity) -> bool:
        proc = Path("/proc") / str(identity.pid)
        if not proc.exists():
            return False
        try:
            if proc.stat().st_uid != os.getuid():
                return False
            if _process_start_ticks(identity.pid) != identity.start_ticks:
                return False
            command = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return False
        return self._worker_marker.encode() in command and b"--internal-worker" in command

    def stop(self, identity: WorkerIdentity) -> None:
        # Recheck immediately before signalling.  A stale/reused PID or a
        # foreign same-user process is never a valid stop target.
        if not self.is_running(identity):
            return
        os.kill(identity.pid, signal.SIGTERM)
        deadline = time.monotonic() + WORKER_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self.is_running(identity):
                return
            time.sleep(0.1)
        if not self.is_running(identity):
            return
        os.kill(identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.is_running(identity):
                return
            time.sleep(0.05)
        raise RuntimeError("CUDA keepalive worker did not stop")


class LocalKeepaliveController:
    """Idempotently reconcile one code-owned worker with a boolean intent."""

    def __init__(
        self,
        *,
        provider: KeepaliveProcessProvider | None = None,
        state_directory: Path | None = None,
        observed_pid_resolver: Callable[[], int] | None = None,
    ) -> None:
        self.provider = provider or TorchSubprocessProvider()
        self.state_directory = state_directory or default_state_directory()
        self.observed_pid_resolver = observed_pid_resolver or _resolve_observed_host_pid

    def set_enabled(self, enabled: bool) -> KeepaliveResponse:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._ensure_state_directory()
        with self._lock():
            identity = self._read_identity()
            running = identity is not None and self.provider.is_running(identity)
            if enabled:
                if running:
                    return KeepaliveResponse(
                        enabled=True,
                        changed=False,
                        status="running",
                        worker=self._attest_running_worker(identity),
                    )
                if identity is not None:
                    self._remove_state()
                started = self.provider.start()
                if not self.provider.is_running(started):
                    raise RuntimeError("keepalive provider did not retain its worker")
                try:
                    # Persist the namespace-local identity before host-PID
                    # verification so a failed verification still has a safe,
                    # exact stop target.
                    self._write_identity(started)
                    worker = self._attest_running_worker(started)
                    return KeepaliveResponse(
                        enabled=True,
                        changed=True,
                        status="running",
                        worker=worker,
                    )
                except Exception:
                    if self.provider.is_running(started):
                        self.provider.stop(started)
                    if not self.provider.is_running(started):
                        self._remove_state()
                    raise

            if identity is None:
                return KeepaliveResponse(
                    enabled=False, changed=False, status="stopped", worker=None
                )
            if running:
                self.provider.stop(identity)
                if self.provider.is_running(identity):
                    raise RuntimeError("keepalive provider left its worker running")
            self._remove_state()
            return KeepaliveResponse(enabled=False, changed=True, status="stopped", worker=None)

    def _attest_running_worker(
        self, identity: WorkerIdentity
    ) -> KeepaliveWorkerAttestation:
        observed_pid = self.observed_pid_resolver()
        if not self.provider.is_running(identity):
            raise RuntimeError("keepalive worker exited during host PID verification")
        return KeepaliveWorkerAttestation(
            pid=observed_pid,
            # This remains the namespace-local process start identity.  It is
            # retained only as helper-produced lifecycle evidence; safe stop
            # uses the complete namespace-local identity stored on disk.
            start_ticks=identity.start_ticks,
        )

    @property
    def _state_path(self) -> Path:
        return self.state_directory / "worker.json"

    @property
    def _lock_path(self) -> Path:
        return self.state_directory / "control.lock"

    def _ensure_state_directory(self) -> None:
        self.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = self.state_directory.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise RuntimeError("keepalive state directory must be private and owned by this user")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(
            self._lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
            ):
                raise RuntimeError("keepalive control lock must be private and owned by this user")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_identity(self) -> WorkerIdentity | None:
        try:
            descriptor = os.open(
                self._state_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                details = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.getuid()
                    or details.st_mode & 0o077
                ):
                    raise RuntimeError(
                        "keepalive worker state must be private and owned by this user"
                    )
                raw = handle.read(MAX_KEEPALIVE_MESSAGE_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_KEEPALIVE_MESSAGE_BYTES:
                raise RuntimeError("keepalive worker state is too large")
            value = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("keepalive worker state is unreadable") from exc
        if not isinstance(value, dict) or set(value) != {"pid", "start_ticks"}:
            raise RuntimeError("keepalive worker state has invalid fields")
        pid = value["pid"]
        start_ticks = value["start_ticks"]
        if type(pid) is not int or pid <= 0 or type(start_ticks) is not int or start_ticks <= 0:
            raise RuntimeError("keepalive worker state has invalid identity")
        return WorkerIdentity(pid=pid, start_ticks=start_ticks)

    def _write_identity(self, identity: WorkerIdentity) -> None:
        temporary = self.state_directory / f"worker.{os.getpid()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(identity), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_state(self) -> None:
        self._state_path.unlink(missing_ok=True)


def handle_request(
    payload: bytes,
    *,
    controller: LocalKeepaliveController | None = None,
) -> KeepaliveResponse:
    request = KeepaliveRequest.decode(payload)
    return (controller or LocalKeepaliveController()).set_enabled(request.enabled)


def _process_start_ticks(pid: int) -> int | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        tail = raw[raw.rindex(")") + 2 :].split()
        return int(tail[19])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def _run_nvidia_smi_query(query_argument: str) -> str:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                query_argument,
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("nvidia-smi host PID verification failed") from exc
    if (
        len(result.stdout.encode("utf-8", errors="replace")) > MAX_NVIDIA_SMI_OUTPUT_BYTES
        or len(result.stderr.encode("utf-8", errors="replace")) > MAX_NVIDIA_SMI_OUTPUT_BYTES
    ):
        raise RuntimeError("nvidia-smi host PID verification output is too large")
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi host PID verification failed")
    return result.stdout


def _resolve_observed_host_pid() -> int:
    """Return the one NVIDIA-driver PID covering every visible GPU.

    The helper runs the same fixed nvidia-smi view as the collector.  READY is
    emitted only after the worker has allocated and synchronized on every
    visible GPU, so exactly one compute PID with full coverage is the worker's
    collector-domain (host) PID.  Anything less exact is refused.
    """

    gpu_lines = [
        line.strip()
        for line in _run_nvidia_smi_query("--query-gpu=uuid").splitlines()
        if line.strip()
    ]
    visible_gpu_uuids = set(gpu_lines)
    if not visible_gpu_uuids or len(visible_gpu_uuids) != len(gpu_lines):
        raise RuntimeError("visible GPU identity verification failed")

    observed_gpu_uuids: set[str] = set()
    observed_pids: set[int] = set()
    process_lines = [
        line.strip()
        for line in _run_nvidia_smi_query("--query-compute-apps=gpu_uuid,pid").splitlines()
        if line.strip()
    ]
    for line in process_lines:
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2 or not fields[0] or fields[0] not in visible_gpu_uuids:
            raise RuntimeError("GPU process identity verification failed")
        try:
            pid = int(fields[1])
        except ValueError as exc:
            raise RuntimeError("GPU process identity verification failed") from exc
        if pid <= 0:
            raise RuntimeError("GPU process identity verification failed")
        observed_gpu_uuids.add(fields[0])
        observed_pids.add(pid)

    if observed_gpu_uuids != visible_gpu_uuids or len(observed_pids) != 1:
        raise RuntimeError(
            "keepalive worker is not the unique compute PID covering every visible GPU"
        )
    return next(iter(observed_pids))


def _run_cuda_worker(ready_fd: int) -> None:
    ready = os.fdopen(ready_fd, "wb", buffering=0)
    try:
        if "CUDA_VISIBLE_DEVICES" in os.environ:
            raise RuntimeError("CUDA_VISIBLE_DEVICES must be unset for whole-endpoint keepalive")
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch with CUDA support is required") from exc
        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            raise RuntimeError("no CUDA GPU is visible")

        held_allocations: list[list[Any]] = []
        compute_inputs: list[tuple[Any, Any]] = []
        for index in range(torch.cuda.device_count()):
            device = torch.device(f"cuda:{index}")
            properties = torch.cuda.get_device_properties(device)
            target_bytes = math.ceil(properties.total_memory * TARGET_MEMORY_FRACTION)
            free_bytes, _ = torch.cuda.mem_get_info(device)
            if free_bytes < target_bytes + 128 * 1024 * 1024:
                raise RuntimeError(f"GPU {index} lacks memory for fixed keepalive target")
            allocations: list[Any] = []
            remaining = target_bytes
            while remaining > 0:
                size = min(remaining, ALLOCATION_CHUNK_BYTES)
                allocations.append(torch.empty(size, dtype=torch.uint8, device=device))
                remaining -= size
            held_allocations.append(allocations)
            compute_inputs.append(
                (
                    torch.randn((2048, 2048), dtype=torch.float16, device=device),
                    torch.randn((2048, 2048), dtype=torch.float16, device=device),
                )
            )
        for index in range(torch.cuda.device_count()):
            torch.cuda.synchronize(index)
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

    worker_errors: list[BaseException] = []

    def exercise_gpu(index: int, left: Any, right: Any) -> None:
        import torch

        try:
            torch.cuda.set_device(index)
            while not stop_event.is_set():
                period_started = time.monotonic()
                active_until = period_started + DUTY_PERIOD_SECONDS * ACTIVE_DUTY_FRACTION
                while time.monotonic() < active_until and not stop_event.is_set():
                    torch.mm(left, right)
                    torch.cuda.synchronize(index)
                stop_event.wait(max(0.0, period_started + DUTY_PERIOD_SECONDS - time.monotonic()))
        except BaseException as exc:
            worker_errors.append(exc)
            stop_event.set()

    workers = [
        threading.Thread(target=exercise_gpu, args=(index, *inputs), daemon=True)
        for index, inputs in enumerate(compute_inputs)
    ]
    for worker in workers:
        worker.start()
    stop_event.wait()
    for worker in workers:
        worker.join(timeout=2)
    # Keep the allocation lists live through orderly worker shutdown.
    del held_allocations
    if worker_errors:
        raise SystemExit(1) from worker_errors[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="reconcile the sealed ServerPilot keepalive worker")
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
        payload = sys.stdin.buffer.read(MAX_KEEPALIVE_MESSAGE_BYTES + 1)
        response = handle_request(payload)
        sys.stdout.buffer.write(response.encode())
    except Exception as exc:
        print(f"serverpilot-keepalive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
