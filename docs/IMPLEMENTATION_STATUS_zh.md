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
- ServerPilot 只协调归属。任务由项目已有且获授权的执行路径启动和停止；任务可观测后绑定，完成或启动失败后释放 lease。
- mutation 重试复用调用方生成的 `idempotency_key`。管理动作还需要当前任务明确授权和 `approval_ref`。
- Codex MCP 从 `CODEX_THREAD_ID` 派生独立 actor 与 `codex://threads/<uuid>`。该 URI 只用于 Agent 间发现，不授予认证或调度权限；公开 GUI 不展示 Agent、owner 或协调 URI。

### 采集与 Adapter

- Collector 只执行代码封闭的只读探针。`server-script-v1` 固定调用 `serverpilot-collect --schema-version 1`，并严格校验受限 JSON。
- 数据采集间隔为持久化的 5 / 10 / 30 秒设置。数据库提交成功后才更新内存和 GUI；提交失败保留原值。
- 服务器在应有观测缺失时显示连接或采集问题，对应资源停止分配。GUI 刷新只重新读取控制面状态，不伪造一次服务器观测。
- 可选占卡 adapter 只在 endpoint 显式配置后启用。它使用固定 helper 管理自己的 worker，不监听 claim 自动启停，不抢占项目任务，也不接受任意 shell、路径或 GPU selector。
- Slurm adapter 使用封闭的 transport / inspection profile。VPN 只检测，不自动操作；取消作业需要单独授权。

### macOS App

- 仓库最外层只保留一个最新 `ServerPilot.app`。目标 App 冻结 Python 后端与迁移，不要求目标 Mac 另外安装 Python 或 `uv`。
- App 固定为浅色、不透明界面，一级页面只有`服务器 / 使用情况 / 设置`。
- 服务器页使用 Beszel 式紧凑表格。表头为`服务器 / 项目与当前任务 / GPU 配置 / 空闲 / GPU / 显存 / CPU / 内存`；数字使用中性色，压力颜色只用于状态点与进度柱。
- 所有表头可排序。只有当前排序列显示单个方向箭头；搜索、筛选、表头和服务器行可键盘到达。
- 详情页显示 SSH、当前资源、GPU 型号与状态、项目 / 当前任务，以及 1h / 6h / 24h 历史。多 GPU 序列使用稳定的不同颜色；采样间断不补线。
- 宽窗口使用右侧 inspector，窄窗口进入完整详情并保留可见的`返回资源列表`。
- 使用情况页只展示项目、当前任务与 GPU；不提供 Agent、聊天、消息或 Codex 链接。
- 设置页只展示本机服务地址、数据采集间隔和版本。

## 已完成验证

当前候选已经通过独立 Strict Review，结论为 `ACCEPT`。直接证据如下：

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `334 passed` |
| Ruff | 通过 |
| Swift desktop/core 全量类型检查 | 通过 |
| Alembic | 单头 `20260812_0018` |
| macOS App 构建 | `desktop/build-macos-app.sh` 通过 |
| standalone 验证 | `desktop/verify-macos-app.sh` 通过 |
| 文本与补丁完整性 | `git diff --check` 通过 |
| App 落盘 | 根目录唯一 `ServerPilot.app` |

GUI 验收覆盖 1024、1280 和 1440 宽度，以及服务器表格、排序、搜索、键盘焦点、详情返回、使用情况、设置、错误、空状态和多 GPU 历史图。证据位于：

- `build/plan-closeout/native-ui-acceptance-final3/manifest.json`
- `build/plan-closeout/evidence/`
- `docs/teamwork/cases/c-34f04361fc1a544e483dfee0bc8eb4343cd5b920bc1ffaa43cbb2b7ff2436e88/reviews/a48055759c63b956adfcd3f7502726a0dc1f79d540ef67d5b505c447f5de8d30.md`

这些证据证明当前本机构建和夹具路径符合验收合同，不代表任意时刻的现场 GPU 容量。

## 尚未完成的验证

当前机器只有 CommandLineTools，没有完整 Xcode / XCTest。因此以下项目没有宣称通过：

1. 8 项 XCUITest 的真实执行。
2. VoiceOver 自动化。
3. 高对比度和 Reduce Motion 的行为级验证。
4. error ScrollView、详情与确认框的完整 XCUITest 证据。

这些是明确的环境补验，不应写成已有结果。提供完整 Xcode 后可直接补跑，不改变当前资源合同。

以下运行环境能力也仍需单独授权和现场证据：

- A800 占卡 helper 的实际显存、利用率、停止响应与主任务无干扰对照。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 非 Codex 客户端和外部调度器的现场联调。
