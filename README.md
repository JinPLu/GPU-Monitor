<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="88" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center"><strong>让 Agent 协同调度服务器算力，并由人持续监督。</strong></p>

<p align="center">
  <a href="#-四件事">用途</a> ·
  <a href="#-一分钟开始">开始</a> ·
  <a href="#-agent--mcp">Agent / MCP</a> ·
  <a href="#-一条数据路径">数据路径</a> ·
  <a href="#-文档">文档</a>
</p>

<p align="center">
  <a href="https://github.com/JinPLu/ServerPilot/releases/tag/v1.2.0"><img src="https://img.shields.io/badge/release-v1.2.0-0F766E" alt="ServerPilot v1.2.0"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-14%2B-111827?logo=apple&logoColor=white" alt="macOS 14+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

ServerPilot 把直连服务器和外部集群的 GPU / CPU 汇总到一个本机控制面。你可以
查看资源，为项目、Agent 和任务申请资源，并在资源不足时排队。App、CLI 和 MCP
始终读取同一份状态。

## 🎯 四件事

| 你要做什么 | ServerPilot 做什么 |
| --- | --- |
| 🖧 **管理服务器** | 汇总 GPU、显存、进程、CPU 和内存；添加、维护或退役服务器 |
| 📁 **管理项目资源** | 按项目、Agent 和任务分配资源；不足时排队；结束后归还 |
| 🤖 **让 Agent 自助申请** | Codex、Claude Code 或 Cursor 通过 MCP 申请、等待和归还资源 |
| 🧮 **使用外部集群** | 通过独立调度入口提交、查询和取消 Slurm 作业 |

### 选择入口

| 场景 | 入口 |
| --- | --- |
| 你要查看或手动管理 | 桌面 App 或 <http://127.0.0.1:8787/> |
| 脚本要读取或维护 | `serverpilot` CLI / REST API |
| Agent 要申请资源 | `serverpilot-mcp` |
| 任务要提交到 Slurm | MCP scheduler 工具 |

## 🚀 一分钟开始

macOS 准备 [Python 3.12+](https://www.python.org/) 和
[uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
open http://127.0.0.1:8787/
```

查看完整状态：

```bash
serverpilot state
```

后台 daemon 是唯一状态 owner。关闭 App 或网页不会停止控制面。

桌面版从源码构建：

- macOS：`zsh desktop/build-macos-app.sh`
- Windows：`.\desktop\build-windows-app.ps1`

## 🤖 Agent / MCP

安装 daemon 后，为 Codex 注册 MCP：

```bash
codex mcp add serverpilot \
  --env GPU_BROKER_URL=http://127.0.0.1:8787 \
  -- serverpilot-mcp
python3 scripts/install_agent_policy.py codex --install
```

Agent 只走一条资源路径：

```text
profile / 资源合同 → claim → 必要时等待 → 使用 lease.resources[]
→ 项目执行 workload → bind → release
```

资源不足时，Agent 必须等待；资源位置只取自 `lease.resources[]`。完整安装和合同见
[Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

`gpu-broker`、`gpu-broker-mcp` 与 `GPU_BROKER_*` 仍是兼容别名；已有 daemon、MCP
配置和状态数据无需迁移即可继续工作。

## 🔄 一条数据路径

```text
App / CLI / MCP ── loopback REST ──> BrokerService ──> SQLite
GPU / CPU 服务器 ── 固定只读 Collector ──────────────┘
```

- App、CLI 和 MCP 的常规读操作共享 `GET /api/v1/state`；ServerPilot 的资源真相仍由
  `current` 与既有 `history` 共同组成，新增遥测曲线按需通过专用 endpoint history 接口读取。
- 调度、队列、租约、状态和审计规则只存在于 `BrokerService`。
- Collector 与 Slurm 的实现现在通过受限 adapter registry 接入：adapter 只能提供观测或调度
  结果，不能创建 lease/claim、执行任意命令，或增加新的 Agent 凭证路径。
- 服务器退役使用 `active → draining → retired`；App 各页面从下一份统一状态一起更新。
- CLI 和 MCP 不直连 SQLite 或 SSH。

> [!IMPORTANT]
> ServerPilot 只协调资源，不执行任务。项目和 Agent 只能使用
> `lease.resources[]` 返回的位置运行已获授权的任务。

> [!CAUTION]
> Slurm 登录节点不是普通 GPU 服务器，也不能证明 GPU 已分配。瀚海22必须使用
> `SchedulerTarget` 和 scheduler 工具，详见 [Slurm 指南](docs/HANHAI22_SLURM_zh.md)。

## 📚 文档

- 🤖 [Agent / MCP 安装与资源合同](docs/AGENT_MCP_zh.md)
- 🧮 [瀚海22 / Slurm 调度](docs/HANHAI22_SLURM_zh.md)
- ✅ [当前实现与验证状态](docs/IMPLEMENTATION_STATUS_zh.md)
- 🎨 [桌面设计系统](docs/DESIGN_SYSTEM.md)
- 🔌 [Adapter 能力与边界](docs/ADAPTERS_zh.md)
- 🧑‍💻 [参与贡献](CONTRIBUTING.md)
- 🔐 [安全说明](SECURITY.md)

## 📄 License

[MIT](LICENSE)
