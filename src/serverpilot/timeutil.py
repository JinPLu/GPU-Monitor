"""UTC-only time and stable JSON/hash helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(UTC)


def json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_load(value: str) -> Any:
    return json.loads(value)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json_dump(value).encode("utf-8")).hexdigest()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
