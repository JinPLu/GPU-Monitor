"""Internal typed adapter registry for external GPU backends.

Adapters are intentionally sealed to the built-in identifiers in this module.
They do not receive BrokerService, database sessions, actors, or claim state.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from gpu_broker.config import EndpointConfig
from gpu_broker.collector_protocol import SERVER_SCRIPT_REMOTE_COMMAND


AdapterId = Literal["raw-ssh", "slurm-command"]
Capability = Literal["observation", "scheduler", "provisioning-preview", "operation"]
OperationId = Literal["scheduler.submit", "scheduler.cancel", "scheduler.upload"]
ParameterType = Literal["string", "path"]
RawSSHProbe = Literal["endpoint-telemetry", "process-details"]
ObservationProfile = Literal["linux-nvidia", "linux-host", "server-script-v1"]


GPU_QUERY = (
    "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
    "utilization.gpu,utilization.memory,temperature.gpu,power.draw,pstate "
    "--format=csv,noheader,nounits"
)
PROCESS_QUERY = (
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name "
    "--format=csv,noheader,nounits"
)
IDENTITY_QUERY = "hostname; cat /proc/sys/kernel/random/boot_id"
HOST_RESOURCES_QUERY = (
    "getconf _NPROCESSORS_ONLN; "
    "awk '/MemTotal:/{total=$2} /MemAvailable:/{available=$2} "
    "END {printf \"%d %d\\n\", total/1024, available/1024}' /proc/meminfo; "
    "cut -d ' ' -f1 /proc/loadavg; "
    "awk 'NR == 1 && $1 == \"cpu\" {idle=$5+$6; total=0; "
    "for (i=2; i<=NF; i++) total+=$i; printf \"%d %d\\n\", total, idle}' /proc/stat"
)
GPU_SECTION = "__GPU_BROKER_GPU__"
PROCESS_SECTION = "__GPU_BROKER_PROCESSES__"
IDENTITY_SECTION = "__GPU_BROKER_IDENTITY__"
HOST_RESOURCES_SECTION = "__GPU_BROKER_HOST_RESOURCES__"
GPU_UNAVAILABLE = "__GPU_BROKER_GPU_UNAVAILABLE__"
RAW_SSH_COMBINED_QUERY = (
    f"set -e; printf '{GPU_SECTION}\\n'; "
    f"if command -v nvidia-smi >/dev/null 2>&1; then "
    f"{GPU_QUERY} 2>/dev/null || printf '{GPU_UNAVAILABLE}\\n'; "
    f"else printf '{GPU_UNAVAILABLE}\\n'; fi; "
    f"printf '{PROCESS_SECTION}\\n'; "
    f"if command -v nvidia-smi >/dev/null 2>&1; then "
    f"{PROCESS_QUERY} 2>/dev/null || true; fi; "
    f"printf '{IDENTITY_SECTION}\\n'; {IDENTITY_QUERY}; "
    f"printf '{HOST_RESOURCES_SECTION}\\n'; {HOST_RESOURCES_QUERY}"
)
RAW_SSH_HOST_ONLY_QUERY = (
    f"set -e; printf '{GPU_SECTION}\\n{GPU_UNAVAILABLE}\\n'; "
    f"printf '{PROCESS_SECTION}\\n'; "
    f"printf '{IDENTITY_SECTION}\\n'; {IDENTITY_QUERY}; "
    f"printf '{HOST_RESOURCES_SECTION}\\n'; {HOST_RESOURCES_QUERY}"
)

_OBSERVATION_QUERIES: Mapping[ObservationProfile, str] = MappingProxyType(
    {
        "linux-nvidia": RAW_SSH_COMBINED_QUERY,
        "linux-host": RAW_SSH_HOST_ONLY_QUERY,
        # This is an immutable entry invocation, not an endpoint-configured
        # shell, path, argv, or local prefix.  The remote administrator may
        # maintain the command's local implementation behind this contract.
        "server-script-v1": SERVER_SCRIPT_REMOTE_COMMAND,
    }
)


class AdapterRegistryError(KeyError):
    """Raised when a requested sealed adapter id is not registered."""


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    required: Literal[True] = True
    field: Literal["approval_ref"] = "approval_ref"
    current_task_only: bool = True


@dataclass(frozen=True, slots=True)
class OperationParameter:
    name: str
    type: ParameterType
    required: bool = True


@dataclass(frozen=True, slots=True)
class OperationSpec:
    id: OperationId
    title: str
    parameters: tuple[OperationParameter, ...]
    approval: ApprovalRequirement
    executes: Literal[False] = False

    def schema(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "approval": {
                "required": self.approval.required,
                "field": self.approval.field,
                "current_task_only": self.approval.current_task_only,
            },
            "executes": self.executes,
            "parameters": [
                {
                    "name": parameter.name,
                    "type": parameter.type,
                    "required": parameter.required,
                }
                for parameter in self.parameters
            ],
        }


@dataclass(frozen=True, slots=True)
class ProvisioningRecipePreview:
    id: str
    title: str
    summary: str
    executable: Literal[False] = False


@dataclass(frozen=True, slots=True)
class AdapterDefinition:
    id: AdapterId
    capabilities: frozenset[Capability]
    operations: tuple[OperationSpec, ...] = ()
    provisioning_previews: tuple[ProvisioningRecipePreview, ...] = ()

    def operation_schema(self) -> tuple[dict[str, Any], ...]:
        return tuple(operation.schema() for operation in self.operations)


class AdapterRegistry:
    def __init__(self, definitions: tuple[AdapterDefinition, ...]) -> None:
        by_id = {definition.id: definition for definition in definitions}
        if len(by_id) != len(definitions):
            raise ValueError("adapter definitions must use unique ids")
        self._definitions = MappingProxyType(by_id)

    def get(self, adapter_id: str) -> AdapterDefinition:
        try:
            return self._definitions[adapter_id]  # type: ignore[index]
        except KeyError as exc:
            raise AdapterRegistryError(f"unknown adapter: {adapter_id}") from exc

    def require_capability(self, adapter_id: str, capability: Capability) -> AdapterDefinition:
        definition = self.get(adapter_id)
        if capability not in definition.capabilities:
            raise AdapterRegistryError(f"adapter {adapter_id} does not provide {capability}")
        return definition

    def ids(self) -> tuple[AdapterId, ...]:
        return tuple(self._definitions)


@dataclass(frozen=True, slots=True)
class RawSSHResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


MAX_RAW_SSH_STDOUT_BYTES = 1_048_576
MAX_RAW_SSH_STDERR_BYTES = 16_384


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    *,
    maximum_bytes: int,
) -> tuple[bytes, bool]:
    """Drain a process stream while retaining only a bounded prefix.

    Continuing to drain after the limit prevents a noisy remote process from
    deadlocking on a full SSH pipe, while retaining no unbounded output in the
    broker process.
    """

    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await stream.read(65_536):
        remaining = maximum_bytes - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


def _decode_remote_output(value: bytes, *, stream_name: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SSH {stream_name} is not valid UTF-8") from exc


class RawSSHObservationAdapter:
    id: AdapterId = "raw-ssh"

    async def run_probe(
        self,
        endpoint: EndpointConfig,
        *,
        probe: RawSSHProbe,
        connect_timeout_seconds: int,
        process_ids: tuple[int, ...] = (),
    ) -> RawSSHResult:
        if probe == "endpoint-telemetry":
            try:
                remote_command = _OBSERVATION_QUERIES[endpoint.observation_profile]
            except KeyError as exc:  # defensive for pre-migration/corrupt rows
                raise ValueError(
                    f"unknown endpoint observation profile: {endpoint.observation_profile}"
                ) from exc
        elif probe == "process-details":
            if not process_ids or any(type(pid) is not int or pid <= 0 for pid in process_ids):
                raise ValueError("process-details probe requires positive integer process ids")
            remote_command = "ps -o pid=,user=,etimes=,comm= -p " + ",".join(
                str(pid) for pid in sorted(set(process_ids))
            )
        else:
            raise ValueError(f"unknown raw SSH probe: {probe}")
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={connect_timeout_seconds}",
            "-p",
            str(endpoint.port),
            f"{endpoint.ssh_user}@{endpoint.host}",
            remote_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if getattr(process, "stdout", None) is None or getattr(process, "stderr", None) is None:
            # Compatibility path for a deliberately minimal fake process. The
            # production subprocess always supplies pipes, which take the
            # bounded streaming path below.
            stdout, stderr = await process.communicate()
            stdout_truncated = len(stdout) > MAX_RAW_SSH_STDOUT_BYTES
            stderr_truncated = len(stderr) > MAX_RAW_SSH_STDERR_BYTES
            stdout = stdout[:MAX_RAW_SSH_STDOUT_BYTES]
            stderr = stderr[:MAX_RAW_SSH_STDERR_BYTES]
        else:
            (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
                _read_bounded_stream(process.stdout, maximum_bytes=MAX_RAW_SSH_STDOUT_BYTES),
                _read_bounded_stream(process.stderr, maximum_bytes=MAX_RAW_SSH_STDERR_BYTES),
            )
            await process.wait()
        return RawSSHResult(
            returncode=process.returncode or 0,
            stdout=_decode_remote_output(stdout, stream_name="stdout"),
            stderr=_decode_remote_output(stderr, stream_name="stderr"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


class AdapterCommandError(RuntimeError):
    def __init__(self, message: str, *, access_required: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.access_required = access_required
        self.uncertain = uncertain


def _clean_output(value: str) -> str:
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    return value.replace("\r", "").strip()


class SlurmCommandSchedulerAdapter:
    id: AdapterId = "slurm-command"

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runner = runner

    @staticmethod
    def transport_prefix(connection: dict[str, Any]) -> list[str]:
        """Resolve a sealed scheduler transport outside target configuration.

        A target can choose only a profile ID. The profile-to-wrapper mapping
        comes from the local deployment's environment; each wrapper must be an
        absolute, zero-argument program that accepts the broker's final remote
        command. This keeps a cooperative API payload from becoming argv.
        """

        profile = connection.get("transport_profile")
        if not isinstance(profile, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", profile):
            raise AdapterCommandError("scheduler target has an invalid transport profile")

        raw_profiles = os.environ.get("SERVERPILOT_SCHEDULER_TRANSPORTS") or os.environ.get(
            "GPU_BROKER_SCHEDULER_TRANSPORTS"
        )
        if raw_profiles:
            try:
                profiles = json.loads(raw_profiles)
            except json.JSONDecodeError as exc:
                raise AdapterCommandError("scheduler transport mapping is invalid JSON") from exc
            if not isinstance(profiles, dict):
                raise AdapterCommandError("scheduler transport mapping must be an object")
        else:
            legacy_helper = os.environ.get("SERVERPILOT_SCHEDULER_HELPER") or os.environ.get(
                "GPU_BROKER_SCHEDULER_HELPER"
            )
            profiles = {"default": legacy_helper} if legacy_helper else {}

        helper = profiles.get(profile)
        if not isinstance(helper, str):
            raise AdapterCommandError(
                f"scheduler transport profile {profile!r} is not configured locally"
            )
        if not os.path.isabs(helper) or any(character in helper for character in ("\x00", "\n", "\r")):
            raise AdapterCommandError("scheduler transport helper must be an absolute single executable path")
        return [helper]

    def run(
        self,
        connection: dict[str, Any],
        arguments: list[str],
        *,
        mutating: bool,
        timeout_seconds: int,
    ) -> str:
        remote_command = shlex.join(arguments)
        try:
            result = self.runner(
                [*self.transport_prefix(connection), remote_command],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterCommandError(
                "scheduler mutation timed out; its remote outcome is unknown"
                if mutating
                else "scheduler access timed out; connect the approved VPN and retry",
                access_required=not mutating,
                uncertain=mutating,
            ) from exc
        except OSError as exc:
            raise AdapterCommandError(f"scheduler helper could not start: {type(exc).__name__}") from exc
        output = _clean_output("\n".join(part for part in (result.stdout, result.stderr) if part))
        if result.returncode != 0:
            access_required = result.returncode in {20, 21, 22, 23, 24, 25, 255} or any(
                marker in output.lower()
                for marker in (
                    "connection timed out",
                    "network is unreachable",
                    "no route to host",
                    "vpn disconnected",
                    "vpn is disconnected",
                    "vpn required",
                    "connect the approved vpn",
                    "认证失败",
                    "验证",
                )
            )
            message = output[-1500:] if output else f"helper exited with code {result.returncode}"
            raise AdapterCommandError(
                message,
                access_required=access_required,
                uncertain=mutating and not access_required,
            )
        return output

    def upload(
        self,
        command: list[str],
        *,
        upload_timeout_seconds: int,
    ) -> str:
        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=upload_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterCommandError("staged upload timed out", uncertain=True) from exc
        except OSError as exc:
            raise AdapterCommandError(f"scp could not start: {type(exc).__name__}") from exc
        output = _clean_output("\n".join(part for part in (result.stdout, result.stderr) if part))
        if result.returncode != 0:
            access_required = result.returncode == 255
            raise AdapterCommandError(
                output[-1500:] or f"scp exited with code {result.returncode}",
                access_required=access_required,
                uncertain=not access_required,
            )
        return output


_APPROVAL = ApprovalRequirement()
_ADAPTER_DEFINITIONS = (
    AdapterDefinition(id="raw-ssh", capabilities=frozenset({"observation"})),
    AdapterDefinition(
        id="slurm-command",
        capabilities=frozenset({"scheduler", "provisioning-preview", "operation"}),
        operations=(
            OperationSpec(
                id="scheduler.submit",
                title="Submit approved scheduler workload",
                parameters=(
                    OperationParameter("scheduler_target_id", "string"),
                    OperationParameter("workload_profile_id", "string"),
                    OperationParameter("approval_ref", "string"),
                ),
                approval=_APPROVAL,
            ),
            OperationSpec(
                id="scheduler.cancel",
                title="Cancel approved scheduler workload",
                parameters=(
                    OperationParameter("scheduler_target_id", "string"),
                    OperationParameter("scheduler_job_id", "string"),
                    OperationParameter("approval_ref", "string"),
                ),
                approval=_APPROVAL,
            ),
            OperationSpec(
                id="scheduler.upload",
                title="Upload approved scheduler payload",
                parameters=(
                    OperationParameter("scheduler_target_id", "string"),
                    OperationParameter("local_path", "path"),
                    OperationParameter("remote_directory", "path"),
                    OperationParameter("approval_ref", "string"),
                ),
                approval=_APPROVAL,
            ),
        ),
        provisioning_previews=(
            ProvisioningRecipePreview(
                id="slurm-one-off-preview",
                title="Preview Slurm one-off workload recipe",
                summary="Builds a non-executable preview from an approved scheduler profile.",
            ),
        ),
    ),
)

ADAPTER_REGISTRY = AdapterRegistry(_ADAPTER_DEFINITIONS)
RAW_SSH_OBSERVATION_ADAPTER = RawSSHObservationAdapter()


def scheduler_adapter(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SlurmCommandSchedulerAdapter:
    ADAPTER_REGISTRY.require_capability("slurm-command", "scheduler")
    return SlurmCommandSchedulerAdapter(runner=runner)
