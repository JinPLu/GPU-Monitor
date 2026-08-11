# ServerPilot Agent / MCP 全局安装与调度指南

MCP 的完整运行契约由 ServerPilot server instructions 和工具 schema 提供；英文全局适配块见 [`AGENT_MCP_policy.en.md`](AGENT_MCP_policy.en.md)，保留“GPU 相关任务可主动调度 ServerPilot、复用已批准资源合同、无容量时明确停止当次申请、禁止旁路和禁止推断缺失输入”的跨客户端规则。本文件只保留安装、注册和日常输入。

## 一次性安装

在本仓库根目录执行：

```bash
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

ServerPilot 默认将 inventory、租约和历史数据保存在
`~/Library/Application Support/ServerPilot/`。
首次安装会在目标不存在时迁移仓库里的 `configs/inventory.yaml` 和
`state/serverpilot.sqlite3`，并保留原文件作为回滚源。源码更新后重新执行
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
| Codex | `codex mcp add serverpilot --env SERVERPILOT_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `claude mcp add --scope user serverpilot --env SERVERPILOT_URL=http://127.0.0.1:8787 -- serverpilot-mcp` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | 在 `~/.cursor/mcp.json` 配置 `serverpilot` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

Codex 推荐只启用下面的日常工具，不要把全局工具权限改成全放行。
`gpu_set_keepalive` 只在本机已配置保活时保留，否则从列表删除。工具在 allowlist 中不代表当前任务
已获得管理授权；资源合同、项目归属和写操作仍由 MCP instructions、工具 schema 与当前任务决定。

```toml
[mcp_servers.serverpilot]
command = "serverpilot-mcp"
enabled_tools = [
  "control_plane_state",
  "gpu_coordination", "gpu_status", "gpu_list", "gpu_who", "gpu_list_profiles",
  "gpu_claim_profile", "gpu_claim",
  "gpu_bind_observed_workload", "gpu_release",
  "gpu_set_keepalive",
  "gpu_scheduler_targets", "gpu_scheduler_access_status", "gpu_scheduler_profiles",
  "gpu_scheduler_submit_profile", "gpu_scheduler_submit_once",
  "gpu_scheduler_job_status",
  "resource_providers", "resource_monitor", "resource_claims",
  "resource_evaluate_plan", "resource_claim", "resource_release", "resource_record_actual",
]

[mcp_servers.serverpilot.env]
SERVERPILOT_URL = "http://127.0.0.1:8787"
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

## 项目资源卡：把日常申请变成开箱即用

全局安装让 Agent 知道 ServerPilot 的规则；每个需要 GPU 的项目再保存一张很小的
“资源卡” `.serverpilot/resource-card.json`。它只指向一个已创建、启用的 direct-GPU
预设任务和项目自己的入口名称，**不**包含服务器、IP、GPU 编号、CUDA selector、路径、命令或 SSH
内容。项目管理员只在接入项目时配置一次；以后的 Agent 不再填写或猜测 profile、显存、服务器或卡号。

先通过既有 ServerPilot 管理界面/受控管理接口创建该项目的 direct-GPU 预设任务，再在项目根目录运行：

```bash
serverpilot project resource-card write project-default-gpu \
  --entrypoint project-gpu-run
