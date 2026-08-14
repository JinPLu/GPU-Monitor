# 更新日志

[English](CHANGELOG.en.md)

这里只记录用户能感受到的变化；实现细节见 Git 提交。

## 1.5.6 - 2026-08-15

**ServerPilot 1.5.6 修复了任务归还后自身占卡 worker 被误报为“任务使用中”的问题。**

- Agent 的日常合约不变：任务结束仍只需调用 `gpu_release`；ServerPilot 自行恢复空闲占卡，Agent 不需要关闭占卡开关。
- helper 会对它自己登记的 v3 worker 作只读身份证明，并证明目标卡唯一的 driver-visible PID；Broker 仅在该证明与新鲜采集的 PID/boot identity 一致时重新登记 worker。占卡进程重启、daemon 重启或短暂控制面丢失后，不会再把 ServerPilot 自己的 worker 当作外部任务。
- 证明不一致、状态损坏或额外业务进程仍保持 fail closed，绝不自动收养；Agent 会得到“占卡校验失败，暂不可申请”，而不是误导性的“任务使用中”。
- 已确认停止的 keeper 会清除旧进程身份，避免下一次启动拿旧 PID 误比对新 worker。

## 1.5.5 - 2026-08-14

**ServerPilot 1.5.5 将占卡协议升级到 v3，并收紧不同 GPU/驱动环境中的设备选择与控制面可靠性。**

- 占卡 adapter 启停前先执行只读 `--protocol-info` 预检，确认 v3、pidfd identity 与 PCI bus ID 能力；预检不通过时返回 `keepalive_helper_incompatible`，不发送 mutation。
- 占卡 wire/state 使用 v3 与 `workers.v3.json`；旧 v2 payload/state 不读取、不删除、不停止，直接 fail closed。

- `gpu_index` 只保留服务器显示编号；collector schema v2 另按 PCI bus 生成 `cuda_ordinal`。`gpu_apply` 返回 `cuda_device_order=PCI_BUS_ID`、租约 ordinal 集合和逐卡 ordinal，不再把 GPU UUID 当作 `CUDA_VISIBLE_DEVICES`。
- 没有当前 CUDA ordinal 的 GPU 不参与分配；旧 collector schema 与旧 PID-only 占卡状态直接 fail closed，不再收养或降级处理。
- 占卡 worker 持久保存 PID、Linux boot ID、进程启动 ticks 与固定 marker；停止时通过 pidfd 锁定进程。即使目标 Python 没有 pidfd 包装，仍调用 Linux pidfd syscall，不退回普通 PID signal。
- 修正请求体实际限流与断连转发、并发限流、Web CSRF、CSV 字段投影和 SQLite 原子备份；普通 Agent 改派继续受 lease owner 限制，App operator 使用独立纠错路由。
- CPU/内存 admission 同时计入 direct GPU commitments 与 generic host claims；routine MCP 的传输重试使用进程内调用命名空间，不会把后续同参数申请折叠为旧 lease。

## 1.5.3 - 2026-08-14

**ServerPilot 1.5.3 让 Agent 可以直接连接服务器，同时不再混淆工作目录和代码路径。**

- routine `gpu_status` 与 `gpu_apply` 返回结构化 `ssh {host, port, user}`；Agent 在申请成功后直接 SSH 执行 workload，不再把“未加入 Codex saved hosts”误报成服务器不可达。
- 明确 `workspace_path` 是 SSH 后操作所在的远端工作目录，不是代码仓库路径；Agent 先进入该目录，再在其下进行命令、代码同步与产物操作。
- 新增机器可读的 `workspace {path, kind=working_directory, use_as_cwd=true, code_location=not_provided}`，将连接参数、工作目录、代码位置和 CUDA selector 分成四个独立概念；保留 `workspace_path` 兼容字段。
- Agent 规则明确区分正常 SSH 执行与绕过 ServerPilot 查卡、选卡、申请或释放 GPU。

## 1.5.2 - 2026-08-13

**ServerPilot 1.5.2 清理了“归属待确认”的误报，并恢复 App 的人工纠错能力。**

- **任务正常重启不再误报归属冲突。** routine Agent 的旧进程已经全部退出、且完整采集确认每张租用 GPU 都出现同一轮新进程时，ServerPilot 自动更新观察归属；新进程只有稳定出现才会触发冲突，完整采集确认重试间隙为空时会保留租约并清除瞬态冲突；旧新进程稳定并存、不完整采集和高级显式 binding 仍然 fail closed。
- **历史错误会真正结束。** 租约释放或失去活动资源后，`lease_process_conflict` 与 `orphaned_busy` 告警立即关闭；升级启动与 reconcile 会清理旧版本遗留的陈旧告警。
- **App 状态与 broker 保持一致。** 原生 App 直接采用 `desired / actual / publicly_available / public_status`，不可申请的卡不再显示“可用”，`desired=ON, actual=OFF` 明确显示“占卡未运行”。
- **人类可以释放孤立的 Agent 租约。** App 使用独立的 operator 纠错路由释放确认已结束的任务；日常 Agent 仍只能释放自己的 lease。

## 1.5.1 - 2026-08-13

**ServerPilot 1.5.1 让 Agent 更容易把 GPU 租约正确转换为远端单卡或多卡工作负载。**

