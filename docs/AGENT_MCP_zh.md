# Agent / MCP 全局安装

The short cross-platform English client adapter is [`AGENT_MCP_policy.en.md`](AGENT_MCP_policy.en.md). The MCP server's `instructions` are the runtime contract; this file only explains registration, policy installation, and task-time inputs.

运行契约由 `gpu-broker` MCP instructions 提供；其他项目无需读取本仓库文档或复制 GPU 工作流。

## 一次性安装

在本仓库根目录执行：

```bash
uv tool install --force .
```

如果 `gpu-broker-mcp` 已在 PATH，可在各客户端使用同一个 stdio server：

| 客户端 | MCP 注册方式 | 全局规则 |
| --- | --- | --- |
| Codex | `codex mcp add gpu-broker --env GPU_BROKER_URL=http://127.0.0.1:8787 -- gpu-broker-mcp` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `claude mcp add --scope user gpu-broker --env GPU_BROKER_URL=http://127.0.0.1:8787 -- gpu-broker-mcp` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | 在 `~/.cursor/mcp.json` 配置 `gpu-broker` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

Codex 可在 `~/.codex/config.toml` 的 `[mcp_servers.gpu-broker]` 下限制日常工具：

```toml
enabled_tools = [
  "gpu_status", "gpu_coordination", "gpu_list", "gpu_who", "gpu_list_profiles", "gpu_claim_profile", "gpu_claim",
  "gpu_request_status", "gpu_cancel_request", "gpu_activate_lease", "gpu_bind_observed_workload", "gpu_grant_server_project", "gpu_release",
]
```

这组工具覆盖共享协调看板、按项目配置认领、例外的一次性认领、排队取消、已启动 workload 的观测绑定、显式跨项目服务器授权和释放；预约、续期、注册服务器等管理工具默认不启用。`gpu_grant_server_project` 仍受全局规则的“用户明确授权指定项目与既有服务器”限制。重启 Codex，并用 `codex mcp get gpu-broker --json` 确认配置；源码更新后执行 `uv tool install --force .`，并重启本机 `127.0.0.1:8787` 服务，使 MCP 与服务加载同一版本。

Cursor 的全局 MCP 示例：

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

参考客户端文档：[Claude Code MCP](https://docs.anthropic.com/en/docs/claude-code/mcp) · [Cursor MCP](https://docs.cursor.com/context/model-context-protocol)。

## 任务时提供的信息

无需在其他项目的 `AGENTS.md` 配置 GPU 说明。项目可选地定义“工作负载配置”：固定项目、用途、GPU 数量、可选的服务器子集和最大租约窗口；这不是每次申请的预计占用时间。日常任务明确给出 `profile_id` 时，Agent 调用 `gpu_claim_profile`；它不得根据工作目录、任务标题、空闲 GPU 或 profile 列表自行挑选配置。

没有 `profile_id` 时，用户或已批准任务合同明确给出 `project_id`、任务和 GPU 数量，Agent 即可使用一次性 `gpu_claim`；任务名自动记录为用途。除非任务明确指定服务器或 GPU，否则不传 placement，让 Broker 自己在共享队列中公平选址。认领无论立即分配还是排队后分配都会自动变为使用中；任务完成后立即释放租约。若 Broker 明确返回 `project_endpoint_scope`，它会给出项目/服务器边界；只有用户明确授权把该项目加入该已登记服务器时，Agent 才能调用 `gpu_grant_server_project`。该操作只增加项目访问权、不移除已有项目，并立即重试排队申请。profile 不是 Agent 自主扩大资源范围的授权。

## Agent 间共享协调

不需要专门的“调度 Agent”。任一 Agent 在需要了解集群协作状态时调用只读 `gpu_coordination`：它按服务器展示总卡、可分配卡、已租卡、实际受管运行卡、空租约卡、未归属计算进程、显存/利用率事实值，并列出该服务器的租约 owner、项目和任务；同时按 Agent 汇总当前租约和排队压力。

已获租约的任务启动远端 workload 后，只需调用 `gpu_bind_observed_workload(agent_name, lease_id)`；可选的 `run_id` 不填时由 Broker 从 lease 生成稳定标识。Broker 只采纳其自身刚采集到的该 lease GPU 进程身份，不会运行、停止或改写远端任务；这样所有 Agent 都能从看板看到 `RUNNING_MANAGED`，而不会把正常工作误判成未托管进程。

不要引用本仓库路径，也不要复制 MCP 工作流。GPU Broker 桌面应用或本机服务必须运行在 `127.0.0.1:8787`；服务不可用时，MCP 会失败且不得自行推断 GPU 可用性。

## 跨平台规则安装

```bash
python3 scripts/install_agent_policy.py codex --install
python3 scripts/install_agent_policy.py claude --install
python3 scripts/install_agent_policy.py cursor --print
```

Cursor 的 User Rules 由 Cursor 设置界面管理；命令只打印需要粘贴的英文规则。安装器只依赖 Python 标准库，在 macOS、Linux 和 Windows 上按用户目录工作；Windows 没有 `python3` 时使用 `python`。使用 `all --print` 可检查三个平台的渲染结果。

规则安装和 MCP 注册是两个动作：安装器只维护全局规则块，不会替你修改客户端的 MCP 配置。Codex、Claude Code 和 Cursor 都可注册同一个 stdio server，但注册位置由各客户端管理；请在客户端中确认 `gpu-broker` 已在线后再使用规则。