serverpilot project resource-card check
```

`write` 只会在预设任务存在、已启用且确实是 direct-GPU 时落盘；`check` 在安装、升级和 CI
中重复校验。资源卡缺失、格式错误、预设任务不存在/禁用，都会明确报为“项目资源卡未配置”，
不是 Agent 去扫描 inventory 或补参数的问题。项目的 `AGENTS.md` 只需写一行：

```text
GPU 任务使用 .serverpilot/resource-card.json 和全局 ServerPilot MCP policy。
```

## Agent 调度行为准则

ServerPilot 的分工很明确：Agent 按明确合同申请、执行和归还；人类通过服务器、使用情况和历史图监督利用率与归属。Agent 不自行计算“还有多少可用资源”——服务端的 `available` 投影才是准入真相；stale、unknown、unmanaged、conflict 或 maintenance 一律不可分配，不能以 `capacity - used` 替代。

日常裸机任务只走一条路径：**claim → execute → bind → release**。

1. 项目有 `.serverpilot/resource-card.json` 时，读取其中唯一的 `profile_id` 并调用 `gpu_claim_profile`；否则合同已含 `profile_id` 时同样调用它。只有两者都没有且任务完整给出 `project_id`、任务名、`gpu_count` 与 CPU / 内存 / 显存绝对阈值时，才调用 `gpu_claim`。
2. claim 只会立即返回 `HELD` / `ACTIVE` lease，或以 `no_capacity` 失败。`no_capacity` 不创建队列、不授权执行；不得自行发明等待/轮询流程或绕过 ServerPilot。
3. 只使用返回的 `lease.resources[]` 落点；不从 inventory、IP、旧快照或进程列表推断 host、GPU、CUDA selector 或可用容量。
4. Agent 只通过项目既有、已获授权的执行路径启动 workload；观测到后 bind，完成或启动失败后 release。每次重试 mutation 均复用同一个 `idempotency_key`。
5. 任务结论应回报 request / lease 或 scheduler job ID、最终状态与任何阻塞原因，方便人类在 ServerPilot 中继续跟踪。

Codex 启动 MCP 时，adapter 会自动从当前 task 派生独立 actor 和
`codex://threads/<uuid>` 协调 URI；协调投影可将该 URI 返回给其他 Agent，用于发现资源的当前任务。
该元数据不授予租约权限，App 也不展示消息或协调链接。

`gpu_set_keepalive` 是 endpoint 级的显式策略开关，但实际保活、健康和停止按每张 GPU 独立
执行。开启后只调和当前已验证的空闲 GPU；有任务、未托管、冲突或 stale 的 GPU 不受影响，稍后
重新空闲时可加入。普通 ServerPilot 受管即时 claim 在无容量时，可仅回收服务规划出的精确、
已验证保活 GPU，随后以新鲜空观测重试原有 claim；这不会触碰同机其他 GPU，也不会成为直接 SSH
任务的抢占机制。直接 SSH / 非受管任务必须在启动前显式关闭该 endpoint 的空闲占卡策略。此操作
属于 endpoint 管理，必须带当前任务的非空 `approval_ref` 和调用方生成、重试复用的
`idempotency_key`。

端点添加、更新、pause、resume、retire、保活切换、取消外部作业等属于管理动作。只有当前任务给出明确人类授权时才调用；每个端点管理 MCP 写操作还必须提供非空的当前任务 `approval_ref` 和调用方生成、可重试复用的非空 `idempotency_key`；`owner_project_id` 仅供归属展示，不构成管理权限。

默认工作流：

- `control_plane_state`、`gpu_coordination`、`gpu_status`、`gpu_list`、`gpu_who` 与 `gpu_list_profiles` 可读取 ServerPilot 状态和可用资源合同；普通预批准 profile 或显式合同 claim 不要求先读协调看板。
- 当前项目有有效资源卡时直接用它的 `profile_id` 调用 `gpu_claim_profile`；否则当前任务已给出 `profile_id`，或已给出 `project_id`、`gpu_count` 与必要阈值时，直接用 `gpu_claim_profile` 或 `gpu_claim`。成功时立即获得 `HELD` 或 `ACTIVE` lease；`no_capacity` 表示当次未分配且没有队列，应回报阻塞原因并停止当次执行。资源卡/预设任务不完整时明确报告“项目资源卡未配置”，不扫描可用卡来兜底。
- 成功 lease 的具体落点只读返回的 `lease.resources[]`。每个 resource 提供 `endpoint`（`id`、`host`、`port`、`ssh_user`）、`gpus`（`id`、`gpu_uuid`、`gpu_index`）、`cuda_visible_devices` 和 `commitment`；Agent 不自行拼接或推断 placement。随后它通过项目既有执行路径启动或停止获授权的 workload；ServerPilot 不代为启动或停止。启动后调用 `gpu_bind_observed_workload`，完成或启动失败后调用 `gpu_release`。
- 所有会重试的写操作都传入并复用同一个调用方生成的 `idempotency_key`。

