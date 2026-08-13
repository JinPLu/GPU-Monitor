# ServerPilot 当前实现与验证状态

更新时间：2026-08-13（Asia/Shanghai）

本文只记录当前事实、直接证据和仍未验证的边界。历史过程见 `docs/archive/`。

## 当前四项功能

1. **信息采集**：固定只读探针采集服务器 CPU、内存、GPU、进程和历史趋势；APP 刷新只读取这份状态。
2. **人类监控与纠错**：APP 展示服务器、任务与 GPU。任务详情允许人保持卡数不变，直接选择新的 GPU；目标 GPU 正在占卡时先按卡停止并刷新确认，再更新分配，随后提示对应 Agent 按返回的 `CUDA_VISIBLE_DEVICES` 重启任务。
3. **Agent 操作**：默认 MCP 只有 `gpu_status`、`gpu_apply`、`gpu_release`。单 endpoint 申请的顶层与逐行兼容字段 `cuda_visible_devices` 是完整资源集合；`gpus[]` 中每项新增 `gpu_cuda_visible_devices`，只含该卡 UUID，分别支持一个多卡进程与每卡一个进程。`workspace_path` 是远端路径，须经当前授权 endpoint 进入；启动前做最小 CUDA gate，失败立即释放并在当前任务内避开同一环境。租约持续到显式释放或 App 人工处理，容量不足直接返回 `no_capacity`，不排队且同一 turn 不反复轮询；多个 lease 由申请者逐个确认释放。
4. **空闲 GPU 占卡**：明确分开持久意图与当前进程状态。endpoint 的 `desired` 只有 `ON / OFF`，只随用户开关改变；逐卡 `actual` 只有 `ON / OFF / ERROR`，由 helper 操作与新鲜采集更新。内部逐卡归属不再使用 TTL，并持久保存唯一的 PID、boot ID 和进程启动时间；只有新鲜采集与该身份完全匹配才是 `actual=ON`。无进程为 `OFF`，额外或替代业务进程为 `ERROR/CONFLICT` 并 fail closed。正常启动直接读取持久 ownership，采集后原地更新 `actual`，不会为了重建归属而停止或重启远端 helper。

每个 endpoint 现在只有一个 canonical `workspace_path` 字段。新增服务器时 REST、MCP advanced 管理工具、Web 和原生 APP 都要求填写绝对远端路径；endpoint 快照、Agent 状态和 GPU 申请结果沿既有投影返回同一字段。用户已确认当前共用目录为 `/media/datasets/OminiEWM_Data/tmp/ljp`。历史记录迁移保留且未知路径保持空值，不猜测项目子目录。该字段只是元数据和操作指引，不创建/删除远端目录、不授权启动 workload；密封占卡 helper 固定布局为 `${workspace_path}/serverpilot-keepalive`，adapter 直接执行 `./serverpilot-keepalive --schema-version 2`，不依赖远端 `PATH`。

占卡 GPU 对 APP、REST 和 MCP 仍计为可用；`desired=ON, actual=OFF` 时 GPU 空闲则仍可申请，同时下一轮按策略重新启动 helper。真正分配前，Agent 申请、浏览器快速申请、预设申请和 APP 人工改派都复用同一个“选中 GPU → 逐卡停止 helper → 定向采集 → 结束占卡记录 → 普通申请或改派”实现。

loopback 控制面不使用登录 token：没有 token model、登录页面、签发接口或撤销接口。服务器永久删除也不在 REST、Web、MCP 或 APP 公共面中；暂停和恢复是非破坏操作。升级迁移只移除旧摘要字段，不删除已有 token 表、退役服务器、占卡请求、占卡租约或 lease resource。

占卡链路没有校验摘要、attestation、自动重试、退避器、第二套定时器、自动抢占或整机占卡状态机。

项目明确要求的资源正确性边界仍保留：过期采集不能被当成可用 GPU，Agent 只能使用实际返回的 lease 资源。这两项来自当前项目合同，不新增状态机。

