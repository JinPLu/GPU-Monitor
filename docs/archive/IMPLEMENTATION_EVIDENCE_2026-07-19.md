# 2026-07-19 实施证据归档

这是历史验收记录，不代表当前 GPU 可用性；当前结论见 `../IMPLEMENTATION_STATUS_zh.md`。

## 自动验证

`23 passed, 1 warning`。覆盖 migration、备份恢复、collector、fail-closed 状态、并发申请、API/GUI/MCP/CLI 与 macOS 构建路径；warning 来自 FastAPI/Starlette 测试依赖弃用提示。

## 人工 shadow

固定只读 `collect once` 曾覆盖多个 endpoint 与 GPU；当观察到非托管 compute process 时，控制面正确禁止分配。具体主机、端口、容量与利用数据不进入公开仓库。

## 5 分钟轻量化冒烟

- CPU、内存、snapshot 延迟与体积均完成轻量冒烟；具体环境数据不进入公开仓库。
- SSH 超时时，对应 GPU 被标记为 `UNKNOWN_STALE` 并禁止分配。
- current telemetry 保持每 GPU 一行，历史按分钟留点；正常采样不追加审计，仅 provider 状态切换追加。
- 桌面进程树为原生 App 与 `.venv/bin/gpu-broker` 后端。

## GUI 验收

桌面端已支持 macOS 风格侧栏、系统明暗外观、键盘焦点、reduced motion，以及零 endpoint 首次接入。SSH 输入仅接受 `ssh [-p PORT] USER@HOST`，提交前经过无副作用预览和确认。

该次冒烟不能证明 2 小时内存稳定性或完整 24 小时数据库上限。
