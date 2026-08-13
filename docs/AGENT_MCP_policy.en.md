# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP.

1. `gpu_status(include_busy=false)` lists GPUs, `ssh`, structured `workspace`, `workspace_path`, and `status`. `include_busy=true` adds a human-readable task.
2. `gpu_apply(server_id?, gpu_count=1, task?)` allocates GPUs. Never read a UI title or give a GPU ID. Treat `ssh` as the connection; `workspace.path` as remote cwd (`kind=working_directory`, `use_as_cwd=true`); `code_location=not_provided` as no repository path; CUDA fields as device selectors. Connect without client host setup, `cd` to the workspace, and keep commands, code synchronization, and artifacts under it. For one endpoint, `cuda_visible_devices` selects the lease; each `gpus[]` row has one-UUID `gpu_cuda_visible_devices`. CUDA gate failure requires release and avoiding that server.
3. `gpu_release(lease_id)` releases an allocation.

`no_capacity` means no allocation or queue. Refresh once, then wait. Retry `Transport closed` once. Track every lease until `released`.

Routine leases require explicit release or App correction. ServerPilot provides GPU coordination only. Direct SSH is normal; never use SSH, SQLite, inventory, or `nvidia-smi` to bypass discovery, selection, allocation, or release. Authorized non-GPU remote work, including Git synchronization, does not require a ServerPilot lease. Advanced compatibility tools are outside the routine path.
