# Adapter 能力与边界

ServerPilot 用 adapter 接入不同的观测和调度实现，但 adapter 不是第二个控制面。
资源身份、容量、认领、租约、审计和准入判断始终由 ServerPilot 服务持有。

## 当前注册能力

| Adapter | 能力 | 作用 | 不负责 |
| --- | --- | --- | --- |
| `raw-ssh` | `observation` | 仅运行代码内固定的主机/GPU telemetry probe，或以已观测正 PID 构成的固定 `ps` 详情 probe | 任意 shell、环境变量、私钥读取、lease/claim 写入 |
| `server-script-v1` | `endpoint_keepalive` | 通过固定 `serverpilot-keepalive --schema-version 2` 协调显式配置的逐 GPU 空闲保活 | 项目任务启停、调用方指定 GPU、任意 shell、路径或环境 |
| `slurm-command` | `scheduler` | 执行代码固定的 Slurm 查询、提交、取消和上传路径 | 把 SchedulerTarget 变成 Endpoint、分配裸机 GPU、绕过 approval |

Registry 也是封闭的：未知 adapter 或不存在的 capability 会 fail closed。它并不从配置读取
`argv`、shell、环境或 Agent target。固定 operation 只有带输入/输出 schema 与审批要求的已注册
操作；目前只为既有 Slurm contract 保留元数据，未新增通用 REST 或 MCP 执行入口。

服务器连接配置只能选择代码注册的 profile，不能提供读取命令：Endpoint 的
`observation_profile` 当前可选 `server-script-v1`、`linux-nvidia` 或 `linux-host`；其中
`server-script-v1` 只会调用固定的 `serverpilot-collect --schema-version 1`，完整安装与 JSON
合同见 [COLLECTOR_SCRIPT_zh.md](COLLECTOR_SCRIPT_zh.md)。SchedulerTarget 的
`transport_profile` 是受限 profile ID，`inspection_profile` 可选
`slurm-basic` 或 `slurm-capacity`。因此某个集群的读取差异以通用 profile 表达，由人类在
连接时选择；新增 profile 必须同时新增固定探针、固定 parser 与测试，不能靠目标 ID、IP 或
配置中的 shell/argv 分支。

本机管理员用 `SERVERPILOT_SCHEDULER_TRANSPORTS` 维护 profile 到绝对包装器路径的 JSON 映射，例如
`{"cluster-a":"/opt/serverpilot/cluster-a-router","cluster-b":"/opt/serverpilot/cluster-b-router"}`。
每个包装器只接收 ServerPilot 在最后一位传入的固定远端命令。为了兼容单目标部署，
`SERVERPILOT_SCHEDULER_HELPER=/绝对路径/包装器` 只映射 `default` profile。
SchedulerTarget API 只保存 `transport_profile` 和 `inspection_profile`，不会保存路径或额外参数。
未配置 profile、遗留 `command_prefix` 或未知 profile 一律 fail closed；升级时旧 target 会被去除
argv 并禁用，等待管理员重新配置。

`raw-ssh` 的公开执行入口也不接收命令字符串：它只接受 `endpoint-telemetry` 与
`process-details` 两个 probe 标识；后者仅可使用采集到的正整数 PID。这样即使未来调用点变多，
配置、Agent 输入和 endpoint 元数据也不能成为远程 shell 内容。

## 开发机保活 adapter

Endpoint 只能通过可选的 `keepalive_adapter_id: server-script-v1` 显式 opt in。REST 和 MCP
的请求仍只有 `enabled: true|false`，对应 endpoint 的期望策略
`disabled` / `idle_keepalive`；不接受 shell、命令路径、环境变量、PID、GPU selector
或每卡参数。一次开启会只为**当前符合条件的空闲 GPU**分别创建独立 worker；同一台机器上
有项目任务、未托管进程、stale 或冲突的 GPU 保持不动，之后重新空闲时由采集后的调和补入。
远程 helper 只会启停自己创建的、由 ServerPilot 传入精确物理 UUID 的 worker，停止前再以 PID、
Linux `/proc` start ticks 和固定 worker 标识验证身份。

