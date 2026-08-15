"""Windows desktop launcher for the bundled ServerPilot web console.

The launcher owns only local process lifecycle. Scheduling, leases, audit and
inventory validation stay in the shared FastAPI/BrokerService path.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import uvicorn

from serverpilot import __version__
from serverpilot.api import create_app
from serverpilot.config import Settings


APP_NAME = "ServerPilot"
DATA_DIRECTORY_NAME = "ServerPilot"
DEFAULT_PORT = 8787
READY_TIMEOUT_SECONDS = 30.0
DEFAULT_INVENTORY = """schema_version: 1
collector:
  enabled: true
  interval_seconds: 10
  stale_after_seconds: 30
  ssh_connect_timeout_seconds: 8
projects:
  - id: default
    display_name: Default
    weight: 1
    quota_gpus: null
    concurrency_limit: null
endpoints: []
"""


class LauncherError(RuntimeError):
    """Raised for launch-time failures that should be shown to the user."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    inventory_path: Path
    database_url: str
    port: int
    external_inventory: bool


def default_data_dir(environment: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    configured = environment.get("SERVERPILOT_DATA_DIR") or None
    if configured:
        return Path(configured).expanduser()
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / DATA_DIRECTORY_NAME
    return Path.home() / ".serverpilot"


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.expanduser().resolve().as_posix()}"


