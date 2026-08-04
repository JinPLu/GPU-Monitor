# gpu-broker 实施状态

更新时间：2026-08-02（Asia/Shanghai）

## 当前状态

- 本机功能版已交付：GUI、CLI 和 MCP 共用 REST 领域逻辑，可查看、认领、排队、预约和释放 GPU。
- macOS 控制面改由用户级 headless LaunchAgent 长期持有，唯一数据目录是
  `~/Library/Application Support/GPU Broker/`。MCP 首次 REST 调用会执行
  带 daemon identity 的 health/ready 检查并在单进程锁内自动 ensure；迁移先在
  同目录临时文件中校验，再以 no-clobber 原子发布。GUI 仅作为客户端，启动时先
  ensure，退出时不再终止后端。MCP 仍不直连 SQLite/SSH，也不会创建备用状态目录。
  Windows 首轮保持原行为。
- 默认无登录且仅监听 loopback；Collector 固定只读，异常、失效 telemetry 和非托管进程均 fail closed。
- 裸机租约不替 Agent 执行命令。Agent 获得 lease 后，用项目既有执行路径在
  `lease.resources[]` 返回的 endpoint、GPU、`cuda_visible_devices` 上运行或停止已获授权的
  workload；Broker 只协调分配与观测归属。外部 Slurm 是独立的
  `SchedulerTarget/SchedulerJob` adapter，由项目 owner 提交、查询和取消其作业。
- endpoint 是项目资源。新 endpoint 必须声明 `owner_project_id`；项目 Agent 可自行添加、
  更新、维护、drain 和 retire 自己的 endpoint，无需管理员角色。删除的第一步是
  `active → draining`：不再放置新任务，但不停止既有任务，也不删除遥测或审计；活跃 lease
  结束后第二次操作转为 `retired`。
- Windows 桌面源码构建入口已加入：PowerShell + PyInstaller 生成 `dist/windows/GPU Broker/GPU Broker.exe`，运行时写入 `%LOCALAPPDATA%\GPU Broker`，启动同一 loopback REST/Web UI，不复制调度、租约或审计规则；它仍不是已签名安装包。
- 预设任务（workload profile）是可选的持久化资源合同：明确 `profile_id` 的 GUI/MCP 认领只传配置与任务，服务在立即或排队后分配时自动激活。没有 profile 时，一次性认领需要任意非空项目标识、任务、GPU 数量，以及需要的 CPU 核数、系统内存 MiB、单卡总显存/可用显存 MiB 等绝对值下限，任务名自动记录为用途；项目标识首次使用会自动登记为中性归属标签，不需要预创建、项目管理入口或服务器项目授权。申请不再要求预计占用时间，任务完成后释放租约；最大租约窗口是调度与未来预约共同遵守的硬边界。Agent 不得根据任务、目录或空闲容量自行挑选 profile，也不得推断项目、GPU 数量或 CPU/内存/显存需求。
- Broker 提供面向 Agent 的共享协调看板：`gpu_coordination` 返回服务器、GPU、队列、
  空租约、受管与未归属进程和实际利用率。成功 lease 还带 endpoint/GPU 结构化资源与
  CPU、内存承诺；CUDA selector 只来自该返回值。`gpu_bind_observed_workload` 将已启动且
  已被 Collector 观测到的 lease 进程登记为受管，不启动或停止进程。
- macOS 桌面总览是人类的实时观察面：区分已登记、在线/新鲜和可分配容量，并显示空租约、
  队列与 stale 信号；不再把人工操作当作资源调度的必经步骤。
- macOS App 已把 Python 后端、数据库迁移和空 inventory 冻结进 `Contents/Resources/BrokerRuntime`。
  目标 Mac 不需要额外安装 Python、`uv` 或 `gpu-broker` CLI；构建机仍需 Python 3.12 与 `uv`，
  PyInstaller 只作为构建期工具解析。独立验证会从 `/tmp` 冷启动内置后端、执行新库迁移、读取
  snapshot，并确认 Swift 前端没有非系统动态库依赖。正式产物安装在不受项目同步盘 Finder
  元数据污染的 `~/Applications/GPU Broker.app`，`dist/GPU Broker.app` 保持为项目入口链接。
- Collector 同时展示每台服务器的 CPU 核数、1 分钟负载及系统内存可用量/总量；调度会将
  lease 的 CPU/内存需求持久化为 endpoint commitment，并按剩余量及显存下限筛选候选资源。
- Collector 成功完成一次 endpoint 全量观测后，Broker 会把该 endpoint 未再次出现的历史 GPU
  标记为 absent，而不是删除遥测、审计或 lease。snapshot/API 的活跃 GPU 列表、总数和分配候选
  只使用最新完整集合；缺失 GPU 的 lease 引用保留为 fail-closed 诊断信号。采集失败、超时、
  malformed 或显式非完整观测不会改变既有 presence；晚到且 `observed_at` 早于当前 endpoint
  最新观测时间的结果会被兼容地忽略，不更新 provider、presence、进程或队列。
