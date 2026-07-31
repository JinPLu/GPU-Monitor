<p align="center">
  <img src="desktop/assets/GPU%20Broker%20Icon.png" width="96" alt="GPU Broker icon">
</p>

<h1 align="center">GPU Broker</h1>

<p align="center">给共享 GPU 准备的本机协调台：看清资源、认领租约、排队等待、用完释放。</p>

<p align="center">
  <a href="docs/AGENT_MCP_zh.md">MCP 安装</a> ·
  <a href="docs/HANHAI22_SLURM_zh.md">瀚海22 / Slurm</a> ·
  <a href="docs/DESIGN_SYSTEM.md">桌面设计</a> ·
  <a href="docs/IMPLEMENTATION_STATUS_zh.md">当前状态</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a> ·
  <a href="LICENSE">MIT License</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Desktop-macOS%20%7C%20Windows-334155" alt="macOS and Windows desktop">
  <img src="https://img.shields.io/badge/REST%20%2F%20CLI%20%2F%20MCP-loopback-0F766E" alt="REST CLI MCP loopback">
  <img src="https://img.shields.io/badge/Remote%20control-never-C2410C" alt="Does not control remote workloads">
</p>

> [!IMPORTANT]
> GPU Broker 负责“谁占用哪块 GPU”、分配与只读探测，不能被直连 SSH 或本地
> inventory 旁路。租约本身不执行命令；任务已获授权后，Agent 可通过项目自己的
> 执行路径在 `lease.resources[]` 指定的资源上启动或停止 workload。每个 resource
> 都给出 endpoint、GPU、`cuda_visible_devices` 和 commitment，Agent 不自行推断。

## 能帮你做什么

GPU Broker 把人类、脚本和 Agent 放到同一个本机资源看板上。大家看到同一份状态，走同一套队列和租约规则，最后用可审计的记录收尾。

- 看清服务器、GPU、显存、利用率、CPU 和内存状态。
- 认领空闲 GPU；忙时进入队列，由 Broker 统一排队和选址。
- 为项目添加 endpoint，或将不再使用的 endpoint 退役并排空。
- 让 Dashboard、CLI、REST 和 MCP 共享同一个服务层，不复制调度逻辑。
- 用固定只读 SSH 探针采集状态，不读取私钥、环境变量或任务内容。

简单查看和手动认领用 Dashboard；自动化和 Agent 协作走 CLI、REST 或 MCP。

## 快速开始