def runtime_paths(environment: Mapping[str, str] | None = None) -> RuntimePaths:
    environment = os.environ if environment is None else environment
    data_dir = default_data_dir(environment)
    inventory_config = environment.get("SERVERPILOT_INVENTORY") or None
    inventory_path = Path(inventory_config).expanduser() if inventory_config else data_dir / "inventory.yaml"
    database_url = (environment.get("SERVERPILOT_DATABASE_URL") or None) or sqlite_url(
        data_dir / "state" / "serverpilot.sqlite3"
    )
    try:
        port = int(environment.get("SERVERPILOT_BIND_PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise LauncherError("SERVERPILOT_BIND_PORT 必须是 1 到 65535 之间的整数。") from exc
    if not 1 <= port <= 65535:
        raise LauncherError("SERVERPILOT_BIND_PORT 必须是 1 到 65535 之间的整数。")
    return RuntimePaths(
        data_dir=data_dir,
        inventory_path=inventory_path,
        database_url=database_url,
        port=port,
        external_inventory=inventory_config is not None,
    )


def ensure_inventory(path: Path, *, create_default: bool) -> None:
    if path.is_file():
        return
    if not create_default:
        raise LauncherError(f"找不到 inventory 文件：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_INVENTORY, encoding="utf-8")


def resource_path(*parts: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return bundle_root.joinpath(*parts)


def broker_health(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=0.8) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return False
    capabilities = payload.get("capabilities")
    return (
        payload.get("status") == "live"
        and isinstance(capabilities, list)
        and "coordination_board" in capabilities
    )


def port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.4)
        return client.connect_ex(("127.0.0.1", port)) == 0


def choose_port(preferred: int) -> tuple[int, bool]:
    if broker_health(preferred):
        return preferred, True
    if not port_accepts_connections(preferred):
        return preferred, False
    for candidate in range(preferred + 1, min(preferred + 50, 65535) + 1):
        if not port_accepts_connections(candidate):
            return candidate, False
    raise LauncherError("找不到可用的本机端口来启动 ServerPilot。")


def wait_until_ready(port: int, timeout_seconds: float = READY_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if broker_health(port):
            return True
        time.sleep(0.2)
    return False


class BrokerServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(
            create_app(self.settings),
            host=self.settings.bind_host,
            port=self.settings.bind_port,
            log_level="warning",
            access_log=False,
            loop="asyncio",
            http="h11",
            ws="none",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="serverpilot-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)


class DesktopBridge:
    """Narrow pywebview bridge for the Windows-only desktop presentation.

    The frontend can only call named REST projections and mutations against the
    loopback broker started by this launcher. It never receives a database path,
    SSH command, arbitrary URL, or a generic request primitive.
    """

    def __init__(
        self,
        base_url: str,
        paths: RuntimePaths,
        *,
        actor_id: str = "human",
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.paths = paths
        self.actor_id = actor_id
        self._opener = opener

    def app_info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "data": {
                "name": APP_NAME,
                "version": __version__,
                "base_url": self.base_url,
                "data_dir": str(self.paths.data_dir),
            },
        }

    def snapshot(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/state")

    def endpoint_history(self, endpoint_id: str, window_seconds: int) -> dict[str, Any]:
        if window_seconds not in {3_600, 21_600, 86_400}:
            return self._invalid("资源历史范围无效。")
        return self._request(
            "GET",
            f"/api/v1/endpoints/{quote(endpoint_id, safe='')}/history"
            f"?window_seconds={window_seconds}&points=120",
        )

    def create_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id",
            "host",
            "port",
            "ssh_user",
            "workspace_path",
            "observation_profile",
            "keepalive_adapter_id",
            "labels",
            "owner_project_id",
        }
        return self._request("POST", "/api/v1/endpoints", self._only(endpoint, allowed))

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        allowed = {"project_id", "task_ref", "purpose", "constraints"}
        return self._request("POST", "/api/v1/claims", self._only(request, allowed))

    def set_keepalive(self, endpoint_id: str, enabled: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/endpoints/{quote(endpoint_id, safe='')}/keepalive",
            {"enabled": bool(enabled)},
        )

    def collector_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/settings/collector")

    def update_collector_interval(self, interval_seconds: int) -> dict[str, Any]:
        if interval_seconds not in {5, 10, 30}:
            return self._invalid("数据采集间隔必须是 5、10 或 30 秒。")
        return self._request(
            "PATCH",
            "/api/v1/settings/collector",
            {"interval_seconds": interval_seconds},
        )

    @staticmethod
    def _only(payload: object, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items() if key in allowed}

    @staticmethod
    def _invalid(message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": "invalid_input", "message": message}}

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(f"{self.base_url}{path}", data=body, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("X-ServerPilot-Actor", self.actor_id)
        if body is not None:
            request.add_header("Content-Type", "application/json")
            request.add_header("Idempotency-Key", f"windows-{uuid.uuid4()}")
        try:
            with self._opener(request, timeout=8) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            return self._http_error(exc)
        except (OSError, URLError, TimeoutError):
            return {
                "ok": False,
                "error": {"code": "local_service_unavailable", "message": "无法连接本机 ServerPilot 服务。"},
            }
        try:
            return {"ok": True, "data": json.loads(raw)}
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": {"code": "invalid_response", "message": "本机服务返回了无法读取的数据。"},
            }

    @staticmethod
    def _http_error(exc: HTTPError) -> dict[str, Any]:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return {
            "ok": False,
            "error": {
                "code": code if isinstance(code, str) else "service_rejected",
                "message": message if isinstance(message, str) else f"本机服务拒绝了操作（HTTP {exc.code}）。",
            },
        }


def settings_for(paths: RuntimePaths) -> Settings:
    return Settings(
        database_url=paths.database_url,
        inventory_path=paths.inventory_path,
        project_root=paths.data_dir,
        bind_host="127.0.0.1",
        bind_port=paths.port,
    )


def show_error(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        print(f"{title}: {message}", file=sys.stderr)
        return
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, message)
    root.destroy()


def run_desktop_window(base_url: str, paths: RuntimePaths, server: BrokerServer | None) -> None:
    """Show the dedicated Windows desktop view in the system WebView2 host."""

    try:
        import webview
    except ImportError as exc:
        raise LauncherError("Windows 桌面运行资源不完整；请重新安装 ServerPilot。") from exc

    index = resource_path("desktop", "windows", "ui", "index.html")
    if not index.is_file():
        raise LauncherError("Windows 桌面界面资源不完整；请重新安装 ServerPilot。")

    bridge = DesktopBridge(base_url, paths)
    webview.create_window(
        APP_NAME,
        url=str(index),
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(1024, 640),
        text_select=True,
        confirm_close=False,
    )
    try:
        webview.start(
            gui="edgechromium",
            private_mode=True,
            http_server=True,
        )
    except Exception as exc:
        raise LauncherError(
            "无法启动 Windows WebView2。请安装 Microsoft Edge WebView2 Runtime 后重试。"
        ) from exc
    finally:
        # The bundled backend belongs to this app instance. An already running
        # broker remains untouched so MCP and a separately launched app keep
        # sharing the same local control plane.
        if server is not None:
            server.stop()


def launch() -> int:
    paths = runtime_paths()
    ensure_inventory(paths.inventory_path, create_default=not paths.external_inventory)
    port, already_running = choose_port(paths.port)
    paths = replace(paths, port=port)
    server: BrokerServer | None = None

    if not already_running:
        server = BrokerServer(settings_for(paths))
        server.start()
        if not wait_until_ready(paths.port):
            server.stop()
            raise LauncherError("本机 ServerPilot 服务未能在规定时间内启动。请检查数据目录和 inventory。")

    base_url = f"http://127.0.0.1:{paths.port}/"
    run_desktop_window(base_url, paths, server)
    return 0


def main() -> int:
    try:
        return launch()
    except Exception as exc:
        show_error("无法启动 ServerPilot", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
