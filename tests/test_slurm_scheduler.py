from __future__ import annotations

import base64
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gpu_broker.api import create_app
from gpu_broker.config import Settings
from gpu_broker.models import SchedulerJob
from gpu_broker.schemas import ResourceConstraints, SchedulerOneOffSubmit
from gpu_broker.slurm import (
    CommandSlurmProvider,
    SCHEDULER_INSPECTION_SCRIPT,
    SlurmProviderError,
    SlurmSubmission,
    _scheduler_submit_script,
)


class FakeSlurmProvider:
    def __init__(self) -> None:
        self.access = {
            "status": "ready",
            "partitions": [
                {
                    "partition": "GPU-8A100",
                    "default": False,
                    "availability": "up",
                    "time_limit": "10-00:00:00",
                }
            ],
            "checked_at": "2026-07-31T00:00:00+00:00",
        }
        self.submissions: list[dict[str, Any]] = []
        self.cancellations: list[str] = []
        self.uploads: list[dict[str, Any]] = []
        self.observation = {
            "state": "RUNNING",
            "raw_state": "RUNNING",
            "elapsed_seconds": 12,
            "allocated_tres": {"gres/gpu": "1", "cpu": "8"},
            "exit_code": None,
            "node_list": "g001",
            "started_at": "2026-07-31T00:01:00+00:00",
            "completed_at": None,
        }

    def access_status(self, connection: dict[str, Any]) -> dict[str, Any]:
        return self.access

    def find_by_name(
        self,
        connection: dict[str, Any],
        job_name: str,
    ) -> SlurmSubmission | None:
        return None

    def submit(
        self,
        connection: dict[str, Any],
        *,
        broker_job_id: str,
        request: dict[str, Any],
        script_body: str,
    ) -> SlurmSubmission:
        self.submissions.append(
            {
                "connection": connection,
                "broker_job_id": broker_job_id,
                "request": request,
                "script_body": script_body,
            }
        )
        return SlurmSubmission("123456")

    def query(
        self,
        connection: dict[str, Any],
        scheduler_job_id: str,
    ) -> dict[str, Any]:
        assert scheduler_job_id == "123456"
        return self.observation

    def cancel(
        self,
        connection: dict[str, Any],
        scheduler_job_id: str,
    ) -> None:
        self.cancellations.append(scheduler_job_id)

    def upload(
        self,
        connection: dict[str, Any],
        *,
        local_path: Path,
        remote_directory: str,
        transfer_id: str,
    ) -> str:
        self.uploads.append(
            {
                "connection": connection,
                "local_path": local_path,
                "remote_directory": remote_directory,
                "transfer_id": transfer_id,
            }
        )
        return (
            f"{remote_directory}/gpu-broker-{transfer_id}/{local_path.name}"
        )


def _client(tmp_path: Path, inventory) -> tuple[TestClient, FakeSlurmProvider]:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(inventory.model_dump(mode="json")),
        encoding="utf-8",
    )
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'scheduler.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    provider = FakeSlurmProvider()
    app.state.service.slurm_provider = provider
    return TestClient(app), provider


def _decoded_submit_script(remote_command: str) -> str:
    outer = shlex.split(remote_command)
    assert outer[:2] == ["bash", "-lc"]
    wrapper = shlex.split(outer[2])
    assert wrapper[:2] == ["printf", "%s"]
    return base64.b64decode(wrapper[2]).decode("utf-8")


def _write_fake_command(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\nset -uo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o700)


