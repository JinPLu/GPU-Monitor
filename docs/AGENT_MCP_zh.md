# ServerPilot Agent / MCP 使用指南

ServerPilot 负责确认与协调算力归属；项目自己启动和停止已获授权的 workload。工具参数和完整约束以 MCP server instructions / schema 为准，本页只保留安装与日常路径。

## 安装与注册

在本仓库根目录安装本机服务：

```bash
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

按客户端注册 MCP，并安装短全局规则：

| 客户端 | MCP 注册 | 全局规则 |
| --- | --- | --- |
| Codex | `codex mcp add serverpilot --env SERVERPILOT_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `claude mcp add --scope user serverpilot --env SERVERPILOT_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | 在 `~/.cursor/mcp.json` 配置 `serverpilot` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

Codex 还需要在 `~/.codex/config.toml` 的 `[mcp_servers.serverpilot]` 下加入：

```toml
env_vars = ["CODEX_THREAD_ID"]
```

Codex 宿主本身已有当前 task ID；`env_vars` 只是把它转发给 MCP 子进程。ServerPilot 用它生成当前 task 的 Actor 和 `codex://threads/<task-id>`。缺失时 `gpu_status` 仍能查看空闲卡，但 `gpu_apply`、`gpu_release` 会失败；不会猜 ID，也不会回退到共享 `agent`。

服务未运行时，macOS 的 MCP 会尝试启动同一用户的 LaunchAgent；如果服务不兼容或未就绪，调用会明确失败，不会创建备用数据库或改走 SSH。

默认 `serverpilot-mcp` 只暴露 `gpu_status`、`gpu_apply`、`gpu_release`。scheduler、通用资源、端点管理和低层 lease 兼容工具仍保留，但只有设置 `SERVERPILOT_MCP_PROFILE=advanced` 才会出现在工具发现结果中。默认配置不需要 `enabled_tools` 白名单。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

1. `gpu_status()` 只返回当前可用 GPU：`server_id`、`workspace_path`、`gpu_id`、`index`、`name`、`vram_mib` 和简短中文 `status`。占卡相关状态直接显示“可用 · 空闲占卡”或“可用 · 占卡异常：原因”；没有 GPU 时返回“无 GPU”。需要查看占用者或连接失败卡时调用 `gpu_status(include_busy=true)`；忙卡额外带 `task` 和 `agent_url`。
2. `gpu_apply(server_id?, gpu_count=1, task?)` 直接申请。ServerPilot 自动选择具体 GPU；Agent 不提供 GPU ID。成功只返回 `lease_id` 与 `gpus[{server_id, workspace_path, gpu_id, cuda_visible_devices}]`。Agent 先进入返回的 `workspace_path`，再使用该项的 `cuda_visible_devices` 启动项目 workload。
3. workload 停止或启动失败时调用 `gpu_release(lease_id)`。

已确认的空闲占卡 GPU 仍出现在可用容量中。`gpu_apply` 真正分配这张卡前会自动停止该卡 helper、定向刷新并确认空闲，再返回普通工作租约；Agent 不需要也不能手工执行占卡停止流程。

新增服务器必须同时登记一个绝对 `workspace_path`；当前用户确认的共用值是 `/media/datasets/OminiEWM_Data/tmp/ljp`。历史 endpoint 没有该元数据时状态明确返回空值，需通过现有 endpoint 更新面补齐，不会猜测子项目路径。该字段只是元数据和操作指引：ServerPilot 不会自动创建/删除远端目录，也不会因此获得启动项目 workload 的权限。密封占卡 helper 只采用固定布局 `${workspace_path}/serverpilot-keepalive`；adapter 以该 workspace 为工作目录，直接执行 `./serverpilot-keepalive --schema-version 2`，不从远端 `PATH` 查找，也不接受 caller 提供的 helper 路径、命令或参数。

`no_capacity` 不创建队列。例行租约持续到显式 `gpu_release` 或 App 人工处理，不需要 bind、renew、heartbeat、coordination 或 `idempotency_key`。

## 边界

- 通用资源、管理与 scheduler 操作是 advanced 兼容接口，不进入默认 Agent 上下文。
- `codex://threads/<uuid>` 是 Codex task 的交接引用；ServerPilot 只保存并返回它，不负责打开或解析。
- App 负责人工查看和纠错；默认 Agent 路径不需要额外生命周期步骤。

## 自检

```bash
serverpilot daemon status --json
serverpilot-mcp --help
python3 scripts/install_agent_policy.py all --print
```
