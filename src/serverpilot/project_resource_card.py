"""Strict project-local pointer to a ServerPilot workload profile.

The card deliberately contains no placement or execution instructions.  A
consumer claims ``profile_id`` through ServerPilot, then resolves
``execution_entrypoint`` in the project's own, separately declared entrypoint
registry.  Host names, GPU identities, paths, arguments, and shell text do not
belong in this contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


PROJECT_RESOURCE_CARD_SCHEMA_VERSION = 1
MAX_PROJECT_RESOURCE_CARD_BYTES = 4_096
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"


class ProjectResourceCardError(ValueError):
    """Raised when a project resource card cannot be read, decoded, or validated."""


class ProjectResourceCard(BaseModel):
    """Schema v1 pointer to one direct-GPU profile and one project entrypoint."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[PROJECT_RESOURCE_CARD_SCHEMA_VERSION]
    profile_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    execution_entrypoint: str = Field(pattern=_IDENTIFIER_PATTERN)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        # ``bool`` is an ``int`` subclass in Python; the wire contract must not
        # therefore accept JSON true as schema version 1.
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value


def loads_project_resource_card(
    payload: str | bytes,
    *,
    source: str = "project resource card",
) -> ProjectResourceCard:
    """Decode one exact schema-v1 card from UTF-8 JSON."""

    if not isinstance(payload, (str, bytes)):
        raise TypeError("project resource card payload must be str or bytes")
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > MAX_PROJECT_RESOURCE_CARD_BYTES:
        raise ProjectResourceCardError(
            f"{source} exceeds {MAX_PROJECT_RESOURCE_CARD_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except ProjectResourceCardError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectResourceCardError(f"{source} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectResourceCardError(f"{source} must be a JSON object")
    try:
        return ProjectResourceCard.model_validate(value)
    except ValidationError as exc:
        raise ProjectResourceCardError(f"{source} does not match schema v1: {exc}") from exc


def load_project_resource_card(path: Path) -> ProjectResourceCard:
    """Read and validate one project resource card from ``path``."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProjectResourceCardError(f"cannot read project resource card {path}: {exc}") from exc
    return loads_project_resource_card(payload, source=f"project resource card {path}")


def dumps_project_resource_card(card: ProjectResourceCard) -> str:
    """Return deterministic, compact schema-v1 JSON terminated by a newline."""

    if not isinstance(card, ProjectResourceCard):
        raise TypeError("card must be a ProjectResourceCard")
    return json.dumps(
        card.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n"


def dump_project_resource_card(card: ProjectResourceCard, path: Path) -> None:
    """Validate and write one deterministic project resource card to ``path``."""

    payload = dumps_project_resource_card(card)
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise ProjectResourceCardError(f"cannot write project resource card {path}: {exc}") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProjectResourceCardError(
                f"project resource card JSON contains duplicate field {key!r}"
            )
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ProjectResourceCardError(
        f"project resource card JSON contains non-standard constant {value!r}"
    )
