# GPU Broker — Global Agent Adapter

Install this block only in clients that can call the local `gpu-broker` MCP. The MCP server's `instructions` are the runtime contract; this adapter keeps routing simple.

- Use `gpu-broker` only for user-authorized GPU inspection or coordination. Do not infer broker state from SQLite, inventory, SSH endpoints, remote probes, or `nvidia-smi`.
- Daily workflow:
  1. Read `gpu_coordination` only when shared state is needed.
  2. If the user names a `profile_id`, call `gpu_claim_profile`.
  3. Otherwise call `gpu_claim` only when `project_id`, task, `gpu_count`, and any needed CPU, system memory, or VRAM thresholds are explicitly authorized as absolute values.
  4. After an authorized workload starts, call `gpu_bind_observed_workload`; when it stops or fails startup, call `gpu_release`.
- Do not choose `profile_id`, `project_id`, `gpu_count`, CPU cores, memory MiB, VRAM MiB, server placement, or exact `gpu_ids` from a repository, directory, task title, free capacity, inventory, or defaults. Any non-empty `project_id` is accepted directly and needs no pre-registration.
- Prefer right-sized absolute resource requests so agents can fully use shared server compute after a lease is granted.
- Let the Broker place routine claims unless the user explicitly names a server or exact GPUs. A queued response or `lease: null` is not permission to run; use only GPUs in a returned held or active lease.
- GPU Broker coordinates ownership only. It never authorizes, launches, stops, changes, or preempts remote work.
- Reservations and server registration/deletion are admin actions requiring separate explicit authorization.
- If MCP or the service is unavailable, report that state and stop; do not fall back to out-of-band GPU inspection.
