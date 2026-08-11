# ServerPilot (`serverpilot` package)

- 本机 loopback GPU 协作控制面；GUI、CLI、MCP 共用 REST/领域逻辑。租约只协调归属，不授权启动或停止工作负载。
- `README.md` 是用户入口；当前交付与 gate 只写 `docs/IMPLEMENTATION_STATUS_zh.md`；全局 MCP 安装见 `docs/AGENT_MCP_zh.md`，外部 Agent 的运行契约以 MCP instructions 为准。
- 源码在 `src/serverpilot/`；`build/`、`dist/`、`*.egg-info/`、`state/` 是生成物或运行状态。`inventory.yaml` 仅描述静态资产，不能证明 GPU 可用。

## 实现边界

- `service.py` 拥有调度、租约、队列、状态和审计；`api.py` 组合接口；持久化契约在 `database.py`、`models.py`、`src/serverpilot/migrations/`。CLI 的运营命令和 MCP 必须走 REST；仅 `init`、`serve`、`backup`、`restore`、`collect once` 等本地维护入口可直接组合领域服务，不得直连 SQLite/SSH 或复制领域规则。
- Collector 只能执行固定只读 SSH 探针；不得接收 shell、读取私钥、完整命令或环境，也不得改变远端运行时。
- GPU UUID 与 endpoint `id` 是身份边界；同 IP 不同端口不可合并。telemetry/采集异常、非托管进程、维护或冲突一律 fail closed。
- `project_id + task_ref` 是工作任务的稳定身份。Agent 间协调只走 MCP：Agent 应自动登记自身可用的 Codex task URI（如 `codex://threads/<thread-id>`），协调读面应把对方的 Actor 身份与该 URI 一并返回，接收方再通过 Codex 直接联系。ServerPilot GUI 不实现消息、收件箱、未读状态或通信表单，也不要求用户手填协调 URI；在 MCP 尚未返回 URI 时不得虚构联系入口。
- 非 loopback、访问控制、远端运行时或自动 allocator 的开放须单独批准。
- 测试和迁移使用临时数据库与 fake provider，不碰实时 `state/`；迁移不得覆盖活动数据库。

## 修改与验证

- Python 3.12；依赖改动同步 `uv.lock`。持久化或公共行为改动同步 migration、文档和测试。
- 运行最贴近改动面的测试与 Ruff；除非用户明确要求只读 shadow 采集，否则不得连接真实 GPU。
- 仓库中的唯一桌面应用固定为根目录 `ServerPilot.app`。不得在 `dist/`、`build/`、`~/Applications` 或仓库其他位置创建、复制或保留第二个 `ServerPilot*.app`，也不得用编号副本规避覆盖。
- 每次桌面端修改后必须运行 `zsh desktop/build-macos-app.sh`，该脚本原地替换根目录 `ServerPilot.app`；随后运行 `zsh desktop/verify-macos-app.sh`。评审和启动一律使用根目录这一份，不能打开旧路径或缓存副本。

<!-- TEAMWORK_PROJECT_START -->
## Teamwork Project Instructions

- Project label (local routing only): `serverpilot`.
- Read `docs/teamwork/index.json` first before choosing Teamwork memory routes.
- Normal Teamwork workflow writes use case-v2 only; legacy-v1 and old collaboration modes are migration inputs, not runtime routes.
- For Collaborate dialogue, brainstorm, and challenge checkpoints, use the selected v2 case manifest and `live/collaborate.md`; accepted decisions use `decision.md`. Route both through `case-inspect`, `case-schema`, and `case-apply`; never mirror them into ordinary memory, legacy Discussion/Design, or a report.
- For ordinary durable memory, follow the relevant case manifest. Keep volatile progress in its actual artifact.
<!-- TEAMWORK_PROJECT_END -->