def _run_scheduler_submit_script(
    tmp_path: Path,
    *,
    sbatch_body: str,
    squeue_body: str = "exit 0",
    sacct_body: str = "exit 0",
    script_body: str = "true\n",
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_command(fake_bin / "sbatch", sbatch_body)
    _write_fake_command(fake_bin / "squeue", squeue_body)
    _write_fake_command(fake_bin / "sacct", sacct_body)
    _write_fake_command(fake_bin / "sleep", "exit 0")
    script = _scheduler_submit_script(
        [
            "sbatch",
            "--parsable",
            "--job-name=gb-test-job",
            "--comment=gpu-broker:broker-test-id",
        ],
        job_name="gb-test-job",
        comment="gpu-broker:broker-test-id",
        script_body=script_body,
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    return subprocess.run(
        ["/bin/bash"],
        input=script,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _structured_marker(stderr: str, prefix: str) -> dict[str, str]:
    line = next(line for line in stderr.splitlines() if line.startswith(prefix))
    return dict(field.split("=", 1) for field in line.split("|")[2:])


def _target() -> dict[str, Any]:
    return {
        "id": "hanhai22",
        "display_name": "USTC Hanhai22",
        "adapter": "slurm-command",
        "command_prefix": ["/Users/test/.local/bin/hh22", "1"],
        "credential_refs": {
            "ssh_password_service": "ustc-hanhai22-ssh-password",
            "totp_service": "ustc-hanhai22-totp-secret",
        },
        "capabilities": ["access-status", "submit", "status", "cancel"],
        "access_hint": "Connect USTC SCC VPN and retry; Broker does not operate VPN.",
        "enabled": True,
    }


def _profile() -> dict[str, Any]:
    return {
        "id": "hanhai-a100-smoke",
        "project_id": "project-a",
        "display_name": "Hanhai A100 smoke test",
        "purpose": "approved Hanhai A100 smoke test",
        "duration_seconds": 3600,
        "constraints": {"gpu_count": 1, "placement": "pack"},
        "runtime_kind": "slurm",
        "scheduler_target_id": "hanhai22",
        "scheduler": {
            "partition": "GPU-8A100",
            "qos": "gpu_8a100",
            "gpu_type": "a100",
            "cpu_cores": 8,
            "memory_mib": 65536,
            "nodes": 1,
            "tasks_per_node": 1,
            "working_directory": "/home/test/project",
            "stdout_pattern": "smoke-%j.out",
            "stderr_pattern": "smoke-%j.err",
        },
        "scheduler_script": "set -euo pipefail\nsrun python -u smoke.py\n",
        "grant_project_ids": ["project-b"],
        "grant_all_projects": False,
        "retain_submission_body": False,
        "enabled": True,
    }


def _upload_target() -> dict[str, Any]:
    target = _target()
    target["capabilities"] = [
        "access-status",
        "submit",
        "status",
        "cancel",
        "data-transfer",
    ]
    target["upload"] = {
        "ssh_host": "211.86.151.113",
        "ssh_user": "test",
        "ssh_port": 22,
        "control_path": "/Users/test/.ssh/cm/%C",
    }
    return target


def test_command_slurm_access_status_parses_fixed_inspection_output() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(
                [
                    "spawn /usr/bin/ssh -- sinfo -h -o '%P|%a|%l'",
                    "GB|identity|hanhai22-01|jinplu|/home/jinplu|/home/jinplu",
                    "GB|path|home|/home/jinplu|directory|true",
                    "GB|path|home-root|/home|directory|false",
                    "GB|path|software|/opt|directory|false",
                    "GB|filesystem|gpfs|524288000|1024000|523264000|1%|/home",
                    "GB|quota|Disk quotas for user jinplu:",
                    "GB|sbatch-env|SBATCH_QOS|true|true|7|cksum:123456789",
                    (
                        "GB|qos-probe|partition|available|CPU-64C256GB|"
                        "cpu,normal|N/A|normal"
                    ),
                    "GB|association-qos|acct-a|CPU-64C256GB|cpu,normal|normal",
                    "GB|association-qos|acct-a|(none)|normal|normal",
                    "GB|qos-probe|association|available|2",
                    (
                        "GB|aliyunpan-cli|"
                        + base64.b64encode(
                            b"/home/jinplu/.local/bin/aliyunpan"
                        ).decode("ascii")
                    ),
                    "GB|aliyunpan-cli-status|available|1",
                    (
                        "GB|aliyunpan-config|true|true|"
                        + base64.b64encode(
                            b"/home/jinplu/.config/aliyunpan"
                        ).decode("ascii")
                    ),
                    "GB|partition|GPU-8A100|up|10-00:00:00|18|0/1152/0/1152|gpu:a100:8",
                    "GB|partition|test*|up|20:00|2|0/128/0/128|(null)",
                ]
            ),
            stderr="",
        )

    provider = CommandSlurmProvider(runner=runner)
    result = provider.access_status({"command_prefix": ["/Users/test/.local/bin/hh22", "1"]})

    assert result["status"] == "ready"
    assert result["identity"] == {
        "hostname": "hanhai22-01",
        "user": "jinplu",
        "home": "/home/jinplu",
        "pwd": "/home/jinplu",
    }
    assert result["paths"][0] == {
        "label": "home",
        "path": "/home/jinplu",
        "kind": "directory",
        "writable": True,
    }
    assert result["filesystem"]["available_kib"] == 523264000
    assert result["quota_summary"] == ["Disk quotas for user jinplu:"]
    assert result["sbatch_environment"] == {
        "name": "SBATCH_QOS",
        "present": True,
        "nonempty": True,
        "byte_count": 7,
        "digest": "cksum:123456789",
    }
    assert result["partition_qos"] == {
        "status": "available",
        "partition": "CPU-64C256GB",
        "allow_qos": "cpu,normal",
        "qos": "N/A",
        "default_qos": "normal",
    }
    assert result["association_qos"] == {
        "status": "available",
        "count": 2,
        "associations": [
            {
                "account": "acct-a",
                "partition": "CPU-64C256GB",
                "qos": "cpu,normal",
                "default_qos": "normal",
            },
            {
                "account": "acct-a",
                "partition": None,
                "qos": "normal",
                "default_qos": "normal",
            },
        ],
    }
    assert result["aliyunpan_cli"] == {
        "status": "available",
        "count": 1,
        "executables": ["/home/jinplu/.local/bin/aliyunpan"],
    }
    assert result["aliyunpan_config"] == {
        "path": "/home/jinplu/.config/aliyunpan",
        "exists": True,
        "readable": True,
    }
    assert [partition["partition"] for partition in result["partitions"]] == [
        "GPU-8A100",
        "test",
    ]
    assert result["partitions"][1]["default"] is True
    assert result["partitions"][0]["node_count"] == 18
    assert result["partitions"][0]["cpus"] == "0/1152/0/1152"
    assert result["partitions"][0]["gres"] == "gpu:a100:8"
    assert len(calls) == 1
    assert calls[0][:2] == ["/Users/test/.local/bin/hh22", "1"]
    assert calls[0][-1].startswith("bash -lc ")


@pytest.mark.parametrize(
    ("partition_marker", "association_marker", "partition_status", "assoc_status"),
    [
        (
            "GB|qos-probe|partition|unsupported|1|29|cksum:101",
            "GB|qos-probe|association|denied|1|17|cksum:202",
            "unsupported",
            "denied",
        ),
        (
            "GB|qos-probe|partition|unavailable|0|0|cksum:303",
            "GB|qos-probe|association|unsupported|1|22|cksum:404",
            "unavailable",
            "unsupported",
        ),
    ],
)
def test_command_slurm_access_status_parses_nonleaking_qos_probe_failures(
    partition_marker: str,
    association_marker: str,
    partition_status: str,
    assoc_status: str,
) -> None:
    output = "\n".join(
        [
            "GB|identity|host|user|/home/user|/home/user",
            "GB|sbatch-env|SBATCH_QOS|false|false|0|none",
            partition_marker,
            association_marker,
            "GB|partition|CPU-64C256GB|up|15-00:00:00|1|0/64/0/64|(null)",
        ]
    )

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    result = CommandSlurmProvider(runner=runner).access_status(
        {"command_prefix": ["approved-helper", "1"]}
    )

    assert result["sbatch_environment"]["present"] is False
    assert result["partition_qos"]["status"] == partition_status
    assert result["association_qos"]["status"] == assoc_status
    assert result["association_qos"]["associations"] == []
    assert "permission denied" not in str(result)
    assert "unknown field" not in str(result)


def test_command_slurm_access_status_rejects_unapproved_discovery_paths() -> None:
    def encoded(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    output = "\n".join(
        [
            "GB|identity|host|user|/home/user|/home/user",
            f"GB|aliyunpan-cli|{encoded('/home/user/bin/aliyunpan')}",
            f"GB|aliyunpan-cli|{encoded('/usr/bin/aliyunpan')}",
            f"GB|aliyunpan-cli|{encoded('/tmp/aliyunpan-secret')}",
            f"GB|aliyunpan-cli|{encoded('/usr/bin/nested/aliyunpan')}",
            f"GB|aliyunpan-cli|{encoded('relative/aliyunpan')}",
            "GB|aliyunpan-cli|not-valid-base64!",
            "GB|aliyunpan-cli-status|available|99",
            f"GB|aliyunpan-config|true|true|{encoded('/tmp/private-config')}",
            "GB|partition|CPU-64C256GB|up|15-00:00:00|1|0/64/0/64|(null)",
        ]
    )

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    result = CommandSlurmProvider(runner=runner).access_status(
        {"command_prefix": ["approved-helper", "1"]}
    )

    assert result["aliyunpan_cli"] == {
        "status": "available",
        "count": 2,
        "executables": ["/home/user/bin/aliyunpan", "/usr/bin/aliyunpan"],
    }
    assert "aliyunpan_config" not in result
    assert "/tmp/aliyunpan-secret" not in str(result)
    assert "/usr/bin/nested/aliyunpan" not in str(result)


def test_scheduler_inspection_reports_only_digest_for_remote_sbatch_qos(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "inspection-bin"
    fake_bin.mkdir()
    _write_fake_command(
        fake_bin / "scontrol",
        (
            "printf 'PartitionName=CPU-64C256GB AllowQos=cpu,normal "
            "QoS=N/A DefaultQOS=normal\\n'"
        ),
    )
    _write_fake_command(
        fake_bin / "sacctmgr",
        (
            "printf 'acct-a|%s|CPU-64C256GB|cpu,normal|normal|\\n' "
            '"$(id -un)"'
        ),
    )
    _write_fake_command(
        fake_bin / "sinfo",
        "printf 'GB|partition|CPU-64C256GB|up|15-00:00:00|1|0/64/0/64|(null)\\n'",
    )
    _write_fake_command(fake_bin / "quota", "exit 0")
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["SBATCH_QOS"] = "private-remote-qos"

    completed = subprocess.run(
        ["/bin/bash"],
        input=SCHEDULER_INSPECTION_SCRIPT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert "private-remote-qos" not in completed.stdout
    env_marker = next(
        line
        for line in completed.stdout.splitlines()
        if line.startswith("GB|sbatch-env|SBATCH_QOS|")
    )
    env_parts = env_marker.split("|")
    assert env_parts[3:6] == ["true", "true", "18"]
    assert env_parts[6].startswith("cksum:")
    assert env_parts[6].removeprefix("cksum:").isdigit()
    assert "GB|qos-probe|partition|available|CPU-64C256GB|" in completed.stdout
    assert "GB|association-qos|acct-a|CPU-64C256GB|cpu,normal|normal" in completed.stdout
    assert "GB|qos-probe|association|available|1" in completed.stdout


def test_scheduler_inspection_bounded_aliyunpan_discovery_does_not_read_config(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local" / "bin"
    share_bin = fake_home / ".local" / "share" / "tools"
    local_bin.mkdir(parents=True)
    share_bin.mkdir(parents=True)
    for index in range(20):
        candidate = (
            local_bin / "aliyunpan"
            if index == 0
            else share_bin / f"aliyunpan-{index:02d}"
        )
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o700)
    proxyfix = local_bin / "aliyunpan-v0.4.0-proxyfix"
    proxyfix.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proxyfix.chmod(0o700)
    config_dir = fake_home / ".config" / "aliyunpan"
    config_dir.mkdir(parents=True)
    secret = "must-not-read-config-secret"
    (config_dir / "config.json").write_text(secret, encoding="utf-8")

    fake_bin = tmp_path / "discovery-bin"
    fake_bin.mkdir()
    _write_fake_command(
        fake_bin / "scontrol",
        "printf 'PartitionName=CPU-64C256GB AllowQos=ALL QoS=N/A\\n'",
    )
    _write_fake_command(fake_bin / "sacctmgr", "exit 0")
    _write_fake_command(
        fake_bin / "sinfo",
        "printf 'GB|partition|CPU-64C256GB|up|15-00:00:00|1|0/64/0/64|(null)\\n'",
    )
    _write_fake_command(fake_bin / "quota", "exit 0")
    environment = dict(os.environ)
    environment["HOME"] = str(fake_home)
    environment["PATH"] = f"{local_bin}:{fake_bin}:{environment['PATH']}"

    completed = subprocess.run(
        ["/bin/bash"],
        input=SCHEDULER_INSPECTION_SCRIPT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    cli_markers = [
        line.split("|", 2)[2]
        for line in completed.stdout.splitlines()
        if line.startswith("GB|aliyunpan-cli|")
    ]
    discovered = [
        base64.b64decode(value, validate=True).decode("utf-8")
        for value in cli_markers
    ]
    assert len(discovered) == 16
    assert str(local_bin / "aliyunpan") in discovered
    assert str(proxyfix) in discovered
    assert all(path.startswith(f"{fake_home}/") for path in discovered)
    assert "GB|aliyunpan-cli-status|available|16" in completed.stdout
    assert "GB|aliyunpan-config|true|true|" in completed.stdout
    assert secret not in completed.stdout


def test_command_slurm_opaque_command_echo_does_not_trigger_vpn_access_error() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            87,
            stdout="spawn helper opaque-base64-fragment-vpn-fragment\n",
            stderr="",
        )

    provider = CommandSlurmProvider(runner=runner)
    with pytest.raises(SlurmProviderError) as raised:
        provider._run(
            {"command_prefix": ["/Users/test/.local/bin/hh22", "1"]},
            ["encoded-remote-command"],
            mutating=True,
        )

    assert raised.value.access_required is False
    assert raised.value.uncertain is True


def test_command_slurm_explicit_vpn_failure_remains_access_required() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="VPN disconnected; connect the approved VPN.\n",
        )

    provider = CommandSlurmProvider(runner=runner)
    with pytest.raises(SlurmProviderError) as raised:
        provider._run(
            {"command_prefix": ["/Users/test/.local/bin/hh22", "1"]},
            ["read-only-command"],
            mutating=False,
        )

    assert raised.value.access_required is True
    assert raised.value.uncertain is False


def test_command_slurm_cpu_only_submission_omits_gpu_gres() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "spawn approved-login-helper -- encoded-command\n"
                "GB|scheduler-submit|123456\n"
            ),
            stderr="",
        )

    provider = CommandSlurmProvider(runner=runner)
    submission = provider.submit(
        {"command_prefix": ["/Users/test/.local/bin/hh22", "1"]},
        broker_job_id="cpu-only-job",
        request={
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "cpu_cores": 64,
                "memory_mib": 256 * 1024,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test/project",
                "stdout_pattern": "cpu-%j.out",
                "stderr_pattern": "cpu-%j.err",
            },
        },
        script_body="set -euo pipefail\nsrun python -u cpu_job.py\n",
    )

    assert submission.scheduler_job_id == "123456"
    assert len(calls) == 1
    remote_command = calls[0][-1]
    assert "GB|scheduler-submit|" not in remote_command
    assert "GB|scheduler-submit-error|" not in remote_command
    submit_script = _decoded_submit_script(remote_command)
    assert "--partition=CPU-64C256GB" in submit_script
    assert "--cpus-per-task=64" in submit_script
    assert "--mem=262144M" in submit_script
    assert "--gres=" not in submit_script
    assert "--qos=" not in submit_script
    assert "--wrap=" not in submit_script


def test_command_slurm_gpu_submission_includes_gres() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="GB|scheduler-submit|123456\n",
            stderr="",
        )

    provider = CommandSlurmProvider(runner=runner)
    submission = provider.submit(
        {"command_prefix": ["/Users/test/.local/bin/hh22", "1"]},
        broker_job_id="gpu-job",
        request={
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1},
            "scheduler": {
                "partition": "GPU-8A100",
                "qos": "gpu_8a100",
                "gpu_type": "a100",
                "cpu_cores": 8,
                "memory_mib": 65536,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test/project",
                "stdout_pattern": "gpu-%j.out",
                "stderr_pattern": "gpu-%j.err",
            },
        },
        script_body="set -euo pipefail\nsrun python -u gpu_job.py\n",
    )

    assert submission.scheduler_job_id == "123456"
    submit_script = _decoded_submit_script(calls[0][-1])
    assert "--gres=gpu:a100:1" in submit_script
    assert "--qos=gpu_8a100" in submit_script
    assert "--wrap=" not in submit_script