Agent 合同现已明确限定作用域：ServerPilot 只协调 GPU，禁止绕过的对象是 GPU 发现、选卡、申请和释放；已获得当前授权端点的 Git 同步、文件维护与只读环境检查不需要 GPU lease。`workspace_path` 仍只是元数据，不提供远端 shell 或额外授权。`Transport closed` 与 `no_capacity` 分开处理，前者最多重试一次；同一任务内的 CUDA 初始化失败不会立即重试同一 server。

## 已完成验证

以下自动化结果来自当前工作树；测试使用临时数据库和 fake provider。

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `382` 项 collected，`PYTHONPATH=src uv run pytest -q` 通过；覆盖 endpoint workspace 投影、迁移保留旧记录、workspace 内固定 helper 入口、三工具 MCP、占卡 ON/OFF 下的 routine 工具调用、daemon 重启不扰动 helper、业务进程替代 keeper 时 fail closed、keeper 启动与 Agent 申请串行，以及无容量重试、同机双卡、顶层/逐行兼容集合投影、逐卡单 UUID 新字段、跨 endpoint 去歧义、申请、释放和让位路径 |
| Ruff | `.venv/bin/ruff check src tests` 通过 |
| 数据迁移 | 当前源码迁移头 `20260813_0024`；`keepalive_current` 保存 `actual/error_reason` 与逐卡唯一进程身份，只把仍有 active resource 的活动 keepalive lease 转为无 TTL，保留 terminal keeper 与 workload 历史 expiry |
| MCP 上下文 | 默认发现结果严格为 3 个工具；instructions 直接说明远端 workspace、顶层完整 selector、逐卡 selector、CUDA gate、失败释放和有限重试；schema 和返回投影都有字段白名单测试 |
| Agent 任务说明 | 默认 MCP 不依赖客户端身份、UI 标题或专用环境变量。`gpu_apply(task?)` 接收用户任务名或当前目标的简短人类可读概括；未提供时使用“未命名任务”。`gpu_status(include_busy=true)` 为忙卡返回人类可读 `task` |
| macOS App 构建 | `zsh desktop/build-macos-app.sh` 通过，包含 Swift 桌面端编译 |
| standalone 验证 | `zsh desktop/verify-macos-app.sh` 通过 |
| 冗余机制扫描 | 运行源码和桌面端没有摘要计算、登录 token、永久删除入口、占卡 `STARTING/HELD`、额外定时器或自动抢占 |
| 文本与补丁完整性 | `git diff --check` 通过 |
| App 落盘 | 根目录唯一 `ServerPilot.app` |

四项核心功能的收敛决定记录在 `docs/teamwork/cases/c-f379fac55e2c1c893405737d74f7bdc3c2f3615e8a9fbb15e1aeff3b9c389dca/decision.md`。

service 快照直接提供统一的 `publicly_available` 和简短中文 `public_status`；routine MCP 与 Web 只投影这份结果，不再各自判断占卡容量。API 与 Swift 模型分别校验 `desired=ON/OFF` 与 `actual=ON/OFF/ERROR`，遇到未知值会明确拒绝。

## 已完成现场验收

