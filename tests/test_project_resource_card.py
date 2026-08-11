from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from serverpilot.project_resource_card import (
    MAX_PROJECT_RESOURCE_CARD_BYTES,
    ProjectResourceCard,
    ProjectResourceCardError,
    dump_project_resource_card,
    dumps_project_resource_card,
    load_project_resource_card,
    loads_project_resource_card,
)


VALID_CARD = {
    "schema_version": 1,
    "profile_id": "storyboard-renderer",
    "execution_entrypoint": "renderer-qualification",
}


def test_model_and_json_schema_are_exact_v1_contract() -> None:
    card = ProjectResourceCard.model_validate(VALID_CARD)

    assert card.model_dump(mode="json") == VALID_CARD
    schema = ProjectResourceCard.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["schema_version", "profile_id", "execution_entrypoint"]
    assert schema["properties"]["schema_version"]["const"] == 1


def test_card_is_immutable() -> None:
    card = ProjectResourceCard.model_validate(VALID_CARD)

    with pytest.raises(ValidationError, match="frozen"):
        card.profile_id = "another-profile"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_id", ""),
        ("profile_id", "A-profile"),
        ("profile_id", "a"),
        ("profile_id", "profile_with_underscore"),
        ("profile_id", "profile/id"),
        ("profile_id", "profile;echo"),
        ("profile_id", "a" * 65),
        ("execution_entrypoint", ""),
        ("execution_entrypoint", " run-task"),
        ("execution_entrypoint", "run task"),
        ("execution_entrypoint", "./run-task"),
        ("execution_entrypoint", "run-task --fast"),
        ("execution_entrypoint", "a" * 65),
    ],
)
def test_identifiers_are_nonempty_bounded_names(field: str, value: str) -> None:
    payload = {**VALID_CARD, field: value}

    with pytest.raises(ValidationError):
        ProjectResourceCard.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_CARD, "schema_version": "1"},
        {**VALID_CARD, "schema_version": True},
        {**VALID_CARD, "schema_version": 2},
        {**VALID_CARD, "profile_id": 123},
        {**VALID_CARD, "execution_entrypoint": ["renderer-qualification"]},
        {**VALID_CARD, "host": "gpu.example.test"},
        {**VALID_CARD, "gpu_ids": ["GPU-secret"]},
        {**VALID_CARD, "command": "python train.py"},
    ],
)
def test_model_rejects_coercion_wrong_version_and_extra_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProjectResourceCard.model_validate(payload)


@pytest.mark.parametrize("missing", list(VALID_CARD))
def test_model_rejects_missing_fields(missing: str) -> None:
    payload = {key: value for key, value in VALID_CARD.items() if key != missing}

    with pytest.raises(ValidationError):
        ProjectResourceCard.model_validate(payload)


def test_loads_rejects_duplicate_fields() -> None:
    payload = (
        '{"schema_version":1,"profile_id":"storyboard-renderer",'
        '"profile_id":"other-profile","execution_entrypoint":"renderer-qualification"}'
    )

    with pytest.raises(ProjectResourceCardError, match="duplicate field 'profile_id'"):
        loads_project_resource_card(payload)


@pytest.mark.parametrize("payload", ["[]", "null", '"card"', "1"])
def test_loads_requires_json_object(payload: str) -> None:
    with pytest.raises(ProjectResourceCardError, match="must be a JSON object"):
        loads_project_resource_card(payload)


@pytest.mark.parametrize("payload", ["{", "not json", '{"schema_version":NaN}'])
def test_loads_rejects_invalid_or_nonstandard_json(payload: str) -> None:
    with pytest.raises(ProjectResourceCardError):
        loads_project_resource_card(payload)


def test_loads_rejects_invalid_utf8() -> None:
    with pytest.raises(ProjectResourceCardError, match="not valid UTF-8 JSON"):
        loads_project_resource_card(b"\xff")


def test_loads_rejects_oversized_input_before_json_decoding() -> None:
    payload = b" " * (MAX_PROJECT_RESOURCE_CARD_BYTES + 1)

    with pytest.raises(ProjectResourceCardError, match="exceeds 4096 bytes"):
        loads_project_resource_card(payload)


def test_loads_uses_precise_error_and_preserves_validation_cause() -> None:
    with pytest.raises(ProjectResourceCardError, match="does not match schema v1") as raised:
        loads_project_resource_card(json.dumps({**VALID_CARD, "command": "python train.py"}))

    assert isinstance(raised.value.__cause__, ValidationError)


def test_dump_is_deterministic_and_round_trips(tmp_path) -> None:
    card = ProjectResourceCard.model_validate(VALID_CARD)
    path = tmp_path / "serverpilot-resources.json"

    dump_project_resource_card(card, path)

    expected = (
        '{"schema_version":1,"profile_id":"storyboard-renderer",'
        '"execution_entrypoint":"renderer-qualification"}\n'
    )
    assert dumps_project_resource_card(card) == expected
    assert path.read_text(encoding="utf-8") == expected
    assert load_project_resource_card(path) == card


def test_load_wraps_file_error(tmp_path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(ProjectResourceCardError, match="cannot read project resource card") as raised:
        load_project_resource_card(path)

    assert isinstance(raised.value.__cause__, OSError)


def test_dump_wraps_file_error(tmp_path) -> None:
    card = ProjectResourceCard.model_validate(VALID_CARD)

    with pytest.raises(ProjectResourceCardError, match="cannot write project resource card") as raised:
        dump_project_resource_card(card, tmp_path)

    assert isinstance(raised.value.__cause__, OSError)


def test_helpers_reject_wrong_python_input_types() -> None:
    with pytest.raises(TypeError, match="payload must be str or bytes"):
        loads_project_resource_card(VALID_CARD)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="card must be a ProjectResourceCard"):
        dumps_project_resource_card(VALID_CARD)  # type: ignore[arg-type]