def test_command_slurm_submission_uses_only_stdin_script_and_option_argv(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "full-path-bin"
    fake_bin.mkdir()
    argv_capture = tmp_path / "sbatch-argv.txt"
    script_capture = tmp_path / "sbatch-stdin.sh"
    _write_fake_command(
        fake_bin / "sbatch",
        (
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_capture))}\n"
            f"cat > {shlex.quote(str(script_capture))}\n"
            "printf '123456;hanhai22\\n'"
        ),
    )
    _write_fake_command(fake_bin / "squeue", "exit 0")
    _write_fake_command(fake_bin / "sacct", "exit 0")
    _write_fake_command(fake_bin / "sleep", "exit 0")
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        executed = subprocess.run(
            ["/bin/bash", "-c", command[-1]],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        return subprocess.CompletedProcess(
            command,
            executed.returncode,
            stdout=executed.stdout,
            stderr=executed.stderr,
        )

    script_body = "set -euo pipefail\nprintf 'exact script bytes\\n'\n"
    provider = CommandSlurmProvider(runner=runner)
    submission = provider.submit(
        {"command_prefix": ["approved-helper", "1"]},
        broker_job_id="stdin-script-job",
        request={
            "duration_seconds": 300,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "cpu_cores": 1,
                "memory_mib": 1024,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test/project",
                "stdout_pattern": "cpu-%j.out",
                "stderr_pattern": "cpu-%j.err",
            },
        },
        script_body=script_body,
    )

    assert submission.scheduler_job_id == "123456"
    assert script_capture.read_bytes() == script_body.encode("utf-8")
    argv = argv_capture.read_text(encoding="utf-8").splitlines()
    assert argv == [
        "--parsable",
        "--job-name=gb-stdin-script-job",
        "--comment=gpu-broker:stdin-script-job",
        "--partition=CPU-64C256GB",
        "--nodes=1",
        "--ntasks-per-node=1",
        "--cpus-per-task=1",
        "--mem=1024M",
        "--time=00:05:00",
        "--chdir=/home/test/project",
        "--output=cpu-%j.out",
        "--error=cpu-%j.err",
    ]
    assert all(argument.startswith("--") for argument in argv)


