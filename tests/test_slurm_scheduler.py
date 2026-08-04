from __future__ import annotations

from pathlib import Path
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
from gpu_broker.slurm import CommandSlurmProvider, SlurmProviderError, SlurmSubmission


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


def test_command_slurm_cpu_only_submission_omits_gpu_gres() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="123456\n", stderr="")

    provider = CommandSlurmProvider(runner=runner)
    submission = provider.submit(
        {"command_prefix": ["/Users/test/.local/bin/hh22", "1"]},
        broker_job_id="cpu-only-job",
        request={
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 0},
            "scheduler": {
                "partition": "CPU-64C256GB",
                "qos": "cpu",
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
    assert "--partition=CPU-64C256GB" in remote_command
    assert "--cpus-per-task=64" in remote_command
    assert "--mem=262144M" in remote_command
    assert "--gres=" not in remote_command


def test_command_slurm_gpu_submission_includes_gres() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="123456\n", stderr="")

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
    assert "--gres=gpu:a100:1" in calls[0][-1]


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
                "qos": "cpu",
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
                "qos": "cpu",
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
