# Adapter 能力与边界

GPU Broker 用 adapter 接入不同的观测和调度实现，但 adapter 不是第二个控制面。
资源身份、容量、认领、队列、租约、审计和准入判断始终由 `BrokerService` 持有。

## 当前注册能力

| Adapter | 能力 | 作用 | 不负责 |
| --- | --- | --- | --- |
| `raw-ssh` | `observation` | 仅运行代码内固定的主机/GPU telemetry probe，或以已观测正 PID 构成的固定 `ps` 详情 probe | 任意 shell、环境变量、私钥读取、lease/claim 写入 |
| `slurm-command` | `scheduler` | 执行代码固定的 Slurm 查询、提交、取消和上传路径 | 把 SchedulerTarget 变成 Endpoint、分配裸机 GPU、绕过 approval |

Registry 也是封闭的：未知 adapter 或不存在的 capability 会 fail closed。它并不从配置读取
`argv`、shell、环境或 Agent target。固定 operation 只有带输入/输出 schema 与审批要求的已注册
操作；目前只为既有 Slurm contract 保留元数据，未新增通用 REST 或 MCP 执行入口。

`raw-ssh` 的公开执行入口也不接收命令字符串：它只接受 `endpoint-telemetry` 与
`process-details` 两个 probe 标识；后者仅可使用采集到的正整数 PID。这样即使未来调用点变多，
配置、Agent 输入和 endpoint 元数据也不能成为远程 shell 内容。

## 预留能力

- `provisioning-preview` 只能生成版本化、不可执行的部署说明或预览。
- `operation` 必须是固定 ID、代码内固定参数形状、明确 approval、审计和不确定结果语义的操作。

这两项不是远端执行授权。任何实际 provisioning 或新的 mutating operation 必须先有独立的
业务合同和当前任务批准。

## 对 Agent 和 UI 的影响

Agent 与 UI 继续使用统一的资源监控、claim、queue、reservation 和 scheduler 合同，不需要选择
vendor adapter，也不需要额外 discovery/configuration 往返。adapter ID 只可作为诊断 provenance
显示，不能作为资源身份；endpoint 仍由 endpoint `id` 标识，GPU 仍由
`endpoint_id:gpu_uuid` 标识。

## 明确禁止

- adapter 获取 `Session`、`BrokerService`、`ActorContext` 或 lease/claim repository；
- adapter 创建、修改或释放 lease、claim、reservation；
- 配置驱动的任意 `argv`、shell、环境、Agent target 或通用 `execute()`；
- 为 adapter 引入第二套认证、注册 token、匿名 observer API 或非 loopback listener；
- 将 SchedulerTarget 合并为普通 Endpoint。
