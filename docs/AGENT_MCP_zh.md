# Agent / MCP 全局安装

运行契约由 `gpu-broker` MCP instructions 提供，其他项目无需读取本仓库文档。

## 一次性安装

在本仓库根目录执行：

```bash
uv tool install --force .
codex mcp add gpu-broker \
  --env GPU_BROKER_URL=http://127.0.0.1:8787 \
  -- "$HOME/.local/share/uv/tools/gpu-broker/bin/gpu-broker-mcp"
```

然后在 `~/.codex/config.toml` 的 `[mcp_servers.gpu-broker]` 下加入：

```toml
enabled_tools = ["gpu_status", "gpu_claim", "gpu_request_status", "gpu_schedule", "gpu_release"]
```

这会隐藏日常工作流不需要的管理工具。重启 Codex，并用 `codex mcp get gpu-broker --json` 确认配置；源码更新后只需重新执行 `uv tool install --force .`。

## 项目配置

每个使用 GPU 的项目只在自己的 `AGENTS.md` 写实际项目 ID，例如：

```md
本项目的 GPU Broker `project_id` 是 `example-project`；GPU 工作负载使用全局 `gpu-broker` MCP，并向项目级操作显式传入该值。
```

不要引用本仓库路径，也不要复制 MCP 工作流。GPU Broker 桌面应用或本机服务必须运行在 `127.0.0.1:8787`；服务不可用时，MCP 会失败且不得自行推断 GPU 可用性。
