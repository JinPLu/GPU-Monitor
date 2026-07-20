"""Windows desktop launcher for the bundled GPU Broker web console.

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
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from gpu_broker.api import create_app
from gpu_broker.config import Settings


APP_NAME = "GPU Broker"
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
    configured = environment.get("GPU_BROKER_DATA_DIR") or None
    if configured:
        return Path(configured).expanduser()
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / ".gpu-broker"


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.expanduser().resolve().as_posix()}"


def runtime_paths(environment: Mapping[str, str] | None = None) -> RuntimePaths:
    environment = os.environ if environment is None else environment
    data_dir = default_data_dir(environment)
    inventory_config = environment.get("GPU_BROKER_INVENTORY") or None
    inventory_path = Path(inventory_config).expanduser() if inventory_config else data_dir / "inventory.yaml"
    database_url = (environment.get("GPU_BROKER_DATABASE_URL") or None) or sqlite_url(
        data_dir / "state" / "gpu-broker.sqlite3"
    )
    try:
        port = int(environment.get("GPU_BROKER_BIND_PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise LauncherError("GPU_BROKER_BIND_PORT 必须是 1 到 65535 之间的整数。") from exc
    if not 1 <= port <= 65535:
        raise LauncherError("GPU_BROKER_BIND_PORT 必须是 1 到 65535 之间的整数。")
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
    raise LauncherError("找不到可用的本机端口来启动 GPU Broker。")


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
        self._thread = threading.Thread(target=self._server.run, name="gpu-broker-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)


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


def run_status_window(base_url: str, paths: RuntimePaths, server: BrokerServer | None) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("430x220")
    root.minsize(390, 210)

    icon = resource_path("desktop", "assets", "GPU Broker Icon.png")
    if icon.is_file():
        try:
            image = tk.PhotoImage(file=str(icon))
            root.iconphoto(True, image)
        except tk.TclError:
            pass

    root.columnconfigure(0, weight=1)
    frame = tk.Frame(root, padx=22, pady=20)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)

    title = tk.Label(frame, text=APP_NAME, font=("Segoe UI", 16, "bold"), anchor="w")
    title.grid(row=0, column=0, sticky="ew")

    status = tk.Label(
        frame,
        text=f"本机服务已运行：{base_url}",
        font=("Segoe UI", 10),
        anchor="w",
        wraplength=370,
        justify="left",
    )
    status.grid(row=1, column=0, sticky="ew", pady=(10, 2))

    data_path = tk.Label(
        frame,
        text=f"数据目录：{paths.data_dir}",
        font=("Segoe UI", 9),
        fg="#54656f",
        anchor="w",
        wraplength=370,
        justify="left",
    )
    data_path.grid(row=2, column=0, sticky="ew")

    button_bar = tk.Frame(frame)
    button_bar.grid(row=3, column=0, sticky="ew", pady=(22, 0))

    def open_dashboard() -> None:
        webbrowser.open(base_url)

    def copy_url() -> None:
        root.clipboard_clear()
        root.clipboard_append(base_url)
        messagebox.showinfo(APP_NAME, "Dashboard 地址已复制。")

    def quit_app() -> None:
        if server is not None:
            server.stop()
        root.destroy()

    tk.Button(button_bar, text="打开 Dashboard", command=open_dashboard, width=16).pack(side="left")
    tk.Button(button_bar, text="复制地址", command=copy_url, width=10).pack(side="left", padx=(10, 0))
    tk.Button(button_bar, text="退出", command=quit_app, width=8).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()


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
            raise LauncherError("本机 GPU Broker 服务未能在规定时间内启动。请检查数据目录和 inventory。")

    base_url = f"http://127.0.0.1:{paths.port}/"
    webbrowser.open(base_url)
    run_status_window(base_url, paths, server)
    return 0


def main() -> int:
    try:
        return launch()
    except Exception as exc:
        show_error("无法启动 GPU Broker", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
