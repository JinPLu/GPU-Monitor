# GPU Broker — Global Agent Adapter

Use the local `gpu-broker` MCP proactively for GPU coordination; its server
instructions and tool schemas are authoritative. Do not bypass Broker allocation
or freshness/probing through SSH, SQLite, inventory, remote probes, or
`nvidia-smi`. A request or accepted plan to run, continue, or monitor a GPU task
authorizes routine owner-scoped coordination once its `profile_id`, or
`project_id`, `gpu_count`, and needed thresholds are recorded. Reuse that
contract without duplicate questions and never infer missing inputs.

For bare-metal work, use `gpu_coordination` → `gpu_claim_profile` or
`gpu_claim` → execute → `gpu_release`. A queued/null result cannot be executed.
When Broker returns a lease, use only its structured `lease.resources[]` for
placement. Each resource supplies `endpoint` (`id`, `host`, `port`, `ssh_user`),
`gpus` (`id`, `gpu_uuid`, `gpu_index`), `cuda_visible_devices`, and `commitment`.
Broker coordinates allocation and observed attribution; the Agent may use its
project's normal execution path to start or stop the authorized workload on those
resources. Bind it after startup and release it when done. Supply a caller-stable
`idempotency_key` for a retried mutation. Endpoints are project-owned
(`owner_project_id`) and their lifecycle can move to `draining`; draining blocks
new placement and never stops an existing workload.

Hanhai22 and other external Slurm clusters are SchedulerTargets, never raw SSH
endpoints. Use the distinct scheduler adapter to discover a target and perform
owner-controlled submit, status, and cancel operations. A Slurm `PENDING` job is
not a bare-metal lease; scheduler status and `AllocTRES` establish allocation.
`access_required` means ask the user to connect the approved VPN and retry; do
not automate VPN access. On macOS MCP automatically ensures the shared headless
loopback daemon. If it remains unavailable, report that state rather than using a
bypass.
