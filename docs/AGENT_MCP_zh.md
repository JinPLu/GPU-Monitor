# Agent / MCP 全局安装

MCP 的完整运行契约由 `gpu-broker` server instructions 和工具 schema 提供；英文全局适配块见 [`AGENT_MCP_policy.en.md`](AGENT_MCP_policy.en.md)，保留“GPU 相关任务可主动调度 Broker、复用已批准资源合同、排队继续等待、禁止旁路和禁止推断缺失输入”的跨客户端规则。本文件只保留安装、注册和日常输入。

## 一次性安装

在本仓库根目录执行：

```bash
uv tool install --force .
```

源码更新后重新执行同一命令，并重启本机 `127.0.0.1:8787` 服务。

## 注册 MCP

所有客户端共用同一个 stdio server：

| 客户端 | MCP 注册 | 全局规则 |
| --- | --- | --- |
| Codex | `codex mcp add gpu-broker --env GPU_BROKER_URL=http://127.0.0.1:8787 -- gpu-broker-mcp` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `claude mcp add --scope user gpu-broker --env GPU_BROKER_URL=http://127.0.0.1:8787 -- gpu-broker-mcp` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | 在 `~/.cursor/mcp.json` 配置 `gpu-broker` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

Codex 推荐只启用日常工具，并给这个 MCP server 单独开启工具免重复审批；不要把全局审批直接关成全放行。

```toml
[mcp_servers.gpu-broker]
command = "gpu-broker-mcp"
enabled_tools = [
  "gpu_coordination", "gpu_status", "gpu_list", "gpu_who", "gpu_list_profiles",
  "gpu_claim_profile", "gpu_claim", "gpu_request_status", "gpu_cancel_request",
  "gpu_activate_lease", "gpu_bind_observed_workload", "gpu_release",
]
default_tools_approval_mode = "approve"

[mcp_servers.gpu-broker.env]
GPU_BROKER_URL = "http://127.0.0.1:8787"
```

预约、续期、注册/删除服务器等管理工具不要放进默认工具集；需要时由用户单独授权。

Cursor MCP 示例：

```json
{
  "mcpServers": {
    "gpu-broker": {
      "command": "gpu-broker-mcp",
      "env": {"GPU_BROKER_URL": "http://127.0.0.1:8787"}
    }
  }
}
```

## 日常任务输入

Agent 不需要复制本仓库工作流，也不需要在其他项目写 GPU 说明。任务只给以下两种输入之一：

- 预设任务：明确 `profile_id` 和任务名，Agent 调用 `gpu_claim_profile`。
- 一次性认领：明确任意非空 `project_id`、任务名、`gpu_count`，以及需要的 CPU 核数、系统内存 MiB、显存 MiB 等绝对值下限，Agent 调用 `gpu_claim`。

不要让 Agent 按工作目录、任务标题、空闲 GPU、profile 列表或 inventory 自己挑配置。CPU、内存和显存需求要按绝对值表达，例如 `min_available_cpu_cores=16`、`min_available_memory_mib=65536`、`min_free_vram_mib=61440`。除非任务明确指定服务器或 GPU，否则不传 placement，让 Broker 自己排队和选址。

同一个持续任务里已经明确过 `profile_id`，或已经明确过 `project_id` 与 `gpu_count` 时，Agent 应复用这份资源合同，不应重复询问。通过运行时 preflight 后，Agent 应主动调用 `gpu_claim_profile` 或 `gpu_claim`；如果进入队列，应继续监控直到获配或任务取消，而不是停下来问用户能不能用 MCP。

资源下限应尽量贴近任务真实需求；租约分配后，Agent 应在 workload 支持时充分使用获批 GPU，例如把相互独立的 job 并行分布到租约内的 GPU 上，但不得启动 dummy 占卡进程或做不安全并发。Agent 应在远端 workload 启动后绑定观测、结束后释放。

远端 workload 已经启动后，Agent 调用 `gpu_bind_observed_workload(agent_name, lease_id)`；任务结束或启动失败后调用 `gpu_release(agent_name, lease_id)`。这些动作只记录归属，不启动、不停止、不抢占远端进程。

## 验证

```bash
gpu-broker-mcp --help
codex mcp get gpu-broker --json
python3 scripts/install_agent_policy.py all --print
```

MCP 或本机服务不可用时，Agent 应报告不可用并停止；不得改读 SQLite、SSH、inventory 或 `nvidia-smi`。

规则安装和 MCP 注册是两个独立动作：安装器只维护全局规则块，不自动注册 MCP；Cursor 只打印需要粘贴的规则。
