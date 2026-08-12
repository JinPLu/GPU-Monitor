# ServerPilot 当前实现与验证状态

更新时间：2026-08-13（Asia/Shanghai）

本文只记录当前事实、直接证据和仍未验证的边界。历史过程见 `docs/archive/`。

## 当前四项功能

1. **信息采集**：固定只读探针采集服务器 CPU、内存、GPU、进程和历史趋势；APP 刷新只读取这份状态。
2. **人类监控与纠错**：APP 展示服务器、任务与 GPU。任务详情允许人保持卡数不变，直接选择新的 GPU；目标 GPU 正在占卡时先按卡停止并刷新确认，再更新分配，随后提示对应 Agent 按返回的 `CUDA_VISIBLE_DEVICES` 重启任务。
3. **Agent 操作**：默认 MCP 只有 `gpu_status`、`gpu_apply`、`gpu_release`。申请返回 `lease_id` 与选中的服务器/GPU/`CUDA_VISIBLE_DEVICES`；租约持续到显式释放或 App 人工处理，容量不足直接返回 `no_capacity`，不排队。
4. **空闲 GPU 占卡**：持久化的是 endpoint 的“空闲自动占卡策略”，不是一次启动动作。每轮既有采集之后逐卡协调：空闲且 helper 不在就启动，空闲且 helper 已在就保持，任务使用中不占卡，任务释放后在下一轮采集恢复。占卡只有 `OFF / ACTIVE / ERROR` 三种对外结果，不写 `STARTING` 或 keepalive `HELD`。启动和采集确认成功后才创建 `ACTIVE` 占卡记录；失败不留下半成品租约，并由既有 policy、GPU 状态和 helper 观测直接显示具体中文 `ERROR`，下一采集周期照常重试。

每个 endpoint 现在只有一个 canonical `workspace_path` 字段。新增服务器时 REST、MCP advanced 管理工具、Web 和原生 APP 都要求填写绝对远端路径；endpoint 快照、Agent 状态和 GPU 申请结果沿既有投影返回同一字段。用户已确认当前共用目录为 `/media/datasets/OminiEWM_Data/tmp/ljp`。历史记录迁移保留且未知路径保持空值，不猜测项目子目录。该字段只是元数据和操作指引，不创建/删除远端目录、不授权启动 workload；密封占卡 helper 固定布局为 `${workspace_path}/serverpilot-keepalive`，adapter 直接执行 `./serverpilot-keepalive --schema-version 2`，不依赖远端 `PATH`。

占卡 GPU 对 APP、REST 和 MCP 仍计为可用；helper 意外退出但新采集确认 GPU 空闲时也计为可用并显示具体中文异常。真正分配前，Agent 申请、浏览器快速申请、预设申请和 APP 人工改派都复用同一个“选中 GPU → 逐卡停止 helper → 定向采集 → 结束占卡记录 → 普通申请或改派”实现。

loopback 控制面不使用登录 token：没有 token model、登录页面、签发接口或撤销接口。服务器永久删除也不在 REST、Web、MCP 或 APP 公共面中；暂停和恢复是非破坏操作。升级迁移只移除旧摘要字段，不删除已有 token 表、退役服务器、占卡请求、占卡租约或 lease resource。

占卡链路没有校验摘要、attestation、自动重试、退避器、第二套定时器、自动抢占或整机占卡状态机。

项目明确要求的资源正确性边界仍保留：过期采集不能被当成可用 GPU，Agent 只能使用实际返回的 lease 资源。这两项来自当前项目合同，不新增状态机。

## 已完成验证

以下自动化结果来自当前工作树；测试使用临时数据库和 fake provider。

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `355` 项 collected，`PYTHONPATH=src uv run pytest -q` 通过；覆盖 endpoint workspace 投影、迁移保留旧记录、workspace 内固定 helper 入口、三工具 MCP、Codex task URI、首次申请和改派后的真实进程自动归属，以及既有占卡/让位路径 |
| Ruff | `.venv/bin/ruff check src tests` 通过 |
| 数据迁移 | 当前源码迁移头 `20260813_0022`；新增 nullable endpoint 元数据列以保留历史记录，新增入口要求显式路径 |
| MCP 上下文 | 默认发现结果严格为 3 个工具；instructions 为 `306` 字，每个工具一句说明；schema 和返回投影都有字段白名单测试 |
| Codex task URL | 当前 Codex 配置声明转发既有 `CODEX_THREAD_ID`；client/header 与缺失变量停止申请/释放均有测试；新建子任务实际发现三个工具，但宿主没有向子任务 MCP 进程注入该变量，因此申请按合同在 daemon 前停止。顶层验收使用当前顶层 task 的既有 UUID 启动全新 stdio 进程完成实机申请与释放，没有生成随机身份 |
| macOS App 构建 | `zsh desktop/build-macos-app.sh` 通过，包含 Swift 桌面端编译 |
| standalone 验证 | `zsh desktop/verify-macos-app.sh` 通过 |
| 冗余机制扫描 | 运行源码和桌面端没有摘要计算、登录 token、永久删除入口、占卡 `STARTING/HELD`、额外定时器或自动抢占 |
| 文本与补丁完整性 | `git diff --check` 通过 |
| App 落盘 | 根目录唯一 `ServerPilot.app` |