def test_command_slurm_query_ignores_pty_noise_and_parses_naive_sacct_time() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(
                [
                    "spawn approved-login-helper -- opaque-command",
                    "site quota notice",
                    (
                        "1093431|PENDING|0|billing=1,cpu=1,mem=1024M|0:0|"
                        "cpu001|2026-08-04T14:20:00|Unknown"
                    ),
                    "Shared connection to scheduler closed.",
                ]
            ),
            stderr="",
        )

    observation = CommandSlurmProvider(runner=runner).query(
        {"command_prefix": ["approved-helper", "1"]},
        "1093431",
    )

    assert observation == {
        "state": "PENDING",
        "raw_state": "PENDING",
        "elapsed_seconds": 0,
        "allocated_tres": {"billing": "1", "cpu": "1", "mem": "1024M"},
        "exit_code": "0:0",
        "node_list": "cpu001",
        "started_at": "2026-08-04T14:20:00",
        "completed_at": "Unknown",
    }


def test_unquoted_site_forwarder_splits_legacy_wrap_into_residual_arguments(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "legacy-forwarder-bin"
    fake_bin.mkdir()
    argv_capture = tmp_path / "legacy-native-argv.txt"
    opaque_error = "sbatch: error: --wrap option not permitted with script args".ljust(
        71, "x"
    )
    assert len(opaque_error.encode("utf-8")) == 71
    _write_fake_command(fake_bin / "sbatch", "native-sbatch $@")
    _write_fake_command(
        fake_bin / "native-sbatch",
        (
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_capture))}\n"
            f"printf %s {shlex.quote(opaque_error)}"
        ),
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    legacy_arguments = [
        "sbatch",
        "--parsable",
        "--wrap=printf %s opaque-payload | base64 -d | /bin/bash",
    ]

    result = subprocess.run(
        ["/bin/bash", "-c", shlex.join(legacy_arguments)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert len(result.stdout.encode("utf-8")) == 71
    assert argv_capture.read_text(encoding="utf-8").splitlines() == [
        "--parsable",
        "--wrap=printf",
        "%s",
        "opaque-payload",
        "|",
        "base64",
        "-d",
        "|",
        "/bin/bash",
    ]


@pytest.mark.parametrize(
    "sbatch_body",
    [
        "printf 'site notice\\n123456;hanhai22\\ntrailing notice\\n'",
        (
            "printf 'site notice\\n\\033[31mSubmitted batch job "
            "123456\\033[0m\\ntrailing notice\\n'"
        ),
    ],
)
def test_scheduler_submit_script_normalizes_supported_multiline_output(
    tmp_path: Path,
    sbatch_body: str,
) -> None:
    result = _run_scheduler_submit_script(tmp_path, sbatch_body=sbatch_body)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "GB|scheduler-submit|123456\n"


@pytest.mark.parametrize(
    ("squeue_body", "sacct_body"),
    [
        (
            (
                "printf '123456|gpu-broker:broker-test-id\\n"
                "999999|unrelated-comment\\n'"
            ),
            "exit 0",
        ),
        (
            "printf '999999|unrelated-comment\\n'",
            "printf '123456|gpu-broker:broker-test-id\\n'",
        ),
    ],
)
def test_scheduler_submit_script_recovers_by_unique_name_and_comment(
    tmp_path: Path,
    squeue_body: str,
    sacct_body: str,
) -> None:
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body="printf 'site notice without an identifier\\n'",
        squeue_body=squeue_body,
        sacct_body=sacct_body,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "GB|scheduler-submit|123456\n"


@pytest.mark.parametrize(
    (
        "sbatch_body",
        "squeue_body",
        "expected_status",
        "expected_class",
        "expected_recovery",
    ),
    [
        (
            "printf '123456\\n654321\\n'",
            "exit 0",
            86,
            "ambiguous-id",
            "not-run",
        ),
        (
            "printf 'site notice without an identifier\\n'",
            (
                "printf '123456|gpu-broker:broker-test-id\\n"
                "654321|gpu-broker:broker-test-id\\n'"
            ),
            86,
            "ambiguous-recovery",
            "ambiguous",
        ),
        (
            "printf 'site notice without an identifier\\n'",
            "exit 0",
            87,
            "no-id-after-lookup",
            "none",
        ),
    ],
)
def test_scheduler_submit_script_fails_closed_without_one_unique_id(
    tmp_path: Path,
    sbatch_body: str,
    squeue_body: str,
    expected_status: int,
    expected_class: str,
    expected_recovery: str,
) -> None:
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body=sbatch_body,
        squeue_body=squeue_body,
    )

    assert result.returncode == expected_status
    assert result.stdout == ""
    assert (
        f"GB|scheduler-submit-error|class={expected_class}|"
        f"exit={expected_status}|" in result.stderr
    )
    assert "|digest=cksum:" in result.stderr
    assert f"|recovery={expected_recovery}" in result.stderr
    assert "site notice" not in result.stderr
    assert "123456" not in result.stderr


@pytest.mark.parametrize(
    ("sbatch_body", "expected_status", "expected_class", "expected_size"),
    [
        (
            "printf 'sbatch: unrecognized option --legacy\\n' >&2\nexit 1",
            1,
            "unsupported-option",
            "nonempty",
        ),
        (
            "printf 'sbatch: command not found\\n' >&2\nexit 127",
            127,
            "command-not-found",
            "nonempty",
        ),
        (
            "printf 'slurm controller rejected request\\n' >&2\nexit 2",
            2,
            "slurm-error",
            "nonempty",
        ),
        ("exit 1", 1, "slurm-error", "empty"),
    ],
)
def test_scheduler_submit_script_reports_sanitized_sbatch_failure(
    tmp_path: Path,
    sbatch_body: str,
    expected_status: int,
    expected_class: str,
    expected_size: str,
) -> None:
    result = _run_scheduler_submit_script(tmp_path, sbatch_body=sbatch_body)

    assert result.returncode == expected_status
    assert result.stdout == ""
    assert (
        f"GB|scheduler-submit-error|class={expected_class}|"
        f"exit={expected_status}|" in result.stderr
    )
    assert "|digest=cksum:" in result.stderr
    assert "|recovery=not-run" in result.stderr
    if expected_size == "empty":
        assert "|lines=0|bytes=0|" in result.stderr
    else:
        assert "|lines=1|" in result.stderr
        assert "|bytes=0|" not in result.stderr
    assert "sbatch:" not in result.stderr
    assert "controller rejected" not in result.stderr


@pytest.mark.parametrize(
    ("sbatch_body", "expected"),
    [
        (
            "printf 'Job accepted: allocation 123456\\n'",
            {
                "clean_lines": "1",
                "clean_nonspace": "1",
                "clean_changed": "0",
                "standard_ids": "0",
                "numeric_runs": "1",
                "min_digits": "6",
                "max_digits": "6",
                "jobid_runs": "1",
                "colon": "1",
                "kw_job": "1",
                "kw_error": "0",
            },
        ),
        (
            "printf 'warning: delayed\\nSubmitted job 123456\\n'",
            {
                "clean_lines": "2",
                "standard_ids": "0",
                "numeric_runs": "1",
                "jobid_runs": "1",
                "colon": "1",
                "kw_submitted": "1",
                "kw_batch": "0",
                "kw_job": "1",
                "kw_warning": "1",
            },
        ),
        (
            "printf '\\033[31m\\001\\033[0m'",
            {
                "clean_lines": "0",
                "clean_bytes": "0",
                "clean_nonspace": "0",
                "clean_changed": "1",
                "standard_ids": "0",
                "numeric_runs": "0",
                "jobid_runs": "0",
            },
        ),
        (
            "printf 'sbatch: error: policy 123456 denied\\n'",
            {
                "clean_lines": "1",
                "standard_ids": "0",
                "numeric_runs": "1",
                "jobid_runs": "1",
                "colon": "1",
                "kw_error": "1",
                "kw_policy": "1",
                "kw_denied": "1",
            },
        ),
    ],
)
def test_scheduler_submit_script_reports_nonsecret_output_shape(
    tmp_path: Path,
    sbatch_body: str,
    expected: dict[str, str],
) -> None:
    result = _run_scheduler_submit_script(tmp_path, sbatch_body=sbatch_body)

    assert result.returncode == 87
    assert result.stdout == ""
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    for field, value in expected.items():
        assert shape[field] == value
    assert int(shape["raw_bytes"]) >= int(shape["clean_bytes"])
    assert "123456" not in result.stderr
    assert "Job accepted" not in result.stderr
    assert "allocation" not in result.stderr
    assert "sbatch: error:" not in result.stderr


@pytest.mark.parametrize(
    "sbatch_body",
    [
        "printf 'Job accepted: allocation 123456\\n'",
        "printf 'tracking reference 123456\\n'",
        "printf 'policy reference 123456 denied\\n'",
    ],
)
def test_scheduler_submit_script_does_not_trust_one_unbound_numeric_run(
    tmp_path: Path,
    sbatch_body: str,
) -> None:
    result = _run_scheduler_submit_script(tmp_path, sbatch_body=sbatch_body)

    assert result.returncode == 87
    assert result.stdout == ""
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    assert shape["standard_ids"] == "0"
    assert shape["numeric_runs"] == "1"
    assert shape["jobid_runs"] == "1"


def test_scheduler_submit_script_reports_shape_for_opaque_71_byte_line(
    tmp_path: Path,
) -> None:
    opaque_output = "sensitive-policy-text".ljust(71, "x")
    assert len(opaque_output.encode("utf-8")) == 71
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body=f"printf %s {shlex.quote(opaque_output)}",
    )

    assert result.returncode == 87
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    assert shape["raw_lines"] == "1"
    assert shape["raw_bytes"] == "71"
    assert shape["clean_lines"] == "1"
    assert shape["clean_bytes"] == "71"
    assert shape["kw_policy"] == "1"
    assert opaque_output not in result.stderr


