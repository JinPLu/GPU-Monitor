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

默认 `serverpilot-mcp` 只暴露 `gpu_status`、`gpu_apply`、`gpu_release`。scheduler、通用资源、端点管理和低层 lease 兼容工具仍保留，但只有设置 `SERVERPILOT_MCP_PROFILE=advanced` 才会出现在工具发现结果中。默认配置不需要 `enabled_tools` 白名单。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

1. `gpu_status()` 只返回当前可用 GPU：`server_id`、结构化 `ssh {host, port, user}`、结构化 `workspace`、兼容字段 `workspace_path`、GPU 信息、简短中文 `status`，以及占卡的 `keepalive.desired / actual`。`workspace` 固定表达远端工作目录语义：`{path, kind=working_directory, use_as_cwd=true, code_location=not_provided}`。没有 GPU 时返回“无 GPU”；需要查看占用者或连接失败卡时调用 `gpu_status(include_busy=true)`，忙卡带人类可读的 `task`。
2. `gpu_apply(server_id?, gpu_count=1, task?)` 直接申请。`task` 使用用户给定的任务名或当前目标的简短人类可读概括，不读取客户端 UI 标题；未提供时记录为“未命名任务”。ServerPilot 自动选 GPU，Agent 不提供 GPU ID。申请结果中的四类信息必须分开理解：`ssh` 是连接参数；`workspace.path` 是 SSH 后先进入的远端 cwd；`code_location=not_provided` 表示 ServerPilot 不提供也不暗示代码仓库位置；CUDA 字段只选择已租用设备。已登记且凭据可达的服务器可按 `ssh` 直接连接，无需添加 Codex saved host。连接后先 `cd workspace.path`，后续命令、代码同步和产物操作都在该工作目录下进行，不得把它当代码路径或据此猜测仓库位置。单 endpoint 顶层 `cuda_visible_devices` 选择整个租约；每行 `gpu_cuda_visible_devices` 只选择对应一张卡。跨 endpoint 结果不生成有歧义的顶层 server/SSH/workspace/CUDA 字段。随后做最小 CUDA gate，再启动 workload。
3. 最小 CUDA gate 失败或 workload 未能启动时立即调用 `gpu_release(lease_id)`；在当前任务内记下失败的 `server_id` 与原因，后续申请避开同一环境。workload 停止时同样释放。

已确认的空闲占卡 GPU 仍出现在可用容量中。`gpu_apply` 真正分配这张卡前会自动停止该卡 helper、定向刷新并确认空闲，再返回普通工作租约；Agent 不需要也不能手工执行占卡停止流程。

新增服务器必须同时登记一个绝对 `workspace_path`；当前用户确认的共用值是 `/media/datasets/OminiEWM_Data/tmp/ljp`。历史 endpoint 没有该元数据时状态明确返回空值，需通过现有 endpoint 更新面补齐，不会猜测子项目路径。该字段是远端操作的工作目录元数据，不是代码仓库路径：ServerPilot 不会自动创建/删除远端目录，也不会因此获得启动项目 workload 的权限。密封占卡 helper 只采用固定布局 `${workspace_path}/serverpilot-keepalive`；adapter 以该 workspace 为工作目录，直接执行 `./serverpilot-keepalive --schema-version 2`，不从远端 `PATH` 查找，也不接受 caller 提供的 helper 路径、命令或参数。

`no_capacity` 不创建队列，也不代表传输失败。同一 turn 最多再刷新一次状态，然后把再次尝试留给下一 turn 或后续工作周期，不在当前 turn 反复申请。`Transport closed` 与 `no_capacity` 分开处理：前者最多重试一次，仍失败就报告传输错误。

例行租约持续到显式 `gpu_release` 或 App 人工处理，不需要 bind、renew、heartbeat、coordination 或 `idempotency_key`。如果一个任务持有多个 lease，必须维护显式的 `lease_id` 清单；申请者负责逐个释放并确认结果为 `released`，不能因其中一个已释放就把整个任务视为完成。

ServerPilot 只协调 GPU。申请成功后的直接 SSH 是执行 workload 的正常路径；禁止的是通过 SSH、SQLite、静态 inventory 或 `nvidia-smi` 绕过 ServerPilot 发现、指定、申请或释放 GPU。已经登记且当前凭据可达的 endpoint 可以直接 SSH，不要求先加入 Codex saved hosts。非 GPU 远端操作，例如 Git 同步、文件维护和只读环境检查，不需要申请 GPU lease；普通 SSH 操作本身不等于绕过 ServerPilot。

## 边界

- 通用资源、管理与 scheduler 操作是 advanced 兼容接口，不进入默认 Agent 上下文。
- App 负责人工查看和纠错；默认 Agent 路径不需要额外生命周期步骤。
- ServerPilot 返回 SSH 连接参数但不提供密码、私钥或 shell；它复用当前用户已有凭据，也不代替项目自己的远端执行授权。

## 自检

```bash
serverpilot daemon status --json
serverpilot-mcp --help
python3 scripts/install_agent_policy.py all --print
```
