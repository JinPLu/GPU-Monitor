# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP. Its three tools are the normal GPU path:

1. `gpu_status(include_busy=false)` lists available GPUs with remote `workspace_path` and short Chinese `status`. Use
   `include_busy=true` for busy or disconnected rows; occupied rows include a
   human-readable task.
2. `gpu_apply(server_id?, gpu_count=1, task?)` asks the broker to choose GPUs.
   Set `task` to the task name or a short human-readable goal; do not read a
   client UI title.
   Never provide a GPU ID. Enter the returned `workspace_path`, then use only
   the returned `gpus[]` and `cuda_visible_devices`. `no_capacity` means no
   allocation and no queue.
3. `gpu_release(lease_id)` releases the allocation when the workload stops or fails to start.

Routine leases last until explicit release or human correction in the App.
ServerPilot governs GPU coordination only: do not use SSH, SQLite, inventory,
or `nvidia-smi` to discover, select, claim, or release GPUs outside the broker.
Authorized non-GPU remote work—including Git synchronization, maintenance, and
read-only checks—does not require a ServerPilot lease, but still requires a
current authorized endpoint. Advanced compatibility tools are outside the routine path.