def test_scheduler_submit_script_classifies_rc0_error_output_without_accepting_id(
    tmp_path: Path,
) -> None:
    opaque_output = "sbatch: error: --wrap option not permitted".ljust(71, "x")
    assert len(opaque_output.encode("utf-8")) == 71
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body=f"printf %s {shlex.quote(opaque_output)}",
    )

    assert result.returncode == 87
    assert result.stdout == ""
    failure = _structured_marker(result.stderr, "GB|scheduler-submit-error|")
    assert failure["class"] == "scheduler-error-output"
    assert failure["exit"] == "87"
    assert failure["recovery"] == "none"
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    assert shape["clean_bytes"] == "71"
    assert shape["standard_ids"] == "0"
    assert shape["kw_batch"] == "1"
    assert shape["kw_error"] == "1"
    assert shape["kw_option"] == "1"
    assert shape["kw_wrap"] == "1"
    assert shape["kw_not_permitted"] == "1"
    assert opaque_output not in result.stderr


@pytest.mark.parametrize(
    ("opaque_output", "expected_flags"),
    [
        (
            "opaque unrecognized option comment parsable wrap",
            {
                "kw_unrecognized",
                "kw_option",
                "kw_comment",
                "kw_parsable",
                "kw_wrap",
            },
        ),
        (
            "opaque account partition qos node cpu memory time chdir output",
            {
                "kw_account",
                "kw_partition",
                "kw_qos",
                "kw_node",
                "kw_cpu",
                "kw_memory",
                "kw_time",
                "kw_chdir",
                "kw_output",
            },
        ),
        (
            "opaque reservation association group user limit permission available",
            {
                "kw_reservation",
                "kw_association",
                "kw_group",
                "kw_user",
                "kw_limit",
                "kw_permission",
                "kw_available",
            },
        ),
        (
            "opaque configuration contact controller submit fail not permitted",
            {
                "kw_configuration",
                "kw_contact",
                "kw_controller",
                "kw_submit",
                "kw_fail",
                "kw_not_permitted",
            },
        ),
        (
            (
                "opaque system submissions disabled unexpected message received "
                "plugin filter"
            ),
            {
                "kw_system",
                "kw_submissions",
                "kw_disabled",
                "kw_unexpected",
                "kw_message",
                "kw_received",
                "kw_plugin",
                "kw_filter",
            },
        ),
    ],
)
def test_scheduler_submit_script_reports_bounded_failure_taxonomy(
    tmp_path: Path,
    opaque_output: str,
    expected_flags: set[str],
) -> None:
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body=f"printf %s {shlex.quote(opaque_output)}",
    )

    assert result.returncode == 87
    assert result.stdout == ""
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    for flag in expected_flags:
        assert shape[flag] == "1"
    assert opaque_output not in result.stderr


