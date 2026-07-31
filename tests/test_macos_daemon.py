from __future__ import annotations

import plistlib
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gpu_broker import cli, daemon, mcp_server
from gpu_broker.daemon import (
    DaemonConfig,
    DaemonError,
    MacOSDaemonManager,
    daemon_instance_id,
    probe_ready,
    render_launch_agent,
    resolve_daemon_config,
)


def _config(tmp_path: Path) -> DaemonConfig:
    executable = tmp_path / "bin" / "gpu-broker"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    data_dir = tmp_path / "Application Support" / "GPU Broker"
    return DaemonConfig(
        base_url="http://127.0.0.1:8787",
        host="127.0.0.1",
        port=8787,
        data_dir=data_dir,
        database_path=data_dir / "state/gpu-broker.sqlite3",
        inventory_path=data_dir / "inventory.yaml",
        plist_path=tmp_path / "Library/LaunchAgents/local.gpu-broker.daemon.plist",
        log_dir=tmp_path / "Library/Logs/GPU Broker",
        lock_path=data_dir / "daemon.ensure.lock",
        executable=executable,
    )


def test_resolve_daemon_config_uses_application_support_and_explicit_executable(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "gpu-broker"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    config = resolve_daemon_config(
        {
            "HOME": str(tmp_path),
            "GPU_BROKER_DAEMON_EXECUTABLE": str(executable),
            "GPU_BROKER_URL": "http://127.0.0.1:8787",
            "GPU_BROKER_DATA_DIR": str(tmp_path / "ignored-data"),
            "GPU_BROKER_DATABASE_PATH": str(tmp_path / "ignored.sqlite3"),
            "GPU_BROKER_INVENTORY": str(tmp_path / "ignored.yaml"),
        }
    )

    assert config.data_dir == tmp_path / "Library/Application Support/GPU Broker"
    assert config.database_path == config.data_dir / "state/gpu-broker.sqlite3"
    assert config.inventory_path == config.data_dir / "inventory.yaml"
    assert config.executable == executable


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8787",
        "http://10.40.1.222:8787",
        "http://127.0.0.1:8787/api",
        "http://user:secret@127.0.0.1:8787",
    ],
)
def test_resolve_daemon_config_rejects_non_loopback_or_ambiguous_urls(
    tmp_path: Path,
    url: str,
) -> None:
    executable = tmp_path / "gpu-broker"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    with pytest.raises(DaemonError):
        resolve_daemon_config(
            {
                "HOME": str(tmp_path),
                "GPU_BROKER_DAEMON_EXECUTABLE": str(executable),
                "GPU_BROKER_URL": url,
            }
        )


def test_launch_agent_owns_one_loopback_server_and_preserves_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    payload = plistlib.loads(render_launch_agent(config))

    assert payload["Label"] == "local.gpu-broker.daemon"
    assert payload["WorkingDirectory"] == str(config.data_dir)
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    arguments = payload["ProgramArguments"]
    assert arguments[0] == str(config.executable)
    assert arguments[1:] == [
        "serve",
        "--db",
        str(config.database_path),
        "--inventory",
        str(config.inventory_path),
        "--host",
        "127.0.0.1",
        "--port",
        "8787",
        "--daemon-instance-id",
        daemon_instance_id(config),
    ]


def test_ready_probe_requires_exact_daemon_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    payload = {
        "status": "ready",
        "database_ready": True,
        "inventory_readable": True,
        "single_writer": True,
        "daemon_instance_id": "some-other-process",
    }
    monkeypatch.setattr(daemon, "_probe_json", lambda *_args, **_kwargs: payload)

    with pytest.raises(DaemonError, match="not the installed"):
        probe_ready(config)

    payload["daemon_instance_id"] = daemon_instance_id(config)
    assert probe_ready(config) == payload


def test_install_migrates_inventory_and_database_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    source_root = tmp_path / "source"
    (source_root / "configs").mkdir(parents=True)
    (source_root / "state").mkdir()
    inventory_text = "schema_version: 1\nprojects: []\nendpoints: []\n"
    (source_root / "configs/inventory.yaml").write_text(
        inventory_text,
        encoding="utf-8",
    )
    with sqlite3.connect(source_root / "state/gpu-broker.sqlite3") as connection:
        connection.execute("CREATE TABLE proof (value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof VALUES ('preserved')")

    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(manager, "_loaded", lambda: False)

    first = manager.install(source_root, start=False)
    second = manager.install(source_root, start=False)

    assert first["migrated_inventory"] is True
    assert first["migrated_database"] is True
    assert second["migrated_inventory"] is False
    assert second["migrated_database"] is False
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT value FROM proof").fetchone() == ("preserved",)
    assert config.inventory_path.read_text(encoding="utf-8") == inventory_text
    assert config.plist_path.is_file()


