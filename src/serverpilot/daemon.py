"""macOS user-daemon lifecycle for the loopback ServerPilot control plane."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .config import ConfigurationError, load_inventory
try:
    import fcntl
except ImportError:  # pragma: no cover - the first implementation is macOS-only
    fcntl = None  # type: ignore[assignment]


DAEMON_LABEL = "local.serverpilot.daemon"
LEGACY_DAEMON_LABEL = "local.gpu-broker.daemon"
# A daemon can keep the same semantic version while still running an older
# in-memory module after ``uv tool install --force`` or an app rebuild.  Keep a
# small capability floor for the lifecycle probe so ``ensure`` treats that
# process as stale and restarts the owned LaunchAgent.  This is deliberately a
# feature capability (rather than a timestamp or package version): it is
# stable across installs and cannot be spoofed by an unchanged semver.
EXPECTED_CAPABILITIES = frozenset({"coordination_board", "endpoint_conflict_cleanup"})
DAEMON_PROTOCOL = "macos-launchagent-v1"


class DaemonError(RuntimeError):
    """Raised when the local daemon cannot be installed or made ready."""


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    base_url: str
    host: str
    port: int
    data_dir: Path
    database_path: Path
    inventory_path: Path
    plist_path: Path
    log_dir: Path
    lock_path: Path
    executable: Path
    label: str = DAEMON_LABEL


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home()).expanduser().resolve()


def _loopback_url(value: str) -> tuple[str, int, str]:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise DaemonError("macOS daemon auto-start requires a loopback http SERVERPILOT_URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DaemonError("SERVERPILOT_URL must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise DaemonError("SERVERPILOT_URL must not include an API path")
    port = parsed.port or 80
    host = parsed.hostname
    assert host is not None
    return host, port, f"http://{host}:{port}"


def _daemon_executable(environment: Mapping[str, str], home: Path) -> Path:
    configured = environment.get("SERVERPILOT_DAEMON_EXECUTABLE")
    candidates = [
        Path(configured).expanduser() if configured else None,
        home / ".local/share/uv/tools/serverpilot/bin/serverpilot",
        Path(shutil.which("serverpilot") or ""),
        Path(sys.executable).with_name("serverpilot"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise DaemonError(
        "cannot find a durable serverpilot executable; install it with "
        "`uv tool install --force /path/to/serverpilot`"
    )


def resolve_daemon_config(
    environment: Mapping[str, str] | None = None,
) -> DaemonConfig:
    environment = os.environ if environment is None else environment
    home = _home(environment)
    host, port, base_url = _loopback_url(
        environment.get("SERVERPILOT_URL", "http://127.0.0.1:8787")
    )
    data_dir = (home / "Library/Application Support/ServerPilot").resolve()
    database_path = data_dir / "state/serverpilot.sqlite3"
    inventory_path = data_dir / "inventory.yaml"
    return DaemonConfig(
        base_url=base_url,
        host=host,
        port=port,
        data_dir=data_dir,
        database_path=database_path,
        inventory_path=inventory_path,
        plist_path=home / "Library/LaunchAgents" / f"{DAEMON_LABEL}.plist",
        log_dir=home / "Library/Logs/ServerPilot",
        lock_path=data_dir / "daemon.ensure.lock",
        executable=_daemon_executable(environment, home),
    )


def daemon_instance_id_for_paths(
    database_path: Path,
    inventory_path: Path,
    *,
    label: str = DAEMON_LABEL,
) -> str:
    identity = "|".join(
        (
            DAEMON_PROTOCOL,
            label,
            str(database_path.expanduser().resolve()),
            str(inventory_path.expanduser().resolve()),
        )
    )
    return identity


def daemon_instance_id(config: DaemonConfig) -> str:
    return daemon_instance_id_for_paths(
        config.database_path,
        config.inventory_path,
        label=config.label,
    )


def _probe_json(url: str, path: str, timeout_seconds: float = 0.8) -> dict[str, Any] | None:
    try:
        response = httpx.get(
            f"{url}{path}",
            timeout=timeout_seconds,
            trust_env=False,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise DaemonError(
            f"{url} responded to {path} with incompatible HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DaemonError(f"{url} is occupied by a non-JSON service") from exc
    if not isinstance(payload, dict):
        raise DaemonError(f"{url} returned an invalid health response")
    return payload


def probe_live(config: DaemonConfig) -> dict[str, Any] | None:
    payload = _probe_json(config.base_url, "/health/live")
    if payload is None:
        return None
    capabilities = payload.get("capabilities")
    if (
        payload.get("status") != "live"
        or payload.get("schema_version") != "v1"
        or not isinstance(capabilities, list)
        or not EXPECTED_CAPABILITIES.issubset(capabilities)
    ):
        raise DaemonError(
            f"{config.base_url} is occupied by an incompatible ServerPilot service"
        )
    return payload


def probe_ready(config: DaemonConfig) -> dict[str, Any] | None:
    payload = _probe_json(config.base_url, "/health/ready")
    if payload is None:
        return None
    if (
        payload.get("status") != "ready"
        or payload.get("database_ready") is not True
        or payload.get("inventory_readable") is not True
        or payload.get("single_writer") is not True
        or payload.get("daemon_instance_id") != daemon_instance_id(config)
    ):
        raise DaemonError(
            f"{config.base_url} is not the installed ServerPilot macOS daemon"
        )
    return payload


def render_launch_agent(config: DaemonConfig) -> bytes:
    stdout_path = config.log_dir / "daemon.stdout.log"
    stderr_path = config.log_dir / "daemon.stderr.log"
    payload = {
        "Label": config.label,
        "ProgramArguments": [
            str(config.executable),
            "serve",
            "--db",
            str(config.database_path),
            "--inventory",
            str(config.inventory_path),
            "--host",
            config.host,
            "--port",
            str(config.port),
            "--daemon-instance-id",
            daemon_instance_id(config),
        ],
        "WorkingDirectory": str(config.data_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _publish_inventory(source: Path, destination: Path) -> bool:
    if destination.exists():
        if not destination.is_file():
            raise DaemonError(f"inventory target is not a regular file: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".inventory.",
            suffix=".yaml",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copy2(source, temporary)
        try:
            load_inventory(temporary)
        except ConfigurationError as exc:
            raise DaemonError(f"source ServerPilot inventory is invalid: {exc}") from exc
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_sqlite(source: Path, destination: Path) -> bool:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        if not destination.is_file():
            raise DaemonError(f"database target is not a regular file: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        integrity = source_db.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise DaemonError("source ServerPilot database failed integrity_check")
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=".serverpilot.",
                suffix=".sqlite3",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
            with sqlite3.connect(temporary) as destination_db:
                source_db.backup(destination_db)
            with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as copied_db:
                copied_integrity = copied_db.execute("PRAGMA integrity_check").fetchone()
            if copied_integrity is None or copied_integrity[0] != "ok":
                raise DaemonError("migrated ServerPilot database failed integrity_check")
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return False
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class MacOSDaemonManager:
    def __init__(self, config: DaemonConfig | None = None) -> None:
        if sys.platform != "darwin":
            raise DaemonError("headless daemon management is currently implemented for macOS only")
        self.config = config or resolve_daemon_config()

    @property
    def launch_domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def service_target(self) -> str:
        return f"{self.launch_domain}/{self.config.label}"

    @property
    def legacy_service_target(self) -> str:
        return f"{self.launch_domain}/{LEGACY_DAEMON_LABEL}"

    def _launchctl(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def _loaded(self) -> bool:
        result = self._launchctl("print", self.service_target, check=False)
        return result.returncode == 0

    def _legacy_loaded(self) -> bool:
        try:
            result = self._launchctl("print", self.legacy_service_target, check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _launchd_pid(self) -> int | None:
        result = self._launchctl("print", self.service_target, check=False)
        if result.returncode != 0:
            return None
        match = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _parent_process_id(process_id: int) -> int | None:
        """Return one process's parent without invoking a shell.

        PyInstaller's onefile bootloader remains the launchd-owned process and
        starts the Python service as its direct child. A direct parent match is
        therefore an owned service; arbitrary descendants are intentionally not
        accepted.
        """

        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(process_id)],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.8,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            parent_id = int(result.stdout.strip())
        except ValueError:
            return None
        return parent_id if parent_id > 0 else None

    def _ready_process_is_owned_by_launchd(
        self, process_id: object, launchd_pid: int | None
    ) -> bool:
        if (
            launchd_pid is None
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id < 1
        ):
            return False
        return process_id == launchd_pid or self._parent_process_id(process_id) == launchd_pid

    def _probe_owned_ready(self) -> dict[str, Any] | None:
        live = probe_live(self.config)
        if live is None:
            return None
        ready = probe_ready(self.config)
        if ready is None:
            return None
        launchd_pid = self._launchd_pid()
        if not self._ready_process_is_owned_by_launchd(ready.get("process_id"), launchd_pid):
            raise DaemonError(
                f"{self.config.base_url} is not served by {self.service_target}"
            )
        return ready

    def _write_plist(self) -> bool:
        expected = render_launch_agent(self.config)
        if self.config.plist_path.is_file() and self.config.plist_path.read_bytes() == expected:
            return False
        self.config.plist_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.config.plist_path.parent,
                prefix=f".{self.config.label}.",
                suffix=".plist",
                delete=False,
            ) as handle:
                handle.write(expected)
                temporary = Path(handle.name)
            os.replace(temporary, self.config.plist_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True

    @contextlib.contextmanager
    def _install_lock(self) -> Any:
        if fcntl is None:  # pragma: no cover - guarded by the macOS constructor
            raise DaemonError("file locking is unavailable on this platform")
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield

    def _bootout_if_loaded(self) -> bool:
        if not self._loaded():
            return False
        result = self._launchctl("bootout", self.service_target, check=False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._loaded():
                return True
            time.sleep(0.05)
        details = (result.stderr or result.stdout).strip()
        raise DaemonError(
            f"launchctl bootout did not unload {self.service_target}"
            + (f": {details}" if details else "")
        )

    def _bootout_legacy_if_loaded(self) -> bool:
        if not self._legacy_loaded():
            return False
        result = self._launchctl("bootout", self.legacy_service_target, check=False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._legacy_loaded():
                return True
            time.sleep(0.05)
        details = (result.stderr or result.stdout).strip()
        raise DaemonError(
            f"launchctl bootout did not unload {self.legacy_service_target}"
            + (f": {details}" if details else "")
        )

    def _prepare_data(self, source_root: Path | None) -> dict[str, bool]:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        legacy_data_dir = self.config.data_dir.parent / "GPU Broker"
        migrated_inventory = False
        migrated_database = False
        if not self.config.inventory_path.is_file():
            legacy_inventory = legacy_data_dir / "inventory.yaml"
            source_inventory = (
                legacy_inventory
                if legacy_inventory.is_file()
                else (
                    source_root.expanduser().resolve() / "configs/inventory.yaml"
                    if source_root is not None
                    else None
                )
            )
            if source_inventory is None:
                raise DaemonError(
                    f"missing {self.config.inventory_path}; run `serverpilot daemon install "
                    "--source-root /path/to/serverpilot` once"
                )
            if not source_inventory.is_file():
                raise DaemonError(f"source inventory does not exist: {source_inventory}")
            migrated_inventory = _publish_inventory(
                source_inventory,
                self.config.inventory_path,
            )
        if not self.config.database_path.exists():
            legacy_database = legacy_data_dir / "state/gpu-broker.sqlite3"
            source_database = legacy_database
            if not source_database.is_file() and source_root is not None:
                source_database = (
                    source_root.expanduser().resolve() / "state/serverpilot.sqlite3"
                )
            if source_database.is_file():
                migrated_database = _copy_sqlite(
                    source_database,
                    self.config.database_path,
                )
        return {
            "migrated_inventory": migrated_inventory,
            "migrated_database": migrated_database,
        }

    def _install_locked(
        self,
        source_root: Path | None = None,
        *,
        start: bool = True,
    ) -> dict[str, Any]:
        self._bootout_legacy_if_loaded()
        migration = self._prepare_data(source_root)
        changed = self._write_plist()
        if self._loaded() and changed:
            self._bootout_if_loaded()
        if start:
            self.start()
        return {
            "installed": True,
            "plist_changed": changed,
            "data_dir": str(self.config.data_dir),
            "database_path": str(self.config.database_path),
            "inventory_path": str(self.config.inventory_path),
            **migration,
        }

    def install(self, source_root: Path | None = None, *, start: bool = True) -> dict[str, Any]:
        with self._install_lock():
            return self._install_locked(source_root, start=start)

    def start(self, *, timeout_seconds: float = 15.0) -> None:
        if not self.config.plist_path.is_file():
            raise DaemonError("daemon is not installed")
        if not self._loaded():
            result = self._launchctl(
                "bootstrap",
                self.launch_domain,
                str(self.config.plist_path),
                check=False,
            )
            if result.returncode != 0 and not self._loaded():
                raise DaemonError(
                    f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}"
                )
        self._launchctl("kickstart", "-k", self.service_target)
        deadline = time.monotonic() + timeout_seconds
        last_error: DaemonError | None = None
        while time.monotonic() < deadline:
            try:
                if self._probe_owned_ready() is not None:
                    return
            except DaemonError as exc:
                last_error = exc
            time.sleep(0.15)
        if last_error is not None:
            raise last_error
        raise DaemonError(
            f"ServerPilot daemon did not become ready at {self.config.base_url}"
        )

    def stop(self) -> None:
        self._bootout_if_loaded()

    def uninstall(self) -> dict[str, Any]:
        with self._install_lock():
            self.stop()
            removed = self.config.plist_path.exists()
            self.config.plist_path.unlink(missing_ok=True)
        return {
            "uninstalled": True,
            "plist_removed": removed,
            "data_preserved": str(self.config.data_dir),
        }

    def status(self) -> dict[str, Any]:
        live = probe_live(self.config)
        ready = self._probe_owned_ready() if live is not None else None
        return {
            "installed": self.config.plist_path.is_file(),
            "loaded": self._loaded(),
            "live": live is not None,
            "ready": ready is not None,
            "base_url": self.config.base_url,
            "data_dir": str(self.config.data_dir),
            "database_path": str(self.config.database_path),
            "inventory_path": str(self.config.inventory_path),
        }

    def ensure(
        self,
        source_root: Path | None = None,
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        self._bootout_legacy_if_loaded()
        try:
            if self._probe_owned_ready() is not None:
                return self.status()
        except DaemonError:
            if not (self._loaded() and self.config.plist_path.is_file()):
                raise
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        with self._install_lock():
            try:
                if self._probe_owned_ready() is not None:
                    return self.status()
            except DaemonError:
                if not (self._loaded() and self.config.plist_path.is_file()):
                    raise
            self._install_locked(source_root, start=False)
            self.start(timeout_seconds=timeout_seconds)
            return self.status()


def ensure_broker_ready_for_mcp() -> None:
    if sys.platform != "darwin":
        return
    if os.environ.get("SERVERPILOT_AUTOSTART", "1").lower() in {"0", "false", "no"}:
        return
    MacOSDaemonManager().ensure()


def format_status(value: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2)
    return "\n".join(f"{key}: {item}" for key, item in value.items())