- 本轮升级前已把实时数据库备份到 `~/Library/Application Support/ServerPilot/state/backups/pre-0023-20260813.sqlite3`，并在最终迁移前另存 `.artifacts/live-backups/pre-0024-20260813.sqlite3`；最终源码迁移头为 `20260813_0024`。
- 已安装当前本地 MCP/daemon 并重启。daemon 为 `live=true`、`ready=true`；全新 stdio 实际发现严格只有 `gpu_status`、`gpu_apply`、`gpu_release`。
- 本轮直接通过 Agent routine MCP 在 181 完成两组现场验收。占卡开启时，`gpu_status(include_busy=false)` 返回两张“可用 · 空闲占卡”，`keepalive={desired: ON, actual: ON}`；`gpu_apply` 自动停止所选 GPU 的 helper、返回真实 `workspace_path` 与 `cuda_visible_devices`，`gpu_status(include_busy=true)` 显示验收任务，随后 `gpu_release` 成功并恢复占卡。占卡关闭后，状态变为“可用 · 未开启占卡”，同一套 `gpu_apply → gpu_status → gpu_release` 仍成功且不启动 helper。验收后 181 已恢复 `desired=ON / actual=ON`，两张卡均为“可用 · 空闲占卡”。
- daemon 重启自动化验收会模拟远端 helper 仍在运行：重启后的对账不调用 adapter、不改变远端 PID、不更换逐卡 lease ID，继续沿用 `expires_at=NULL` 的持久 ownership，并由采集保持 `desired=ON / actual=ON`。
- 最终安装版在 222 实际重启 daemon 前后比较 7 个当时空闲 keeper：7 个 PID 逐一不变，没有为“重建 ownership”停止或重启远端 helper。
- 最终安装的 `serverpilot-mcp` stdio 子进程发现严格只有三个日常工具。在 222 上实际完成：9 卡超容量申请返回 `no_capacity` 且不排队；占卡开启时双卡 `gpu_apply → gpu_status(include_busy=true) → gpu_release`，释放后两张卡恢复 keeper；占卡关闭时 `gpu_status(false)` 返回 7 张 `desired=OFF / actual=OFF` 的可用卡，单卡申请、任务可见和释放成功，随后已恢复 endpoint 策略为 ON。
- 上述实际 Agent 申请返回顶层 `server_id`、`workspace_path=/media/datasets/OminiEWM_Data/tmp/ljp` 和精确 `cuda_visible_devices`，同时保留 `gpus[]` 中的逐卡字段。
- 在仅配置 `SERVERPILOT_URL`、没有任何客户端专用环境变量的全新 stdio 进程中，三个工具 schema 保持不变，`gpu_status` 成功；随后在 181 实际申请 1 张 GPU，任务记录为“ServerPilot 通用 MCP 申请释放验收”，再用返回的 `lease_id` 成功释放。请求终态为 `RELEASED`，181 两张卡随后均恢复“可用 · 空闲占卡”。
- 四个 endpoint 的 `workspace_path` 均为 `/media/datasets/OminiEWM_Data/tmp/ljp`。181 和 203 的 helper 固定入口及模块位于该工作区；实际 worker 的 cwd 与 Python 命令也位于该工作区。调试阶段的 `/root/.local/...` 与 `/usr/local/bin/serverpilot-keepalive` 副本已经移除。
- 181 的两张 GPU 与 203 的四张 GPU 都完成过真实逐卡占卡验收；空闲验收时分别达到 2/2 和 4/4 `actual=ON`、公开可用，中文显示“可用 · 空闲占卡”。历史上 203 曾因占卡归属丢失被误判为 `BUSY_UNMANAGED`；本轮升级后的同一时点快照已恢复 4/4 `desired=ON / actual=ON`。
- 222 的原始失败不是采集延迟：固定 helper 能执行，但原 Python 的 PyTorch 2.5.1+cu124 不支持该机 `sm_120`，且 CUDA UUID selector 在该机不能初始化。helper 改为复用该工作区已有、实际通过 `sm_120` kernel 的 PyTorch 2.7.1+cu128，8 个 worker 并发启动；真实策略 `OFF → ON` 已完成，启动阶段用时 8.74 秒并达到 8/8 `ACTIVE`、8/8 公开可用。UUID 到 CUDA ordinal 的实现按 `PCI_BUS_ID` 排序映射，避免把 `nvidia-smi` index 直接当作默认 CUDA ordinal；222 的单卡密封启动实测只在目标 `GPU-1d99ddc9-b0dd-59a1-9239-0eb798f5a45f` 观测到一个进程，其余七卡为零，精确停止后八卡均为零，再恢复策略后八卡均各有一个进程并回到 8/8 `ACTIVE`。
- 181 实际申请只停止选中 GPU 的 helper，另一张继续占卡；释放后同一 GPU 恢复。根目录 APP 又实际完成 181 的“申请 → 改派 → 释放”，释放后约 12 秒恢复 2/2 `ACTIVE`；同一 APP 将占卡策略关闭约 5 秒后达到 0/2，再开启约 7 秒后恢复 2/2。
- 203 空闲时的实际申请只停止选中 GPU 的 helper，另外三张继续占卡；释放后下一次成功采集恢复到 4/4 `actual=ON`。历史现场的四张 `BUSY_UNMANAGED` 已确认是占卡归属丢失造成的误判；当前持久归属修复后四张均重新作为可让位的空闲占卡公开给 Agent。
- 全新 MCP stdio 进程的实际工具清单严格只有 `gpu_status`、`gpu_apply`、`gpu_release`；本轮验收开始时已有的 Storyboard workload 依旧正常显示并完全避开，没有为验收中断、释放或重分配这些租约。
- 根目录 APP 的一次手动“更新”实测用时 656 ms；同一轮中 181、203、222 三个 GPU endpoint 的采集完成时间相差 448 ms。随后多轮自动刷新均继续更新观测，没有把三个 endpoint 串行累加成约 8 秒一次的等待。
- 通过根目录原生 APP 实际点击完成了申请表填写、选择 203、申请成功、查看任务、释放确认和释放。申请结果返回 `/media/datasets/OminiEWM_Data/tmp/ljp`、精确 GPU UUID 与 `cuda_visible_devices`；选中卡 helper 退出，另外三卡保持占卡。
- 在申请到的 GPU 上从 Storyboard 正式入口执行了 Bernini R-1 真实项目：真实 checkpoint、300×3584 external feature 和 renderer 均完成，输出为 H.264 832×480、81 帧、16fps、5.0625 秒；运行现场达到 45,226 MiB、100% GPU 利用率和 334W。
- 首次真实运行发现普通 lease 的进程没有自动归属；修复后，同一正式项目无需额外 bind 工具即可由既有采集直接变为 `ACTIVE / RUNNING_MANAGED / workloads 非空`。只有现有普通 lease 的每张目标 GPU 都观测到新鲜进程时才建立既有 binding；无 lease 的进程仍是未托管。
- 原生 APP 的实际可见“使用情况”页已接通已有改派能力。现场把同一 lease 从 203 GPU 0 改派到正在占卡的 GPU 1：目标 helper 逐卡退出，旧 GPU 0 下一采集周期恢复占卡；随后按新 `cuda_visible_devices` 重启同一 Bernini 正式项目并完成。
- 改派后的真实运行又发现 `ACTIVE` 且 binding 已清空的 lease 不会重新归属；最小修复后，同一真实 PID 在下一采集周期变为 `RUNNING_MANAGED`，未新增 MCP 工具、状态机、重试或摘要。
- 使用本机浏览器实际点击 Web“申请 GPU”，填写项目、任务和 203 服务器并提交，随后在 Web 页面勾选确认并点击“归还资源”。真实 lease 创建、释放和下一采集周期恢复占卡均完成，不再以 REST 测试代替按钮验收。
- 两段真实项目的临时 request、receipt 和视频在核验后已精确清理，对应四个 launcher/workload PID 均不存在。占卡进程现在由持久的逐卡进程身份识别；不匹配或额外进程会进入 `ERROR/CONFLICT` 而不会被伪装成可用 keeper。
- 根目录 `ServerPilot.app` 已重新构建、standalone 验证通过并实际操作；仓库只有这一份 `ServerPilot*.app`。

## 尚未完成的验证

当前机器没有下载 XCTest，因此没有运行 Swift 单元测试或 XCUITest；这不再阻塞核心功能验收。原生 APP 已通过实际构建、standalone 校验和真实按钮交互。尚未完成的桌面辅助体验检查只剩 VoiceOver、键盘完整焦点顺序、缩放重排、色彩对比度测量与 Reduce Motion；不能仅凭截图宣称这些辅助功能完全合规。

以下运行环境能力仍未验证：

- 215 没有 GPU 且观测不完整；本轮没有为它伪造成功状态。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 其他 MCP 客户端和外部调度器的现场联调。
