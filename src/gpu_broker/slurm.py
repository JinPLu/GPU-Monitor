"""Bounded Slurm command adapter for external scheduler targets.

The adapter receives a fixed local command prefix (for example the user-owned
``hh22`` authentication helper) and appends one shell-quoted remote command.
It never accepts raw SSH options or a free-form remote command from MCP.
"""

from __future__ import annotations

import base64
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol


TERMINAL_SLURM_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "TIMEOUT",
}

SCHEDULER_INSPECTION_SCRIPT = r"""
set -uo pipefail

printf 'GB|identity|%s|%s|%s|%s\n' \
  "$(hostname -f 2>/dev/null || hostname)" \
  "$(id -un)" \
  "$HOME" \
  "$PWD"

emit_path() {
  label=$1
  candidate=$2
  if [ -e "$candidate" ]; then
    kind=other
    [ -d "$candidate" ] && kind=directory
    writable=false
    [ -w "$candidate" ] && writable=true
    printf 'GB|path|%s|%s|%s|%s\n' \
      "$label" "$candidate" "$kind" "$writable"
  fi
}

emit_path home "$HOME"
emit_path home-root /home
emit_path software /opt
emit_path scratch /scratch
emit_path data /data
emit_path public /public

df -Pk "$HOME" | awk \
  'NR == 2 { printf "GB|filesystem|%s|%s|%s|%s|%s|%s\n", $1, $2, $3, $4, $5, $6 }'

if command -v quota >/dev/null 2>&1; then
  (quota -s 2>&1 || true) \
    | sed -e 's/|/ /g' -e 's/^/GB|quota|/' \
    | head -n 20
fi

sinfo -h -o 'GB|partition|%P|%a|%l|%D|%C|%G'
""".strip()


class SlurmProviderError(RuntimeError):
    def __init__(self, message: str, *, access_required: bool = False, uncertain: bool = False) -> None:
        super().__init__(message)
        self.access_required = access_required
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class SlurmSubmission:
    scheduler_job_id: str
    raw_state: str = "SUBMITTED"


class SlurmProvider(Protocol):
    def access_status(self, connection: dict[str, Any]) -> dict[str, Any]: ...

    def find_by_name(
        self, connection: dict[str, Any], job_name: str
    ) -> SlurmSubmission | None: ...

    def submit(
        self,
        connection: dict[str, Any],
        *,
        broker_job_id: str,
        request: dict[str, Any],
        script_body: str,
    ) -> SlurmSubmission: ...

    def query(
        self, connection: dict[str, Any], scheduler_job_id: str
    ) -> dict[str, Any]: ...

    def cancel(self, connection: dict[str, Any], scheduler_job_id: str) -> None: ...

    def upload(
        self,
        connection: dict[str, Any],
        *,
        local_path: Path,
        remote_directory: str,
        transfer_id: str,
    ) -> str: ...


def broker_job_name(broker_job_id: str) -> str:
    return f"gb-{broker_job_id[:24]}"


def broker_state(raw_state: str) -> str:
    normalized = raw_state.strip().upper().split("+", 1)[0].split()[0]
    if normalized in {
        "SUBMITTED",
        "PENDING",
        "CONFIGURING",
        "REQUEUED",
        "RESIZING",
        "CANCEL_REQUESTED",
    }:
        return "PENDING"
    if normalized in {"RUNNING", "COMPLETING", "SIGNALING", "STAGE_OUT"}:
        return "RUNNING"
    if normalized == "COMPLETED":
        return "COMPLETED"
    if normalized in {"CANCELLED", "PREEMPTED", "REVOKED"}:
        return "CANCELLED"
    if normalized == "TIMEOUT":
        return "TIMEOUT"
    if normalized in TERMINAL_SLURM_STATES:
        return "FAILED"
    return "UNKNOWN"


def _slurm_time(seconds: int) -> str:
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    prefix = f"{days}-" if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _clean_output(value: str) -> str:
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    return value.replace("\r", "").strip()


