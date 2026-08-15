from __future__ import annotations

import importlib.util
import json
import sys
import types
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "desktop" / "windows_launcher.py"
    spec = importlib.util.spec_from_file_location("serverpilot_windows_launcher", path)
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
            "SERVERPILOT_INVENTORY": "",
            "SERVERPILOT_DATABASE_URL": "",
            "SERVERPILOT_BIND_PORT": "8899",
        }
    )

    assert paths.data_dir == tmp_path / "ServerPilot"
    assert paths.inventory_path == tmp_path / "ServerPilot" / "inventory.yaml"
    assert paths.database_url.endswith("/ServerPilot/state/serverpilot.sqlite3")
    assert paths.port == 8899
    assert paths.external_inventory is False


def test_windows_runtime_paths_reject_invalid_port() -> None:
    launcher = load_launcher()

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"SERVERPILOT_BIND_PORT": "not-a-port"})

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"SERVERPILOT_BIND_PORT": "70000"})


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


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_windows_desktop_bridge_limits_the_rest_surface_and_sets_identity_headers(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append((request, timeout))
        return FakeResponse({"data": {"current": {"endpoints": []}}})

    paths = launcher.RuntimePaths(
        data_dir=tmp_path,
        inventory_path=tmp_path / "inventory.yaml",
        database_url="sqlite:///example.sqlite3",
        port=8787,
        external_inventory=False,
    )
    bridge = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener)

    result = bridge.claim(
        {
            "project_id": "demo",
            "task_ref": "train",
            "purpose": "test",
            "constraints": {"gpu_count": 1},
            "unexpected_url": "https://example.invalid",
        }
    )

    assert result["ok"] is True
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8787/api/v1/claims"
    assert timeout == 8
    assert request.get_header("X-serverpilot-actor") == "human"
    assert request.get_header("Idempotency-key").startswith("windows-")
    assert json.loads(request.data.decode("utf-8")) == {
        "project_id": "demo",
        "task_ref": "train",
        "purpose": "test",
        "constraints": {"gpu_count": 1},
    }


def test_windows_desktop_bridge_maps_service_errors_without_exposing_transport_details(tmp_path: Path) -> None:
    launcher = load_launcher()

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        del timeout
        raise HTTPError(
            request.full_url,
            422,
            "unprocessable entity",
            hdrs=None,
            fp=BytesIO('{"error":{"code":"no_capacity","message":"当前没有可用 GPU。"}}'.encode("utf-8")),
        )

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    result = launcher.DesktopBridge("http://127.0.0.1:8787", paths, opener=opener).snapshot()

    assert result == {
        "ok": False,
        "error": {"code": "no_capacity", "message": "当前没有可用 GPU。"},
    }


def test_windows_desktop_ui_is_bundled_as_a_dedicated_full_window_surface() -> None:
    root = Path(__file__).resolve().parents[1]
    index = root / "desktop" / "windows" / "ui" / "index.html"
    script = root / "desktop" / "windows" / "ui" / "app.js"
    spec = root / "desktop" / "windows" / "ServerPilotWindows.spec"

    index_source = index.read_text(encoding="utf-8")
    assert "服务器" in index_source
    assert 'id="usage-page"' in index_source
    assert "GPU 显存状态" in script.read_text(encoding="utf-8")
    assert "endpoint_history" in script.read_text(encoding="utf-8")
    assert "usageRecords" in script.read_text(encoding="utf-8")
    assert "DesktopBridge" in (root / "desktop" / "windows_launcher.py").read_text(encoding="utf-8")
    spec_source = spec.read_text(encoding="utf-8")
    assert '"desktop" / "windows" / "ui"' in spec_source
    assert 'icon=str(project_root / "desktop" / "assets" / "ServerPilot Icon.png")' in spec_source


def test_windows_desktop_window_uses_local_ui_and_webview2_http_host(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    launcher = load_launcher()
    calls: dict[str, object] = {}

    def create_window(title: str, **kwargs: object) -> None:
        calls["title"] = title
        calls["window"] = kwargs

    def start(**kwargs: object) -> None:
        calls["start"] = kwargs

    fake_webview = types.SimpleNamespace(create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    stopped = []
    server = types.SimpleNamespace(stop=lambda: stopped.append(True))
    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)

    launcher.run_desktop_window("http://127.0.0.1:8787/", paths, server)

    assert calls["title"] == "ServerPilot"
    window_url = str(calls["window"]["url"]).replace("\\", "/")  # type: ignore[index]
    assert window_url.endswith("desktop/windows/ui/index.html")
    assert calls["start"] == {"gui": "edgechromium", "private_mode": True, "http_server": True}
    assert stopped == [True]
