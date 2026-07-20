# GPU Broker — Global Agent Adapter

Install this block only in clients that can call the local `gpu-broker` MCP. The MCP server instructions and tool schemas are the runtime contract; this adapter only supplies routing and safety boundaries.

- Use the local `gpu-broker` MCP only for explicitly requested GPU inspection or coordination; follow its server instructions and tool schemas.
- Never infer broker state or bypass it through SSH, SQLite, inventory, remote probes, or `nvidia-smi`. If the MCP or service is unavailable, report that state and stop.
- Do not infer profile, project, resource, or placement values from repositories, task titles, capacity, inventory, or defaults; use only explicitly authorized task inputs.
- GPU Broker coordinates ownership only. Remote workload actions and administrative changes require separate explicit authorization.
