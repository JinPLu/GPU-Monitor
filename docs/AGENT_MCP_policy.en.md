# GPU Broker — Global Agent Adapter

Use this block only where the local `gpu-broker` MCP is callable; its server
instructions and tool schemas remain authoritative.

- Treat Broker inspection and coordination as freely schedulable infrastructure
  for GPU-relevant work: call read-only Broker tools proactively without asking.
- A user request or accepted plan to run, continue, or monitor a GPU-dependent
  task authorizes its routine Broker claim once an approved `profile_id`, or
  `project_id` and `gpu_count`, is already recorded in the current task, an
  accepted plan, or a prior successful claim for the same continuing task. Reuse
  that contract; do not stop for duplicate confirmation. Never invent a missing
  project, profile, or GPU count from a directory, task title, free capacity, or
  defaults.
- Call `gpu_claim_profile` for a named enabled profile; otherwise call
  `gpu_claim` as soon as runtime preflight passes. Omit server and exact GPU IDs
  unless explicitly required, so the Broker owns placement and fair queuing.
  If queued, monitor the request and continue when allocated instead of ending
  with a user question. A queued or null lease is not permission to run.
- Use the full approved GPU count when the workload supports it: parallelize
  independent jobs across the lease, without dummy processes or unsafe
  concurrency. Bind the observed workload after startup and release the lease
  after completion or failed startup.
- Use `gpu_coordination` for broker state. Do not bypass the Broker through SSH,
  SQLite, inventory, remote probes, or `nvidia-smi`. Registration, reservations,
  access changes, renewal, cancellation, preemption, and other administrative or
  remote-workload effects still require their own explicit authority.
