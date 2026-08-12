# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP. Its three routine tools are the complete normal
GPU path:

1. `gpu_status()` lists available GPUs with their remote `workspace_path` and a short Chinese `status`. Use
   `include_busy=true` only when you need busy or disconnected rows; an
   occupied row also includes its task and `agent_url`.
2. `gpu_apply(server_id?, gpu_count=1, task?)` asks the broker to choose GPUs.
   Never provide a GPU ID. Enter the returned `workspace_path`, then use only
   the returned `gpus[]` and `cuda_visible_devices`. `no_capacity` means no
   allocation and no queue.
3. `gpu_release(lease_id)` releases the allocation when the workload stops or
   fails to start.

Codex must forward its existing `CODEX_THREAD_ID` to the MCP process. ServerPilot
then records `codex://threads/<uuid>` for contact. If the variable is missing,
apply and release stop; do not invent a fallback identity or URL.

Routine leases last until explicit release or human correction in the App. Do
not bypass ServerPilot through SSH, SQLite, inventory, or `nvidia-smi`.
Advanced compatibility tools are outside the routine path.