- **单卡进程不再需要猜 selector。** `gpu_apply` 保留既有 `cuda_visible_devices` 完整集合，并在 `gpus[]` 每项新增 `gpu_cuda_visible_devices` 单 UUID，兼容原有多卡调用的同时支持每卡一个进程。
- **远端工作区边界更清楚。** `workspace_path` 明确是所选服务器上的路径；Agent 要先通过当前获授权的远端端点进入，不会再把它当成本地 Codex worktree 路径。
- **运行时失败更快让出 GPU。** 日常契约要求 workload 前先用返回的 selector 做最小 CUDA 初始化；失败立即释放，并在当前任务中记住不兼容的服务器，避免立即重试同一组合。
- **无容量和传输失败不再触发无效轮询。** `no_capacity` 留到下一 turn 或后续工作周期；transport 失败最多重试一次，且不会被误报为容量不足。
- **并行任务的释放责任更明确。** 申请者显式跟踪每个 `lease_id`，即使把执行交给子任务，也要等到每个 lease 明确返回 `released=true`。

## 1.5.0 - 2026-08-12

**ServerPilot 1.5.0 把日常 GPU 协调和原生监控收敛为更直接的工作流。**

- **空闲占卡按 GPU 独立管理。** endpoint 只保留一个持久开关，ServerPilot 为每张合格空闲卡维护独立 worker；Agent 申请时只让选中的占卡进程退出，不影响同机其他 GPU。
- **没有容量就明确失败。** 裸机 GPU 申请只会立即返回 lease 或 `no_capacity`，不再产生隐式等待队列；Agent 只使用返回的 endpoint、GPU 与 CUDA selector。
- **项目可以声明资源卡。** `.serverpilot/resource-card.json` 为项目保存可校验的 direct-GPU 预设合同，减少 Agent 根据任务名或当前空闲量猜配置。
- **App 聚焦日常监控。** 原生界面收敛为“服务器 / 使用情况 / 设置”，统一展示服务器资源、当前项目与任务、逐卡状态和历史趋势。
- **归属、采集和占卡继续 fail closed。** stale、未知、非托管进程、冲突或维护中的 GPU 不会被投影为可申请资源。

## 1.4.0 - 2026-08-10

**ServerPilot 1.4.0 建立了统一资源控制面和可独立运行的原生桌面体验。**

- **GPU、CPU、内存和外部调度目标共享资源合同。** 服务端统一计算 capacity、used、claimed 与 available；GUI、CLI 和 MCP 不再各自推算可用量。
- **服务器采集扩展为受限脚本协议。** `server-script-v1` 只允许固定入口输出受校验的只读快照；脚本缺失、超限或格式错误都会安全闭锁。
- **原生 App 提供完整资源总览。** 可搜索、筛选和排序服务器，并按 endpoint 查看 CPU、内存、GPU 利用率和显存的 1h / 6h / 24h 历史。
- **后台服务与 App 生命周期分离。** 用户级 daemon 长期持有状态，关闭 GUI 不会停止控制面；App 内置后端和迁移，可完成独立构建与验证。
- **资源归属与外部调度边界更清楚。** 项目、Agent、任务、空 lease 和队列使用同一投影；Slurm 通过独立受限 adapter 接入，不伪装成普通 GPU 服务器。

## 1.3.0 - 2026-08-10

**ServerPilot 1.3.0 移除了面向特定服务器或集群的硬编码。**

- Endpoint 使用固定 `observation_profile`，外部 scheduler 使用受限的 transport / inspection profile。
- 本机管理员把 profile 映射到可信绝对路径 wrapper；API、App 和 MCP 不能提交任意 shell、argv 或环境变量。
- 未知或未配置 profile 一律 fail closed；旧 scheduler 命令配置升级后会停用，等待管理员重新选择安全 profile。
- 文档和运行契约改为通用 adapter 模型，不再依赖单个集群名称。

## 1.2.0 - 2026-08-10

**GPU Broker 正式更名为 ServerPilot。**

- GitHub 仓库、macOS / Windows App、Web UI、文档和公共 API 标题统一使用 ServerPilot。
- 新增 `serverpilot` 与 `serverpilot-mcp` 命令入口。
- 保留旧 `gpu-broker`、`gpu-broker-mcp`、`GPU_BROKER_*`、daemon identity 和数据目录兼容入口，升级不丢失 inventory、历史、租约或 MCP 配置。
- `/api/v1/state` 与既有调度语义保持兼容。

## 1.1.0 - 2026-08-10

**GPU Broker 1.1.0 扩展了服务器遥测和原生资源监控。**

- 新增封闭的 observation / scheduler adapter 边界，没有开放第二套认证或远程命令控制面。
- CPU、内存、GPU、claim 与 availability 使用 fail-closed 资源投影；CPU-only endpoint 不再伪造 GPU 容量。
- 保存受限的 endpoint 遥测历史，并按稳定 GPU UUID 展示逐卡序列。
- macOS 资源页加入 CPU、内存、GPU 利用率和 GPU 显存图表，按需读取历史并降低悬停渲染开销。
- `/api/v1/state` 继续作为分配状态的权威快照，既有 lease、CLI、MCP 和 Slurm 语义保持兼容。

## 1.0.0 - 2026-08-06

**GPU Broker 首个稳定版本。**

- App、CLI 与 MCP 统一读取权威的 `/api/v1/state` 快照。
- macOS 原生 App 以稳定 ID 协调服务器、项目和资源状态；删除服务器后相关页面同步更新。
- revision 与资源使用来自同一份已提交控制面快照，减少跨页面和跨客户端的数据分叉。
- loopback REST 与领域服务成为唯一公共业务路径。
