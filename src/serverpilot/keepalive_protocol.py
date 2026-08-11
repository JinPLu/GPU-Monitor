"""Sealed JSON contract for the endpoint keepalive helper.

Protocol v2 is deliberately per-GPU.  The broker supplies an exact set of
already-known NVIDIA GPU UUIDs; it cannot supply a shell fragment, path,
environment, PID, or CUDA selector.  The helper reports one independently
attested worker for every requested GPU.

The v1 decoder is retained only to identify an old helper cleanly.  A v1
request cannot be executed by the v2 helper because whole-endpoint workers
cannot safely be translated into per-GPU ownership.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


KEEPALIVE_LEGACY_SCHEMA_VERSION = 1
KEEPALIVE_SCHEMA_VERSION = 2
KEEPALIVE_ENTRYPOINT = "serverpilot-keepalive"
KEEPALIVE_REMOTE_COMMAND = (
    f"{KEEPALIVE_ENTRYPOINT} --schema-version {KEEPALIVE_SCHEMA_VERSION}"
)

# A response for 64 GPUs remains comfortably below this limit.  Both sides
# reject larger input before decoding so a broken/malicious helper never makes
# either process retain unbounded output.
MAX_KEEPALIVE_MESSAGE_BYTES = 16_384
MAX_KEEPALIVE_GPU_UUIDS = 64
GPU_UUID_PATTERN = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class KeepaliveProtocolError(ValueError):
    """Raised when a keepalive protocol message is not exact and unambiguous."""


def validate_gpu_uuid(value: object) -> str:
    """Return one physical NVIDIA GPU UUID or reject it without coercion."""

    if not isinstance(value, str) or not GPU_UUID_PATTERN.fullmatch(value):
        raise KeepaliveProtocolError("keepalive GPU UUID is malformed")
    return value


def _validate_gpu_uuids(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise KeepaliveProtocolError("keepalive gpu_uuids must be an array")
    if len(value) > MAX_KEEPALIVE_GPU_UUIDS:
        raise KeepaliveProtocolError("keepalive gpu_uuids has too many entries")
    if not value and not allow_empty:
        raise KeepaliveProtocolError("keepalive gpu_uuids cannot be empty")
    gpu_uuids = tuple(validate_gpu_uuid(item) for item in value)
    if len(set(gpu_uuids)) != len(gpu_uuids):
        raise KeepaliveProtocolError("keepalive gpu_uuids contains duplicates")
    return gpu_uuids


@dataclass(frozen=True, slots=True)
class KeepaliveRequest:
    """A v2 exact GPU-set request.

    ``gpu_uuids`` is a tuple internally so it cannot be mutated after the
    request has crossed the adapter boundary.  ``encode`` also validates it,
    so callers that construct this dataclass directly get the same strict
    boundary as decoded requests.
    """

    enabled: bool
    gpu_uuids: tuple[str, ...]

    def encode(self) -> bytes:
        if type(self.enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        gpu_uuids = _validate_gpu_uuids(list(self.gpu_uuids))
        return _encode(
            {
                "schema_version": KEEPALIVE_SCHEMA_VERSION,
                "enabled": self.enabled,
                "gpu_uuids": list(gpu_uuids),
            }
        )

    @classmethod
    def decode(cls, payload: bytes | str) -> KeepaliveRequest | LegacyKeepaliveRequest:
        value = _decode_object(payload)
        schema_version = value.get("schema_version")
        if schema_version == KEEPALIVE_LEGACY_SCHEMA_VERSION:
            return LegacyKeepaliveRequest.decode_object(value)
        if schema_version != KEEPALIVE_SCHEMA_VERSION:
            raise KeepaliveProtocolError(
                "only keepalive schema versions 1 and 2 are recognized"
            )
        if set(value) != {"schema_version", "enabled", "gpu_uuids"}:
            raise KeepaliveProtocolError("keepalive request fields do not match schema v2")
        enabled = value["enabled"]
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        return cls(enabled=enabled, gpu_uuids=_validate_gpu_uuids(value["gpu_uuids"]))


@dataclass(frozen=True, slots=True)
class LegacyKeepaliveRequest:
    """Decoded v1 request, never executable by the v2 helper."""

    enabled: bool

    @classmethod
    def decode_object(cls, value: dict[str, Any]) -> LegacyKeepaliveRequest:
        if set(value) != {"schema_version", "enabled"}:
            raise KeepaliveProtocolError("keepalive request fields do not match schema v1")
        enabled = value["enabled"]
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        return cls(enabled=enabled)


KeepaliveStatus = Literal["running", "stopped"]
KeepaliveOutcome = Literal["started", "stopped", "unchanged"]


@dataclass(frozen=True, slots=True)
class KeepaliveWorkerAttestation:
    """The helper's host-visible PID and private lifecycle identity."""

    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if (
            type(self.pid) is not int
            or self.pid <= 0
            or type(self.start_ticks) is not int
            or self.start_ticks <= 0
        ):
            raise KeepaliveProtocolError("keepalive worker attestation is invalid")


