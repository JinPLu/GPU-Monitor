<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="96" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center">
  <strong>Agent 自己拿卡，人类实时监控。</strong><br>
  MCP for Agent · Native Mac App · Open Source
</p>

<p align="center">
  <a href="#-核心功能">核心功能</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-agent-用法">Agent 用法</a> ·
  <a href="#-边界与安全">边界与安全</a> ·
  <a href="#-文档">文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-native%20App-111827?logo=apple&logoColor=white" alt="Native macOS App">
  <img src="https://img.shields.io/badge/MCP-3%20routine%20tools-7C3AED" alt="Three routine MCP tools">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/assets/serverpilot-workflow-cartoon.png" width="960" alt="ServerPilot：将零散 GPU 汇总为可供 Agent 协作的资源池，并由人类监控">
</p>

<p align="center"><sub>概念示意图，不代表真实资源状态或 App 页面。</sub></p>

> 🤖 Agent 会写代码、跑实验了，GPU 还需要一张张指定吗？

对 Agent，ServerPilot 是一个 MCP：查卡、申请、归还。对人，它是一个 Mac App：看多台服务器的空闲、占用、任务和异常。

## ✨ 核心功能

### 🛰️ 人类实时监控：多台服务器，一张图

按设置的采集间隔更新服务器状态；GPU 空闲、占用、任务归属和采集异常，都在 App 里直接看。

### 🧩 Agent 自主调度：有空卡，Agent 自己领

默认 MCP 只有三个日常工具：

- 🔍 `gpu_status`：查看可用 GPU 及最近显存、利用率遥测
- 🔑 `gpu_apply`：申请 GPU
- ♻️ `gpu_release`：明确归还

申请成功后，Agent 会拿到 SSH 连接、远端工作目录、CUDA selector 和 `lease_id`，不用再猜服务器、目录或 GPU 编号。

### 🛠️ 人类纠错反馈：平时不打扰，需要时再纠正

Agent 正常干活时，人只看全局。发现归属不对、连接异常或遗留占用，再人工确认和纠正；可处理遗留租约，或在保持卡数不变的前提下改派 GPU。

### 🐶 一个小彩蛋：空闲 GPU，先占着（可选）

逐卡 keepalive 可让确认空闲的 GPU 按策略保持待命。Agent 申请时只让出目标卡，用完归还后再恢复待命，不影响同机其他 GPU。

只用于你有管理权限的服务器，不碰未知或他人正在运行的任务。

## 🚀 快速开始

需要 [Python 3.12+](https://www.python.org/)、[uv](https://docs.astral.sh/uv/) 和一台 macOS 主机。

### 1. 🧰 启动本机控制面

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

服务默认监听 `http://127.0.0.1:8787`。

### 2. 🖥️ 登记你的 GPU 服务器

在有管理权限的服务器部署同版本采集入口，并保证非交互 SSH 可以执行：

```text
serverpilot-collect --schema-version 2
```

在 App 中添加 SSH 连接和绝对远端工作目录。新鲜采集成功后，GPU 才进入可申请状态。详细要求见[服务器采集协议](docs/COLLECTOR_SCRIPT_zh.md)。

### 3. 🤖 接入 Agent

以 Codex 为例：

```bash
codex mcp add serverpilot \
  --env SERVERPILOT_URL=http://127.0.0.1:8787 \
  -- serverpilot-mcp
python3 scripts/install_agent_policy.py codex --install
```

Claude Code 与 Cursor 的接入方式见 [Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

## 🤖 Agent 用法

```text
gpu_status → gpu_apply(task="任务名") → 使用返回的分配 → gpu_release(lease_id)
```

- Agent 不传 GPU ID，`gpu_apply` 负责选卡。
- `gpu_status` 的逐卡 `telemetry` 同时包含最近观测与近 10 分钟平均资源使用；GUI 与 MCP 都读取 daemon 的同一份 REST 快照，不会重复 SSH 采集。首次只读采集会把端点记录为 GPU、纯 CPU 或尚未确认；纯 CPU 服务器保留 CPU/内存监控，并以说明性的 `cpu_only_servers` 返回给 Agent，但不会参与 GPU 分配。汇总 `telemetry_summary` 的 `current_average_*` 只表示当前瞬时样本的跨卡平均；它们不决定可申请性，仍以 `status` 和 `gpu_apply` 为准。
- SSH 后先进入返回的 `workspace.path`，再使用 CUDA selector。`workspace.path` 是远端工作目录，不是代码仓库。
- CUDA 初始化或 workload 启动失败时，立即 `gpu_release`。
- `no_capacity` 表示不分配、不排队；不要在同一轮反复申请。

## 🖥️ 打开 Mac App

```bash
zsh desktop/build-macos-app.sh
open "./ServerPilot.app"
```

App 负责看状态、看归属、看异常。人工改派只更新 lease 和 CUDA selector，不迁移正在运行的进程；对应 Agent 需按新 selector 重启 workload。

## 🛡️ 边界与安全

- ServerPilot **只协调 GPU 归属**，不替项目启动、停止、迁移或抢占 workload。
- 服务器状态只来自固定的只读 SSH 采集入口；不接收任意远端命令，也不提供密码或私钥。
- 采集过期、连接异常、未知进程或资源冲突时，一律拒绝分配（fail closed）。
- 控制面默认只监听本机 loopback；GPU UUID 与 endpoint 是资源身份边界。

## 📚 文档

- [Agent / MCP 指南](docs/AGENT_MCP_zh.md)
- [服务器采集协议](docs/COLLECTOR_SCRIPT_zh.md)
- [keepalive 与 Adapter](docs/ADAPTERS_zh.md)
- [当前实现与验证状态](docs/IMPLEMENTATION_STATUS_zh.md)
- [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

## License

[MIT](LICENSE)
