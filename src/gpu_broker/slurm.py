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

clean_probe_output() {
  LC_ALL=C sed -E $'s/\x1B\[[0-9;?]*[ -\/]*[@-~]//g' \
    | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177'
}

emit_qos_probe_failure() {
  probe=$1
  probe_output=$2
  cleaned=$(printf '%s' "$probe_output" | clean_probe_output)
  lowered=$(printf '%s' "$cleaned" | LC_ALL=C tr '[:upper:]' '[:lower:]')
  case "$lowered" in
    *'permission denied'*|*'access denied'*|*'not authorized'*|*'not permitted'*)
      probe_status=denied
      ;;
    *'command not found'*|*'unknown field'*|*'invalid field'*|*'not found'*)
      probe_status=unsupported
      ;;
    *) probe_status=unavailable ;;
  esac
  probe_lines=$(printf '%s' "$cleaned" | awk 'NR { lines=NR } END { print lines + 0 }')
  probe_bytes=$(printf '%s' "$cleaned" | LC_ALL=C wc -c | tr -d '[:space:]')
  probe_digest=$(printf '%s' "$cleaned" | LC_ALL=C cksum | awk '{ print $1 }')
  printf 'GB|qos-probe|%s|%s|%s|%s|cksum:%s\n' \
    "$probe" "$probe_status" "$probe_lines" "$probe_bytes" "$probe_digest"
}

if [ "${SBATCH_QOS+x}" = x ]; then
  sbatch_qos_present=true
  if [ -n "$SBATCH_QOS" ]; then sbatch_qos_nonempty=true; else sbatch_qos_nonempty=false; fi
  sbatch_qos_bytes=$(printf '%s' "$SBATCH_QOS" | LC_ALL=C wc -c | tr -d '[:space:]')
  sbatch_qos_digest=cksum:$(printf '%s' "$SBATCH_QOS" | LC_ALL=C cksum | awk '{ print $1 }')
else
  sbatch_qos_present=false
  sbatch_qos_nonempty=false
  sbatch_qos_bytes=0
  sbatch_qos_digest=none
fi
printf 'GB|sbatch-env|SBATCH_QOS|%s|%s|%s|%s\n' \
  "$sbatch_qos_present" "$sbatch_qos_nonempty" \
  "$sbatch_qos_bytes" "$sbatch_qos_digest"

partition_qos_output=$(scontrol show partition CPU-64C256GB -o 2>&1)
partition_qos_status=$?
if [ "$partition_qos_status" -eq 0 ] && [ -n "$partition_qos_output" ]; then
  printf '%s\n' "$partition_qos_output" | clean_probe_output | awk '
    {
      for (i=1; i<=NF; i++) {
        split($i, pair, "=")
        key=toupper(pair[1])
        value=substr($i, length(pair[1]) + 2)
        if (key == "PARTITIONNAME") partition=value
        else if (key == "ALLOWQOS") allow_qos=value
        else if (key == "QOS") qos=value
        else if (key == "DEFAULTQOS") default_qos=value
      }
    }
    function safe(value) {
      if (value == "") return "(none)"
      if (value !~ /^[[:alnum:]_.,:+\/()=-]+$/) return "(redacted)"
      return value
    }
    END {
      printf "GB|qos-probe|partition|available|%s|%s|%s|%s\n", \
        safe(partition), safe(allow_qos), safe(qos), safe(default_qos)
    }
  '
else
  emit_qos_probe_failure partition "$partition_qos_output"
fi

current_user=$(id -un)
association_qos_output=$(sacctmgr -n -P show assoc where user="$current_user" \
  format=Account,User,Partition,QOS,DefaultQOS 2>&1)
