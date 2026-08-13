# 更新日志

[English](CHANGELOG.en.md)

这里只记录用户能感受到的变化；实现细节见 Git 提交。

## 1.5.1 - 2026-08-13

**ServerPilot 1.5.1 让 Agent 更容易把 GPU 租约正确转换为远端单卡或多卡工作负载。**

- **单卡进程不再需要猜 selector。** `gpu_apply` 保留既有 `cuda_visible_devices` 完整集合，并在 `gpus[]` 每项新增 `gpu_cuda_visible_devices` 单 UUID，兼容原有多卡调用的同时支持每卡一个进程。
- **远端工作区边界更清楚。** `workspace_path` 明确是所选服务器上的路径；Agent 要先通过当前获授权的远端端点进入，不会再把它当成本地 Codex worktree 路径。
- **运行时失败更快让出 GPU。** 日常契约要求 workload 前先用返回的 selector 做最小 CUDA 初始化；失败立即释放，并在当前任务中记住不兼容的服务器，避免立即重试同一组合。
- **无容量和传输失败不再触发无效轮询。** `no_capacity` 留到下一 turn 或后续工作周期；transport 失败最多重试一次，且不会被误报为容量不足。
- **并行任务的释放责任更明确。** 申请者显式跟踪每个 `lease_id`，即使把执行交给子任务，也要等到每个 lease 明确返回 `released=true`。
