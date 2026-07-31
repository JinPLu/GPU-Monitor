# GPU Broker — Global Agent Adapter

Use the local `gpu-broker` MCP proactively for GPU work; its server instructions
and tool schemas are authoritative. Do not bypass Broker allocation or
freshness/probing through SSH, SQLite, inventory, remote probes, or `nvidia-smi`.
A request or accepted plan to run, continue, or monitor a GPU task authorizes a
routine owner-scoped claim once its `profile_id`, or `project_id`, `gpu_count`,
and needed thresholds are recorded. Reuse that contract without duplicate
questions and never infer missing inputs.

For bare-metal work, use `gpu_claim_profile` or `gpu_claim` → project execution
on `lease.resources[]` → `gpu_bind_observed_workload` → `gpu_release`.
`gpu_coordination` is optional context for ordinary pre-approved profile claims
or explicit-contract claims. A queued/null result cannot be executed; poll the
existing request with `gpu_wait_for_claim` until a `HELD` or `ACTIVE` lease,
terminal state, or timeout. When Broker returns a lease, use only its structured
`lease.resources[]` for placement. Each resource supplies `endpoint` (`id`,
`host`, `port`, `ssh_user`), `gpus` (`id`, `gpu_uuid`, `gpu_index`),
`cuda_visible_devices`, and `commitment`. Broker coordinates allocation and
observed attribution; the Agent may use its project's normal execution path to
start or stop the authorized workload on those resources. Bind it after startup
and release it when done. Supply a caller-stable `idempotency_key` for a retried
mutation. Endpoints are project-owned
(`owner_project_id`) and their lifecycle can move to `draining`; draining blocks
new placement and never stops an existing workload.

The low-level request, activate, release-lease, and bind-workload tools remain
advanced compatibility tools. Prefer the claim / wait / execute / bind /
release path for new Agent work.

Hanhai22 and other external Slurm clusters are SchedulerTargets, never raw SSH
endpoints. Use the distinct scheduler adapter to discover a target and perform
owner-controlled submit, status, and cancel operations. A Slurm `PENDING` job is
not a bare-metal lease; scheduler status and `AllocTRES` establish allocation.
`access_required` means ask the user to connect the approved VPN and retry; do
not automate VPN access. On macOS MCP automatically ensures the shared headless
loopback daemon. If it remains unavailable, report that state rather than using a
bypass.
