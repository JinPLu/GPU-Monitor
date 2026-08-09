# ServerPilot Agent / MCP 全局安装与调度指南

MCP 的完整运行契约由 ServerPilot server instructions 和工具 schema 提供；英文全局适配块见 [`AGENT_MCP_policy.en.md`](AGENT_MCP_policy.en.md)，保留“GPU 相关任务可主动调度 Broker、复用已批准资源合同、排队继续等待、禁止旁路和禁止推断缺失输入”的跨客户端规则。本文件只保留安装、注册和日常输入。

## 一次性安装

在本仓库根目录执行：

```bash
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

为保证升级不丢失已有 inventory、租约和历史数据，ServerPilot 继续使用兼容数据目录
`~/Library/Application Support/GPU Broker/`；这是内部路径，不影响 App 显示名。
首次安装会在目标不存在时迁移仓库里的 `configs/inventory.yaml` 和
`state/gpu-broker.sqlite3`，并保留原文件作为回滚源。源码更新后重新执行
以下两条命令；第二条会更新并重启 LaunchAgent，不能只依赖一次健康检查完成
版本切换：

```bash
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
```

常用生命周期命令：

```bash
serverpilot daemon ensure
serverpilot daemon status --json
serverpilot daemon stop
serverpilot daemon start
serverpilot daemon uninstall  # 只移除 LaunchAgent，保留数据
```

## 注册 MCP

所有客户端共用同一个 stdio server：

| 客户端 | MCP 注册 | 全局规则 |
| --- | --- | --- |
| Codex | `codex mcp add serverpilot --env GPU_BROKER_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `claude mcp add --scope user serverpilot --env GPU_BROKER_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | 在 `~/.cursor/mcp.json` 配置 `serverpilot` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

Codex 推荐只启用日常工具，并让下面的 owner-scoped 工具不再逐次等待人工确认；不要把全局工具权限改成全放行。资源合同和项目归属仍由 MCP instructions、工具 schema 与当前任务决定。

```toml
[mcp_servers.serverpilot]
command = "serverpilot-mcp"
enabled_tools = [
  "control_plane_state",
  "gpu_coordination", "gpu_status", "gpu_list", "gpu_who", "gpu_list_profiles",
  "gpu_claim_profile", "gpu_claim", "gpu_wait_for_claim",
  "gpu_bind_observed_workload", "gpu_release",
  "gpu_scheduler_targets", "gpu_scheduler_access_status", "gpu_scheduler_profiles",
  "gpu_scheduler_submit_profile", "gpu_scheduler_submit_once",
  "gpu_scheduler_job_status",
  "resource_providers", "resource_monitor", "resource_claims",
  "resource_evaluate_plan", "resource_claim", "resource_release", "resource_record_actual",
]

