from __future__ import annotations

import json

from typer.testing import CliRunner

from serverpilot import cli as cli_module
from serverpilot.cli import app


class _ProfilesClient:
    def __init__(self, profiles):  # type: ignore[no-untyped-def]
        self.profiles = profiles

    def workload_profiles(self):  # type: ignore[no-untyped-def]
        return {"schema_version": "v1", "data": self.profiles}


def _profile(**overrides):  # type: ignore[no-untyped-def]
    return {
        "id": "storyboard-renderer",
        "project_id": "storyboard",
        "runtime_kind": "direct-gpu",
        "enabled": True,
        **overrides,
    }


def test_resource_card_write_then_check_verifies_the_same_profile(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def client(url, actor):  # type: ignore[no-untyped-def]
        calls.append((url, actor))
        return _ProfilesClient([_profile()])

    monkeypatch.setattr(cli_module, "_client", client)
    path = tmp_path / ".serverpilot" / "resource-card.json"
    runner = CliRunner()

    written = runner.invoke(
        app,
        [
            "project",
            "resource-card",
            "write",
            "storyboard-renderer",
            "--entrypoint",
            "renderer-qualification",
            "--path",
            str(path),
            "--json",
        ],
    )

    assert written.exit_code == 0, written.output
    assert json.loads(written.stdout)["resource_card"] == {
        "schema_version": 1,
        "profile_id": "storyboard-renderer",
        "execution_entrypoint": "renderer-qualification",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "profile_id": "storyboard-renderer",
        "execution_entrypoint": "renderer-qualification",
    }

    checked = runner.invoke(
        app,
        ["project", "resource-card", "check", "--path", str(path), "--json"],
    )

    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)["status"] == "ready"
    assert calls == [(None, None), (None, None)]


def test_resource_card_write_does_not_create_an_unverified_card(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli_module, "_client", lambda _url, _actor: _ProfilesClient([]))
    path = tmp_path / "resource-card.json"

    result = CliRunner().invoke(
        app,
        [
            "project",
            "resource-card",
            "write",
            "missing-profile",
            "--entrypoint",
            "renderer-qualification",
            "--path",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not path.exists()


def test_resource_card_check_rejects_disabled_or_non_direct_gpu_profile(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "resource-card.json"
    path.write_text(
        '{"schema_version":1,"profile_id":"storyboard-renderer",'
        '"execution_entrypoint":"renderer-qualification"}\n',
        encoding="utf-8",
    )
    runner = CliRunner()

    monkeypatch.setattr(
        cli_module, "_client", lambda _url, _actor: _ProfilesClient([_profile(enabled=False)])
    )
    disabled = runner.invoke(app, ["project", "resource-card", "check", "--path", str(path)])
    assert disabled.exit_code == 2
    assert "disabled" in disabled.output

    monkeypatch.setattr(
        cli_module,
        "_client",
        lambda _url, _actor: _ProfilesClient([_profile(runtime_kind="slurm")]),
    )
    scheduler = runner.invoke(app, ["project", "resource-card", "check", "--path", str(path)])
    assert scheduler.exit_code == 2