def test_invalid_inventory_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    source_root = tmp_path / "source"
    (source_root / "configs").mkdir(parents=True)
    (source_root / "configs/inventory.yaml").write_text("projects: [", encoding="utf-8")
    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(manager, "_loaded", lambda: False)

    with pytest.raises(DaemonError, match="inventory is invalid"):
        manager.install(source_root, start=False)

    assert not config.inventory_path.exists()


def test_ensure_is_noop_when_compatible_service_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(
        daemon,
        "probe_live",
        lambda _config: {
            "status": "live",
            "schema_version": "v1",
            "capabilities": ["coordination_board"],
        },
    )
    monkeypatch.setattr(
        daemon,
        "probe_ready",
        lambda _config: {
            "status": "ready",
            "database_ready": True,
            "inventory_readable": True,
            "single_writer": True,
            "daemon_instance_id": daemon_instance_id(config),
            "process_id": 4242,
        },
    )
    monkeypatch.setattr(manager, "_loaded", lambda: True)
    monkeypatch.setattr(manager, "_launchd_pid", lambda: 4242)
    monkeypatch.setattr(
        manager,
        "start",
        lambda: pytest.fail("healthy ensure must not restart the daemon"),
    )

    result = manager.ensure()

    assert result["live"] is True
    assert result["ready"] is True
    assert not config.lock_path.exists()


def test_ensure_rejects_matching_identity_from_non_launchd_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(
        daemon,
        "probe_live",
        lambda _config: {
            "status": "live",
            "schema_version": "v1",
            "capabilities": ["coordination_board"],
        },
    )
    monkeypatch.setattr(
        daemon,
        "probe_ready",
        lambda _config: {
            "status": "ready",
            "database_ready": True,
            "inventory_readable": True,
            "single_writer": True,
            "daemon_instance_id": daemon_instance_id(config),
            "process_id": 9001,
        },
    )
    monkeypatch.setattr(manager, "_launchd_pid", lambda: None)
    monkeypatch.setattr(manager, "_loaded", lambda: False)

    with pytest.raises(DaemonError, match="not served by"):
        manager.ensure()


def test_serve_rejects_daemon_identity_for_alternate_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    alternate_inventory = tmp_path / "alternate.yaml"
    alternate_inventory.write_text(
        "schema_version: 1\nprojects: []\nendpoints: []\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "serve",
            "--db",
            str(tmp_path / "alternate.sqlite3"),
            "--inventory",
            str(alternate_inventory),
            "--daemon-instance-id",
            daemon_instance_id(config),
        ],
    )

    assert result.exit_code == 2
    assert "does not match --db and --inventory" in result.output


def test_ensure_rejects_foreign_service_without_owned_launch_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(
        daemon,
        "probe_live",
        lambda _config: {
            "status": "live",
            "schema_version": "v1",
            "capabilities": ["coordination_board"],
        },
    )
    monkeypatch.setattr(
        daemon,
        "probe_ready",
        lambda _config: (_ for _ in ()).throw(
            DaemonError("not the installed daemon")
        ),
    )
    monkeypatch.setattr(manager, "_loaded", lambda: False)

    with pytest.raises(DaemonError, match="not the installed daemon"):
        manager.ensure()


def test_uninstall_preserves_plist_when_launchctl_cannot_unload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    config = _config(tmp_path)
    config.plist_path.parent.mkdir(parents=True)
    config.plist_path.write_bytes(render_launch_agent(config))
    manager = MacOSDaemonManager(config)
    monkeypatch.setattr(manager, "_loaded", lambda: True)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "operation failed"

    monkeypatch.setattr(manager, "_launchctl", lambda *_args, **_kwargs: Failed())
    ticks = iter((0.0, 4.0))
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(ticks))

    with pytest.raises(DaemonError, match="did not unload"):
        manager.uninstall()

    assert config.plist_path.is_file()


def test_mcp_ensures_daemon_before_constructing_rest_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeClient:
        pass

    monkeypatch.setattr(
        mcp_server,
        "ensure_broker_ready_for_mcp",
        lambda: calls.append("ensure"),
    )
    monkeypatch.setattr(
        mcp_server.BrokerClient,
        "from_env",
        lambda *, actor=None: calls.append(f"client:{actor}") or FakeClient(),
    )

    assert isinstance(mcp_server._client("agent-a"), FakeClient)
    assert calls == ["ensure", "client:agent-a"]


def test_macos_gui_no_longer_owns_or_terminates_server_process() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "desktop" / "GPU Broker.swift"
    ).read_text(encoding="utf-8")

    assert '"daemon", "ensure", "--source-root"' in source
    launch_body = source.split(
        "func applicationDidFinishLaunching", maxsplit=1
    )[1].split("func applicationShouldTerminate", maxsplit=1)[0]
    assert "ensureDaemon()" in launch_body
    assert "connectOrStartServer()" not in launch_body
    for forbidden in (
        "serverProcess",
        "process.terminate()",
        '"serve", "--db"',
        "startServer(executable:",
    ):
        assert forbidden not in source
