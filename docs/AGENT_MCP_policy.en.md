# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP.

1. `gpu_status(include_busy=false, server_id?)` returns allocatable GPUs plus compact `busy_gpus` naming each busy card's human-readable task. `include_busy=true` adds busy-card telemetry; `server_id` narrows scope.
2. `gpu_apply(server_id?, gpu_count=1, task?)` allocates GPUs; never use a UI title or GPU ID.
3. `gpu_release(lease_id)` releases one allocation, echoing its settled `state`.

Connection and workspace live per server in `servers[]`, not per GPU: `ssh` connects; `workspace.path` (`workspace_path`) is remote cwd, never a repository path. UUIDs identify, never select; `cd` to the workspace. Set `CUDA_DEVICE_ORDER=PCI_BUS_ID`; `cuda_visible_devices` is that server's ordinal set; each `gpus[]` row has its own `gpu_cuda_visible_devices`. CUDA failure requires release and avoiding that server.

`no_capacity` means no allocation or queue; `busy_gpus` names holders. Refresh once, then wait. Retry `Transport closed` once. Leases persist until released or App correction; confirm each `released`.

ServerPilot provides GPU coordination only. Direct SSH is normal; never bypass discovery, selection, allocation, or release via SSH, SQLite, inventory, or `nvidia-smi`. Non-GPU remote work, including Git synchronization, does not require a ServerPilot lease. Advanced compatibility tools are outside the routine path.
