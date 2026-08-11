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
| 🧩 **统一零散 GPU** | 汇总多台服务器和不同型号 GPU，以同一份状态形成可协作使用的资源池。 |
| 🤖 **Agent 协作调度** | 多个 Agent 按项目与任务申请、使用和归还 GPU；成功立即获得明确的资源，容量不足返回 `no_capacity`。 |
| 👁️ **人类实时监控** | 在紧凑表格中查看服务器、项目 / 任务、GPU 型号与资源压力；打开详情即可查看每张 GPU 和历史趋势。 |
| 🔎 **状态与归属追溯** | 记录分配、任务绑定与释放，让人和 Agent 随时确认 GPU 正在为哪个项目、哪个任务工作。 |

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

- **服务器**：紧凑资源表、筛选与排序；点击服务器查看 GPU 和 1h / 6h / 24h 历史。
- **使用情况**：按项目查看当前任务与已分配 GPU。
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

Agent 可直接调用 `gpu_apply` 即时申请：成功获得明确的 `lease.resources[]`，容量不足返回 `no_capacity`；需要诊断时再调用 `gpu_status`。Codex task URI 会自动登记，供 Agent 发现协作对象；它只是宿主侧交接引用，App 不实现聊天或消息功能。

默认 `serverpilot-mcp` 只发布日常 GPU 工具；scheduler、兼容性资源、端点管理和低层 lease 工具需显式设置 `SERVERPILOT_MCP_PROFILE=advanced`。

安装、申请、绑定、释放与协调规则见 [Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

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
