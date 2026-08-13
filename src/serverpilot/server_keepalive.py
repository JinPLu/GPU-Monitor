"""Per-GPU keepalive helper.

The public helper accepts exactly one typed protocol-v3 request.  A request
names physical GPU UUIDs already selected by ServerPilot; it never accepts an
executable, PID, path, arbitrary environment, or CUDA selector.  Each target
receives a separate CUDA process, so stopping GPU A does not stop GPU B.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from serverpilot.keepalive_protocol import (
    KEEPALIVE_SCHEMA_VERSION,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    keepalive_protocol_info,
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
WORKER_PROCESS_MARKER = "serverpilot-keepalive-worker-v3"
LINUX_PIDFD_SEND_SIGNAL_SYSCALL = 424
LINUX_PIDFD_OPEN_SYSCALL = 434
LINUX_PIDFD_SYSCALL_MACHINES = frozenset({"aarch64", "arm64", "amd64", "x86_64"})


@dataclass(frozen=True)
class KeepaliveProcessIdentity:
    """Exact identity for one worker within a single Linux boot."""

    pid: int
    boot_id: str
    start_time_ticks: int
    worker_marker: str


def default_state_directory() -> Path:
    """Return the helper state directory on the remote endpoint."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "serverpilot" / "keepalive"
    return Path.home() / ".local" / "state" / "serverpilot" / "keepalive"


class KeepaliveProcessProvider(Protocol):
    """Local implementation boundary; no Broker/API value reaches this layer."""

    def start(self, gpu_uuid: str) -> KeepaliveProcessIdentity: ...

    def is_running(self, identity: KeepaliveProcessIdentity) -> bool: ...

    def stop(self, identity: KeepaliveProcessIdentity) -> None: ...


