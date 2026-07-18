<p align="center">
  <img src="desktop/assets/GPU%20Broker%20Icon.png" width="112" alt="GPU Broker app icon">
</p>

<h1 align="center">GPU Broker</h1>

<p align="center">
  给小团队用的一台本机 GPU 协作控制面：粘贴 SSH 地址、看清集群状态、认领或排队。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-13%2B-2563EB" alt="macOS 13+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2563EB" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/assets/dashboard.jpg" width="900" alt="GPU Broker cluster scheduling dashboard">
</p>

## 开始使用

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、系统 `ssh`，以及可访问且已安装 `nvidia-smi` 的 GPU 服务器。

```bash
uv sync --python 3.12 --no-editable
uv run --no-editable gpu-broker init
uv run --no-editable gpu-broker serve
```

打开 [http://127.0.0.1:8787/](http://127.0.0.1:8787/)。服务默认只监听本机。

## 添加服务器

在首页粘贴常见 SSH 地址即可，例如：

```text
ssh -p 4482 root@10.40.1.222
```

GPU Broker 只解析并预览这条地址；确认后才登记服务器，并只运行固定的只读状态采集。

## 选择入口

| 需求 | 入口 |
| --- | --- |
| 在浏览器调度集群 | [本机控制台](http://127.0.0.1:8787/) |
| 用 macOS 独立窗口 | [`GPU Broker.app`](./GPU%20Broker.app) |
| 让 Agent 安排 GPU | [MCP 指南](docs/AGENT_MCP_zh.md) |

构建 macOS 应用：

```bash
zsh desktop/build-macos-app.sh
open "GPU Broker.app"
```

## 安全边界

- 租约只协调归属，不会启动、停止或抢占远端任务。
- SSH Collector 不读取私钥、完整命令、环境或任务数据，也不修改远端。
- telemetry 异常、非托管进程、维护或冲突时一律拒绝分配。

实现状态见 [实施状态](docs/IMPLEMENTATION_STATUS_zh.md)。开发与测试：

```bash
uv run --extra dev --no-editable pytest
uv run --extra dev --no-editable ruff check .
```
