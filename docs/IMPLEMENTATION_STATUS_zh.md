# ServerPilot 当前实现与验证状态

更新时间：2026-08-12（Asia/Shanghai）

本文只记录当前事实、直接证据和仍未验证的边界。历史过程见 `docs/archive/`。

## 当前事实

### 控制面

- ServerPilot 是单用户、本机运行的资源协调器。用户级 LaunchAgent 长期持有状态；GUI、CLI 和 MCP 通过同一 loopback REST/domain 合同读取与写入。
- 唯一数据目录是 `~/Library/Application Support/ServerPilot/`。MCP 不直连 SQLite 或 SSH，也不会创建备用数据库。
- GPU、主机 CPU / 内存和外部 SchedulerTarget 使用同一资源合同与审计链。SchedulerTarget 不会伪装成普通服务器；`PENDING` 作业不表示已获得裸机 GPU。
- 服务端 `available` 投影是准入真相。stale、unknown、unmanaged、conflict 和 maintenance 资源均不可分配；客户端不得自行用容量减用量重算。

### 申请与任务边界

- 裸机申请只会立即返回 `HELD` / `ACTIVE` lease，或返回 `no_capacity`。`no_capacity` 不创建等待队列，也不授权执行。
- 成功 lease 返回 `resources[]`、endpoint、GPU 和 `cuda_visible_devices`。Agent 只能使用这些落点。
- 日常 GPU Agent 直接调用 `gpu_apply`；只有需要查看可用性或诊断 placement 时才调用 `gpu_status`，旧的 `gpu_list` 只在 advanced profile 提供。`server_id` 可选，默认一张 GPU；ServerPilot 自动记录例行申请的归属并选择实际可分配的 GPU，Agent 只使用返回的资源并在完成后释放。
- MCP 的默认状态读面只返回摘要、服务器容量和紧凑 GPU 列表；`gpu_list` 直接使用窄 REST 投影，不把 scheduler、通用资源和历史集合带回 Agent 上下文。紧凑状态会区分可见 workload lease 与内部 keepalive 归属：`available` 始终排除所有占用，`verified_keepalive` 仅表示可由 `gpu_apply` 在逐卡停止并取得新鲜空观测后尝试回收，`HELD` / `CONFLICT` 仍不可分配。完整控制面仍由 REST `control_plane_state` 保留给显式诊断。
- 默认 stdio MCP 只暴露日常 GPU 工具；scheduler、通用资源、端点管理和低层 lease 兼容函数仍可通过 `SERVERPILOT_MCP_PROFILE=advanced` 显式启用。
- ServerPilot 只协调归属。任务由项目已有且获授权的执行路径启动和停止；启动后 Agent 用该 lease 的 `gpu_bind_observed_workload` 确认可观测进程归属，完成或启动失败后释放 lease。
- mutation 重试复用调用方生成的 `idempotency_key`。管理动作还需要当前任务明确授权和 `approval_ref`。
- Codex MCP 从 `CODEX_THREAD_ID` 派生独立 actor 与 `codex://threads/<uuid>`。该 URI 只用于 Agent 间发现，不授予认证或调度权限，也不是 ServerPilot 会打开或解析的 URL；公开 GUI 不展示 Agent、owner 或协调 URI。客户端若维护 MCP allowlist，升级后必须同步新 routine 工具。

### 采集与 Adapter

- Collector 只执行代码封闭的只读探针。`server-script-v1` 固定调用 `serverpilot-collect --schema-version 1`，并严格校验受限 JSON。
- 数据采集间隔为持久化的 5 / 10 / 30 秒设置。数据库提交成功后才更新内存和 GUI；提交失败保留原值。
- 服务器在应有观测缺失时显示连接或采集问题，对应资源停止分配。GUI 刷新只重新读取控制面状态，不伪造一次服务器观测。
- 可选空闲占卡 adapter 在用户明确点击“开始占卡”时由 ServerPilot 自动挂载。服务器只有一个期望策略开关，但它为每张合格的空闲 GPU 管理独立 worker / lease / 健康状态；忙碌、未托管、冲突和 stale GPU 不会被占用。helper 只接受 ServerPilot 已知的物理 GPU UUID，不接受任意 shell、路径、PID、环境或调用方 selector。
- 逐卡隔离已覆盖工作负载冲突：一张 GPU 的遗留归属不会阻断同一服务器其它空闲 GPU 的占卡启动；详情页提供“清理遗留归属”，先做新鲜采集并确认没有进程后才释放。
- 受管即时 claim 先按原有分配；仅在无容量且服务能给出完整精确的逐卡 keepalive 回收计划时，才停止目标 GPU、做新鲜空观测并在同一 endpoint 锁内重试普通 claim。它不抢占直接 SSH 或未托管任务；后者必须先由管理员关闭该 endpoint 策略。旧整机 keepalive lease 被迁移标记为 legacy，保持 fail closed，不能猜测转换为新逐卡 lease。
- helper 固定约 31% 显存、30% GPU duty cycle、单 PyTorch CPU 线程和 100ms 节流；稳态没有磁盘或网络轮询。五分钟宽限期后，读模型要求至少 30% 显存和 30% 滚动 GPU 利用率，否则标记为 `DEGRADED`；实际主机 CPU / RSS 与 GPU 效果仍需现场对照。
- Slurm adapter 使用封闭的 transport / inspection profile。VPN 只检测，不自动操作；取消作业需要单独授权。

