<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="96" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center"><strong>把服务器算力变成清晰、可申请、可追溯的资源池。🛰️</strong></p>

<p align="center">
  <a href="#-你可以用它做什么">你能做什么</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-服务器采集脚本">采集脚本</a> ·
  <a href="#-让-agent-使用资源">Agent / MCP</a> ·
  <a href="#-常用入口">常用入口</a> ·
  <a href="#-文档与帮助">文档</a>
</p>

<p align="center">
  <a href="https://github.com/JinPLu/ServerPilot/releases/tag/v1.4.0"><img src="https://img.shields.io/badge/release-v1.4.0-0F766E" alt="ServerPilot v1.4.0"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-14%2B-111827?logo=apple&logoColor=white" alt="macOS 14+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

你有多台 GPU / CPU 服务器、多个项目和多个 Agent？ServerPilot 将它们汇总到你的**本机资源池**：
人可以在 App 中看清资源和归属，Agent 可以通过 MCP 申请资源，所有人始终看到同一份状态。

> [!TIP]
> ServerPilot 适合“多人 / 多 Agent 共用一批机器”的场景。它帮你协调资源归属；项目仍按自己的方式启动、停止和管理任务。

## ✨ 你可以用它做什么

| 你的目标 | ServerPilot 帮你完成 |
| --- | --- |
| 🖥️ **一眼看清资源** | 查看每台服务器的 GPU、显存、进程、CPU、内存、在线状态与历史趋势。 |
| ➕ **灵活管理服务器** | 添加、编辑服务器；需要腾挪时“暂停接收新任务”，准备好后恢复，彻底不用时再退役。 |
| 🎟️ **安全申请资源** | 为项目、Agent 或任务申请 GPU / CPU / 内存；资源不足时自动排队，分配结果清楚可追溯。 |
| 🤖 **让 Agent 自助协作** | Codex、Claude Code、Cursor 等 Agent 通过 MCP 申请、等待、使用并归还资源。 |
| 🔌 **适配特殊服务器** | 普通服务器直接采集；容器、前缀命令或特殊环境可使用每台服务器自己的采集脚本。 |
| 🧮 **接入外部集群** | 通过独立入口使用 Slurm 等外部调度器，不把登录节点误当作普通 GPU 服务器。 |

### 🧭 日常流程

```text
➕ 添加服务器 → 📡 获取只读资源快照 → 🎟️ 申请资源 → 🧑‍💻 项目启动任务
                                      ↓
                              🤖 Agent 获得 lease.resources[]
                                      ↓
                          🔗 绑定已观察到的任务 → ♻️ 完成后归还
```

## 🚀 快速开始

### 1. 在本机安装控制面

准备 [Python 3.12+](https://www.python.org/) 和 [uv](https://docs.astral.sh/uv/)，然后执行：

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

打开资源页：<http://127.0.0.1:8787/>

```bash
serverpilot state
```

> [!NOTE]
> daemon 是唯一的状态持有者。关闭网页或桌面 App 不会停止资源控制面。

### 2. 使用桌面 App（可选）

从源码构建原生桌面 App：

```bash
zsh desktop/build-macos-app.sh
open "$HOME/Applications/ServerPilot.app"
```

App 提供资源总览、服务器详情、可调整宽度的列表 / 详情视图、项目与 Agent 归属，以及暂停、恢复、退役等操作。

### 3. 添加你的第一台服务器

在 App 点击 **➕ 添加服务器**，或使用对应的 CLI / MCP 管理入口。添加后等待一次采集完成，再以页面显示的在线状态和可分配资源为准。

## 🔌 服务器采集脚本

大多数 NVIDIA 服务器可以使用默认的只读采集方式。对于 Docker、站点前缀命令或其他特殊环境，ServerPilot 1.4.0 默认支持 **服务器采集脚本**：

- 每台服务器只维护自己的 `serverpilot-collect --schema-version 1` 本地入口；
- 入口输出 GPU、CPU 和内存的受限 JSON 快照；
- 中央 Collector 统一执行、校验与节流，App 和 MCP 只读取同一份资源状态；
- 脚本缺失、超时或输出不合法时会明确闭锁，不会把未知资源当作可申请资源。

📖 安装与 JSON 格式请看：[服务器采集脚本协议](docs/COLLECTOR_SCRIPT_zh.md)。

## 🤖 让 Agent 使用资源

安装 daemon 后，为 Codex 注册 MCP：

```bash
codex mcp add serverpilot \
  --env GPU_BROKER_URL=http://127.0.0.1:8787 \
  -- serverpilot-mcp
python3 scripts/install_agent_policy.py codex --install
```

Agent 的正确使用方式很简单：

1. 🎟️ 提供已批准的资源 profile，或完整的项目、任务与资源下限；
2. ⏳ 申请后如进入队列，继续等待同一个请求；
3. 🧩 只使用返回的 `lease.resources[]` 和 `cuda_visible_devices` 启动已授权任务；
4. 🔗 任务启动后绑定已观察到的工作负载，完成或启动失败后归还资源。

> [!IMPORTANT]
> Agent 不能根据 IP、GPU 编号、旧快照或“容量减已用”自行选择机器。过期、未知、冲突、维护或未托管资源不会被分配。

完整安装、资源合同与外部 Agent 行为请看：[Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

## 🧰 常用入口

| 场景 | 推荐入口 |
| --- | --- |
| 👀 查看资源、历史和归属 | 桌面 App 或 <http://127.0.0.1:8787/> |
| 🖱️ 管理服务器生命周期 | App：添加 / 编辑 / 暂停 / 恢复 / 退役 |
| 💻 自动化查看和维护 | `serverpilot` CLI 或 loopback REST API |
| 🤖 让 Agent 申请资源 | `serverpilot-mcp` |
| 🧮 提交 Slurm 作业 | MCP scheduler 工具 |

兼容提示：`gpu-broker`、`gpu-broker-mcp` 与 `GPU_BROKER_*` 环境变量仍可继续使用，无需迁移既有 daemon、MCP 配置或状态数据。

## 🛡️ 资源协调的边界

- ✅ ServerPilot 协调**谁可以使用哪些资源**，并保留租约、队列和审计记录。
- ✅ 暂停服务器只阻止新任务，**不会停止**已有远端任务；退役前需先排空。
- ✅ “已申领，待使用”的资源不会因为暂时没有激活而被提前自动释放。
- ❌ ServerPilot 不会替你启动、停止或抢占项目任务。
- ❌ App、CLI 与 MCP 不会接受任意 shell、SSH 参数、私钥或远程执行命令。

## 📚 文档与帮助

- 🤖 [Agent / MCP 安装与资源合同](docs/AGENT_MCP_zh.md)
- 🔌 [服务器采集脚本协议](docs/COLLECTOR_SCRIPT_zh.md)
- 🧮 [外部调度器与 Adapter](docs/ADAPTERS_zh.md)
- ✅ [当前实现与验证状态](docs/IMPLEMENTATION_STATUS_zh.md)
- 🎨 [桌面设计系统](docs/DESIGN_SYSTEM.md)
- 🔐 [安全说明](SECURITY.md)
- 🧑‍💻 [参与贡献](CONTRIBUTING.md)

## 📄 License

[MIT](LICENSE)
