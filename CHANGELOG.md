# 更新日志

[English](CHANGELOG.en.md)

这里只记录用户能感受到的变化；实现细节见 Git 提交。

## 1.5.2 - 2026-08-13

**ServerPilot 1.5.2 清理了“归属待确认”的误报，并恢复 App 的人工纠错能力。**

- **任务正常重启不再误报归属冲突。** routine Agent 的旧进程已经全部退出、且完整采集确认每张租用 GPU 都出现同一轮新进程时，ServerPilot 自动更新观察归属；新进程只有稳定出现才会触发冲突，完整采集确认重试间隙为空时会保留租约并清除瞬态冲突；旧新进程稳定并存、不完整采集和高级显式 binding 仍然 fail closed。
- **历史错误会真正结束。** 租约释放或失去活动资源后，`lease_process_conflict` 与 `orphaned_busy` 告警立即关闭；升级启动与 reconcile 会清理旧版本遗留的陈旧告警。
- **App 状态与 broker 保持一致。** 原生 App 直接采用 `desired / actual / publicly_available / public_status`，不可申请的卡不再显示“可用”，`desired=ON, actual=OFF` 明确显示“占卡未运行”。
- **人类可以释放孤立的 Agent 租约。** App 使用独立的 operator 纠错路由释放确认已结束的任务；日常 Agent 仍只能释放自己的 lease。

## 1.5.1 - 2026-08-13

**ServerPilot 1.5.1 让 Agent 更容易把 GPU 租约正确转换为远端单卡或多卡工作负载。**

- **单卡进程不再需要猜 selector。** `gpu_apply` 保留既有 `cuda_visible_devices` 完整集合，并在 `gpus[]` 每项新增 `gpu_cuda_visible_devices` 单 UUID，兼容原有多卡调用的同时支持每卡一个进程。
- **远端工作区边界更清楚。** `workspace_path` 明确是所选服务器上的路径；Agent 要先通过当前获授权的远端端点进入，不会再把它当成本地 Codex worktree 路径。
- **运行时失败更快让出 GPU。** 日常契约要求 workload 前先用返回的 selector 做最小 CUDA 初始化；失败立即释放，并在当前任务中记住不兼容的服务器，避免立即重试同一组合。
- **无容量和传输失败不再触发无效轮询。** `no_capacity` 留到下一 turn 或后续工作周期；transport 失败最多重试一次，且不会被误报为容量不足。
- **并行任务的释放责任更明确。** 申请者显式跟踪每个 `lease_id`，即使把执行交给子任务，也要等到每个 lease 明确返回 `released=true`。