四项核心功能的收敛决定记录在 `docs/teamwork/cases/c-f379fac55e2c1c893405737d74f7bdc3c2f3615e8a9fbb15e1aeff3b9c389dca/decision.md`。

service 快照直接提供统一的 `publicly_available` 和简短中文 `public_status`；routine MCP 与 Web 只投影这份结果，不再各自判断占卡容量。API 与 Swift 模型遇到未知占卡 policy/state 会明确拒绝，不会伪装成 `disabled`、`OFF` 或 `ERROR`。

## 已完成现场验收

- 已备份实时数据库到 `serverpilot-before-0022-workspace-20260813.sqlite3`，随后把规范 state 从 `20260812_0021` 迁移到 `20260813_0022`。`PRAGMA integrity_check` 为 `ok`；迁移前已有的两条 `workload_bindings` 外键异常没有增加；endpoint、GPU、request、lease、lease resource 与 workload binding 的既有记录均保留。
- 已安装当前本地 MCP/daemon 并重启。daemon 为 `live=true`、`ready=true`；全新 stdio 实际发现严格只有 `gpu_status`、`gpu_apply`、`gpu_release`。
- 四个 endpoint 的 `workspace_path` 均为 `/media/datasets/OminiEWM_Data/tmp/ljp`。181 和 203 的 helper 固定入口及模块位于该工作区；实际 worker 的 cwd 与 Python 命令也位于该工作区。调试阶段的 `/root/.local/...` 与 `/usr/local/bin/serverpilot-keepalive` 副本已经移除。
- 181 的两张 GPU 与 203 的四张 GPU 均完成真实逐卡占卡。状态为 `ACTIVE`、公开可用，中文显示“可用 · 空闲占卡”。
- 181 实际申请只停止选中 GPU 的 helper，另一张继续占卡；释放后同一 GPU 在下一次成功采集中恢复，终态 2/2 `ACTIVE`。
- 203 实际申请只停止选中 GPU 的 helper，另外三张继续占卡；释放后先准确显示“可用 · 占卡异常：未检测到占卡程序”，下一次成功采集恢复，终态 4/4 `ACTIVE`。
- 全新 Codex task 的宿主实际工具清单严格只有 `gpu_status`、`gpu_apply`、`gpu_release`；`gpu_status(include_busy=false)` 实际返回 14 张 GPU、统一 `workspace_path` 和简短中文状态，没有 bind、renew 或 coordination 工具。
- 通过根目录原生 APP 实际点击完成了申请表填写、选择 203、申请成功、查看任务、释放确认和释放。申请结果返回 `/media/datasets/OminiEWM_Data/tmp/ljp`、精确 GPU UUID 与 `cuda_visible_devices`；选中卡 helper 退出，另外三卡保持占卡。
- 在申请到的 GPU 上从 Storyboard 正式入口执行了 Bernini R-1 真实项目：真实 checkpoint、300×3584 external feature 和 renderer 均完成，输出为 H.264 832×480、81 帧、16fps、5.0625 秒；运行现场达到 45,226 MiB、100% GPU 利用率和 334W。
- 首次真实运行发现普通 lease 的进程没有自动归属；修复后，同一正式项目无需额外 bind 工具即可由既有采集直接变为 `ACTIVE / RUNNING_MANAGED / workloads 非空`。只有现有普通 lease 的每张目标 GPU 都观测到新鲜进程时才建立既有 binding；无 lease 的进程仍是未托管。
- 原生 APP 的实际可见“使用情况”页已接通已有改派能力。现场把同一 lease 从 203 GPU 0 改派到正在占卡的 GPU 1：目标 helper 逐卡退出，旧 GPU 0 下一采集周期恢复占卡；随后按新 `cuda_visible_devices` 重启同一 Bernini 正式项目并完成。
- 改派后的真实运行又发现 `ACTIVE` 且 binding 已清空的 lease 不会重新归属；最小修复后，同一真实 PID 在下一采集周期变为 `RUNNING_MANAGED`，未新增 MCP 工具、状态机、重试或摘要。
- 使用本机浏览器实际点击 Web“申请 GPU”，填写项目、任务和 203 服务器并提交，随后在 Web 页面勾选确认并点击“归还资源”。真实 lease 创建、释放和下一采集周期恢复占卡均完成，不再以 REST 测试代替按钮验收。
- 两段真实项目临时 request、receipt 和视频在核验后已精确清理；四个 launcher/workload PID 均不存在。最终为 14 available、0 busy、0 workload、6 verified keepalive、0 attention，203 为 4/4 `ACTIVE`。
- 根目录 `ServerPilot.app` 已重新构建、standalone 验证通过并实际操作；仓库只有这一份 `ServerPilot*.app`。

## 尚未完成的验证

当前机器没有下载 XCTest，因此没有运行 Swift 单元测试或 XCUITest；这不再阻塞核心功能验收。原生 APP 已通过实际构建、standalone 校验和真实按钮交互。尚未完成的桌面辅助体验检查只剩 VoiceOver、键盘完整焦点顺序、缩放重排、色彩对比度测量与 Reduce Motion；不能仅凭截图宣称这些辅助功能完全合规。

以下运行环境能力仍未验证：

- 222 保留禁用占卡策略；验收期间曾出现一次 SSH 采集超时，随后恢复在线。215 没有 GPU 且观测不完整。本轮没有为它们伪造成功状态。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 非 Codex 客户端和外部调度器的现场联调。
