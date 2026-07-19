# gpu-broker 实施状态

更新时间：2026-07-20（Asia/Shanghai）

## 当前状态

- 本机功能版已交付：GUI、CLI 和 MCP 共用 REST 领域逻辑，可查看、认领、排队、预约和释放 GPU。
- 默认无登录且仅监听 loopback；Collector 固定只读，异常、失效 telemetry 和非托管进程均 fail closed。
- 当前开放的是协作租约；租约本身不授权远端执行，也不启动、停止或抢占工作负载。自动 allocator 与远端生命周期管理未开放。
- GUI 支持首次 SSH 接入预览；macOS App 可直接使用 `⌘V` 或系统剪贴板按钮，一次粘贴多行 SSH 地址时逐行预览并只登记有效行。粘贴内容只解析、不执行，并拒绝远端命令、管道、跳板和密钥参数。服务器可从 Dashboard、REST、CLI 或 MCP 删除；删除只移除本机监控登记和当前观测记录，不会停止远端任务，并会拒绝仍被活跃/历史租约、未来预约、排队请求或启用预设任务引用的服务器。
- Windows 桌面源码构建入口已加入：PowerShell + PyInstaller 生成 `dist/windows/GPU Broker/GPU Broker.exe`，运行时写入 `%LOCALAPPDATA%\GPU Broker`，启动同一 loopback REST/Web UI，不复制调度、租约或审计规则；它仍不是已签名安装包。
- 预设任务（workload profile）是可选的持久化资源合同：明确 `profile_id` 的 GUI/MCP 认领只传配置与任务，服务在立即或排队后分配时自动激活。没有 profile 时，一次性认领只需任意非空项目标识、任务和 GPU 数量，任务名自动记录为用途；项目标识首次使用会自动登记为中性归属标签，不需要预创建、项目管理入口或服务器项目授权。申请不再要求预计占用时间，任务完成后释放租约；最大租约窗口是调度与未来预约共同遵守的硬边界。Agent 不得根据任务、目录或空闲容量自行挑选 profile，也不得推断项目或 GPU 数量。
- Broker 现在提供面向 Agent 的共享协调看板：不需要专门调度 Agent。`gpu_coordination` 一次返回每台服务器由谁租用、项目/任务、总卡/空卡/租约卡、受管运行与空租约、未归属进程、队列压力和事实性显存/利用率；`gpu_bind_observed_workload(agent_name, lease_id)` 将已启动的、当前采集到的 lease 进程登记为受管（`run_id` 可省略），不会启动或停止远端任务。采集端以小范围容忍 `ps etimes` 推算启动时刻的一秒级抖动，避免长任务因观测误差丢失归属；真实归属冲突可由租约所有者用同一动作基于当前观测自助恢复。
- GUI 已采用浅色 Apple Home 式资源空间：真实机房环境背景、玻璃侧栏、Apple 系统字体层级与可筛选状态分类；首屏默认以集群聚合行和显存/利用率数据条展示调度，单 GPU 对象卡片按需展开；图标按 SF Symbols 的简化、同权重和系统强调原则统一映射到本地 Phosphor 资源。
- Collector 同时展示每台服务器的 CPU 核数、1 分钟负载及系统内存可用量/总量；这些观测随 REST 与 MCP 快照返回，供 Agent 选择 GPU 所在服务器时参考。
- 外部 Agent 运行契约由 `gpu-broker` MCP instructions 提供；英文全局规则只是跨客户端的短适配块，仓库文档不进入其日常上下文。

## 验证快照

- 本轮完整 pytest、Ruff、diff 空白检查与 macOS 桌面构建已通过；Windows launcher 的路径和默认 inventory 初始化有单元测试覆盖。Windows `.exe` 仍需在 Windows 上运行 `desktop\build-windows-app.ps1` 产出。
- 两次人工只读 shadow 和一次 5 分钟轻量化冒烟已完成；非托管进程与失效 endpoint 均正确闭锁。
- 上述结果是 2026-07-19 的历史证据，不代表当前 GPU 可用性。详细记录见 [归档](archive/IMPLEMENTATION_EVIDENCE_2026-07-19.md)。

## 未完成 gate

1. 完整工作日 shadow 与人工 `nvidia-smi` 对照。
2. 2 小时内存 soak 与 24 小时数据库上限观察。
3. 非 loopback 部署所需的 TLS、访问控制和持久服务管理。
4. 外部项目旧事实源改写及各项目规则接入。
5. dstack/Slurm、自动启停、抢占和其他远端运行时集成。
