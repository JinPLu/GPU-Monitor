"""Reference implementation of the sealed per-GPU keepalive helper.

The public helper accepts exactly one typed protocol-v2 request.  A request
names physical GPU UUIDs already selected by ServerPilot; it never accepts an
executable, PID, path, arbitrary environment, or CUDA selector.  Each target
receives a separate CUDA process and private state entry, so stopping GPU A
cannot signal GPU B (or any arbitrary host process).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from serverpilot.keepalive_protocol import (
    KEEPALIVE_SCHEMA_VERSION,
    MAX_KEEPALIVE_MESSAGE_BYTES,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveWorkerAttestation,
    LegacyKeepaliveRequest,
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
MAX_NVIDIA_SMI_OUTPUT_BYTES = 64 * 1024


def default_state_directory() -> Path:
    """Return the code-owned private state directory on the remote endpoint."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute() and ".." not in candidate.parts:
            return candidate / "serverpilot" / "keepalive"
    return Path.home() / ".local" / "state" / "serverpilot" / "keepalive"


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Namespace-local process identity that is safe to signal only after verify."""

    pid: int
    start_ticks: int


class KeepaliveProcessProvider(Protocol):
    """Local implementation boundary; no Broker/API value reaches this layer."""

    def start(self, gpu_uuid: str) -> WorkerIdentity: ...

    def is_running(self, identity: WorkerIdentity) -> bool: ...

    def stop(self, identity: WorkerIdentity) -> None: ...


class TorchSubprocessProvider:
    """Start exactly one fixed PyTorch/CUDA worker for one exact GPU UUID."""

    _worker_marker = "serverpilot.server_keepalive"

    def start(self, gpu_uuid: str) -> WorkerIdentity:
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
        # A stale/reused PID, a same-user non-helper process, or an identity
        # that was never recorded is never a valid signal target.
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
    """Idempotently reconcile independent, exact-GPU workers in one call."""

    def __init__(
        self,
        *,
        provider: KeepaliveProcessProvider | None = None,
        state_directory: Path | None = None,
        observed_pid_resolver: Callable[[str], int] | None = None,
        known_gpu_uuids_resolver: Callable[[], set[str]] | None = None,
    ) -> None:
        self.provider = provider or TorchSubprocessProvider()
        self.state_directory = state_directory or default_state_directory()
        self.observed_pid_resolver = observed_pid_resolver or _resolve_observed_host_pid
        self.known_gpu_uuids_resolver = known_gpu_uuids_resolver or _resolve_known_gpu_uuids

    def set_enabled(self, enabled: bool, gpu_uuids: list[str] | tuple[str, ...]) -> KeepaliveResponse:
        """Reconcile only the supplied GPU UUIDs and return exact attestations.

        New workers start serially.  If a later startup fails, workers started
        by this invocation are safely rolled back while pre-existing targets
        retain their original state; the operation never expands to other
        endpoint GPUs.
        """

        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        try:
            requested = tuple(validate_gpu_uuid(gpu_uuid) for gpu_uuid in gpu_uuids)
        except TypeError as exc:
            raise ValueError("gpu_uuids must be an iterable of GPU UUID strings") from exc
        if not requested:
            raise ValueError("gpu_uuids cannot be empty")
        if len(set(requested)) != len(requested):
            raise ValueError("gpu_uuids contains duplicates")
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
        identities: dict[str, WorkerIdentity],
    ) -> KeepaliveResponse:
        results: list[KeepaliveGPUResult] = []
        started: list[tuple[str, WorkerIdentity]] = []
        try:
            for gpu_uuid in requested:
                identity = identities.get(gpu_uuid)
                if identity is not None and self.provider.is_running(identity):
                    results.append(
                        KeepaliveGPUResult(
                            gpu_uuid=gpu_uuid,
                            status="running",
                            outcome="unchanged",
                            worker=self._attest_running_worker(gpu_uuid, identity),
                        )
                    )
                    continue
                if identity is not None:
                    # The state belongs to this helper but no longer verifies
                    # as a current worker.  Never signal it; replace only its
                    # own stale mapping.
                    identities.pop(gpu_uuid)
                    self._write_identities(identities)
                identity = self.provider.start(gpu_uuid)
                if not self.provider.is_running(identity):
                    raise RuntimeError("keepalive provider did not retain its worker")
                # Persist before nvidia-smi attestation: a verification error
                # still leaves an exact, helper-owned stop identity.
                identities[gpu_uuid] = identity
                self._write_identities(identities)
                try:
                    worker = self._attest_running_worker(gpu_uuid, identity)
                except Exception:
                    self._stop_and_forget(gpu_uuid, identity, identities)
                    raise
                started.append((gpu_uuid, identity))
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="running",
                        outcome="started",
                        worker=worker,
                    )
                )
        except Exception:
            # Start is deliberately serial and its rollback is limited to the
            # identities created above.  Existing independently managed GPUs
            # are neither stopped nor modified.
            for gpu_uuid, identity in reversed(started):
                self._stop_and_forget(gpu_uuid, identity, identities)
            raise
        return KeepaliveResponse(enabled=True, results=tuple(results))

    def _disable(
        self,
        requested: tuple[str, ...],
        identities: dict[str, WorkerIdentity],
    ) -> KeepaliveResponse:
        results: list[KeepaliveGPUResult] = []
        for gpu_uuid in requested:
            identity = identities.get(gpu_uuid)
            if identity is None:
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid, status="stopped", outcome="unchanged", worker=None
                    )
                )
                continue
            # Stops are scoped to an identity recovered from this helper's
            # private mapping and re-verified immediately by the provider.
            if self.provider.is_running(identity):
                self.provider.stop(identity)
                if self.provider.is_running(identity):
                    raise RuntimeError("keepalive provider left its worker running")
            identities.pop(gpu_uuid)
            self._write_identities(identities)
            results.append(
                KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid, status="stopped", outcome="stopped", worker=None
                )
            )
        return KeepaliveResponse(enabled=False, results=tuple(results))

    def _stop_and_forget(
        self,
        gpu_uuid: str,
        identity: WorkerIdentity,
        identities: dict[str, WorkerIdentity],
    ) -> None:
        if self.provider.is_running(identity):
            self.provider.stop(identity)
        if not self.provider.is_running(identity):
            identities.pop(gpu_uuid, None)
            self._write_identities(identities)

    def _attest_running_worker(
        self, gpu_uuid: str, identity: WorkerIdentity
    ) -> KeepaliveWorkerAttestation:
        observed_pid = self.observed_pid_resolver(gpu_uuid)
        if not self.provider.is_running(identity):
            raise RuntimeError("keepalive worker exited during host PID verification")
        return KeepaliveWorkerAttestation(pid=observed_pid, start_ticks=identity.start_ticks)

    @property
    def _state_path(self) -> Path:
        return self.state_directory / "workers.v2.json"

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

    def _read_identities(self) -> dict[str, WorkerIdentity]:
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
                    raise RuntimeError("keepalive worker state must be private and owned by this user")
                raw = handle.read(MAX_KEEPALIVE_MESSAGE_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_KEEPALIVE_MESSAGE_BYTES:
                raise RuntimeError("keepalive worker state is too large")
            value = json.loads(raw)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("keepalive worker state is unreadable") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "workers"}:
            raise RuntimeError("keepalive worker state has invalid fields")
        if value["schema_version"] != KEEPALIVE_SCHEMA_VERSION or not isinstance(value["workers"], list):
            raise RuntimeError("keepalive worker state has invalid schema")
        workers = value["workers"]
        if len(workers) > 64:
            raise RuntimeError("keepalive worker state has too many workers")
        identities: dict[str, WorkerIdentity] = {}
        for worker in workers:
            if not isinstance(worker, dict) or set(worker) != {"gpu_uuid", "pid", "start_ticks"}:
                raise RuntimeError("keepalive worker state has invalid worker")
            try:
                gpu_uuid = validate_gpu_uuid(worker["gpu_uuid"])
            except KeepaliveProtocolError as exc:
                raise RuntimeError("keepalive worker state has invalid GPU UUID") from exc
            pid = worker["pid"]
            start_ticks = worker["start_ticks"]
            if (
                gpu_uuid in identities
                or type(pid) is not int
                or pid <= 0
                or type(start_ticks) is not int
                or start_ticks <= 0
            ):
                raise RuntimeError("keepalive worker state has invalid identity")
            identities[gpu_uuid] = WorkerIdentity(pid=pid, start_ticks=start_ticks)
        return identities

    def _write_identities(self, identities: dict[str, WorkerIdentity]) -> None:
        if not identities:
            self._state_path.unlink(missing_ok=True)
            return
        workers = [
            {"gpu_uuid": gpu_uuid, "pid": identity.pid, "start_ticks": identity.start_ticks}
            for gpu_uuid, identity in sorted(identities.items())
        ]
        payload = json.dumps(
            {"schema_version": KEEPALIVE_SCHEMA_VERSION, "workers": workers},
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > MAX_KEEPALIVE_MESSAGE_BYTES:
            raise RuntimeError("keepalive worker state is too large")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        temporary = self.state_directory / f"workers.{os.getpid()}.{digest}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)


def handle_request(
    payload: bytes,
    *,
    controller: LocalKeepaliveController | None = None,
) -> KeepaliveResponse:
    request = KeepaliveRequest.decode(payload)
    if isinstance(request, LegacyKeepaliveRequest):
        raise KeepaliveProtocolError(
            "keepalive schema v1 cannot control per-GPU workers; deploy schema v2 helper"
        )
    return (controller or LocalKeepaliveController()).set_enabled(request.enabled, request.gpu_uuids)


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
    if (
        len(result.stdout.encode("utf-8", errors="replace")) > MAX_NVIDIA_SMI_OUTPUT_BYTES
        or len(result.stderr.encode("utf-8", errors="replace")) > MAX_NVIDIA_SMI_OUTPUT_BYTES
    ):
        raise RuntimeError("nvidia-smi host PID verification output is too large")
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi host PID verification failed")
    return result.stdout


def _resolve_observed_host_pid(gpu_uuid: str) -> int:
    """Return one target GPU's sole host-visible CUDA PID.

    Worker processes are intentionally one-GPU processes.  Other GPU rows are
    irrelevant; any missing, duplicate, or competing process on *this* target
    makes attribution ambiguous and is rejected.
    """

    gpu_uuid = validate_gpu_uuid(gpu_uuid)
    process_lines = [
        line.strip()
        for line in _run_nvidia_smi_query("--query-compute-apps=gpu_uuid,pid").splitlines()
        if line.strip()
    ]
    target_pids: list[int] = []
    for line in process_lines:
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2 or not fields[0]:
            raise RuntimeError("GPU process identity verification failed")
        try:
            pid = int(fields[1])
        except ValueError as exc:
            raise RuntimeError("GPU process identity verification failed") from exc
        if pid <= 0:
            raise RuntimeError("GPU process identity verification failed")
        if fields[0] == gpu_uuid:
            target_pids.append(pid)
    if len(target_pids) != 1:
        raise RuntimeError("keepalive worker is not the unique compute PID for target GPU")
    return target_pids[0]


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
        payload = sys.stdin.buffer.read(MAX_KEEPALIVE_MESSAGE_BYTES + 1)
        response = handle_request(payload)
        sys.stdout.buffer.write(response.encode())
    except Exception as exc:
        print(f"serverpilot-keepalive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