准备条件：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev --reinstall-package gpu-broker
uv run --reinstall-package gpu-broker gpu-broker init
uv run --reinstall-package gpu-broker gpu-broker serve
```

服务默认只监听 <http://127.0.0.1:8787/>。

macOS 日常使用推荐安装用户级后台服务。首次安装会把仓库中的 inventory
和现有 SQLite 状态迁移到 `~/Library/Application Support/GPU Broker/`；
仓库原文件保留，不会删除：

```bash
uv tool install --force .
gpu-broker daemon install --source-root "$PWD"
gpu-broker daemon status
```

后台服务是唯一的 REST/状态 owner。MCP 会在首次调用前自动确保它就绪，
GUI 只是客户端；关闭 GUI 不会停止调度控制面。

更新源码后要同时更新全局命令并重装 LaunchAgent；`daemon install` 会用新
启动参数重启已安装服务：

```bash
uv tool install --force .
gpu-broker daemon install --source-root "$PWD"
```

### macOS 桌面版

```bash
zsh desktop/build-macos-app.sh
open "dist/GPU Broker.app"
```

### Windows 桌面版

在 Windows PowerShell 中运行：

```powershell
.\desktop\build-windows-app.ps1
.\dist\windows\GPU Broker\GPU Broker.exe
```

桌面版都是源码构建产物，不是已签名安装包。Windows 首次运行会在 `%LOCALAPPDATA%\GPU Broker\` 写入默认 inventory 和 SQLite state；路径和端口可用 `GPU_BROKER_DATA_DIR`、`GPU_BROKER_INVENTORY`、`GPU_BROKER_DATABASE_URL`、`GPU_BROKER_BIND_PORT` 覆盖。

## 直接使用

| 你想做的事 | 入口 |
| --- | --- |
| 查看资源、管理项目 endpoint、认领或退役 endpoint | Dashboard |
| 写脚本、备份、迁移、一次性采集 | CLI |
| 接入本机工具或内部页面 | REST |
| 让 Agent 查询、认领、绑定观测、释放租约 | MCP |

常用命令：

```bash
gpu-broker status
gpu-broker gpu list
gpu-broker request queue
gpu-broker endpoint delete <endpoint_id>
gpu-broker lease release --help
```

## 服务器管理

在 Dashboard 里粘贴一行或多行 `ssh [-p PORT] USER@HOST`。GPU Broker 会逐行预览，只登记合法且不重复的地址。

粘贴内容只用于解析地址，不会作为 shell 命令执行。管道、跳板、密钥参数和额外 shell 片段会被拒绝。

endpoint 属于项目，而不是全局管理员资产；返回体以 `owner_project_id` 与
`lifecycle_state` 表达归属和生命周期。项目 owner 可从 Dashboard、REST、CLI
或 MCP 将误登记或已退役的 endpoint 标记为 draining。draining 后不再接收新
placement，也不会停止已有远端任务；关联的活跃租约和排队需求排空后，Broker 才
完成移除。

> [!CAUTION]
> 瀚海22一类 Slurm 集群不是普通 SSH 裸机。登录节点不代表计算节点或可用
> GPU，不应直接加入当前服务器池；实际资源必须由 Slurm 分配。Broker 使用独立
> `SchedulerTarget/SchedulerJob` adapter 发现、提交和跟踪，详见
> [瀚海22外部 Slurm 调度指南](docs/HANHAI22_SLURM_zh.md)。

## Agent / MCP

完成一次 macOS daemon 安装后，为目标 Agent 注册同一个 `gpu-broker-mcp`。
Agent 不依赖 GUI：MCP 首次调用会检查并启动用户级后台服务。日常流程很短：

```text
看协调看板 -> 认领或排队 -> 读取 lease.resources[] -> 项目执行 workload -> 绑定观测 -> 释放
```

预设任务使用 `profile_id` 和任务名。临时任务需要项目标识、任务名、GPU 数量，并按绝对值给出需要的 CPU 核数、系统内存和显存下限。资源合同已在当前任务、已接受计划或同一持续任务的历史认领中明确后，Agent 应主动 claim、排队等待并复用合同，不应反复询问能否使用 MCP。queued/null 结果不能执行；成功 lease 的实际落点只取自 `lease.resources[]`，其中含 endpoint、GPU、`cuda_visible_devices` 和 commitment。随后 Agent 可用项目既有执行路径运行或停止已获授权的 workload；Broker 不会替它启动或停止。对会重试的写操作复用调用方稳定的 `idempotency_key`。

外部 Slurm 项目先调用 `gpu_scheduler_targets` 和
`gpu_scheduler_access_status`。这是独立的 SchedulerTarget adapter：项目 owner
可提交、查询和取消自己的 Slurm 作业；不用也不能把登录节点当裸机 GPU endpoint。
Slurm 作业与裸机 lease 分开记录，`PENDING` 不表示已经获得 GPU，状态与
`AllocTRES` 才是分配事实。当前不公开 staged upload。

完整安装和全局规则见：

- [安装与客户端适配](docs/AGENT_MCP_zh.md)
- [英文全局路由与安全规则](docs/AGENT_MCP_policy.en.md)

## 权限与安全边界

- 默认只绑定 loopback；不要直接暴露到网络。
- 裸机 lease 协调归属，不执行远端运行时；项目自己的执行路径在获得 lease 后
  使用 `lease.resources[]`。外部 Slurm adapter 是独立的项目 owner submit/status/cancel 接口。
- inventory 只是静态资产清单，不能证明 GPU 当前可用。
- GPU UUID 和 endpoint `id` 是身份边界；同 IP 不同端口不合并。
- telemetry 过期、采集异常、非托管进程、维护或冲突都会拒绝分配。
- 本地 actor 是审计标签，不是认证凭据。
- 自动启停、抢占、dstack、交互式 Slurm、下载/同步和非 loopback 部署不在
  当前开放边界内。

## 开发与验证

```bash
uv sync --extra dev --reinstall-package gpu-broker
uv run --reinstall-package gpu-broker pytest
uv run --reinstall-package gpu-broker ruff check .
```

改桌面壳或打包路径时，在对应平台运行构建脚本：macOS 用 `zsh desktop/build-macos-app.sh`，Windows 用 `.\desktop\build-windows-app.ps1`。

源码主要在 `src/gpu_broker/`，桌面壳和资源在 `desktop/`，测试在 `tests/`，规则与状态文档在 `docs/`。不要提交 `dist/`、`build/`、`state/`、`.venv/`、`.codegraph/`、真实 inventory、私钥、`.env`、本地数据库或临时 QA 截图。

## 更多资料

- [贡献指南](CONTRIBUTING.md)
- [安全边界与报告](SECURITY.md)
- [瀚海22外部 Slurm 调度指南](docs/HANHAI22_SLURM_zh.md)
- [桌面设计系统](docs/DESIGN_SYSTEM.md)
- [实施状态与未完成 gate](docs/IMPLEMENTATION_STATUS_zh.md)
- [历史验证归档](docs/archive/IMPLEMENTATION_EVIDENCE_2026-07-19.md)

## License

[MIT](LICENSE)
