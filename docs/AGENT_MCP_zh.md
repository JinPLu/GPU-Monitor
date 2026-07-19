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
  "gpu_status", "gpu_list", "gpu_who", "gpu_claim",
  "gpu_request_status", "gpu_cancel_request", "gpu_activate_lease", "gpu_release",
]
```

这组工具覆盖查看、认领、排队取消、激活和释放；预约、续期、注册服务器等管理工具默认不启用。重启 Codex，并用 `codex mcp get gpu-broker --json` 确认配置；源码更新后只需重新执行 `uv tool install --force .`。

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

无需在其他项目的 `AGENTS.md` 配置 GPU 说明。用户在任务中明确给出 `project_id`、预算边界、任务、用途、GPU 数量和时长；Agent 通过全局 MCP 将这些值传入项目级操作，不能按工作目录猜测项目身份。

不要引用本仓库路径，也不要复制 MCP 工作流。GPU Broker 桌面应用或本机服务必须运行在 `127.0.0.1:8787`；服务不可用时，MCP 会失败且不得自行推断 GPU 可用性。

## 跨平台规则安装

```bash
python3 scripts/install_agent_policy.py codex --install
python3 scripts/install_agent_policy.py claude --install
python3 scripts/install_agent_policy.py cursor --print
```

Cursor 的 User Rules 由 Cursor 设置界面管理；命令只打印需要粘贴的英文规则。安装器只依赖 Python 标准库，在 macOS、Linux 和 Windows 上按用户目录工作；Windows 没有 `python3` 时使用 `python`。使用 `all --print` 可检查三个平台的渲染结果。

规则安装和 MCP 注册是两个动作：安装器只维护全局规则块，不会替你修改客户端的 MCP 配置。Codex、Claude Code 和 Cursor 都可注册同一个 stdio server，但注册位置由各客户端管理；请在客户端中确认 `gpu-broker` 已在线后再使用规则。
