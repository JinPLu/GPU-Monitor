"""Small JSON contract for per-GPU occupancy start and stop."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


KEEPALIVE_SCHEMA_VERSION = 3
KEEPALIVE_IMPLEMENTATION_VERSION = "1.5.5"
KEEPALIVE_PROTOCOL_INFO_CAPABILITIES = (
    "per_gpu_keepalive",
    "pidfd_identity",
    "pci_bus_id",
)
# Fixed layout under every endpoint workspace. This is deliberately not
# configurable and is invoked directly instead of being found through PATH.
KEEPALIVE_ENTRYPOINT = "./serverpilot-keepalive"
KEEPALIVE_PROTOCOL_INFO_COMMAND = f"{KEEPALIVE_ENTRYPOINT} --protocol-info"
KEEPALIVE_REMOTE_COMMAND = (
    f"{KEEPALIVE_ENTRYPOINT} --schema-version {KEEPALIVE_SCHEMA_VERSION}"
)

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
    if not value and not allow_empty:
        raise KeepaliveProtocolError("keepalive gpu_uuids cannot be empty")
    return tuple(validate_gpu_uuid(item) for item in value)


@dataclass(frozen=True, slots=True)
class KeepaliveRequest:
    """A v3 per-GPU request."""

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
    def decode(cls, payload: bytes | str) -> KeepaliveRequest:
        value = _decode_object(payload)
        if value.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
            raise KeepaliveProtocolError(
                "keepalive schema version mismatch: "
                f"expected {KEEPALIVE_SCHEMA_VERSION}, got {value.get('schema_version')!r}"
            )
        try:
            enabled = value["enabled"]
            raw_gpu_uuids = value["gpu_uuids"]
        except KeyError as exc:
            raise KeepaliveProtocolError("keepalive request is missing a required field") from exc
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive enabled must be a boolean")
        return cls(enabled=enabled, gpu_uuids=_validate_gpu_uuids(raw_gpu_uuids))

KeepaliveStatus = Literal["running", "stopped"]
KeepaliveOutcome = Literal["started", "stopped", "unchanged"]


@dataclass(frozen=True, slots=True)
class KeepaliveGPUResult:
    """One GPU's reconciliation result."""

    gpu_uuid: str
    status: KeepaliveStatus
    outcome: KeepaliveOutcome

    @property
    def changed(self) -> bool:
        return self.outcome != "unchanged"


@dataclass(frozen=True, slots=True)
class KeepaliveResponse:
    enabled: bool
    results: tuple[KeepaliveGPUResult, ...]

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
                    }
                    for result in self.results
                ],
            }
        )

    @classmethod
    def decode(cls, payload: bytes | str) -> KeepaliveResponse:
        value = _decode_object(payload)
        if value.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
            raise KeepaliveProtocolError(
                "keepalive schema version mismatch: "
                f"expected {KEEPALIVE_SCHEMA_VERSION}, got {value.get('schema_version')!r}"
            )
        try:
            enabled = value["enabled"]
            raw_results = value["results"]
        except KeyError as exc:
            raise KeepaliveProtocolError("keepalive response is missing a required field") from exc
        if type(enabled) is not bool:
            raise KeepaliveProtocolError("keepalive response enabled is invalid")
        if not isinstance(raw_results, list):
            raise KeepaliveProtocolError("keepalive response results must be an array")
        results = tuple(_decode_gpu_result(raw) for raw in raw_results)
        return cls(enabled=enabled, results=results)


def _decode_gpu_result(value: object) -> KeepaliveGPUResult:
    if not isinstance(value, dict):
        raise KeepaliveProtocolError("keepalive GPU result fields are invalid")
    try:
        gpu_uuid = validate_gpu_uuid(value["gpu_uuid"])
        status = value["status"]
        outcome = value["outcome"]
    except KeyError as exc:
        raise KeepaliveProtocolError("keepalive GPU result is missing a required field") from exc
    if status not in {"running", "stopped"}:
        raise KeepaliveProtocolError("keepalive GPU result status is invalid")
    if outcome not in {"started", "stopped", "unchanged"}:
        raise KeepaliveProtocolError("keepalive GPU result outcome is invalid")
    return KeepaliveGPUResult(
        gpu_uuid=gpu_uuid,
        status=status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
    )


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"


def keepalive_protocol_info() -> dict[str, Any]:
    """Return the helper capability record used by adapter preflight."""

    return {
        "kind": "serverpilot-keepalive",
        "schema_version": KEEPALIVE_SCHEMA_VERSION,
        "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
        "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
    }


def _decode_object(payload: bytes | str) -> dict[str, Any]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepaliveProtocolError("keepalive message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise KeepaliveProtocolError("keepalive message must be a JSON object")
    return value
