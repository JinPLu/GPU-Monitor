# Changelog

[中文](CHANGELOG.md)

This changelog records user-visible changes; implementation details belong in Git history.

## 1.5.9 - 2026-08-15

**ServerPilot 1.5.9 provides Windows users with a complete desktop App that follows the same resource workflow as macOS.**

- The Windows app uses a system WebView2 desktop window rather than opening an external browser. Overview, search, filters, header sorting, GPU claims, server registration, occupancy control, and collector settings use a narrow local bridge to the same loopback control plane.
- The server table fills the available window width, keeps GPU Configuration toward the left, and displays and sorts GPU utilization, memory utilization, CPU load, and system-memory utilization with the same rolling ten-minute basis.
- Windows server details reuse the macOS per-GPU memory rings and free / occupancy / busy / error labels, plus a 2×2 CPU, memory, GPU utilization, and memory-history layout.
- Each GitHub Release is built on a Windows runner and receives a `ServerPilot-*-windows-x64.zip` asset, so Windows users do not need to install Python or uv.

## 1.5.8 - 2026-08-15

**ServerPilot 1.5.8 makes server resource summaries and desktop GPU details more consistent and readable.**

- GPU utilization, memory utilization, normalized CPU load, and system-memory utilization in the server overview now use the same rolling ten-minute observation window. Endpoint snapshots add `host_telemetry.recent_average`, and an older local service is no longer treated as compatible.
- The resource table fills its available width: Project / Current Task absorbs the spare space, GPU Configuration stays toward the left, and the four resource columns use full professional labels and one shared ten-minute sort basis.
- Server details return to compact horizontal per-GPU cards. Each ring shows current memory use, while a small state label distinguishes free, occupancy, busy, and error; resource history remains a fixed 2×2 chart layout.

## 1.5.7 - 2026-08-15

**ServerPilot 1.5.7 makes GPU and CPU-only server resource states appear consistently from one live snapshot.**

- `gpu_status` returns each GPU's latest observation and a rolling ten-minute average of memory, GPU/memory-controller utilization, and temperature, alongside a summary of the visible cards to distinguish sustained load from a momentary spike.
- The GUI and MCP share the daemon REST snapshot rather than collecting over SSH separately; the GPU detail view displays that same per-GPU average.
- A new server's first read-only collection identifies it as GPU, CPU-only, or unconfirmed. Confirmed CPU-only servers retain CPU/memory monitoring and are explicitly shown in the GUI and `gpu_status.cpu_only_servers`, but are never GPU allocation targets.

## 1.5.6 - 2026-08-15

**ServerPilot 1.5.6 fixes idle keepalive workers being misreported as running workloads after a task releases its GPU lease.**

- The routine Agent contract is unchanged: a finished task calls `gpu_release`; ServerPilot restores idle keepalive itself and the Agent never turns the policy off.
- The helper now provides read-only proof for its own recorded v3 workers, including the sole driver-visible PID on the target GPU. The Broker rebinds a worker only when that sealed proof matches a fresh collector PID/boot observation, covering worker and daemon restarts without adopting arbitrary processes.
- Mismatched proof, damaged state, or an additional workload process remain fail-closed. Agents see a precise occupancy-verification failure rather than the misleading “task in use” label.
- A verified keeper stop clears its previous process identity so the next worker cannot be compared to a stale PID.

## 1.5.5 - 2026-08-14

**ServerPilot 1.5.5 upgrades the keepalive protocol to v3 and tightens GPU/control-plane reliability.**

- The keepalive adapter performs a read-only `--protocol-info` preflight before every mutation and requires v3, pidfd identity, and PCI bus ID capabilities; incompatible helpers return `keepalive_helper_incompatible` without receiving a mutation payload.
- Keepalive wire/state use v3 and `workers.v3.json`; v2 payloads/state are rejected fail-closed and are never adopted, deleted, or signaled.

## 1.5.4 - 2026-08-14

**ServerPilot 1.5.4 fixes device selection across GPU and driver environments and tightens occupancy and control-plane reliability.**