def _pidfd_open(pid: int) -> int:
    """Open a Linux pidfd even when the endpoint Python omits its wrapper."""

    native = getattr(os, "pidfd_open", None)
    if native is not None:
        return native(pid)
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("pidfd_open is available only on Linux")
    if os.uname().machine.lower() not in LINUX_PIDFD_SYSCALL_MACHINES:
        raise NotImplementedError("pidfd_open syscall number is not defined for this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = syscall(
        ctypes.c_long(LINUX_PIDFD_OPEN_SYSCALL),
        ctypes.c_int(pid),
        ctypes.c_uint(0),
    )
    if result >= 0:
        return int(result)
    error_number = ctypes.get_errno()
    if error_number == errno.ESRCH:
        raise ProcessLookupError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _pidfd_send_signal(pidfd: int, signal_number: int) -> None:
    """Signal the exact process referenced by a pidfd, never a numeric PID."""

    native = getattr(signal, "pidfd_send_signal", None)
    if native is not None:
        native(pidfd, signal_number)
        return
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("pidfd_send_signal is available only on Linux")
    if os.uname().machine.lower() not in LINUX_PIDFD_SYSCALL_MACHINES:
        raise NotImplementedError(
            "pidfd_send_signal syscall number is not defined for this architecture"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = syscall(
        ctypes.c_long(LINUX_PIDFD_SEND_SIGNAL_SYSCALL),
        ctypes.c_int(pidfd),
        ctypes.c_int(signal_number),
        ctypes.c_void_p(),
        ctypes.c_uint(0),
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.ESRCH:
        raise ProcessLookupError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


class TorchSubprocessProvider:
    """Start exactly one fixed PyTorch/CUDA worker for one exact GPU UUID."""

    _worker_marker = "serverpilot.server_keepalive"

    def __init__(self) -> None:
        self._gpu_ordinals: dict[str, str] | None = None

    def _cuda_visible_device(self, gpu_uuid: str) -> str:
        """Resolve one UUID to its PCI_BUS_ID-ordered CUDA ordinal."""

        if self._gpu_ordinals is None:
            self._gpu_ordinals = _resolve_gpu_ordinals()
        try:
            return self._gpu_ordinals[gpu_uuid]
        except KeyError as exc:
            raise RuntimeError(
                "keepalive CUDA PCI ordinal mapping does not contain requested GPU UUID"
            ) from exc

    def start(self, gpu_uuid: str) -> KeepaliveProcessIdentity:
        gpu_uuid = validate_gpu_uuid(gpu_uuid)
        cuda_visible_device = self._cuda_visible_device(gpu_uuid)
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
                    "--worker-marker",
                    WORKER_PROCESS_MARKER,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # The worker's CUDA selector is assigned only here, from the
                # helper's validated typed target.  It is not an API/CLI
                # parameter and no caller can add an environment variable.
                env={
                    **os.environ,
                    "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                    "CUDA_VISIBLE_DEVICES": cuda_visible_device,
                },
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
                _terminate_started_process(process)
            else:
                process.wait(timeout=2)
            detail = message.removeprefix("ERROR:") or "worker readiness timed out"
            raise RuntimeError(f"CUDA keepalive worker could not start: {detail}")
        try:
            # READY is emitted only after exec and fixed-argument parsing.  Capturing
            # the /proc marker here avoids mistaking the short pre-exec fork window
            # for an identity mismatch while the Popen handle still safely owns
            # failure cleanup.
            identity = _capture_worker_process_identity(process.pid)
        except RuntimeError as exc:
            _terminate_started_process(process)
            raise RuntimeError("CUDA keepalive worker identity could not be verified") from exc
        if not self.is_running(identity):
            _terminate_started_process(process)
            raise RuntimeError("CUDA keepalive worker identity changed during startup")
        return identity

    def is_running(self, identity: KeepaliveProcessIdentity) -> bool:
        return _worker_process_matches(identity)

    def stop(self, identity: KeepaliveProcessIdentity) -> None:
        # A pidfd pins the process that was opened, closing the final check-to-
        # signal PID-reuse race.  Hosts without pidfds fail closed: sending an
        # ordinary PID signal would not provide the required identity safety.
        if not self.is_running(identity):
            return
        try:
            pidfd = _pidfd_open(identity.pid)
        except ProcessLookupError:
            return
        except NotImplementedError as exc:
            raise RuntimeError("safe keepalive process signaling is unavailable") from exc
        except OSError as exc:
            raise RuntimeError("keepalive worker identity could not be opened safely") from exc
        try:
            # Revalidate after opening the pidfd.  If the PID was already
            # reused, the pidfd points at that foreign process and no signal is
            # sent.  Once validated, later pidfd signals cannot follow reuse.
            if not self.is_running(identity):
                return
            try:
                _pidfd_send_signal(pidfd, signal.SIGTERM)
            except ProcessLookupError:
                return
            except (NotImplementedError, OSError) as exc:
                raise RuntimeError("keepalive worker could not be signaled safely") from exc
            ready, _, _ = select.select([pidfd], [], [], WORKER_STOP_TIMEOUT_SECONDS)
            if ready:
                return
            try:
                _pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                return
            except (NotImplementedError, OSError) as exc:
                raise RuntimeError("keepalive worker could not be signaled safely") from exc
            ready, _, _ = select.select([pidfd], [], [], 2)
            if not ready:
                raise RuntimeError("CUDA keepalive worker did not stop")
        finally:
            os.close(pidfd)


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
        identities: dict[str, KeepaliveProcessIdentity],
    ) -> KeepaliveResponse:
        results_by_uuid: dict[str, KeepaliveGPUResult] = {}
        pending: list[str] = []
        for gpu_uuid in requested:
            identity = identities.get(gpu_uuid)
            if identity is not None and self.provider.is_running(identity):
                results_by_uuid[gpu_uuid] = KeepaliveGPUResult(
                    gpu_uuid=gpu_uuid,
                    status="running",
                    outcome="unchanged",
                )
                continue
            if identity is not None:
                identities.pop(gpu_uuid)
                self._write_identities(identities)
            pending.append(gpu_uuid)

        failures: list[Exception] = []
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending)) as executor:
                started = {
                    gpu_uuid: executor.submit(self.provider.start, gpu_uuid)
                    for gpu_uuid in pending
                }
                for gpu_uuid in pending:
                    try:
                        identity = started[gpu_uuid].result()
                    except Exception as exc:
                        failures.append(exc)
                        continue
                    identities[gpu_uuid] = identity
                    results_by_uuid[gpu_uuid] = KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid,
                        status="running",
                        outcome="started",
                    )
            self._write_identities(identities)
        if failures:
            raise RuntimeError(f"CUDA keepalive worker could not start: {failures[0]}")
        return KeepaliveResponse(
            enabled=True,
            results=tuple(results_by_uuid[gpu_uuid] for gpu_uuid in requested),
        )

    def _disable(
        self,
        requested: tuple[str, ...],
        identities: dict[str, KeepaliveProcessIdentity],
    ) -> KeepaliveResponse:
        results: list[KeepaliveGPUResult] = []
        for gpu_uuid in requested:
            identity = identities.get(gpu_uuid)
            if identity is None:
                results.append(
                    KeepaliveGPUResult(
                        gpu_uuid=gpu_uuid, status="stopped", outcome="unchanged"
                    )
                )
                continue
            if self.provider.is_running(identity):
                self.provider.stop(identity)
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
        return self.state_directory / "workers.v3.json"

    @property
    def _legacy_state_path(self) -> Path:
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

    def _read_identities(self) -> dict[str, KeepaliveProcessIdentity]:
        # Never inspect or remove a v2 state file: its PID-only identity format
        # is not safe to reconcile.  Refuse all operations until an operator
        # handles the legacy file explicitly.
        if self._legacy_state_path.exists():
            raise RuntimeError(
                "legacy keepalive worker state workers.v2.json is present; "
                f"schema version mismatch: expected {KEEPALIVE_SCHEMA_VERSION}"
            )
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("keepalive worker state is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
            observed_version = value.get("schema_version") if isinstance(value, dict) else None
            raise RuntimeError(
                "keepalive worker state schema version mismatch: "
                f"expected {KEEPALIVE_SCHEMA_VERSION}, got {observed_version!r}"
            )
        try:
            workers = value["workers"]
        except (TypeError, KeyError) as exc:
            raise RuntimeError("keepalive worker state has invalid workers") from exc
        if not isinstance(workers, list):
            raise RuntimeError("keepalive worker state has invalid workers")
        identities: dict[str, KeepaliveProcessIdentity] = {}
        for worker in workers:
            try:
                gpu_uuid = validate_gpu_uuid(worker["gpu_uuid"])
                pid = worker["pid"]
            except (TypeError, KeyError, ValueError, KeepaliveProtocolError) as exc:
                raise RuntimeError("keepalive worker state has invalid worker") from exc
            if type(pid) is not int or pid <= 0:
                raise RuntimeError("keepalive worker state has invalid worker")
            identity_fields = {"boot_id", "start_time_ticks", "worker_marker"}
            if identity_fields.intersection(worker) != identity_fields:
                raise RuntimeError("keepalive worker state has invalid worker identity")
            boot_id = worker["boot_id"]
            start_time_ticks = worker["start_time_ticks"]
            worker_marker = worker["worker_marker"]
            if (
                not isinstance(boot_id, str)
                or not boot_id
                or len(boot_id) > 128
                or type(start_time_ticks) is not int
                or start_time_ticks <= 0
                or worker_marker != WORKER_PROCESS_MARKER
            ):
                raise RuntimeError("keepalive worker state has invalid worker identity")
            identity = KeepaliveProcessIdentity(
                pid=pid,
                boot_id=boot_id,
                start_time_ticks=start_time_ticks,
                worker_marker=worker_marker,
            )
            if gpu_uuid in identities:
                raise RuntimeError("keepalive worker state contains duplicate GPU UUIDs")
            identities[gpu_uuid] = identity
        return identities

    def _write_identities(self, identities: dict[str, KeepaliveProcessIdentity]) -> None:
        if not identities:
            try:
                self._state_path.unlink()
            except FileNotFoundError:
                return
            self._fsync_state_directory()
            return
        workers: list[dict[str, Any]] = []
        for gpu_uuid, identity in sorted(identities.items()):
            worker: dict[str, Any] = {
                "gpu_uuid": gpu_uuid,
                "pid": identity.pid,
                "boot_id": identity.boot_id,
                "start_time_ticks": identity.start_time_ticks,
                "worker_marker": identity.worker_marker,
            }
            workers.append(worker)
        payload = json.dumps(
            {"schema_version": KEEPALIVE_SCHEMA_VERSION, "workers": workers},
            separators=(",", ":"),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_directory,
            prefix=".workers.v3.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        descriptor_open = True
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                descriptor_open = False
                state_file.write(payload)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self._state_path)
            self._fsync_state_directory()
        finally:
            if descriptor_open:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _fsync_state_directory(self) -> None:
        directory_descriptor = os.open(
            self.state_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def handle_request(
    payload: bytes,
    *,
    controller: LocalKeepaliveController | None = None,
) -> KeepaliveResponse:
    request = KeepaliveRequest.decode(payload)
    return (controller or LocalKeepaliveController()).set_enabled(request.enabled, request.gpu_uuids)


def _terminate_started_process(process: subprocess.Popen[bytes]) -> None:
    """Stop only the direct child represented by this fresh Popen handle."""

    if process.poll() is not None:
        process.wait(timeout=2)
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


_LINUX_BOOT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _read_linux_boot_id() -> str:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Linux boot identity is unavailable") from exc
    if _LINUX_BOOT_ID_PATTERN.fullmatch(boot_id) is None:
        raise RuntimeError("Linux boot identity is invalid")
    return boot_id


def _read_process_start_time_ticks(pid: int) -> int:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("worker process start identity is unavailable") from exc
    _prefix, separator, fields = stat_line.rpartition(") ")
    if not separator:
        raise RuntimeError("worker process start identity is invalid")
    # The tail starts with proc(5) field 3 (state); starttime is field 22.
    tail = fields.split()
    try:
        start_time_ticks = int(tail[19])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("worker process start identity is invalid") from exc
    if start_time_ticks <= 0:
        raise RuntimeError("worker process start identity is invalid")
    return start_time_ticks


def _read_process_command(pid: int) -> tuple[bytes, ...]:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise RuntimeError("worker process marker is unavailable") from exc
    arguments = tuple(argument for argument in command.split(b"\0") if argument)
    if not arguments:
        raise RuntimeError("worker process marker is unavailable")
    return arguments


def _command_has_worker_marker(arguments: tuple[bytes, ...]) -> bool:
    module = TorchSubprocessProvider._worker_marker.encode("ascii")
    has_module = any(
        arguments[index : index + 2] == (b"-m", module)
        for index in range(len(arguments) - 1)
    )
    marker = WORKER_PROCESS_MARKER.encode("ascii")
    return has_module and b"--internal-worker" in arguments and any(
        arguments[index : index + 2] == (b"--worker-marker", marker)
        for index in range(len(arguments) - 1)
    )


def _capture_worker_process_identity(pid: int) -> KeepaliveProcessIdentity:
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("worker PID is invalid")
    identity = KeepaliveProcessIdentity(
        pid=pid,
        boot_id=_read_linux_boot_id(),
        start_time_ticks=_read_process_start_time_ticks(pid),
        worker_marker=WORKER_PROCESS_MARKER,
    )
    try:
        marked = _command_has_worker_marker(_read_process_command(pid))
    except UnicodeEncodeError as exc:
        raise RuntimeError("worker process marker is invalid") from exc
    if not marked:
        raise RuntimeError("worker process marker does not match")
    return identity


def _worker_process_matches(identity: KeepaliveProcessIdentity) -> bool:
    if (
        type(identity.pid) is not int
        or identity.pid <= 0
        or identity.worker_marker != WORKER_PROCESS_MARKER
    ):
        return False
    try:
        return (
            _read_linux_boot_id() == identity.boot_id
            and _read_process_start_time_ticks(identity.pid) == identity.start_time_ticks
            and _command_has_worker_marker(_read_process_command(identity.pid))
        )
    except (RuntimeError, UnicodeEncodeError):
        return False


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


_PCI_BUS_ID_PATTERN = re.compile(
    r"^(?P<domain>[0-9A-Fa-f]{4,8}):(?P<bus>[0-9A-Fa-f]{2}):"
    r"(?P<device>[0-9A-Fa-f]{2})\.(?P<function>[0-7])$"
)


def _resolve_gpu_ordinals() -> dict[str, str]:
    """Map UUID identity to CUDA ordinals under ``PCI_BUS_ID`` ordering."""

    observed: list[tuple[tuple[int, int, int, int], str]] = []
    seen_indices: set[int] = set()
    seen_uuids: set[str] = set()
    seen_bus_ids: set[tuple[int, int, int, int]] = set()
    output = _run_nvidia_smi_query("--query-gpu=index,uuid,pci.bus_id")
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",", maxsplit=2)]
        if len(parts) != 3 or not parts[0].isdigit():
            raise RuntimeError("keepalive CUDA PCI ordinal mapping is invalid")
        observed_index = int(parts[0])
        gpu_uuid = validate_gpu_uuid(parts[1])
        match = _PCI_BUS_ID_PATTERN.fullmatch(parts[2])
        if match is None:
            raise RuntimeError("keepalive CUDA PCI ordinal mapping is invalid")
        bus_id = tuple(int(match.group(name), 16) for name in ("domain", "bus", "device", "function"))
        if observed_index in seen_indices or gpu_uuid in seen_uuids or bus_id in seen_bus_ids:
            raise RuntimeError("keepalive CUDA PCI ordinal mapping is invalid")
        seen_indices.add(observed_index)
        seen_uuids.add(gpu_uuid)
        seen_bus_ids.add(bus_id)
        observed.append((bus_id, gpu_uuid))
    if not observed:
        raise RuntimeError("keepalive CUDA PCI ordinal mapping is empty")
    return {
        gpu_uuid: str(ordinal)
        for ordinal, (_bus_id, gpu_uuid) in enumerate(sorted(observed))
    }


