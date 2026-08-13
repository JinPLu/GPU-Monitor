# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP. Routine GPU tools:

1. `gpu_status(include_busy=false)` lists available GPUs, remote `workspace_path`, and Chinese `status`. `include_busy=true` adds unavailable rows and their human-readable task.
2. `gpu_apply(server_id?, gpu_count=1, task?)` allocates GPUs. Give a human-readable task; never read a UI title or provide a GPU ID. Enter remote `workspace_path` through the authorized endpoint, not the local worktree. For one endpoint, `cuda_visible_devices` selects the lease; each `gpus[]` row adds one-UUID `gpu_cuda_visible_devices`. Use the former for multi-GPU and the latter per GPU. Run a minimal CUDA gate; on failure, release and avoid that server for this task.
3. `gpu_release(lease_id)` releases a stopped or failed allocation.

`no_capacity` means no allocation or queue. Refresh once, then wait. Retry `Transport closed` once. Track each lease until `released`.

Routine leases require explicit release or App correction. ServerPilot provides GPU coordination only: no bypass via SSH, SQLite, inventory, or `nvidia-smi`. Authorized non-GPU remote work, including Git synchronization, does not require a ServerPilot lease but requires an authorized endpoint. Advanced compatibility tools are outside the routine path.