def test_scheduler_submit_script_reports_non_ascii_byte_count_without_text(
    tmp_path: Path,
) -> None:
    opaque_output = "站点错误"
    result = _run_scheduler_submit_script(
        tmp_path,
        sbatch_body=f"printf %s {shlex.quote(opaque_output)}",
    )

    assert result.returncode == 87
    shape = _structured_marker(result.stderr, "GB|scheduler-submit-shape|")
    assert shape["clean_bytes"] == str(len(opaque_output.encode("utf-8")))
    assert shape["ascii_bytes"] == "0"
    assert shape["non_ascii_bytes"] == str(len(opaque_output.encode("utf-8")))
    assert shape["ascii_only"] == "0"
    assert opaque_output not in result.stderr


def test_scheduler_one_off_accepts_cpu_constraints_but_direct_constraints_reject_zero() -> None:
    with pytest.raises(ValidationError):
        ResourceConstraints.model_validate({"gpu_count": 0})

    submission = SchedulerOneOffSubmit.model_validate(
        {
            "target_id": "hanhai22",
            "project_id": "project-a",
            "task_ref": "cpu-one-off",
            "purpose": "approved CPU-only one-off job",
            "approval_ref": "thread:approved-cpu-one-off",
            "duration_seconds": 600,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "cpu_cores": 64,
                "memory_mib": 256 * 1024,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test",
            },
            "script_body": "true\n",
        }
    )

    assert submission.constraints.gpu_count == 0
    assert submission.scheduler.qos is None
    assert submission.approval_ref == "thread:approved-cpu-one-off"