class CommandSlurmProvider:
    """Execute fixed Slurm commands through a configured local login helper."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        *,
        timeout_seconds: int = 45,
        upload_timeout_seconds: int = 24 * 60 * 60,
    ) -> None:
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.upload_timeout_seconds = upload_timeout_seconds

    @staticmethod
    def _command_prefix(connection: dict[str, Any]) -> list[str]:
        prefix = connection.get("command_prefix")
        if (
            not isinstance(prefix, list)
            or not prefix
            or any(not isinstance(item, str) or not item or "\x00" in item for item in prefix)
        ):
            raise SlurmProviderError("scheduler target has an invalid command_prefix")
        return prefix

    def _run(
        self,
        connection: dict[str, Any],
        arguments: list[str],
        *,
        mutating: bool,
    ) -> str:
        remote_command = shlex.join(arguments)
        try:
            result = self.runner(
                [*self._command_prefix(connection), remote_command],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlurmProviderError(
                "scheduler mutation timed out; its remote outcome is unknown"
                if mutating
                else "scheduler access timed out; connect the approved VPN and retry",
                access_required=not mutating,
                uncertain=mutating,
            ) from exc
        except OSError as exc:
            raise SlurmProviderError(f"scheduler helper could not start: {type(exc).__name__}") from exc
        output = _clean_output("\n".join(part for part in (result.stdout, result.stderr) if part))
        if result.returncode != 0:
            access_required = result.returncode in {20, 21, 22, 23, 24, 25, 255} or any(
                marker in output.lower()
                for marker in (
                    "connection timed out",
                    "network is unreachable",
                    "no route to host",
                    "vpn",
                    "认证失败",
                    "验证",
                )
            )
            message = output[-1500:] if output else f"helper exited with code {result.returncode}"
            raise SlurmProviderError(
                message,
                access_required=access_required,
                uncertain=mutating and not access_required,
            )
        return output

    def access_status(self, connection: dict[str, Any]) -> dict[str, Any]:
        try:
            output = self._run(
                connection,
                ["bash", "-lc", SCHEDULER_INSPECTION_SCRIPT],
                mutating=False,
            )
        except SlurmProviderError as exc:
            return {
                "status": "access_required" if exc.access_required else "unavailable",
                "message": str(exc),
                "checked_at": datetime.now(UTC).isoformat(),
            }
        identity: dict[str, str] | None = None
        paths: list[dict[str, Any]] = []
        filesystem: dict[str, Any] | None = None
        quota_summary: list[str] = []
        partitions: list[dict[str, Any]] = []
        for line in output.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 2 or parts[0] != "GB":
                continue
            record_type = parts[1]
            if record_type == "identity" and len(parts) == 6:
                identity = {
                    "hostname": parts[2],
                    "user": parts[3],
                    "home": parts[4],
                    "pwd": parts[5],
                }
            elif record_type == "path" and len(parts) == 6:
                paths.append(
                    {
                        "label": parts[2],
                        "path": parts[3],
                        "kind": parts[4],
                        "writable": parts[5] == "true",
                    }
                )
            elif record_type == "filesystem" and len(parts) == 8:
                filesystem = {
                    "source": parts[2],
                    "total_kib": int(parts[3]),
                    "used_kib": int(parts[4]),
                    "available_kib": int(parts[5]),
                    "used_percent": parts[6],
                    "mount": parts[7],
                }
            elif record_type == "quota" and len(parts) >= 3:
                quota_summary.append("|".join(parts[2:]))
            elif record_type == "partition" and len(parts) == 8 and parts[2]:
                partitions.append(
                    {
                        "partition": parts[2].rstrip("*"),
                        "default": parts[2].endswith("*"),
                        "availability": parts[3],
                        "time_limit": parts[4],
                        "node_count": int(parts[5]),
                        "cpus": parts[6],
                        "gres": parts[7],
                    }
                )
        return {
            "status": "ready",
            "identity": identity,
            "paths": paths,
            "filesystem": filesystem,
            "quota_summary": quota_summary,
            "partitions": partitions,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def find_by_name(
        self, connection: dict[str, Any], job_name: str
    ) -> SlurmSubmission | None:
        output = self._run(
            connection,
            ["squeue", "-h", "-n", job_name, "-o", "%i|%T"],
            mutating=False,
        )
        for line in output.splitlines():
            parts = line.strip().split("|", 1)
            if parts and parts[0].isdigit():
                return SlurmSubmission(
                    scheduler_job_id=parts[0],
                    raw_state=parts[1] if len(parts) > 1 else "UNKNOWN",
                )
        # A mutation can time out after Slurm accepts it.  squeue covers only
        # active jobs, so a recovery check must also inspect sacct history
        # before the broker can conclude that a submission is still unknown.
        history = self._run(
            connection,
            [
                "sacct",
                "-X",
                "-n",
                "-P",
                f"--name={job_name}",
                "--format=JobIDRaw,State",
            ],
            mutating=False,
        )
        for line in history.splitlines():
            parts = line.strip().split("|", 1)
            if parts and parts[0].isdigit():
                return SlurmSubmission(
                    scheduler_job_id=parts[0],
                    raw_state=parts[1] if len(parts) > 1 else "UNKNOWN",
                )
        return None

    def submit(
        self,
        connection: dict[str, Any],
        *,
        broker_job_id: str,
        request: dict[str, Any],
        script_body: str,
    ) -> SlurmSubmission:
        constraints = request["constraints"]
        scheduler = request["scheduler"]
        gpu_count = int(constraints["gpu_count"])
        gpu_type = scheduler.get("gpu_type")
        encoded_script = base64.b64encode(script_body.encode("utf-8")).decode("ascii")
        wrapped = f"printf %s {shlex.quote(encoded_script)} | base64 -d | /bin/bash"
        arguments = [
            "sbatch",
            "--parsable",
            f"--job-name={broker_job_name(broker_job_id)}",
            f"--comment=gpu-broker:{broker_job_id}",
            f"--partition={scheduler['partition']}",
            f"--nodes={scheduler['nodes']}",
            f"--ntasks-per-node={scheduler['tasks_per_node']}",
            f"--cpus-per-task={scheduler['cpu_cores']}",
            f"--mem={scheduler['memory_mib']}M",
            f"--time={_slurm_time(int(request['duration_seconds']))}",
            f"--chdir={scheduler['working_directory']}",
            f"--output={scheduler['stdout_pattern']}",
            f"--error={scheduler['stderr_pattern']}",
            f"--wrap={wrapped}",
        ]
        if scheduler.get("qos"):
            arguments.insert(5, f"--qos={scheduler['qos']}")
        if gpu_count:
            gres = f"gpu:{gpu_type}:{gpu_count}" if gpu_type else f"gpu:{gpu_count}"
            arguments.insert(6, f"--gres={gres}")
        output = self._run(connection, arguments, mutating=True)
        match = re.search(r"(?m)^\s*(\d+)(?:;[^\s]+)?\s*$", output)
        if match is None:
            raise SlurmProviderError(
                "sbatch succeeded but did not return a parsable Slurm Job ID",
                uncertain=True,
            )
        return SlurmSubmission(scheduler_job_id=match.group(1))

    def query(
        self, connection: dict[str, Any], scheduler_job_id: str
    ) -> dict[str, Any]:
        if not scheduler_job_id.isdigit():
            raise SlurmProviderError("stored Slurm Job ID is invalid")
        output = self._run(
            connection,
            [
                "sacct",
                "-X",
                "-n",
                "-P",
                "-j",
                scheduler_job_id,
                "--format=JobIDRaw,State,ElapsedRaw,AllocTRES,ExitCode,NodeList,Start,End",
            ],
            mutating=False,
        )
        selected: list[str] | None = None
        for line in output.splitlines():
            parts = line.strip().split("|")
            if parts and parts[0] == scheduler_job_id:
                selected = parts
                break
        if selected is None:
            queue = self._run(
                connection,
                ["squeue", "-h", "-j", scheduler_job_id, "-o", "%i|%T|%M|%b|%N|%S"],
                mutating=False,
            )
            for line in queue.splitlines():
                parts = line.strip().split("|")
                if parts and parts[0] == scheduler_job_id:
                    raw_state = parts[1]
                    return {
                        "state": broker_state(raw_state),
                        "raw_state": raw_state,
                        "elapsed_seconds": None,
                        "allocated_tres": {"gres": parts[3]} if len(parts) > 3 else {},
                        "exit_code": None,
                        "node_list": parts[4] if len(parts) > 4 else None,
                        "started_at": parts[5] if len(parts) > 5 else None,
                        "completed_at": None,
                    }
            raise SlurmProviderError("Slurm no longer reports the requested job")
        selected += [""] * (8 - len(selected))
        raw_state = selected[1]
        allocated_tres = {
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for item in selected[3].split(",")
            if "=" in item
        }
        elapsed_seconds = int(selected[2]) if selected[2].isdigit() else None
        return {
            "state": broker_state(raw_state),
            "raw_state": raw_state,
            "elapsed_seconds": elapsed_seconds,
            "allocated_tres": allocated_tres,
            "exit_code": selected[4] or None,
            "node_list": selected[5] or None,
            "started_at": selected[6] or None,
            "completed_at": selected[7] or None,
        }

    def cancel(self, connection: dict[str, Any], scheduler_job_id: str) -> None:
        if not scheduler_job_id.isdigit():
            raise SlurmProviderError("stored Slurm Job ID is invalid")
        self._run(connection, ["scancel", scheduler_job_id], mutating=True)

    def upload(
        self,
        connection: dict[str, Any],
        *,
        local_path: Path,
        remote_directory: str,
        transfer_id: str,
    ) -> str:
        upload = connection.get("upload")
        if not isinstance(upload, dict):
            raise SlurmProviderError(
                "scheduler target has no staged upload configuration"
            )
        basename = local_path.name
        if not re.fullmatch(r"[A-Za-z0-9._@+-]{1,255}", basename):
            raise SlurmProviderError(
                "local source basename must use letters, numbers, '.', '_', '@', '+' or '-'"
            )
        remote_stage = (
            remote_directory.rstrip("/") + f"/gpu-broker-{transfer_id}"
        )
        self._run(
            connection,
            ["mkdir", "-p", "-m", "700", "--", remote_stage],
            mutating=True,
        )
        host = upload.get("ssh_host")
        username = upload.get("ssh_user")
        port = upload.get("ssh_port")
        control_path = upload.get("control_path")
        if (
            not isinstance(host, str)
            or not isinstance(username, str)
            or not isinstance(port, int)
            or not isinstance(control_path, str)
        ):
            raise SlurmProviderError("scheduler staged upload metadata is invalid")
        command = [
            "/usr/bin/scp",
            "-q",
            "-P",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=10m",
            "-o",
            f"ControlPath={control_path}",
        ]
        if local_path.is_dir():
            command.append("-r")
        command.extend(
            [
                str(local_path),
                f"{username}@{host}:{remote_stage}/",
            ]
        )
        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.upload_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlurmProviderError(
                "staged upload timed out",
                uncertain=True,
            ) from exc
        except OSError as exc:
            raise SlurmProviderError(
                f"scp could not start: {type(exc).__name__}"
            ) from exc
        output = _clean_output(
            "\n".join(part for part in (result.stdout, result.stderr) if part)
        )
        if result.returncode != 0:
            access_required = result.returncode == 255
            raise SlurmProviderError(
                output[-1500:] or f"scp exited with code {result.returncode}",
                access_required=access_required,
                uncertain=not access_required,
            )
        return f"{remote_stage}/{basename}"
