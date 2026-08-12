# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP. Its three routine tools are the complete normal
GPU path:

1. `gpu_status(include_busy=false)` lists available GPUs with their remote `workspace_path` and a short Chinese `status`. Use
   `include_busy=true` only when you need busy or disconnected rows; an
   occupied row includes its human-readable task.
2. `gpu_apply(server_id?, gpu_count=1, task?)` asks the broker to choose GPUs.
   Set `task` to the user's task name or a short human-readable summary of the
   current goal; do not try to read a client UI title.
   Never provide a GPU ID. Enter the returned `workspace_path`, then use only
   the returned `gpus[]` and `cuda_visible_devices`. `no_capacity` means no
   allocation and no queue.
3. `gpu_release(lease_id)` releases the allocation when the workload stops or
   fails to start.

Routine leases last until explicit release or human correction in the App. Do
not bypass ServerPilot through SSH, SQLite, inventory, or `nvidia-smi`.
Advanced compatibility tools are outside the routine path.