通用资源工作流：

- `resource_providers` 与 `resource_monitor` 用于查看 GPU、主机 CPU/内存和外部 scheduler 三类资源；调度队列或 `PENDING` 作业不是裸机可用容量。它们是观察面，不替代服务端 `available` 准入投影。
- Agent 对当前任务提供由自身基准或历史运行得出的、从小到大的候选资源合同，先调用 `resource_evaluate_plan`。ServerPilot 只在相邻扩容同时节省至少 10% 剩余时间且至少 120 秒时选择更大合同；首个收益不足的候选会终止扩容。
- CPU/内存-only 合同允许 `gpu_count=0`。已获配的主机容量通过 `resource_claim` 认领，完成后 `resource_release`；实际耗时用 `resource_record_actual` 追加记录。这些工具不会启动远端命令、绕过 owner、配额或新鲜 telemetry 校验。

端点是本机服务器清单。端点返回可选的 `owner_project_id` 归属说明和 `lifecycle_state`，但归属不构成管理权限。`gpu_add_server`、`gpu_update_server`、`gpu_pause_server`、`gpu_resume_server` 和 `gpu_retire_server` 只能在当前任务有明确人类授权、并带 `approval_ref` 与稳定 `idempotency_key` 时使用。pause 是 `active → draining`：不再接收新 placement，但采集和已有 lease/workload 继续；resume 是 `draining → active`；retire 只能在 draining 且关联活跃 lease 已清空后执行，历史证据保留。兼容工具 `gpu_delete_server` 已弃用且只等同于 pause，不会通过重复调用自动退役。

端点的 `observation_profile` 只能选择代码封闭的只读协议，不能携带命令、argv、shell、SSH 选项或秘密。新 REST/MCP 创建端点默认使用 `server-script-v1`；既有 inventory/档案中未指定 profile 的端点保持兼容的 `linux-nvidia`。`server-script-v1` 固定对应部署拥有的 `serverpilot-collect --schema-version 1` 只读协议；记录里不会保存或接受可执行命令配置。

旧版 request / lease 低层工具只为兼容保留，不属于公开日常路径。新 Agent 只采用上面的 claim / execute / bind / release 流程；不得从兼容工具推导等待或排队语义。

Cursor MCP 示例：

```json
{
  "mcpServers": {
    "serverpilot": {
      "command": "serverpilot-mcp",
      "env": {"SERVERPILOT_URL": "http://127.0.0.1:8787"}
    }
  }
}
```

## 日常任务输入

Agent 不需要复制本仓库工作流，也不需要在每个任务里重新写 GPU 参数。日常项目优先用资源卡；只有没有资源卡的临时任务才给下列输入之一：

- 项目资源卡：项目根目录 `.serverpilot/resource-card.json` 指向唯一已启用预设任务；Agent 读取 `profile_id` 和项目入口名，再调用 `gpu_claim_profile`。
- 预设任务：明确 `profile_id` 和任务名，Agent 调用 `gpu_claim_profile`。
- 一次性 GPU 认领：明确任意非空 `project_id`、任务名、`gpu_count`，以及需要的 CPU 核数、系统内存 MiB、显存 MiB 等绝对值下限，Agent 调用 `gpu_claim`。
- CPU / 内存-only 或混合算力任务：提供从小到大的显式候选合同，先调用 `resource_evaluate_plan`，再以选定合同 `resource_claim`；可令 `gpu_count=0`，完成后调用 `resource_record_actual` 和 `resource_release`。

不要让 Agent 按工作目录、任务标题、空闲 GPU、profile 列表或 inventory 自己挑配置。CPU、内存和显存需求要按绝对值表达，例如 `min_available_cpu_cores=16`、`min_available_memory_mib=65536`、`min_free_vram_mib=61440`。除非任务明确指定服务器或 GPU，否则不传 placement，让 ServerPilot 自己选址。