当前固定策略的代码目标是每卡约 31% 显存、30% GPU duty cycle、单 PyTorch CPU 线程和
100ms 节流周期；worker 的稳态不读写磁盘或网络，状态文件只在调和时访问。该设置减少
CPU / I/O 干扰，但其实际 CPU、RSS、显存、五分钟 GPU 利用率与主任务干扰仍需在目标硬件上
验收，不能由参数或单元测试代替。远程前置为：

- 同版本 `serverpilot-keepalive --schema-version 2` 在固定 SSH 命令路径上可执行；
- Python 环境含可用的 PyTorch CUDA，且每个 worker 只能看到其被验证的一张物理 GPU；
- endpoint 已显式配置 `keepalive_adapter_id`。

服务为每张目标 GPU 建立内部 lease，再以一次新鲜的单 endpoint Collector 观测核对各自的
worker 证据；额外进程、stale/不完整观测、远程超时或结果不确定均 fail closed。旧版整机
lease 不会被迁移猜测为逐卡 lease，必须显式停止并做空观测后才能恢复。Collector 自身仍只做
固定读取；可变更的只是这个独立、封闭的 keepalive adapter。

普通受管即时 claim 先走原有分配；仅在无容量且服务能规划出**完整、精确、已验证的逐卡
keepalive** 落点时，才停止那些 GPU、做新鲜空观测、释放对应 lease，并在同一 endpoint 锁内
重试普通 claim。它不是泛化抢占：不会触碰同机其他 GPU、legacy lease、未托管进程或直接 SSH
任务；任一步不确定就保持 `no_capacity`。受管 workload release 后，仍开启策略的已验证空闲
GPU 可在后续采集调和中恢复占卡。直接 SSH / 非 ServerPilot 工作仍须先显式关闭该 endpoint
的空闲占卡策略。

关闭操作不会把“数据库里没有 keepalive lease”当成已经停止：每张被停止的 GPU 都会幂等调用远程
helper，并用一次新的目标 endpoint 观测确认**该 GPU**没有进程后才释放它的内部 lease。helper 的
worker 身份保存在服务器用户的私有持久目录：优先
`${XDG_STATE_HOME}/serverpilot/keepalive`，否则为
`~/.local/state/serverpilot/keepalive`；目录和状态文件会校验 owner、私有 mode 与
no-follow。若状态文件在 worker 仍运行时丢失，helper 不会扫描或猜测 PID，更不会停止未知
进程；随后观测到的残留会让关闭 fail closed。再次启用则会在 adapter 执行前被 ServerPilot 的
未托管进程观测阻止，避免启动第二个 worker。

## 预留能力

- `provisioning-preview` 只能生成版本化、不可执行的部署说明或预览。
- `operation` 必须是固定 ID、代码内固定参数形状、明确 approval、审计和不确定结果语义的操作。

这两项不是远端执行授权。任何实际 provisioning 或新的 mutating operation 必须先有独立的
业务合同和当前任务批准。

## 对 Agent 和 UI 的影响

Agent 与 UI 继续使用统一的资源监控、即时 claim、lease 和 scheduler 合同，不需要选择 vendor
adapter，也不需要额外 discovery/configuration 往返。adapter ID 只可作为诊断 provenance，不能
作为资源身份；endpoint 仍由 endpoint `id` 标识，GPU 仍由 `endpoint_id:gpu_uuid` 标识。

## 明确禁止

- adapter 获取 `Session`、`BrokerService`、`ActorContext` 或 lease/claim repository；
- adapter 创建、修改或释放 lease、claim 或其他资源归属记录；
- 配置驱动的任意 `argv`、shell、环境、Agent target 或通用 `execute()`；
- 为 adapter 引入第二套认证、注册 token、匿名 observer API 或非 loopback listener；
- 将 SchedulerTarget 合并为普通 Endpoint。
