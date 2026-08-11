# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP for compute coordination. Its tool schemas and
server instructions are authoritative.

## Routine GPU work

1. Call `gpu_apply` directly. Read `gpu_status` only when you need to inspect
   availability or diagnose placement; the legacy `gpu_list` read is available
   in the advanced profile. The broker chooses the actual GPU and does not consume a preflight result. `server_id` is optional
   and `gpu_count` defaults to one.
   ServerPilot derives routine attribution and selects the actual allocatable
   GPU or GPUs; the Agent does not provide a project, task, profile, GPU ID, or
   capacity calculation.
2. On success, use only the returned `lease.resources[]`, including
   `cuda_visible_devices`, for the already-authorized workload.
3. After it starts, call `gpu_bind_observed_workload` with the returned
   `lease_id`; `run_id` is optional. This confirms the observed workload is
   attached to that exact lease.
4. Call `gpu_release` when that workload stops or fails to start.

`no_capacity` creates no queue and grants no permission to run. Reuse the same
`idempotency_key` for a retried mutation. Availability and coordination are
observations, not an admission calculation: never infer a placement from names,
inventory, process lists, or apparent free capacity, and never bypass
ServerPilot through SSH, SQLite, inventory, or `nvidia-smi`.

## Compatibility and administration

Administrative, generic-resource, workload-binding, and scheduler operations
remain compatibility surfaces; they are not normal routine-GPU inputs. Endpoint
changes, keepalive changes, and scheduler-job cancellation require the current
task's explicit human authorization, a non-empty `approval_ref`, and a stable
idempotency key.

External clusters are `SchedulerTarget`s, not SSH endpoints. Use scheduler
tools; if access is required or the MCP is unavailable, report that state and
stop rather than bypassing it.

`codex://threads/<uuid>` is an opaque coordination handoff reference, not a
URL that ServerPilot opens or resolves. If a host maintains an MCP allowlist,
refresh it after upgrades so newly exposed routine tools are not hidden. The
default stdio server exposes routine GPU tools; set
`SERVERPILOT_MCP_PROFILE=advanced` for scheduler, compatibility resource,
endpoint, or low-level lease tools.