### macOS App

- 仓库最外层只保留一个最新 `ServerPilot.app`。目标 App 冻结 Python 后端与迁移，不要求目标 Mac 另外安装 Python 或 `uv`。
- App 固定为浅色、不透明界面，一级页面只有`服务器 / 使用情况 / 设置`。
- 服务器页使用 Beszel 式紧凑表格。表头为`服务器 / 项目与当前任务 / GPU 配置 / 空闲 / GPU / 显存 / CPU / 内存`；数字使用中性色，压力颜色只用于状态点与进度柱。
- 所有表头可排序。只有当前排序列显示单个方向箭头；搜索、筛选、表头和服务器行可键盘到达。
- 详情页显示 SSH、当前资源、GPU 型号与状态、项目 / 当前任务，以及 1h / 6h / 24h 历史。多 GPU 序列使用稳定的不同颜色；采样间断不补线。
- 服务器行统一打开独立详情弹窗，列表不会因展开详情而压缩；详情页保留 GPU 状态、归属说明与 1h / 6h / 24h 历史。
- 使用情况页只展示项目、当前任务与 GPU；不提供 Agent、聊天、消息或 Codex 链接。
- 设置页只展示本机服务地址、数据采集间隔和版本。

## 已完成验证

逐 GPU 空闲占卡候选已经通过独立 Strict Review。当前工作将日常 Agent GPU 申请收敛为服务端自动归属的 `gpu_apply`，并修正服务器详情与归属状态的呈现；在完成复核前，不声明新的发布结论。下表只记录已完成的直接验证。

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `344` 项 collected，`PYTHONPATH=src uv run pytest -q` 通过 |
| Ruff | 通过 |
| Swift desktop/core 全量类型检查 | 通过 |
| Alembic | 单头 `20260812_0019` |
| macOS App 构建 | `desktop/build-macos-app.sh` 通过 |
| standalone 验证 | `desktop/verify-macos-app.sh` 通过 |
| 文本与补丁完整性 | `git diff --check` 通过 |
| App 落盘 | 根目录唯一 `ServerPilot.app` |

既有 GUI 验收覆盖 1024、1280 和 1440 宽度，以及服务器表格、排序、搜索、键盘焦点、详情返回、使用情况、设置、错误、空状态和多 GPU 历史图。本次逐 GPU 空闲占卡还以 `keepalive` 假夹具实际生成了服务器列表的三个固定尺寸截图；显示为单一 endpoint 开关、逐卡占卡覆盖和普通运行任务并存。证据位于：

- `build/plan-closeout/native-ui-acceptance-final3/manifest.json`
- `build/plan-closeout/evidence/`
- `build/per-gpu-keepalive-final-ui-20260812/`
- `docs/teamwork/cases/c-34f04361fc1a544e483dfee0bc8eb4343cd5b920bc1ffaa43cbb2b7ff2436e88/reviews/a48055759c63b956adfcd3f7502726a0dc1f79d540ef67d5b505c447f5de8d30.md`

这些证据证明当前本机构建和夹具路径符合验收合同，不代表任意时刻的现场 GPU 容量。

## 尚未完成的验证

当前机器只有 CommandLineTools，没有完整 Xcode / XCTest。因此以下项目没有宣称通过：

1. 8 项 XCUITest 的真实执行。
2. VoiceOver 自动化。
3. 高对比度和 Reduce Motion 的行为级验证。
4. error ScrollView、详情与确认框的完整 XCUITest 证据。
5. 本次逐 GPU 占卡详情中的健康信息与启动中卡不计作任务的 XCUITest / 详情截图。

这些是明确的环境补验，不应写成已有结果。提供完整 Xcode 后可直接补跑，不改变当前资源合同。

以下运行环境能力也仍需单独授权和现场证据：

- A800 占卡 helper 的实际显存、利用率、停止响应与主任务无干扰对照。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 非 Codex 客户端和外部调度器的现场联调。
