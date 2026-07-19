# gpu-broker

- 本机 loopback GPU 协作控制面；GUI、CLI、MCP 共用 REST/领域逻辑。租约只协调归属，不授权启动或停止工作负载。
- `README.md` 是用户入口；当前交付与 gate 只写 `docs/IMPLEMENTATION_STATUS_zh.md`；全局 MCP 安装见 `docs/AGENT_MCP_zh.md`，外部 Agent 的运行契约以 MCP instructions 为准。
- 源码在 `src/gpu_broker/`；`build/`、`dist/`、`*.egg-info/`、`state/` 是生成物或运行状态。`inventory.yaml` 仅描述静态资产，不能证明 GPU 可用。
- 结构查询优先 CodeGraph，字面查询用 `rg`；不手改 `.codegraph/` 或生成的 CodeGraph 指引。

## 实现边界

- `service.py` 拥有调度、租约、队列、状态和审计；`api.py` 组合接口；持久化契约在 `database.py`、`models.py`、`src/gpu_broker/migrations/`。CLI 的运营命令和 MCP 必须走 REST；仅 `init`、`serve`、`backup`、`restore`、`collect once` 等本地维护入口可直接组合领域服务，不得直连 SQLite/SSH 或复制领域规则。
- Collector 只能执行固定只读 SSH 探针；不得接收 shell、读取私钥、完整命令或环境，也不得改变远端运行时。
- GPU UUID 与 endpoint `id` 是身份边界；同 IP 不同端口不可合并。telemetry/采集异常、非托管进程、维护或冲突一律 fail closed。
- 非 loopback、访问控制、远端运行时或自动 allocator 的开放须单独批准。
- 测试和迁移使用临时数据库与 fake provider，不碰实时 `state/`；迁移不得覆盖活动数据库。

## 修改与验证

- Python 3.12；依赖改动同步 `uv.lock`。持久化或公共行为改动同步 migration、文档和测试。
- 运行最贴近改动面的测试与 Ruff；除非用户明确要求只读 shadow 采集，否则不得连接真实 GPU。
- 修改桌面壳或打包路径时运行 `zsh desktop/build-macos-app.sh`；`dist/GPU Broker.app` 只作为构建产物。