def _run_cuda_worker(ready_fd: int) -> None:
    """Run one fixed worker against the single GPU made visible by its provider."""

    ready = os.fdopen(ready_fd, "wb", buffering=0)
    try:
        cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible_devices is None:
            raise RuntimeError("CUDA_VISIBLE_DEVICES must be set only by the keepalive provider")
        if not cuda_visible_devices.isdigit():
            raise RuntimeError("keepalive provider supplied an invalid CUDA device index")
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch with CUDA support is required") from exc
        torch.set_num_threads(1)
        with contextlib.suppress(RuntimeError):
            torch.set_num_interop_threads(1)
        visible_device_count = torch.cuda.device_count()
        if visible_device_count != 1:
            raise RuntimeError(
                f"CUDA visible device count is {visible_device_count}; expected exactly one"
            )
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch CUDA runtime could not initialize the selected GPU")

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
    parser.add_argument("--protocol-info", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-marker", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.protocol_info:
        if (
            arguments.schema_version is not None
            or arguments.internal_worker
            or arguments.ready_fd is not None
            or arguments.worker_marker is not None
        ):
            parser.error("invalid protocol-info invocation")
        sys.stdout.buffer.write(
            json.dumps(keepalive_protocol_info(), separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        return
    if arguments.internal_worker:
        if (
            arguments.schema_version is not None
            or arguments.ready_fd is None
            or arguments.worker_marker != WORKER_PROCESS_MARKER
        ):
            parser.error("invalid internal worker invocation")
        _run_cuda_worker(arguments.ready_fd)
        return
    if (
        arguments.schema_version != KEEPALIVE_SCHEMA_VERSION
        or arguments.ready_fd is not None
        or arguments.worker_marker is not None
    ):
        parser.error(
            "keepalive schema version mismatch: "
            f"expected {KEEPALIVE_SCHEMA_VERSION}"
        )
    try:
        payload = sys.stdin.buffer.read()
        response = handle_request(payload)
        sys.stdout.buffer.write(response.encode())
    except Exception as exc:
        print(f"serverpilot-keepalive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
