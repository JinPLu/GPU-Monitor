# GPU Broker — Global Agent Adapter

Install this short block in a client's global rules when the local `gpu-broker` MCP is available. The MCP server's `instructions` are the runtime contract; this block keeps client routing and safety behavior consistent without adding project-local GPU instructions.

- Use `gpu-broker` MCP only when the user asks to inspect or coordinate GPU resources. Do not read SQLite, inventory, SSH endpoints, or remote GPU probes to infer broker state.
- For an allocation request, require explicit user authorization, `project_id`, task, purpose, `gpu_count`, and `hours`; add exact `gpu_ids` only when requested. Do not infer values from a repository, directory, task title, or defaults.
- Call `gpu_claim` atomically. A queued response or `lease: null` is not permission to run; use only GPUs in a returned held or active lease.
- For an existing lease, activate before work when available and call `gpu_release` after the workload stops (or failed startup) with the same `agent_name` and `lease_id` wherever those fields are accepted, plus a reason. Cancel only an explicitly abandoned request. Renew only with explicit, bounded authorization.
- Reservations, server registration, and workload binding are separate administrative operations and require separate explicit authorization.
- GPU Broker coordinates ownership only; it does not authorize, start, stop, or preempt remote work. If MCP or the service is unavailable, report that state and do not fall back to SSH, inventory, SQLite, or `nvidia-smi`.
- The loopback actor label is an audit identity, not authentication or proof of project authorization.
