from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "desktop" / "windows_launcher.py"
    spec = importlib.util.spec_from_file_location("gpu_broker_windows_launcher", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_runtime_paths_use_local_app_data(tmp_path: Path) -> None:
    launcher = load_launcher()

    paths = launcher.runtime_paths(
        {
            "LOCALAPPDATA": str(tmp_path),
            "GPU_BROKER_INVENTORY": "",
            "GPU_BROKER_DATABASE_URL": "",
            "GPU_BROKER_BIND_PORT": "8899",
        }
    )

    assert paths.data_dir == tmp_path / "GPU Broker"
    assert paths.inventory_path == tmp_path / "GPU Broker" / "inventory.yaml"
    assert paths.database_url.endswith("/GPU Broker/state/gpu-broker.sqlite3")
    assert paths.port == 8899
    assert paths.external_inventory is False


def test_windows_runtime_paths_reject_invalid_port() -> None:
    launcher = load_launcher()

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"GPU_BROKER_BIND_PORT": "not-a-port"})

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"GPU_BROKER_BIND_PORT": "70000"})


def test_windows_launcher_creates_default_inventory_without_overwriting(tmp_path: Path) -> None:
    launcher = load_launcher()
    inventory = tmp_path / "inventory.yaml"

    launcher.ensure_inventory(inventory, create_default=True)

    parsed = yaml.safe_load(inventory.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed["collector"]["enabled"] is True
    assert parsed["endpoints"] == []

    inventory.write_text("schema_version: 1\nendpoints: []\n", encoding="utf-8")
    launcher.ensure_inventory(inventory, create_default=True)

    assert inventory.read_text(encoding="utf-8") == "schema_version: 1\nendpoints: []\n"


def test_windows_launcher_requires_external_inventory_to_exist(tmp_path: Path) -> None:
    launcher = load_launcher()

    with pytest.raises(launcher.LauncherError):
        launcher.ensure_inventory(tmp_path / "missing.yaml", create_default=False)