[mcp_servers.serverpilot.env]
GPU_BROKER_URL = "http://127.0.0.1:8787"
```

`serverpilot-mcp` 仍然只通过 REST 调用调度服务；它不会直连 SQLite 或 SSH。
常规只读工具从 canonical `/api/v1/state` 的同一 revision 投影；
`control_plane_state` 可直接读取完整状态 envelope（含 `snapshot_revision`、
`data.current` 与 `data.history`），需要等待后端达到指定 revision 时传入
`minimum_snapshot_revision`。
当 loopback 服务未运行时，macOS MCP 会通过同一用户级 LaunchAgent 自动
`ensure`，多个 Agent 复用同一 daemon。若端口被不兼容服务占用、daemon 未完成
一次性安装、readiness 失败或响应服务的 daemon identity 与固定数据路径不一致，
则调用 fail closed，不会接受手工旧进程，也不会创建备用数据库。

## Agent 调度行为准则

ServerPilot 的分工很明确：Agent 按明确合同申请、等待、执行和归还；人类通过资源、工作、队列和历史页面监督利用率与归属。Agent 不自行计算“还有多少可用资源”——服务端的 `available` 投影才是准入真相；stale、unknown、unmanaged、conflict 或 maintenance 一律不可分配，不能以 `capacity - used` 替代。

日常裸机任务只走一条路径：**claim → wait → execute → bind → release**。

1. 合同已含 `profile_id` 时调用 `gpu_claim_profile`；否则必须已有 `project_id`、任务名、`gpu_count` 与 CPU / 内存 / 显存等绝对阈值，才调用 `gpu_claim`。
2. queued/null 不授权执行。对同一 request 调 `gpu_wait_for_claim`，直到 `HELD` / `ACTIVE`、终态或任务超时；已批准合同不变时不重复创建 request 或反复询问人类。
3. 只使用返回的 `lease.resources[]` 落点；不从 inventory、IP、旧快照或进程列表推断 host、GPU、CUDA selector 或可用容量。
4. Agent 只通过项目既有、已获授权的执行路径启动 workload；观测到后 bind，完成或启动失败后 release。每次重试 mutation 均复用同一个 `idempotency_key`。
5. 任务结论应回报 request / lease 或 scheduler job ID、最终状态与任何阻塞原因，方便人类在 ServerPilot 中继续跟踪。

端点添加、draining、retire、取消外部作业等属于管理动作，不能进入默认 Agent 工具白名单。只有当前任务给出明确人类授权时才启用对应工具；`owner_project_id` 仅供归属展示，不构成管理权限。

默认工作流：

- `control_plane_state`、`gpu_coordination`、`gpu_status`、`gpu_list`、`gpu_who` 与 `gpu_list_profiles` 可读取 Broker 状态和可用资源合同；普通预批准 profile 或显式合同 claim 不要求先读协调看板。
- 当前任务已给出 `profile_id`，或已给出 `project_id`、`gpu_count` 与必要阈值时，直接用 `gpu_claim_profile` 或 `gpu_claim`。得到 queued/null 结果时用 `gpu_wait_for_claim(agent_name, request_id)` 继续轮询已有 request/lease，直到获得 `HELD` 或 `ACTIVE` lease、终止状态或超时；queued/null 不能据此执行；不要再为同一持续任务重复询问人工确认。
- 成功 lease 的具体落点只读返回的 `lease.resources[]`。每个 resource 提供 `endpoint`（`id`、`host`、`port`、`ssh_user`）、`gpus`（`id`、`gpu_uuid`、`gpu_index`）、`cuda_visible_devices` 和 `commitment`；Agent 不自行拼接或推断 placement。随后它通过项目既有执行路径启动或停止获授权的 workload；Broker 不代为启动或停止。启动后调用 `gpu_bind_observed_workload`，完成或启动失败后调用 `gpu_release`。
- 所有会重试的写操作都传入并复用同一个调用方生成的 `idempotency_key`。

通用资源工作流：

- `resource_providers` 与 `resource_monitor` 用于查看 GPU、主机 CPU/内存和外部 scheduler 三类资源；调度队列或 `PENDING` 作业不是裸机可用容量。它们是观察面，不替代服务端 `available` 准入投影。
- Agent 对当前任务提供由自身基准或历史运行得出的、从小到大的候选资源合同，先调用 `resource_evaluate_plan`。Broker 只在相邻扩容同时节省至少 10% 剩余时间且至少 120 秒时选择更大合同；首个收益不足的候选会终止扩容。
- CPU/内存-only 合同允许 `gpu_count=0`。已获配的主机容量通过 `resource_claim` 认领，完成后 `resource_release`；实际耗时用 `resource_record_actual` 追加记录。这些工具不会启动远端命令、绕过 owner、配额或新鲜 telemetry 校验。

端点是本机服务器清单。端点返回可选的 `owner_project_id` 归属说明和 `lifecycle_state`，但归属不构成管理权限。新增、draining 或退役 endpoint 仅在当前任务有明确人类授权时使用对应管理工具；`gpu_delete_server` 的语义是先转为 `draining`：不再接收新 placement，已有 workload 不会被停止，待关联租约和队列需求排空后再完成退役。

`gpu_request`、`gpu_request_status`、`gpu_cancel_request`、`gpu_activate_lease`、`gpu_release_lease` 与 `gpu_bind_workload` 是兼容性高级低层工具，不放在默认路径；新 Agent 优先采用上面的 claim / wait / execute / bind / release 流程。

Cursor MCP 示例：

```json
{
  "mcpServers": {
    "serverpilot": {
      "command": "serverpilot-mcp",
      "env": {"GPU_BROKER_URL": "http://127.0.0.1:8787"}
    }
  }
}
```

## 日常任务输入

Agent 不需要复制本仓库工作流，也不需要在其他项目写 GPU 说明。任务只给以下两种输入之一：

- 预设任务：明确 `profile_id` 和任务名，Agent 调用 `gpu_claim_profile`。
- 一次性 GPU 认领：明确任意非空 `project_id`、任务名、`gpu_count`，以及需要的 CPU 核数、系统内存 MiB、显存 MiB 等绝对值下限，Agent 调用 `gpu_claim`。
- CPU / 内存-only 或混合算力任务：提供从小到大的显式候选合同，先调用 `resource_evaluate_plan`，再以选定合同 `resource_claim`；可令 `gpu_count=0`，完成后调用 `resource_record_actual` 和 `resource_release`。

不要让 Agent 按工作目录、任务标题、空闲 GPU、profile 列表或 inventory 自己挑配置。CPU、内存和显存需求要按绝对值表达，例如 `min_available_cpu_cores=16`、`min_available_memory_mib=65536`、`min_free_vram_mib=61440`。除非任务明确指定服务器或 GPU，否则不传 placement，让 Broker 自己排队和选址。

同一个持续任务里已经明确过 `profile_id`，或已经明确过 `project_id` 与 `gpu_count` 时，Agent 应复用这份资源合同，不应重复询问。通过运行时 preflight 后，Agent 应主动调用 `gpu_claim_profile` 或 `gpu_claim`；如果进入队列，应调用 `gpu_wait_for_claim` 监控直到获配、终止或任务取消。

资源下限应尽量贴近任务真实需求；租约分配后，Agent 只从 `lease.resources[]` 取得 placement：每项都有 endpoint（`id`、`host`、`port`、`ssh_user`）、GPU（`id`、`gpu_uuid`、`gpu_index`）、`cuda_visible_devices` 与 `commitment`。它在 workload 支持时充分使用这些资源，例如把相互独立的 job 并行分布到租约内的 GPU 上，但不得启动 dummy 占卡进程或做不安全并发。Agent 通过项目已有执行路径启动或停止已获授权的 workload，而不是让 Broker 执行命令。

远端 workload 已经启动后，Agent 调用 `gpu_bind_observed_workload(agent_name, lease_id)`；任务结束或启动失败后调用 `gpu_release(agent_name, lease_id)`。这些动作只记录归属，不启动、不停止、不抢占远端进程。

## 其他项目

不要把本仓库的具体集群、Slurm、SSH 或 ServerPilot 细则复制到每个项目的 `AGENTS.md`。其他项目只需要完成两件全局配置：注册 `serverpilot` MCP，并安装本仓库维护的全局 Agent policy block，例如 Codex 使用：

```bash
python3 scripts/install_agent_policy.py codex --install
```

项目本地说明只记录该项目自己的资源合同，例如固定 `profile_id`，或一次性任务需要的 `project_id`、`gpu_count`、CPU / 内存 / 显存下限和任务名。若某项目已有旧的“直接 `ssh` / `sbatch`”Agent 规则，应删除具体登录节点、端口、脚本和手工 fallback，替换为短指针：

```text
GPU 和外部调度任务使用全局 ServerPilot MCP policy。外部集群是 SchedulerTarget；
Agent 调用 gpu_scheduler_targets / gpu_scheduler_access_status，并对发现的 target_id 使用项目 owner 的
scheduler 工具，不回退到项目本地 SSH 规则。
```

## 外部调度型服务器

任何 Slurm 集群都不是当前 Broker 的普通 SSH endpoint。目标任务明确使用外部 scheduler 时，
Agent 走同一条通用 scheduler 流程：

- 不把登录节点登记为普通 GPU 服务器。
- 不调用当前裸机 `gpu_claim` 表示已经获得外部集群 GPU。
- 先调用 `gpu_scheduler_targets`，从返回的 target ID 选择目标后再调用
  `gpu_scheduler_access_status(target_id)`；不得从项目本地 IP/端口规则猜入口。
- Profile 任务调用 `gpu_scheduler_submit_profile`；项目 owner 的一次性脚本调用
  `gpu_scheduler_submit_once`，并提供脚本与资源合同。
- 用 `gpu_scheduler_job_status` 读取 Job ID、状态和 AllocTRES；不调用
  `gpu_bind_observed_workload`，因为裸机 Collector 不观测动态计算节点。
- 不从 inventory、登录节点连通性或历史分区快照推断可用 GPU。
- VPN 只检测：`access_required` 时提示用户连接批准的 VPN 后重试，不自动操作。
- `gpu_scheduler_cancel` 需要当前任务的单独、明确人类授权；取消只作用于其作业。

Slurm `PENDING` 不创建裸机 lease；只有 Slurm 状态和 AllocTRES 能证明外部分配。
当前不公开数据上传、下载、任意远端 shell、交互式终端、续期或抢占。
已经打开、尚未热加载专用 scheduler 工具的旧任务，可从
`gpu_coordination.data.scheduler_targets[*].last_access` 读取最近一次接入观测；
不得因此回退到项目本地 SSH 规则。

服务器由人类在连接时配置：普通 endpoint 选择封闭的 `observation_profile`，外部
SchedulerTarget 选择封闭的 `transport_profile` 与 `inspection_profile`。这些字段只选择代码注册的
固定读取能力，不能承载命令、argv、shell、私钥、SSH option 或 secret；Agent 不选择、不修改
profile，也不按任何服务器名称编写分支。

## 验证

```bash
serverpilot-mcp --help
codex mcp get serverpilot --json
python3 scripts/install_agent_policy.py all --print
```

MCP 自动 ensure 仍失败或本机服务不兼容时，Agent 应报告不可用并停止；不得改读
SQLite、SSH、inventory 或 `nvidia-smi`。关闭 GUI 不影响后台服务。

规则安装和 MCP 注册是两个独立动作：安装器只维护全局规则块，不自动注册 MCP；Cursor 只打印需要粘贴的规则。
