# Changelog

[中文](CHANGELOG.md)

This changelog records user-visible changes; implementation details belong in Git history.

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