def test_scheduler_target_is_discoverable_and_access_is_read_only(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    headers = {
        "X-GPU-Broker-Actor": "scheduler-admin",
        "Idempotency-Key": "target-hanhai22",
    }
    created = client.post("/api/v1/scheduler-targets", json=_target(), headers=headers)
    assert created.status_code == 200

    listed = client.get(
        "/api/v1/scheduler-targets",
        headers={"X-GPU-Broker-Actor": "other-project-agent"},
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == "hanhai22"
    assert listed.json()["data"][0]["kind"] == "external-scheduler"
    assert "command_prefix" not in listed.json()["data"][0]

    access = client.get(
        "/api/v1/scheduler-targets/hanhai22/access",
        headers={"X-GPU-Broker-Actor": "other-project-agent"},
    )
    assert access.status_code == 200
    assert access.json()["access"] == provider.access
    coordination = client.get(
        "/api/v1/coordination",
        headers={"X-GPU-Broker-Actor": "other-project-agent"},
    )
    cached_target = coordination.json()["data"]["scheduler_targets"][0]
    assert cached_target["id"] == "hanhai22"
    assert cached_target["last_access"]["status"] == "ready"
    assert cached_target["last_access"]["checked_at"] is not None


def test_granted_project_can_submit_profile_idempotently_and_refresh(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-hanhai22",
        },
    )
    profile = client.post(
        "/api/v1/workload-profiles",
        json=_profile(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "profile-hanhai22",
        },
    )
    assert profile.status_code == 200, profile.text

    visible = client.get(
        "/api/v1/workload-profiles?project_id=project-b",
        headers={"X-GPU-Broker-Actor": "storyboard-agent"},
    )
    assert [item["id"] for item in visible.json()["data"]] == [
        "hanhai-a100-smoke"
    ]

    submit_headers = {
        "X-GPU-Broker-Actor": "storyboard-agent",
        "Idempotency-Key": "storyboard-smoke-1",
    }
    first = client.post(
        "/api/v1/workload-profiles/hanhai-a100-smoke/scheduler-submit",
        json={"project_id": "project-b", "task_ref": "storyboard-smoke"},
        headers=submit_headers,
    )
    second = client.post(
        "/api/v1/workload-profiles/hanhai-a100-smoke/scheduler-submit",
        json={"project_id": "project-b", "task_ref": "storyboard-smoke"},
        headers=submit_headers,
    )
    assert first.status_code == 200, first.text
    assert second.json() == first.json()
    assert len(provider.submissions) == 1
    job = first.json()["scheduler_job"]
    assert job["scheduler_job_id"] == "123456"
    assert job["state"] == "PENDING"
    assert job["project_id"] == "project-b"
    assert job["script_body_retained"] is False

    refreshed = client.get(
        f"/api/v1/scheduler-jobs/{job['id']}",
        headers={"X-GPU-Broker-Actor": "storyboard-agent"},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["scheduler_job"]["state"] == "RUNNING"
    assert refreshed.json()["scheduler_job"]["allocated_tres"]["gres/gpu"] == "1"
    assert refreshed.json()["scheduler_job"]["node_list"] == "g001"
    assert refreshed.json()["scheduler_job"]["started_at"] == (
        "2026-07-31T00:01:00+00:00"
    )

    cancelled = client.post(
        f"/api/v1/scheduler-jobs/{job['id']}/cancel",
        json={"reason": "user requested stop"},
        headers={
            "X-GPU-Broker-Actor": "storyboard-agent",
            "Idempotency-Key": "cancel-storyboard-smoke",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["scheduler_job"]["state"] == "CANCEL_REQUESTED"
    assert provider.cancellations == ["123456"]


def test_scheduler_refresh_drops_timezone_naive_external_time_without_500(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-naive-time",
        },
    )
    submitted = client.post(
        "/api/v1/scheduler-jobs",
        json={
            "target_id": "hanhai22",
            "project_id": "project-b",
            "task_ref": "naive-slurm-time",
            "purpose": "verify timezone-naive Slurm status is fail-closed",
            "approval_ref": "thread:naive-slurm-time",
            "duration_seconds": 300,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "cpu_cores": 1,
                "memory_mib": 1024,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test",
            },
            "script_body": "true\n",
        },
        headers={
            "X-GPU-Broker-Actor": "storyboard-agent",
            "Idempotency-Key": "submit-naive-time",
        },
    )
    assert submitted.status_code == 200, submitted.text
    broker_job_id = submitted.json()["scheduler_job"]["id"]
    provider.observation = {
        **provider.observation,
        "started_at": "2026-08-04T14:20:00",
        "completed_at": "2026-08-04T14:21:00",
    }

    refreshed = client.get(
        f"/api/v1/scheduler-jobs/{broker_job_id}",
        headers={"X-GPU-Broker-Actor": "storyboard-agent"},
    )

    assert refreshed.status_code == 200, refreshed.text
    job = refreshed.json()["scheduler_job"]
    assert job["state"] == "RUNNING"
    assert job["raw_state"] == "RUNNING"
    assert job["allocated_tres"] == {"cpu": "8", "gres/gpu": "1"}
    assert job["started_at"] is None
    assert job["completed_at"] is None


def test_cpu_only_one_off_is_valid_and_reaches_provider_without_gpu_request(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-hanhai22",
        },
    )
    submitted = client.post(
        "/api/v1/scheduler-jobs",
        json={
            "target_id": "hanhai22",
            "project_id": "project-b",
            "task_ref": "cpu-staging",
            "purpose": "approved CPU and memory staging work",
            "approval_ref": "thread:approved-cpu-staging",
            "duration_seconds": 600,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "cpu_cores": 64,
                "memory_mib": 256 * 1024,
                "nodes": 1,
                "tasks_per_node": 1,
                "working_directory": "/home/test",
            },
            "script_body": "true\n",
        },
        headers={
            "X-GPU-Broker-Actor": "storyboard-agent",
            "Idempotency-Key": "storyboard-cpu-staging-1",
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert provider.submissions[0]["request"]["constraints"]["gpu_count"] == 0
    assert provider.submissions[0]["request"]["scheduler"]["qos"] is None
    assert provider.submissions[0]["request"]["scheduler"]["gpu_type"] is None


def test_cpu_only_scheduler_rejects_gpu_type(
    tmp_path: Path,
    inventory,
) -> None:
    client, _provider = _client(tmp_path, inventory)
    request = {
        "target_id": "hanhai22",
        "project_id": "project-a",
        "task_ref": "invalid-cpu-contract",
        "purpose": "must not combine a CPU-only request with a GPU type",
        "approval_ref": "thread:invalid-cpu-contract",
        "duration_seconds": 600,
        "constraints": {"gpu_count": 0},
        "scheduler": {
            "partition": "CPU-64C256GB",
            "qos": "cpu",
            "gpu_type": "a100",
            "cpu_cores": 64,
            "memory_mib": 256 * 1024,
            "nodes": 1,
            "tasks_per_node": 1,
            "working_directory": "/home/test",
        },
        "script_body": "true\n",
    }
    response = client.post(
        "/api/v1/scheduler-jobs",
        json=request,
        headers={
            "X-GPU-Broker-Actor": "storyboard-agent",
            "Idempotency-Key": "invalid-cpu-contract-1",
        },
    )
    assert response.status_code == 422
    assert "CPU-only Slurm submissions cannot define gpu_type" in response.text


def test_one_off_requires_access_and_does_not_retain_script_by_default(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-hanhai22",
        },
    )
    request = {
        "target_id": "hanhai22",
        "project_id": "project-b",
        "task_ref": "one-off",
        "purpose": "one-off smoke test",
        "approval_ref": "thread:one-off-approved",
        "duration_seconds": 1200,
        "constraints": {"gpu_count": 1},
        "scheduler": {
            "partition": "GPU-8A100",
            "qos": "gpu_8a100",
            "gpu_type": "a100",
            "cpu_cores": 4,
            "memory_mib": 8192,
            "nodes": 1,
            "tasks_per_node": 1,
            "working_directory": "/home/test/one-off",
        },
        "script_body": "set -euo pipefail\nsrun python run.py\n",
    }
    provider.access = {
        "status": "access_required",
        "message": "VPN disconnected",
        "checked_at": "2026-07-31T00:00:00+00:00",
    }
    blocked = client.post(
        "/api/v1/scheduler-jobs",
        json=request,
        headers={
            "X-GPU-Broker-Actor": "one-off-agent",
            "Idempotency-Key": "one-off-1",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "access_required"
    assert client.get(
        "/api/v1/scheduler-jobs",
        headers={"X-GPU-Broker-Actor": "one-off-agent"},
    ).json()["data"] == []

    provider.access = {
        "status": "ready",
        "partitions": [],
        "checked_at": "2026-07-31T00:01:00+00:00",
    }
    submitted = client.post(
        "/api/v1/scheduler-jobs",
        json=request,
        headers={
            "X-GPU-Broker-Actor": "one-off-agent",
            "Idempotency-Key": "one-off-1",
        },
    )
    assert submitted.status_code == 200, submitted.text
    job = submitted.json()["scheduler_job"]
    assert job["approval_ref"] == "thread:one-off-approved"
    assert job["script_body_retained"] is False
    service = client.app.state.service
    with service.database.session() as session:
        stored = session.get(
            SchedulerJob,
            job["id"],
        )
        assert stored is not None
        assert stored.approval_ref == "thread:one-off-approved"
        assert stored.script_body is None
        assert len(stored.script_digest) == 64


def test_access_failure_from_submit_is_reported_without_claiming_gpu(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-hanhai22",
        },
    )

    def fail_submit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SlurmProviderError("VPN route disappeared", access_required=True)

    provider.submit = fail_submit  # type: ignore[method-assign]
    request = {
        "target_id": "hanhai22",
        "project_id": "project-a",
        "task_ref": "route-loss",
        "purpose": "test route loss",
        "approval_ref": "thread:route-loss-approved",
        "duration_seconds": 600,
        "constraints": {"gpu_count": 1},
        "scheduler": {
            "partition": "GPU-8A100",
            "qos": "gpu_8a100",
            "gpu_type": "a100",
            "cpu_cores": 1,
            "memory_mib": 1024,
            "nodes": 1,
            "tasks_per_node": 1,
            "working_directory": "/home/test",
        },
        "script_body": "true\n",
    }
    response = client.post(
        "/api/v1/scheduler-jobs",
        json=request,
        headers={
            "X-GPU-Broker-Actor": "route-loss-agent",
            "Idempotency-Key": "route-loss-1",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "access_required"
    jobs = client.get(
        "/api/v1/scheduler-jobs",
        headers={"X-GPU-Broker-Actor": "route-loss-agent"},
    ).json()["data"]
    assert jobs[0]["state"] == "ACCESS_REQUIRED"
    assert jobs[0]["scheduler_job_id"] is None


def test_scheduler_upload_uses_unique_stage_and_persisted_status(
    tmp_path: Path,
    inventory,
) -> None:
    client, provider = _client(tmp_path, inventory)
    client.post(
        "/api/v1/scheduler-targets",
        json=_upload_target(),
        headers={
            "X-GPU-Broker-Actor": "scheduler-admin",
            "Idempotency-Key": "target-hanhai22-upload",
        },
    )
    source = tmp_path / "dataset.bin"
    source.write_bytes(b"dataset")
    headers = {
        "X-GPU-Broker-Actor": "storyboard-agent",
        "Idempotency-Key": "upload-storyboard-dataset",
    }
    started = client.post(
        "/api/v1/scheduler-transfers",
        json={
            "target_id": "hanhai22",
            "project_id": "project-b",
            "local_path": str(source),
            "remote_directory": "/home/test/staging",
            "approval_ref": "thread:approved-exact-paths",
        },
        headers=headers,
    )
    assert started.status_code == 200, started.text
    transfer_id = started.json()["scheduler_transfer"]["id"]
    for _ in range(50):
        status = client.get(
            f"/api/v1/scheduler-transfers/{transfer_id}",
            headers={"X-GPU-Broker-Actor": "storyboard-agent"},
        )
        if status.json()["scheduler_transfer"]["state"] != "TRANSFERRING":
            break
        time.sleep(0.01)
    transfer = status.json()["scheduler_transfer"]
    assert transfer["state"] == "COMPLETED"
    assert transfer["remote_staged_path"] == (
        f"/home/test/staging/gpu-broker-{transfer_id}/dataset.bin"
    )
    assert transfer["source_size_bytes"] == 7
    assert len(provider.uploads) == 1

    retried = client.post(
        "/api/v1/scheduler-transfers",
        json={
            "target_id": "hanhai22",
            "project_id": "project-b",
            "local_path": str(source),
            "remote_directory": "/home/test/staging",
            "approval_ref": "thread:approved-exact-paths",
        },
        headers=headers,
    )
    assert retried.status_code == 200
    assert retried.json()["scheduler_transfer"]["id"] == transfer_id
    assert len(provider.uploads) == 1
