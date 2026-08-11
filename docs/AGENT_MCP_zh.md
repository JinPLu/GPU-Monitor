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

服务未运行时，macOS 的 MCP 会尝试启动同一用户的 LaunchAgent；如果服务不兼容或未就绪，调用会明确失败，不会创建备用数据库或改走 SSH。

默认 `serverpilot-mcp` 只暴露日常 GPU 工具；scheduler、通用资源、端点管理和低层 lease 兼容工具仍保留，但只有设置 `SERVERPILOT_MCP_PROFILE=advanced` 才会出现在工具发现结果中。若客户端维护 `enabled_tools` 白名单，升级后同步加入当前 routine 工具，不要保留已退役名称。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

1. 直接调用 `gpu_apply`。只有需要查看可用性或诊断 placement 时才调用 `gpu_status`；前置读取不会改变申请结果。旧的 `gpu_list` 只在 advanced profile 提供。`server_id` 可选，`gpu_count` 默认是 1；ServerPilot 自动记录例行申请的归属并选择实际可分配的 GPU。Agent 不输入项目、任务、profile、GPU ID，也不计算容量。
2. 成功时只使用返回的 `lease.resources[]`，包括 `cuda_visible_devices`，来运行已获授权的 workload。
3. workload 启动后，使用返回的 `lease_id` 调用 `gpu_bind_observed_workload`；`run_id` 可选。它把已观测的 workload 确认绑定到这张 lease。
4. workload 停止或启动失败时调用 `gpu_release`。

`no_capacity` 不创建队列，也不授权执行。重试同一写操作时复用同一个 `idempotency_key`。状态与协调工具是观察面，不可据此手工计算 placement；不要从名称、inventory、进程列表或表面空闲容量推断结果，也不要通过 SSH、SQLite、inventory 或 `nvidia-smi` 旁路。

## 边界

- 通用资源、workload 绑定、管理与 scheduler 操作是兼容接口，不是日常 GPU 申请的输入。
- 端点管理、保活开关和取消外部作业需要当前任务的明确人类授权、非空 `approval_ref` 与稳定的 `idempotency_key`。
- 外部集群是 `SchedulerTarget`，使用 scheduler 工具。出现 `access_required`、MCP 不可用或无容量时，报告状态并停止；不要改用 SSH 等旁路。
- `codex://threads/<uuid>` 只是宿主侧协调交接引用，不是 ServerPilot 会打开或解析的 URL。升级后若客户端维护 MCP allowlist，必须包含 `gpu_apply`，并移除已退役的 `gpu_claim`、`gpu_claim_profile` 等名称。

## 自检

```bash
serverpilot daemon status --json
serverpilot-mcp --help
python3 scripts/install_agent_policy.py all --print
```
