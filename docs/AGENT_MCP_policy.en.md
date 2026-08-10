# ServerPilot — Global Agent Scheduling Policy

Use the local `serverpilot` MCP for server-compute coordination. Existing
installations may still expose the compatible `gpu-broker` MCP name; its tool
schemas and ServerPilot's server instructions remain authoritative.

A routine owner-scoped claim is allowed only when the task already records its
approved `profile_id`, or its complete explicit `project_id`, `gpu_count`, task
reference, and resource thresholds. Never infer missing inputs.

## Normal bare-metal path

1. When the task already records a `profile_id`, call `gpu_claim_profile`.
   Otherwise, when it records `project_id`, `gpu_count`, task reference, and
   every required CPU, memory, and VRAM threshold, call `gpu_claim`.
2. A `queued` or null result grants nothing. Continue the *same* request with
   `gpu_wait_for_claim` until it reaches `HELD` or `ACTIVE`, a terminal state,
   or the task's timeout. Do not create duplicate requests or ask the human
   again for an unchanged, already-approved contract.
3. Use only returned `lease.resources[]` for placement, including its
   `cuda_visible_devices` selector. Never infer a host, GPU index, CUDA
   selector, or free capacity from inventory, IP addresses, process lists, or a
   previous snapshot.
4. The project may start its already-authorized workload through the project's
   normal execution path. ServerPilot never launches, stops, or preempts workloads.
   Call `gpu_bind_observed_workload` after it starts, then `gpu_release` after
   completion or a failed start.
5. Reuse one caller-stable `idempotency_key` for every retry of a mutation.

`control_plane_state`, `gpu_status`, `gpu_list`, `gpu_who`, and
`gpu_coordination` are observation tools, not an admission calculation. The
service's availability projection is authoritative: stale, unknown, unmanaged,
conflicting, or maintained resources are not allocatable. Agents must not turn
`capacity - used` into a placement decision.

## CPU, memory, and human oversight

For CPU/memory-only or mixed compute work, state the smallest explicit resource
candidates and use `resource_evaluate_plan` before `resource_claim`. A
`gpu_count=0` contract is valid when no GPU is required. Use
`resource_record_actual` after work completes so people can compare requested
and observed demand; `resource_release` ends the claim. These tools coordinate
resource ownership only and never run a remote command.

Humans supervise through ServerPilot's resource, work, queue, and history
views. Agents should report the request/lease or scheduler job identifier,
terminal state, and any blocked reason in the task outcome. They must not add,
update, pause, resume, retire, drain, or reconfigure servers unless the current
task explicitly gives that local inventory action. Every endpoint administration
MCP mutation also requires a non-empty current-task `approval_ref` and a
caller-stable, non-empty `idempotency_key`; `owner_project_id` is attribution,
not an Agent management permission. Pause moves `active` to `draining`, blocks
new placement, and keeps collection and current leases running. Resume moves
`draining` to `active`. Explicit retire is allowed only from `draining` once
active leases and endpoint-pinned queued requests are clear; it retains identity
and evidence. Deprecated `gpu_delete_server` is pause only and never auto-retires.

## External schedulers

External Slurm clusters are `SchedulerTarget`s, never raw SSH endpoints.
Discover the target with `gpu_scheduler_targets`, check the selected target with
`gpu_scheduler_access_status`, and use the scheduler tools. A Slurm `PENDING`
job is not a bare-metal lease; scheduler state and `AllocTRES` establish the
allocation. `access_required` means ask the user to connect the approved VPN;
never automate VPN access or fall back to SSH.

Connection metadata can select only a sealed transport and read-only inspection
profile. It cannot contain an executable path, argv, shell fragment, SSH option,
secret, or arbitrary probe. An endpoint likewise selects a sealed observation
profile. New REST/MCP endpoint creation defaults to `server-script-v1`; an
existing inventory/profile record that omits selection remains compatible with
`linux-nvidia`. `server-script-v1` always maps to the deployment-owned read-only
`serverpilot-collect --schema-version 1` protocol; the endpoint record never
accepts an executable or command configuration. Agents do not select or alter
either profile without the explicit administration authorization above.

`gpu_scheduler_submit_profile` follows the approved profile. A one-off submit
requires the exact script/resource contract and a current-task `approval_ref`.
Cancellation is a separate, explicit user-authorized action. If the MCP,
daemon, or scheduler access is unavailable, report that state and stop rather
than bypassing ServerPilot via SSH, SQLite, inventory, remote probes, or
`nvidia-smi`.

The low-level request, activate, release-lease, and bind-workload tools are
advanced compatibility tools. New work follows claim → wait → execute → bind →
release.