同一个持续任务里已经明确过 `profile_id`，或已经明确过 `project_id` 与 `gpu_count` 时，Agent 应复用这份资源合同，不应重复询问。通过运行时 preflight 后，Agent 应主动调用 `gpu_claim_profile` 或 `gpu_claim`；如果返回 `no_capacity`，当次申请立即结束，不得将其视为排队授权。

资源下限应尽量贴近任务真实需求；租约分配后，Agent 只从 `lease.resources[]` 取得 placement：每项都有 endpoint（`id`、`host`、`port`、`ssh_user`）、GPU（`id`、`gpu_uuid`、`gpu_index`）、`cuda_visible_devices` 与 `commitment`。它在 workload 支持时充分使用这些资源，例如把相互独立的 job 并行分布到租约内的 GPU 上，但不得启动 dummy 占卡进程或做不安全并发。Agent 通过项目已有执行路径启动或停止已获授权的 workload，而不是让 ServerPilot 执行命令。

远端 workload 已经启动后，Agent 调用 `gpu_bind_observed_workload(lease_id)`；任务结束或启动失败后调用 `gpu_release(lease_id)`。Codex 会自动使用当前 task 的身份；其他客户端可保留传入自己的稳定 `agent_name`。这些动作只记录归属，不启动、不停止、不抢占远端进程。

## 其他项目

不要把本仓库的具体集群、Slurm、SSH 或 ServerPilot 细则复制到每个项目的 `AGENTS.md`。其他项目只需要完成两件全局配置：注册 `serverpilot` MCP，并安装本仓库维护的全局 Agent policy block，例如 Codex 使用：

```bash
python3 scripts/install_agent_policy.py codex --install
```

项目本地说明优先只记录 `.serverpilot/resource-card.json` 的短指针；资源卡本身指向固定预设任务和入口名。一次性任务才记录完整 `project_id`、`gpu_count`、CPU / 内存 / 显存下限和任务名。若某项目已有旧的“直接 `ssh` / `sbatch`”Agent 规则，应删除具体登录节点、端口、脚本和手工 fallback，替换为短指针：

```text
GPU 和外部调度任务使用全局 ServerPilot MCP policy。外部集群是 SchedulerTarget；
Agent 调用 gpu_scheduler_targets / gpu_scheduler_access_status，并对发现的 target_id 使用项目 owner 的
scheduler 工具，不回退到项目本地 SSH 规则。
```

## 外部调度型服务器

任何 Slurm 集群都不是当前 ServerPilot 的普通 SSH endpoint。目标任务明确使用外部 scheduler 时，
Agent 走同一条通用 scheduler 流程：

- 不把登录节点登记为普通 GPU 服务器。
- 不调用当前裸机 `gpu_claim` 表示已经获得外部集群 GPU。
- 先调用 `gpu_scheduler_targets`，从返回的 target ID 选择目标后再调用
  `gpu_scheduler_access_status(target_id)`；不得从项目本地 IP/端口规则猜入口。
- Profile 任务调用 `gpu_scheduler_submit_profile`；项目 owner 的一次性脚本调用
  `gpu_scheduler_submit_once`，并提供脚本与资源合同。
- 用 `gpu_scheduler_job_status` 读取 Job ID、状态和 AllocTRES；不调用
  `gpu_bind_observed_workload`，因为裸机 Collector 不观测动态计算节点。
- 不从 inventory、登录节点连通性或历史分区快照推断 GPU 容量。
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
serverpilot project resource-card check
```

MCP 自动 ensure 仍失败或本机服务不兼容时，Agent 应报告不可用并停止；不得改读
SQLite、SSH、inventory 或 `nvidia-smi`。关闭 GUI 不影响后台服务。

规则安装和 MCP 注册是两个独立动作：安装器只维护全局规则块，不自动注册 MCP；Cursor 只打印需要粘贴的规则。
