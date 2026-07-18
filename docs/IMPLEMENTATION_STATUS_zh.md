# gpu-broker 实施状态

更新时间：2026-07-19（Asia/Shanghai）

## 当前状态

- 本机功能版已交付：GUI、CLI 和 MCP 共用 REST 领域逻辑，可查看、认领、排队、预约和释放 GPU。
- 默认无登录且仅监听 loopback；Collector 固定只读，异常、失效 telemetry 和非托管进程均 fail closed。
- 当前开放的是协作租约；租约本身不授权远端执行，也不启动、停止或抢占工作负载。自动 allocator 与远端生命周期管理未开放。
- GUI 支持首次 SSH 接入预览；粘贴内容只解析、不执行，并拒绝远端命令、管道、跳板和密钥参数。
- GUI 已采用浅色 Apple Home 式资源空间：真实机房环境背景、玻璃侧栏、Apple 系统字体层级与可筛选状态分类；首屏默认以集群聚合行和显存/利用率数据条展示调度，单 GPU 对象卡片按需展开；图标按 SF Symbols 的简化、同权重和系统强调原则统一映射到本地 Phosphor 资源。
- 外部 Agent 运行契约由全局 MCP instructions 提供；仓库文档不进入其日常上下文。

## 验证快照

- 本轮 GUI/API 聚焦测试、JavaScript 语法检查与 Ruff 已通过；更广的历史验证见归档。
- 两次人工只读 shadow 和一次 5 分钟轻量化冒烟已完成；非托管进程与失效 endpoint 均正确闭锁。
- 上述结果是 2026-07-19 的历史证据，不代表当前 GPU 可用性。详细记录见 [归档](archive/IMPLEMENTATION_EVIDENCE_2026-07-19.md)。

## 未完成 gate

1. 完整工作日 shadow 与人工 `nvidia-smi` 对照。
2. 2 小时内存 soak 与 24 小时数据库上限观察。
3. 非 loopback 部署所需的 TLS、访问控制和持久服务管理。
4. 外部项目旧事实源改写及各项目规则接入。
5. dstack/Slurm、自动启停、抢占和其他远端运行时集成。
