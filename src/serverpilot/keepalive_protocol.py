"""Sealed JSON contract for the endpoint keepalive helper.

The remote command is immutable.  The only caller-controlled value crossing
the boundary is the desired boolean state; paths, commands, environment,
process identifiers, and GPU selectors are deliberately absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal


KEEPALIVE_SCHEMA_VERSION = 1
KEEPALIVE_ENTRYPOINT = "serverpilot-keepalive"
KEEPALIVE_REMOTE_COMMAND = (
    f"{KEEPALIVE_ENTRYPOINT} --schema-version {KEEPALIVE_SCHEMA_VERSION}"
)
MAX_KEEPALIVE_MESSAGE_BYTES = 4_096


class KeepaliveProtocolError(ValueError):
    """Raised when a keepalive protocol message is not exactly schema v1."""


@dataclass(frozen=True, slots=True)
class KeepaliveRequest:
    enabled: bool

    def encode(self) -> bytes:
        return _encode({"schema_version": KEEPALIVE_SCHEMA_VERSION, "enabled": self.enabled})

    @classmethod
    def decode(cls, payload: bytes | str) -> KeepaliveRequest:
        value = _decode_object(payload)
        if set(value) != {"schema_version", "enabled"}:
            raise KeepaliveProtocolError("keepalive request fields do not match schema v1")
        _require_schema_version(value)
        enabled = value["enabled"]
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        return cls(enabled=enabled)


KeepaliveStatus = Literal["running", "stopped"]


@dataclass(frozen=True, slots=True)
class KeepaliveWorkerAttestation:
    """Helper-produced identity used only to match a fresh observation.

    ``pid`` is resolved through the helper's nvidia-smi view so it is in the
    same PID domain as collector observations. ``start_ticks`` remains opaque
    namespace-local lifecycle evidence and is never used as a remote stop
    target.
    """

    pid: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class KeepaliveResponse:
    enabled: bool
    changed: bool
    status: KeepaliveStatus
    worker: KeepaliveWorkerAttestation | None

    def encode(self) -> bytes:
        return _encode(
            {
                "schema_version": KEEPALIVE_SCHEMA_VERSION,
                "enabled": self.enabled,
                "changed": self.changed,
                "status": self.status,
                "worker": (
                    {"pid": self.worker.pid, "start_ticks": self.worker.start_ticks}
                    if self.worker is not None
                    else None
                ),
            }
        )

    @classmethod
    def decode(cls, payload: bytes | str) -> KeepaliveResponse:
        value = _decode_object(payload)
        if set(value) != {"schema_version", "enabled", "changed", "status", "worker"}:
            raise KeepaliveProtocolError("keepalive response fields do not match schema v1")
        _require_schema_version(value)
        enabled = value["enabled"]
        changed = value["changed"]
        status = value["status"]
        if type(enabled) is not bool or type(changed) is not bool:
            raise KeepaliveProtocolError("keepalive response booleans are invalid")
        if status not in {"running", "stopped"}:
            raise KeepaliveProtocolError("keepalive response status is invalid")
        if enabled != (status == "running"):
            raise KeepaliveProtocolError("keepalive response state is inconsistent")
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
        if enabled != (worker is not None):
            raise KeepaliveProtocolError("keepalive worker attestation is inconsistent")
        return cls(enabled=enabled, changed=changed, status=status, worker=worker)


def _encode(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_KEEPALIVE_MESSAGE_BYTES:  # defensive if schema grows
        raise KeepaliveProtocolError("keepalive message is too large")
    return payload


def _decode_object(payload: bytes | str) -> dict[str, Any]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > MAX_KEEPALIVE_MESSAGE_BYTES:
        raise KeepaliveProtocolError("keepalive message is too large")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepaliveProtocolError("keepalive message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise KeepaliveProtocolError("keepalive message must be a JSON object")
    return value


def _require_schema_version(value: dict[str, Any]) -> None:
    if value.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
        raise KeepaliveProtocolError(
            f"only keepalive schema version {KEEPALIVE_SCHEMA_VERSION} is supported"
        )
