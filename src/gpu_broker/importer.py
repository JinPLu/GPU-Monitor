"""One-way importer for legacy servers.txt files into strict global inventory YAML."""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path

import yaml

from gpu_broker.config import EndpointConfig, InventoryConfig, ProjectConfig


SSH_PATTERN = re.compile(r"^\s*#?\s*ssh\s+-p\s+(?P<port>\d+)\s+(?P<user>[A-Za-z_][\w-]*)@(?P<host>[A-Za-z0-9_.-]+)\s*$")
SSH_COMMAND_PATTERN = re.compile(
    r"^ *ssh(?: +-p +(?P<port>[0-9]+))? +"
    r"(?P<user>[A-Za-z_][A-Za-z0-9_-]{0,31})@(?P<host>[A-Za-z0-9.-]+) *$",
    re.ASCII,
)
DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", re.ASCII)


@dataclass(frozen=True, slots=True)
class ParsedSSHCommand:
    """A strictly parsed SSH destination; parsing has no external side effects."""

    user: str
    host: str
    port: int

    @property
    def endpoint_id(self) -> str:
        normalized_host = re.sub(r"[^a-z0-9]+", "-", self.host).strip("-")
        return f"server-{normalized_host}-p{self.port}"

    @property
    def normalized_command(self) -> str:
        port = f" -p {self.port}" if self.port != 22 else ""
        return f"ssh{port} {self.user}@{self.host}"


def parse_ssh_command(command: str) -> ParsedSSHCommand:
    """Parse exactly ``ssh [-p PORT] USER@HOST`` without running or resolving it."""

    if not command or len(command) > 512:
        raise ValueError("SSH command must be one non-empty line")
    if any(ord(character) < 32 or ord(character) == 127 for character in command):
        raise ValueError("SSH command must not contain newlines or control characters")
    match = SSH_COMMAND_PATTERN.fullmatch(command)
    if match is None:
        raise ValueError("expected exactly: ssh [-p PORT] USER@HOST")

    raw_port = match.group("port")
    port = int(raw_port) if raw_port is not None else 22
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")

    host = match.group("host")
    if len(host) > 253:
        raise ValueError("SSH host is too long")
    if all(character in "0123456789." for character in host):
        try:
            normalized_host = str(IPv4Address(host))
        except AddressValueError as exc:
            raise ValueError("SSH host is not a valid IPv4 address") from exc
    else:
        labels = host.split(".")
        if not labels or any(DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
            raise ValueError("SSH host is not a valid DNS hostname")
        normalized_host = host.lower()

    parsed = ParsedSSHCommand(
        user=match.group("user"),
        host=normalized_host,
        port=port,
    )
    if len(parsed.endpoint_id) > 128:
        raise ValueError("SSH host is too long for the deterministic endpoint id")
    return parsed


@dataclass(frozen=True, slots=True)
class ImportReport:
    endpoints: list[EndpointConfig]
    duplicate_addresses: list[str]
    ignored_lines: int

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint_count": len(self.endpoints),
            "duplicate_addresses": self.duplicate_addresses,
            "ignored_lines": self.ignored_lines,
            "endpoints": [endpoint.model_dump(mode="json") for endpoint in self.endpoints],
        }


def _endpoint_id(host: str, port: int) -> str:
    safe_host = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    return f"ssh-{safe_host}-p{port}"


def import_servers_files(
    paths: list[Path],
    *,
    project_ids: list[str],
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
) -> ImportReport:
    if not paths:
        raise ValueError("at least one servers.txt path is required")
    if not project_ids:
        raise ValueError("at least one project id is required")
    parsed: dict[tuple[str, int], EndpointConfig] = {}
    duplicate_addresses: list[str] = []
    ignored = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = SSH_PATTERN.match(line)
            if match is None:
                ignored += 1
                continue
            host = match.group("host")
            port = int(match.group("port"))
            user = match.group("user")
            key = (host, port)
            if key in parsed:
                duplicate_addresses.append(f"{host}:{port}")
                continue
            parsed[key] = EndpointConfig(
                id=_endpoint_id(host, port),
                host=host,
                port=port,
                ssh_user=user,
                labels=["direct-ssh", "gpu"],
                storage_group=None,
                expected_gpu_count=expected_gpu_count,
                expected_gpu_total_vram_mib=expected_gpu_total_vram_mib,
                project_ids=project_ids,
            )
    if not parsed:
        raise ValueError("no ssh -p user@host endpoint lines found")
    return ImportReport(
        endpoints=sorted(parsed.values(), key=lambda endpoint: endpoint.id),
        duplicate_addresses=sorted(set(duplicate_addresses)),
        ignored_lines=ignored,
    )


def write_inventory(
    output: Path,
    report: ImportReport,
    *,
    projects: list[ProjectConfig],
) -> InventoryConfig:
    inventory = InventoryConfig(
        schema_version=1,
        projects=projects,
        endpoints=report.endpoints,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(inventory.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return inventory
