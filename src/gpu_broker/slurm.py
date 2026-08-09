"""Bounded Slurm command adapter for external scheduler targets.

Each target selects a sealed transport and a sealed, read-only inspection
profile.  The local deployment resolves the transport helper; target data never
contains an executable, argv, SSH option, or secret.
"""

from __future__ import annotations

import base64
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol

from gpu_broker.adapters import AdapterCommandError, SlurmCommandSchedulerAdapter, scheduler_adapter


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

_SCHEDULER_BASIC_INSPECTION_SCRIPT = r"""
set -eu

printf 'GB|identity|%s|%s|%s|%s\n' \
  "$(hostname -f 2>/dev/null || hostname)" "$(id -un)" "$HOME" "$PWD"
if [ -e "$HOME" ]; then
  writable=false
  [ -w "$HOME" ] && writable=true
  printf 'GB|path|home|%s|directory|%s\n' "$HOME" "$writable"
fi
df -Pk "$HOME" | awk \
  'NR == 2 { printf "GB|filesystem|%s|%s|%s|%s|%s|%s\n", $1, $2, $3, $4, $5, $6 }'
if command -v quota >/dev/null 2>&1; then
  (quota -s 2>&1 || true) | sed -e 's/|/ /g' -e 's/^/GB|quota|/' | head -n 20
fi
""".strip()

_SCHEDULER_CAPACITY_INSPECTION_SCRIPT = (
    _SCHEDULER_BASIC_INSPECTION_SCRIPT
    + "\n"
    + r"""
sinfo -h -o 'GB|partition|%P|%a|%l|%D|%C|%G'
""".strip()
)

SchedulerInspectionProfile = Literal["slurm-basic", "slurm-capacity"]
SCHEDULER_INSPECTION_SCRIPTS: Mapping[SchedulerInspectionProfile, str] = MappingProxyType(
    {
        "slurm-basic": _SCHEDULER_BASIC_INSPECTION_SCRIPT,
        "slurm-capacity": _SCHEDULER_CAPACITY_INSPECTION_SCRIPT,
    }
)


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