@dataclass(frozen=True, slots=True)
class KeepaliveGPUResult:
    """One exact GPU's idempotent reconciliation result."""

    gpu_uuid: str
    status: KeepaliveStatus
    outcome: KeepaliveOutcome
    worker: KeepaliveWorkerAttestation | None

    def __post_init__(self) -> None:
        validate_gpu_uuid(self.gpu_uuid)
        if self.status not in {"running", "stopped"}:
            raise KeepaliveProtocolError("keepalive GPU result status is invalid")
        if self.outcome not in {"started", "stopped", "unchanged"}:
            raise KeepaliveProtocolError("keepalive GPU result outcome is invalid")
        if self.status == "running":
            if self.worker is None:
                raise KeepaliveProtocolError("running keepalive GPU result lacks worker")
            if self.outcome == "stopped":
                raise KeepaliveProtocolError("running keepalive GPU result cannot be stopped")
        elif self.worker is not None:
            raise KeepaliveProtocolError("stopped keepalive GPU result has worker")
        elif self.outcome == "started":
            raise KeepaliveProtocolError("stopped keepalive GPU result cannot be started")

    @property
    def changed(self) -> bool:
        return self.outcome != "unchanged"


@dataclass(frozen=True, slots=True)
class KeepaliveResponse:
    enabled: bool
    results: tuple[KeepaliveGPUResult, ...]

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        if not self.results or len(self.results) > MAX_KEEPALIVE_GPU_UUIDS:
            raise KeepaliveProtocolError("keepalive response must contain bounded GPU results")
        gpu_uuids = tuple(result.gpu_uuid for result in self.results)
        if len(gpu_uuids) != len(set(gpu_uuids)):
            raise KeepaliveProtocolError("keepalive response contains duplicate GPU results")
        expected_status: KeepaliveStatus = "running" if self.enabled else "stopped"
        if any(result.status != expected_status for result in self.results):
            raise KeepaliveProtocolError("keepalive response state is inconsistent")

    def encode(self) -> bytes:
        return _encode(
            {
                "schema_version": KEEPALIVE_SCHEMA_VERSION,
                "enabled": self.enabled,
                "results": [
                    {
                        "gpu_uuid": result.gpu_uuid,
                        "status": result.status,
                        "outcome": result.outcome,
                        "worker": (
                            {
                                "pid": result.worker.pid,
                                "start_ticks": result.worker.start_ticks,
                            }
                            if result.worker is not None
                            else None
                        ),
                    }
                    for result in self.results
                ],
            }
        )

    @classmethod
    def decode(cls, payload: bytes | str) -> KeepaliveResponse:
        value = _decode_object(payload)
        if value.get("schema_version") == KEEPALIVE_LEGACY_SCHEMA_VERSION:
            # This spelling lets the sealed adapter distinguish an old remote
            # helper from a malformed v2 answer without treating v1 as a valid
            # per-GPU result.
            raise KeepaliveProtocolError("keepalive schema v1 response is unsupported for per-GPU control")
        if value.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
            raise KeepaliveProtocolError("only keepalive schema version 2 is supported")
        if set(value) != {"schema_version", "enabled", "results"}:
            raise KeepaliveProtocolError("keepalive response fields do not match schema v2")
        enabled = value["enabled"]
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive response enabled is invalid")
        raw_results = value["results"]
        if not isinstance(raw_results, list):
            raise KeepaliveProtocolError("keepalive response results must be an array")
        if not raw_results or len(raw_results) > MAX_KEEPALIVE_GPU_UUIDS:
            raise KeepaliveProtocolError("keepalive response results has invalid size")
        results = tuple(_decode_gpu_result(raw) for raw in raw_results)
        return cls(enabled=enabled, results=results)


def _decode_gpu_result(value: object) -> KeepaliveGPUResult:
    if not isinstance(value, dict) or set(value) != {"gpu_uuid", "status", "outcome", "worker"}:
        raise KeepaliveProtocolError("keepalive GPU result fields are invalid")
    gpu_uuid = validate_gpu_uuid(value["gpu_uuid"])
    status = value["status"]
    outcome = value["outcome"]
    raw_worker = value["worker"]
    worker: KeepaliveWorkerAttestation | None
    if raw_worker is None:
        worker = None
    elif isinstance(raw_worker, dict) and set(raw_worker) == {"pid", "start_ticks"}:
        pid = raw_worker["pid"]
        start_ticks = raw_worker["start_ticks"]
        if type(pid) is not int or pid <= 0 or type(start_ticks) is not int or start_ticks <= 0:
            raise KeepaliveProtocolError("keepalive worker attestation is invalid")
        worker = KeepaliveWorkerAttestation(pid=pid, start_ticks=start_ticks)
    else:
        raise KeepaliveProtocolError("keepalive worker attestation is invalid")
    return KeepaliveGPUResult(
        gpu_uuid=gpu_uuid,
        status=status,
        outcome=outcome,
        worker=worker,
    )


def _encode(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_KEEPALIVE_MESSAGE_BYTES:
        raise KeepaliveProtocolError("keepalive message is too large")
    return payload


def _decode_object(payload: bytes | str) -> dict[str, Any]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > MAX_KEEPALIVE_MESSAGE_BYTES:
        raise KeepaliveProtocolError("keepalive message is too large")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except KeepaliveProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepaliveProtocolError("keepalive message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise KeepaliveProtocolError("keepalive message must be a JSON object")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Make duplicate JSON fields a protocol error instead of last-key-wins."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise KeepaliveProtocolError("keepalive JSON object contains duplicate fields")
        value[key] = item
    return value
