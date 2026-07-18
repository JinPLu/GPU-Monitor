<h1 align="center">GPU Broker</h1>

<p align="center">
  让人类和 Agent 安全地共享 SSH GPU 服务器：查看状态、认领、排队、预约和释放，都经过同一个本机控制面。
</p>

> **macOS 应用入口：[`GPU Broker.app`](./GPU%20Broker.app)**  
> 构建后会直接出现在项目根目录，用户和 Agent 无需进入 `dist/` 查找。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/access-loopback%20only-0F766E" alt="仅监听本机 loopback">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2563EB" alt="MIT License"></a>
</p>

GPU Broker 适合小团队协调已有的 NVIDIA GPU 服务器。它只读采集状态并记录协作归属，不会替你启动、停止或抢占远端任务。

| 你想做什么 | 推荐入口 |
| --- | --- |
| 立即查看和管理 GPU | 浏览器界面 |
| 在 macOS 独立窗口使用 | 项目根目录的 [`GPU Broker.app`](./GPU%20Broker.app) |
| 让 Codex 等 Agent 安排 GPU | 全局 MCP |

## 快速开始

### 1. 准备环境

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、系统 `ssh` 命令，以及可通过 SSH 访问且安装了 `nvidia-smi` 的服务器。

安装 uv（macOS / Linux）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

下载或克隆本仓库，进入仓库根目录，然后安装 Python 3.12 和运行依赖：

```bash
uv python install 3.12
uv sync --python 3.12 --no-editable
```

### 2. 启动

```bash
uv run --no-editable gpu-broker init
uv run --no-editable gpu-broker serve
```

打开 [http://127.0.0.1:8787/](http://127.0.0.1:8787/)。默认无需登录，服务只监听本机。

### 3. 添加第一台服务器

先确认 SSH 已配置好密钥和主机指纹，不需要交互输入密码：

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -p PORT USER@HOST nvidia-smi
```

在首页点击“添加服务器”，粘贴 `ssh -p PORT USER@HOST`。GPU Broker 只解析并预览这条命令；确认后才登记服务器，随后使用固定只读探针采集状态。

当服务器显示在线且 GPU 不再是 `UNKNOWN_*` 时，即可在界面中认领或预约。资源不足会自动进入队列。

## macOS 桌面应用

需要 macOS 13+ 和 Xcode Command Line Tools。在仓库根目录运行：

```bash
xcode-select --install  # 尚未安装时执行一次
zsh desktop/build-macos-app.sh
open "GPU Broker.app"
```

桌面应用会复用已运行的本机服务；否则自动初始化并启动它。实际构建产物仍保存在 `dist/GPU Broker.app`，根目录入口是由构建脚本维护的轻量符号链接，不会复制应用包。

## Agent / MCP

安装完成后，Agent 通过全局 MCP 工作，无需进入或读取本仓库。一次性安装、Codex 配置和外部项目的单行 `AGENTS.md` 写法见 [Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

## 更新与备份

更新源码安装：

```bash
git pull --ff-only
uv sync --no-editable
```

如果使用桌面应用，再运行一次 `zsh desktop/build-macos-app.sh`。本机状态保存在被 Git 忽略的 `state/`；备份到仓库外：

```bash
uv run --no-editable gpu-broker backup --output /safe/path/gpu-broker.sqlite3
```

## 常见问题

- 页面打不开：确认 `gpu-broker serve` 仍在运行，且端口 `8787` 未被占用。
- 服务器显示失效：先重新执行上面的 SSH / `nvidia-smi` 检查；交互密码、未知主机指纹和 SSH 超时都会使采集失败。
- GPU 不可认领：telemetry 失效、非托管进程、维护或冲突都会触发 fail closed；查看界面中的状态原因。
- MCP 无法连接：先启动本机服务，再用 `codex mcp get gpu-broker --json` 检查全局配置。

## 安全边界

- `configs/inventory.yaml` 只描述静态资产，不能证明 GPU 当前可用。
- Collector 不读取私钥、完整命令行、环境变量或任务数据，也不改变远端运行时。
- 租约只协调归属；工作负载仍需项目或资源所有者授权。
- 非本机部署需要另行设计 TLS、访问控制和持久服务管理。

当前交付与未完成 gate 见 [实施状态](docs/IMPLEMENTATION_STATUS_zh.md)。

## 开发

```bash
uv sync --extra dev --no-editable
uv run --extra dev --no-editable pytest
uv run --extra dev --no-editable ruff check .
```

本项目采用 [MIT License](LICENSE)。