def _scheduler_submit_script(
    arguments: list[str],
    *,
    job_name: str,
    comment: str,
    script_body: str,
) -> str:
    submit_command = shlex.join(arguments)
    encoded_script = base64.b64encode(script_body.encode("utf-8")).decode("ascii")
    submit_pipeline = (
        f"printf %s {shlex.quote(encoded_script)} | base64 -d | {submit_command}"
    )
    quoted_job_name = shlex.quote(job_name)
    quoted_comment = shlex.quote(comment)
    return f"""\
set -uo pipefail
job_name={quoted_job_name}
expected_comment={quoted_comment}

clean_output() {{
  LC_ALL=C sed -E $'s/\\x1B\\[[0-9;?]*[ -\\/]*[@-~]//g' \\
    | LC_ALL=C tr -d '\\000-\\010\\013\\014\\016-\\037\\177'
}}

extract_submit_ids() {{
  clean_output | awk '
    /^[[:space:]]*[0-9]+(;[^[:space:]]+)?[[:space:]]*$/ {{
      line=$0
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      sub(/;.*/, "", line)
      print line
      next
    }}
    /^[[:space:]]*Submitted batch job[[:space:]]+[0-9]+[[:space:]]*$/ {{
      line=$0
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      sub(/^Submitted batch job[[:space:]]+/, "", line)
      print line
    }}
  '
}}

count_ids() {{
  awk 'NF {{ count++ }} END {{ print count + 0 }}'
}}

emit_shape() {{
  shape_output=$1
  raw_lines=$(printf '%s' "$shape_output" | awk 'NR {{ lines=NR }} END {{ print lines + 0 }}')
  raw_bytes=$(printf '%s' "$shape_output" | LC_ALL=C wc -c | tr -d '[:space:]')
  cleaned_shape=$(printf '%s' "$shape_output" | clean_output)
  clean_lines=$(printf '%s' "$cleaned_shape" | awk 'NR {{ lines=NR }} END {{ print lines + 0 }}')
  clean_bytes=$(printf '%s' "$cleaned_shape" | LC_ALL=C wc -c | tr -d '[:space:]')
  non_ascii_bytes=$(printf '%s' "$cleaned_shape" | LC_ALL=C tr -cd '\\200-\\377' | wc -c | tr -d '[:space:]')
  ascii_bytes=$((clean_bytes - non_ascii_bytes))
  if [ "$non_ascii_bytes" -eq 0 ]; then ascii_only=1; else ascii_only=0; fi
  clean_nonspace=$(printf '%s' "$cleaned_shape" | awk '
    /[^[:space:]]/ {{ found=1 }}
    END {{ print found + 0 }}
  ')
  numeric_shape=$(printf '%s' "$cleaned_shape" | awk '
    {{
      remaining=$0
      while (match(remaining, /[0-9]+/)) {{
        digits=RLENGTH
        runs++
        if (!min_digits || digits < min_digits) min_digits=digits
        if (digits > max_digits) max_digits=digits
        candidate=substr(remaining, RSTART, RLENGTH)
        if (digits <= 10 && candidate !~ /^0/) jobid_runs++
        remaining=substr(remaining, RSTART + RLENGTH)
      }}
    }}
    END {{
      printf "numeric_runs=%d|min_digits=%d|max_digits=%d|jobid_runs=%d", \
        runs + 0, min_digits + 0, max_digits + 0, jobid_runs + 0
    }}
  ')
  standard_ids=$(printf '%s\n' "$cleaned_shape" | extract_submit_ids | count_ids)
  lowered_shape=$(printf '%s' "$cleaned_shape" | LC_ALL=C tr '[:upper:]' '[:lower:]')
  case "$shape_output" in *';'*) has_semicolon=1 ;; *) has_semicolon=0 ;; esac
  case "$shape_output" in *'|'*) has_pipe=1 ;; *) has_pipe=0 ;; esac
  case "$shape_output" in *':'*) has_colon=1 ;; *) has_colon=0 ;; esac
  case "$shape_output" in *'='*) has_equals=1 ;; *) has_equals=0 ;; esac
  case "$lowered_shape" in *submitted*) kw_submitted=1 ;; *) kw_submitted=0 ;; esac
  case "$lowered_shape" in *batch*) kw_batch=1 ;; *) kw_batch=0 ;; esac
  case "$lowered_shape" in *job*) kw_job=1 ;; *) kw_job=0 ;; esac
  case "$lowered_shape" in *warning*) kw_warning=1 ;; *) kw_warning=0 ;; esac
  case "$lowered_shape" in *error*) kw_error=1 ;; *) kw_error=0 ;; esac
  case "$lowered_shape" in *policy*) kw_policy=1 ;; *) kw_policy=0 ;; esac
  case "$lowered_shape" in *denied*) kw_denied=1 ;; *) kw_denied=0 ;; esac
  case "$lowered_shape" in *invalid*) kw_invalid=1 ;; *) kw_invalid=0 ;; esac
  case "$lowered_shape" in *unrecognized*) kw_unrecognized=1 ;; *) kw_unrecognized=0 ;; esac
  case "$lowered_shape" in *option*) kw_option=1 ;; *) kw_option=0 ;; esac
  case "$lowered_shape" in *comment*) kw_comment=1 ;; *) kw_comment=0 ;; esac
  case "$lowered_shape" in *parsable*) kw_parsable=1 ;; *) kw_parsable=0 ;; esac
  case "$lowered_shape" in *wrap*) kw_wrap=1 ;; *) kw_wrap=0 ;; esac
  case "$lowered_shape" in *account*) kw_account=1 ;; *) kw_account=0 ;; esac
  case "$lowered_shape" in *partition*) kw_partition=1 ;; *) kw_partition=0 ;; esac
  case "$lowered_shape" in *qos*) kw_qos=1 ;; *) kw_qos=0 ;; esac
  case "$lowered_shape" in *node*) kw_node=1 ;; *) kw_node=0 ;; esac
  case "$lowered_shape" in *cpu*) kw_cpu=1 ;; *) kw_cpu=0 ;; esac
  case "$lowered_shape" in *memory*) kw_memory=1 ;; *) kw_memory=0 ;; esac
  case "$lowered_shape" in *time*) kw_time=1 ;; *) kw_time=0 ;; esac
  case "$lowered_shape" in *chdir*) kw_chdir=1 ;; *) kw_chdir=0 ;; esac
  case "$lowered_shape" in *output*) kw_output=1 ;; *) kw_output=0 ;; esac
  case "$lowered_shape" in *reservation*) kw_reservation=1 ;; *) kw_reservation=0 ;; esac
  case "$lowered_shape" in *association*) kw_association=1 ;; *) kw_association=0 ;; esac
  case "$lowered_shape" in *group*) kw_group=1 ;; *) kw_group=0 ;; esac
  case "$lowered_shape" in *user*) kw_user=1 ;; *) kw_user=0 ;; esac
  case "$lowered_shape" in *limit*) kw_limit=1 ;; *) kw_limit=0 ;; esac
  case "$lowered_shape" in *permission*) kw_permission=1 ;; *) kw_permission=0 ;; esac
  case "$lowered_shape" in *available*) kw_available=1 ;; *) kw_available=0 ;; esac
  case "$lowered_shape" in *configuration*) kw_configuration=1 ;; *) kw_configuration=0 ;; esac
  case "$lowered_shape" in *contact*) kw_contact=1 ;; *) kw_contact=0 ;; esac
  case "$lowered_shape" in *controller*) kw_controller=1 ;; *) kw_controller=0 ;; esac
  case "$lowered_shape" in *submit*) kw_submit=1 ;; *) kw_submit=0 ;; esac
  case "$lowered_shape" in *fail*) kw_fail=1 ;; *) kw_fail=0 ;; esac
  case "$lowered_shape" in *'not permitted'*) kw_not_permitted=1 ;; *) kw_not_permitted=0 ;; esac
  case "$lowered_shape" in *system*) kw_system=1 ;; *) kw_system=0 ;; esac
  case "$lowered_shape" in *submissions*) kw_submissions=1 ;; *) kw_submissions=0 ;; esac
  case "$lowered_shape" in *disabled*) kw_disabled=1 ;; *) kw_disabled=0 ;; esac
  case "$lowered_shape" in *unexpected*) kw_unexpected=1 ;; *) kw_unexpected=0 ;; esac
  case "$lowered_shape" in *message*) kw_message=1 ;; *) kw_message=0 ;; esac
  case "$lowered_shape" in *received*) kw_received=1 ;; *) kw_received=0 ;; esac
  case "$lowered_shape" in *plugin*) kw_plugin=1 ;; *) kw_plugin=0 ;; esac
  case "$lowered_shape" in *filter*) kw_filter=1 ;; *) kw_filter=0 ;; esac
  if [ "$raw_bytes" -ne "$clean_bytes" ]; then clean_changed=1; else clean_changed=0; fi
  printf 'GB|scheduler-submit-shape|raw_lines=%s|raw_bytes=%s|clean_lines=%s|clean_bytes=%s|ascii_bytes=%s|non_ascii_bytes=%s|ascii_only=%s|clean_nonspace=%s|clean_changed=%s|standard_ids=%s|%s|semicolon=%s|pipe=%s|colon=%s|equals=%s|kw_submitted=%s|kw_batch=%s|kw_job=%s|kw_warning=%s|kw_error=%s|kw_policy=%s|kw_denied=%s|kw_invalid=%s|kw_unrecognized=%s|kw_option=%s|kw_comment=%s|kw_parsable=%s|kw_wrap=%s|kw_account=%s|kw_partition=%s|kw_qos=%s|kw_node=%s|kw_cpu=%s|kw_memory=%s|kw_time=%s|kw_chdir=%s|kw_output=%s|kw_reservation=%s|kw_association=%s|kw_group=%s|kw_user=%s|kw_limit=%s|kw_permission=%s|kw_available=%s|kw_configuration=%s|kw_contact=%s|kw_controller=%s|kw_submit=%s|kw_fail=%s|kw_not_permitted=%s|kw_system=%s|kw_submissions=%s|kw_disabled=%s|kw_unexpected=%s|kw_message=%s|kw_received=%s|kw_plugin=%s|kw_filter=%s\n' \
    "$raw_lines" "$raw_bytes" "$clean_lines" "$clean_bytes" \
    "$ascii_bytes" "$non_ascii_bytes" "$ascii_only" "$clean_nonspace" \
    "$clean_changed" "$standard_ids" "$numeric_shape" \
    "$has_semicolon" "$has_pipe" "$has_colon" "$has_equals" \
    "$kw_submitted" "$kw_batch" "$kw_job" "$kw_warning" "$kw_error" \
    "$kw_policy" "$kw_denied" "$kw_invalid" "$kw_unrecognized" \
    "$kw_option" "$kw_comment" "$kw_parsable" "$kw_wrap" "$kw_account" \
    "$kw_partition" "$kw_qos" "$kw_node" "$kw_cpu" "$kw_memory" \
    "$kw_time" "$kw_chdir" "$kw_output" "$kw_reservation" \
    "$kw_association" "$kw_group" "$kw_user" "$kw_limit" \
    "$kw_permission" "$kw_available" "$kw_configuration" "$kw_contact" \
    "$kw_controller" "$kw_submit" "$kw_fail" "$kw_not_permitted" \
    "$kw_system" "$kw_submissions" "$kw_disabled" "$kw_unexpected" \
    "$kw_message" "$kw_received" "$kw_plugin" "$kw_filter" >&2
}}

emit_failure() {{
  failure_class=$1
  failure_status=$2
  recovery=$3
  failure_output=$4
  cleaned_failure=$(printf '%s' "$failure_output" | clean_output)
  failure_lines=$(printf '%s' "$cleaned_failure" | awk 'NF || NR {{ lines=NR }} END {{ print lines + 0 }}')
  failure_bytes=$(printf '%s' "$cleaned_failure" | LC_ALL=C wc -c | tr -d '[:space:]')
  failure_digest=$(printf '%s' "$cleaned_failure" | LC_ALL=C cksum | awk '{{ print $1 }}')
  printf 'GB|scheduler-submit-error|class=%s|exit=%s|lines=%s|bytes=%s|digest=cksum:%s|recovery=%s\n' \
    "$failure_class" "$failure_status" "$failure_lines" "$failure_bytes" \
    "$failure_digest" "$recovery" >&2
  emit_shape "$failure_output"
}}

classify_submit_failure() {{
  failure_status=$1
  failure_output=$2
  if [ "$failure_status" -eq 127 ]; then
    printf 'command-not-found\n'
    return
  fi
  lowered=$(printf '%s' "$failure_output" | clean_output | LC_ALL=C tr '[:upper:]' '[:lower:]')
  case "$lowered" in
    *'command not found'*) printf 'command-not-found\n' ;;
    *'unrecognized option'*|*'unknown option'*|*'invalid option'*)
      printf 'unsupported-option\n'
      ;;
    *) printf 'slurm-error\n' ;;
  esac
}}

classify_zero_status_no_id() {{
  zero_status_output=$1
  lowered=$(printf '%s' "$zero_status_output" | clean_output | LC_ALL=C tr '[:upper:]' '[:lower:]')
  case "$lowered" in
    *error*) printf 'scheduler-error-output\n' ;;
    *) printf 'no-id-after-lookup\n' ;;
  esac
}}

set +e
submit_output=$({{ {submit_pipeline}; }} 2>&1)
submit_status=$?
set -e
if [ "$submit_status" -ne 0 ]; then
  failure_class=$(classify_submit_failure "$submit_status" "$submit_output")
  emit_failure "$failure_class" "$submit_status" not-run "$submit_output"
  exit "$submit_status"
fi

submit_ids=$(printf '%s\\n' "$submit_output" | extract_submit_ids | LC_ALL=C sort -u)
submit_count=$(printf '%s\\n' "$submit_ids" | count_ids)
if [ "$submit_count" -gt 1 ]; then
  emit_failure ambiguous-id 86 not-run "$submit_output"
  exit 86
fi
if [ "$submit_count" -eq 1 ]; then
  printf 'GB|scheduler-submit|%s\\n' "$submit_ids"
  exit 0
fi

lookup_ids() {{
  {{
    squeue -h -n "$job_name" -o '%i|%k' 2>/dev/null || true
    sacct -X -n -P --name="$job_name" --format=JobIDRaw,Comment 2>/dev/null || true
  }} | clean_output | awk -F '|' -v expected="$expected_comment" '
    {{
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
    }}
    $1 ~ /^[0-9]+$/ && $2 == expected {{ print $1 }}
  ' | LC_ALL=C sort -u
}}

for attempt in 1 2 3; do
  recovered_ids=$(lookup_ids)
  recovered_count=$(printf '%s\\n' "$recovered_ids" | count_ids)
  if [ "$recovered_count" -gt 1 ]; then
    emit_failure ambiguous-recovery 86 ambiguous "$recovered_ids"
    exit 86
  fi
  if [ "$recovered_count" -eq 1 ]; then
    printf 'GB|scheduler-submit|%s\\n' "$recovered_ids"
    exit 0
  fi
  if [ "$attempt" -lt 3 ]; then
    sleep 1
  fi
done
failure_class=$(classify_zero_status_no_id "$submit_output")
emit_failure "$failure_class" 87 none "$submit_output"
exit 87
"""


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
        self.adapter: SlurmCommandSchedulerAdapter = scheduler_adapter(runner=runner)

    def _run(
        self,
        connection: dict[str, Any],
        arguments: list[str],
        *,
        mutating: bool,
    ) -> str:
        try:
            return self.adapter.run(
                connection,
                arguments,
                mutating=mutating,
                timeout_seconds=self.timeout_seconds,
            )
        except AdapterCommandError as exc:
            raise SlurmProviderError(
                str(exc),
                access_required=exc.access_required,
                uncertain=exc.uncertain,
            ) from exc

    def access_status(self, connection: dict[str, Any]) -> dict[str, Any]:
        profile = connection.get("inspection_profile")
        try:
            script = SCHEDULER_INSPECTION_SCRIPTS[profile]
        except (KeyError, TypeError):
            return {
                "status": "unavailable",
                "message": "scheduler target has an unsupported inspection profile",
                "checked_at": datetime.now(UTC).isoformat(),
            }
        try:
            output = self._run(
                connection,
                ["bash", "-lc", script],
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
        result = {
            "status": "ready",
            "identity": identity,
            "paths": paths,
            "filesystem": filesystem,
            "quota_summary": quota_summary,
            "partitions": partitions,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        return result

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
        ]
        if scheduler.get("qos"):
            arguments.insert(5, f"--qos={scheduler['qos']}")
        if gpu_count:
            gres = f"gpu:{gpu_type}:{gpu_count}" if gpu_type else f"gpu:{gpu_count}"
            arguments.insert(6, f"--gres={gres}")
        job_name = broker_job_name(broker_job_id)
        submit_script = _scheduler_submit_script(
            arguments,
            job_name=job_name,
            comment=f"gpu-broker:{broker_job_id}",
            script_body=script_body,
        )
        encoded_submit_script = base64.b64encode(
            submit_script.encode("utf-8")
        ).decode("ascii")
        remote_wrapper = (
            f"printf %s {shlex.quote(encoded_submit_script)} | base64 -d | /bin/bash"
        )
        output = self._run(
            connection,
            ["bash", "-lc", remote_wrapper],
            mutating=True,
        )
        matches = re.findall(r"(?m)^GB\|scheduler-submit\|(\d+)$", output)
        if len(matches) != 1:
            raise SlurmProviderError(
                "sbatch succeeded but did not return a parsable Slurm Job ID",
                uncertain=True,
            )
        return SlurmSubmission(scheduler_job_id=matches[0])

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
            self.adapter.upload(command, upload_timeout_seconds=self.upload_timeout_seconds)
        except AdapterCommandError as exc:
            raise SlurmProviderError(
                str(exc),
                access_required=exc.access_required,
                uncertain=exc.uncertain,
            ) from exc
        return f"{remote_stage}/{basename}"