- `gpu_index` remains the server's display index, while collector schema v2 derives a separate PCI-bus-ordered `cuda_ordinal`. `gpu_apply` returns `cuda_device_order=PCI_BUS_ID`, a lease-wide ordinal set, and per-GPU ordinals instead of placing GPU UUIDs in `CUDA_VISIBLE_DEVICES`.
- GPUs without a current CUDA ordinal are not allocated. Old collector schemas and PID-only occupancy state fail closed instead of being adopted or downgraded.
- Occupancy workers persist PID, Linux boot ID, process start ticks, and a fixed marker. Stops pin the process with pidfd; endpoints whose Python lacks pidfd wrappers use the Linux pidfd syscalls and never fall back to signaling a bare PID.
- The release also fixes actual request-body limiting and disconnect forwarding, concurrent rate limiting, Web CSRF, CSV field projection, and atomic SQLite backup. Routine reassignment remains lease-owner-only, with a separate App operator correction route.
- CPU and memory admission accounts for both direct GPU commitments and generic host claims. Routine MCP transport retries use a process-scoped call namespace without collapsing later same-parameter claims into an old lease.

## 1.5.3 - 2026-08-14

**ServerPilot 1.5.3 lets Agents connect directly without conflating a working directory with a code path.**

- Routine `gpu_status` and `gpu_apply` return structured `ssh {host, port, user}` data. After allocation, Agents use direct SSH for the workload instead of treating a missing Codex saved host as a missing server.
- Clarified that `workspace_path` is the remote working directory for post-SSH operations, not a source repository path. Agents enter it first, then run commands and synchronize code or artifacts beneath it.
- Added machine-readable `workspace {path, kind=working_directory, use_as_cwd=true, code_location=not_provided}` data, separating connection details, the working directory, code location, and CUDA selectors while retaining legacy `workspace_path`.
- Agent guidance now distinguishes normal SSH execution from bypassing ServerPilot for GPU discovery, selection, allocation, or release.

## 1.5.2 - 2026-08-13

**ServerPilot 1.5.2 removes false ownership-conflict errors and restores human correction in the App.**

- **Routine workload restarts no longer become false conflicts.** When every old process has exited and one complete observation sees a wholly new cohort on every leased GPU, ServerPilot refreshes the routine Agent's observed ownership. A newcomer must itself be observed repeatedly before it can trigger conflict, while a complete empty retry window keeps the lease and clears the transient conflict. Stable mixed cohorts, incomplete observations, and advanced explicit bindings remain fail closed.
- **Historical errors actually end.** Releasing a lease or removing its active resources closes `lease_process_conflict` and `orphaned_busy`; startup and reconciliation repair stale alerts left by older versions.
- **The App follows the broker's canonical status.** The native App consumes `desired / actual / publicly_available / public_status`, never labels an unavailable GPU as available, and shows `desired=ON, actual=OFF` as occupancy not running.
- **Humans can release orphaned Agent leases.** The App uses a dedicated operator correction route after confirming a task has ended; routine Agents remain limited to their own leases.

## 1.5.1 - 2026-08-13

**ServerPilot 1.5.1 makes it easier for Agents to turn GPU leases into correct remote single-GPU and multi-GPU workloads.**

- **Per-GPU processes no longer need to guess a selector.** `gpu_apply` retains the existing complete-set `cuda_visible_devices` and adds one-UUID `gpu_cuda_visible_devices` to each `gpus[]` row, preserving multi-GPU callers while supporting one process per GPU.
- **The remote workspace boundary is explicit.** `workspace_path` is a path on the selected server. An Agent enters it through the currently authorized remote endpoint instead of treating it as a path in the local Codex worktree.
- **Runtime failures yield GPUs sooner.** Agent guidance now requires a minimal CUDA initialization with the returned selector before the workload, immediate release on failure, and avoiding the incompatible server within the current task.
- **Capacity and transport failures no longer invite unproductive polling.** `no_capacity` waits for a later turn or work cycle; a transport failure is retried at most once and is never reported as a capacity shortage.
- **Parallel lease ownership is easier to finish correctly.** The requester explicitly tracks each `lease_id` and waits for `released=true` from every lease, including leases handed to child tasks.

## 1.5.0 - 2026-08-12

**ServerPilot 1.5.0 makes routine GPU coordination and native monitoring more direct.**

