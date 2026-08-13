<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="96" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center"><strong>让多个 Agent 协作调度零散 GPU，让人实时看清全局。🛰️</strong></p>

<p align="center">
  <a href="#-核心能力">核心能力</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-桌面-app">桌面 App</a> ·
  <a href="#-agent--mcp">Agent / MCP</a> ·
  <a href="#-文档">文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-14%2B-111827?logo=apple&logoColor=white" alt="macOS 14+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

**一个本机用户，管理多台服务器、多个项目和多个 Agent。**

ServerPilot 把散落在不同服务器上的 GPU 汇总为一个资源池：多个 Agent 通过 MCP 按项目和任务协作调度 GPU；人通过原生 App 实时监控服务器、GPU 型号、当前归属与历史趋势。Agent 与人读取同一份控制面状态，不再各自对着 IP、旧快照和 GPU 编号猜测资源。

![ServerPilot 工作流](docs/assets/serverpilot-workflow-cartoon.png)

## ✨ 核心能力

| 核心价值 | ServerPilot 提供什么 |
| --- | --- |
| 📡 **信息采集** | 从已登记服务器读取 CPU、内存、GPU、进程和历史趋势，形成一份当前状态。 |
| 👁️ **人类实时监控** | 在原生 App 查看服务器与任务；可把任务改到人选定的 GPU，也可在确认任务结束后释放孤立租约；日常 Agent 仍只能释放自己的租约。 |
| 🤖 **Agent 操作** | Agent 申请 GPU、进入返回的远端 `workspace_path`、使用 `CUDA_VISIBLE_DEVICES`，并在结束时归还。 |
| 🟢 **空闲 GPU 占卡** | 每轮采集后逐卡自动恢复占卡；占卡 GPU 仍算可用，Agent 申请或人改派时只停止对应卡，刷新确认后交付。 |

## 🚀 快速开始

准备 [Python 3.12+](https://www.python.org/) 和 [uv](https://docs.astral.sh/uv/)，然后在本机运行：

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

控制面默认监听 <http://127.0.0.1:8787/>；关闭 App 不会停止后台服务。

## 🖥️ 桌面 App

构建后，仓库最外层只保留一个最新 App：

```bash
zsh desktop/build-macos-app.sh
open "./ServerPilot.app"
```

浅色原生 App 只保留三个一级页面：

- **服务器**：添加时同时登记绝对远端工作区路径；资源表和详情清楚显示该路径、GPU 和 1h / 6h / 24h 历史。
- **使用情况**：按项目查看当前任务与已分配 GPU，并可由人按原卡数改到指定 GPU。
- **设置**：本机服务地址、数据采集间隔和版本。

服务器按设置的 **5 / 10 / 30 秒**间隔采集；刷新读取控制面已经汇总的最新状态。

## 🤖 Agent / MCP

为 Codex 注册本机 MCP：

```bash
codex mcp add serverpilot \
  --env SERVERPILOT_URL=http://127.0.0.1:8787 \
  -- serverpilot-mcp
python3 scripts/install_agent_policy.py codex --install
```

默认 MCP 只有三个工具：`gpu_status` 返回“哪台服务器的哪张 GPU 可用”、结构化 `ssh {host, port, user}`、该服务器的 `workspace_path` 及简短中文状态，`gpu_apply` 返回 `lease_id` 和选中的 `gpus[]`（每项含 `ssh` 与 `workspace_path`），`gpu_release` 归还租约。调用 `gpu_apply` 时，`task` 使用用户给定的任务名或当前目标的简短人类可读概括，不读取客户端 UI 标题；未提供时记录为“未命名任务”。申请成功后，Agent 用返回的 SSH 参数直接进入服务器；已登记且当前凭据可达的服务器不需要再添加为 Codex saved host。SSH 后先 `cd workspace_path`，后续命令、代码同步和产物操作都以它为远端工作目录。`workspace_path` 不是代码仓库路径，Agent 不得据此猜测项目代码位置，也不能在本地 worktree 直接进入。单 endpoint 租约的顶层 `cuda_visible_devices` 是完整集合，适合一个多卡进程；`gpus[]` 中每项保留这一兼容字段，并新增只含该卡 UUID 的 `gpu_cuda_visible_devices`，适合每卡一个进程。随后按对应 selector 做最小 CUDA gate 并启动 workload；gate 或启动失败须立即释放，并在当前任务内记下服务器与失败原因以避开同一环境。需要查看占用者或连接失败卡时用 `gpu_status(include_busy=true)`；忙卡返回 `task`。

为避免 Agent 依赖自然语言猜测，状态和申请结果还返回统一的 `workspace {path, kind, use_as_cwd, code_location}`。其中 `kind=working_directory`、`use_as_cwd=true`，而 `code_location=not_provided` 明确表示 ServerPilot 不知道也不提供代码仓库位置。连接参数、工作目录、代码位置和 CUDA selector 是四个相互独立的概念；旧 `workspace_path` 字段继续保留以兼容既有调用方。

`no_capacity` 没有分配也不排队：同一 turn 最多刷新一次，后续交给下一 turn 或工作周期；`Transport closed` 是传输错误，最多重试一次，不能当作无容量。例行申请不需要绑定、续租、协调工具或 `idempotency_key`；租约持续到显式释放或在 App 中人工处理。多个 lease 必须用显式清单跟踪，申请者负责逐个确认已 `released`。

ServerPilot 的约束只覆盖 GPU 协调：申请成功后的直接 SSH 是 workload 正常执行路径；不得通过 SSH、静态 inventory、SQLite 或 `nvidia-smi` 绕过它发现、指定、申请或释放 GPU。已登记且当前凭据可达的 endpoint 可以直接 SSH，不要求加入 Codex saved hosts。Git 同步、文件维护和只读环境检查等非 GPU 操作不需要 GPU lease；ServerPilot 只返回连接参数，不提供密码、私钥或替代项目自己的远端命令权限。

当前已确认服务器共用的远端工作目录是 `/media/datasets/OminiEWM_Data/tmp/ljp`。它是执行远端操作时的 cwd，不是代码仓库路径；需要的代码应在该目录中同步/放置，或按任务已知信息进入它的现有子目录。它不会让 ServerPilot 自动创建/删除目录，也不授权启动项目 workload。密封占卡 helper 的固定入口是 `${workspace_path}/serverpilot-keepalive`；ServerPilot 以该 workspace 为工作目录并直接执行 `./serverpilot-keepalive --schema-version 2`，不从远端 `PATH` 查找，也不允许 caller 覆盖 helper 路径或参数。

默认 `serverpilot-mcp` 只发布日常 GPU 工具；scheduler、兼容性资源、端点管理和低层 lease 工具需显式设置 `SERVERPILOT_MCP_PROFILE=advanced`。

安装、申请与释放规则见 [Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

## 📚 文档

- 🤖 [Agent / MCP 安装与资源合同](docs/AGENT_MCP_zh.md)
- 📡 [服务器采集协议](docs/COLLECTOR_SCRIPT_zh.md)
- 🔌 [外部调度器与 Adapter](docs/ADAPTERS_zh.md)
- ✅ [当前实现与验证状态](docs/IMPLEMENTATION_STATUS_zh.md)
- 🎨 [桌面设计系统](docs/DESIGN_SYSTEM.md)
- 🔐 [安全说明](SECURITY.md)
- 🧑‍💻 [参与贡献](CONTRIBUTING.md)

## 📄 License

[MIT](LICENSE)