association_qos_status=$?
if [ "$association_qos_status" -eq 0 ]; then
  printf '%s\n' "$association_qos_output" | clean_probe_output | awk \
    -F '|' -v current_user="$current_user" '
    function safe(value) {
      if (value == "") return "(none)"
      if (value !~ /^[[:alnum:]_.,:+\/()=-]+$/) return "(redacted)"
      return value
    }
    $2 == current_user && count < 64 {
      printf "GB|association-qos|%s|%s|%s|%s\n", \
        safe($1), safe($3), safe($4), safe($5)
      count++
    }
    END { printf "GB|qos-probe|association|available|%d\n", count + 0 }
  '
else
  emit_qos_probe_failure association "$association_qos_output"
fi

aliyunpan_count=0
aliyunpan_seen=

emit_aliyunpan_candidate() {
  candidate=$1
  [ "$aliyunpan_count" -lt 16 ] || return
  [ -f "$candidate" ] && [ -x "$candidate" ] || return
  command -v realpath >/dev/null 2>&1 || return
  canonical=$(realpath "$candidate" 2>/dev/null) || return
  case "$canonical" in
    "$HOME"/*) ;;
    *)
      canonical_parent=$(dirname "$canonical")
      case "$canonical_parent" in
        /bin|/usr/bin|/usr/local/bin|/usr/sbin|/usr/local/sbin) ;;
        *) return ;;
      esac
      ;;
  esac
  case "$canonical" in *'|'*|*$'\n'*|*$'\r'*) return ;; esac
  if printf '%s\n' "$aliyunpan_seen" | grep -Fqx -- "$canonical"; then return; fi
  aliyunpan_seen="${aliyunpan_seen}${aliyunpan_seen:+$'\n'}${canonical}"
  encoded_path=$(printf '%s' "$canonical" | base64 | tr -d '\r\n')
  printf 'GB|aliyunpan-cli|%s\n' "$encoded_path"
  aliyunpan_count=$((aliyunpan_count + 1))
}

command_candidate=$(command -v aliyunpan 2>/dev/null || true)
[ -n "$command_candidate" ] && emit_aliyunpan_candidate "$command_candidate"
emit_aliyunpan_candidate "$HOME/.local/bin/aliyunpan-v0.4.0-proxyfix"
emit_aliyunpan_candidate "$HOME/.local/bin/aliyunpan"
while IFS= read -r candidate; do
  emit_aliyunpan_candidate "$candidate"
  [ "$aliyunpan_count" -lt 16 ] || break
done < <(
  for root in \
    "$HOME/.local/bin" \
    "$HOME/bin" \
    "$HOME/.local/share" \
    "$HOME/aliyunpan" \
    "$HOME/AliyunPan"; do
    [ -d "$root" ] || continue
    find "$root" -mindepth 1 -maxdepth 2 -type f -name 'aliyunpan*' -print
  done | LC_ALL=C sort -u
)
if [ "$aliyunpan_count" -gt 0 ]; then
  aliyunpan_status=available
else
  aliyunpan_status=missing
fi
printf 'GB|aliyunpan-cli-status|%s|%s\n' "$aliyunpan_status" "$aliyunpan_count"

aliyunpan_config="$HOME/.config/aliyunpan"
aliyunpan_config_exists=false
aliyunpan_config_readable=false
if [ -d "$aliyunpan_config" ]; then
  aliyunpan_config_exists=true
  [ -r "$aliyunpan_config" ] && aliyunpan_config_readable=true
  canonical_config=$(realpath "$aliyunpan_config" 2>/dev/null || printf '%s' "$aliyunpan_config")
else
  canonical_config=$aliyunpan_config
fi
encoded_config=$(printf '%s' "$canonical_config" | base64 | tr -d '\r\n')
printf 'GB|aliyunpan-config|%s|%s|%s\n' \
  "$aliyunpan_config_exists" "$aliyunpan_config_readable" "$encoded_config"

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


def _optional_probe_value(value: str) -> str | None:
    return None if value == "(none)" else value


def _decoded_approved_discovery_path(value: str, *, home: str | None) -> str | None:
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded.startswith("/") or any(ord(character) < 32 for character in decoded):
        return None
    path = Path(decoded)
    if ".." in path.parts:
        return None
    if home and decoded.startswith(f"{home.rstrip('/')}/"):
        return decoded
    if str(path.parent) in {
        "/bin",
        "/usr/bin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/local/sbin",
    }:
        return decoded
    return None


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

    @staticmethod
    def _command_prefix(connection: dict[str, Any]) -> list[str]:
        try:
            return SlurmCommandSchedulerAdapter.command_prefix(connection)
        except AdapterCommandError as exc:
            raise SlurmProviderError("scheduler target has an invalid command_prefix") from exc

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
        sbatch_environment: dict[str, Any] | None = None
        partition_qos: dict[str, Any] | None = None
        association_qos_status: dict[str, Any] | None = None
        association_qos: list[dict[str, str | None]] = []
        aliyunpan_cli_status: str | None = None
        aliyunpan_executables: list[str] = []
        aliyunpan_config: dict[str, Any] | None = None
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
            elif record_type == "sbatch-env" and len(parts) == 7:
                sbatch_environment = {
                    "name": parts[2],
                    "present": parts[3] == "true",
                    "nonempty": parts[4] == "true",
                    "byte_count": int(parts[5]),
                    "digest": parts[6],
                }
            elif record_type == "qos-probe" and len(parts) >= 5:
                probe = parts[2]
                status = parts[3]
                if probe == "partition" and status == "available" and len(parts) == 8:
                    partition_qos = {
                        "status": status,
                        "partition": _optional_probe_value(parts[4]),
                        "allow_qos": _optional_probe_value(parts[5]),
                        "qos": _optional_probe_value(parts[6]),
                        "default_qos": _optional_probe_value(parts[7]),
                    }
                elif probe == "association" and status == "available" and len(parts) == 5:
                    association_qos_status = {
                        "status": status,
                        "count": int(parts[4]),
                    }
                elif status in {"denied", "unsupported", "unavailable"} and len(parts) == 7:
                    failure = {
                        "status": status,
                        "output_lines": int(parts[4]),
                        "output_bytes": int(parts[5]),
                        "output_digest": parts[6],
                    }
                    if probe == "partition":
                        partition_qos = failure
                    elif probe == "association":
                        association_qos_status = failure
            elif record_type == "association-qos" and len(parts) == 6:
                association_qos.append(
                    {
                        "account": _optional_probe_value(parts[2]),
                        "partition": _optional_probe_value(parts[3]),
                        "qos": _optional_probe_value(parts[4]),
                        "default_qos": _optional_probe_value(parts[5]),
                    }
                )
            elif record_type == "aliyunpan-cli" and len(parts) == 3:
                home = identity.get("home") if identity is not None else None
                executable = _decoded_approved_discovery_path(parts[2], home=home)
                if executable is not None and executable not in aliyunpan_executables:
                    aliyunpan_executables.append(executable)
            elif record_type == "aliyunpan-cli-status" and len(parts) == 4:
                if parts[2] in {"available", "missing"} and parts[3].isdigit():
                    aliyunpan_cli_status = parts[2]
            elif record_type == "aliyunpan-config" and len(parts) == 5:
                home = identity.get("home") if identity is not None else None
                config_path = _decoded_approved_discovery_path(parts[4], home=home)
                if config_path is not None:
                    aliyunpan_config = {
                        "path": config_path,
                        "exists": parts[2] == "true",
                        "readable": parts[3] == "true",
                    }
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
        if sbatch_environment is not None:
            result["sbatch_environment"] = sbatch_environment
        if partition_qos is not None:
            result["partition_qos"] = partition_qos
        if association_qos_status is not None:
            result["association_qos"] = {
                **association_qos_status,
                "associations": association_qos,
            }
        if aliyunpan_cli_status is not None:
            result["aliyunpan_cli"] = {
                "status": "available" if aliyunpan_executables else aliyunpan_cli_status,
                "count": len(aliyunpan_executables),
                "executables": aliyunpan_executables,
            }
        if aliyunpan_config is not None:
            result["aliyunpan_config"] = aliyunpan_config
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