- 外部 Agent 的完整运行契约由 `gpu-broker` MCP instructions 和工具 schema 提供：任务已
  给出 profile 或项目资源合同时，Agent 主动 claim、等待队列、按 lease 执行、绑定并释放；
  不能从 inventory、空闲容量或任务名猜合同，也不能旁路 Broker。
- 已实现独立外部 Slurm adapter：`SchedulerTarget` 全局发现、`SchedulerJob` 与追加式状态
  事件持久化，MCP 提供目标/接入查询、Profile 或带资源合同的一次性提交、状态刷新与 owner
  cancel。瀚海22不会注册为普通 endpoint；脚本默认只保存摘要，VPN 只检测不自动操作，
  Slurm `PENDING` 不创建裸机 lease。staged upload 暂不公开。

## 验证快照

- 当前 macOS 桌面已完成 1440×820 与 1024×640 两种窗口尺寸的实机截图复查，并通过原生构建；删除能力探测、旧服务禁用移除提示、REST-only 删除、租约状态页、资源约束表单、绝对资源指标和新文本口径均保留。Windows launcher 的路径和默认 inventory 初始化有单元测试覆盖；Windows `.exe` 仍需在 Windows 上运行 `desktop\build-windows-app.ps1` 产出。
- 两次人工只读 shadow 和一次 5 分钟轻量化冒烟已完成；非托管进程与失效 endpoint 均正确闭锁。
- 上述结果是 2026-07-19 的历史证据，不代表当前 GPU 可用性。详细记录见 [归档](archive/IMPLEMENTATION_EVIDENCE_2026-07-19.md)。
- 2026-08-01 在临时数据库与 fake observation 路径上新增 GPU presence 回归：连续完整观测的
  UUID 删除/新增、API snapshot、缺失后重现、重复 UUID/index 拒绝、缺失 GPU 上的活跃 lease
  保留，以及队列 eligibility 均由单元测试覆盖。该证据只证明本地同路径缺陷已修复，不证明现场
  64 GPU 事件已经完全归因。
- 2026-08-01 macOS 桌面壳完成 Apple Home 风格的原生自适应重构，并将最低版本统一为
  macOS 14（Info.plist、Swift Package 与 Mach-O `minos` 一致）。0/1/8/64 GPU 及 stale、
  error、conflict、maintenance、queued 夹具均通过离线契约校验；快照超过服务端新鲜度阈值或
  刷新失败时只显示最后可用数据，认领、移除、归还与新增等资源操作 fail closed。删除确认刷新
  复用同一单飞/generation 协调器，夹具加载会拒绝经 symlink 逃逸到项目 `state/`。
- 2026-08-02 前景内容层与侧栏改为更高不透明度的 regular-material 表面，surface、stroke、
  ambient 与 glass token 会按深浅外观切换，保留空间背景但提高文本和卡片的稳定对比；工具栏
  补齐“添加服务器”和“认领 GPU”入口，并继续受快照可信度与 endpoint 可用性约束。总览最多
  直接展示 8 项状态例外，超出部分提供有可访问名称的服务器池跳转，避免异常风暴压住服务器与
  租约信息。固定测试夹具会明确说明只读原因并停用无效刷新，不再误报为快照过期。正式 macOS
  产物已完成 ad-hoc 签名与 standalone 冷启动验证。
- 2026-08-02 视觉令牌进一步对齐 Apple Home 与 macOS 原生语义：交互改用系统 control accent，
  健康、等待和危险改用 system green/orange/red；深浅背景分别采用中性 graphite 与 fog，卡片和
  次级按钮改为 native material 叠加轻量中性色，而不是玫瑰色或蓝黑色实体面板。空间背景保留
  冷日光与暖灯光的真实色差，并降低全窗遮罩，使内容层更接近 Apple Home 的通透层级。自定义
  action button style 同步读取原生 `isEnabled`，只读夹具中的主按钮不会再以可操作的高亮色呈现。
- 原生 core build、应用打包、159 个 Python 测试、Ruff、证据门禁正负控均通过。当前机器只有
  CommandLineTools、缺少完整 Xcode/XCTest，因此 Swift XCTest、XCUI 与 VoiceOver 自动化仍是
  环境 gate；benchmark 会对此明确失败，不会把 0 测试或 skipped run 报为成功。

## 未完成 gate

1. 完整工作日 shadow 与人工 `nvidia-smi` 对照。
2. 2 小时内存 soak 与 24 小时数据库上限观察。
3. 非 loopback 部署所需的 TLS、访问控制和持久服务管理。
4. Cursor 等非 Codex 客户端的全局规则手工接入与跨客户端实机复核。
5. dstack、自动启停、抢占、交互式 Slurm、下载/同步和其他远端运行时集成。
