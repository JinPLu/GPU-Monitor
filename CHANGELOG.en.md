# Changelog

[中文](CHANGELOG.md)

This changelog records user-visible changes; implementation details belong in Git history.

## 1.5.1 - 2026-08-13

**ServerPilot 1.5.1 makes it easier for Agents to turn GPU leases into correct remote single-GPU and multi-GPU workloads.**

- **Per-GPU processes no longer need to guess a selector.** `gpu_apply` retains the existing complete-set `cuda_visible_devices` and adds one-UUID `gpu_cuda_visible_devices` to each `gpus[]` row, preserving multi-GPU callers while supporting one process per GPU.
- **The remote workspace boundary is explicit.** `workspace_path` is a path on the selected server. An Agent enters it through the currently authorized remote endpoint instead of treating it as a path in the local Codex worktree.
- **Runtime failures yield GPUs sooner.** Agent guidance now requires a minimal CUDA initialization with the returned selector before the workload, immediate release on failure, and avoiding the incompatible server within the current task.
- **Capacity and transport failures no longer invite unproductive polling.** `no_capacity` waits for a later turn or work cycle; a transport failure is retried at most once and is never reported as a capacity shortage.
- **Parallel lease ownership is easier to finish correctly.** The requester explicitly tracks each `lease_id` and waits for `released=true` from every lease, including leases handed to child tasks.