- **Idle occupancy is managed per GPU.** Each endpoint retains one persistent policy switch while ServerPilot manages one worker per eligible idle GPU. A claim yields only the selected workers and leaves other GPUs untouched.
- **Capacity failure is explicit.** A direct GPU request returns a lease immediately or `no_capacity`; it does not create a hidden queue. Agents use only the returned endpoint, GPUs, and CUDA selector.
- **Projects can declare resource cards.** A validated `.serverpilot/resource-card.json` stores a direct-GPU preset contract so Agents do not infer configuration from task names or current free capacity.
- **The App focuses on daily monitoring.** The native UI is organized around Servers, Usage, and Settings, with shared server resources, current projects and tasks, per-GPU state, and history.
- **Ownership, collection, and occupancy remain fail closed.** Stale, unknown, unmanaged, conflicting, or maintained GPUs are never projected as claimable.

## 1.4.0 - 2026-08-10

**ServerPilot 1.4.0 establishes a unified resource control plane and a standalone native desktop experience.**

- **GPU, CPU, memory, and external scheduler targets share resource contracts.** The service computes capacity, used, claimed, and available once; the GUI, CLI, and MCP no longer derive availability independently.
- **Server collection gains a constrained script protocol.** `server-script-v1` accepts validated read-only snapshots from a fixed entry point; missing, oversized, or malformed output fails closed.
- **The native App provides a complete resource overview.** Servers are searchable, filterable, and sortable, with per-endpoint 1h / 6h / 24h histories for CPU, memory, GPU utilization, and GPU memory.
- **The daemon outlives the App.** A user LaunchAgent owns durable state, closing the GUI does not stop the control plane, and the App bundles its backend and migrations for standalone build verification.
- **Ownership and external scheduling boundaries are explicit.** Projects, Agents, tasks, empty leases, and queues share one projection, while Slurm remains a separate constrained adapter instead of masquerading as a direct GPU server.

## 1.3.0 - 2026-08-10

**ServerPilot 1.3.0 removes server- and cluster-specific behavior from runtime policy.**

- Endpoints use fixed `observation_profile` values, while external schedulers use constrained transport and inspection profiles.
- A local administrator maps profiles to trusted absolute-path wrappers; the API, App, and MCP cannot submit arbitrary shell, argv, or environment values.
- Unknown or missing profiles fail closed. Legacy scheduler command configuration is disabled during upgrade until an administrator selects a safe profile.
- Documentation and runtime guidance now describe a generic adapter model instead of naming one cluster.

## 1.2.0 - 2026-08-10

**GPU Broker becomes ServerPilot.**

- The GitHub repository, macOS and Windows Apps, Web UI, documentation, and public API title adopt the ServerPilot name.
- New `serverpilot` and `serverpilot-mcp` command entry points are available.
- The old `gpu-broker`, `gpu-broker-mcp`, `GPU_BROKER_*`, daemon identity, and data directories remain compatible, preserving inventory, history, leases, and MCP registrations across the upgrade.
- `/api/v1/state` and existing scheduler semantics remain compatible.

## 1.1.0 - 2026-08-10

**GPU Broker 1.1.0 expands server telemetry and native resource monitoring.**

- Sealed observation and scheduler adapter boundaries are introduced without opening another authentication or remote-command control plane.
- CPU, memory, GPU, claim, and availability projections fail closed, while CPU-only endpoints remain observable without fabricated GPU capacity.
- Bounded endpoint telemetry history includes stable per-GPU UUID series.
- The macOS resource workflow adds on-demand CPU, memory, GPU utilization, and GPU-memory charts with lower-cost hover rendering.
- `/api/v1/state` remains the authoritative allocation snapshot, preserving existing leases, CLI, MCP, and Slurm semantics.

## 1.0.0 - 2026-08-06

**The first stable GPU Broker release.**

- The App, CLI, and MCP share the authoritative `/api/v1/state` snapshot.
- The native macOS App coordinates servers, projects, and resource state by stable ID; removing a server updates related views together.
- Revisions and resource usage come from one committed control-plane snapshot, reducing divergence between views and clients.
- Loopback REST and the domain service are the only public business path.
